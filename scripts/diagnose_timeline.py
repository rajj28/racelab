"""When does each agent's retrieval actually execute, relative to the policy update?

Two explanations for the tight-window zero have now been ruled out. Arrival
timing does not explain it: at a 1000 ms window the update lands at 500 ms and
agents arriving at 50 ms should read the stale ceiling. Retrieval latency does
not explain it either: a warm retrieval costs about 86 ms, leaving roughly 30%
of even a 400 ms window able to hold stale memory.

So this stops inferring and records the timeline. For every retrieval it logs
the milliseconds elapsed since the run's threads were released and the ceiling
that retrieval returned, and it logs when the policy update was written. The
gap between "agent arrived" and "agent retrieved" includes opening a connection
to the cluster, which is a TLS handshake to CockroachDB Cloud and is not free.

RESULT: that gap was the whole problem. Before the fix, the earliest retrieval
at a 400 ms window landed at 530 ms while the policy update was already written
at 299 ms, and 0 of 20 retrievals were stale at every window swept -- so the
C-vs-C-ops ablation had been comparing two arms that both held fresh memory.
Opening the per-agent connections before releasing the threads, and giving the
updater its own connection instead of making it queue behind twenty agents for
six pooled ones, moved the first retrieval to 218 ms and produced 8-9 stale
retrievals of 20 at every window. This script is the regression check for that
condition: if a future change puts cost back in front of the first retrieval,
the stale count goes to zero here before any result looks wrong.

Run:  python scripts/diagnose_timeline.py
"""

from __future__ import annotations

import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from racelab import experiment as exp
from racelab.arms import ARMS, ArmId
from racelab.db import ConnectionPool
from racelab.embeddings import get_embedder
from racelab.experiment import RunConfig, run_once
from scenario.decide import infer_ceiling, propose
from scripts.run_sweep import build_scenario

WINDOWS = [400, 1000, 2500]


class Probe:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.t0: float | None = None
        self.retrievals: list[tuple[float, int]] = []
        self.update_at: float | None = None

    def reset(self) -> None:
        with self.lock:
            self.t0 = None
            self.retrievals = []
            self.update_at = None

    def ms(self) -> float:
        return (time.perf_counter() - (self.t0 or time.perf_counter())) * 1000.0


PROBE = Probe()
_real_connect = exp.connect
_RealStore = exp.MemoryStore


def connect_probe(*args, **kwargs):
    # Connections are now all opened before the threads are released, so the
    # last one to return is the closest available marker for "the run starts".
    # Anchoring on `arrival_offsets` instead would put t0 before ~8 seconds of
    # connection setup and make every timestamp meaningless.
    conn = _real_connect(*args, **kwargs)
    PROBE.t0 = time.perf_counter()
    return conn


class StoreProbe(_RealStore):  # type: ignore[misc,valid-type]
    def retrieve(self, *args, **kwargs):
        out = super().retrieve(*args, **kwargs)
        at = PROBE.ms()
        ceiling, _ = infer_ceiling(out)
        with PROBE.lock:
            PROBE.retrievals.append((at, ceiling))
        return out

    def add(self, *args, **kwargs):
        out = super().add(*args, **kwargs)
        with PROBE.lock:
            PROBE.update_at = PROBE.ms()
        return out


def main() -> int:
    exp.connect = connect_probe
    exp.MemoryStore = StoreProbe

    scenario = build_scenario()
    embedder = get_embedder("titan")
    pool = ConnectionPool("crdb", size=6)

    def reason(memories, observed, hard_limit):
        ceiling, _ = infer_ceiling(memories)
        return propose(ceiling, observed, hard_limit)

    print("Retrieval timeline, C-ops arm (stale memory by construction)")
    print("=" * 78)

    try:
        for window in WINDOWS:
            PROBE.reset()
            config = RunConfig(
                arm=ARMS[ArmId.C_OPS], scenario=scenario, seed=2000,
                agent_count=20, arrival_window_ms=window, reasoning_gap_ms=200.0,
            )
            out = run_once(config, embedder, reason, pool)

            with PROBE.lock:
                rets = sorted(PROBE.retrievals)
                upd = PROBE.update_at

            stale = [t for t, c in rets if c == 80]
            fresh = [t for t, c in rets if c == 60]
            planned = window * 0.5

            print(f"\nwindow {window} ms   arrivals spread over [0, {window})")
            print(f"  policy update planned at {planned:>7.0f} ms   "
                  f"actually written at {upd if upd is None else round(upd):>7} ms")
            print(f"  first retrieval at       {min(t for t, _ in rets):>7.0f} ms   "
                  f"last at {max(t for t, _ in rets):>7.0f} ms   n={len(rets)}")
            print(f"  retrievals returning $80 (stale): {len(stale):>3}"
                  + (f"  up to {max(stale):.0f} ms" if stale else ""))
            print(f"  retrievals returning $60 (fresh): {len(fresh):>3}"
                  + (f"  from {min(fresh):.0f} ms" if fresh else ""))
            print(f"  final sum {out.final_sum}   conflicts {out.conflicts}   "
                  f"committed {out.committed}   abstained {out.abstained}")
    finally:
        pool.close()
        exp.connect = _real_connect
        exp.MemoryStore = _RealStore

    print()
    print("=" * 78)
    print("Compare the first-retrieval time against the update time. If the")
    print("earliest retrieval already lands after the update, no agent in this")
    print("arm ever held stale memory, and the arm cannot show its cost.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
