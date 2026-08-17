"""Two-stage execution: generate decisions once, replay them during the race.

Stage 1 calls the model once per `(account, corpus state, observed sum)` and
writes the result to disk. Stage 2 -- the 100-run concurrency experiment --
looks decisions up instead of calling the model. The rationale is in
`docs/METHODOLOGY.md`, entry 2: the experiment measures database and protocol
behaviour, and 2000+ live model calls inside it would add a large uncontrolled
source of variance that has nothing to do with the hypothesis, while making the
run unreproducible for anyone without the same Bedrock access.

## The keying, and why it is exhaustive rather than sampled

The action space is bounded and every option is a multiple of 5, so the set of
operational readings an agent can encounter is finite and small: every multiple
of 5 from 0 to just past the hard limit. Stage 1 enumerates all of them.

This matters more than it looks. If intents were generated only for the readings
observed in a pilot run, then a conflict-aware agent that re-read an unusual sum
would find no cached decision, and the experiment would have to either fail or
fall back to something else at exactly the moment the hypothesis is being
tested. Enumerating the whole reachable space means a re-read can never miss.

## What is deliberately not cached

The retrieval step is not cached. Memory refresh happens live against
CockroachDB during the race, because *that is the mechanism under test* in the
C arm -- caching it would remove the thing the ablation is meant to isolate.
Only the model's choice given a context is cached.

## Provenance is recorded and checked

Every cache file records which provider produced it. A cache built from the
deterministic reference implementation cannot be silently reported as a model
result: `require_provider` raises. Mixing them would produce a number whose
meaning nobody could reconstruct afterwards.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
from typing import Callable, Iterable

from .decide import ALLOCATION_OPTIONS, Decision, infer_ceiling, propose

CACHE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "results" / "intents"
STEP = 5  # every allocation option is a multiple of 5


def reachable_sums(hard_limit: int) -> list[int]:
    """Every operational reading an agent can encounter, exhaustively.

    Runs past the hard limit deliberately: under the naive policy the total can
    and does exceed it, and a conflict-aware agent re-reading afterwards still
    needs a decision -- which will be `abstain`, but it has to be a real cached
    decision rather than a lookup miss.
    """
    ceiling = hard_limit + max(ALLOCATION_OPTIONS)
    return list(range(0, ceiling + STEP, STEP))


def _key(account_id: str, corpus_state: str, observed_sum: int) -> str:
    return f"{account_id}|{corpus_state}|{observed_sum}"


class IntentCache:
    """Disk-backed decisions, keyed by what the agent could observe."""

    def __init__(self, provider: str, path: pathlib.Path | None = None):
        self.provider = provider
        self.path = path or (CACHE_ROOT / f"{provider}.json")
        self.entries: dict[str, dict] = {}
        self.meta: dict = {"provider": provider}
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.meta = raw.get("meta", self.meta)
            self.entries = raw.get("entries", {})

    # -- persistence ------------------------------------------------------

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"meta": self.meta, "entries": self.entries}, indent=2),
            encoding="utf-8",
        )

    def __len__(self) -> int:
        return len(self.entries)

    # -- reading ----------------------------------------------------------

    def lookup(self, account_id: str, corpus_state: str, observed_sum: int) -> Decision:
        key = _key(account_id, corpus_state, observed_sum)
        try:
            payload = self.entries[key]
        except KeyError:
            raise KeyError(
                f"no cached intent for {key}. Stage 1 enumerates every reachable "
                f"reading, so a miss means the cache was generated for a different "
                f"hard limit or corpus state -- not that this reading is unusual. "
                f"Regenerate rather than falling back."
            ) from None
        return Decision(
            action=payload["action"],
            amount=payload["amount"],
            inferred_ceiling=payload["inferred_ceiling"],
            observed_sum=observed_sum,
            rationale=payload["rationale"],
            memory_ids=tuple(payload.get("memory_ids", ())),
            source="cache",
        )

    def require_provider(self, expected: str) -> None:
        """Refuse to let a reference-built cache be reported as a model result."""
        actual = self.meta.get("provider")
        if actual != expected:
            raise RuntimeError(
                f"intent cache at {self.path} was generated by {actual!r}, but this "
                f"run expects {expected!r}. Results from a deterministic reference "
                f"implementation and results from a language model are not "
                f"interchangeable and must not be reported as one another."
            )

    # -- writing ----------------------------------------------------------

    def put(self, account_id: str, corpus_state: str, decision: Decision) -> None:
        payload = dataclasses.asdict(decision)
        payload["memory_ids"] = list(decision.memory_ids)
        payload.pop("observed_sum", None)  # it is part of the key
        payload.pop("source", None)  # provenance lives in meta, per file
        self.entries[_key(account_id, corpus_state, decision.observed_sum)] = payload


# --------------------------------------------------------------------------
# Stage 1
# --------------------------------------------------------------------------

# Given retrieved memories and a reading, return a decision.
DecisionProvider = Callable[..., Decision]


def reference_provider(*, account_id, memories, observed_sum, hard_limit) -> Decision:
    """The deterministic reference. Useful, and clearly labelled as not a model."""
    ceiling, source_id = infer_ceiling(memories)
    base = propose(ceiling, observed_sum, hard_limit)
    return dataclasses.replace(
        base,
        memory_ids=tuple(m.memory_id for m in memories),
        source="reference",
    )


def generate(
    cache: IntentCache,
    *,
    provider: DecisionProvider,
    account_id: str,
    corpus_state: str,
    memories: Iterable,
    hard_limit: int,
    on_progress: Callable[[int, int], None] | None = None,
    skip_existing: bool = True,
) -> int:
    """Fill the cache for one account in one corpus state. Returns calls made."""
    memories = list(memories)
    sums = reachable_sums(hard_limit)
    made = 0
    for i, observed in enumerate(sums):
        if skip_existing and _key(account_id, corpus_state, observed) in cache.entries:
            continue
        decision = provider(
            account_id=account_id,
            memories=memories,
            observed_sum=observed,
            hard_limit=hard_limit,
        )
        cache.put(account_id, corpus_state, decision)
        made += 1
        if on_progress:
            on_progress(i + 1, len(sums))
    cache.meta.setdefault("accounts", {})[f"{account_id}|{corpus_state}"] = {
        "hard_limit": hard_limit,
        "memory_ids": [m.memory_id for m in memories],
        "readings": len(sums),
    }
    return made
