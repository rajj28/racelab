"""A PUBLIC, read-mostly demo endpoint: race real agents, return what happened.

## What this is

The write gateway (`deploy/lambda_handler.py`) is IAM-signed on purpose: it
writes to a ledger an organisation cares about. This is the other thing — an
endpoint a judge can hit from a browser with no credentials, which runs a **real
race against the real cluster** and returns every event it produced.

Nothing here is simulated. `run_once` is the same function the published sweep
calls; the connections, the `40001`s and the rows are all real. What differs from
the local server is only *transport*: Lambda cannot stream Server-Sent Events
from a Python runtime, so instead of pushing events as they happen this returns
them in one response, each stamped with the millisecond it occurred at. The
browser replays them on that timeline. The race is live; the delivery is not.

## Why it is safe to expose

A public endpoint in front of a real database needs more than good intentions:

  * **A distributed lock in the database itself**, not Lambda concurrency. The
    account's concurrency limit is 10 and AWS refuses a reservation that would
    drop unreserved capacity below its minimum, so `reserved_concurrent_executions`
    is not available to us. A row in `demo_lock` is, and it works across every
    concurrent invocation regardless of how AWS schedules them.
  * **Its own account id.** It never touches `hero-001` or anything the tests
    assert on, so a stranger hammering this cannot corrupt a measurement.
  * **Hard caps** on agents and arrival window, below what the sweep uses,
    because the cluster's measured connection budget is about 30.
  * **Arm A is refused.** It needs a PostgreSQL that only exists on a laptop.

## Why it does not use the sweep's account

`hero-001` is load-bearing for the test suite and for `docs/index.html`. A demo
that mutates it would make every judge's visit a small act of vandalism against
the evidence. `demo-live-001` is seeded on first use and reset before each run.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import sys
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import psycopg

from racelab.arms import ARMS, ORDER
from racelab.db import ConnectionPool, connect, normalize_tls
from racelab.embeddings import get_embedder
from racelab.experiment import RunConfig, Scenario, run_once
from racelab.integrations.aws import log_event, resolve_dsn
from racelab.memory import MemoryStore
from scenario.corpus import HERO, MemorySeed
from scenario.decide import RETRIEVAL_QUERY, infer_ceiling, propose

ACCOUNT = "demo-live-001"
HARD_LIMIT = 100
STALE_CEILING, CURRENT_CEILING = 80, 60

MAX_AGENTS, MIN_AGENTS = 10, 2
MAX_WINDOW = 2000
LOCK_TTL_S = 90                      # a crashed invocation must not wedge the demo

_DSN: str | None = None
_pool: ConnectionPool | None = None
_embedder = None
_seeded = False

# The demo's own corpus. Same shape as the hero scenario -- four notes, one of
# which is superseded mid-run -- but under an account nothing else reads.
SEED_MEMORIES = [
    MemorySeed("demo-m1", "Temporary authorization ceiling for this account is "
               "$80 per billing cycle, pending completion of the quarterly review.",
               "policy"),
    MemorySeed("demo-m2", "Historical allocations against this account averaged "
               "$45 per request during the previous quarter.", "history"),
    MemorySeed("demo-m3", "The primary contact for this account is the procurement "
               "team, who prefer approvals to be batched rather than issued "
               "individually.", "note"),
    MemorySeed("demo-m4", "This account was migrated from the legacy billing system "
               "in March and its historical records before that date are incomplete.",
               "note"),
]
UPDATE_MEMORY = MemorySeed(
    "demo-m5",
    "Authorization ceiling reduced to $60 per billing cycle, effective "
    "immediately, superseding the prior temporary ceiling.",
    "policy", supersedes="demo-m1")


def _dsn() -> str:
    global _DSN
    if _DSN is None:
        _DSN = normalize_tls(resolve_dsn().value)
    return _DSN


def _conn():
    return psycopg.connect(_dsn(), autocommit=True, connect_timeout=10)


def _resources():
    global _pool, _embedder
    if _embedder is None:
        _embedder = get_embedder("titan")
    if _pool is None:
        _pool = ConnectionPool("crdb", size=4)
    return _pool, _embedder


# --------------------------------------------------------------------------
# the lock, in the database rather than in AWS
# --------------------------------------------------------------------------

LOCK_DDL = """
CREATE TABLE IF NOT EXISTS demo_lock (
    id         INT PRIMARY KEY DEFAULT 1,
    holder     TEXT,
    taken_at   TIMESTAMPTZ
)
"""


def _acquire(conn, token: str) -> bool:
    """One run at a time, enforced where every invocation can see it.

    An expired lock is stealable: a Lambda that is killed mid-run would
    otherwise leave the demo permanently unavailable, which is a worse failure
    than two overlapping runs.
    """
    conn.execute(LOCK_DDL)
    conn.execute("INSERT INTO demo_lock (id, holder, taken_at) VALUES (1, NULL, NULL) "
                 "ON CONFLICT (id) DO NOTHING")
    # The TTL is multiplied into an interval rather than interpolated into an
    # INTERVAL literal: a placeholder inside a quoted literal is not a
    # placeholder, it is the two characters %s.
    row = conn.execute(
        "UPDATE demo_lock SET holder = %s, taken_at = now() WHERE id = 1 "
        "AND (holder IS NULL OR taken_at < now() - (%s * INTERVAL '1 second')) "
        "RETURNING holder", (token, LOCK_TTL_S)).fetchone()
    return row is not None


def _release(conn, token: str) -> None:
    try:
        conn.execute("UPDATE demo_lock SET holder = NULL WHERE id = 1 AND holder = %s",
                     (token,))
    except Exception:  # noqa: BLE001 - the TTL will clear it
        pass


# --------------------------------------------------------------------------
# seeding
# --------------------------------------------------------------------------


def _seed(conn) -> None:
    """Create the demo account and its notes, once per container."""
    global _seeded
    if _seeded:
        return
    _, embedder = _resources()
    conn.execute(
        "INSERT INTO accounts (account_id, name, hard_limit) VALUES (%s,%s,%s) "
        "ON CONFLICT (account_id) DO UPDATE SET hard_limit = EXCLUDED.hard_limit",
        (ACCOUNT, "Live demo", HARD_LIMIT))
    have = {r[0] for r in conn.execute(
        "SELECT memory_id FROM memories WHERE account_id = %s", (ACCOUNT,)).fetchall()}
    store = MemoryStore(conn, embedder)
    now = datetime.datetime.now(datetime.timezone.utc)
    for i, m in enumerate(SEED_MEMORIES):
        if m.memory_id in have:
            continue
        store.add(memory_id=m.memory_id, account_id=ACCOUNT, text=m.text,
                  kind=m.kind, supersedes=m.supersedes,
                  created_at=now - datetime.timedelta(hours=4 - i))
    _seeded = True


def _reset(conn) -> None:
    """Clear the ledger and withdraw the superseding note, so a run starts clean."""
    conn.execute("DELETE FROM allocations WHERE account_id = %s", (ACCOUNT,))
    conn.execute("DELETE FROM memories WHERE memory_id = %s", (UPDATE_MEMORY.memory_id,))


def _state(conn) -> dict:
    rows = conn.execute(
        "SELECT memory_id, kind, text, supersedes FROM memories "
        "WHERE account_id = %s ORDER BY created_at", (ACCOUNT,)).fetchall()
    superseded = {r[3] for r in rows if r[3]}
    total = int(conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM allocations WHERE account_id = %s",
        (ACCOUNT,)).fetchone()[0])
    return {
        "account": ACCOUNT, "total": total, "hard_limit": HARD_LIMIT,
        "memories": [{"memory_id": r[0], "kind": r[1], "text": r[2],
                      "supersedes": r[3], "is_superseded": r[0] in superseded}
                     for r in rows],
    }


# --------------------------------------------------------------------------
# the observer: collects, rather than streams
# --------------------------------------------------------------------------


class Collect:
    def __init__(self) -> None:
        self.t0: float | None = None
        self.events: list[dict] = []

    def _ms(self, at: float) -> float:
        return round((at - self.t0) * 1000.0, 1) if self.t0 else 0.0

    def on_release(self, *, at, offsets, run_id, arm_id, scenario) -> None:
        self.t0 = at
        self.events.append({"type": "release", "run_id": run_id, "arm": arm_id,
                            "offsets": [round(o * 1000.0, 1) for o in offsets],
                            "hard_limit": scenario.hard_limit,
                            "stale_ceiling": scenario.stale_ceiling,
                            "current_ceiling": scenario.current_ceiling})

    def on_policy_update(self, *, at) -> None:
        self.events.append({"type": "policy", "at_ms": self._ms(at)})

    def on_decision(self, *, agent_id, arrival_offset, ctx, decision, at) -> None:
        self.events.append({
            "type": "decision", "agent_id": agent_id, "attempt": ctx.attempt_no,
            "at_ms": self._ms(at), "observed": ctx.observed,
            "action": decision.action, "amount": decision.amount,
            "ceiling": decision.inferred_ceiling,
            "memories": [{"memory_id": m.memory_id, "kind": m.kind, "text": m.text}
                         for m in (ctx.memory or [])]})

    def on_result(self, *, agent_id, arrival_offset, result, at) -> None:
        self.events.append({
            "type": "result", "agent_id": agent_id, "at_ms": self._ms(at),
            "outcome": result.outcome, "action": result.action,
            "conflicts": result.conflicts, "revised": bool(result.revised),
            "attempts": result.attempts_made, "reason_calls": result.reason_calls,
            "memory_refreshes": result.memory_refreshes})


def _reason(memories, observed, hard_limit):
    ceiling, _ = infer_ceiling(memories)
    return propose(ceiling, observed, hard_limit)


# --------------------------------------------------------------------------
# handler
# --------------------------------------------------------------------------

CORS = {
    "access-control-allow-origin": "*",
    "access-control-allow-headers": "content-type",
    "access-control-allow-methods": "GET,POST,OPTIONS",
    "content-type": "application/json",
}


def _reply(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": CORS, "body": json.dumps(body, default=str)}


def handler(event, context):  # noqa: ANN001 - AWS signature
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "POST").upper()
    if method == "OPTIONS":
        return {"statusCode": 204, "headers": CORS, "body": ""}

    raw = event.get("rawPath") or event.get("path") or "/"
    body = event.get("body")
    if isinstance(body, str) and body:
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            return _reply(400, {"error": "body is not valid JSON"})
    payload = body if isinstance(body, dict) else {}

    try:
        # -- describe the arms ------------------------------------------
        if raw.endswith("/arms") or payload.get("op") == "arms":
            human = {
                "A-rc": ("Same database, protection off",
                         "CockroachDB at READ COMMITTED. The control: same cluster, "
                         "same latency, only the safety setting differs."),
                "B": ("Told, and ignores it",
                      "Serializable. It is told about every collision, retries, and "
                      "sends the same answer again."),
                "C-ops": ("Works it out again",
                          "On a collision it throws the decision away and re-reads "
                          "the balance -- but keeps the notes it already had."),
                "C": ("Works it out and re-reads the notes",
                      "Also refreshes the retrieved policy, so it can notice a rule "
                      "that moved while it was thinking."),
            }
            arms = [{"id": ARMS[k].id.value, "name": human[ARMS[k].id.value][0],
                     "blurb": human[ARMS[k].id.value][1],
                     "backend": ARMS[k].backend,
                     "isolation": ARMS[k].isolation or "SERIALIZABLE",
                     "re_reason": ARMS[k].re_reason,
                     "refresh_memory": ARMS[k].refresh_memory,
                     "needs_postgres": False}
                    for k in ORDER if ARMS[k].id.value in human]
            return _reply(200, {"arms": arms, "min_agents": MIN_AGENTS,
                                "max_agents": MAX_AGENTS})

        # -- current state ----------------------------------------------
        if raw.endswith("/state") or payload.get("op") == "state":
            with _conn() as conn:
                _seed(conn)
                return _reply(200, _state(conn))

        # -- run ----------------------------------------------------------
        arm_id = str(payload.get("arm", "C"))
        if arm_id == "A":
            return _reply(400, {
                "error": "arm A runs on stock PostgreSQL, which only exists on a "
                         "developer's machine. Clone the repo to run it.",
            })
        match = [ARMS[k] for k in ORDER if ARMS[k].id.value == arm_id]
        if not match:
            return _reply(400, {"error": f"unknown arm {arm_id!r}"})
        arm = match[0]

        agents = max(MIN_AGENTS, min(MAX_AGENTS, int(payload.get("agents", 8))))
        window = max(0, min(MAX_WINDOW, int(payload.get("window", 600))))

        token = uuid.uuid4().hex[:12]
        started = time.time()
        with _conn() as conn:
            if not _acquire(conn, token):
                return _reply(429, {
                    "error": "another race is running",
                    "why": "the cluster's connection budget is about 30 and each "
                           "race holds one connection per agent, so they run one at "
                           "a time. Try again in a few seconds.",
                })
            try:
                _seed(conn)
                _reset(conn)
                pool, embedder = _resources()
                scenario = Scenario(
                    account_id=ACCOUNT, hard_limit=HARD_LIMIT,
                    stale_ceiling=STALE_CEILING, current_ceiling=CURRENT_CEILING,
                    retrieval_query=RETRIEVAL_QUERY, update_memory=UPDATE_MEMORY)
                cap = Collect()
                outcome = run_once(
                    RunConfig(arm=arm, scenario=scenario,
                              seed=int(time.time()) % 100000, agent_count=agents,
                              arrival_window_ms=window, reasoning_gap_ms=200.0),
                    embedder, _reason, pool, observer=cap)
                state = _state(conn)
            finally:
                _release(conn, token)

        log_event("demo_race", arm=arm_id, agents=agents, window_ms=window,
                  final_sum=outcome.final_sum, conflicts=outcome.conflicts,
                  elapsed_ms=round((time.time() - started) * 1000))

        return _reply(200, {
            "run_id": outcome.run_id,
            "arm": outcome.arm_id.value,
            "events": cap.events,
            "state": state,
            "summary": {
                "final_sum": outcome.final_sum,
                "hard_limit": outcome.hard_limit,
                "ceiling": outcome.current_ceiling,
                "over_hard_limit": bool(outcome.violated_hard_limit),
                "breached_policy": bool(outcome.breached_policy),
                "conflicts": outcome.conflicts,
                "voided": bool(outcome.voided),
                "void_reason": outcome.void_reason,
                "elapsed_ms": round((time.time() - started) * 1000),
            },
        })

    except psycopg.OperationalError as exc:
        log_event("demo_db_unreachable", error=str(exc)[:300], level="ERROR")
        return _reply(503, {"error": "the cluster is unreachable", "detail": str(exc)[:200]})
    except Exception as exc:  # noqa: BLE001
        log_event("demo_unhandled", error=f"{type(exc).__name__}: {exc}"[:400],
                  level="ERROR")
        return _reply(500, {"error": f"{type(exc).__name__}: {exc}"[:300]})


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="invoke the demo handler locally")
    ap.add_argument("--arm", default="C")
    ap.add_argument("--agents", type=int, default=6)
    ap.add_argument("--window", type=int, default=600)
    ap.add_argument("--op", default=None, help="arms | state")
    a = ap.parse_args()
    req = {"op": a.op} if a.op else {"arm": a.arm, "agents": a.agents, "window": a.window}
    out = handler({"body": json.dumps(req)}, None)
    d = json.loads(out["body"])
    if "summary" in d:
        s = d["summary"]
        print(f"{out['statusCode']} arm={d['arm']} final=${s['final_sum']} "
              f"limit=${s['hard_limit']} ceiling=${s['ceiling']} "
              f"over={s['over_hard_limit']} breached={s['breached_policy']} "
              f"conflicts={s['conflicts']} events={len(d['events'])} "
              f"in {s['elapsed_ms']}ms")
    else:
        print(out["statusCode"], json.dumps(d)[:400])
