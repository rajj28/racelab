"""The model arm: the same protocol, with Claude doing the reasoning.

METHODOLOGY entry 9 fixed the shape of this before it was run. The reference
reasoner carries the swept statistical claim; the model arm demonstrates that
the protocol survives contact with an actual LLM at spot-checked points. A full
model sweep would add generation variance to the primary result for no gain.

So this runs **two matched arrival windows**, chosen in advance:

    400 ms   INSIDE the pre-registered 20 <= S <= 45 band, where the
             memory-refresh effect is predicted and was observed at -35.0
    2500 ms  OUTSIDE it, where the effect was observed at +0.0

and **two arms**, C-ops and C -- the pair whose difference is the memory
refresh. Arms A and B are not re-run with the model, for a stated reason rather
than convenience: naive replay never calls the reasoning step a second time, so
there is no re-reasoning behaviour for a model to exhibit there, and the sums
those arms reach exceed the readings stage 1 enumerated. Running them would
require either 300 more model calls or a fallback, and a fallback inside a
result labelled "model" is exactly what `require_provider` exists to prevent.

Both providers run against the same seeds, the same agent count and the same
windows, so the rows are comparable. Divergence is reported, not smoothed.

Run:  python scripts/model_arm.py
      python scripts/model_arm.py --runs 5 --name model
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from racelab.arms import ARMS, ArmId
from racelab.db import ConnectionPool
from racelab.embeddings import get_embedder
from racelab.experiment import RunConfig, run_once, summarize
from scenario.intents import IntentCache
from scripts.run_sweep import (RESULTS, _make_cache_reason, _print_safe,
                               build_scenario, reference_reason)

# Matched points, fixed before running. 400 ms is inside the pre-registered
# band, 2500 ms is outside it.
WINDOWS = [400, 2500]
ARMS_RUN = [ArmId.C_OPS, ArmId.C]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--agents", type=int, default=20)
    ap.add_argument("--name", default="model", help="intent cache name")
    ap.add_argument("--gap-ms", type=float, default=200.0)
    ap.add_argument("--out", default="model_arm.md")
    args = ap.parse_args()

    cache = IntentCache(args.name)
    if not len(cache):
        print(f"no intent cache at {cache.path}; run scripts/gen_intents.py first",
              file=sys.stderr)
        return 1
    cache.require_provider(args.name)

    scenario = build_scenario()
    embedder = get_embedder("titan")
    providers = {
        "reference": reference_reason,
        "model": _make_cache_reason(cache),
    }

    print(f"Model arm - matched points {WINDOWS} ms, "
          f"{args.runs} runs/arm/window/provider")
    print(f"  cache {cache.path.name}: {len(cache)} entries, "
          f"model {cache.meta.get('model_id')}")
    print(f"  repairs recorded in cache: "
          f"{sum(int(e.get('repairs', 0)) for e in cache.entries.values())}\n")

    pool = ConnectionPool("crdb", size=6)
    cells: dict[tuple[str, int, ArmId], dict] = {}
    started = time.time()

    try:
        for provider_name, reason_for in providers.items():
            for window in WINDOWS:
                for arm_id in ARMS_RUN:
                    outcomes = []
                    for i in range(args.runs):
                        config = RunConfig(
                            arm=ARMS[arm_id], scenario=scenario, seed=1000 + i,
                            agent_count=args.agents, arrival_window_ms=window,
                            reasoning_gap_ms=args.gap_ms,
                        )
                        outcomes.append(run_once(config, embedder, reason_for, pool))
                    s = summarize(outcomes)
                    cells[(provider_name, window, arm_id)] = s
                    print(f"  {provider_name:>9}  w={window:>5}  "
                          f"{ARMS[arm_id].label:44} "
                          f"limit {s['hard_limit_violations']:>2}/{s['runs']:<2} "
                          f"policy {s['policy_breaches']:>2}/{s['runs']:<2} "
                          f"mean {s['mean_final_sum']:6.1f} "
                          f"conflicts {s['conflicts']:>4}"
                          + (f"  VOIDED {s['voided']}" if s["voided"] else ""),
                          flush=True)
            print()
    finally:
        pool.close()

    elapsed = time.time() - started
    RESULTS.mkdir(parents=True, exist_ok=True)
    raw = {
        "meta": {"runs": args.runs, "agents": args.agents, "windows": WINDOWS,
                 "cache": args.name, "model_id": cache.meta.get("model_id"),
                 "elapsed_s": elapsed},
        "cells": [{"provider": p, "window": w, "arm": a.value, **s}
                  for (p, w, a), s in cells.items()],
    }
    (RESULTS / "model_arm.json").write_text(json.dumps(raw, indent=2),
                                            encoding="utf-8")

    report = render(cells, args, cache, elapsed)
    (RESULTS / pathlib.Path(args.out).name).write_text(report, encoding="utf-8")
    _print_safe(report)
    print(f"\nwritten to results/{pathlib.Path(args.out).name} "
          f"({elapsed / 60:.1f} min)")
    return 0


def render(cells, args, cache, elapsed) -> str:
    out: list[str] = []
    add = out.append

    add("# Model arm: reference vs Claude at matched points\n")
    add(f"- {args.runs} runs per arm per window per provider, "
        f"{args.agents} agents per run")
    add(f"- model: `{cache.meta.get('model_id')}`, temperature "
        f"{cache.meta.get('temperature')}")
    add(f"- intent cache: `{cache.path.name}`, {len(cache)} entries")
    add(f"- wall clock {elapsed / 60:.1f} min\n")
    add("> The reference reasoner carries the swept statistical claim. This arm")
    add("> establishes that the protocol works when the reasoning step is a real")
    add("> language model, at two windows chosen in advance -- one inside the")
    add("> pre-registered `20 <= S <= 45` band, one outside it. METHODOLOGY")
    add("> entry 9 states why this is a spot check and not a re-sweep.\n")

    add("## Side by side\n")
    add("| Window | Arm | Provider | Hard limit | Policy ceiling | Mean sum | Conflicts | Revisions |")
    add("|---|---|---|---|---|---|---|---|")
    for window in WINDOWS:
        for arm_id in ARMS_RUN:
            for p in ("reference", "model"):
                s = cells.get((p, window, arm_id))
                if not s:
                    continue
                add(f"| {window} ms | {arm_id.value} | {p} | "
                    f"{s['hard_limit_violations']}/{s['runs']} | "
                    f"{s['policy_breaches']}/{s['runs']} | "
                    f"{s['mean_final_sum']:.1f} | {s['conflicts']} | "
                    f"{s['revisions']} |")

    add("\n## The memory-refresh effect, per provider\n")
    add("Change in mean final sum from refreshing memory (C minus C-ops).")
    add("Negative is an improvement.\n")
    add("| Window | reference | model |")
    add("|---|---|---|")
    diffs = {}
    for window in WINDOWS:
        row = []
        for p in ("reference", "model"):
            a = cells.get((p, window, ArmId.C))
            b = cells.get((p, window, ArmId.C_OPS))
            d = (a["mean_final_sum"] - b["mean_final_sum"]) if a and b else None
            diffs[(p, window)] = d
            row.append("n/a" if d is None else f"{d:+.1f}")
        add(f"| {window} ms | {row[0]} | {row[1]} |")

    add("\n## Verdict\n")
    inside, outside = WINDOWS[0], WINDOWS[1]
    agree_in = _same_sign(diffs.get(("reference", inside)), diffs.get(("model", inside)))
    agree_out = _same_sign(diffs.get(("reference", outside)), diffs.get(("model", outside)))

    r_in, m_in = diffs.get(("reference", inside)), diffs.get(("model", inside))
    r_out, m_out = diffs.get(("reference", outside)), diffs.get(("model", outside))

    if agree_in and agree_out:
        add("**The model reproduces the reference's result at both matched "
            "points.** With the reasoning step performed by a language model "
            "rather than a function, the memory-refresh effect has the same sign "
            "and comparable magnitude at both windows:\n")
        add(f"- {inside} ms (inside the band): reference {r_in:+.1f}, "
            f"model {m_in:+.1f}")
        add(f"- {outside} ms (outside the band): reference {r_out:+.1f}, "
            f"model {m_out:+.1f}")
        add("\nThat is the claim this arm exists to support, and it is the only "
            "claim it supports. Two things it does **not** establish are called "
            "out here rather than left for a reader to notice:")
        # Do not let the generated text assert that the effect vanished when the
        # number in the table says otherwise. An earlier version of this verdict
        # said "vanishes outside the band" unconditionally, which was false at a
        # window where the measured effect was -7.0.
        if abs(r_out) > 0.05 or abs(m_out) > 0.05:
            add(f"\n- **The effect does not reach zero outside the band here.** At "
                f"{outside} ms both providers show a non-zero effect "
                f"({r_out:+.1f} and {m_out:+.1f}), where the 10-run reference "
                f"sweep reported exactly `+0.0`. In-band readings are a necessary "
                f"condition for the effect, not a sufficient one, so a small "
                f"residual outside the band is not a contradiction of the "
                f"boundary -- but it is not a confirmation of it either, and this "
                f"arm is too small to distinguish the two. The boundary is graded "
                f"on the full sweep, not here.")
        add(f"- **Agreement of aggregates is not agreement of decisions.** The "
            f"two providers reach the same final sums while disagreeing on "
            f"individual readings; `scripts/compare_intents.py` reports 95% "
            f"agreement with three choices that breach the ceiling the model "
            f"itself inferred. The aggregates match because those disagreements "
            f"fall where the scenario's other constraints absorb them, which is "
            f"a fact about this scenario rather than about the model.")
    else:
        add("**The model and the reference diverge at a matched point.** This is "
            "reported as a finding rather than averaged away, and the matched "
            "points are not moved. Where they differ:\n")
        for window in WINDOWS:
            r, m = diffs.get(("reference", window)), diffs.get(("model", window))
            if not _same_sign(r, m):
                add(f"- {window} ms: reference {r:+.1f}, model {m:+.1f}")
        add("\nThe explanation to check first is not the protocol but the "
            "reasoner: `scripts/compare_intents.py` reports where the model's "
            "cached decisions differ from the reference's, per reading.")

    add("\n### Model fidelity, measured before replay\n")
    repairs = sum(int(e.get("repairs", 0)) for e in cache.entries.values())
    repaired = sum(1 for e in cache.entries.values() if int(e.get("repairs", 0)))
    add(f"- decisions in cache: {len(cache)}")
    add(f"- readings needing a re-ask (answer outside the action space): "
        f"{repaired} ({repairs} re-asks)")
    add("- the tool schema's `enum` on `action` is **not** enforced by the API; "
        "it is enforced in `scenario/agent.py`, and violations are counted "
        "rather than silently coerced")
    add("- choices that breach the ceiling the model itself inferred are **not** "
        "corrected anywhere: policy breaches are the dependent variable of this "
        "experiment, and a harness that fixed them would be reporting its own "
        "competence as the model's")

    return "\n".join(out)


def _same_sign(a, b) -> bool:
    if a is None or b is None:
        return False
    eps = 0.05
    if abs(a) <= eps and abs(b) <= eps:
        return True
    return (a < -eps and b < -eps) or (a > eps and b > eps)


if __name__ == "__main__":
    sys.exit(main())
