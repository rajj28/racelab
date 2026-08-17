"""The swept experiment: four arms, staggered arrival, mid-run policy change.

One run is: `agent_count` agents arriving over `arrival_window_ms`, each
retrieving memory, opening a transaction, reading the operational sum,
deciding, writing, and committing. Part-way through, an out-of-band policy
update lowers the authorization ceiling from $80 to $60 (METHODOLOGY entry 3),
so agents that started before it and re-decide after it can see a different
ceiling.

Two things are measured per run, and they are co-primary:

  hard-limit violation   final sum > accounts.hard_limit
  policy-ceiling breach  final sum > the ceiling in force at the end of the run

They fail for different reasons. The hard limit is a column in the database and
re-reading operational state recovers it. The policy ceiling exists only as a
retrieved memory, and nothing in the operational state can tell an agent that it
moved.

Every run also records the operational sums observed at each *re-decision*, so
the pre-registered ablation boundary in METHODOLOGY entry 8 can be checked
against what actually happened rather than argued for afterwards.

Arms are held identical in everything except backend, isolation and policy. In
particular a given seed produces the same agent set and the same arrival offsets
in all four arms, so the arms see the same workload.
"""

from __future__ import annotations

import concurrent.futures as cf
import dataclasses
import datetime
import random
import threading
import time
import uuid
from dataclasses import dataclass, field

import psycopg

from .arms import ARMS, Arm, ArmId
from .conflict import ArmCollapse, ConflictAware, DecisionContext, ListTelemetry
from .db import connect
from .memory import MemoryStore


@dataclass(frozen=True)
class Scenario:
    account_id: str
    hard_limit: int
    stale_ceiling: int
    current_ceiling: int
    retrieval_query: str
    update_memory: object  # MemorySeed for the superseding policy


@dataclass
class RunConfig:
    arm: Arm
    scenario: Scenario
    seed: int
    agent_count: int = 20
    arrival_window_ms: int = 1500
    reasoning_gap_ms: float = 200.0
    # Where in the arrival window the out-of-band policy update lands. 0.5 puts
    # it halfway, so roughly half the agents begin under the old ceiling.
    policy_update_at: float = 0.5
    max_attempts: int = 5


@dataclass
class RunOutcome:
    run_id: str
    arm_id: ArmId
    seed: int
    arrival_window_ms: int
    agent_count: int
    final_sum: int
    hard_limit: int
    current_ceiling: int
    committed: int = 0
    abstained: int = 0
    conflicts: int = 0
    revisions: int = 0
    reason_calls: int = 0
    memory_refreshes: int = 0
    errors: int = 0
    exhausted: int = 0
    # Operational sums observed at each re-decision. The pre-registered
    # ablation boundary is a claim about these, so they are kept.
    redecision_reads: list[int] = field(default_factory=list)
    voided: bool = False
    void_reason: str | None = None

    @property
    def violated_hard_limit(self) -> bool:
        return self.final_sum > self.hard_limit

    @property
    def breached_policy(self) -> bool:
        return self.final_sum > self.current_ceiling


def arrival_offsets(seed: int, agent_count: int, window_ms: int) -> list[float]:
    """Seeded arrival offsets, identical across arms for a given seed.

    Uniform over [0, window_ms). A seed fixes the workload; it does not fix the
    interleaving, which distributed scheduling legitimately varies. See the
    determinism claim in METHODOLOGY.
    """
    rng = random.Random(seed)
    return sorted(rng.uniform(0, window_ms) / 1000.0 for _ in range(agent_count))


def reset_run_state(config: RunConfig) -> None:
    """Clear allocations on the racing backend, remove the superseding memory."""
    sc = config.scenario
    with connect("crdb") as mem:
        mem.execute("DELETE FROM memories WHERE memory_id = %s",
                    (sc.update_memory.memory_id,))
    with connect(config.arm.backend) as conn:
        conn.execute("DELETE FROM allocations WHERE account_id = %s", (sc.account_id,))
        conn.execute(
            "INSERT INTO accounts (account_id, name, hard_limit) VALUES (%s, %s, %s) "
            "ON CONFLICT (account_id) DO UPDATE SET hard_limit = EXCLUDED.hard_limit",
            (sc.account_id, sc.account_id, sc.hard_limit),
        )


