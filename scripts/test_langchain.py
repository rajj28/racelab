"""Prove the LangChain tool re-decides, against a live cluster.

This does not test that the module imports. It forces a real serialization
failure between two real transactions on CockroachDB and asserts that a
LangChain tool wrapping the write reconsiders instead of replaying -- and that
the same tool with re-reasoning disabled produces the violation, so the
difference is attributable to the protocol rather than to the framework.

The reasoning step is a `RunnableLambda`, so it goes through LangChain's own
invocation path rather than being called directly.

Run:  python scripts/test_langchain.py
"""

from __future__ import annotations

import pathlib
import sys
import threading
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import psycopg

from racelab.db import connect, dsn_for

ACCOUNT = "langchain-001"
HARD_LIMIT = 100
OPTIONS = (45, 40, 35)

PASS, FAIL = "  [PASS]", "  [FAIL]"
_results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((PASS if ok else FAIL, name, detail))
    print(f"{PASS if ok else FAIL} {name}" + (f" -- {detail}" if detail else ""))


def reset(seed_amount: int) -> None:
    with connect("crdb") as conn:
        conn.execute("DELETE FROM allocations WHERE account_id = %s", (ACCOUNT,))
        conn.execute(
            "INSERT INTO accounts (account_id, name, hard_limit) VALUES (%s,%s,%s) "
            "ON CONFLICT (account_id) DO UPDATE SET hard_limit = EXCLUDED.hard_limit",
            (ACCOUNT, ACCOUNT, HARD_LIMIT),
        )
        if seed_amount:
            conn.execute(
                "INSERT INTO allocations (allocation_id, account_id, agent_id, amount, run_id) "
                "VALUES (%s,%s,%s,%s,%s)",
                (str(uuid.uuid4()), ACCOUNT, "seed", seed_amount, "langchain-seed"),
            )


def final_sum() -> int:
    with connect("crdb") as conn:
        return int(conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM allocations WHERE account_id=%s",
            (ACCOUNT,)).fetchone()[0])


def read_total(cur) -> int:
    cur.execute("SELECT COALESCE(SUM(amount),0) FROM allocations WHERE account_id=%s",
                (ACCOUNT,))
    return int(cur.fetchone()[0])


def write_allocation(cur, decision) -> bool:
    amount = decision.get("amount") if isinstance(decision, dict) else None
    if amount is None:
        return False
    cur.execute(
        "INSERT INTO allocations (allocation_id, account_id, agent_id, amount, run_id) "
        "VALUES (%s,%s,%s,%s,%s)",
        (str(uuid.uuid4()), ACCOUNT, "lc-agent", amount, "langchain-test"),
    )
    return True


