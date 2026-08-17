"""RaceLab Phase 1 gate.

The whole project rests on one empirical claim, and this script is the only
thing that can establish it. No LLM, no vector search, no framework: just two
(or N) concurrent transactions racing on a shared authorization budget.

Each worker runs the minimal shape of the real scenario:

    BEGIN [ISOLATION LEVEL ...]
    SELECT COALESCE(SUM(amount), 0) FROM gate_allocations WHERE account_id = %s
    <sleep reasoning_gap_ms>          -- stands in for the reasoning step
    INSERT INTO gate_allocations ...  -- a DIFFERENT row per worker
    COMMIT

Two properties of that shape matter and are easy to lose by accident:

1.  The aggregate read is INSIDE the transaction. That read is what places
    other workers' rows into the transaction's refresh span. Without it,
    CockroachDB has nothing to detect and there is no experiment.
2.  Every statement is its own round trip, and the SELECT's results are
    returned to the client before the INSERT is sent. A transaction whose
    results have already reached the client cannot be transparently retried
    server-side, which is what makes a serialization failure client-visible
    rather than silently resolved inside the cluster.

Each worker INSERTs its own row rather than updating a shared row, so this is
write skew across rows, not row-lock contention. Two transactions can each read
a sum, each decide their own write is within budget, and both be right about
what they read and wrong about the result.

Pass conditions:

  PASS 1  PostgreSQL READ COMMITTED: all workers commit, zero errors, and the
          final SUM exceeds the account's hard limit. READ COMMITTED permits
          this execution; the invariant is violated with nothing raised.
  PASS 2  CockroachDB SERIALIZABLE: at least one worker sees a client-visible
          SQLSTATE 40001 reaching this process.

Usage:
    python spike/gate.py trial  --backend pg   --workers 2  --gap-ms 200
    python spike/gate.py sweep  --backend crdb --workers 2
    python spike/gate.py all
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
from typing import Any

import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from conn import resolve_dsn  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
RESULTS_DIR = REPO_ROOT / "results" / "gate"

# The exact transaction shape, kept as data so the results document can quote
# what actually ran rather than a prose approximation of it.
TXN_SHAPE = [
    "BEGIN [TRANSACTION ISOLATION LEVEL <isolation>]",
    "SHOW transaction_isolation",
    "SELECT COALESCE(SUM(amount), 0) FROM gate_allocations WHERE account_id = %s",
    "<client-side sleep: reasoning_gap_ms>",
    "INSERT INTO gate_allocations (allocation_id, account_id, agent_id, amount, run_id) "
    "VALUES (%s, %s, %s, %s, %s)",
    "COMMIT",
]

BACKENDS = {
    "pg": {
        "label": "PostgreSQL",
        "dsn_env": "RACELAB_PG_DSN",
        "isolation": "READ COMMITTED",
    },
    "crdb": {
        "label": "CockroachDB",
        "dsn_env": "RACELAB_CRDB_DSN",
        "isolation": "SERIALIZABLE",
    },
}

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS gate_accounts (
        account_id TEXT PRIMARY KEY,
        hard_limit INT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gate_allocations (
        allocation_id UUID PRIMARY KEY,
        account_id    TEXT NOT NULL,
        agent_id      TEXT NOT NULL,
        amount        INT  NOT NULL,
        run_id        TEXT NOT NULL,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS gate_allocations_account_idx ON gate_allocations (account_id)",
]


# --------------------------------------------------------------------------
# result records
# --------------------------------------------------------------------------


@dataclass
class WorkerResult:
    agent_id: str
    # committed | retryable_conflict | deadlock | error. Starts as "unknown" so
    # a worker that dies before it can classify itself is still counted.
    outcome: str = "unknown"
    observed_sum: int | None = None
    amount: int | None = None
    isolation_reported: str | None = None
    error_code: str | None = None
    error_stage: str | None = None  # select | insert | commit | begin
    error_message: str | None = None
    started_at: float = 0.0
    ended_at: float = 0.0

    @property
    def duration_ms(self) -> float:
        return (self.ended_at - self.started_at) * 1000.0


@dataclass
class TrialResult:
    backend: str
    backend_label: str
    isolation_requested: str
    isolation_reported: str | None
    workers: int
    gap_ms: int
    amount: int
    hard_limit: int
    final_sum: int
    invariant_violated: bool
    commits: int
    client_visible_40001: int
    deadlocks: int
    other_errors: int
    run_id: str
    server_version: str
    wall_ms: float
    worker_results: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------


def dsn_for(backend: str) -> str:
    return resolve_dsn(BACKENDS[backend]["dsn_env"])


def server_version(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute("SELECT version()").fetchone()[0]


def ensure_schema(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        for stmt in SCHEMA:
            conn.execute(stmt)


def reset_account(dsn: str, account_id: str, hard_limit: int) -> None:
    """Clear prior allocations so each trial starts from a known state."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DELETE FROM gate_allocations WHERE account_id = %s", (account_id,))
        conn.execute(
            "INSERT INTO gate_accounts (account_id, hard_limit) VALUES (%s, %s) "
            "ON CONFLICT (account_id) DO UPDATE SET hard_limit = EXCLUDED.hard_limit",
            (account_id, hard_limit),
        )