def run_once(config: RunConfig, embedder, reason_for, memory_pool=None,
             observer=None) -> RunOutcome:
    """One run of one arm.

    `reason_for(memories, observed, hard_limit) -> Decision` is injected so the
    same harness drives the deterministic reference and the cached model
    intents without knowing which it has.

    `observer` is optional instrumentation, used to build the inspection UI. It
    changes nothing about what is measured -- no arm, window, metric or run
    depends on it -- it only reports moments the aggregate `RunOutcome` throws
    away: when the threads were released, each decision as it was made, the
    policy update landing, and each agent's final result. Any of its four hooks
    may be absent.
    """
    arm, sc = config.arm, config.scenario
    run_id = f"{arm.id.value}-w{config.arrival_window_ms}-s{config.seed}-{uuid.uuid4().hex[:6]}"
    reset_run_state(config)

    outcome = RunOutcome(
        run_id=run_id, arm_id=arm.id, seed=config.seed,
        arrival_window_ms=config.arrival_window_ms, agent_count=config.agent_count,
        final_sum=0, hard_limit=sc.hard_limit, current_ceiling=sc.current_ceiling,
    )

    offsets = arrival_offsets(config.seed, config.agent_count, config.arrival_window_ms)
    telemetry = ListTelemetry()
    lock = threading.Lock()
    started = threading.Event()
    results: list = []

    # Everything below is set up *before* the threads are released, because
    # anything paid after that point is charged against the arrival window and
    # silently competes with the policy update. The first sweep lost its memory
    # ablation to exactly this: a per-agent TLS handshake to CockroachDB Cloud
    # costs ~391 ms, it sat in front of the first retrieval, and the update
    # therefore landed before any agent could read stale memory. METHODOLOGY
    # entry 10 has the measurements.
    #
    # Racing connections are still strictly one per agent -- pre-opening them
    # changes when they are created, not whether they are shared.
    # Opened concurrently: twenty sequential 391 ms handshakes is eight seconds
    # of setup per run, which is dead time in every cell of the sweep. Doing
    # them in parallel changes only how long setup takes, not what the agents
    # get -- each still receives its own private connection, and all of them
    # still exist before any thread is released.
    with cf.ThreadPoolExecutor(max_workers=config.agent_count + 1) as pre:
        conn_futures = [pre.submit(connect, arm.backend)
                        for _ in range(config.agent_count)]
        updater_future = pre.submit(connect, "crdb")
        agent_conns = [f.result() for f in conn_futures]
        updater_conn = updater_future.result()
    # The updater gets its own connection rather than a pooled lease: it was
    # contending with twenty agents for six pooled connections, which made its
    # write land up to 900 ms after its scheduled time.
    embedder.embed(sc.update_memory.text)  # warm the cache off the critical path

    def _notify(hook: str, **kw) -> None:
        """Call an observer hook if it exists. Instrumentation must never be able
        to fail a run, so a broken observer is swallowed rather than propagated
        into the measurement."""
        if observer is None:
            return
        fn = getattr(observer, hook, None)
        if fn is None:
            return
        try:
            with lock:
                fn(**kw)
        except Exception:  # noqa: BLE001 - instrumentation is not the experiment
            pass

    def agent(index: int, offset: float) -> None:
        started.wait()
        time.sleep(offset)
        agent_id = f"agent-{index:02d}"

        def refresh_memory(_agent_id: str):
            # Pooled, and never the racing connection. Retrieval happens before
            # BEGIN and after ROLLBACK, never inside a transaction, so it cannot
            # join a refresh span.
            with memory_pool.lease() as mem_conn:
                return MemoryStore(mem_conn, embedder).retrieve(
                    sc.account_id, sc.retrieval_query, k=4
                )

        def read_sum(cur: psycopg.Cursor) -> int:
            cur.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM allocations WHERE account_id = %s",
                (sc.account_id,),
            )
            return int(cur.fetchone()[0])

        def apply(cur: psycopg.Cursor, proposal) -> bool:
            if proposal.amount is None:
                return False
            cur.execute(
                "INSERT INTO allocations (allocation_id, account_id, agent_id, amount, run_id) "
                "VALUES (%s, %s, %s, %s, %s)",
                (str(uuid.uuid4()), sc.account_id, agent_id, proposal.amount, run_id),
            )
            return True

        def reason(ctx: DecisionContext):
            if ctx.attempt_no > 0:
                with lock:
                    outcome.redecision_reads.append(ctx.observed)
            decision = reason_for(ctx.memory or [], ctx.observed, sc.hard_limit)
            _notify("on_decision", agent_id=agent_id, arrival_offset=offset,
                    ctx=ctx, decision=decision, at=time.perf_counter())
            return decision

        wrapper = ConflictAware(
            operational_read=read_sum,
            apply=apply,
            reason=reason,
            re_reason=arm.re_reason,
            refresh_memory=refresh_memory,
            refresh_memory_on_conflict=arm.refresh_memory,
            isolation=arm.isolation,
            max_attempts=config.max_attempts,
            reasoning_gap_ms=config.reasoning_gap_ms,
            telemetry=telemetry,
        )

        try:
            result = wrapper.run(agent_conns[index], agent_id=agent_id, run_id=run_id)
            with lock:
                results.append(result)
            _notify("on_result", agent_id=agent_id, arrival_offset=offset,
                    result=result, at=time.perf_counter())
        except ArmCollapse as exc:
            with lock:
                outcome.voided = True
                outcome.void_reason = str(exc)
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            with lock:
                outcome.errors += 1
                if outcome.void_reason is None:
                    outcome.void_reason = f"{type(exc).__name__}: {exc}"

    def policy_updater() -> None:
        started.wait()
        time.sleep(config.arrival_window_ms * config.policy_update_at / 1000.0)
        seed_mem = sc.update_memory
        MemoryStore(updater_conn, embedder).add(
            memory_id=seed_mem.memory_id,
            account_id=sc.account_id,
            text=seed_mem.text,
            kind=seed_mem.kind,
            supersedes=seed_mem.supersedes,
            created_at=datetime.datetime.now(datetime.timezone.utc),
        )
        _notify("on_policy_update", at=time.perf_counter())

    threads = [threading.Thread(target=agent, args=(i, off))
               for i, off in enumerate(offsets)]
    threads.append(threading.Thread(target=policy_updater))
    for t in threads:
        t.start()
    _notify("on_release", at=time.perf_counter(), offsets=offsets, run_id=run_id,
            arm_id=arm.id.value, scenario=sc)
    started.set()
    for t in threads:
        t.join(timeout=120)

    for conn in (*agent_conns, updater_conn):
        try:
            conn.close()
        except psycopg.Error:
            pass

    for result in results:
        outcome.conflicts += result.conflicts
        outcome.reason_calls += result.reason_calls
        outcome.memory_refreshes += result.memory_refreshes
        outcome.revisions += 1 if result.revised else 0
        if result.outcome == "committed":
            outcome.committed += 1
        elif result.outcome == "abstained":
            outcome.abstained += 1
        elif result.outcome == "exhausted":
            outcome.exhausted += 1
        elif result.outcome == "error":
            outcome.errors += 1

    with connect(arm.backend) as conn:
        outcome.final_sum = int(
            conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM allocations WHERE account_id = %s",
                (sc.account_id,),
            ).fetchone()[0]
        )

    _record_run(config, outcome)
    return outcome


