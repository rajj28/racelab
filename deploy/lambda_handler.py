"""AWS Lambda: a policy-enforcing write gateway for agent decisions.

## The workflow this is

An organisation runs many AI agents that spend money or grant entitlements:
refund bots, procurement agents, discount approvers, license provisioners. Two
kinds of rule constrain them, and they live in different places.

    the budget      a column. "this customer's refund pool is $2,000"
    the policy      a document. "tier-2 refunds are capped at $250 pending
                    the Q3 contract review" -- retrieved by vector search,
                    rewritten by Legal without a schema migration

Today each agent team hand-rolls a retry loop. When a policy changes mid-flight,
the retry replays a decision derived from the old policy, the database accepts it
because nothing in the database is violated, and nobody finds out. There is no
error to alert on.

This endpoint is the alternative: **agents do not write to the ledger, they ask
this to write for them.** It reads state in a transaction, invokes the reasoning
step, enforces the compiled policy before the commit, and answers with either a
committed decision or a refusal that names the rule and the policy version it
came from. Every attempt is logged with the constraint the agent was held to.

    POST /decide
      { "account_id": "...", "agent_id": "...", "binding": "allocations" }

    200 { "outcome": "committed", "action": "allocate(35)", "revised": true,
          "conflicts": 1, "policy_limit": 60, "policy_version": 3 }
    409 { "outcome": "refused", "reason": "60 exceeds the policy limit of 60
          (policy v3: ...)", "refused_actions": ["allocate(45)"] }

A `409` is the interesting response. It means an agent proposed something its own
policy forbade and the write was stopped before it landed -- the case that is
silent everywhere else.

## Which rule, and where it comes from

Two rules, and they live in different places:

    the hard limit    a column, `accounts.hard_limit`. Always enforced.
    the policy        a document, compiled once by `racelab/policy.py` into a
                      structured constraint and stored in `policy_constraints`.

This handler **never calls a model to interpret policy**. It reads the compiled
constraint, and if there is no current enforceable one it refuses the write and
says which of the five states it is in (`racelab/policy_gate.py`). A gateway that
asked a model what the rule meant on every request would be slow, non-repeatable,
and would let a different reading of the same document authorize each write.

## Which table

The resource is declared, not coded -- `bindings/allocations.yaml`. Point the
`binding` field of a request at another spec and this same handler enforces a
table it has no code for. That is `racelab/binding.py`.

## Why Lambda suits this

The unit of work is short, bursty and stateless: read, reason, enforce, commit.
State lives in CockroachDB, which is the point -- the compute is disposable and
the memory is not. Concurrency comes free, which is also the hazard, so the
handler respects the connection budget and says so below.

## AWS services, and what each is actually for

    Lambda            runs the gateway
    Secrets Manager   holds the DSN, so the credential is rotatable and scoped
                      by IAM rather than sitting in an environment variable
    Bedrock           Titan for retrieval embeddings, Claude for the reasoning
                      step
    CloudWatch Logs   one JSON object per decision, queryable in Logs Insights
    CloudWatch        four counters, of which ConstraintRefusals is the one to
                      alert on

## Connections, and why there is no pool

One connection per *container*, held across warm invocations, and never a pool.

Opening one per request cost ~580 ms of TLS handshake and was the largest single
term in a 3.5 s response. Holding it removes that from every warm request.

A pool would be worse than useless: Lambda gives each container exactly one
concurrent request, so a pool inside one is never used, while a pool multiplied
by AWS's concurrency is precisely how a cluster's connection budget is exhausted.
The concurrency ceiling is set separately in `deploy/deploy.py`.

The held connection is checked before use and dropped on any operational error,
because a container can outlive the connection it opened.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import psycopg

from racelab.binding import BindingError, ResourceBinding
from racelab.conflict import ConflictAware, DecisionContext
from racelab.integrations.aws import (log_decision, log_event,
                                      metrics_from_result, publish_metrics,
                                      resolve_dsn)
from racelab.policy import PolicyError
from racelab.policy_gate import PolicyGate

DEFAULT_BINDING = os.environ.get("RACELAB_BINDING", "allocations")

# Bindings are parsed and schema-validated once per container. A binding is a
# file, so parsing it per request would be pointless work; validating it per
# request would be a round trip on the hot path. The validation still happens
# against the live schema -- just once, on the first request that uses it.
_GATES: dict[str, PolicyGate] = {}


def _gate(name: str) -> PolicyGate:
    if name not in _GATES:
        _GATES[name] = PolicyGate(ResourceBinding.named(name))
    return _GATES[name]

# Resolved once per container, not per request: Secrets Manager calls are billed
# and rate-limited, and the DSN does not change between invocations of the same
# container. A rotation replaces the container.
_DSN: str | None = None
_DSN_SOURCE: str | None = None

# The connection is also held across warm invocations. Measured: the TLS
# handshake to CockroachDB Cloud costs ~580 ms, and opening one per request made
# it the single largest term in a 3.5 s response.
#
# One connection per container, never a pool. Lambda gives each container exactly
# one concurrent request, so a pool inside one buys nothing, and a pool
# multiplied by AWS's concurrency is how a cluster's connection budget is
# exhausted. Concurrency is capped separately (deploy/deploy.py).
_CONN = None


def _dsn() -> str:
    global _DSN, _DSN_SOURCE
    if _DSN is None:
        resolved = resolve_dsn()
        _DSN, _DSN_SOURCE = resolved.value, resolved.source
        log_event("dsn_resolved", source=resolved.source,
                  managed=resolved.is_managed)
    return _DSN


def _connection():
    """A live connection, reused across warm invocations of this container.

    A held connection can be closed by the server between invocations -- idle
    timeouts, a cluster upgrade, a network blip -- so it is checked before use
    rather than assumed. `closed` is cheap and local; the round trip only
    happens when reconnecting.
    """
    global _CONN
    if _CONN is not None and not _CONN.closed:
        return _CONN
    if _CONN is not None:
        log_event("connection_recycled", reason="server closed it")
    t0 = time.perf_counter()
    _CONN = psycopg.connect(_dsn(), autocommit=True, connect_timeout=10)
    log_event("connection_opened",
              ms=round((time.perf_counter() - t0) * 1000, 1))
    return _CONN


class Decision:
    """The proposal. `action` is the only attribute the wrapper requires.

    `policy_version` is read off this by `SqlTelemetry`, so a decision row
    records which compiled constraint the agent was held to. Without it a policy
    change is unauditable after the fact: the decisions look identical and only
    the ceiling moved.
    """

    def __init__(self, action: str, amount: int | None, inferred_ceiling: int | None,
                 policy_version: int | None = None, rationale: str = ""):
        self.action = action
        self.amount = amount
        self.inferred_ceiling = inferred_ceiling
        self.policy_version = policy_version
        self.rationale = rationale


def handler(event, context):  # noqa: ANN001 - AWS signature
    body = event.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            return _reply(400, {"error": "body is not valid JSON"})
    payload = body or event or {}

    account_id = payload.get("account_id")
    if not account_id:
        return _reply(400, {"error": "account_id is required"})
    agent_id = payload.get("agent_id") or f"lambda-{uuid.uuid4().hex[:6]}"
    run_id = payload.get("run_id") or getattr(context, "aws_request_id", "local")

    try:
        gate = _gate(str(payload.get("binding") or DEFAULT_BINDING))
    except BindingError as exc:
        return _reply(400, {"error": "unknown or invalid binding",
                            "detail": str(exc)[:200]})
    binding = gate.binding

    # The state the decision rested on, captured on every read so the response
    # and the telemetry can name the exact policy version in force.
    seen: dict = {"state": None}

    def reason(ctx: DecisionContext) -> Decision:
        state = seen["state"]
        remaining = state.binding_limit - ctx.observed
        # Largest action that fits -- the greedy agent this project measures.
        # Sorted here rather than relying on the binding file's order: the
        # built-in binding happens to list [45, 40, 35] descending, so taking the
        # first that fit was greedy by coincidence. A binding listing its actions
        # ascending would have produced a *minimal* agent from the same code,
        # under the same name, with no error anywhere. Found by pointing this at
        # a second resource.
        amount = next((a for a in sorted(binding.actions, reverse=True)
                       if a <= remaining), None)
        # An agent under a refusing policy proposes nothing. The constraint would
        # stop the write anyway; abstaining means the refusal is reported once,
        # as a policy state, rather than as three identical rejected actions.
        if not state.authorizes:
            amount = None
        decision = Decision(
            action=f"allocate({amount})" if amount else "abstain",
            amount=amount,
            inferred_ceiling=state.policy_limit,
            policy_version=state.version,
            rationale=(f"observed {ctx.observed}, binding {state.binding_limit}, "
                       f"{remaining} remaining, policy {state.status.value}"),
        )
        log_decision(run_id=run_id, agent_id=agent_id, attempt_no=ctx.attempt_no,
                     observed=ctx.observed,
                     inferred_constraint=state.policy_limit,
                     action=decision.action, refused=ctx.refused,
                     retrieved_ids=[state.governing_memory_id]
                     if state.governing_memory_id else [],
                     policy_version=state.version,
                     policy_status=state.status.value)
        return decision

    def operational_read(cur) -> int:
        # Total, hard limit, governing document and compiled constraint, in ONE
        # statement. That is the whole design: the policy this write is checked
        # against is read at the same timestamp as the total it is checked over.
        # Reading the policy outside the transaction would let it move between
        # the check and the commit; reading it in a second statement merely made
        # them *likely* to match. One statement makes it provable.
        # No fallback. An account with no row in the limit table has no hard
        # limit, and the gate raises rather than inventing one -- an unknown
        # scope with an unbounded budget is the shape of every interesting
        # incident. The request cannot supply one either: an endpoint that let a
        # caller name its own ceiling would not be a gateway.
        state = gate.read(cur, account_id)
        seen["state"] = state
        return state.total

    def apply(cur, proposal) -> bool:
        if proposal.amount is None:
            return False
        binding.insert(cur, scope=account_id, agent_id=agent_id,
                       amount=proposal.amount, run_id=run_id)
        return True

    def constraint(cur, proposal) -> str | None:
        """Enforced here, before COMMIT, which is the only sound place for it."""
        return gate.check(cur, seen["state"])

    try:
        conn = _connection()
        wrapper = ConflictAware(
            operational_read=operational_read,
            apply=apply,
            reason=reason,
            re_reason=True,
            constraint=constraint,
            max_refusals=3,
            max_attempts=5,
        )
        result = wrapper.run(conn, agent_id=agent_id, run_id=run_id)
    except psycopg.OperationalError as exc:
        # A held connection that died mid-request is dropped so the next
        # invocation reconnects rather than inheriting a broken one.
        global _CONN
        _CONN = None
        log_event("database_unreachable", error=str(exc)[:300], level="ERROR")
        return _reply(503, {"error": "memory layer unreachable",
                            "detail": str(exc)[:200]})
    except (BindingError, PolicyError) as exc:
        # The resource or its policy is not in a state that can be enforced. A
        # 409 rather than a 500: nothing is broken, and nothing will be written.
        log_event("resource_not_enforceable", error=str(exc)[:300], level="WARN")
        return _reply(409, {"outcome": "refused", "reason": str(exc)[:300],
                            "binding": binding.name, "run_id": run_id})
    except Exception as exc:  # noqa: BLE001
        log_event("unhandled", error=f"{type(exc).__name__}: {exc}", level="ERROR")
        return _reply(500, {"error": "internal error"})

    publish_metrics(metrics_from_result(result), arm="gateway")

    state = seen["state"]
    out = {
        "outcome": result.outcome,
        "action": result.action,
        "revised": result.revised,
        "conflicts": result.conflicts,
        "reason_calls": result.reason_calls,
        "attempts": result.attempts_made,
        "refusals": result.refusals,
        "binding": binding.name,
        "resource": binding.resource,
        "hard_limit": state.hard_limit if state else None,
        # Kept under its old name as well: the field is in CloudWatch dashboards
        # and in `deploy/invoke.py`. It now carries the COMPILED limit, so a
        # reader who has both sees the same number from a different mechanism.
        "inferred_ceiling": state.policy_limit if state else None,
        "dsn_source": _DSN_SOURCE,
        "run_id": run_id,
    }
    if state is not None:
        out.update(state.as_dict())
        out["still_permitted"] = [f"allocate({a})"
                                  for a in state.permitted(binding.actions)]

    # A policy that cannot authorize is reported as a refusal even when the
    # agent abstained rather than proposing something. Returning 200 "abstained"
    # would let a caller read "nothing to do here" from what is actually
    # "this account is unenforceable and needs attention".
    if state is not None and not state.authorizes and result.outcome != "committed":
        out["outcome"] = "refused"
        out["reason"] = state.detail
        return _reply(409, out)
    if result.outcome == "refused":
        out["reason"] = result.error
        out["refused_actions"] = list(result.refused_actions)
        # 409, not 500. The system worked: a write was stopped because it
        # violated the compiled policy the agent is held to. That is a business
        # outcome to be handled, not a fault to be retried.
        return _reply(409, out)
    if result.outcome == "error":
        out["reason"] = result.error
        return _reply(500, out)
    return _reply(200, out)


def _reply(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, default=str),
    }


if __name__ == "__main__":
    # Local invocation, so the handler is testable without deploying.
    import argparse
    ap = argparse.ArgumentParser(description="invoke the gateway locally")
    ap.add_argument("--account", default="hero-001")
    ap.add_argument("--agent", default="local-agent")
    ap.add_argument("--binding", default=DEFAULT_BINDING,
                    help="which bindings/<name>.yaml resource to enforce")
    args = ap.parse_args()
    print(json.dumps(handler({"body": json.dumps(
        {"account_id": args.account, "agent_id": args.agent,
         "binding": args.binding})}, None), indent=2))
