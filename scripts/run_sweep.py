"""Run the swept experiment across four arms and a range of arrival windows.

    python scripts/run_sweep.py --smoke            # 2 runs/cell, check it works
    python scripts/run_sweep.py --runs 20          # the shape sweep
    python scripts/run_sweep.py --runs 100 --windows 1500   # the headline cell

Writes `results/sweep.md` plus the raw cells as `.json`, and checks the
observed re-decision readings
against the ablation boundary pre-registered in METHODOLOGY entry 8.

The reasoning step defaults to the deterministic reference implementation, and
the output says so on every table. Swap it for cached model intents with
`--provider cache:<name>` once stage-1 generation has been run; the provider is
recorded in the report because a reference result and a model result are not
interchangeable.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from racelab.arms import ARMS, ORDER, ArmId, contributions
from racelab.db import ConnectionPool
from racelab.integrations import ccloud
from racelab.embeddings import get_embedder
from racelab.experiment import RunConfig, Scenario, run_once, summarize
from scenario.corpus import HERO
from scenario.decide import RETRIEVAL_QUERY, infer_ceiling, propose
from scenario.intents import IntentCache

RESULTS = pathlib.Path(__file__).resolve().parents[1] / "results"

# Pre-registered in METHODOLOGY entry 8, before the sweep was run.
BOUNDARY_LOW, BOUNDARY_HIGH = 20, 45

DEFAULT_WINDOWS = [400, 1000, 1500, 2500, 4000]


def build_scenario() -> Scenario:
    return Scenario(
        account_id=HERO.account_id,
        hard_limit=HERO.hard_limit,
        stale_ceiling=80,
        current_ceiling=60,
        retrieval_query=RETRIEVAL_QUERY,
        update_memory=HERO.update[0],
    )


def reference_reason(memories, observed, hard_limit):
    ceiling, _ = infer_ceiling(memories)
    return propose(ceiling, observed, hard_limit)


def _make_cache_reason(cache: IntentCache):
    """Replay a cached decision, keyed by the context the agent actually got.

    The corpus state is *derived* from the retrieved memory ids rather than from
    the clock: if the superseding policy is among them the lookup is `fresh`,
    otherwise `stale`. So an agent whose refresh genuinely failed to surface the
    new policy replays the stale decision, which is the honest answer rather
    than the tidy one.

    A miss raises. Stage 1 enumerates every reachable reading, so a miss means
    the cache does not cover this arm's action space -- not that this reading is
    exotic -- and falling back to the reference here would silently mix a
    deterministic decision into a result labelled as the model's.
    """
    update_id = HERO.update[0].memory_id

    def reason(memories, observed, hard_limit):
        state = "fresh" if any(m.memory_id == update_id for m in memories) else "stale"
        return cache.lookup(HERO.account_id, state, observed)

    return reason


def main() -> int:
    ap = argparse.ArgumentParser(description="run the RaceLab sweep")
    ap.add_argument("--runs", type=int, default=20, help="runs per arm per window")
    ap.add_argument("--agents", type=int, default=20)
    ap.add_argument("--windows", type=int, nargs="+", default=DEFAULT_WINDOWS)
    ap.add_argument("--gap-ms", type=float, default=200.0)
    ap.add_argument("--smoke", action="store_true", help="2 runs, 2 windows")
    ap.add_argument("--provider", default="reference",
                    help="reference | cache:<name>")
    ap.add_argument("--arms", nargs="+", default=None,
                    help="subset of arm ids to run, e.g. --arms C-ops C")
    ap.add_argument("--pool", type=int, default=6,
                    help="pooled connections for memory retrieval")
    ap.add_argument("--out", default="sweep.md",
                    help="filename under results/; a path prefix is stripped")
    args = ap.parse_args()

    if args.smoke:
        args.runs, args.windows = 2, [400, 1500]

    arms_to_run = [a for a in ORDER if a.value in args.arms] if args.arms else list(ORDER)
    if not arms_to_run:
        print(f"no arms matched {args.arms}; valid: {[a.value for a in ORDER]}",
              file=sys.stderr)
        return 2

    if args.provider == "reference":
        reason_for = reference_reason
    elif args.provider.startswith("cache:"):
        name = args.provider.split(":", 1)[1]
        cache = IntentCache(name)
        if not len(cache):
            print(f"intent cache {name!r} is empty; run scripts/gen_intents.py first",
                  file=sys.stderr)
            return 2
        # Refuses to let a reference-built cache be reported as a model result.
        cache.require_provider(name)
        reason_for = _make_cache_reason(cache)
        print(f"replaying cached intents: {len(cache)} entries, "
              f"kind {cache.meta.get('kind')!r}, model {cache.meta.get('model_id')}")
    else:
        print(f"unknown provider {args.provider!r}; expected 'reference' or "
              f"'cache:<name>'", file=sys.stderr)
        return 2

    # Control-plane preflight, before a single agent starts. This exists because
    # the first full sweep died mid-run on a connection ceiling that SQL could
    # not have warned about (FEEDBACK entry 6). The agent now asks the control
    # plane whether the concurrency it is about to create is sane for the plan.
    #
    # Advisory when ccloud is not authenticated, blocking when it is and says no:
    # a repository that cannot be run without a control-plane session would be
    # worse, but a session that reports a real problem should stop the run.
    planned = args.agents + 1 + args.pool  # per-agent racing conns + updater + pool
    try:
        pf = ccloud.preflight(planned_connections=planned)
        print(pf.explain())
        if not pf.ok and pf.cluster_name is not None:
            print("\npreflight refused the run. Reduce --agents/--pool or raise the "
                  "cluster plan.", file=sys.stderr)
            return 3
    except ccloud.CcloudUnavailable as exc:
        print(f"control-plane preflight skipped: {exc}")
    print()

    scenario = build_scenario()
    embedder = get_embedder("titan")
    print(f"RaceLab sweep — {args.runs} runs/arm/window, {args.agents} agents/run")
    print(f"windows: {args.windows} ms | reasoning gap {args.gap_ms} ms")
    print(f"reasoning provider: {args.provider}")
    print(f"hard limit ${scenario.hard_limit}, ceiling ${scenario.stale_ceiling} "
          f"-> ${scenario.current_ceiling} mid-run\n")

    started = time.time()
    cells: dict[tuple[int, ArmId], dict] = {}
    # One pool for the whole sweep, and it is for MEMORY RETRIEVAL ONLY.
    #
    # Twenty agents each holding a racing connection and a memory connection is
    # forty concurrent connections per run, which CockroachDB Cloud Basic
    # declines -- it is what killed the first full sweep at the 2500 ms window.
    # Racing connections must stay per-agent: separate connections are what make
    # them race, and pooling them would serialise the very interleaving under
    # test.
    #
    # Pooling retrieval is safe for one specific reason: memory is read BEFORE
    # `BEGIN` and AFTER `ROLLBACK`, never between them. A pooled connection
    # therefore cannot join a transaction's refresh span.
    #
    # If a pooled connection ever did participate in the raced transaction it
    # would silently widen that refresh span -- adding reads the agent never
    # made to the set the cluster checks for serializability -- and the
    # experiment would be measuring an artefact of its own connection handling.
    # It would not error. It would just quietly produce different conflict rates
    # and no signal that anything was wrong. Hence: this pool never touches a
    # transaction.
    memory_pool = ConnectionPool("crdb", size=args.pool)

    for window in args.windows:
        for arm_id in arms_to_run:
            arm = ARMS[arm_id]
            outcomes = []
            for i in range(args.runs):
                config = RunConfig(
                    arm=arm, scenario=scenario, seed=1000 + i,
                    agent_count=args.agents, arrival_window_ms=window,
                    reasoning_gap_ms=args.gap_ms,
                )
                outcomes.append(
                    run_once(config, embedder, reason_for, memory_pool)
                )
            summary = summarize(outcomes)
            cells[(window, arm_id)] = summary
            print(f"  w={window:>5}  {arm.label:44} "
                  f"limit {summary['hard_limit_violations']:>3}/{summary['runs']:<3} "
                  f"policy {summary['policy_breaches']:>3}/{summary['runs']:<3} "
                  f"mean sum {summary['mean_final_sum']:6.1f} "
                  f"conflicts {summary['conflicts']:>4} "
                  f"revisions {summary['revisions']:>4}"
                  + ("  VOIDED " + str(summary["voided"]) if summary["voided"] else ""))
        print()

    memory_pool.close()
    elapsed = time.time() - started
    RESULTS.mkdir(parents=True, exist_ok=True)

    # Persist the raw cells before rendering. A sweep is expensive, and the
    # report is a view of it -- re-running an hour of measurement because the
    # presentation needed changing would be self-inflicted.
    raw = {
        "meta": {"runs": args.runs, "agents": args.agents, "windows": args.windows,
                 "gap_ms": args.gap_ms, "provider": args.provider,
                 "elapsed_s": elapsed},
        "cells": [{"window": w, "arm": a.value, **s} for (w, a), s in cells.items()],
    }
    (RESULTS / (pathlib.Path(args.out).stem + ".json")).write_text(
        json.dumps(raw, indent=2), encoding="utf-8"
    )

    report = render(cells, args, scenario, elapsed)
    # Take only the basename: `--out results/x.md` would otherwise resolve to
    # results/results/x.md and die here, after every cell had already been
    # measured. It did exactly that once.
    out_name = pathlib.Path(args.out).name
    (RESULTS / out_name).write_text(report, encoding="utf-8")
    _print_safe(report)
    print(f"\nwritten to results/{args.out}  ({elapsed / 60:.1f} min)")
    return 0


def _print_safe(text: str) -> None:
    """The Windows console default is cp1252 and cannot encode every character
    a report may contain. Losing a finished sweep to an encoding error while
    printing it would be an absurd way to lose an hour of measurement."""
    enc = sys.stdout.encoding or "utf-8"
    print(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def render(cells, args, scenario, elapsed) -> str:
    lines: list[str] = []
    add = lines.append

    add("# Sweep results\n")
    add(f"- {args.runs} runs per arm per window, {args.agents} agents per run")
    add(f"- reasoning provider: **{args.provider}**")
    add(f"- hard limit ${scenario.hard_limit}; policy ceiling "
        f"${scenario.stale_ceiling} lowered to ${scenario.current_ceiling} mid-run")
    add(f"- wall clock {elapsed / 60:.1f} min\n")

    if args.provider == "reference":
        add("> The reasoning step is the deterministic reference implementation,")
        add("> not a language model. This measures the protocol, which is what the")
        add("> hypothesis is about, and it is stated here rather than implied.")
        add("> The model arm is a spot check at two matched windows; see")
        add("> METHODOLOGY entry 9 for why it is not a full re-sweep.\n")

    add("The pre-registered checks come first, before any aggregate table, so")
    add("that the predictions are graded against the data rather than the data")
    add("summarised and the predictions consulted afterwards.\n")

    # -- (a) the boundary ------------------------------------------------
    add("\n## (a) Pre-registered boundary check\n")
    add("METHODOLOGY entry 8, written before this ran: refreshing memory can")
    add(f"change an outcome **only** where the post-conflict reading falls in")
    add(f"`[{BOUNDARY_LOW}, {BOUNDARY_HIGH}]`, and contributes **exactly zero** outside it.\n")
    add("| Window | re-decision reads | in band | median read | memory-refresh effect | consistent |")
    add("|---|---|---|---|---|---|")
    for window in args.windows:
        reads = cells[(window, ArmId.C)]["redecision_reads"]
        effect = (cells[(window, ArmId.C)]["mean_final_sum"]
                  - cells[(window, ArmId.C_OPS)]["mean_final_sum"])
        in_band = sum(1 for r in reads if BOUNDARY_LOW <= r <= BOUNDARY_HIGH)
        pct = (100.0 * in_band / len(reads)) if reads else 0.0
        median = statistics.median(reads) if reads else float("nan")
        ok = "yes" if (in_band > 0 or abs(effect) <= 0.05) else "**NO**"
        add(f"| {window} ms | {len(reads)} | {in_band} ({pct:.0f}%) "
            f"| {median:.0f} | {effect:+.1f} | {ok} |")

    add("\n" + verdict(cells, args))

    # -- (b) the shape ---------------------------------------------------
    add("\n\n## (b) Per-window shape\n")
    add("Registered prediction: **non-monotonic and peaked** — near zero at tight")
    add("windows (readings below the band), maximal in the middle (readings inside")
    add("it), falling again at wide windows (readings above it).\n")
    effects = [(w, cells[(w, ArmId.C)]["mean_final_sum"]
                   - cells[(w, ArmId.C_OPS)]["mean_final_sum"]) for w in args.windows]
    add("| Window | memory-refresh effect |")
    add("|---|---|")
    for w, e in effects:
        bar = "#" * int(min(40, abs(e)))
        add(f"| {w} ms | {e:+7.1f} {bar} |")
    add("\n" + shape_verdict(effects))

    # -- (c) the two gaps ------------------------------------------------
    add("\n\n## (c) C-vs-B and C-vs-C-ops, separately\n")
    add("Registered prediction: at wide windows the C-vs-C-ops gap closes while")
    add("the C-vs-B gap stays large. \"Memory refresh stops mattering\" must not")
    add("be read as \"conflict-awareness stops mattering\".\n")
    add("| Window | C vs B (mean sum) | C vs C-ops (mean sum) | C vs B (policy breaches) |")
    add("|---|---|---|---|")
    for window in args.windows:
        b = cells[(window, ArmId.B)]
        co = cells[(window, ArmId.C_OPS)]
        c = cells[(window, ArmId.C)]
        add(f"| {window} ms | {c['mean_final_sum'] - b['mean_final_sum']:+.1f} "
            f"| {c['mean_final_sum'] - co['mean_final_sum']:+.1f} "
            f"| {c['policy_breaches']} vs {b['policy_breaches']} of {b['runs']} |")

    # -- (d) the aggregates ----------------------------------------------
    add("\n\n## (d) Aggregate tables\n")
    for window in args.windows:
        add(f"\n### Arrival window {window} ms\n")
        add("| Arm | Hard-limit violations | Policy breaches | Mean final sum | "
            "Conflicts | Revisions | Committed | Abstained |")
        add("|---|---|---|---|---|---|---|---|")
        # Derived from the cells actually present, not from a CLI flag. `render`
        # is also called by scripts/render_sweep.py against a saved JSON file,
        # where no argument list exists -- an earlier version referenced the
        # flag here and raised NameError at the end of a 25-cell sweep, after
        # every measurement was already done.
        for arm_id in [a for a in ORDER if (window, a) in cells]:
            s = cells[(window, arm_id)]
            add(f"| {ARMS[arm_id].label} | {s['hard_limit_violations']}/{s['runs']} "
                f"| {s['policy_breaches']}/{s['runs']} | {s['mean_final_sum']:.1f} "
                f"| {s['conflicts']} | {s['revisions']} | {s['committed']} "
                f"| {s['abstained']} |")

    add("\n\n### Full decomposition\n")
    add("Change in mean final sum. Negative is an improvement.\n")
    add("`B-A` crosses PostgreSQL and CockroachDB at different network latencies,")
    add("so it confounds isolation level with deployment. `B-A-rc` is the")
    add("vendor-controlled version: same cluster, only the isolation level differs.")
    add("Where the two disagree, trust the controlled one.\n")
    add("| Window | B-A (confounded) | B-A-rc (controlled) | re-reason over fresh "
        "state (C-ops-B) | refresh memory (C-C-ops) |")
    add("|---|---|---|---|---|")
    for window in args.windows:
        by_arm = {a: cells[(window, a)]["mean_final_sum"]
                  for a in ORDER if (window, a) in cells}
        c = contributions(by_arm)

        def fmt(key: str) -> str:
            v = c.get(key)
            return "n/a" if v is None else f"{v:+.1f}"

        add(f"| {window} ms | {fmt('isolation_surfaces_conflict')} "
            f"| {fmt('isolation_surfaces_conflict_same_vendor')} "
            f"| {fmt('re_reasoning_over_fresh_operational_state')} "
            f"| {fmt('refreshing_semantic_memory')} |")

    # Rates, not means. The means above are noisy for the naive arms because they
    # depend on how many agents happened to get through, which moves with
    # deployment latency. The violation rate does not.
    add("\n\n### Hard-limit violation rate, all windows pooled\n")
    add("The primary metric. A rate, not a mean, so it does not move with the")
    add("action space or with how fast the backend answers.\n")
    add("| Arm | Runs over the hard limit | Runs |")
    add("|---|---|---|")
    for arm_id in ORDER:
        rows = [cells[(w, arm_id)] for w in args.windows if (w, arm_id) in cells]
        if not rows:
            continue
        bad = sum(r["hard_limit_violations"] for r in rows)
        tot = sum(r["runs"] for r in rows)
        add(f"| {ARMS[arm_id].label} | {bad} | {tot} |")

    return "\n".join(lines) + "\n"


def shape_verdict(effects) -> str:
    """Was the effect curve non-monotonic, as registered?"""
    mags = [abs(e) for _, e in effects]
    if len(mags) < 3:
        return "_Too few windows to judge shape._"
    peak = mags.index(max(mags))
    non_monotonic = 0 < peak < len(mags) - 1
    if non_monotonic:
        return (f"**Shape held.** The effect peaks at "
                f"{effects[peak][0]} ms with tails on both sides, as registered.")
    where = "the tightest" if peak == 0 else "the widest"
    return (f"**Shape did not hold as registered.** The effect is largest at "
            f"{where} window ({effects[peak][0]} ms) rather than in the middle, so "
            f"the curve is monotonic over the range measured. Reported as a failed "
            f"prediction; the band itself is graded separately in (a) and is not "
            f"adjusted to fit this.")


def verdict(cells, args) -> str:
    """State whether the pre-registration held, including if it did not."""
    rows = []
    for window in args.windows:
        reads = cells[(window, ArmId.C)]["redecision_reads"]
        effect = (cells[(window, ArmId.C)]["mean_final_sum"]
                  - cells[(window, ArmId.C_OPS)]["mean_final_sum"])
        in_band = sum(1 for r in reads if BOUNDARY_LOW <= r <= BOUNDARY_HIGH)
        rows.append((window, len(reads), in_band, effect))

    # Checked in both directions. An earlier version tested only the first, and
    # marked as "consistent" a window where 126 of 129 readings were in band and
    # the effect was exactly zero -- which is not a literal contradiction, since
    # in-band is necessary rather than sufficient, but it is the case where the
    # registered *mechanism* has stopped explaining the numbers. That went
    # unnoticed for a whole sweep. See METHODOLOGY entry 10.
    violations = [
        f"window {w} ms: {n} re-decision reads, none in band, but the "
        f"memory-refresh effect was {e:+.1f} rather than 0"
        for w, n, ib, e in rows if n and ib == 0 and abs(e) > 0.05
    ]
    unexplained = [
        f"window {w} ms: {ib} of {n} re-decision reads in band, yet the "
        f"memory-refresh effect was {e:+.1f} -- in-band readings are a "
        f"necessary condition, so this is not a contradiction, but the "
        f"registered mechanism does not account for the zero"
        for w, n, ib, e in rows
        if n and ib > 0.5 * n and abs(e) <= 0.05
    ]

    out = ["### Verdict\n"]
    if violations:
        out.append("**The pre-registered boundary did not hold.**\n")
        out.extend(f"- {v}" for v in violations)
        out.append("\nThe stated mechanism does not fully explain the observed "
                   "difference between C-ops and C, so the decomposition should "
                   "not be relied on until that is understood.")
    elif unexplained:
        out.append("**The pre-registered boundary was not contradicted, but it is "
                   "not carrying the result either.**\n")
        out.extend(f"- {u}" for u in unexplained)
        out.append("\nDo not report this as the boundary holding. A window with "
                   "readings in band and no effect needs a mechanism, and until "
                   "there is one the agreement between the prediction and the "
                   "numbers is not evidence for the prediction.")
    else:
        out.append("**The pre-registered boundary held.** Every window where the "
                   "memory-refresh effect was non-zero had re-decision readings "
                   "inside the predicted band, and every window with no readings "
                   "in band showed no effect.\n")
        out.append("A conditional effect whose boundary was derived and written "
                   "down before the measurement is stronger evidence than an "
                   "unconditional one: it predicts where the effect must vanish, "
                   "and that prediction can fail.")
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(main())
