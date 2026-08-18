"""Compile an account's policy document into a constraint the gateway enforces.

This is the step that keeps the model out of the write path. It reads the
governing policy memory, compiles it once, and stores the result in
`policy_constraints`. From then on, every write is checked against that stored
constraint by SQL alone -- no model, no interpretation, no variance.

    python scripts/compile_policies.py --show
    python scripts/compile_policies.py --account hero-001
    python scripts/compile_policies.py --all
    python scripts/compile_policies.py --account hero-001 \
        --resolve "Authorization ceiling for this account is $60, cumulative."

## Why `--resolve` exists, and what it admits

Compiling this project's own hero policy produces an **unenforceable**
constraint, twice over:

    "$80 per billing cycle, pending completion of the quarterly review"
      -> a billing cycle can start on any day, so it is not calendar_month
      -> "pending the quarterly review" has no end date to compile

The compiler is right both times. Mapping a billing cycle onto a calendar month
would enforce the wrong window silently, which is precisely the failure the whole
compiler exists to stop. So the honest outcome is that the policy document, as
written, cannot be enforced -- and `racelab/policy_gate.py` refuses to authorize
anything against it.

`--resolve` is the operator saying what the rule means. The clarification is
compiled like any other policy text, stored as the next version, keyed to the
governing memory it interprets, and attributed to whoever ran it. It is not a
bypass: an unenforceable resolution is still unenforceable. It is the audit
trail for a human judgment that was going to be made anyway, made once and
recorded, instead of made implicitly by a regular expression on every write.

Storing a *knowingly* unenforceable constraint is also allowed
(`--accept-unenforceable`) and is occasionally the right thing: it pins a version
number to the refusal, so an operator reading the gateway's 409 can see which
compilation produced it.
"""

from __future__ import annotations

import argparse
import dataclasses
import getpass
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from racelab.binding import ResourceBinding  # noqa: E402
from racelab.db import connect  # noqa: E402
from racelab.policy import (PolicyError, columns_of, compile_policy,  # noqa: E402
                            current, store)


def governing(conn, account_id: str) -> tuple[str | None, str | None]:
    """The newest policy-kind memory: the document an agent would retrieve."""
    row = conn.execute(
        "SELECT memory_id, text FROM memories WHERE account_id = %s "
        "AND kind = 'policy' ORDER BY created_at DESC LIMIT 1",
        (account_id,)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def accounts_with_policy(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT account_id FROM memories WHERE kind = 'policy' "
        "ORDER BY account_id").fetchall()]


def show(conn) -> int:
    rows = conn.execute(
        "SELECT account_id, version, enforceable, fingerprint, source_memory_id, "
        "compiled_by, created_at FROM policy_constraints ORDER BY account_id, version"
    ).fetchall()
    if not rows:
        print("no compiled constraints yet")
        return 0

    # Which version governs is NOT simply the newest. The gate enforces the
    # constraint compiled from the document that is in force, so a reverted
    # policy brings its own older constraint back with it. Marking the highest
    # version number here would have told an operator the opposite of what the
    # gateway does, which is the kind of tooling mistake that gets believed.
    governs: dict[str, int] = {}
    for (account,) in {(r[0],) for r in rows}:
        memory_id, _ = governing(conn, account)
        if memory_id is None:
            # No policy document: the gate reports `none` and enforces only the
            # hard limit, whatever rows happen to sit in policy_constraints.
            continue
        best = [r[1] for r in rows if r[0] == account and r[4] == memory_id]
        if best:
            governs[account] = max(best)

    print(f"{'account':<20} {'v':>3}  {'enforceable':<11} {'fingerprint':<17} "
          f"{'from':<12} compiled_by")
    for account, version, enforceable, fingerprint, source, by, _ in rows:
        marker = "*" if governs.get(account) == version else " "
        print(f"{marker}{account:<19} {version:>3}  {str(bool(enforceable)):<11} "
              f"{fingerprint:<17} {str(source or '-'):<12} {(by or '?')[:40]}")
    print("\n* = the version the gateway would enforce right now (the one compiled")
    print("    from the policy document currently in force). An account with no")
    print("    star has a governing document nothing was compiled from: the")
    print("    gateway refuses every write against it.")
    return 0