# --------------------------------------------------------------------------
# the worker
# --------------------------------------------------------------------------


def worker(
    dsn: str,
    isolation: str,
    account_id: str,
    agent_id: str,
    amount: int,
    run_id: str,
    gap_ms: int,
    barrier: threading.Barrier,
    out: list[WorkerResult],
    lock: threading.Lock,
    arrival_delay_s: float = 0.0,
) -> None:
    result = WorkerResult(agent_id=agent_id, amount=amount)
    stage = "connect"
    try:
        # autocommit=True plus explicit BEGIN/COMMIT strings, so the client
        # controls the transaction boundaries outright. This rules out the
        # driver quietly folding the whole thing into one implicit transaction
        # or one batched round trip.
        with psycopg.connect(dsn, autocommit=True) as conn:
            # All workers still rendezvous, so the run has a common origin;
            # the arrival delay is then applied on top. With a delay of 0 this
            # is exactly the synchronized barrier the gate was measured under.
            barrier.wait(timeout=60)
            if arrival_delay_s:
                time.sleep(arrival_delay_s)
            result.started_at = time.perf_counter()

            stage = "begin"
            conn.execute(f"BEGIN TRANSACTION ISOLATION LEVEL {isolation}")

            stage = "show"
            result.isolation_reported = conn.execute("SHOW transaction_isolation").fetchone()[0]

            stage = "select"
            observed = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM gate_allocations WHERE account_id = %s",
                (account_id,),
            ).fetchone()[0]
            result.observed_sum = int(observed)

            # The reasoning gap. In the real system this is a vector search and
            # a model call; here it is just time, because the only thing that
            # matters to the database is that the read's results are already in
            # the client's hands while other transactions are still writing.
            if gap_ms:
                time.sleep(gap_ms / 1000.0)

            stage = "insert"
            conn.execute(
                "INSERT INTO gate_allocations "
                "(allocation_id, account_id, agent_id, amount, run_id) "
                "VALUES (%s, %s, %s, %s, %s)",
                (str(uuid.uuid4()), account_id, agent_id, amount, run_id),
            )

            stage = "commit"
            conn.execute("COMMIT")
            result.outcome = "committed"
    except psycopg.Error as exc:
        sqlstate = getattr(exc, "sqlstate", None)
        result.error_code = sqlstate
        result.error_stage = stage
        result.error_message = str(exc).strip().splitlines()[0][:300]
        if sqlstate == "40001":
            result.outcome = "retryable_conflict"
        elif sqlstate == "40P01":
            result.outcome = "deadlock"
        else:
            result.outcome = "error"
    except Exception as exc:  # barrier timeout, etc.
        result.outcome = "error"
        result.error_stage = stage
        result.error_message = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        result.ended_at = time.perf_counter()
        with lock:
            out.append(result)


