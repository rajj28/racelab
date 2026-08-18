"""Resolve the policy a write is actually held to, and refuse when it cannot.

## What changed, and why it matters

Until now both write paths -- the Lambda gateway and the MCP server -- found
their policy ceiling with `re.search(r"\\$\\s*(\\d+)")` over retrieved text. That
extractor cannot be wrong in an interesting way; it can only be wrong silently.
It reads `"TIER-2 refunds are capped at $250"` as a global $250 and applies it to
everyone, and it reads `"$250 per calendar month"` as `$250 ever`.

`racelab/policy.py` replaced interpretation with **compilation**: a model reads
the policy once, emits a structured `Constraint`, and that constraint is stored
and versioned. This module is the piece that was missing -- the thing that
decides, at write time, *which* compiled constraint governs, and what to do when
none does.

## The five states, and why four of them refuse

    none            no policy document exists for this scope. Only the hard
                    limit binds, and it always binds. Authorized.
    compiled        a current, enforceable constraint, compiled from the
                    governing document. Authorized, and its version is
                    recorded on every decision it permits.
    uncompiled      a policy document exists and nothing has been compiled from
                    it. REFUSED.
    stale           a constraint exists, but the governing document is not the
                    one it was compiled from -- Legal rewrote the rule and
                    nobody recompiled. REFUSED.
    unenforceable   the constraint compiled, and carries clauses the language
                    cannot express. REFUSED.

`uncompiled` and `stale` are the states the old regex could not have: it read
whatever text was newest and produced a number, so a policy change that nobody
noticed still yielded confident enforcement of *something*. Refusing is the
honest answer, and it is the one that fails in the direction of not spending
money.

**We measured this on our own corpus.** Both hero policies -- `"$80 per billing
cycle, pending completion of the quarterly review"` and `"reduced to $60 per
billing cycle"` -- compile to `unenforceable`. A billing cycle can start on any
day, so it is not `calendar_month`, and the compiler is right to refuse it. The
demo account therefore needs an operator to say what the rule means before this
gate will authorize anything against it (`scripts/compile_policies.py
--resolve`). That is not a defect of the compiler. It is the compiler telling us
our own policy document was ambiguous, which the regex never could.

## No model runs here

Compilation is a separate, explicit step. This module reads `policy_constraints`
and nothing else; it never calls Bedrock, never interprets text, and adds no
latency beyond one CTE in a statement the write path already issues. That is the
split `racelab/policy.py` argues for, made structural: interpretation is slow,
ambiguous and reviewable; enforcement is fast, deterministic and unattended.

## One statement, one timestamp

`read()` fetches the running total, the hard limit, the governing policy document
and the compiled constraint in a **single statement**, inside the caller's
transaction. That is not a latency optimization (though it is one). It is what
makes the guarantee provable: the policy this write is checked against was read
at the same timestamp as the total it is checked over, and under SERIALIZABLE
that timestamp is the one the commit lands at, or there is no commit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum

import psycopg

from .binding import ResourceBinding
from .policy import Constraint, PolicyError, columns_of


class PolicyStatus(str, Enum):
    NONE = "none"
    COMPILED = "compiled"
    UNCOMPILED = "uncompiled"
    STALE = "stale"
    UNENFORCEABLE = "unenforceable"
    MISMATCHED = "mismatched"
    NOT_IN_FORCE = "not_in_force"


# The states under which a write may proceed. Everything else refuses. Written
# as an allowlist rather than a list of refusals so that a state added later
# fails closed by default, which is the direction a mistake here should go.
AUTHORIZING = frozenset({PolicyStatus.NONE, PolicyStatus.COMPILED,
                         PolicyStatus.NOT_IN_FORCE})


@dataclass(frozen=True)
class GateState:
    """Everything the write path needs to know, read at one timestamp."""

    scope: str
    total: int
    hard_limit: int
    status: PolicyStatus
    detail: str
    constraint: Constraint | None = None
    version: int | None = None
    fingerprint: str | None = None
    governing_memory_id: str | None = None
    governing_text: str | None = None
    compiled_from: str | None = None

    @property
    def authorizes(self) -> bool:
        return self.status in AUTHORIZING

    @property
    def policy_limit(self) -> int | None:
        """The ceiling the policy imposes, or None when policy does not bind."""
        if self.status is PolicyStatus.COMPILED and self.constraint is not None:
            return self.constraint.limit
        return None

    @property
    def binding_limit(self) -> int:
        """The limit that actually stops an agent: the stricter of the two."""
        policy = self.policy_limit
        return self.hard_limit if policy is None else min(self.hard_limit, policy)

    def permitted(self, actions) -> list[int]:
        """Which actions would still fit, as things stand right now."""
        if not self.authorizes:
            return []
        remaining = self.binding_limit - self.total
        return [a for a in actions if a <= remaining]

    def as_dict(self) -> dict:
        """The policy provenance carried on every response and every decision."""
        out = {
            "policy_status": self.status.value,
            "policy_version": self.version,
            "policy_limit": self.policy_limit,
            "policy_fingerprint": self.fingerprint,
            "policy_source_memory": self.compiled_from,
            "governing_memory": self.governing_memory_id,
            "policy_detail": self.detail,
        }
        # The text of the rule in force, but only when the gate is refusing.
        # Whoever has to fix an `uncompiled` or `unenforceable` account needs to
        # read the sentence that could not be compiled, and making them go and
        # look it up by memory id is the difference between an actionable
        # refusal and a puzzling one. Omitted on the happy path, where it would
        # be noise on every successful write.
        if not self.authorizes and self.governing_text:
            out["governing_text"] = self.governing_text[:300]
        return out


class PolicyGate:
    """Reads the governing policy for one bound resource, inside a transaction."""

    def __init__(self, binding: ResourceBinding):
        self.binding = binding
        self._validated = False
        self._columns: set[str] | None = None

    # -- setup ------------------------------------------------------------

    def ensure_valid(self, cur: psycopg.Cursor) -> None:
        """Validate the binding against the live schema, once per process."""
        if self._validated:
            return
        self.binding.validate(cur)
        self._columns = columns_of(cur, self.binding.resource)
        self._validated = True

    # -- the read ---------------------------------------------------------

    def read_sql(self) -> str:
        """The combined read, built from identifiers the database confirmed.

        `memories` and `policy_constraints` are keyed by `account_id` whatever
        the bound resource's scope column happens to be called: they are the
        memory layer's own tables, and the scope key of a memory is not the same
        thing as the scope column of a ledger. A refunds binding scoped by
        `customer_id` stores that customer's id in `memories.account_id`.
        """
        b = self.binding
        parts = [
            f"WITH total AS (SELECT {b.aggregate_sql} AS t FROM {b.resource} "
            f"WHERE {b.scope_column} = %(scope)s)",
        ]
        limit_lookup = b.hard_limit_sql()
        if limit_lookup:
            parts.append(f"lim AS ({limit_lookup})")
        parts.append(
            "pol AS (SELECT memory_id, text FROM memories "
            "WHERE account_id = %(scope)s AND kind = 'policy' "
            "ORDER BY created_at DESC LIMIT 1)")
        # Two lookups, and the distinction between them is the whole point.
        #
        # `governing` is the constraint compiled from the document that is in
        # force RIGHT NOW. That -- not the highest version number -- is the rule
        # an agent is subject to. Keying on the document rather than on recency
        # means a reverted policy re-enters force with the constraint that was
        # compiled for it, instead of leaving the newest version in charge of a
        # document it never read.
        #
        # `newest` exists only to tell two failures apart: "a rule was compiled
        # for this account, just not for the current document" (stale -- someone
        # rewrote the policy and did not recompile) and "nothing was ever
        # compiled here" (uncompiled). Both refuse; they need different fixes,
        # and a message that cannot tell them apart sends an operator looking in
        # the wrong place.
        parts.append(
            "governing AS (SELECT version, compiled, enforceable, source_memory_id, "
            "fingerprint FROM policy_constraints WHERE account_id = %(scope)s "
            "AND source_memory_id = (SELECT memory_id FROM pol) "
            "ORDER BY version DESC LIMIT 1)")
        parts.append(
            "newest AS (SELECT version, source_memory_id FROM policy_constraints "
            "WHERE account_id = %(scope)s ORDER BY version DESC LIMIT 1)")

        limit_expr = "(SELECT h FROM lim)" if b.hard_limit_table else "NULL"
        return (
            ", ".join(parts) + f"""
