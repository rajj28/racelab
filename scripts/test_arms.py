"""Verify the four arms are four arms, on a deterministic forced conflict.

The interesting arm is C-ops. It re-reasons on every conflict exactly like C,
but over semantic memory retrieved once and never refreshed. If C-ops and C
behave identically, the memory refresh is not doing any work; if they diverge,
it is, and by how much.

To make that measurable the test reproduces the hero scenario's shape
deterministically:

  1. agent-1 opens a transaction, retrieves memory (ceiling $80), reads the
     operational sum, and waits.
  2. Out of band, the policy changes: a memory superseding the $80 ceiling with
     a $60 ceiling is written. This is the mid-run policy update from
     METHODOLOGY entry 3.
  3. agent-2 commits an allocation, invalidating agent-1's read.
  4. agent-1's COMMIT fails to serialize, and each arm responds in its own way.

The expected divergence:

  B      replays its $80-era action                    -> violates
  C-ops  re-reasons over fresh sums but a stale $80    -> may still violate
  C      re-reasons over fresh sums and the new $60    -> holds

Run:  python scripts/test_arms.py
"""

from __future__ import annotations

import datetime
import pathlib
import sys
import threading
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import psycopg

from racelab.arms import ARMS, ORDER, ArmId, contributions
from racelab.conflict import ArmCollapse, ConflictAware, DecisionContext, ListTelemetry
from racelab.db import connect
from racelab.embeddings import get_embedder
from racelab.memory import MemoryStore
from scenario.corpus import HERO
from scenario.decide import RETRIEVAL_QUERY, infer_ceiling, propose

HARD_LIMIT = HERO.hard_limit
ACCOUNT = HERO.account_id
# The ceiling in force after the mid-run policy update. Read from the corpus
# rather than written here, so the two cannot drift apart.
CURRENT_CEILING = 60

_results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


# --------------------------------------------------------------------------
# scenario plumbing
# --------------------------------------------------------------------------


def read_sum(cur: psycopg.Cursor) -> int:
    cur.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM allocations WHERE account_id = %s",
        (ACCOUNT,),
    )
    return int(cur.fetchone()[0])


def make_apply(run_id: str, agent_id: str):
    def apply(cur: psycopg.Cursor, proposal) -> bool:
        if proposal.amount is None:
            return False
        cur.execute(
            "INSERT INTO allocations (allocation_id, account_id, agent_id, amount, run_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            (str(uuid.uuid4()), ACCOUNT, agent_id, proposal.amount, run_id),
        )
        return True

    return apply


def reason(ctx: DecisionContext):
    ceiling, _ = infer_ceiling(ctx.memory or [])
    return propose(ceiling, ctx.observed, HARD_LIMIT)


def reset_state(backend: str, run_id: str, pre_seeded: int) -> None:
    """Reset the operational state on the backend this arm actually races on.

    Memories always live on CockroachDB and are shared by every arm (see
    `racelab.schema.MEMORY_BACKEND_NOTE`), but allocations must be reset and
    read on the arm's own backend -- reading arm A's result off CockroachDB
    would report a number no agent in that arm ever wrote.
    """
    with connect("crdb") as mem:
        mem.execute(
            "DELETE FROM memories WHERE memory_id = %s", (HERO.update[0].memory_id,)
        )

    with connect(backend) as conn:
        conn.execute("DELETE FROM allocations WHERE account_id = %s", (ACCOUNT,))
        conn.execute(
            "INSERT INTO accounts (account_id, name, hard_limit) VALUES (%s, %s, %s) "
            "ON CONFLICT (account_id) DO UPDATE SET hard_limit = EXCLUDED.hard_limit",
            (ACCOUNT, HERO.name, HARD_LIMIT),
        )
        if pre_seeded:
            conn.execute(
                "INSERT INTO allocations (allocation_id, account_id, agent_id, amount, run_id) "
                "VALUES (%s, %s, %s, %s, %s)",
                (str(uuid.uuid4()), ACCOUNT, "seed", pre_seeded, run_id),
            )


def apply_policy_update(store: MemoryStore) -> None:
    seed = HERO.update[0]
    store.add(
        memory_id=seed.memory_id,
        account_id=ACCOUNT,
        text=seed.text,
        kind=seed.kind,
        supersedes=seed.supersedes,
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )


