"""A live API over the real experiment, so the race can be watched as it runs.

## What this is, and what it is not

Every event this streams comes from **twenty real agents racing on a real
CockroachDB cluster**. There is no simulation here: `run_once` is the same
function the published sweep calls, the connections are real, the `40001`s are
real, and the rows land in the same tables the tests assert against.

What it adds is a live view. `run_once` already grew four observer hooks for the
static inspection UI -- release, decision, policy update, result -- and they are
instrumentation only: no arm, window, metric or run depends on them, and a
failing observer is swallowed rather than propagated into the measurement. This
module attaches an observer that pushes those moments onto a queue, and streams
the queue to a browser over Server-Sent Events.

So the browser watches the experiment. It does not reproduce it.

## Why there is a hard single-run lock

The cluster's measured connection budget is about **30**. One run opens one
connection per agent plus one for the policy updater, so a 20-agent run sits at
21. Two concurrent runs exhaust the plan and both fail in a way that looks like
a bug in the protocol rather than a bug in the demo.

So runs are serialised globally, and a second caller gets `409` with the reason
rather than a queue position. `MAX_AGENTS` is capped below 20 by default for the
same reason -- a demo that takes the cluster down is not a demo.

## Why it is not exposed publicly

This writes to a real ledger. It binds to localhost, and the deploy notes say
what to change if you want it hosted. An open write endpoint in front of a
financial-shaped table is not a demo convenience.

    pip install flask
    python -m racelab.server                 # http://127.0.0.1:8000
    python -m racelab.server --port 9000
"""

from __future__ import annotations

import argparse
import json
import pathlib
import queue
import sys
import threading
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

try:
    from flask import Flask, Response, jsonify, request, send_from_directory
except ImportError as exc:  # pragma: no cover
    raise SystemExit("this server needs Flask:\n    pip install flask") from exc

from racelab.arms import ARMS, ORDER
from racelab.db import ConnectionPool, connect
from racelab.embeddings import get_embedder
from racelab.experiment import RunConfig, Scenario, run_once
from scenario.decide import RETRIEVAL_QUERY

# The demo's own account, seeding and reset live with the public Lambda handler
# so the two deployments cannot drift into telling different stories. Crucially
# this is NOT `hero-001`: that account is load-bearing for the test suite and for
# docs/index.html, and a public endpoint that mutated it would let any visitor
# quietly corrupt the published evidence.
from deploy.demo_handler import (ACCOUNT, CURRENT_CEILING, HARD_LIMIT,
                                 STALE_CEILING, UPDATE_MEMORY, _reason,
                                 _reset as _demo_reset, _seed as _demo_seed)

REPO = pathlib.Path(__file__).resolve().parents[1]
DIST = REPO / "app" / "dist"

# Deliberately below the 20 the sweep uses. See the module docstring: the
# cluster's connection budget is the binding constraint, not the browser's.
MAX_AGENTS = 14
MIN_AGENTS = 2

app = Flask(__name__, static_folder=None)

_run_lock = threading.Lock()
_current: dict = {"token": None, "queue": None, "started": 0.0}

# One pooled set of memory connections for the whole process. Racing connections
# stay per-agent -- that is what makes them race -- but retrieval is pooled, and
# rebuilding the pool per run would pay six handshakes every time.
_pool: ConnectionPool | None = None
_embedder = None


def _resources():
    global _pool, _embedder
    if _embedder is None:
        _embedder = get_embedder("titan")
    if _pool is None:
        _pool = ConnectionPool("crdb", size=6)
    return _pool, _embedder


# --------------------------------------------------------------------------
# the live observer
# --------------------------------------------------------------------------