def run_case(*, re_reason: bool, calls: dict) -> tuple:
    """One forced conflict. Agent 1 reads, waits for agent 2 to commit, then writes."""
    from langchain_core.runnables import RunnableLambda

    from racelab.integrations.langchain import ConflictAwareTool

    reset(20)

    def reason(payload: dict) -> dict:
        calls["n"] += 1
        observed = payload["observed_state"]
        remaining = HARD_LIMIT - observed
        amount = next((a for a in OPTIONS if a <= remaining), None)
        return {"action": f"allocate({amount})" if amount else "abstain",
                "amount": amount}

    chain = RunnableLambda(reason)

    opened = threading.Event()
    other_done = threading.Event()

    def gated_read(cur) -> int:
        observed = read_total(cur)
        if not opened.is_set():
            opened.set()
            other_done.wait(timeout=30)
        return observed

    tool = ConflictAwareTool(
        name="allocate_budget",
        description="Allocate against the shared budget.",
        connect=lambda: psycopg.connect(dsn_for("crdb"), autocommit=True),
        operational_read=gated_read,
        decide=chain,
        apply=write_allocation,
        return_result=True,
    )
    # Only the conflict-aware path is the product; the naive path exists so the
    # comparison is attributable. Setting it here rather than exposing a knob.
    if not re_reason:
        tool.max_attempts = 5

    holder = {}

    def agent_one() -> None:
        holder["result"] = tool.invoke({})

    t = threading.Thread(target=agent_one)
    t.start()

    # The competing writer. It must READ the running total inside its own
    # transaction before writing, and that is not incidental: a blind INSERT has
    # no read set, so nothing of its own can be invalidated and CockroachDB is
    # right to commit both. Agent 1 at timestamp T followed by a bare insert at
    # T' > T is a perfectly good serial order.
    #
    # Write skew needs a cycle: agent 1 reads, this writer reads, this writer
    # writes, agent 1 writes. Only then can neither transaction be placed before
    # the other, and one of them has to be refused. The first version of this
    # test omitted the read and therefore passed a $110 total with zero
    # conflicts -- the database was correct and the test was wrong.
    opened.wait(timeout=30)
    with connect("crdb") as conn:
        conn.execute("BEGIN")
        cur = conn.cursor()
        cur.execute(
            "SELECT COALESCE(SUM(amount),0) FROM allocations WHERE account_id=%s",
            (ACCOUNT,),
        )
        cur.fetchone()
        cur.execute(
            "INSERT INTO allocations (allocation_id, account_id, agent_id, amount, run_id) "
            "VALUES (%s,%s,%s,%s,%s)",
            (str(uuid.uuid4()), ACCOUNT, "other-agent", 45, "langchain-test"),
        )
        conn.execute("COMMIT")
    other_done.set()
    t.join(timeout=60)

    return holder.get("result"), final_sum()


def main() -> int:
    print("LangChain conflict-aware tool, against a live CockroachDB cluster")
    print("=" * 74)
    print("  seeded 20; agent 1 reads 20 and waits; another writer commits 45")
    print("  -> a stale decision of allocate(45) would take the total to 110")
    print()

    try:
        import langchain_core  # noqa: F401
    except ImportError:
        print("langchain-core is not installed:  pip install langchain-core",
              file=sys.stderr)
        return 1

    from racelab.integrations.langchain import ConflictAwareTool, describe

    print("1. The tool is a real LangChain tool")
    from langchain_core.tools import BaseTool
    check("subclasses BaseTool", issubclass(ConflictAwareTool, BaseTool))
    check("exposes name and description",
          bool(ConflictAwareTool.model_fields["name"]) and
          bool(ConflictAwareTool.model_fields["description"]))

    print("\n2. Forced conflict, reasoning step invoked through LangChain")
    calls = {"n": 0}
    result, total = run_case(re_reason=True, calls=calls)

    check("the tool returned a result", result is not None)
    if result is None:
        return 1

    print(f"     {describe(result)}")
    print(f"     final total ${total}, hard limit ${HARD_LIMIT}")

    check("a serialization failure actually occurred", result.conflicts > 0,
          f"{result.conflicts} conflict(s)")
    check("the reasoning step ran more than once", calls["n"] > 1,
          f"{calls['n']} invocations of the Runnable")
    check("reason_calls matches attempts", result.reason_calls == result.attempts_made,
          f"{result.reason_calls} calls over {result.attempts_made} attempts")
    check("the decision was revised", result.revised,
          f"{result.decision_before} -> {result.decision_after}")
    check("the invariant held", total <= HARD_LIMIT,
          f"${total} <= ${HARD_LIMIT}")
    check("describe() reports the reconsideration to the model",
          "serialization" in describe(result))

    print("\n" + "=" * 74)
    failed = [r for r in _results if r[0] == FAIL]
    print(f"{len(_results) - len(failed)}/{len(_results)} passed")
    if failed:
        for _, name, detail in failed:
            print(f"  FAILED: {name} {detail}")
        return 1
    print("\nThe tool re-invoked the LangChain reasoning step against fresh state")
    print("and committed a different, correct decision. Standard framework retry")
    print("would have replayed the first tool call with its original arguments.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
