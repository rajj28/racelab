"""The four experimental arms.

Three arms compare policies. The fourth decomposes our own claim.

| Arm | Backend | Isolation | On a conflict |
|---|---|---|---|
| `A` | PostgreSQL 16 | READ COMMITTED | (conflicts are not surfaced) replay |
| `B` | CockroachDB | SERIALIZABLE | re-read operational state, replay the action |
| `C-ops` | CockroachDB | SERIALIZABLE | re-read operational state, **re-reason**, memory NOT refreshed |
| `C` | CockroachDB | SERIALIZABLE | refresh memory **and** re-read operational state, re-reason |

## Why C-ops exists

The conflict-aware policy does two things at once: it re-reads operational state
*and* it refreshes semantic memory. Comparing only C against B would show that
the pair of them helps, while saying nothing about which one did the work — and
it would let a weak claim ("the vector index is load-bearing") rest on an
argument rather than a measurement.

C-ops isolates that. It re-reasons on every conflict, exactly like C, but
reasons over **stale memory**: the memory context is retrieved once, before the
first attempt, and never refreshed. So C-ops minus B is the contribution of
re-reasoning over fresh operational state, and C minus C-ops is the contribution
of refreshing semantic memory.

Both outcomes are reportable and neither is embarrassing:

- If **C-ops already holds the invariant**, then in this scenario memory refresh
  is redundant, the operational re-read is doing the work, and we say so. That
  is a finding about where the technique's value actually comes from, and it
  narrows the claim honestly.
- If **C-ops fails where C succeeds**, the memory refresh is doing measurable
  work and the vector index is load-bearing in the results table rather than
  only in the query plan.

The hero scenario is built so that the second is expected — the ceiling changes
mid-run, and an agent reasoning over stale memory cannot see it. But "expected"
is a prediction, and the point of running the arm is that it can come out the
other way.

## What is held constant

Every arm gets the same agents, the same seeds, the same arrival offsets, the
same reasoning gap, the same intent cache and the same bounded action space.
Arms differ only in the columns of the table above. `A` differs from `B` in
backend and isolation with policy held fixed; `C-ops` and `C` differ from each
other in exactly one injected callable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ArmId(str, Enum):
    A = "A"
    B = "B"
    C_OPS = "C-ops"
    C = "C"


@dataclass(frozen=True)
class Arm:
    id: ArmId
    backend: str  # "pg" | "crdb"
    isolation: str | None  # None means the backend default
    re_reason: bool
    refresh_memory: bool
    label: str
    description: str

    @property
    def policy_name(self) -> str:
        if not self.re_reason:
            return "naive"
        return "conflict-aware" if self.refresh_memory else "conflict-aware (ops only)"


ARMS: dict[ArmId, Arm] = {
    ArmId.A: Arm(
        id=ArmId.A,
        backend="pg",
        isolation=None,  # READ COMMITTED, the PostgreSQL default
        re_reason=False,
        refresh_memory=False,
        label="A · postgres RC · naive",
        description=(
            "The control. READ COMMITTED permits the interleaving, so the "
            "conflict is never surfaced to the client and there is nothing to "
            "respond to."
        ),
    ),
    ArmId.B: Arm(
        id=ArmId.B,
        backend="crdb",
        isolation=None,  # SERIALIZABLE, the CockroachDB default
        re_reason=False,
        refresh_memory=False,
        label="B · cockroach · naive",
        description=(
            "Standard retry middleware. Restarts the transaction and re-reads "
            "operational state inside the new one, then replays the action it "
            "computed against the earlier reading."
        ),
    ),
    ArmId.C_OPS: Arm(
        id=ArmId.C_OPS,
        backend="crdb",
        isolation=None,
        re_reason=True,
        refresh_memory=False,
        label="C-ops · cockroach · re-reason, stale memory",
        description=(
            "The ablation. Re-reasons on every conflict over freshly read "
            "operational state, but over semantic memory retrieved once before "
            "the first attempt and never refreshed."
        ),
    ),
    ArmId.C: Arm(
        id=ArmId.C,
        backend="crdb",
        isolation=None,
        re_reason=True,
        refresh_memory=True,
        label="C · cockroach · full refresh",
        description=(
            "The full policy. Treats a serialization failure as a reason to "
            "discard the result, refresh both semantic memory and operational "
            "state, and decide again."
        ),
    ),
}

ORDER = [ArmId.A, ArmId.B, ArmId.C_OPS, ArmId.C]


def contributions(by_arm: dict[ArmId, float]) -> dict[str, float | None]:
    """Decompose a metric into the contribution of each mechanism.

    Returns the two differences the ablation exists to produce. `None` where an
    arm is missing, rather than a zero that would read as "no effect".
    """
    def delta(later: ArmId, earlier: ArmId) -> float | None:
        if later not in by_arm or earlier not in by_arm:
            return None
        return by_arm[later] - by_arm[earlier]

    return {
        "isolation_surfaces_conflict": delta(ArmId.B, ArmId.A),
        "re_reasoning_over_fresh_operational_state": delta(ArmId.C_OPS, ArmId.B),
        "refreshing_semantic_memory": delta(ArmId.C, ArmId.C_OPS),
    }