class Stream:
    """Turns `run_once`'s observer hooks into a queue of JSON-ready events.

    Hooks are called under the run's own lock, so this only ever enqueues --
    never blocks on I/O, never touches the database, and never raises. An
    observer that can fail a run would make the instrumentation part of the
    experiment, which is exactly what it must not be.
    """

    def __init__(self, q: queue.Queue):
        self.q = q
        self.t0: float | None = None

    def _ms(self, at: float) -> float:
        return round((at - self.t0) * 1000.0, 1) if self.t0 else 0.0

    def _put(self, kind: str, **kw) -> None:
        self.q.put({"type": kind, **kw})

    # -- hooks ------------------------------------------------------------

    def on_release(self, *, at, offsets, run_id, arm_id, scenario) -> None:
        self.t0 = at
        self._put("release", run_id=run_id, arm=arm_id,
                  offsets=[round(o * 1000.0, 1) for o in offsets],
                  hard_limit=scenario.hard_limit,
                  stale_ceiling=scenario.stale_ceiling,
                  current_ceiling=scenario.current_ceiling)

    def on_policy_update(self, *, at) -> None:
        self._put("policy", at_ms=self._ms(at))

    def on_decision(self, *, agent_id, arrival_offset, ctx, decision, at) -> None:
        self._put("decision",
                  agent_id=agent_id,
                  attempt=ctx.attempt_no,
                  at_ms=self._ms(at),
                  observed=ctx.observed,
                  action=decision.action,
                  amount=decision.amount,
                  ceiling=decision.inferred_ceiling,
                  rationale=getattr(decision, "rationale", "")[:160],
                  # The retrieved text the ceiling came from. This is the point:
                  # the rule is in these strings, and they change mid-run.
                  memories=[{"memory_id": m.memory_id, "kind": m.kind,
                             "text": m.text} for m in (ctx.memory or [])])

    def on_result(self, *, agent_id, arrival_offset, result, at) -> None:
        self._put("result",
                  agent_id=agent_id,
                  at_ms=self._ms(at),
                  outcome=result.outcome,
                  action=result.action,
                  conflicts=result.conflicts,
                  revised=bool(result.revised),
                  attempts=result.attempts_made,
                  reason_calls=result.reason_calls,
                  memory_refreshes=result.memory_refreshes)


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------


@app.get("/api/arms")
def arms():
    """The five arms, described in plain language for the picker."""
    human = {
        "A":     ("Ordinary database",
                  "Stock PostgreSQL at its default setting. Never told anything changed."),
        "A-rc":  ("Same database, protection off",
                  "CockroachDB at READ COMMITTED. The control: same cluster, same "
                  "latency, only the safety setting differs."),
        "B":     ("Told, and ignores it",
                  "Serializable. It is told about every collision, retries, and sends "
                  "the same answer again."),
        "C-ops": ("Works it out again",
                  "On a collision it throws the decision away and re-reads the "
                  "balance -- but keeps the notes it already had."),
        "C":     ("Works it out and re-reads the notes",
                  "Also refreshes the retrieved policy, so it can notice a rule that "
                  "moved while it was thinking."),
    }
    out = []
    for key in ORDER:
        a = ARMS[key]
        name, blurb = human[a.id.value]
        out.append({
            "id": a.id.value, "name": name, "blurb": blurb,
            "backend": a.backend,
            "isolation": a.isolation or ("SERIALIZABLE" if a.backend == "crdb"
                                         else "READ COMMITTED"),
            "re_reason": a.re_reason, "refresh_memory": a.refresh_memory,
            # Arm A needs a second database running; say so rather than letting
            # it fail at connect time with something cryptic.
            "needs_postgres": a.backend == "pg",
        })
    return jsonify({"arms": out, "min_agents": MIN_AGENTS, "max_agents": MAX_AGENTS})


@app.get("/api/state")
def state():
    """The ledger and the policy memories, as they stand right now."""
    with connect("crdb") as conn:
        _demo_seed(conn)
        total = int(conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM allocations WHERE account_id=%s",
            (ACCOUNT,)).fetchone()[0])
        limit = int(conn.execute(
            "SELECT hard_limit FROM accounts WHERE account_id=%s",
            (ACCOUNT,)).fetchone()[0])
        rows = conn.execute(
            "SELECT memory_id, kind, text, supersedes FROM memories "
            "WHERE account_id=%s ORDER BY created_at", (ACCOUNT,)).fetchall()
    superseded = {r[3] for r in rows if r[3]}
    return jsonify({
        "account": ACCOUNT,
        "total": total,
        "hard_limit": limit,
        "memories": [{"memory_id": r[0], "kind": r[1], "text": r[2],
                      "supersedes": r[3], "is_superseded": r[0] in superseded}
                     for r in rows],
    })


@app.post("/api/reset")
def reset():
    """Clear the ledger and put the policy back to its pre-update state."""
    _resources()                       # the embedder is needed to seed
    with connect("crdb") as conn:
        _demo_seed(conn)
        _demo_reset(conn)
    return jsonify({"ok": True})


