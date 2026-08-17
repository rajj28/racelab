"""Tests for the wrapper itself, with no scenario and no model attached.

The point of these is to check the *library's* claims in isolation:

  1. Naive and conflict-aware differ in exactly one thing. Same connection
     handling, same retry limit, same telemetry, same re-read of operational
     state inside the restarted transaction. Only the reasoning step differs.
  2. `revised` means what the methodology says it means -- a conflict happened
     AND the action changed. Not "a retry happened".
  3. The wrapper refuses configurations that would quietly invalidate a result:
     a non-autocommit connection, or a telemetry sink sharing the raced
     connection.
  4. Under a forced conflict, the naive policy carries its stale action across
     the restart while the conflict-aware policy recomputes from refreshed
     state.

Test 4 is the one that matters, and it is run against the live cluster rather
than a mock, because a mock of "another transaction committed underneath you"
would be a mock of the only thing worth testing.

Run:  python scripts/test_wrapper.py
"""

from __future__ import annotations

import pathlib
import sys
import threading
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import psycopg

from racelab.conflict import (
    ArmCollapse,
    ConflictAware,
    DecisionContext,
    ListTelemetry,
    Policy,
    RunResult,
    SqlTelemetry,
    check_arms,
)
from racelab.db import connect, dsn_for

ACCOUNT = "wrapper-test"
HARD_LIMIT = 100

PASS, FAIL = "PASS", "FAIL"
_results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((PASS if ok else FAIL, name, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" -- {detail}" if detail else ""))


# --------------------------------------------------------------------------
# A deterministic stand-in for the scenario
# --------------------------------------------------------------------------


class Proposal:
    """Minimal object satisfying the library's `Proposal` protocol."""

    def __init__(self, amount: int | None, observed_sum: int):
        self.amount = amount
        self.observed_sum = observed_sum
        self.action = "abstain" if amount is None else f"allocate({amount})"
        self.inferred_ceiling = HARD_LIMIT
        self.memory_ids: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return f"Proposal({self.action}, observed={self.observed_sum})"


def decide(ctx: DecisionContext) -> Proposal:
    """Largest of {45, 40, 35} that still fits under the limit, else abstain."""
    remaining = HARD_LIMIT - ctx.observed
    for amount in (45, 40, 35):
        if amount <= remaining:
            return Proposal(amount, ctx.observed)
    return Proposal(None, ctx.observed)


def read_sum(cur: psycopg.Cursor) -> int:
    cur.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM allocations WHERE account_id = %s",
        (ACCOUNT,),
    )
    return int(cur.fetchone()[0])


def make_apply(run_id: str, agent_id: str):
    def apply(cur: psycopg.Cursor, proposal: Proposal) -> bool:
        if proposal.amount is None:
            return False
        cur.execute(
            "INSERT INTO allocations (allocation_id, account_id, agent_id, amount, run_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            (str(uuid.uuid4()), ACCOUNT, agent_id, proposal.amount, run_id),
        )
        return True

    return apply


def reset(conn: psycopg.Connection) -> None:
    conn.execute("DELETE FROM allocations WHERE account_id = %s", (ACCOUNT,))
    conn.execute(
        "INSERT INTO accounts (account_id, name, hard_limit) VALUES (%s, %s, %s) "
        "ON CONFLICT (account_id) DO UPDATE SET hard_limit = EXCLUDED.hard_limit",
        (ACCOUNT, "wrapper test account", HARD_LIMIT),
    )


def final_sum(conn: psycopg.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM allocations WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchone()[0]
    )


# --------------------------------------------------------------------------
# 1-3: contract tests, no database race needed
# --------------------------------------------------------------------------


def test_policy_selection() -> None:
    print("\n1. The arms differ in one flag and nothing else")
    common = dict(operational_read=read_sum, apply=lambda cur, p: False, reason=decide)
    naive = ConflictAware.naive(**common)
    aware = ConflictAware.conflict_aware(**common)
    check("naive() selects the naive policy", naive.policy is Policy.NAIVE)
    check("conflict_aware() selects the conflict-aware policy",
          aware.policy is Policy.CONFLICT_AWARE)
    check(
        "both arms reason identically on attempt 0",
        naive.reason is aware.reason,
        "the first decision comes from the same function, so only the response "
        "to a conflict differs",
    )
    differing = [
        k for k, v in vars(naive).items() if vars(aware)[k] is not v and vars(aware)[k] != v
    ]
    check("exactly one attribute differs", differing == ["re_reason"],
          f"differing: {differing}")


