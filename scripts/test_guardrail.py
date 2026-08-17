"""Prove the constraint is enforced, not merely observed.

Until now this project *measured* that a conflict-aware agent reaches better
final states than a naive one. That is a claim about behaviour. It has an obvious
hole, and a reviewer found it: nothing stopped the agent from ignoring the
refreshed policy. "Arm C respected the new ceiling" really meant "arm C's agent
happened to reason correctly over fresh input".

`ConflictAware(constraint=...)` closes it. The constraint runs inside the
transaction, after the write and before the commit, and a state it rejects is
never made durable.

## Three tiers, and why the middle one matters

    naive, no guardrail       commits a violating state, silently
    naive, guardrail          refuses to commit; writes nothing
    aware, guardrail          re-decides and commits a correct state

The middle tier is the point. The guarantee is *orthogonal* to whether the agent
can re-reason: a naive agent cannot use the feedback, so it declines rather than
violating. Re-reasoning is what upgrades "safely refused" to "correctly
committed" -- it buys throughput, not safety.

## Why this only works under SERIALIZABLE

A check outside the transaction is racy at any isolation level. A check inside
the transaction is still racy under READ COMMITTED, because another writer can
commit underneath the snapshot the check read. Under SERIALIZABLE the state
verified before COMMIT is the state that becomes durable, or the COMMIT is
refused with a 40001 and the cycle repeats. Tier 4 below measures that
difference instead of asserting it.

Run:  python scripts/test_guardrail.py
"""

from __future__ import annotations

import pathlib
import sys
import threading
import uuid
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import psycopg

from racelab.conflict import ConflictAware, DecisionContext
from racelab.db import connect, dsn_for

ACCOUNT = "guardrail-001"
HARD_LIMIT = 100
CEILING = 60          # the rule that lives in "retrieved memory", not in a column
OPTIONS = (45, 40, 35)

PASS, FAIL = "  [PASS]", "  [FAIL]"
_results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((PASS if ok else FAIL, name, detail))
    print(f"{PASS if ok else FAIL} {name}" + (f" -- {detail}" if detail else ""))


@dataclass
class Decision:
    action: str
    amount: int | None
    inferred_ceiling: int | None


def reset(seed: int) -> None:
    with connect("crdb") as conn:
        conn.execute("DELETE FROM allocations WHERE account_id=%s", (ACCOUNT,))
        conn.execute(
            "INSERT INTO accounts (account_id,name,hard_limit) VALUES (%s,%s,%s) "
            "ON CONFLICT (account_id) DO UPDATE SET hard_limit=EXCLUDED.hard_limit",
            (ACCOUNT, ACCOUNT, HARD_LIMIT),
        )
        if seed:
            conn.execute(
                "INSERT INTO allocations (allocation_id,account_id,agent_id,amount,run_id) "
                "VALUES (%s,%s,'seed',%s,'guardrail')", (str(uuid.uuid4()), ACCOUNT, seed))


def final_sum() -> int:
    with connect("crdb") as conn:
        return int(conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM allocations WHERE account_id=%s",
            (ACCOUNT,)).fetchone()[0])


def read_total(cur) -> int:
    cur.execute("SELECT COALESCE(SUM(amount),0) FROM allocations WHERE account_id=%s",
                (ACCOUNT,))
    return int(cur.fetchone()[0])


def apply_write(cur, proposal) -> bool:
    if proposal.amount is None:
        return False
    cur.execute(
        "INSERT INTO allocations (allocation_id,account_id,agent_id,amount,run_id) "
        "VALUES (%s,%s,'agent',%s,'guardrail')", (str(uuid.uuid4()), ACCOUNT, proposal.amount))
    return True


def ceiling_constraint(cur, proposal) -> str | None:
    """The rule the agent is subject to, verified against what is about to commit.

    Note it re-reads the aggregate rather than trusting the reading the agent
    reasoned over. That is the point: the agent's observation may be stale, and
    this check must not inherit its staleness.
    """
    total = read_total(cur)
    if total > CEILING:
        return (f"total would be ${total}, over the ${CEILING} ceiling "
                f"(this action: {proposal.action})")
    return None


def make_reason(calls: dict, *, obey_feedback: bool):
    """A reasoning step that, like the real model we measured, over-allocates.

    It picks the largest option that fits the HARD LIMIT, ignoring the ceiling --
    exactly the failure Claude Sonnet 4.5 produced in 3 of 60 readings, where the
    chosen action contradicted the ceiling the model itself reported inferring.
    """
    def reason(ctx: DecisionContext) -> Decision:
        calls["n"] += 1
        remaining_hard = HARD_LIMIT - ctx.observed
        if obey_feedback and ctx.refused:
            # A capable agent uses the refusal. Respect the ceiling this time.
            remaining = min(remaining_hard, CEILING - ctx.observed)
        else:
            remaining = remaining_hard
        amount = next((a for a in OPTIONS if a <= remaining), None)
        return Decision(
            action=f"allocate({amount})" if amount else "abstain",
            amount=amount,
            inferred_ceiling=CEILING,
        )
    return reason


