"""Ceiling inference and the bounded action space.

This module is the *reference* implementation of the reasoning step: it is
deterministic, has no model dependency, and exists so the corpus can be tested
for causal effect without spending Bedrock calls. Phase 3 puts Claude in this
position, choosing from the same action space and returning strict JSON, and the
two are compared.

Keeping a deterministic reference around is not a shortcut. It is how we can
tell the difference between "the model made a different choice" and "the
retrieved context changed", which are very different explanations for a change
in behaviour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Bounded, per the spec. The agent picks one of these or abstains; it does not
# emit a free-form number, so the action space is small enough to cache
# exhaustively and to reason about.
ALLOCATION_OPTIONS = (45, 40, 35)
ABSTAIN = "abstain"

_CEILING_PATTERN = re.compile(r"\$\s*(\d+)")


@dataclass(frozen=True)
class Decision:
    """One decision, from either the reference implementation or the model.

    Both produce this type, so telemetry, the wrapper's `Proposal` protocol and
    the comparison between them all operate on identical structure. `source`
    records which produced it, because "the model chose differently" and "the
    retrieved context changed" are very different explanations for a change in
    behaviour and must never be confused for one another.
    """

    action: str  # "allocate(45)" | "abstain"
    amount: int | None
    inferred_ceiling: int | None
    observed_sum: int
    rationale: str
    memory_ids: tuple[str, ...] = ()
    source: str = "reference"  # reference | model | cache

    @property
    def is_abstain(self) -> bool:
        return self.amount is None


def infer_ceiling(memories) -> tuple[int | None, str | None]:
    """Read an effective ceiling out of the highest-authority policy memory.

    Memories arrive already ranked by `MemoryStore.retrieve`, which has applied
    supersession and recency. So the first policy-kind memory is the current
    policy by construction -- this function does not re-litigate that ordering,
    it trusts it. If retrieval is wrong, this is wrong, which is the point of
    testing them together.
    """
    for m in memories:
        if m.kind != "policy":
            continue
        match = _CEILING_PATTERN.search(m.text)
        if match:
            return int(match.group(1)), m.memory_id
    return None, None


def propose(
    inferred_ceiling: int | None,
    observed_sum: int,
    hard_limit: int,
) -> Decision:
    """Choose the largest allocation that fits under both the ceiling and the limit.

    The effective ceiling can sit below the hard limit (a policy restriction) or
    above it (a stale or over-permissive policy). The hard limit always binds:
    a policy memory claiming a $120 ceiling on a $100 account does not authorize
    $120, and an agent that treated it as authorization would be wrong in a way
    the database would then have to catch.
    """
    binding = hard_limit if inferred_ceiling is None else min(inferred_ceiling, hard_limit)
    remaining = binding - observed_sum

    for amount in ALLOCATION_OPTIONS:
        if amount <= remaining:
            return Decision(
                action=f"allocate({amount})",
                amount=amount,
                inferred_ceiling=inferred_ceiling,
                observed_sum=observed_sum,
                rationale=(
                    f"observed {observed_sum} against binding ceiling {binding} "
                    f"(policy {inferred_ceiling}, hard limit {hard_limit}); "
                    f"{remaining} remaining, so {amount} fits"
                ),
            )

    return Decision(
        action=ABSTAIN,
        amount=None,
        inferred_ceiling=inferred_ceiling,
        observed_sum=observed_sum,
        rationale=(
            f"observed {observed_sum} against binding ceiling {binding} "
            f"(policy {inferred_ceiling}, hard limit {hard_limit}); "
            f"{remaining} remaining, smaller than the smallest option "
            f"{min(ALLOCATION_OPTIONS)}"
        ),
    )


RETRIEVAL_QUERY = (
    "What is the current authorization ceiling and spending policy for this account?"
)
