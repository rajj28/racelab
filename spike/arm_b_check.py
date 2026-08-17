"""Amendment 2: verify that arm B commits successfully AND violates the invariant.

This is a de-risking test, run before Phase 4 rather than after, because the
whole thesis depends on its outcome.

The concern it addresses is subtle and worth stating precisely. Under naive
retry, a transaction that fails to serialize is restarted, and the restarted
transaction re-executes `SELECT SUM(amount)`. So it *does* read fresh state. It
will therefore commit cleanly -- there is no second conflict to detect. The
problem is that the action it inserts was computed against the sum it read
BEFORE the conflict. The transaction is fresh; the decision is stale.

If that stale decision does not actually produce an invariant violation, then
naive retry is sufficient, arm C has nothing to improve on, and the project's
hypothesis is dead. So this must be measured, not assumed.

Arm C differs in exactly one respect: on a serialization failure it discards the
computed action, refreshes both semantic memory and the operational aggregate,
re-runs the reasoning step, and emits a possibly different action -- including
abstaining.

    python spike/arm_b_check.py
    python spike/arm_b_check.py --agents 20 --arrival-window-ms 400 --repeats 5
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import statistics
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field

import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from racelab.db import BACKENDS, dsn_for  # noqa: E402
from racelab.embeddings import get_embedder  # noqa: E402
from racelab.memory import MemoryStore  # noqa: E402
from scenario.corpus import HERO  # noqa: E402
from scenario.decide import RETRIEVAL_QUERY, infer_ceiling, propose  # noqa: E402

RESULTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "results" / "arm_check"

NAIVE = "naive"
CONFLICT_AWARE = "conflict-aware"


@dataclass
class AgentRecord:
    agent_id: str
    policy: str
    outcome: str = "unknown"
    attempts: int = 0
    conflicts: int = 0
    first_ceiling: int | None = None
    final_ceiling: int | None = None
    first_observed_sum: int | None = None
    final_observed_sum: int | None = None
    decision_before: str | None = None
    decision_after: str | None = None
    revised: bool = False
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class ArmResult:
    arm: str
    backend: str
    policy: str
    agents: int
    hard_limit: int
    arrival_window_ms: int
    final_sum: int
    invariant_violated: bool
    commits: int
    abstains: int
    conflicts: int
    revisions: int
    failures: int
    records: list[dict] = field(default_factory=list)


def reset_account(hard_limit: int, account_id: str, backends: tuple[str, ...]) -> None:
    for backend in backends:
        with psycopg.connect(dsn_for(backend), autocommit=True) as conn:
            conn.execute("DELETE FROM allocations WHERE account_id = %s", (account_id,))
            conn.execute(
                "INSERT INTO accounts (account_id, name, hard_limit) VALUES (%s, %s, %s) "
                "ON CONFLICT (account_id) DO UPDATE SET hard_limit = EXCLUDED.hard_limit",
                (account_id, HERO.name, hard_limit),
            )


def reset_memory(embed_provider: str | None, applied: bool) -> None:
    """Put the hero corpus into its pre-update or post-update state."""
    embedder = get_embedder(embed_provider)
    with psycopg.connect(dsn_for("crdb"), autocommit=True) as conn:
        store = MemoryStore(conn, embedder)
        if applied:
            import datetime

            now = datetime.datetime.now(datetime.timezone.utc)
            for mem in HERO.update:
                store.add(mem.memory_id, HERO.account_id, mem.text, mem.kind,
                          supersedes=mem.supersedes, created_at=now)
        else:
            for mem in HERO.update:
                conn.execute("DELETE FROM memories WHERE memory_id = %s", (mem.memory_id,))


def retrieve_ceiling(store: MemoryStore, account_id: str) -> tuple[int | None, list[str]]:
    mems = store.retrieve(account_id, RETRIEVAL_QUERY, k=4)
    ceiling, _ = infer_ceiling(mems)
    return ceiling, [m.memory_id for m in mems]


def agent(
    backend: str,
    policy: str,
    account_id: str,
    agent_id: str,
    hard_limit: int,
    run_id: str,
    arrival_delay_s: float,
    gap_ms: int,
    max_retries: int,
    embed_provider: str | None,
    out: list[AgentRecord],
    lock: threading.Lock,
) -> None:
    rec = AgentRecord(agent_id=agent_id, policy=policy)
    isolation = BACKENDS[backend]["isolation"]
    try:
        time.sleep(arrival_delay_s)

        # Memory lives on CockroachDB and is shared by every arm, so retrieval
        # is identical across arms and cannot confound the comparison.
        mem_conn = psycopg.connect(dsn_for("crdb"), autocommit=True)
        store = MemoryStore(mem_conn, get_embedder(embed_provider))
        conn = psycopg.connect(dsn_for(backend), autocommit=True)

        with mem_conn, conn:
            ceiling, _ = retrieve_ceiling(store, account_id)
            rec.first_ceiling = ceiling
            rec.final_ceiling = ceiling
            decision = None

            for attempt in range(max_retries + 1):
                rec.attempts = attempt + 1
                try:
                    conn.execute(f"BEGIN TRANSACTION ISOLATION LEVEL {isolation}")
                    observed = int(conn.execute(
                        "SELECT COALESCE(SUM(amount), 0) FROM allocations "
                        "WHERE account_id = %s",
                        (account_id,),
                    ).fetchone()[0])

                    if attempt == 0:
                        rec.first_observed_sum = observed
                    rec.final_observed_sum = observed

                    if decision is None:
                        # Reason against what we just read. `decision_before`
                        # is written once and never again: it has to preserve
                        # the ORIGINAL action so that comparing it to
                        # `decision_after` says whether the agent changed its
                        # mind. Overwriting it here makes `revised` permanently
                        # false and quietly turns the experiment into a count of
                        # database retries, which is the one thing it must not
                        # become.
                        decision = propose(ceiling, observed, hard_limit)
                        if rec.decision_before is None:
                            rec.decision_before = decision.action
                        rec.decision_after = decision.action
                    # On a retry the naive policy deliberately does NOT recompute
                    # here. `decision` still holds the action computed against the
                    # pre-conflict sum, and that is the whole point of the arm.

                    if gap_ms:
                        time.sleep(gap_ms / 1000.0)

                    if decision.is_abstain:
                        conn.execute("COMMIT")
                        rec.outcome = "abstained"
                        break

                    conn.execute(
                        "INSERT INTO allocations "
                        "(allocation_id, account_id, agent_id, amount, run_id) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (str(uuid.uuid4()), account_id, agent_id, decision.amount, run_id),
                    )
                    conn.execute("COMMIT")
                    rec.outcome = "committed"
                    break

                except psycopg.Error as exc:
                    sqlstate = getattr(exc, "sqlstate", None)
                    try:
                        conn.execute("ROLLBACK")
                    except psycopg.Error:
                        pass

                    if sqlstate != "40001":
                        rec.outcome = "error"
                        rec.error_code = sqlstate
                        rec.error_message = str(exc).splitlines()[0][:200]
                        break

                    rec.conflicts += 1
                    if attempt >= max_retries:
                        rec.outcome = "exhausted"
                        rec.error_code = sqlstate
                        break

                    if policy == CONFLICT_AWARE:
                        # Treat the serialization failure as an invalidation of
                        # the reasoning, not merely of the transaction: discard
                        # the action, refresh BOTH semantic memory and the
                        # operational aggregate, and reason again.
                        ceiling, _ = retrieve_ceiling(store, account_id)
                        rec.final_ceiling = ceiling
                        decision = None
                    # naive: keep `decision` as-is and simply run the
                    # transaction again. This is what standard retry middleware
                    # does, and it is a reasonable thing to build.

            if decision is not None:
                rec.decision_after = decision.action
                rec.revised = (rec.decision_before != rec.decision_after)

    except Exception as exc:  # noqa: BLE001
        rec.outcome = "error"
        rec.error_message = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        with lock:
            out.append(rec)


def run_arm(
    arm: str,
    backend: str,
    policy: str,
    agents: int,
    hard_limit: int,
    arrival_window_ms: int,
    gap_ms: int,
    max_retries: int,
    seed: int,
    embed_provider: str | None,
    update_after_ms: int | None,
) -> ArmResult:
    account_id = HERO.account_id
    run_id = f"armchk-{arm}-{uuid.uuid4().hex[:8]}"

    reset_account(hard_limit, account_id, (backend,) if backend == "crdb" else (backend, "crdb"))
    reset_memory(embed_provider, applied=False)

    rng = random.Random(seed)
    delays = [rng.uniform(0, arrival_window_ms) / 1000.0 for _ in range(agents)]

    records: list[AgentRecord] = []
    lock = threading.Lock()
    threads = [
        threading.Thread(
            target=agent,
            args=(backend, policy, account_id, f"agent-{i:02d}", hard_limit, run_id,
                  delays[i], gap_ms, max_retries, embed_provider, records, lock),
            name=f"agent-{i:02d}",
        )
        for i in range(agents)
    ]

    updater = None
    if update_after_ms is not None:
        def _apply_update():
            time.sleep(update_after_ms / 1000.0)
            reset_memory(embed_provider, applied=True)

        updater = threading.Thread(target=_apply_update, name="policy-update")

    for t in threads:
        t.start()
    if updater:
        updater.start()
    for t in threads:
        t.join(timeout=240)
    if updater:
        updater.join(timeout=60)

    with psycopg.connect(dsn_for(backend), autocommit=True) as conn:
        final_sum = int(conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM allocations WHERE account_id = %s",
            (account_id,),
        ).fetchone()[0])

    return ArmResult(
        arm=arm,
        backend=backend,
        policy=policy,
        agents=agents,
        hard_limit=hard_limit,
        arrival_window_ms=arrival_window_ms,
        final_sum=final_sum,
        invariant_violated=final_sum > hard_limit,
        commits=sum(1 for r in records if r.outcome == "committed"),
        abstains=sum(1 for r in records if r.outcome == "abstained"),
        conflicts=sum(r.conflicts for r in records),
        revisions=sum(1 for r in records if r.revised),
        failures=sum(1 for r in records if r.outcome in ("error", "exhausted", "unknown")),
        records=[asdict(r) for r in sorted(records, key=lambda r: r.agent_id)],
    )


def print_result(res: ArmResult) -> None:
    print(
        f"  {res.arm:<28} sum={res.final_sum:<5} limit={res.hard_limit:<5} "
        f"violated={'YES' if res.invariant_violated else 'no ':<3} "
        f"commits={res.commits:<3} abstains={res.abstains:<3} "
        f"conflicts={res.conflicts:<3} revisions={res.revisions:<3} "
        f"failures={res.failures}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agents", type=int, default=20)
    ap.add_argument("--hard-limit", type=int, default=100)
    ap.add_argument("--arrival-window-ms", type=int, default=400,
                    help="agents arrive uniformly over this window (0 = simultaneous)")
    ap.add_argument("--gap-ms", type=int, default=50)
    ap.add_argument("--max-retries", type=int, default=5)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--seed", type=int, default=20260814)
    ap.add_argument("--embed-provider", default=None)
    ap.add_argument("--update-after-ms", type=int, default=150,
                    help="apply the superseding policy memory this long into the run")
    args = ap.parse_args()

    arms = [
        ("A  postgres / naive", "pg", NAIVE),
        ("B  cockroach / naive", "crdb", NAIVE),
        ("C  cockroach / conflict-aware", "crdb", CONFLICT_AWARE),
    ]

    all_results: dict[str, list[ArmResult]] = {name: [] for name, _, _ in arms}
    for rep in range(args.repeats):
        print(f"\nrepeat {rep + 1}/{args.repeats}  "
              f"(agents={args.agents}, arrival window={args.arrival_window_ms}ms, "
              f"hard limit={args.hard_limit})")
        for name, backend, policy in arms:
            res = run_arm(
                arm=name, backend=backend, policy=policy, agents=args.agents,
                hard_limit=args.hard_limit, arrival_window_ms=args.arrival_window_ms,
                gap_ms=args.gap_ms, max_retries=args.max_retries,
                seed=args.seed + rep, embed_provider=args.embed_provider,
                update_after_ms=args.update_after_ms,
            )
            all_results[name].append(res)
            print_result(res)

    print("\n" + "=" * 78)
    print("summary across repeats")
    print("=" * 78)
    for name, _, _ in arms:
        rows = all_results[name]
        viol = sum(1 for r in rows if r.invariant_violated)
        print(
            f"  {name:<28} violated {viol}/{len(rows)}  "
            f"mean sum={statistics.mean(r.final_sum for r in rows):.1f}  "
            f"mean conflicts={statistics.mean(r.conflicts for r in rows):.1f}  "
            f"mean revisions={statistics.mean(r.revisions for r in rows):.1f}"
        )

    b = all_results["B  cockroach / naive"]
    c = all_results["C  cockroach / conflict-aware"]
    b_viol = sum(1 for r in b if r.invariant_violated)
    c_viol = sum(1 for r in c if r.invariant_violated)

    print("\nAmendment 2 check -- does arm B commit AND violate?")
    b_commits = sum(r.commits for r in b)
    print(f"  arm B committed {b_commits} allocations across {len(b)} runs")
    print(f"  arm B violated the invariant in {b_viol}/{len(b)} runs")
    print(f"  arm C violated the invariant in {c_viol}/{len(c)} runs")
    if b_viol == 0:
        print("\n  ARM B DID NOT VIOLATE. The thesis is at risk -- naive retry was")
        print("  sufficient here. Do not proceed to Phase 4 without addressing this.")
    elif c_viol >= b_viol:
        print("\n  Arm C did not improve on arm B. Report the honest numbers.")
    else:
        print("\n  Arm B commits cleanly and still violates; arm C does not.")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / "arm_b_check.json"
    path.write_text(json.dumps(
        {"args": vars(args), "results": {k: [asdict(r) for r in v] for k, v in all_results.items()}},
        indent=2, default=str), encoding="utf-8")
    print(f"\n  -> {path}")
    return 0 if b_viol > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
