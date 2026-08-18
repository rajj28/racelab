"""Enforce a resource this repository contains no code for.

Everything else here is demonstrated on `allocations`, which proves the protocol
works and proves nothing about whether it *generalises*. This suite points the
same gateway at `refunds` -- a table with a different name, a different scope
column, a different limit column and a different action space -- declared in
`bindings/refunds.yaml` and implemented nowhere.

    grep -rn "refund" racelab/ deploy/     # nothing. That is the claim.

Five groups:

    1. validation     a binding is edited text that reaches SQL; prove it cannot
    2. gateway        the deployed handler enforces refunds, unmodified
    3. contention     the invariant holds under concurrent writers
    4. cross-resource a constraint compiled for another table is refused
    5. provenance     every decision names the policy version it was made under

Run:  python scripts/test_binding.py
      python scripts/test_binding.py --create    # just build the demo tables
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import threading
import uuid

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import psycopg

from racelab.binding import BindingError, ResourceBinding
from racelab.conflict import (ArmCollapse, ConflictAware, DecisionContext,
                              SqlTelemetry)
from racelab.db import connect, dsn_for
from racelab.policy import Constraint, columns_of, compile_policy, store
from racelab.policy_gate import PolicyGate, PolicyStatus

CUSTOMER = "cust-demo-001"
REFUND_POOL = 500
POLICY = ("The refund ceiling for this customer is $300. This is a cumulative "
          "total across all refunds, with no reset period.")

PASS, FAIL = "  [PASS]", "  [FAIL]"
_results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((PASS if ok else FAIL, name, detail))
    print(f"{PASS if ok else FAIL} {name}" + (f" -- {detail}" if detail else ""))


# --------------------------------------------------------------------------
# the demo resource
# --------------------------------------------------------------------------

DDL = [
    """
    CREATE TABLE IF NOT EXISTS customers (
        customer_id TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        refund_pool INT  NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS refunds (
        refund_id   UUID PRIMARY KEY,
        customer_id TEXT NOT NULL,
        agent_id    TEXT NOT NULL,
        amount      INT  NOT NULL,
        run_id      TEXT NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS refunds_customer_idx ON refunds (customer_id)",
]


def create_tables() -> None:
    with connect("crdb") as conn:
        for stmt in DDL:
            conn.execute(stmt)
    print("  created customers + refunds")


def setup() -> None:
    """A customer, a refund pool, a policy document, and that policy compiled."""
    import datetime

    from racelab.embeddings import get_embedder
    from racelab.memory import MemoryStore

    create_tables()
    binding = ResourceBinding.named("refunds")
    with connect("crdb") as conn:
        conn.execute("DELETE FROM refunds WHERE customer_id = %s", (CUSTOMER,))
        conn.execute("DELETE FROM memories WHERE account_id = %s", (CUSTOMER,))
        conn.execute("DELETE FROM policy_constraints WHERE account_id = %s",
                     (CUSTOMER,))
        conn.execute(
            "INSERT INTO customers (customer_id, name, refund_pool) "
            "VALUES (%s,%s,%s) ON CONFLICT (customer_id) DO UPDATE SET "
            "refund_pool = EXCLUDED.refund_pool",
            (CUSTOMER, "Demo Customer", REFUND_POOL))

        # The policy lives in the same memory layer as every other policy. A
        # memory is scoped by `account_id` whatever the ledger's scope column is
        # called -- the scope key of a memory is not the scope column of a table.
        MemoryStore(conn, get_embedder("titan")).add(
            memory_id="refund-policy-1", account_id=CUSTOMER, text=POLICY,
            kind="policy",
            created_at=datetime.datetime.now(datetime.timezone.utc))

        with conn.cursor() as cur:
            binding.validate(cur)
            cols = columns_of(cur, binding.resource)
        constraint = compile_policy(POLICY, schema_columns=cols,
                                    source_memory_id="refund-policy-1",
                                    **binding.constraint_template())
        if not constraint.enforceable:
            raise SystemExit(f"the refund policy did not compile: "
                             f"{constraint.unsupported}")
        version = store(conn, CUSTOMER, constraint)
    print(f"  customer {CUSTOMER}  pool ${REFUND_POOL}  "
          f"policy v{version}: {constraint.describe()}")


def total() -> int:
    with connect("crdb") as conn:
        return int(conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM refunds WHERE customer_id = %s",
            (CUSTOMER,)).fetchone()[0])