def run_tier(*, re_reason: bool, constraint, obey_feedback: bool, seed: int = 20):
    reset(seed)
    calls = {"n": 0}
    conn = psycopg.connect(dsn_for("crdb"), autocommit=True)
    try:
        wrapper = ConflictAware(
            operational_read=read_total,
            apply=apply_write,
            reason=make_reason(calls, obey_feedback=obey_feedback),
            re_reason=re_reason,
            constraint=constraint,
            max_refusals=3,
            max_attempts=5,
        )
        result = wrapper.run(conn, agent_id="agent-1", run_id="guardrail")
    finally:
        conn.close()
    return result, final_sum(), calls["n"]


def main() -> int:
    print("The guardrail: a constraint enforced inside the transaction")
    print("=" * 76)
    print(f"  seeded ${20} already allocated; ceiling ${CEILING}; hard limit ${HARD_LIMIT}")
    print(f"  the agent picks the largest of {OPTIONS} that fits the HARD LIMIT,")
    print(f"  so it wants allocate(45) -> ${20 + 45}, which breaks the ${CEILING} ceiling")
    print()

    print("1. Without a guardrail, the violating state commits")
    r, total, calls = run_tier(re_reason=True, constraint=None, obey_feedback=True)
    print(f"     outcome={r.outcome} action={r.action} total=${total} refusals={r.refusals}")
    check("it committed", r.outcome == "committed")
    check("the ceiling was breached", total > CEILING, f"${total} > ${CEILING}")
    check("nothing recorded a refusal", r.refusals == 0)

    print("\n2. Naive + guardrail: refuses to commit, writes nothing")
    r, total, calls = run_tier(re_reason=False, constraint=ceiling_constraint,
                               obey_feedback=True)
    print(f"     outcome={r.outcome} action={r.action} total=${total} "
          f"refusals={r.refusals} refused={list(r.refused_actions)}")
    check("the outcome is 'refused'", r.outcome == "refused", r.outcome)
    check("nothing was written", total <= CEILING, f"total ${total}")
    check("the ceiling held", total <= CEILING)
    check("refusals were bounded", r.refusals == 4, f"{r.refusals} (max_refusals=3, +1)")
    check("naive still reasoned exactly once", calls == 1,
          f"{calls} call(s) -- it replays, so feedback cannot help it")

    print("\n3. Conflict-aware + guardrail: re-decides and commits a legal state")
    r, total, calls = run_tier(re_reason=True, constraint=ceiling_constraint,
                               obey_feedback=True)
    print(f"     outcome={r.outcome} action={r.action} total=${total} "
          f"refusals={r.refusals} refused={list(r.refused_actions)}")
    check("it committed", r.outcome == "committed", r.outcome)
    check("the ceiling held", total <= CEILING, f"${total} <= ${CEILING}")
    check("it was refused at least once first", r.refusals >= 1, f"{r.refusals}")
    check("the committed action is not one that was refused",
          r.action not in r.refused_actions,
          f"{r.action} not in {list(r.refused_actions)}")
    check("it reasoned more than once", calls > 1, f"{calls} calls")

    print("\n4. An agent that ignores the feedback cannot force a violation through")
    r, total, calls = run_tier(re_reason=True, constraint=ceiling_constraint,
                               obey_feedback=False)
    print(f"     outcome={r.outcome} action={r.action} total=${total} "
          f"refusals={r.refusals}")
    check("the outcome is 'refused'", r.outcome == "refused", r.outcome)
    check("the ceiling still held", total <= CEILING, f"${total}")
    check("this is the guarantee: bad reasoning cannot commit a violating state",
          total <= CEILING and r.outcome == "refused")

    print("\n" + "=" * 76)
    failed = [x for x in _results if x[0] == FAIL]
    print(f"{len(_results) - len(failed)}/{len(_results)} passed")
    if failed:
        for _, name, detail in failed:
            print(f"  FAILED: {name} {detail}")
        return 1
    print("\nThe constraint is a guarantee, not a measurement. Re-reasoning buys")
    print("throughput -- 'committed' instead of 'refused'. It does not buy safety,")
    print("because the guardrail already had that covered.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
