"""Capture one representative run per arm, with per-agent detail, for the UI.

The swept experiment reports aggregates. To inspect *why* an arm ended where it
did you need the moments the aggregate discards: when each agent arrived, what
sum it observed on each attempt, which memories it had retrieved at the time,
what ceiling it inferred, what it decided before and after a conflict, and when
the policy update landed relative to all of that.

`run_once` grew four optional observer hooks for this. They are instrumentation
only -- no arm, window, metric or run depends on them, and a failing observer is
swallowed rather than allowed to fail a run.

## One run per arm, and why that is honest

This captures a single run per arm, not an average, because a timeline of an
average agent is not a thing that happened. Single runs are cherry-pickable, so
two things are fixed here rather than chosen after looking:

  * the seed and window are constants in this file, not flags
  * the aggregate result for the same cell is embedded alongside, so the UI can
    state how typical the captured run is

The window is 400 ms because that is where the sweep separates the arms most
cleanly on both invariants: C-ops breached the policy ceiling in 10 of 10 runs
and C in 0 of 10, so neither arm's captured run is a lucky draw.

Run:  python scripts/capture_ui.py
      python scripts/capture_ui.py --out docs/ui_data.json
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from racelab.arms import ARMS, ORDER
from racelab.db import ConnectionPool, connect
from racelab.embeddings import get_embedder
from racelab.experiment import RunConfig, run_once
from scenario.corpus import HERO
from scenario.decide import RETRIEVAL_QUERY
from scripts.run_sweep import build_scenario, reference_reason

SEED = 1000
WINDOW_MS = 400
AGENTS = 20
GAP_MS = 200.0

# The cell-level aggregates for this exact configuration, from the corrected
# sweep (results/sweep_fixed.json). Embedded so the UI can say how
# representative the captured run is instead of implying it is the whole result.
SWEEP_CONTEXT = {
    "A": {"hard_limit_violations": 9, "policy_breaches": 10, "mean_final_sum": 196.0},
    "B": {"hard_limit_violations": 10, "policy_breaches": 10, "mean_final_sum": 229.5},
    "C-ops": {"hard_limit_violations": 0, "policy_breaches": 10, "mean_final_sum": 80.0},
    "C": {"hard_limit_violations": 0, "policy_breaches": 0, "mean_final_sum": 45.0},
}


class Capture:
    """Collects one run's timeline. Hooks are called under the run's lock."""

    def __init__(self) -> None:
        self.t0: float | None = None
        self.offsets: list[float] = []
        self.decisions: list[dict] = []
        self.results: dict[str, dict] = {}
        self.policy_update_ms: float | None = None
        self.run_id: str | None = None

    def _ms(self, at: float) -> float:
        return round((at - self.t0) * 1000.0, 1) if self.t0 else 0.0

    # -- hooks ------------------------------------------------------------

    def on_release(self, *, at, offsets, run_id, arm_id, scenario) -> None:
        self.t0 = at
        self.offsets = [round(o * 1000.0, 1) for o in offsets]
        self.run_id = run_id

    def on_policy_update(self, *, at) -> None:
        self.policy_update_ms = self._ms(at)

    def on_decision(self, *, agent_id, arrival_offset, ctx, decision, at) -> None:
        self.decisions.append({
            "agent_id": agent_id,
            "attempt_no": ctx.attempt_no,
            "at_ms": self._ms(at),
            "observed_sum": ctx.observed,
            "action": decision.action,
            "amount": decision.amount,
            "inferred_ceiling": decision.inferred_ceiling,
            "rationale": decision.rationale,
            "memory_ids": list(decision.memory_ids),
            # The memory texts as retrieved at this moment. This is the point of
            # the whole UI: an agent's ceiling is not in the database, it is in
            # these four strings, and they change mid-run.
            "memories": [
                {"memory_id": m.memory_id, "kind": m.kind, "text": m.text}
                for m in (ctx.memory or [])
            ],
        })

    def on_result(self, *, agent_id, arrival_offset, result, at) -> None:
        self.results[agent_id] = {
            "arrival_ms": round(arrival_offset * 1000.0, 1),
            "finished_ms": self._ms(at),
            "outcome": result.outcome,
            "action": result.action,
            "decision_before": result.decision_before,
            "decision_after": result.decision_after,
            "revised": result.revised,
            "conflicts": result.conflicts,
            "reason_calls": result.reason_calls,
            "attempts_made": result.attempts_made,
            "memory_refreshes": result.memory_refreshes,
        }

    # -- assembly ---------------------------------------------------------

    def agents(self) -> list[dict]:
        by_agent: dict[str, list[dict]] = {}
        for d in self.decisions:
            by_agent.setdefault(d["agent_id"], []).append(d)
        out = []
        for agent_id in sorted(set(list(by_agent) + list(self.results))):
            res = dict(self.results.get(agent_id, {}))
            steps = sorted(by_agent.get(agent_id, []), key=lambda d: d["attempt_no"])
            res["agent_id"] = agent_id
            res["steps"] = steps
            if "arrival_ms" not in res and steps:
                res["arrival_ms"] = steps[0]["at_ms"]
            out.append(res)
        return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="docs/ui_data.json")
    args = ap.parse_args()

    scenario = build_scenario()
    embedder = get_embedder("titan")
    pool = ConnectionPool("crdb", size=6)

    # The corpus as the agents will find it, so the UI can show every memory that
    # exists -- including the one that does not exist yet when the run starts.
    with connect("crdb") as conn:
        rows = conn.execute(
            "SELECT memory_id, kind, text, created_at FROM memories "
            "WHERE account_id = %s ORDER BY created_at", (HERO.account_id,)
        ).fetchall()
    corpus = [{"memory_id": r[0], "kind": r[1], "text": r[2],
               "created_at": r[3].isoformat()} for r in rows]

    payload = {
        "meta": {
            "seed": SEED, "window_ms": WINDOW_MS, "agents": AGENTS,
            "gap_ms": GAP_MS,
            "hard_limit": scenario.hard_limit,
            "stale_ceiling": scenario.stale_ceiling,
            "current_ceiling": scenario.current_ceiling,
            "retrieval_query": RETRIEVAL_QUERY,
            "update_memory_id": scenario.update_memory.memory_id,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "provider": "reference",
        },
        "corpus": corpus,
        "arms": [],
    }

    try:
        for arm_id in ORDER:
            arm = ARMS[arm_id]
            cap = Capture()
            config = RunConfig(
                arm=arm, scenario=scenario, seed=SEED, agent_count=AGENTS,
                arrival_window_ms=WINDOW_MS, reasoning_gap_ms=GAP_MS,
            )
            outcome = run_once(config, embedder, reference_reason, pool, observer=cap)

            agents = cap.agents()
            payload["arms"].append({
                "id": arm.id.value,
                "label": arm.label,
                "backend": arm.backend,
                "isolation": arm.isolation,
                "re_reason": arm.re_reason,
                "refresh_memory": arm.refresh_memory,
                "run_id": outcome.run_id,
                "final_sum": outcome.final_sum,
                "violated_hard_limit": outcome.violated_hard_limit,
                "breached_policy": outcome.breached_policy,
                "committed": outcome.committed,
                "abstained": outcome.abstained,
                "exhausted": outcome.exhausted,
                "conflicts": outcome.conflicts,
                "revisions": outcome.revisions,
                "reason_calls": outcome.reason_calls,
                "memory_refreshes": outcome.memory_refreshes,
                # Surfaced rather than dropped: an agent that vanished without a
                # result is a fact about the run, and a UI that silently showed
                # 18 rows where 20 agents ran would be hiding it.
                "errors": outcome.errors,
                "void_reason": outcome.void_reason,
                "voided": outcome.voided,
                "policy_update_ms": cap.policy_update_ms,
                "agents": agents,
                "sweep": SWEEP_CONTEXT.get(arm.id.value),
            })
            print(f"  {arm.label:44} sum {outcome.final_sum:>4}  "
                  f"limit {'VIOLATED' if outcome.violated_hard_limit else 'held':>8}  "
                  f"policy {'BREACHED' if outcome.breached_policy else 'held':>8}  "
                  f"conflicts {outcome.conflicts:>3}  agents {len(agents)}/{AGENTS}"
                  + (f"  errors {outcome.errors}" if outcome.errors else "")
                  + (f"  [{outcome.void_reason}]" if outcome.void_reason else ""),
                  flush=True)
    finally:
        pool.close()

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    kb = out.stat().st_size / 1024
    print(f"\nwrote {out.as_posix()}  ({kb:.0f} KB, {len(payload['arms'])} arms)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
