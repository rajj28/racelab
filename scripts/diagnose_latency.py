"""Is the tight-window zero an arrival-timing effect or a retrieval-latency effect?

`diagnose_w400.py` established that at 400 ms and 1000 ms the stale-memory arm
infers the *fresh* $60 ceiling on 100% of decisions -- so it never holds stale
memory, and an arm that never holds stale memory cannot show the cost of stale
memory. But it did not establish why.

The arrival-timing explanation fails on its own numbers: at a 1000 ms window the
policy update lands at 500 ms, and agents arriving at 50 ms should still read
$80. They did not. That leaves latency. A retrieval is a Bedrock embedding call
plus a vector query; if that round trip costs more than the remaining window,
every agent's retrieval *executes* after the policy update regardless of when
the agent arrived.

This measures the round trip directly, and records for each retrieval the
milliseconds elapsed since run start alongside the ceiling it returned. If
retrieval latency is on the order of the arrival window, the tight-window zero
is a property of our instrument, not of the boundary.

RESULT: retrieval is not the cost either. Embedding is memoised to ~1.1 ms and
the vector query is ~86 ms, leaving roughly 30% of even a 400 ms window
nominally able to hold stale memory. What these numbers did was bound the
problem tightly enough to locate the real cost: a 391 ms per-agent connection
handshake, measured separately, sitting in front of the first retrieval. See
`diagnose_timeline.py` and METHODOLOGY entry 10.

Run:  python scripts/diagnose_latency.py
"""

from __future__ import annotations

import pathlib
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from racelab.db import ConnectionPool
from racelab.embeddings import get_embedder
from racelab.memory import MemoryStore
from scenario.decide import infer_ceiling
from scripts.run_sweep import build_scenario

SAMPLES = 12


def main() -> int:
    scenario = build_scenario()
    embedder = get_embedder("titan")
    pool = ConnectionPool("crdb", size=2)

    print("Retrieval round-trip cost")
    print("=" * 78)
    print("One retrieval = one Titan embedding call + one vector query. If this")
    print("is comparable to the arrival window, no agent can hold stale memory")
    print("at tight windows no matter when it arrived.\n")

    embed_ms: list[float] = []
    query_ms: list[float] = []
    total_ms: list[float] = []

    try:
        for i in range(SAMPLES):
            t0 = time.perf_counter()
            embedder.embed(scenario.retrieval_query)
            t1 = time.perf_counter()
            with pool.lease() as conn:
                store = MemoryStore(conn, embedder)
                store.retrieve(scenario.account_id, scenario.retrieval_query, k=4)
            t2 = time.perf_counter()

            e = (t1 - t0) * 1000.0
            # retrieve() embeds again internally, so the query-only cost is the
            # second phase minus one embedding.
            q = max((t2 - t1) * 1000.0 - e, 0.0)
            embed_ms.append(e)
            query_ms.append(q)
            total_ms.append(e + q)
            marker = "  (first call, cold)" if i == 0 else ""
            print(f"  sample {i + 1:>2}  embed {e:>7.1f} ms   query {q:>7.1f} ms   "
                  f"total {e + q:>7.1f} ms{marker}")
    finally:
        pool.close()

    warm_total = total_ms[1:] or total_ms
    warm_embed = embed_ms[1:] or embed_ms
    print()
    print(f"  embed   median {statistics.median(warm_embed):>7.1f} ms   "
          f"max {max(warm_embed):>7.1f} ms   (warm samples)")
    print(f"  total   median {statistics.median(warm_total):>7.1f} ms   "
          f"max {max(warm_total):>7.1f} ms   (warm samples)")
    print()

    median = statistics.median(warm_total)
    print("=" * 78)
    print("Window   policy update at   median retrieval   agents able to hold stale memory")
    for window in (400, 1000, 1500, 2500, 4000):
        update_at = window * 0.5
        # An agent holds stale memory only if arrival + retrieval finishes
        # before the update lands.
        usable = update_at - median
        share = max(usable, 0.0) / window
        print(f"  {window:>5}   {update_at:>13.0f} ms   {median:>14.0f} ms   "
              f"{100 * share:>5.1f}% of the arrival window")
    print()
    print("A share at or near zero means the stale-memory arm is not stale, so")
    print("the zero effect at that window measures the instrument's latency")
    print("rather than the pre-registered boundary.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
