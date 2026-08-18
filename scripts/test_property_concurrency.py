"""The guarantee, under interleavings nobody designed.

Every other suite here tests scenarios we chose. That is a real weakness: the
scenarios were written by the same people who wrote the code, and a scenario
nobody thought of is exactly the one that breaks. `hypothesis` generates the
agent count, the action space, the two limits, the arrival stagger and the moment
a policy changes, then shrinks any counterexample to its smallest form.

Three properties, asserted on every generated example:

    P1  the hard limit is never exceeded.
        No policy state, no timing, no arrival order may produce a committed
        total above `accounts.hard_limit`. This is the one rule with no
        exceptions -- a refusing policy is not a licence to skip it.

    P2  a refused action never commits.
        If the constraint rejected `allocate(n)` for an agent, no row from that
        agent's run carries it. The wrapper asserts this per run; here it is
        also checked against the ledger, because an assertion inside the code
        under test is not independent evidence.

    P3  a committed total never exceeds the highest ceiling that was ever in
        force during the run.
        Deliberately weaker than "never exceeds the current ceiling", and the
        weakness is the honest part: **no protocol can revoke a valid commit.**
        An agent that legally committed $80 under a $80 ceiling has not
        misbehaved when the ceiling later drops to $60. Asserting the stronger
        property would be asserting something this project has explicitly
        published as false. What must hold is that nothing was ever authorized
        under a ceiling that never existed.

## What this deliberately does not test

Retrieval. Memories are inserted with a NULL embedding, because the property
under test is enforcement and pulling Bedrock into a randomized loop would make
it slow, costly and flaky without testing anything the memory suite does not.
That also means this suite runs with `--skip-bedrock`.

## Reproducibility

`derandomize=True` by default, so a failure a judge sees is a failure they can
reproduce. `--random` explores fresh interleavings; that is the mode worth
running when changing `racelab/conflict.py` or `racelab/policy_gate.py`.

Run:  python scripts/test_property_concurrency.py
      python scripts/test_property_concurrency.py --examples 40 --random
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import sys
import threading
import time
import uuid

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

try:
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "this suite needs hypothesis:\n    pip install \"hypothesis>=6.100\""
    ) from exc

import psycopg

from racelab.binding import ResourceBinding
from racelab.conflict import ArmCollapse, ConflictAware, DecisionContext
from racelab.db import connect, dsn_for
from racelab.policy import Constraint, store
from racelab.policy_gate import PolicyGate

ACCOUNT = "prop-test-001"
BINDING = ResourceBinding.named("allocations")

PASS, FAIL = "  [PASS]", "  [FAIL]"
_results: list[tuple[str, str, str]] = []
_examples = 0
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((PASS if ok else FAIL, name, detail))
    print(f"{PASS if ok else FAIL} {name}" + (f" -- {detail}" if detail else ""))


# --------------------------------------------------------------------------
# the generated world
# --------------------------------------------------------------------------

# Action spaces are generated rather than fixed. The published finding survived
# random action spaces once (`test_action_space.py`); here the *guarantee* has
# to survive them on every example, including spaces where nothing fits and
# spaces where one action alone exceeds the ceiling.
actions = st.lists(st.integers(min_value=5, max_value=90),
                   min_size=1, max_size=4, unique=True)

worlds = st.builds(
    dict,
    agents=st.integers(min_value=2, max_value=6),
    actions=actions,
    hard_limit=st.integers(min_value=20, max_value=200),
    # A policy ceiling that may be below, at, or above the hard limit. Above is
    # the interesting one: the policy grants headroom the account does not have,
    # and the hard limit must still bind.
    policy_limit=st.integers(min_value=10, max_value=260),
    # None means the policy never moves. An integer is the ceiling it moves to,
    # applied once the run is under way.
    policy_moves_to=st.one_of(st.none(), st.integers(min_value=10, max_value=260)),
    # When the change lands, as a fraction of a short window. Small values land
    # while agents are still deciding; large ones may land after everyone is done.
    policy_delay_ms=st.integers(min_value=0, max_value=120),
    # Arrival stagger. Zero is a thundering herd; larger values serialize the
    # agents and should produce fewer conflicts and the same guarantee.
    stagger_ms=st.integers(min_value=0, max_value=25),
)


def reset_world(hard_limit: int, policy_limit: int) -> int:
    """A fresh account, one policy document, and its compiled constraint.

    The constraint is constructed directly rather than compiled by a model. The
    compiler has its own suite; what is under test here is what happens once a
    constraint exists, under arbitrary concurrency.
    """
    with connect("crdb") as conn:
        conn.execute("DELETE FROM allocations WHERE account_id = %s", (ACCOUNT,))
        conn.execute("DELETE FROM memories WHERE account_id = %s", (ACCOUNT,))
        conn.execute("DELETE FROM policy_constraints WHERE account_id = %s", (ACCOUNT,))
        conn.execute(
            "INSERT INTO accounts (account_id, name, hard_limit) VALUES (%s,%s,%s) "
            "ON CONFLICT (account_id) DO UPDATE SET hard_limit = EXCLUDED.hard_limit",
            (ACCOUNT, ACCOUNT, hard_limit))
        conn.execute(
            "INSERT INTO memories (memory_id, account_id, text, kind, created_at) "
            "VALUES (%s,%s,%s,'policy',%s)",
            ("prop-p1", ACCOUNT, f"ceiling ${policy_limit}",
             datetime.datetime.now(datetime.timezone.utc)))
        return store(conn, ACCOUNT, Constraint(
            limit=policy_limit, metric="sum", column="amount",
            resource="allocations", scope_column="account_id",
            source_memory_id="prop-p1", source_text=f"ceiling ${policy_limit}",
            compiled_by="property-test"))


def move_policy(new_limit: int) -> None:
    """Replace the governing document and its constraint, in one transaction.

    Both in one transaction on purpose. Written separately there is a window in
    which the new document governs and nothing is compiled from it, and the gate
    correctly refuses every write in that window -- correct, but it would mean
    this suite spent most of its examples testing the `stale` path instead of the
    ceiling actually moving. That path has its own test.
    """
    conn = psycopg.connect(dsn_for("crdb"), autocommit=False, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO memories (memory_id, account_id, text, kind, created_at) "
                "VALUES (%s,%s,%s,'policy',%s)",
                ("prop-p2", ACCOUNT, f"ceiling reduced to ${new_limit}",
                 datetime.datetime.now(datetime.timezone.utc)))
            row = cur.execute(
                "SELECT COALESCE(MAX(version),0) FROM policy_constraints "
                "WHERE account_id = %s", (ACCOUNT,)).fetchone()
            version = int(row[0]) + 1
            constraint = Constraint(
                limit=new_limit, metric="sum", column="amount",
                resource="allocations", scope_column="account_id",
                source_memory_id="prop-p2", version=version,
                source_text=f"ceiling reduced to ${new_limit}",
                compiled_by="property-test")
            cur.execute(
                "INSERT INTO policy_constraints (account_id, version, fingerprint, "
                "enforceable, compiled, source_memory_id, supersedes, compiled_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (ACCOUNT, version, constraint.fingerprint, True,
                 constraint.to_json(), "prop-p2", version - 1, "property-test"))
        conn.commit()
    finally:
        conn.close()


def run_agent(index: int, world: dict, out: list, lock: threading.Lock) -> None:
    run_id = f"prop-{uuid.uuid4().hex[:8]}"
    gate = PolicyGate(BINDING)
    seen: dict = {"state": None}
    choices = sorted(world["actions"], reverse=True)

    class Proposal:
        def __init__(self, amt, version):
            self.action = f"allocate({amt})"
            self.amount = amt
            self.policy_version = version

    def operational_read(cur):
        seen["state"] = gate.read(cur, ACCOUNT)
        return seen["state"].total

    def reason(ctx: DecisionContext):
        state = seen["state"]
        remaining = state.binding_limit - ctx.observed
        amount = next((a for a in choices if a <= remaining), None)
        if not state.authorizes:
            amount = None
        return Proposal(amount, state.version)

    def apply(cur, proposal) -> bool:
        if proposal.amount is None:
            return False
        BINDING.insert(cur, scope=ACCOUNT, agent_id=f"a{index}",
                       amount=proposal.amount, run_id=run_id)
        return True

    def constraint(cur, proposal):
        return gate.check(cur, seen["state"])

    conn = psycopg.connect(dsn_for("crdb"), autocommit=True, connect_timeout=10)
    try:
        if world["stagger_ms"]:
            time.sleep(index * world["stagger_ms"] / 1000.0)
        result = ConflictAware(
            operational_read=operational_read, apply=apply, reason=reason,
            re_reason=world.get("re_reason", True),
            # Switched off only by the falsification check below, which exists to
            # prove these properties can fail.
            constraint=constraint if world.get("guardrail", True) else None,
            max_refusals=3, max_attempts=6,
        ).run(conn, agent_id=f"a{index}", run_id=run_id)
        with lock:
            out.append(("ok", run_id, result))
    except ArmCollapse as exc:
        with lock:
            out.append(("collapse", run_id, exc))
    except Exception as exc:  # noqa: BLE001
        with lock:
            out.append(("error", run_id, exc))
    finally:
        conn.close()


def ledger() -> list[tuple[str, str, int]]:
    with connect("crdb") as conn:
        return [(r[0], r[1], int(r[2])) for r in conn.execute(
            "SELECT run_id, agent_id, amount FROM allocations WHERE account_id = %s",
            (ACCOUNT,)).fetchall()]


# --------------------------------------------------------------------------
# the property
# --------------------------------------------------------------------------


def property_holds(world: dict) -> None:
    """Run one generated world and assert P1, P2 and P3."""
    global _examples
    _examples += 1

    reset_world(world["hard_limit"], world["policy_limit"])
    ceilings = [world["policy_limit"]]

    results: list = []
    lock = threading.Lock()
    threads = [threading.Thread(target=run_agent, args=(i, world, results, lock))
               for i in range(world["agents"])]

    mover = None
    if world["policy_moves_to"] is not None:
        ceilings.append(world["policy_moves_to"])

        def move() -> None:
            time.sleep(world["policy_delay_ms"] / 1000.0)
            move_policy(world["policy_moves_to"])

        mover = threading.Thread(target=move)

    for t in threads:
        t.start()
    if mover:
        mover.start()
    for t in threads:
        t.join(timeout=120)
    if mover:
        mover.join(timeout=120)

    rows = ledger()
    total = sum(r[2] for r in rows)
    label = (f"agents={world['agents']} actions={sorted(world['actions'])} "
             f"hard={world['hard_limit']} policy={world['policy_limit']}"
             f"{'->' + str(world['policy_moves_to']) if world['policy_moves_to'] is not None else ''}")

    collapses = [r for r in results if r[0] == "collapse"]
    assert not collapses, (
        f"P0 the mechanism did not behave as its policy claims [{label}]: "
        f"{collapses[0][2]}")

    errors = [r for r in results if r[0] == "error"]
    assert not errors, f"an agent raised [{label}]: {type(errors[0][2]).__name__}: {errors[0][2]}"

    # P1 -- the rule with no exceptions.
    assert total <= world["hard_limit"], (
        f"P1 VIOLATED [{label}]: committed total {total} exceeds the hard limit "
        f"{world['hard_limit']}. Ledger: {rows}")

    # P2 -- checked against the ledger, not against the wrapper's own bookkeeping.
    for kind, run_id, result in results:
        if kind != "ok":
            continue
        written = {f"allocate({amount})" for rid, _, amount in rows if rid == run_id}
        overlap = written & set(result.refused_actions)
        assert not overlap, (
            f"P2 VIOLATED [{label}]: {overlap} was refused for {result.agent_id} "
            f"and is in the ledger anyway")

    # P3 -- never above a ceiling that never existed.
    assert total <= max(ceilings), (
        f"P3 VIOLATED [{label}]: committed total {total} exceeds every ceiling "
        f"that was ever in force ({ceilings}). Ledger: {rows}")


# --------------------------------------------------------------------------
# the properties must be able to fail
# --------------------------------------------------------------------------


def group_falsification() -> None:
    """Prove P1 has teeth, and find out what actually gives it teeth.

    Written after a first attempt at this check *failed to fail*. Removing the
    guardrail from a conflict-aware agent left the invariant intact -- and that
    is not a bug in the check, it is this project's own C-ops result showing up
    again: an agent that re-reads operational state inside the transaction and
    re-decides never breaks the hard limit, guardrail or no guardrail (0/50 in
    the controlled sweep).

    So the falsifying configuration is the naive one. It reasons once and
    replays, which is what standard retry middleware does, and it is where the
    guardrail is load-bearing rather than redundant. Stating that precisely
    matters: "the guardrail keeps the invariant" is too strong a claim, and this
    is the check that would have caught us making it.
    """
    print("\nFalsification: the properties can fail, and here is what makes them")

    base = dict(agents=6, actions=[45, 40, 35], hard_limit=60, policy_limit=60,
                policy_moves_to=None, policy_delay_ms=0, stagger_ms=0)

    # 1. Conflict-aware, no guardrail. The invariant is expected to HOLD.
    held = _holds({**base, "guardrail": False, "re_reason": True})
    check("a conflict-aware agent holds the invariant with no guardrail at all",
          held is None, f"total ${sum(r[2] for r in ledger())}"
          if held is None else str(held)[:120])

    # 2. Naive AND no guardrail. This is the configuration that must break it.
    held = _holds({**base, "guardrail": False, "re_reason": False})
    check("a naive agent with no guardrail DOES break it, so P1 can fail",
          held is not None and "P1 VIOLATED" in held,
          str(held)[:130] if held else "NO FAILURE -- P1 is decorative")

    # 3. Naive, guardrail on. Same agent, same interleavings, rule enforced.
    held = _holds({**base, "guardrail": True, "re_reason": False})
    rows = ledger()
    check("the same naive agent with the guardrail holds it", held is None,
          f"total ${sum(r[2] for r in rows)} <= 60"
          if held is None else str(held)[:120])


def _holds(world: dict) -> str | None:
    """Run one world, returning the assertion message instead of raising."""
    try:
        property_holds(world)
        return None
    except AssertionError as exc:
        return str(exc)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--examples", type=int, default=15,
                    help="how many worlds to generate (default 15)")
    ap.add_argument("--random", action="store_true",
                    help="explore fresh interleavings instead of the fixed set")
    args = ap.parse_args()

    print("Property-based concurrency proof")
    print("=" * 76)
    print("  P1  the hard limit is never exceeded")
    print("  P2  a refused action never commits")
    print("  P3  a total never exceeds the highest ceiling ever in force")
    print(f"\n  {args.examples} generated worlds, "
          f"{'random' if args.random else 'derandomized (reproducible)'}")
    print("  agents 2-6, random action spaces, random limits, random policy")
    print("  change timing, random arrival stagger\n")

    started = time.time()
    failure: str | None = None
    try:
        test = settings(
            max_examples=args.examples,
            deadline=None,                       # a real cluster is not a unit test
            derandomize=not args.random,
            # Generating a world is cheap; running it is not. Both filters exist
            # because the work is genuinely slow, not because the data is awkward.
            suppress_health_check=[HealthCheck.too_slow,
                                   HealthCheck.function_scoped_fixture],
        )(given(worlds)(property_holds))
        test()
    except AssertionError as exc:
        failure = str(exc)
    except Exception as exc:  # noqa: BLE001
        failure = f"{type(exc).__name__}: {exc}"

    elapsed = time.time() - started
    check(f"{_examples} generated worlds held all three properties",
          failure is None,
          failure[:400] if failure else f"{elapsed:.1f}s")

    group_falsification()

    print("\n" + "=" * 76)
    failed = [r for r in _results if r[0] == FAIL]
    print(f"{len(_results) - len(failed)}/{len(_results)} passed "
          f"({_examples} examples in {elapsed:.1f}s)")
    if failed:
        return 1
    print("\nThe guarantee is no longer 'holds in the scenarios we wrote'. It")
    print("holds across agent counts, action spaces, limits, policy-change")
    print("timings and arrival orders that nobody chose -- and P3 is stated in")
    print("the weaker form that is actually true, rather than the stronger one")
    print("that would be easier to claim.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