def reset_ledger() -> None:
    with connect("crdb") as conn:
        conn.execute("DELETE FROM refunds WHERE customer_id = %s", (CUSTOMER,))


# --------------------------------------------------------------------------
# 1. validation
# --------------------------------------------------------------------------


def group_validation() -> None:
    print("\n1. A binding is edited text that reaches SQL")

    binding = ResourceBinding.named("refunds")
    check("bindings/refunds.yaml loads", binding.resource == "refunds",
          binding.describe())
    check("the aggregate is parsed, not trusted",
          binding.metric == "sum" and binding.amount_column == "amount")
    check("the hard limit resolves to a column, not a constant",
          (binding.hard_limit_table, binding.hard_limit_column)
          == ("customers", "refund_pool"))

    good = {"resource": "refunds", "scope_column": "customer_id",
            "aggregate": "SUM(amount)", "hard_limit": "customers.refund_pool",
            "actions": [50]}
    hostile = [
        ("resource injection", {**good, "resource": "refunds; DROP TABLE customers --"}),
        ("scope column injection", {**good, "scope_column": "customer_id OR 1=1"}),
        ("aggregate injection",
         {**good, "aggregate": "SUM(amount) FROM refunds; DROP TABLE customers --"}),
        ("hard limit injection", {**good, "hard_limit": "customers.refund_pool; DROP --"}),
        ("missing hard limit", {k: v for k, v in good.items() if k != "hard_limit"}),
        ("no actions", {**good, "actions": []}),
        ("negative action", {**good, "actions": [-50]}),
        # A typo'd key is the realistic failure. Ignoring it would silently drop
        # whatever it was meant to set -- including the hard limit.
        ("typo'd key", {**good, "hardlimit": 100}),
    ]
    for label, spec in hostile:
        rejected = False
        try:
            ResourceBinding.from_dict(spec)
        except BindingError:
            rejected = True
        check(f"rejected at parse: {label}", rejected)

    # Syntax is not the real defence. These are well-formed identifiers that do
    # not exist, and only the database can say so.
    print("\n   and the allowlist comes from the database, not from the file")
    with connect("crdb") as conn, conn.cursor() as cur:
        for label, spec in [
            ("a column that does not exist",
             {**good, "aggregate": "SUM(not_a_column)"}),
            ("a scope column that does not exist",
             {**good, "scope_column": "no_such_column"}),
            # The assumption stated in the module docstring, asserted.
            ("a limit table not keyed by the scope column",
             {**good, "hard_limit": "accounts.hard_limit"}),
        ]:
            rejected = False
            try:
                ResourceBinding.from_dict(spec).validate(cur)
            except BindingError:
                rejected = True
            check(f"rejected against the live schema: {label}", rejected)


# --------------------------------------------------------------------------
# 2. the gateway, unmodified
# --------------------------------------------------------------------------


def group_gateway() -> None:
    print("\n2. The deployed gateway handler enforces refunds, unmodified")
    print("   (deploy/lambda_handler.py contains no reference to refunds)")

    # The module docstring uses refunds as an *illustration* of the workflow, so
    # scanning the whole file would be scanning prose. The claim is about code:
    # no refund table, no refund column, no branch on a resource name.
    source = (REPO / "deploy" / "lambda_handler.py").read_text(encoding="utf-8")
    code = source.split('"""', 2)[-1]
    check("the handler's code mentions refunds nowhere",
          "refund" not in code.lower(),
          f"{len(code)} chars of code scanned (docstring excluded)")

    from deploy import lambda_handler

    reset_ledger()
    outcomes = []
    for i in range(4):
        reply = lambda_handler.handler({"body": json.dumps({
            "account_id": CUSTOMER, "agent_id": f"refund-agent-{i}",
            "binding": "refunds"})}, None)
        body = json.loads(reply["body"])
        outcomes.append((reply["statusCode"], body.get("action"),
                         body.get("policy_version")))
        print(f"     {reply['statusCode']} {body.get('outcome'):<10} "
              f"action={body.get('action')} total_after={total()} "
              f"policy=v{body.get('policy_version')} "
              f"limit={body.get('policy_limit')}")

    check("it committed against a table it has no code for",
          any(a and a.startswith("allocate(") for _, a, _ in outcomes),
          f"{[a for _, a, _ in outcomes]}")
    check("the compiled policy ceiling stopped it at $300, not the $500 pool",
          total() <= 300, f"total ${total()} (pool ${REFUND_POOL})")
    check("every reply names the policy version in force",
          all(v == 1 for _, _, v in outcomes), f"{[v for _, _, v in outcomes]}")

    # The action space came from the binding too, not from a constant in the
    # handler. `allocate(250)` is not in the allocations binding's [45, 40, 35].
    check("the actions came from the binding, not from the handler",
          any(a == "allocate(250)" for _, a, _ in outcomes),
          f"{[a for _, a, _ in outcomes]}")


