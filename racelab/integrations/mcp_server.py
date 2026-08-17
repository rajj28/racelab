"""RaceLab as an MCP server: a guarded write, for any agent that speaks MCP.

Point Claude Code, Cursor, VS Code or a LangChain MCP client at this and the
agent gains a tool that **cannot commit a write that violates the policy the
agent itself retrieved**. No library import, no framework, no change to how the
agent is built.

## Why a server and not a hosted API

The tempting product is an endpoint you POST a proposed action to, which answers
allowed or denied. **It cannot provide this guarantee**, and shipping it would
contradict the result this project exists to establish.

The soundness argument is that the check happens *inside the transaction that
does the write*: under SERIALIZABLE, the state verified before `COMMIT` is the
state that becomes durable, or the commit is refused with a `40001`. An advisory
service sits outside any transaction, so between its "allowed" and the caller's
`COMMIT` another agent can move the state. That is precisely the racy check we
measured as insufficient.

So whoever enforces must also own the transaction. This server owns it, running
against **your** CockroachDB, on **your** machine, with **your** DSN from your
own environment. It never holds anyone else's credentials, which is a reason to
prefer stdio rather than an accident of it.

## The interesting part: `reconsider`

MCP has tool success and tool error. It has no notion of

    "your last answer was derived from state that has since changed --
     here is the new state, decide again."

That gap is real, and it is the same one LangChain has. `decide_and_write`
returns it as a first-class result: not a failure, but a *teaching* response
carrying the total as it is now, the policy as it is now, whether that policy
moved mid-flight, and the action that is no longer valid.

The agent's own model re-decides, in its own context, with the refreshed policy
quoted back to it. The write lands only if the new choice satisfies the
constraint at commit time.

## Tools

    decide_and_write   the guarded write: committed | refused | reconsider
    recall             vector search over policy memory
    remember           write a fact or policy into agentic memory
    audit_decisions    what agents decided, and against which constraint

Writes are **off by default**. `--allow-writes` enables `decide_and_write` and
`remember`; without it this is a read-only inspection surface.

## Run

    pip install "mcp>=2.0"
    python -m racelab.integrations.mcp_server --allow-writes

Or via `.mcp.json` (see `docs/MCP_SERVER.md`).

## Honest scope

This is a **reference server**, not a hardened product. `decide_and_write`
implements the allocation shape this project measures: a summed ledger against a
ceiling parsed from retrieved policy text. Generalising it means injecting your
own read and apply, exactly as `ConflictAware` already requires -- the library is
the general form, and this is the demonstration that the protocol survives being
delivered as a tool.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid

_MISSING = ("racelab.integrations.mcp_server needs the MCP SDK.\n"
            '    pip install "mcp>=2.0"')

try:
    from mcp.server import MCPServer
except ImportError as exc:  # pragma: no cover
    raise ImportError(_MISSING) from exc

import psycopg

from ..conflict import ConflictAware, DecisionContext
from ..db import dsn_for

OPTIONS = (45, 40, 35)
CEILING_PATTERN = re.compile(r"\$\s*(\d+)")

ALLOW_WRITES = False


# --------------------------------------------------------------------------
# state access
# --------------------------------------------------------------------------


def _connect():
    return psycopg.connect(dsn_for("crdb"), autocommit=True, connect_timeout=10)


READ_STATE = """
WITH total AS (
    SELECT COALESCE(SUM(amount), 0) AS t
    FROM allocations WHERE account_id = %(acct)s
), pol AS (
    SELECT array_agg(memory_id ORDER BY created_at DESC) AS ids,
           array_agg(text ORDER BY created_at DESC) AS texts,
           max(created_at) AS newest
    FROM (
        SELECT memory_id, text, created_at FROM memories
        WHERE account_id = %(acct)s AND kind = 'policy'
        ORDER BY created_at DESC LIMIT 4
    ) s
)
SELECT total.t, pol.ids, pol.texts, pol.newest FROM total, pol
"""


def _read_state(cur, account_id: str) -> dict:
    """Total and governing policy, from one statement so they share a timestamp."""
    cur.execute(READ_STATE, {"acct": account_id})
    row = cur.fetchone()
    ids = list(row[1] or [])
    texts = list(row[2] or [])
    ceiling, source = None, None
    for mid, text in zip(ids, texts):
        found = CEILING_PATTERN.search(text or "")
        if found:
            ceiling, source = int(found.group(1)), mid
            break
    return {
        "total": int(row[0] or 0),
        "ceiling": ceiling,
        "ceiling_source": source,
        "policy_ids": ids,
        "policy_texts": texts,
        "policy_newest_at": row[3].isoformat() if row[3] else None,
    }


def _hard_limit(cur, account_id: str, fallback: int) -> int:
    cur.execute("SELECT hard_limit FROM accounts WHERE account_id = %s",
                (account_id,))
    row = cur.fetchone()
    return int(row[0]) if row else fallback


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------

server = MCPServer(
    name="racelab",
    title="RaceLab conflict-aware writes",
    instructions=(
        "Use decide_and_write for any write whose value you derived from state "
        "you read. If it returns status='reconsider', the state your decision "
        "rested on has changed: read the observed_now and policy_now fields, "
        "choose a NEW action that satisfies them, and call it again. Do not "
        "repeat your previous action -- it has already been shown to be stale."
    ),
)


@server.tool(
    description=(
        "Allocate against a shared budget, with the policy ceiling enforced "
        "inside the transaction. Returns status 'committed', 'refused' (your "
        "action violates the policy you retrieved), or 'reconsider' (the state "
        "you reasoned about changed; decide again from the fresh values given)."
    ),
)
def decide_and_write(account_id: str, amount: int,
                     agent_id: str = "mcp-agent") -> str:
    """Attempt one guarded allocation of `amount` against `account_id`."""
    if not ALLOW_WRITES:
        return json.dumps({
            "status": "forbidden",
            "why": "this server was started read-only; restart with --allow-writes",
        }, indent=1)
    if amount not in OPTIONS:
        return json.dumps({
            "status": "invalid",
            "why": f"amount must be one of {list(OPTIONS)}",
            "permitted": list(OPTIONS),
        }, indent=1)

    run_id = f"mcp-{uuid.uuid4().hex[:8]}"
    seen: dict = {}
    # What the agent's decision rested on, captured on the FIRST read so a
    # reconsider response can show then-versus-now rather than only now.
    first: dict = {}

    class Proposal:
        def __init__(self, amt: int):
            self.action = f"allocate({amt})"
            self.amount = amt

    def operational_read(cur) -> int:
        state = _read_state(cur, account_id)
        state["hard_limit"] = _hard_limit(cur, account_id, 100)
        seen.clear()
        seen.update(state)
        if not first:
            first.update(state)
        return state["total"]

    def reason(ctx: DecisionContext) -> Proposal:
        return Proposal(amount)

    def apply(cur, proposal) -> bool:
        cur.execute(
            "INSERT INTO allocations (allocation_id, account_id, agent_id, amount, run_id) "
            "VALUES (%s,%s,%s,%s,%s)",
            (str(uuid.uuid4()), account_id, agent_id, proposal.amount, run_id))
        return True

    def constraint(cur, proposal) -> str | None:
        cur.execute("SELECT COALESCE(SUM(amount),0) FROM allocations "
                    "WHERE account_id = %s", (account_id,))
        total = int(cur.fetchone()[0])
        if total > seen.get("hard_limit", 100):
            return (f"total would be ${total}, over the hard limit of "
                    f"${seen.get('hard_limit')}")
        ceiling = seen.get("ceiling")
        if ceiling is not None and total > ceiling:
            return (f"total would be ${total}, over the ${ceiling} policy ceiling "
                    f"from {seen.get('ceiling_source')}")
        return None

    conn = _connect()
    try:
        wrapper = ConflictAware(
            operational_read=operational_read,
            apply=apply,
            reason=reason,
            re_reason=True,
            constraint=constraint,
            # One attempt at re-deciding is not this server's job: the AGENT
            # re-decides, in its own context, having been told what changed.
            # Retrying here would silently replay the same amount, which is the
            # exact failure this project measures.
            max_refusals=0,
            max_attempts=1,
        )
        result = wrapper.run(conn, agent_id=agent_id, run_id=run_id)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"status": "error",
                           "why": f"{type(exc).__name__}: {exc}"}, indent=1)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    fresh = _fresh_state(account_id)

    if result.outcome == "committed":
        return json.dumps({
            "status": "committed",
            "action": result.action,
            "total_now": fresh["total"],
            "ceiling": fresh["ceiling"],
            "run_id": run_id,
        }, indent=1)

    if result.refusals:
        # The agent's own policy forbade it. Not a conflict -- a judgment error.
        return json.dumps({
            "status": "refused",
            "why": result.error,
            "your_action": f"allocate({amount})",
            "total_now": fresh["total"],
            "policy_now": {"ceiling": fresh["ceiling"],
                           "source_memory": fresh["ceiling_source"]},
            "still_permitted": _still_fit(fresh),
            "guidance": ("your action was not written. Choose one of "
                         f"{_still_fit(fresh)} or abstain."),
        }, indent=1)

    # A serialization failure. THIS is the response MCP has no vocabulary for.
    moved = (first.get("ceiling") is not None
             and fresh.get("ceiling") is not None
             and first["ceiling"] != fresh["ceiling"])
    return json.dumps({
        "status": "reconsider",
        "why": ("another transaction changed the state your decision rested on; "
                "nothing was written"),
        "observed_when_you_decided": first.get("total"),
        "observed_now": fresh["total"],
        "policy_when_you_decided": {"ceiling": first.get("ceiling"),
                                    "source_memory": first.get("ceiling_source")},
        "policy_now": {"ceiling": fresh["ceiling"],
                       "source_memory": fresh["ceiling_source"],
                       "changed_mid_flight": moved},
        "your_previous_action": f"allocate({amount})",
        "still_permitted": _still_fit(fresh),
        "guidance": (
            f"allocate({amount}) was derived from a total of "
            f"{first.get('total')}, which is now {fresh['total']}. "
            + ("The policy ceiling also changed while you were deciding. "
               if moved else "")
            + f"Choose again from {_still_fit(fresh)} or abstain, then call "
              f"decide_and_write once more."),
    }, indent=1)


def _fresh_state(account_id: str) -> dict:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            state = _read_state(cur, account_id)
            state["hard_limit"] = _hard_limit(cur, account_id, 100)
            return state
    finally:
        conn.close()


def _still_fit(state: dict) -> list[str]:
    """Which actions would satisfy the constraint as it stands right now."""
    binding = state.get("hard_limit", 100)
    if state.get("ceiling") is not None:
        binding = min(binding, state["ceiling"])
    remaining = binding - state.get("total", 0)
    return [f"allocate({a})" for a in OPTIONS if a <= remaining]


@server.tool(
    description=("Semantic search over this account's memory, using CockroachDB's "
                 "vector index. Returns the policies and notes an agent would "
                 "retrieve before deciding."),
)
def recall(account_id: str, query: str, k: int = 4) -> str:
    """Retrieve the memories most relevant to `query`."""
    from ..embeddings import get_embedder
    from ..memory import MemoryStore
    conn = _connect()
    try:
        store = MemoryStore(conn, get_embedder("titan"))
        rows = store.retrieve(account_id, query, k=k)
        return json.dumps({
            "query": query,
            "memories": [{"memory_id": m.memory_id, "kind": m.kind,
                          "text": m.text} for m in rows],
            "note": ("the first policy-kind memory is the governing one; "
                     "retrieval has already applied supersession and recency"),
        }, indent=1)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"status": "error",
                           "why": f"{type(exc).__name__}: {exc}"}, indent=1)
    finally:
        conn.close()


@server.tool(
    description=("Write a fact or policy into this account's agentic memory. "
                 "A 'policy' kind stating a dollar ceiling becomes enforceable "
                 "by decide_and_write immediately."),
)
def remember(account_id: str, text: str, kind: str = "note",
             memory_id: str | None = None, supersedes: str | None = None) -> str:
    """Store one memory, embedded and indexed for retrieval."""
    if not ALLOW_WRITES:
        return json.dumps({
            "status": "forbidden",
            "why": "this server was started read-only; restart with --allow-writes",
        }, indent=1)
    import datetime

    from ..embeddings import get_embedder
    from ..memory import MemoryStore
    conn = _connect()
    try:
        MemoryStore(conn, get_embedder("titan")).add(
            memory_id=memory_id or f"mcp-{uuid.uuid4().hex[:8]}",
            account_id=account_id, text=text, kind=kind, supersedes=supersedes,
            created_at=datetime.datetime.now(datetime.timezone.utc))
        return json.dumps({"status": "stored", "kind": kind,
                           "effective": "immediately, for any agent that recalls"},
                          indent=1)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"status": "error",
                           "why": f"{type(exc).__name__}: {exc}"}, indent=1)
    finally:
        conn.close()


@server.tool(
    description=("What agents decided, and which constraint they believed they "
                 "were under. The inferred_ceiling column is the one that "
                 "distinguishes bad reasoning from correct reasoning over a "
                 "stale document."),
)
def audit_decisions(account_id: str | None = None, limit: int = 20) -> str:
    """Recent decisions from the audit trail."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT run_id, agent_id, attempt_no, observed_sum, "
                "inferred_ceiling, decision_after, revised "
                "FROM decisions ORDER BY created_at DESC LIMIT %s",
                (min(limit, 100),))
            rows = cur.fetchall()
        return json.dumps({
            "decisions": [{
                "run_id": r[0], "agent_id": r[1], "attempt_no": r[2],
                "observed_sum": r[3], "inferred_ceiling": r[4],
                "action": r[5], "revised": r[6],
            } for r in rows],
        }, indent=1, default=str)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"status": "error",
                           "why": f"{type(exc).__name__}: {exc}"}, indent=1)
    finally:
        conn.close()


def main() -> int:
    global ALLOW_WRITES
    ap = argparse.ArgumentParser(description="RaceLab MCP server")
    ap.add_argument("--allow-writes", action="store_true",
                    help="enable decide_and_write and remember")
    ap.add_argument("--transport", default="stdio",
                    choices=["stdio", "sse", "streamable-http"])
    args = ap.parse_args()
    ALLOW_WRITES = args.allow_writes

    # stderr, never stdout: stdout IS the protocol channel on stdio transport,
    # and a stray print there corrupts the session.
    print(f"racelab mcp server: writes "
          f"{'ENABLED' if ALLOW_WRITES else 'disabled (read-only)'}",
          file=sys.stderr)
    server.run(transport=args.transport)
    return 0


if __name__ == "__main__":
    sys.exit(main())