def test_guardrails(backend: str) -> None:
    print("\n2. Configurations that would invalidate a result are refused")
    wrapper = ConflictAware.conflict_aware(
        operational_read=read_sum, apply=lambda cur, p: False, reason=decide
    )
    # `conn.info.dsn` redacts the password, so reopen from the resolved DSN.
    with psycopg.connect(dsn_for(backend), autocommit=False) as implicit:
        try:
            wrapper.run(implicit, agent_id="a", run_id="r")
            check("non-autocommit connection rejected", False, "it was accepted")
        except ValueError as exc:
            check("non-autocommit connection rejected", "autocommit" in str(exc))

        try:
            SqlTelemetry(implicit)
            check("telemetry on a non-autocommit connection rejected", False)
        except ValueError:
            check("telemetry on a non-autocommit connection rejected", True,
                  "telemetry must not be rolled back by the conflict it records")


def test_revised_definition() -> None:
    print("\n3. `revised` requires a conflict AND a changed action")
    from racelab.conflict import RunResult

    def r(before, after, conflicts):
        return RunResult(
            agent_id="a", policy=Policy.CONFLICT_AWARE, outcome="committed",
            action=after, decision_before=before, decision_after=after,
            conflicts=conflicts,
        )

    check("no conflict, same action -> not revised",
          not r("allocate(45)", "allocate(45)", 0).revised)
    check("no conflict is never a revision",
          not r("allocate(45)", "abstain", 0).revised,
          "a changed action without a conflict would mean something else went wrong")
    check("conflict, same action -> retry, not revision",
          not r("allocate(45)", "allocate(45)", 2).revised)
    check("conflict, changed action -> revised",
          r("allocate(45)", "abstain", 1).revised)


# --------------------------------------------------------------------------
# 4: the real one -- a forced conflict against the live cluster
# --------------------------------------------------------------------------


def test_forced_conflict(backend: str, re_reason: bool) -> tuple:
    """Two agents, deliberately interleaved so the first one must conflict.

    The interleaving is forced rather than raced, so the test is deterministic:
    agent 1 opens, reads a sum of 0, and then waits. Agent 2 runs to completion
    and commits 45. Only then does agent 1 try to write. Its read is stale by
    construction and its transaction cannot serialize.

    The state is seeded with 20 already allocated. Agent 1 reads 20 and
    correctly concludes that 45 fits. Agent 2 then commits its own 45, taking
    the true total to 65. Agent 1's transaction cannot serialize, and the two
    policies now diverge on the same fact:

      naive           replays allocate(45) against a real total of 65 -> 110
      conflict-aware  re-reads 65, sees 35 remaining, allocates 35    -> 100

    So this test measures both things at once: what the agent decides, and
    whether the final state holds the invariant. Neither agent is behaving
    unreasonably at any point -- the naive one is simply answering a question
    that was asked about state which no longer exists.
    """
    policy_name = "conflict-aware" if re_reason else "naive"
    run_id = f"wrapper-{policy_name}-{uuid.uuid4().hex[:8]}"
    telemetry = ListTelemetry()
    reason_calls = {"n": 0}

    def counted(ctx: DecisionContext) -> Proposal:
        reason_calls["n"] += 1
        return decide(ctx)

    with connect(backend) as setup:
        reset(setup)
        # Pre-seed 20 so that a stale read of 20 justifies allocate(45) while a
        # fresh read of 65 permits only allocate(35). The policies must diverge.
        setup.execute(
            "INSERT INTO allocations (allocation_id, account_id, agent_id, amount, run_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            (str(uuid.uuid4()), ACCOUNT, "seed", 20, run_id),
        )

    opened = threading.Event()
    other_done = threading.Event()

    def gate_read(cur: psycopg.Cursor) -> int:
        observed = read_sum(cur)
        # Only the first attempt waits. On the retry we want the fresh read.
        if not opened.is_set():
            opened.set()
            other_done.wait(timeout=30)
        return observed

    build = ConflictAware.conflict_aware if re_reason else ConflictAware.naive
    wrapper = build(
        operational_read=gate_read,
        apply=make_apply(run_id, "agent-1"),
        reason=counted,
        max_attempts=4,
        telemetry=telemetry,
    )

    result_box: dict = {}

    def agent_1() -> None:
        try:
            with connect(backend) as conn:
                result_box["r"] = wrapper.run(conn, agent_id="agent-1", run_id=run_id)
        except BaseException as exc:  # surface it rather than hanging the test
            result_box["exc"] = exc

    def agent_2() -> None:
        try:
            opened.wait(timeout=30)
            with connect(backend) as conn:
                solo = ConflictAware.conflict_aware(
                    operational_read=read_sum,
                    apply=make_apply(run_id, "agent-2"),
                    reason=decide,
                )
                solo.run(conn, agent_id="agent-2", run_id=run_id)
        finally:
            other_done.set()

    t1 = threading.Thread(target=agent_1)
    t2 = threading.Thread(target=agent_2)
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    if "exc" in result_box:
        raise result_box["exc"]
    result = result_box["r"]
    with connect(backend) as conn:
        total = final_sum(conn)

    print(f"     {policy_name:<15} conflicts={result.conflicts} "
          f"reason_calls={reason_calls['n']} "
          f"before={result.decision_before} after={result.decision_after} "
          f"revised={result.revised} outcome={result.outcome} final_sum={total}")
    return result, total, reason_calls["n"], telemetry