# --------------------------------------------------------------------------
# 3. contention
# --------------------------------------------------------------------------


def group_contention(writers: int = 6) -> None:
    print(f"\n3. {writers} concurrent writers, and the invariant holds")
    print("   (per-thread connections, as the gateway has per-container)")

    reset_ledger()
    binding = ResourceBinding.named("refunds")
    results: list = []
    lock = threading.Lock()
    gate_open = threading.Event()

    def writer(i: int) -> None:
        run_id = f"bind-{uuid.uuid4().hex[:8]}"
        gate = PolicyGate(binding)
        seen: dict = {"state": None}

        class Proposal:
            def __init__(self, amt, version):
                self.action = f"allocate({amt})"
                self.amount = amt
                self.policy_version = version

        def operational_read(cur):
            seen["state"] = gate.read(cur, CUSTOMER)
            return seen["state"].total

        def reason(ctx: DecisionContext):
            state = seen["state"]
            remaining = state.binding_limit - ctx.observed
            amount = next((a for a in sorted(binding.actions, reverse=True)
                           if a <= remaining), None)
            return Proposal(amount, state.version)

        def apply(cur, proposal) -> bool:
            if proposal.amount is None:
                return False
            binding.insert(cur, scope=CUSTOMER, agent_id=f"w{i}",
                           amount=proposal.amount, run_id=run_id)
            return True

        def constraint(cur, proposal):
            return gate.check(cur, seen["state"])

        conn = psycopg.connect(dsn_for("crdb"), autocommit=True, connect_timeout=10)
        # A second connection, because telemetry written inside the raced
        # transaction is rolled back by the conflict it records. Group 5 reads
        # what this writes.
        audit = psycopg.connect(dsn_for("crdb"), autocommit=True, connect_timeout=10)
        try:
            gate_open.wait()
            out = ConflictAware(
                operational_read=operational_read, apply=apply, reason=reason,
                re_reason=True, constraint=constraint,
                max_refusals=3, max_attempts=6,
                telemetry=SqlTelemetry(audit),
            ).run(conn, agent_id=f"w{i}", run_id=run_id)
            with lock:
                results.append(out)
        except ArmCollapse as exc:
            with lock:
                results.append(exc)
        finally:
            conn.close()
            audit.close()

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(writers)]
    for t in threads:
        t.start()
    gate_open.set()
    for t in threads:
        t.join(timeout=120)

    collapsed = [r for r in results if isinstance(r, ArmCollapse)]
    check("no run was voided by the collapse guard", not collapsed,
          str(collapsed[0])[:100] if collapsed else "")

    runs = [r for r in results if not isinstance(r, ArmCollapse)]
    outcomes: dict = {}
    for r in runs:
        outcomes[r.outcome] = outcomes.get(r.outcome, 0) + 1
    conflicts = sum(r.conflicts for r in runs)
    print(f"     {len(runs)} writers -> {outcomes}, {conflicts} conflicts, "
          f"total ${total()}")

    check("the hard limit was never broken", total() <= REFUND_POOL,
          f"${total()} <= ${REFUND_POOL}")
    check("the compiled policy limit was never broken", total() <= 300,
          f"${total()} <= $300")
    # The assertion that would fail if the guardrail were merely advisory.
    committed_refused = [r for r in runs
                         if r.outcome == "committed" and r.action in r.refused_actions]
    check("no refused action was ever committed", not committed_refused,
          f"{len(committed_refused)} violations")
    check("at least one writer got in", outcomes.get("committed", 0) >= 1,
          f"{outcomes.get('committed', 0)} committed")