def _record_run(config: RunConfig, outcome: RunOutcome) -> None:
    """Persist the run-level row. Best-effort: telemetry must not fail a run."""
    try:
        with connect(config.arm.backend) as conn:
            conn.execute(
                """
                INSERT INTO race_runs (run_id, seed, arm, scenario, agent_count,
                                       final_sum, hard_limit, invariant_violated, ended_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (run_id) DO NOTHING
                """,
                (outcome.run_id, outcome.seed, config.arm.id.value,
                 config.scenario.account_id, outcome.agent_count, outcome.final_sum,
                 outcome.hard_limit, outcome.violated_hard_limit),
            )
    except psycopg.Error:
        pass


def summarize(outcomes: list[RunOutcome]) -> dict:
    """Aggregate to the metrics the pre-registration names."""
    valid = [o for o in outcomes if not o.voided]
    n = len(valid) or 1
    return {
        "runs": len(valid),
        "voided": sum(1 for o in outcomes if o.voided),
        "hard_limit_violations": sum(1 for o in valid if o.violated_hard_limit),
        "policy_breaches": sum(1 for o in valid if o.breached_policy),
        "mean_final_sum": sum(o.final_sum for o in valid) / n,
        "conflicts": sum(o.conflicts for o in valid),
        "revisions": sum(o.revisions for o in valid),
        "committed": sum(o.committed for o in valid),
        "abstained": sum(o.abstained for o in valid),
        # Agents that used every attempt and never committed. Reported because
        # it is the difference between "the protocol declined to allocate" and
        # "the protocol never got a turn", which the abstained count alone
        # cannot distinguish.
        "exhausted": sum(o.exhausted for o in valid),
        "memory_refreshes": sum(o.memory_refreshes for o in valid),
        "errors": sum(o.errors for o in valid),
        "redecision_reads": [r for o in valid for r in o.redecision_reads],
    }