def compile_one(conn, account_id: str, binding: ResourceBinding, *,
                resolution: str | None, accept_unenforceable: bool,
                force: bool, cols: set[str]) -> bool:
    """Compile and store one account's policy. Returns True if it is enforceable."""
    memory_id, text = governing(conn, account_id)
    if memory_id is None and resolution is None:
        print(f"  {account_id}: no policy document; nothing to compile "
              f"(the hard limit still binds)")
        return True

    source = resolution if resolution is not None else text
    existing = current(conn, account_id)

    template = binding.constraint_template()
    try:
        constraint = compile_policy(
            source, schema_columns=cols, source_memory_id=memory_id, **template)
    except PolicyError as exc:
        print(f"  {account_id}: FAILED to compile -- {exc}")
        return False

    if resolution is not None:
        # The stored text is what was actually compiled, and it says so. A
        # reader must never think the model produced this reading from the
        # original document when a person supplied the wording.
        constraint = dataclasses.replace(
            constraint,
            source_text=(f"[operator resolution of {memory_id or 'no document'} "
                         f"by {getpass.getuser()}] {source.strip()}"),
            compiled_by=f"{constraint.compiled_by} via operator:{getpass.getuser()}")

    print(f"  {account_id}: {constraint.describe()}")

    if not constraint.enforceable and not accept_unenforceable:
        print(f"    NOT STORED. The gateway will refuse writes against "
              f"{account_id} until this is resolved.")
        print(f"    Fix the policy text, or run again with --resolve "
              f"\"<what the rule means>\".")
        return False

    if (existing is not None and not force
            and existing.fingerprint == constraint.fingerprint
            and existing.source_memory_id == constraint.source_memory_id):
        print(f"    unchanged (v{existing.version}, {existing.fingerprint}); "
              f"not storing a duplicate version")
        return constraint.enforceable

    version = store(conn, account_id, constraint)
    print(f"    stored as v{version}  fingerprint {constraint.fingerprint}"
          + ("" if constraint.enforceable else "  [UNENFORCEABLE -- refuses all writes]"))
    return constraint.enforceable


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account", help="compile this account's governing policy")
    ap.add_argument("--all", action="store_true",
                    help="compile every account that has a policy memory")
    ap.add_argument("--show", action="store_true",
                    help="list stored constraints and exit")
    ap.add_argument("--binding", default="allocations",
                    help="the resource binding the constraint addresses")
    ap.add_argument("--resolve", metavar="TEXT",
                    help="operator wording for a policy the compiler cannot express")
    ap.add_argument("--accept-unenforceable", action="store_true",
                    help="store an unenforceable constraint so the refusal has a version")
    ap.add_argument("--force", action="store_true",
                    help="store a new version even when nothing changed")
    args = ap.parse_args()

    binding = ResourceBinding.named(args.binding)

    with connect("crdb") as conn:
        if args.show:
            return show(conn)

        if args.resolve and not args.account:
            print("--resolve applies to one account; pass --account")
            return 2
        if not args.account and not args.all:
            print("pass --account <id>, --all, or --show")
            return 2

        with conn.cursor() as cur:
            binding.validate(cur)
            cols = columns_of(cur, binding.resource)

        targets = ([args.account] if args.account
                   else accounts_with_policy(conn))
        print(f"binding: {binding.describe()}")
        print(f"compiling {len(targets)} account(s)\n")

        enforceable = 0
        for account in targets:
            if compile_one(conn, account, binding,
                           resolution=args.resolve,
                           accept_unenforceable=args.accept_unenforceable,
                           force=args.force, cols=cols):
                enforceable += 1
            print()

        print(f"{enforceable}/{len(targets)} account(s) are now enforceable")
        if enforceable < len(targets):
            print("\nAn account whose policy did not compile is not unprotected:")
            print("its hard limit is still enforced, and the gateway refuses every")
            print("write rather than authorizing under a rule it cannot express.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