# --------------------------------------------------------------------------
# a trial
# --------------------------------------------------------------------------


def run_trial(
    backend: str,
    workers: int,
    gap_ms: int,
    amount: int,
    hard_limit: int,
    account_id: str = "acct-gate",
    arrival_window_ms: int = 0,
    seed: int = 20260814,
) -> TrialResult:
    cfg = BACKENDS[backend]
    dsn = dsn_for(backend)
    isolation = cfg["isolation"]
    run_id = f"gate-{backend}-{uuid.uuid4().hex[:8]}"

    ensure_schema(dsn)
    reset_account(dsn, account_id, hard_limit)

    rng = random.Random(seed)
    delays = [
        (rng.uniform(0, arrival_window_ms) / 1000.0) if arrival_window_ms else 0.0
        for _ in range(workers)
    ]

    barrier = threading.Barrier(workers)
    results: list[WorkerResult] = []
    lock = threading.Lock()
    threads = [
        threading.Thread(
            target=worker,
            args=(
                dsn,
                isolation,
                account_id,
                f"agent-{i:02d}",
                amount,
                run_id,
                gap_ms,
                barrier,
                results,
                lock,
                delays[i],
            ),
            name=f"agent-{i:02d}",
        )
        for i in range(workers)
    ]

    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=180)
    wall_ms = (time.perf_counter() - t0) * 1000.0

    with psycopg.connect(dsn, autocommit=True) as conn:
        final_sum = int(
            conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM gate_allocations WHERE account_id = %s",
                (account_id,),
            ).fetchone()[0]
        )

    reported = next((r.isolation_reported for r in results if r.isolation_reported), None)
    return TrialResult(
        backend=backend,
        backend_label=cfg["label"],
        isolation_requested=isolation,
        isolation_reported=reported,
        workers=workers,
        gap_ms=gap_ms,
        amount=amount,
        hard_limit=hard_limit,
        final_sum=final_sum,
        invariant_violated=final_sum > hard_limit,
        commits=sum(1 for r in results if r.outcome == "committed"),
        client_visible_40001=sum(1 for r in results if r.outcome == "retryable_conflict"),
        deadlocks=sum(1 for r in results if r.outcome == "deadlock"),
        other_errors=sum(1 for r in results if r.outcome in ("error", "unknown")),
        run_id=run_id,
        server_version=server_version(dsn),
        wall_ms=wall_ms,
        worker_results=[asdict(r) for r in sorted(results, key=lambda r: r.agent_id)],
    )


def print_trial(tr: TrialResult) -> None:
    print(
        f"  gap={tr.gap_ms:>5}ms  workers={tr.workers:<3} "
        f"commits={tr.commits:<3} 40001={tr.client_visible_40001:<3} "
        f"deadlock={tr.deadlocks:<3} err={tr.other_errors:<3} "
        f"sum={tr.final_sum:<5} limit={tr.hard_limit:<5} "
        f"violated={'YES' if tr.invariant_violated else 'no'}"
    )
    for r in tr.worker_results:
        if r["outcome"] not in ("committed", "retryable_conflict"):
            print(f"      ! {r['agent_id']} {r['outcome']} [{r['error_code']}] "
                  f"at {r['error_stage']}: {r['error_message']}")