# --------------------------------------------------------------------------
# 4. cross-resource safety
# --------------------------------------------------------------------------


def group_cross_resource() -> None:
    print("\n4. A constraint compiled for another table is refused, not applied")
    print("   (it would evaluate cleanly and mean nothing, which is worse)")

    binding = ResourceBinding.named("refunds")
    foreign = Constraint(limit=999, metric="sum", column="amount",
                         resource="allocations", scope_column="account_id",
                         source_memory_id="refund-policy-1")
    check("the binding detects the wrong resource",
          binding.matches(foreign) is not None, str(binding.matches(foreign))[:90])

    with connect("crdb") as conn:
        store(conn, CUSTOMER, foreign)
        gate = PolicyGate(binding)
        with conn.cursor() as cur:
            state = gate.read(cur, CUSTOMER)
        check("the gate reports it as mismatched",
              state.status is PolicyStatus.MISMATCHED, state.status.value)
        check("and refuses to authorize under it", not state.authorizes,
              state.detail[:100])

        # A mismatched policy must not disable the hard limit. That rule is the
        # one the database can enforce unaided, and a policy failure is not a
        # reason to stop checking it.
        with conn.cursor() as cur:
            verdict = gate.check(cur, state)
        check("the hard limit is still checked while policy refuses",
              verdict is not None and "not writable" in verdict, str(verdict)[:90])

        # Put the real one back so the suite leaves the account usable.
        conn.execute(
            "DELETE FROM policy_constraints WHERE account_id = %s AND version = "
            "(SELECT max(version) FROM policy_constraints WHERE account_id = %s)",
            (CUSTOMER, CUSTOMER))
        with conn.cursor() as cur:
            state = gate.read(cur, CUSTOMER)
        check("removing it restores the compiled policy",
              state.status is PolicyStatus.COMPILED and state.policy_limit == 300,
              f"{state.status.value} limit={state.policy_limit}")


# --------------------------------------------------------------------------
# 5. provenance
# --------------------------------------------------------------------------


def group_provenance() -> None:
    print("\n5. Every decision names the policy version it was made under")

    with connect("crdb") as conn:
        rows = conn.execute(
            "SELECT d.policy_version, count(*) FROM decisions d "
            "JOIN refunds r ON r.run_id = d.run_id "
            "WHERE r.customer_id = %s GROUP BY 1", (CUSTOMER,)).fetchall()
    versions = {r[0] for r in rows}
    check("committed refund decisions carry a policy version",
          versions and None not in versions, f"versions {sorted(versions)}")

    # The question the column exists to answer, asked of this resource only --
    # a repo-wide count would pass on rows written by another suite.
    with connect("crdb") as conn:
        under_v1 = conn.execute(
            "SELECT count(*) FROM decisions d JOIN refunds r ON r.run_id = d.run_id "
            "WHERE r.customer_id = %s AND d.policy_version = 1",
            (CUSTOMER,)).fetchone()[0]
    check("'which refund decisions were made under v1?' is now a query",
          under_v1 >= 1, f"{under_v1} decisions")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--create", action="store_true",
                    help="create the demo tables and exit")
    args = ap.parse_args()

    if args.create:
        create_tables()
        return 0

    print("Declarative resource binding: enforce a table we wrote no code for")
    print("=" * 76)
    setup()

    group_validation()
    group_gateway()
    group_contention()
    group_cross_resource()
    group_provenance()

    print("\n" + "=" * 76)
    failed = [r for r in _results if r[0] == FAIL]
    print(f"{len(_results) - len(failed)}/{len(_results)} passed")
    if failed:
        for _, name, detail in failed:
            print(f"  FAILED: {name} {detail}")
        return 1
    print("\nThe gateway enforced a resource declared in twenty lines of YAML,")
    print("under a policy compiled from that resource's own document, with the")
    print("hard limit read from a column it was told about rather than one it")
    print("was written against.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
