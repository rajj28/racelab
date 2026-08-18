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

## Which rule is enforced

The compiled one. `racelab/policy.py` turns a policy document into a structured
constraint once, off the write path; `racelab/policy_gate.py` decides which
version governs; this server enforces it inside the transaction. **No model runs
during a write.** If there is no current enforceable constraint, the tool refuses
and names which of the five policy states it is in -- `uncompiled`, `stale`,
`unenforceable` and `mismatched` all mean "nothing will be authorized here", and
each is a condition the old dollar-figure regex could not have detected at all.

The gateway (`deploy/lambda_handler.py`) resolves policy through the same module.
Two write paths with two readings of the same rule would be worse than either.

## Honest scope

This is a **reference server**, not a hardened product. `decide_and_write` acts
on one declared resource (`bindings/*.yaml`, see `racelab/binding.py`), which is
how it reaches tables this repository contains no code for. Anything beyond that
shape means injecting your own read and apply, exactly as `ConflictAware` already
requires -- the library is the general form, and this is the demonstration that
the protocol survives being delivered as a tool.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

_MISSING = ("racelab.integrations.mcp_server needs the MCP SDK.\n"
            '    pip install "mcp>=2.0"')

try:
    from mcp.server import MCPServer
except ImportError as exc:  # pragma: no cover
    raise ImportError(_MISSING) from exc

import psycopg

from ..binding import ResourceBinding
from ..conflict import ConflictAware, DecisionContext, SqlTelemetry
from ..db import dsn_for
from ..policy_gate import PolicyGate

ALLOW_WRITES = False
BINDING_NAME = os.environ.get("RACELAB_BINDING", "allocations")

_GATE: PolicyGate | None = None


def _gate() -> PolicyGate:
    global _GATE
    if _GATE is None:
        _GATE = PolicyGate(ResourceBinding.named(BINDING_NAME))
    return _GATE


# --------------------------------------------------------------------------
# state access
# --------------------------------------------------------------------------


def _connect():
    return psycopg.connect(dsn_for("crdb"), autocommit=True, connect_timeout=10)