def save(payload: dict[str, Any], name: str) -> pathlib.Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / name
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_trial(args: argparse.Namespace) -> int:
    cfg = BACKENDS[args.backend]
    print(f"\n{cfg['label']} / {cfg['isolation']} -- single trial")
    tr = run_trial(args.backend, args.workers, args.gap_ms, args.amount, args.limit)
    print(f"  server: {tr.server_version[:80]}")
    print(f"  isolation reported by server: {tr.isolation_reported}")
    print_trial(tr)
    p = save(asdict(tr), f"trial-{args.backend}-{tr.run_id}.json")
    print(f"  -> {p.relative_to(REPO_ROOT)}")
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    """Vary the reasoning gap. The gap is the only knob that should matter to
    whether a serialization failure becomes client-visible, so sweeping it is
    the honest way to check pass condition 2 rather than reporting one lucky
    or one unlucky configuration."""
    cfg = BACKENDS[args.backend]
    gaps = args.gaps or [0, 5, 25, 50, 100, 250, 500]
    print(f"\n{cfg['label']} / {cfg['isolation']} -- sweep over reasoning_gap_ms")
    print(f"  workers={args.workers} amount={args.amount} hard_limit={args.limit} "
          f"repeats={args.repeats}")
    trials: list[TrialResult] = []
    for gap in gaps:
        for _ in range(args.repeats):
            tr = run_trial(args.backend, args.workers, gap, args.amount, args.limit)
            trials.append(tr)
            print_trial(tr)

    print("\n  summary by gap:")
    for gap in gaps:
        rows = [t for t in trials if t.gap_ms == gap]
        print(
            f"    gap={gap:>5}ms  "
            f"40001 mean={statistics.mean(t.client_visible_40001 for t in rows):.2f}  "
            f"commits mean={statistics.mean(t.commits for t in rows):.2f}  "
            f"violated {sum(1 for t in rows if t.invariant_violated)}/{len(rows)}"
        )

    payload = {
        "backend": args.backend,
        "backend_label": cfg["label"],
        "isolation": cfg["isolation"],
        "transaction_shape": TXN_SHAPE,
        "server_version": trials[0].server_version if trials else None,
        "trials": [asdict(t) for t in trials],
    }
    p = save(payload, f"sweep-{args.backend}.json")
    print(f"\n  -> {p.relative_to(REPO_ROOT)}")
    return 0


def cmd_scale(args: argparse.Namespace) -> int:
    """N=20 concurrent workers, the configuration the gate is judged on."""
    cfg = BACKENDS[args.backend]
    print(f"\n{cfg['label']} / {cfg['isolation']} -- scale to N={args.workers}")
    trials = []
    for i in range(args.repeats):
        tr = run_trial(args.backend, args.workers, args.gap_ms, args.amount, args.limit,
                       arrival_window_ms=args.arrival_window_ms, seed=args.seed + i)
        trials.append(tr)
        print(f"  run {i + 1}/{args.repeats}:", end="")
        print_trial(tr)
    payload = {
        "backend": args.backend,
        "backend_label": cfg["label"],
        "isolation": cfg["isolation"],
        "transaction_shape": TXN_SHAPE,
        "server_version": trials[0].server_version,
        "trials": [asdict(t) for t in trials],
    }
    p = save(payload, f"scale-{args.backend}.json")
    print(f"  -> {p.relative_to(REPO_ROOT)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser, workers: int = 2, amount: int = 60) -> None:
        p.add_argument("--backend", choices=sorted(BACKENDS), required=True)
        p.add_argument("--workers", type=int, default=workers)
        p.add_argument("--amount", type=int, default=amount,
                       help="amount each worker allocates")
        p.add_argument("--limit", type=int, default=100, help="account hard limit")

    p_trial = sub.add_parser("trial", help="one trial")
    common(p_trial)
    p_trial.add_argument("--gap-ms", type=int, default=200)
    p_trial.set_defaults(func=cmd_trial)

    p_sweep = sub.add_parser("sweep", help="sweep reasoning_gap_ms")
    common(p_sweep)
    p_sweep.add_argument("--gaps", type=int, nargs="*", default=None)
    p_sweep.add_argument("--repeats", type=int, default=3)
    p_sweep.set_defaults(func=cmd_sweep)

    p_scale = sub.add_parser("scale", help="N concurrent workers")
    common(p_scale, workers=20, amount=40)
    p_scale.add_argument("--gap-ms", type=int, default=200)
    p_scale.add_argument("--repeats", type=int, default=5)
    p_scale.add_argument("--arrival-window-ms", type=int, default=0,
                         help="0 keeps the synchronized barrier the gate was measured "
                              "under; >0 staggers arrival uniformly over the window")
    p_scale.add_argument("--seed", type=int, default=20260814)
    p_scale.set_defaults(func=cmd_scale)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
