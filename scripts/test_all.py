"""Run the whole test suite without `make`.

`make` is not present on a stock Windows install, and this project is developed
and demonstrated on Windows. A judge who follows a README that opens with
`make test` and gets `command not found` has hit an avoidable wall, so the same
suite is runnable with the interpreter that is guaranteed to be there.

The order is deliberate. `verify_clean_clone` runs first and against its own
throwaway database, so it is the one check that still means something when
seeding is broken or Bedrock is unavailable.

    python scripts/test_all.py
    python scripts/test_all.py --skip-bedrock   # schema and wrapper only
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[1]

SUITE = [
    ("schema", "verify_clean_clone.py",
     "a clean clone gets a vector index the optimizer actually uses", False),
    ("wrapper", "test_wrapper.py",
     "the arms differ by exactly one flag, and the collapse guard fires", False),
    ("memory", "test_memory_causality.py",
     "retrieval is index-backed, causal, and prefers current policy", True),
    ("arms", "test_arms.py",
     "four arms, and the ablation decomposed", True),
    # Needs a live cluster but no Bedrock: the reasoning step is a
    # RunnableLambda, so no embeddings or model calls are involved.
    ("guardrail", "test_guardrail.py",
     "a constraint checked before commit is enforced, not merely observed", False),
    ("action-space", "test_action_space.py",
     "the finding survives random action spaces, so $80.00 was one instance", True),
    ("seats", "test_scenario_seats.py",
     "a second scenario: counts, and a categorical correction", True),
    ("langchain", "test_langchain.py",
     "the LangChain tool re-decides instead of replaying", False),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="run the RaceLab test suite")
    ap.add_argument("--skip-bedrock", action="store_true",
                    help="skip tests that need Bedrock embeddings")
    ap.add_argument("--only", help="run one suite by name")
    args = ap.parse_args()

    selected = [s for s in SUITE if not (args.skip_bedrock and s[3])]
    if args.only:
        selected = [s for s in selected if s[0] == args.only]
        if not selected:
            print(f"no suite named {args.only!r}; expected one of "
                  f"{[s[0] for s in SUITE]}")
            return 2

    results: list[tuple[str, bool, float]] = []
    for name, script, description, _ in selected:
        print(f"\n{'=' * 78}\n{name}: {description}\n{'=' * 78}")
        started = time.time()
        proc = subprocess.run(
            [sys.executable, str(REPO / "scripts" / script)], cwd=REPO
        )
        elapsed = time.time() - started
        results.append((name, proc.returncode == 0, elapsed))

    print(f"\n{'=' * 78}\nSummary\n{'=' * 78}")
    for name, ok, elapsed in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:10} {elapsed:6.1f}s")

    failed = [name for name, ok, _ in results if not ok]
    if failed:
        print(f"\n{len(failed)} suite(s) failed: {', '.join(failed)}")
        return 1
    print(f"\nAll {len(results)} suites passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