def _state_dict(state) -> dict:
    """The shape the tool responses speak in, from a GateState."""
    out = {
        "total": state.total,
        "hard_limit": state.hard_limit,
        "ceiling": state.policy_limit,
        "binding_limit": state.binding_limit,
        "authorizes": state.authorizes,
    }
    out.update(state.as_dict())
    return out


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
        "Write against a scoped budget, with the compiled policy enforced "
        "inside the transaction. Returns status 'committed', 'refused' (your "
        "action violates the policy in force, or the policy cannot be enforced "
        "at all), or 'reconsider' (the state you reasoned about changed; decide "
        "again from the fresh values given)."
    ),
)
def decide_and_write(account_id: str, amount: int,
                     agent_id: str = "mcp-agent") -> str:
    """Attempt one guarded write of `amount` against `account_id`."""
    if not ALLOW_WRITES:
        return json.dumps({
            "status": "forbidden",
            "why": "this server was started read-only; restart with --allow-writes",
        }, indent=1)
    gate = _gate()
    binding = gate.binding
    if amount not in binding.actions:
        return json.dumps({
            "status": "invalid",
            "why": f"amount must be one of {list(binding.actions)}",
            "permitted": list(binding.actions),
        }, indent=1)

    run_id = f"mcp-{uuid.uuid4().hex[:8]}"
    seen: dict = {"state": None}
    # What the agent's decision rested on, captured on the FIRST read so a
    # reconsider response can show then-versus-now rather than only now.
    first: dict = {"state": None}

    class Proposal:
        def __init__(self, amt: int, policy_version: int | None):
            self.action = f"allocate({amt})"
            self.amount = amt
            # Read off the proposal by SqlTelemetry, so a decision row records
            # the policy version it was made under.
            self.policy_version = policy_version

    def operational_read(cur) -> int:
        # No fallback hard limit. A scope with no row in the limit table has no
        # limit, and the gate raises rather than inventing one: a mistyped
        # account_id must not be handed a budget out of nowhere.
        state = gate.read(cur, account_id)
        seen["state"] = state
        if first["state"] is None:
            first["state"] = state
        return state.total

    def reason(ctx: DecisionContext) -> Proposal:
        return Proposal(amount, seen["state"].version)

    def apply(cur, proposal) -> bool:
        binding.insert(cur, scope=account_id, agent_id=agent_id,
                       amount=proposal.amount, run_id=run_id)
        return True

    def constraint(cur, proposal) -> str | None:
        return gate.check(cur, seen["state"])

    conn = _connect()
    # Telemetry gets its OWN connection, in autocommit, and never the raced one.
    # Rows describing a failed attempt written inside the transaction that failed
    # would be rolled back by the very conflict they record -- so the decisions
    # that are most worth having would be exactly the ones that never survive.
    # `SqlTelemetry` refuses a non-autocommit connection for this reason.
    #
    # This is also what makes `audit_decisions` mean anything: before it was
    # added, the server answered that tool from a table its own writes never
    # reached. An audit surface that is empty because nothing wrote to it looks
    # identical to one that is empty because nothing happened.
    telemetry, audit_note = None, None
    try:
        telemetry = SqlTelemetry(_connect())
    except Exception as exc:  # noqa: BLE001
        # An audit outage is not a correctness outage, and conflating them would
        # mean a logging failure could stop a write the policy allows. But it is
        # not swallowed either: the response says the decision went unrecorded.
        audit_note = f"decision not recorded: {type(exc).__name__}: {exc}"[:200]
    try:
        wrapper = ConflictAware(
            telemetry=telemetry,
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
        for handle in (conn, getattr(telemetry, "conn", None)):
            try:
                if handle is not None:
                    handle.close()
            except Exception:  # noqa: BLE001
                pass

    try:
        fresh = _fresh_state(account_id)
    except Exception as exc:  # noqa: BLE001
        # Describing what happened must not itself become an unhandled error --
        # the write has already landed or not, and that outcome is the answer.
        return json.dumps({
            "status": "committed" if result.outcome == "committed" else "error",
            "action": result.action,
            "why": f"could not re-read state to describe the outcome: {exc}"[:200],
            "run_id": run_id,
        }, indent=1)
    fits = _still_fit(fresh)
    now = _state_dict(fresh)
    then = _state_dict(first["state"]) if first["state"] is not None else {}

    if result.outcome == "committed":
        return json.dumps({
            "status": "committed",
            "action": result.action,
            "total_now": fresh.total,
            "ceiling": fresh.policy_limit,
            # The version the write was actually made under, taken from the read
            # inside the transaction rather than from `fresh` -- the policy could
            # have moved in the moment since the commit, and reporting the
            # current version as the one enforced would be a small lie in exactly
            # the situation this project exists to be careful about.
            "policy_version": (first["state"].version
                               if first["state"] is not None else None),
            "run_id": run_id,
            **({"audit": audit_note} if audit_note else {}),
        }, indent=1)

    # A policy that cannot be enforced blocks every action, so there is no
    # "choose something smaller" to offer. Reporting it as an ordinary ceiling
    # breach would send the agent into a retry loop against a rule that is not
    # going to admit anything until a person recompiles it.
    if not fresh.authorizes:
        return json.dumps({
            "status": "refused",
            "why": fresh.detail,
            "your_action": f"allocate({amount})",
            "total_now": fresh.total,
            "policy_now": now,
            "still_permitted": [],
            "guidance": ("nothing was written, and nothing will be authorized "
                         "against this account until its policy is compiled and "
                         "enforceable. This is not something to retry: it needs "
                         "scripts/compile_policies.py, or a policy document that "
                         "the constraint language can express."),
        }, indent=1)

    if result.refusals:
        # The compiled policy forbade it. Not a conflict -- a judgment error.
        return json.dumps({
            "status": "refused",
            "why": result.error,
            "your_action": f"allocate({amount})",
            "total_now": fresh.total,
            "policy_now": now,
            "still_permitted": fits,
            "guidance": ("your action was not written. Choose one of "
                         f"{fits} or abstain."),
        }, indent=1)

    # A serialization failure. THIS is the response MCP has no vocabulary for.
    moved = (first["state"] is not None
             and (first["state"].policy_limit != fresh.policy_limit
                  or first["state"].version != fresh.version))
    return json.dumps({
        "status": "reconsider",
        "why": ("another transaction changed the state your decision rested on; "
                "nothing was written"),
        "observed_when_you_decided": then.get("total"),
        "observed_now": fresh.total,
        "policy_when_you_decided": then,
        "policy_now": {**now, "changed_mid_flight": moved},
        "your_previous_action": f"allocate({amount})",
        "still_permitted": fits,
        "guidance": (
            f"allocate({amount}) was derived from a total of "
            f"{then.get('total')}, which is now {fresh.total}. "
            + ("The policy in force also changed while you were deciding "
               f"(v{then.get('policy_version')} -> v{fresh.version}). "
               if moved else "")
            + f"Choose again from {fits} or abstain, then call "
              f"decide_and_write once more."),
    }, indent=1)


def _fresh_state(account_id: str):
    """Read the gate's view of the world again, outside any transaction.

    Only ever used to *describe* what happened to an agent whose write did not
    land. It is deliberately not what anything is enforced against -- that
    happens inside the transaction, in `constraint`, and reading it here for
    enforcement would be exactly the racy advisory check this server exists to
    argue against.
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            return _gate().read(cur, account_id)
    finally:
        conn.close()


def _still_fit(state) -> list[str]:
    """Which actions would satisfy the constraint as it stands right now."""
    return [f"allocate({a})" for a in state.permitted(_gate().binding.actions)]


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
                 "A 'policy' kind becomes the governing document immediately, "
                 "which means decide_and_write will REFUSE every write against "
                 "this account until the new policy is compiled -- a rule that "
                 "has not been compiled is a rule nothing can be checked "
                 "against, and enforcing the superseded version would enforce a "
                 "policy that has been withdrawn."),
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
        out = {"status": "stored", "kind": kind,
               "effective": "immediately, for any agent that recalls"}
        if kind == "policy":
            out["enforcement"] = (
                "this is now the governing policy document. It is NOT yet "
                "enforceable: run `python scripts/compile_policies.py --account "
                f"{account_id}` to compile it. Until then decide_and_write "
                "refuses every write against this account with policy_status "
                "'stale' or 'uncompiled'.")
        return json.dumps(out, indent=1)
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
                "inferred_ceiling, decision_after, revised, policy_version "
                "FROM decisions ORDER BY created_at DESC LIMIT %s",
                (min(limit, 100),))
            rows = cur.fetchall()
        return json.dumps({
            "decisions": [{
                "run_id": r[0], "agent_id": r[1], "attempt_no": r[2],
                "observed_sum": r[3], "inferred_ceiling": r[4],
                "action": r[5], "revised": r[6], "policy_version": r[7],
            } for r in rows],
            "note": ("policy_version answers 'which decisions were made under "
                     "the old cap?'. A NULL means no compiled constraint "
                     "governed that decision."),
        }, indent=1, default=str)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"status": "error",
                           "why": f"{type(exc).__name__}: {exc}"}, indent=1)
    finally:
        conn.close()


def main() -> int:
    global ALLOW_WRITES, BINDING_NAME
    ap = argparse.ArgumentParser(description="RaceLab MCP server")
    ap.add_argument("--allow-writes", action="store_true",
                    help="enable decide_and_write and remember")
    ap.add_argument("--binding", default=BINDING_NAME,
                    help="which bindings/<name>.yaml resource to enforce")
    ap.add_argument("--transport", default="stdio",
                    choices=["stdio", "sse", "streamable-http"])
    args = ap.parse_args()
    ALLOW_WRITES = args.allow_writes
    BINDING_NAME = args.binding

    # stderr, never stdout: stdout IS the protocol channel on stdio transport,
    # and a stray print there corrupts the session.
    print(f"racelab mcp server: writes "
          f"{'ENABLED' if ALLOW_WRITES else 'disabled (read-only)'}; "
          f"binding {BINDING_NAME}", file=sys.stderr)
    server.run(transport=args.transport)
    return 0


if __name__ == "__main__":
    sys.exit(main())
