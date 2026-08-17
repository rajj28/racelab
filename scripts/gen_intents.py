"""Stage 1: generate the model's decisions once, so stage 2 can replay them.

The concurrency experiment must not make live model calls inside the race
(METHODOLOGY entry 2): it would add generation variance to a measurement about
database and protocol behaviour, and make the run unreproducible for anyone
without the same Bedrock access. So the model is asked every question it could
be asked, once, and the answers are written to disk.

## The two corpus states

The scenario's whole point is that the retrieved context changes mid-run, so the
model is asked each question twice -- once against each corpus state:

    stale   the $60 policy does not exist yet; retrieval returns the $80 ceiling
    fresh   the $60 policy has been written and supersedes it

At replay time the state is not assumed, it is *derived from what the agent
actually retrieved*: if the superseding memory is among the retrieved ids the
lookup is `fresh`, otherwise `stale`. So an agent whose refresh genuinely failed
to surface the new policy gets the stale decision, which is the honest answer.

## Why every reachable reading, not the observed ones

`reachable_sums` enumerates every multiple of five from 0 to past the hard
limit. A conflict-aware agent that re-reads an unusual sum must find a cached
decision, or the experiment would fail at exactly the moment the hypothesis is
being tested. A lookup miss is treated as a bug, never as a reason to fall back.

Run:  python scripts/gen_intents.py                  # model, both states
      python scripts/gen_intents.py --provider reference
      python scripts/gen_intents.py --dry-run        # show the plan, call nothing
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from racelab.db import connect
from racelab.embeddings import get_embedder
from racelab.memory import MemoryStore
from scenario.corpus import HERO
from scenario.decide import RETRIEVAL_QUERY
from scenario.intents import IntentCache, generate, reachable_sums, reference_provider

UPDATE = HERO.update[0]


def _retrieve(conn, embedder, k: int = 4):
    return MemoryStore(conn, embedder).retrieve(HERO.account_id, RETRIEVAL_QUERY, k=k)


def _set_corpus_state(conn, embedder, state: str) -> None:
    """Put the corpus into `stale` or `fresh`, then leave it that way.

    This mutates the shared corpus, so it is restored to `fresh` at the end of
    the run -- the swept experiment resets it per run anyway, but leaving the
    database in a state that depends on whether generation happened to be
    interrupted would be a trap.
    """
    if state == "stale":
        conn.execute("DELETE FROM memories WHERE memory_id = %s", (UPDATE.memory_id,))
    else:
        MemoryStore(conn, embedder).add(
            memory_id=UPDATE.memory_id,
            account_id=HERO.account_id,
            text=UPDATE.text,
            kind=UPDATE.kind,
            supersedes=UPDATE.supersedes,
            created_at=datetime.datetime.now(datetime.timezone.utc),
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", default="model", choices=["model", "reference"])
    ap.add_argument("--name", default=None,
                    help="cache filename stem; defaults to the provider name")
    ap.add_argument("--regenerate", action="store_true",
                    help="re-ask questions already in the cache")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    name = args.name or args.provider
    cache = IntentCache(name)
    sums = reachable_sums(HERO.hard_limit)

    print(f"Stage 1 - {args.provider} -> {cache.path.as_posix()}")
    print(f"  {len(sums)} reachable readings x 2 corpus states = "
          f"{2 * len(sums)} decisions")
    print(f"  already cached: {len(cache)}")

    if args.dry_run:
        print("  dry run, nothing called")
        return 0

    if args.provider == "model":
        from scenario.agent import ClaudeAgent
        agent = ClaudeAgent()
        print(f"  model: {agent.model_id} in {agent.region}")
        provider = agent.decide
    else:
        provider = reference_provider

    # Provenance is set once, here, from what actually produced the entries.
    cache.meta["provider"] = name
    cache.meta["kind"] = args.provider
    cache.meta["generated_at"] = datetime.datetime.now(
        datetime.timezone.utc).isoformat()
    if args.provider == "model":
        cache.meta["model_id"] = agent.model_id
        cache.meta["temperature"] = agent.temperature

    embedder = get_embedder("titan")
    started = time.time()
    total_made = 0

    try:
        with connect("crdb") as conn:
            for state in ("stale", "fresh"):
                _set_corpus_state(conn, embedder, state)
                memories = _retrieve(conn, embedder)
                ids = [m.memory_id for m in memories]
                has_update = UPDATE.memory_id in ids
                print(f"\n  corpus state {state!r}: retrieved {ids}")
                print(f"    superseding memory present in retrieval: {has_update}")

                # If the state does not actually differ in the retrieved context,
                # the two halves of the cache would be identical and the ablation
                # would have nothing to replay.
                if state == "fresh" and not has_update:
                    print("    ERROR: the fresh state did not surface the "
                          "superseding memory, so the two cache halves would be "
                          "identical. Not generating.", file=sys.stderr)
                    return 1
                if state == "stale" and has_update:
                    print("    ERROR: the stale state still contains the "
                          "superseding memory.", file=sys.stderr)
                    return 1

                def progress(i: int, n: int) -> None:
                    if i % 5 == 0 or i == n:
                        print(f"    {i}/{n}", flush=True)

                made = generate(
                    cache,
                    provider=provider,
                    account_id=HERO.account_id,
                    corpus_state=state,
                    memories=memories,
                    hard_limit=HERO.hard_limit,
                    on_progress=progress,
                    skip_existing=not args.regenerate,
                )
                total_made += made
                print(f"    generated {made}, cache now {len(cache)}")
            # Leave the corpus in the state the experiment expects to reset from.
            _set_corpus_state(conn, embedder, "fresh")
    finally:
        cache.save()

    elapsed = time.time() - started
    print(f"\nwrote {cache.path.as_posix()}  "
          f"({len(cache)} entries, {total_made} new, {elapsed:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