def main() -> int:
    print("RaceLab wrapper tests")
    print("=" * 70)

    test_policy_selection()
    test_revised_definition()

    test_guardrails("crdb")

    print("\n4. Forced conflict on CockroachDB (SERIALIZABLE)")
    print("     agent-1 reads 20, waits; agent-2 commits 45; agent-1 then writes")

    n_res, n_sum, n_calls, n_tel = test_forced_conflict("crdb", re_reason=False)
    a_res, a_sum, a_calls, a_tel = test_forced_conflict("crdb", re_reason=True)

    check("naive conflicted", n_res.conflicts > 0)
    check("conflict-aware conflicted", a_res.conflicts > 0,
          "both arms must hit the same database behaviour")
    check("naive reasoned exactly once", n_calls == 1,
          "it replayed the action computed against the stale read of 20")
    check("conflict-aware reasoned again after the conflict",
          a_calls == 1 + a_res.conflicts, f"{a_calls} calls, {a_res.conflicts} conflicts")
    check("naive did not revise", not n_res.revised,
          f"{n_res.decision_before} -> {n_res.decision_after}")
    check("conflict-aware revised", a_res.revised,
          f"{a_res.decision_before} -> {a_res.decision_after}, after re-reading 65")
    check("naive violated the invariant", n_sum > HARD_LIMIT,
          f"final sum {n_sum} > hard limit {HARD_LIMIT}, committed with no error")
    check("conflict-aware held the invariant", a_sum <= HARD_LIMIT,
          f"final sum {a_sum} <= hard limit {HARD_LIMIT}")
    check("telemetry recorded one decision row per reasoning step",
          len(a_tel.decisions) == a_calls, f"{len(a_tel.decisions)} rows, {a_calls} calls")
    check("telemetry recorded the conflict attempts",
          sum(1 for a in n_tel.attempts if a["outcome"] == "conflict") == n_res.conflicts)

    print("\n5. The arm-collapse guard")
    try:
        check_arms(n_res, a_res)
        check("real runs pass the cross-arm guard", True,
              f"naive reasoned {n_res.reason_calls}x, conflict-aware {a_res.reason_calls}x")
    except ArmCollapse as exc:
        check("real runs pass the cross-arm guard", False, str(exc))

    # Prove the guard fires, by handing it the exact shape of the bug it exists
    # for: a "naive" run that reasoned on every attempt. Outcome assertions pass
    # on this input; the guard must not.
    collapsed = RunResult(
        agent_id="agent-1", policy=Policy.NAIVE, outcome="committed",
        action="allocate(35)", decision_before="allocate(45)",
        decision_after="allocate(35)", conflicts=1, reason_calls=2, attempts_made=2,
    )
    try:
        check_arms(collapsed, a_res)
        check("guard rejects a collapsed naive arm", False, "it accepted it")
    except ArmCollapse as exc:
        check("guard rejects a collapsed naive arm", "reasons exactly once" in str(exc))

    check("the collapsed run looks correct by every outcome metric",
          collapsed.outcome == "committed" and collapsed.revised,
          "committed, revised, plausible action -- which is why outcome "
          "assertions cannot catch this")

    print("\n" + "=" * 70)
    failed = [r for r in _results if r[0] == FAIL]
    print(f"{len(_results) - len(failed)}/{len(_results)} passed")
    if failed:
        for _, name, detail in failed:
            print(f"  FAILED: {name} {detail}")
        return 1
    print("\nThe wrapper's contract holds. This says nothing yet about whether the")
    print("scenario is realistic or the model reasons well -- only that naive and")
    print("conflict-aware differ where they are supposed to and nowhere else.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