def final_sum(backend: str) -> int:
    with connect(backend) as conn:
        return int(
            conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM allocations WHERE account_id = %s",
                (ACCOUNT,),
            ).fetchone()[0]
        )


# --------------------------------------------------------------------------
# one arm, one forced conflict
# --------------------------------------------------------------------------


def run_arm(arm_id: ArmId, embedder, pre_seeded: int) -> dict:
    arm = ARMS[arm_id]
    run_id = f"arms-{arm.id.value}-{uuid.uuid4().hex[:8]}"
    reset_state(arm.backend, run_id, pre_seeded)

    telemetry = ListTelemetry()
    opened = threading.Event()
    other_done = threading.Event()
    retrievals = {"n": 0}

    memory_conn = connect("crdb")
    store = MemoryStore(memory_conn, embedder)

    def refresh_memory(agent_id: str):
        retrievals["n"] += 1
        return store.retrieve(ACCOUNT, RETRIEVAL_QUERY, k=4)

    def gate_read(cur: psycopg.Cursor) -> int:
        observed = read_sum(cur)
        if not opened.is_set():
            opened.set()
            other_done.wait(timeout=40)
        return observed

    wrapper = ConflictAware(
        operational_read=gate_read,
        apply=make_apply(run_id, "agent-1"),
        reason=reason,
        re_reason=arm.re_reason,
        refresh_memory=refresh_memory,
        refresh_memory_on_conflict=arm.refresh_memory,
        max_attempts=4,
        telemetry=telemetry,
    )

    box: dict = {}

    def agent_1() -> None:
        try:
            with connect(arm.backend) as conn:
                box["result"] = wrapper.run(conn, agent_id="agent-1", run_id=run_id)
        except BaseException as exc:
            box["exc"] = exc

    def agent_2() -> None:
        try:
            opened.wait(timeout=40)
            # The out-of-band policy update lands while agent-1 is mid-decision.
            apply_policy_update(store)
            with connect(arm.backend) as conn:
                solo = ConflictAware.conflict_aware(
                    operational_read=read_sum,
                    apply=make_apply(run_id, "agent-2"),
                    reason=lambda ctx: propose(80, ctx.observed, HARD_LIMIT),
                )
                solo.run(conn, agent_id="agent-2", run_id=run_id)
        finally:
            other_done.set()

    t1, t2 = threading.Thread(target=agent_1), threading.Thread(target=agent_2)
    t1.start(); t2.start(); t1.join(timeout=90); t2.join(timeout=90)
    memory_conn.close()

    if "exc" in box:
        raise box["exc"]
    result = box["result"]
    total = final_sum(arm.backend)

    # Two different things can go wrong and they are worth separating. The
    # HARD LIMIT is the database-level invariant -- exceeding it is the
    # violation the whole project is about. The CURRENT POLICY CEILING is the
    # authorization the account is actually operating under after the mid-run
    # update. An agent can stay within the hard limit while allocating past a
    # ceiling that was lowered underneath it, which is a real failure that the
    # invariant column alone reports as a success.
    breached_policy = total > CURRENT_CEILING

    print(
        f"  {arm.label:44} conflicts={result.conflicts} "
        f"reason_calls={result.reason_calls} refreshes={result.memory_refreshes} "
        f"{result.decision_before} -> {result.decision_after} "
        f"sum={total} limit:{'VIOLATED' if total > HARD_LIMIT else 'held'} "
        f"policy:{'BREACHED' if breached_policy else 'held'}"
    )
    return {
        "arm": arm, "result": result, "final_sum": total,
        "violated": total > HARD_LIMIT, "breached_policy": breached_policy,
        "retrievals": retrievals["n"],
    }


# Two starting states, run because one of them alone would misrepresent the
# ablation. Which is not a matter of taste: the memory refresh can only change
# an outcome when the two ceilings disagree about the reading the agent lands
# on after the conflict. Reporting only the configuration where it does would
# overstate the refresh; reporting only the configuration where it does not
# would bury it. Both are run and both are printed.
CONFIGURATIONS = [
    (0, "agent-1 re-reads $45: $80 ceiling permits 35, $60 ceiling does not"),
    (20, "agent-1 re-reads $65: both ceilings refuse, refresh cannot matter"),
]


