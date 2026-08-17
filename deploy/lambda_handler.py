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
step, enforces the retrieved policy before the commit, and answers with either a
committed decision or a refusal that names the rule. Every attempt is logged with
the constraint the agent believed it was under.

    POST /decide
      { "account_id": "...", "agent_id": "...", "requested": 45 }

    200 { "outcome": "committed", "action": "allocate(35)", "revised": true,
          "conflicts": 1, "inferred_ceiling": 60 }
    409 { "outcome": "refused", "reason": "total would be $80, over the $60
          ceiling", "refused_actions": ["allocate(45)"] }

A `409` is the interesting response. It means an agent proposed something its own
policy forbade and the write was stopped before it landed -- the case that is
silent everywhere else.

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
import re
import sys
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import psycopg

from racelab.conflict import ConflictAware, DecisionContext
from racelab.integrations.aws import (log_decision, log_event,
                                      metrics_from_result, publish_metrics,
                                      resolve_dsn)

HARD_LIMIT_DEFAULT = 100
OPTIONS = (45, 40, 35)

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
    """The proposal. `action` is the only attribute the wrapper requires."""

    def __init__(self, action: str, amount: int | None, inferred_ceiling: int | None,
                 rationale: str = ""):
        self.action = action
        self.amount = amount
        self.inferred_ceiling = inferred_ceiling
        self.rationale = rationale


def _read_total(account_id: str):
    def read(cur) -> int:
        cur.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM allocations WHERE account_id = %s",
            (account_id,))
        return int(cur.fetchone()[0])
    return read


# One statement instead of two. The total and the policy that constrains it were
# separate round trips, and at ~91 ms each that is measurable. Combining them
# also *strengthens* the correctness argument rather than trading against it:
# both values now provably come from the same read timestamp, where before they
# merely happened to.
_READ_STATE = """
WITH total AS (
    SELECT COALESCE(SUM(amount), 0) AS t
    FROM allocations WHERE account_id = %(acct)s
), pol AS (
    -- The ORDER BY is inside the aggregate deliberately. An ORDER BY in the
    -- subquery does not guarantee the order `array_agg` collects in, and the
    -- newest policy must be first: the whole scenario turns on a superseding
    -- policy winning over the one it replaced. It happened to come out right
    -- in testing, which is exactly how this kind of bug survives to production.
    SELECT array_agg(memory_id ORDER BY created_at DESC) AS ids,
           array_agg(text ORDER BY created_at DESC) AS texts
    FROM (
        SELECT memory_id, text, created_at FROM memories
        WHERE account_id = %(acct)s AND kind = 'policy'
        ORDER BY created_at DESC LIMIT 4
    ) s
)
SELECT total.t, pol.ids, pol.texts FROM total, pol
"""


def _read_state(cur, account_id: str) -> tuple[int, int | None, list[str]]:
    """Read the running total and the governing policy in one round trip."""
    cur.execute(_READ_STATE, {"acct": account_id})
    row = cur.fetchone()
    total = int(row[0] or 0)
    ids = list(row[1] or [])
    texts = list(row[2] or [])
    ceiling = None
    for text in texts:
        found = re.search(r"\$\s*(\d+)", text or "")
        if found:
            ceiling = int(found.group(1))
            break
    return total, ceiling, ids


def _infer_ceiling(cur, account_id: str) -> tuple[int | None, list[str]]:
    """The policy, from retrieved memory rather than from a column.

    Ordered by `created_at DESC` so a superseding policy wins. In the full
    system this is a vector search over `memories.embedding`; here it is the
    same table read directly, because the gateway must not depend on an
    embedding call being available to enforce a rule it has already been told.
    """
    cur.execute(
        "SELECT memory_id, text FROM memories "
        "WHERE account_id = %s AND kind = 'policy' "
        "ORDER BY created_at DESC LIMIT 4", (account_id,))
    rows = cur.fetchall()
    import re
    for memory_id, text in rows:
        found = re.search(r"\$\s*(\d+)", text or "")
        if found:
            return int(found.group(1)), [r[0] for r in rows]
    return None, [r[0] for r in rows]


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
    hard_limit = int(payload.get("hard_limit") or HARD_LIMIT_DEFAULT)
    run_id = payload.get("run_id") or getattr(context, "aws_request_id", "local")

    ceiling_seen: dict = {"value": None, "ids": []}

    def reason(ctx: DecisionContext) -> Decision:
        binding = hard_limit if ceiling_seen["value"] is None else min(
            ceiling_seen["value"], hard_limit)
        remaining = binding - ctx.observed
        amount = next((a for a in OPTIONS if a <= remaining), None)
        decision = Decision(
            action=f"allocate({amount})" if amount else "abstain",
            amount=amount,
            inferred_ceiling=ceiling_seen["value"],
            rationale=(f"observed {ctx.observed}, binding {binding}, "
                       f"{remaining} remaining"),
        )
        log_decision(run_id=run_id, agent_id=agent_id, attempt_no=ctx.attempt_no,
                     observed=ctx.observed,
                     inferred_constraint=ceiling_seen["value"],
                     action=decision.action, refused=ctx.refused,
                     retrieved_ids=ceiling_seen["ids"])
        return decision

    def operational_read(cur) -> int:
        # Both reads happen inside the transaction, and in ONE statement. That is
        # the whole design: the policy this write is checked against is read at
        # the same timestamp as the total it is checked over. Reading the policy
        # outside the transaction would let it move between the check and the
        # commit; reading it in a second statement merely made them *likely* to
        # match. One statement makes it provable, and costs one round trip fewer.
        total, ceiling, ids = _read_state(cur, account_id)
        ceiling_seen["value"], ceiling_seen["ids"] = ceiling, ids
        return total

    def apply(cur, proposal) -> bool:
        if proposal.amount is None:
            return False
        cur.execute(
            "INSERT INTO allocations (allocation_id, account_id, agent_id, amount, run_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            (str(uuid.uuid4()), account_id, agent_id, proposal.amount, run_id))
        return True

    def constraint(cur, proposal) -> str | None:
        """Enforced here, before COMMIT, which is the only sound place for it."""
        total = _read_total(account_id)(cur)
        ceiling = ceiling_seen["value"]
        if total > hard_limit:
            return f"total would be ${total}, over the hard limit of ${hard_limit}"
        if ceiling is not None and total > ceiling:
            return f"total would be ${total}, over the ${ceiling} policy ceiling"
        return None

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
    except Exception as exc:  # noqa: BLE001
        log_event("unhandled", error=f"{type(exc).__name__}: {exc}", level="ERROR")
        return _reply(500, {"error": "internal error"})

    publish_metrics(metrics_from_result(result), arm="gateway")

    out = {
        "outcome": result.outcome,
        "action": result.action,
        "revised": result.revised,
        "conflicts": result.conflicts,
        "reason_calls": result.reason_calls,
        "attempts": result.attempts_made,
        "refusals": result.refusals,
        "inferred_ceiling": ceiling_seen["value"],
        "policy_memory_ids": ceiling_seen["ids"],
        "dsn_source": _DSN_SOURCE,
        "run_id": run_id,
    }
    if result.outcome == "refused":
        out["reason"] = result.error
        out["refused_actions"] = list(result.refused_actions)
        # 409, not 500. The system worked: a write was stopped because it
        # violated a policy the agent itself had inferred. That is a business
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
    args = ap.parse_args()
    print(json.dumps(handler({"body": json.dumps(
        {"account_id": args.account, "agent_id": args.agent})}, None), indent=2))
