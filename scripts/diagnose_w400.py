"""Why is the memory-refresh effect zero at 400 ms when the readings are in band?

The sweep's boundary check passed, but it only tested the direction the
prediction was stated in: readings outside `[20, 45]` must produce no effect.
At the 400 ms window, 126 of 129 re-decision readings were INSIDE the band and
the effect was still exactly zero. That is not a contradiction of the
prediction as written -- being in the band is necessary, not sufficient -- but
the registered *explanation* for the tight-window zero was "readings fall below
the band", and that explanation is wrong. The readings are at 45.

So something else is suppressing the effect. This records, per decision, the
ceiling the agent actually inferred, which is the thing the ablation is
supposed to vary. The hypothesis being tested here:

    at a tight arrival window the out-of-band policy update lands early
    relative to most agents' arrival, so agents in the "stale memory" arm
    retrieve the ALREADY-UPDATED $60 ceiling on their single retrieval --
    and an arm that never held stale memory cannot demonstrate the cost of
    stale memory.

If that is what is happening, C-ops is not stale at 400 ms, and the zero is an
artefact of the scenario's timing rather than of the boundary.

RESULT: the first half was confirmed and the explanation was wrong. C-ops
inferred the fresh $60 ceiling on 100% of decisions at both 400 ms *and*
1000 ms, so it was indeed never stale -- but arrival timing does not explain
that, because at a 1000 ms window the update lands at 500 ms and an agent
arriving at 50 ms should still read $80. The cause was a ~391 ms per-agent TLS
handshake sitting in front of the first retrieval. See `diagnose_latency.py`,
then `diagnose_timeline.py`, then METHODOLOGY entry 10.

Kept in the repo because the chain of three eliminations is the evidence that
the final explanation is the right one, and a reader who sees only the
conclusion has to take it on trust.

Run:  python scripts/diagnose_w400.py
"""

from __future__ import annotations

import collections
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from racelab.arms import ARMS, ArmId
from racelab.db import ConnectionPool
from racelab.embeddings import get_embedder
from racelab.experiment import RunConfig, run_once
from scenario.decide import infer_ceiling, propose
from scripts.run_sweep import build_scenario

WINDOWS = [400, 1000, 2500]
REPEATS = 4


def main() -> int:
    scenario = build_scenario()
    embedder = get_embedder("titan")
    pool = ConnectionPool("crdb", size=6)

    print("Ceilings actually inferred, by arm and arrival window")
    print("=" * 78)
    print("The C-ops arm is supposed to reason over STALE memory ($80). If it is")
    print("inferring $60, it is not stale, and the ablation has nothing to show.\n")

    try:
        for window in WINDOWS:
            for arm_id in (ArmId.C_OPS, ArmId.C):
                seen: collections.Counter = collections.Counter()
                first: collections.Counter = collections.Counter()

                def reason(memories, observed, hard_limit):
                    ceiling, _ = infer_ceiling(memories)
                    seen[ceiling] += 1
                    return propose(ceiling, observed, hard_limit)

                sums = []
                for r in range(REPEATS):
                    config = RunConfig(
                        arm=ARMS[arm_id], scenario=scenario, seed=2000 + r,
                        agent_count=20, arrival_window_ms=window,
                        reasoning_gap_ms=200.0,
                    )
                    out = run_once(config, embedder, reason, pool)
                    sums.append(out.final_sum)

                total = sum(seen.values()) or 1
                stale = seen.get(80, 0)
                fresh = seen.get(60, 0)
                print(f"  w={window:>5} {ARMS[arm_id].label:44} "
                      f"$80 (stale) {stale:>4} ({100*stale/total:>3.0f}%)  "
                      f"$60 (fresh) {fresh:>4} ({100*fresh/total:>3.0f}%)  "
                      f"sums={sums}")
            print()
    finally:
        pool.close()

    print("=" * 78)
    print("If the stale share is near zero at 400 ms and substantial at 1000 ms,")
    print("the tight-window zero is a timing artefact of when the policy update")
    print("lands, not evidence about the boundary.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
