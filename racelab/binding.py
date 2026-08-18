"""Declarative resource binding: point the gateway at **your** table.

## The problem this solves

Everything else in this project is general -- `ConflictAware` knows nothing about
allocations, and the compiled `Constraint` addresses a resource, a scope column
and a metric by name. The *gateway* was not. It had `allocations`, `account_id`
and `SUM(amount)` written into its SQL, so adopting it meant editing it.

A binding is the missing declaration. It names the table an agent writes to, the
column that scopes a limit, the aggregate the limit is over, where the hard limit
lives, and which actions the agent may propose:

    resource:     refunds
    scope_column: customer_id
    aggregate:    SUM(amount)
    hard_limit:   customers.refund_pool
    policy_limit: compiled
    actions:      [50, 100, 250]

That is the whole interface. With it, the gateway enforces a table it has never
heard of, under a policy compiled from that table's own documents, with the same
guarantee: the check runs inside the transaction that does the write.

## What is validated, and why so much of it

A binding is a configuration file, which means it is *text someone edits* -- and
every identifier in it reaches SQL. So it gets the same treatment model output
gets in `racelab/policy.py`: bare-identifier syntax first, then an allowlist read
from `information_schema` rather than declared. `validate()` is not optional
politeness; `PolicyGate` calls it before the first statement runs.

Nothing here is interpolated except identifiers that the database itself has
confirmed exist. Values are bound.

## The one assumption worth stating

`hard_limit: customers.refund_pool` looks up `refund_pool` in `customers` keyed
by the **same** `scope_column` the aggregate is grouped by. A refund pool is per
customer, and the refunds are per customer; if the two were keyed differently the
limit would not be a limit on this sum. Rather than inventing a join syntax, that
assumption is checked -- `scope_column` must exist on the limit table too -- and
a binding that violates it fails to validate instead of quietly summing the wrong
rows.

A constant is also allowed (`hard_limit: 100`), for a resource whose ceiling is
not stored anywhere.
"""

from __future__ import annotations

import json
import pathlib
import re
import uuid
from dataclasses import dataclass
from typing import Any

import psycopg

from .policy import Constraint, columns_of

BINDINGS_DIR = pathlib.Path(__file__).resolve().parents[1] / "bindings"

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_AGGREGATE = re.compile(r"^\s*(SUM|COUNT)\s*\(\s*([A-Za-z_][A-Za-z0-9_]*|\*)\s*\)\s*$",
                        re.IGNORECASE)


class BindingError(ValueError):
    """The binding does not describe something that can be enforced."""


def _ident(name: str, what: str) -> str:
    if not isinstance(name, str) or not _IDENT.match(name):
        raise BindingError(f"{what} {name!r} is not a valid identifier")
    return name


