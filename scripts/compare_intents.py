"""Compare the model's cached decisions against the deterministic reference.

The model arm is a spot check on a protocol, not a claim that a language model
is good at subtraction (METHODOLOGY entry 9). But before replaying cached model
decisions through the concurrency experiment it is worth knowing, per reading,
where the model and the reference agree and where they do not -- because a
divergence in the final sums could otherwise be attributed to the protocol when
it actually came from the reasoner.

Reported per corpus state:

  agreement          same action as the reference
  ceiling breach     the model's own inferred ceiling would be exceeded by the
                     action it chose. NOT corrected anywhere -- policy breaches
                     are the dependent variable of this experiment
  repairs            times the model answered outside the action space and had
                     to be re-asked. The tool schema's enum does not enforce it

Run:  python scripts/compare_intents.py
      python scripts/compare_intents.py --name model
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scenario.corpus import HERO
from scenario.decide import propose
from scenario.intents import IntentCache, reachable_sums

CEILINGS = {"stale": 80, "fresh": 60}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="model")
    args = ap.parse_args()

    cache = IntentCache(args.name)
    if not len(cache):
        print(f"no cache at {cache.path}", file=sys.stderr)
        return 1

    print(f"Model vs reference, per reading   ({cache.path.name})")
    print(f"  provider {cache.meta.get('provider')!r} "
          f"kind {cache.meta.get('kind')!r} model {cache.meta.get('model_id')}")
    print("=" * 92)

    grand = {"n": 0, "agree": 0, "breach": 0, "repairs": 0, "repaired_cells": 0}

    for state, ceiling in CEILINGS.items():
        print(f"\ncorpus state {state!r}   ceiling ${ceiling}, hard limit ${HERO.hard_limit}")
        print(f"  {'obs':>4} {'ref':>13} {'model':>13} {'ceil':>5} {'rep':>4}  note")
        agree = breach = repairs = repaired = 0
        rows = 0

        for observed in reachable_sums(HERO.hard_limit):
            try:
                got = cache.lookup(HERO.account_id, state, observed)
            except KeyError:
                continue
            rows += 1
            ref = propose(ceiling, observed, HERO.hard_limit)
            same = ref.action == got.action
            agree += same
            repairs += got.repairs
            repaired += 1 if got.repairs else 0

            notes = []
            if got.amount is not None:
                total = observed + got.amount
                # Judged against the ceiling the MODEL said it inferred, so this
                # is an internal inconsistency rather than us grading it against
                # our own answer.
                own = got.inferred_ceiling
                if own is not None and total > own:
                    breach += 1
                    notes.append(f"breaches its own ${own} -> ${total}")
                if total > HERO.hard_limit:
                    notes.append(f"exceeds hard limit -> ${total}")
            if got.rejected_actions:
                notes.append("rejected " + ",".join(got.rejected_actions))

            if not same or notes:
                print(f"  {observed:>4} {ref.action:>13} {got.action:>13} "
                      f"{str(got.inferred_ceiling):>5} {got.repairs:>4}  "
                      + "; ".join(notes))

        print(f"  -- {rows} readings: agreement {agree}/{rows}, "
              f"self-inconsistent choices {breach}, "
              f"repairs {repairs} across {repaired} readings")
        grand["n"] += rows
        grand["agree"] += agree
        grand["breach"] += breach
        grand["repairs"] += repairs
        grand["repaired_cells"] += repaired

    n = grand["n"] or 1
    print("\n" + "=" * 92)
    print(f"overall agreement with the reference : {grand['agree']}/{grand['n']} "
          f"({100 * grand['agree'] / n:.0f}%)")
    print(f"choices breaching the model's own inferred ceiling : {grand['breach']}")
    print(f"readings needing a repair : {grand['repaired_cells']} "
          f"({grand['repairs']} re-asks total)")
    print()
    print("Only rows that disagree or carry a note are printed; the rest matched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