SELECT total.t,
       {limit_expr},
       (SELECT memory_id FROM pol),
       (SELECT text FROM pol),
       (SELECT version FROM governing),
       (SELECT compiled FROM governing),
       (SELECT enforceable FROM governing),
       (SELECT source_memory_id FROM governing),
       (SELECT fingerprint FROM governing),
       (SELECT version FROM newest),
       (SELECT source_memory_id FROM newest)
FROM total""")

    def read(self, cur: psycopg.Cursor, scope: str,
             hard_limit_fallback: int | None = None) -> GateState:
        """Resolve the governing policy. Runs inside the caller's transaction."""
        self.ensure_valid(cur)
        cur.execute(self.read_sql(), {"scope": scope})
        row = cur.fetchone()
        total = int(row[0] or 0)

        if self.binding.hard_limit_value is not None:
            hard_limit = self.binding.hard_limit_value
        elif row[1] is not None:
            hard_limit = int(row[1])
        elif hard_limit_fallback is not None:
            hard_limit = int(hard_limit_fallback)
        else:
            # An unknown scope with an unbounded budget is the shape of every
            # interesting incident. It is not a default worth having.
            raise PolicyError(
                f"{self.binding.hard_limit_table}.{self.binding.hard_limit_column} "
                f"has no row for {scope!r}; there is no hard limit to enforce")

        governing_id, governing_text = row[2], row[3]
        version, compiled_raw = row[4], row[5]
        compiled_from, fingerprint = row[7], row[8]
        newest_version, newest_from = row[9], row[10]

        base = dict(scope=scope, total=total, hard_limit=hard_limit,
                    governing_memory_id=governing_id,
                    governing_text=governing_text,
                    compiled_from=compiled_from, version=version,
                    fingerprint=fingerprint)

        if self.binding.policy_limit == "none":
            return GateState(status=PolicyStatus.NONE, constraint=None,
                             detail="this resource is bound with policy_limit: none; "
                                    "only the hard limit binds", **base)

        # No policy document, whatever may be sitting in policy_constraints. A
        # constraint whose document has been withdrawn is not a rule in force,
        # and treating it as one would enforce a policy nobody can read.
        if governing_id is None:
            return GateState(
                status=PolicyStatus.NONE, constraint=None,
                detail=f"no policy document exists for {scope!r}; the hard limit "
                       f"of {hard_limit} is the only rule", **base)

        if compiled_raw is None:
            if newest_version is not None:
                return GateState(
                    status=PolicyStatus.STALE, constraint=None,
                    detail=(f"the newest compiled policy for {scope!r} is v{newest_version}, "
                            f"compiled from {newest_from!r}, but {governing_id!r} now "
                            f"governs. The rule changed and was not recompiled; nothing "
                            f"will be authorized against the withdrawn version. Run "
                            f"scripts/compile_policies.py --account {scope}"), **base)
            return GateState(
                status=PolicyStatus.UNCOMPILED, constraint=None,
                detail=(f"a policy document ({governing_id}) governs {scope!r} and "
                        f"nothing has been compiled from it, so there is no rule "
                        f"this write can be checked against. Run "
                        f"scripts/compile_policies.py --account {scope}"), **base)

        constraint = Constraint.from_json(
            compiled_raw if isinstance(compiled_raw, str) else json.dumps(compiled_raw))

        mismatch = self.binding.matches(constraint)
        if mismatch:
            return GateState(
                status=PolicyStatus.MISMATCHED, constraint=constraint,
                detail=(f"policy v{version} does not address this resource: {mismatch}"),
                **base)

        if not constraint.enforceable:
            return GateState(
                status=PolicyStatus.UNENFORCEABLE, constraint=constraint,
                detail=(f"policy v{version} has clauses the constraint language cannot "
                        f"express, so it authorizes nothing: "
                        f"{'; '.join(constraint.unsupported)}"), **base)

        if not constraint.in_force():
            return GateState(
                status=PolicyStatus.NOT_IN_FORCE, constraint=constraint,
                detail=(f"policy v{version} is not in force today "
                        f"(from {constraint.effective_from}, until "
                        f"{constraint.effective_until}); only the hard limit binds"),
                **base)

        return GateState(status=PolicyStatus.COMPILED, constraint=constraint,
                         detail=f"policy v{version}: {constraint.describe()}", **base)

    # -- the check --------------------------------------------------------

    def check(self, cur: psycopg.Cursor, state: GateState) -> str | None:
        """Evaluate both rules against state as it stands. None means satisfied.

        Designed to be handed straight to `ConflictAware(constraint=...)`, which
        calls it inside the transaction after the write and before the commit --
        the only place the answer is still true when the commit lands.

        The hard limit is checked **first and always**, including in states where
        policy refuses. A refusing policy must not become a reason to skip the
        one rule the database can enforce unaided.
        """
        total = self.binding.read_total(cur, state.scope)

        if total > state.hard_limit:
            return (f"{self.binding.metric} would be {total}, over the hard limit "
                    f"of {state.hard_limit}")

        if not state.authorizes:
            return (f"this resource is not writable while the policy is "
                    f"{state.status.value}: {state.detail}")

        if state.status is PolicyStatus.COMPILED and state.constraint is not None:
            verdict = state.constraint.check(cur, state.scope, self._columns)
            if verdict:
                return verdict
        return None