@dataclass(frozen=True)
class ResourceBinding:
    """One resource the gateway can enforce, declared rather than coded."""

    name: str
    resource: str
    scope_column: str
    metric: str                       # "sum" | "count"
    amount_column: str | None         # required for sum, None for count
    actions: tuple[int, ...]

    # Where the hard limit lives. Exactly one of these is set.
    hard_limit_table: str | None = None
    hard_limit_column: str | None = None
    hard_limit_value: int | None = None

    policy_limit: str = "compiled"    # "compiled" | "none"

    # Columns written on insert. `id_column` is filled with a fresh UUID when
    # named; omit it for a table whose primary key has a default.
    id_column: str | None = None
    agent_column: str = "agent_id"
    run_column: str = "run_id"
    timestamp_column: str = "created_at"

    description: str = ""

    # -- construction -----------------------------------------------------

    @classmethod
    def from_dict(cls, spec: dict, *, name: str | None = None) -> "ResourceBinding":
        unknown = set(spec) - {
            "name", "resource", "scope_column", "aggregate", "hard_limit",
            "policy_limit", "actions", "id_column", "agent_column",
            "run_column", "timestamp_column", "description",
        }
        if unknown:
            # A typo in a key would otherwise be ignored, and a binding that
            # silently drops `hard_limit` is a binding with no hard limit.
            raise BindingError(f"unknown key(s) in binding: {sorted(unknown)}")

        for required in ("resource", "scope_column", "aggregate", "actions"):
            if required not in spec:
                raise BindingError(f"binding is missing {required!r}")

        matched = _AGGREGATE.match(str(spec["aggregate"]))
        if not matched:
            raise BindingError(
                f"aggregate {spec['aggregate']!r} must be SUM(<column>) or COUNT(*)")
        metric, target = matched.group(1).lower(), matched.group(2)
        if metric == "sum":
            if target == "*":
                raise BindingError("SUM(*) is not a thing; name the column")
            amount_column = _ident(target, "aggregate column")
        else:
            if target != "*":
                raise BindingError("only COUNT(*) is supported, not COUNT(column)")
            amount_column = None

        table = column = None
        value = None
        raw_limit = spec.get("hard_limit")
        if raw_limit is None:
            raise BindingError(
                "binding is missing 'hard_limit'. A resource with no hard limit "
                "has nothing the database can enforce on its own, which is the "
                "one guarantee this gateway makes unconditionally")
        if isinstance(raw_limit, int) and not isinstance(raw_limit, bool):
            value = int(raw_limit)
        else:
            parts = str(raw_limit).split(".")
            if len(parts) != 2:
                raise BindingError(
                    f"hard_limit {raw_limit!r} must be an integer or 'table.column'")
            table = _ident(parts[0], "hard limit table")
            column = _ident(parts[1], "hard limit column")

        actions = tuple(int(a) for a in spec["actions"])
        if not actions:
            raise BindingError("a binding with no actions can propose nothing")
        if any(a <= 0 for a in actions):
            raise BindingError(f"actions must be positive; got {actions}")

        policy_limit = str(spec.get("policy_limit", "compiled")).lower()
        if policy_limit not in ("compiled", "none"):
            raise BindingError(
                f"policy_limit {policy_limit!r} must be 'compiled' or 'none'")

        return cls(
            name=str(spec.get("name") or name or spec["resource"]),
            resource=_ident(str(spec["resource"]), "resource"),
            scope_column=_ident(str(spec["scope_column"]), "scope_column"),
            metric=metric,
            amount_column=amount_column,
            actions=actions,
            hard_limit_table=table,
            hard_limit_column=column,
            hard_limit_value=value,
            policy_limit=policy_limit,
            id_column=(_ident(str(spec["id_column"]), "id_column")
                       if spec.get("id_column") else None),
            agent_column=_ident(str(spec.get("agent_column", "agent_id")),
                                "agent_column"),
            run_column=_ident(str(spec.get("run_column", "run_id")), "run_column"),
            timestamp_column=_ident(str(spec.get("timestamp_column", "created_at")),
                                    "timestamp_column"),
            description=str(spec.get("description", "")),
        )

    @classmethod
    def load(cls, path: str | pathlib.Path) -> "ResourceBinding":
        """Load one binding from a .yaml or .json file."""
        path = pathlib.Path(path)
        if not path.exists() and not path.suffix:
            for suffix in (".yaml", ".yml", ".json"):
                if path.with_suffix(suffix).exists():
                    path = path.with_suffix(suffix)
                    break
        if not path.exists():
            raise BindingError(f"no binding at {path}")
        raw = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            spec = json.loads(raw)
        else:
            try:
                import yaml
            except ImportError as exc:  # pragma: no cover
                raise BindingError(
                    "reading a .yaml binding needs PyYAML (pip install pyyaml); "
                    "a .json binding needs nothing") from exc
            spec = yaml.safe_load(raw)
        if not isinstance(spec, dict):
            raise BindingError(f"{path} does not contain a mapping")
        return cls.from_dict(spec, name=path.stem)

    @classmethod
    def named(cls, name: str) -> "ResourceBinding":
        """Load a binding by name from the repository's `bindings/` directory."""
        _ident(name.replace("-", "_"), "binding name")
        return cls.load(BINDINGS_DIR / name)

    # -- validation -------------------------------------------------------

    def validate(self, cur: psycopg.Cursor) -> None:
        """Check every identifier against the columns the database reports.

        Syntax was checked at construction; this is the check that cannot be
        satisfied by a well-formed lie. It runs before the gateway's first
        statement, so a mistyped column is a startup error rather than a
        malformed query at the moment of a write.
        """
        cols = columns_of(cur, self.resource)
        required = [(self.scope_column, "scope_column"),
                    (self.agent_column, "agent_column"),
                    (self.run_column, "run_column")]
        if self.amount_column:
            required.append((self.amount_column, "aggregate column"))
        if self.id_column:
            required.append((self.id_column, "id_column"))
        for column, what in required:
            if column not in cols:
                raise BindingError(
                    f"{what} {column!r} is not a column of {self.resource!r}; "
                    f"known: {sorted(cols)}")
        if self.timestamp_column not in cols:
            # Only a window-bearing policy needs it, so this is a warning-shaped
            # failure: report it now rather than when a windowed policy compiles.
            raise BindingError(
                f"timestamp_column {self.timestamp_column!r} is not a column of "
                f"{self.resource!r}; a windowed policy could not be enforced")

        if self.hard_limit_table:
            limit_cols = columns_of(cur, self.hard_limit_table)
            if self.hard_limit_column not in limit_cols:
                raise BindingError(
                    f"hard limit column {self.hard_limit_column!r} is not a column "
                    f"of {self.hard_limit_table!r}")
            if self.scope_column not in limit_cols:
                raise BindingError(
                    f"{self.hard_limit_table!r} has no {self.scope_column!r}, so the "
                    f"hard limit is not keyed the same way as the sum it limits. "
                    f"See the module docstring: that is an assumption this refuses "
                    f"to make silently")

    # -- reads ------------------------------------------------------------

    @property
    def aggregate_sql(self) -> str:
        return (f"COALESCE(SUM({self.amount_column}), 0)"
                if self.metric == "sum" else "COUNT(*)")

    def total_sql(self) -> str:
        return (f"SELECT {self.aggregate_sql} FROM {self.resource} "
                f"WHERE {self.scope_column} = %(scope)s")

    def read_total(self, cur: psycopg.Cursor, scope: str) -> int:
        cur.execute(self.total_sql(), {"scope": scope})
        row = cur.fetchone()
        return int(row[0] or 0)

    def hard_limit_sql(self) -> str | None:
        """The limit lookup, as a fragment `PolicyGate` folds into its own CTE.

        There is deliberately no `read_hard_limit()` helper beside `read_total`.
        Reading the limit in its own statement would put it at a different read
        timestamp from the sum it limits, which is the exact looseness the gate
        exists to remove -- so the only supported way to obtain it is inside the
        gate's single combined read.
        """
        if not self.hard_limit_table:
            return None
        return (f"SELECT {self.hard_limit_column} AS h FROM {self.hard_limit_table} "
                f"WHERE {self.scope_column} = %(scope)s LIMIT 1")

    # -- writes -----------------------------------------------------------

    def insert(self, cur: psycopg.Cursor, *, scope: str, agent_id: str,
               amount: int, run_id: str) -> None:
        """Write one row. Identifiers are validated; values are bound."""
        columns = [self.scope_column, self.agent_column, self.run_column]
        values: list[Any] = [scope, agent_id, run_id]
        if self.amount_column:
            columns.append(self.amount_column)
            values.append(amount)
        if self.id_column:
            columns.insert(0, self.id_column)
            values.insert(0, str(uuid.uuid4()))
        placeholders = ", ".join(["%s"] * len(columns))
        cur.execute(
            f"INSERT INTO {self.resource} ({', '.join(columns)}) "
            f"VALUES ({placeholders})", values)

    # -- policy -----------------------------------------------------------

    def constraint_template(self) -> dict:
        """The fields `compile_policy` needs to address *this* resource."""
        return {
            "resource": self.resource,
            "scope_column": self.scope_column,
            "timestamp_column": self.timestamp_column,
        }

    def matches(self, constraint: Constraint) -> str | None:
        """Does this compiled constraint address the resource we bound?

        A constraint compiled for `allocations` must never be enforced against
        `refunds`. It would evaluate cleanly and mean nothing, which is worse
        than an error.
        """
        if constraint.resource != self.resource:
            return (f"the compiled constraint is over {constraint.resource!r}, "
                    f"not {self.resource!r}")
        if constraint.scope_column != self.scope_column:
            return (f"the compiled constraint is scoped by "
                    f"{constraint.scope_column!r}, not {self.scope_column!r}")
        if constraint.metric != self.metric:
            return (f"the compiled constraint measures {constraint.metric!r}, "
                    f"but this resource is bound as {self.metric!r}")
        if self.metric == "sum" and constraint.column != self.amount_column:
            return (f"the compiled constraint sums {constraint.column!r}, "
                    f"not {self.amount_column!r}")
        return None

    def describe(self) -> str:
        limit = (f"{self.hard_limit_table}.{self.hard_limit_column}"
                 if self.hard_limit_table else str(self.hard_limit_value))
        return (f"{self.name}: {self.aggregate_sql} over {self.resource} "
                f"per {self.scope_column}, hard limit {limit}, "
                f"policy {self.policy_limit}, actions {list(self.actions)}")


# The binding the rest of this repository's scenario runs on. It is loaded from
# `bindings/allocations.yaml` like any other -- deliberately, so the path the
# gateway takes for our own scenario is the same path it takes for yours. A
# special case for the built-in resource would leave the general path untested
# by everything we run.
def default_binding() -> ResourceBinding:
    return ResourceBinding.named("allocations")


def available() -> list[str]:
    if not BINDINGS_DIR.exists():
        return []
    return sorted({p.stem for p in BINDINGS_DIR.iterdir()
                   if p.suffix in (".yaml", ".yml", ".json")})