@app.get("/api/run")
def run():
    """Race real agents and stream what happens, as it happens.

    `EventSource` only issues GET, which is why this is a GET that has effects.
    It is guarded by the same single-run lock as everything else.
    """
    arm_id = request.args.get("arm", "C")
    try:
        agents = int(request.args.get("agents", 8))
        window = int(request.args.get("window", 600))
    except ValueError:
        return jsonify({"error": "agents and window must be integers"}), 400

    match = [ARMS[k] for k in ORDER if ARMS[k].id.value == arm_id]
    if not match:
        return jsonify({"error": f"unknown arm {arm_id!r}"}), 400
    arm = match[0]
    agents = max(MIN_AGENTS, min(MAX_AGENTS, agents))
    window = max(0, min(4000, window))

    if not _run_lock.acquire(blocking=False):
        # Not a queue. The connection budget is the reason, and saying so is
        # more useful than a spinner.
        return jsonify({
            "error": "a run is already in flight",
            "why": "runs are serialised because the cluster's connection budget "
                   "is about 30 and each run holds one connection per agent",
        }), 409

    q: queue.Queue = queue.Queue()
    token = uuid.uuid4().hex[:8]
    _current.update(token=token, queue=q, started=time.time())

    def drive() -> None:
        try:
            pool, embedder = _resources()
            scenario = Scenario(
                account_id=ACCOUNT, hard_limit=HARD_LIMIT,
                stale_ceiling=STALE_CEILING, current_ceiling=CURRENT_CEILING,
                retrieval_query=RETRIEVAL_QUERY, update_memory=UPDATE_MEMORY)
            config = RunConfig(arm=arm, scenario=scenario, seed=int(time.time()) % 100000,
                               agent_count=agents, arrival_window_ms=window,
                               reasoning_gap_ms=200.0)
            outcome = run_once(config, embedder, _reason, pool,
                               observer=Stream(q))
            q.put({
                "type": "done",
                "run_id": outcome.run_id,
                "arm": outcome.arm_id.value,
                "final_sum": outcome.final_sum,
                "hard_limit": outcome.hard_limit,
                "ceiling": outcome.current_ceiling,
                "over_hard_limit": bool(outcome.violated_hard_limit),
                "breached_policy": bool(outcome.breached_policy),
                "conflicts": outcome.conflicts,
                "voided": bool(outcome.voided),
                "void_reason": outcome.void_reason,
            })
        except Exception as exc:  # noqa: BLE001 - reported to the client, not hidden
            q.put({"type": "error", "error": f"{type(exc).__name__}: {exc}"[:400]})
        finally:
            q.put(None)                       # sentinel: end of stream
            _run_lock.release()

    threading.Thread(target=drive, daemon=True).start()

    def events():
        yield f": run {token}\n\n"
        while True:
            try:
                item = q.get(timeout=180)
            except queue.Empty:
                yield 'data: {"type":"error","error":"timed out waiting for the run"}\n\n'
                return
            if item is None:
                yield 'data: {"type":"end"}\n\n'
                return
            yield f"data: {json.dumps(item)}\n\n"

    return Response(events(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",     # nginx would otherwise hold the stream
        "Connection": "keep-alive",
    })


# --------------------------------------------------------------------------
# the built front end, when there is one
# --------------------------------------------------------------------------


@app.get("/", defaults={"path": "index.html"})
@app.get("/<path:path>")
def spa(path: str):
    if not DIST.exists():
        return Response(
            "<h1>RaceLab API is running</h1>"
            "<p>The front end has not been built yet:</p>"
            "<pre>cd app &amp;&amp; npm install &amp;&amp; npm run build</pre>"
            "<p>Or run it in dev mode with <code>npm run dev</code>, which proxies "
            "<code>/api</code> here.</p>",
            mimetype="text/html")
    target = DIST / path
    if not target.exists():
        path = "index.html"
    return send_from_directory(DIST, path)


def main() -> int:
    ap = argparse.ArgumentParser(description="RaceLab live API")
    ap.add_argument("--host", default="127.0.0.1",
                    help="localhost by default: this writes to a real ledger")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    print(f"racelab api on http://{args.host}:{args.port}")
    print(f"  arms: {', '.join(ARMS[k].id.value for k in ORDER)}")
    print(f"  agents capped at {MAX_AGENTS} (cluster connection budget ~30)")
    print("  runs are serialised; a second caller gets 409\n")
    # threaded: the SSE response holds a worker for the length of a run, and the
    # browser still needs /api/state answered while one is in flight.
    app.run(host=args.host, port=args.port, threaded=True, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