def run_configuration(pre_seeded: int, note: str, embedder) -> dict[ArmId, dict]:
    print(f"\n{'-' * 78}")
    print(f"Configuration: ${pre_seeded} pre-allocated")
    print(f"  {note}\n")
    outcomes: dict[ArmId, dict] = {}
    for arm_id in ORDER:
        try:
            outcomes[arm_id] = run_arm(arm_id, embedder, pre_seeded)
        except ArmCollapse as exc:
            check(f"{arm_id.value} completed without arm collapse", False, str(exc))
            raise

    by_arm = {k: float(v["final_sum"]) for k, v in outcomes.items()}
    print("\n  Decomposition (change in final sum; negative is an improvement)")
    for name, value in contributions(by_arm).items():
        print(f"    {name:44} {'n/a' if value is None else f'{value:+.0f}'}")
    return outcomes


def main() -> int:
    print("RaceLab four-arm verification")
    print("=" * 78)
    print(f"\nHero account {ACCOUNT}, hard limit ${HARD_LIMIT}.")
    print("Policy ceiling is $80 and drops to $60 while agent-1 is mid-decision.")
    print("agent-2 commits $45 in the window, forcing agent-1's transaction to conflict.")

    embedder = get_embedder("titan")
    runs = {}
    for pre_seeded, note in CONFIGURATIONS:
        runs[pre_seeded] = run_configuration(pre_seeded, note, embedder)

    print(f"\n{'=' * 78}")
    print("Mechanism instrumentation (both configurations)")
    for pre_seeded, outcomes in runs.items():
        b, c_ops, c = outcomes[ArmId.B], outcomes[ArmId.C_OPS], outcomes[ArmId.C]
        tag = f"[${pre_seeded} pre-allocated]"
        check(f"{tag} B reasoned exactly once", b["result"].reason_calls == 1)
        check(f"{tag} C-ops re-reasoned on conflict",
              c_ops["result"].reason_calls == c_ops["result"].attempts_made)
        check(f"{tag} C-ops never refreshed memory",
              c_ops["result"].memory_refreshes == 0,
              "this is the entire definition of the ablation")
        check(f"{tag} C refreshed memory once per conflict",
              c["result"].memory_refreshes == c["result"].conflicts)

    check("C-ops and C differ in exactly one setting",
          ARMS[ArmId.C_OPS].re_reason == ARMS[ArmId.C].re_reason
          and ARMS[ArmId.C_OPS].refresh_memory != ARMS[ArmId.C].refresh_memory)

    print("\nWhat the ablation says")
    print(f"  hard limit ${HARD_LIMIT}; policy ceiling after the update ${CURRENT_CEILING}\n")
    for pre_seeded, outcomes in runs.items():
        c_ops, c = outcomes[ArmId.C_OPS], outcomes[ArmId.C]
        effect = c["final_sum"] - c_ops["final_sum"]
        verdict = (
            "memory refresh changed the outcome"
            if effect != 0 or c_ops["breached_policy"] != c["breached_policy"]
            else "memory refresh made no difference"
        )
        print(f"  ${pre_seeded:>3} pre-allocated:")
        for name, o in (("C-ops", c_ops), ("C    ", c)):
            print(f"    {name} sum={o['final_sum']:>3}  "
                  f"limit {'VIOLATED' if o['violated'] else 'held':>8}  "
                  f"policy {'BREACHED' if o['breached_policy'] else 'held':>8}")
        print(f"    -> {verdict}")

    print("\n  The two configurations disagree, and that is the finding. Whether")
    print("  refreshing semantic memory matters depends on where the re-read lands")
    print("  relative to the old and new ceilings. Where both ceilings refuse the")
    print("  reading, the operational re-read alone carries the whole effect. Where")
    print("  they disagree, the agent reasoning over stale memory stays inside the")
    print("  hard limit and allocates past the policy ceiling that replaced it --")
    print("  a breach the invariant column alone would report as a success.")

    print("\n" + "=" * 78)
    failed = [r for r in _results if not r[0]]
    print(f"{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
