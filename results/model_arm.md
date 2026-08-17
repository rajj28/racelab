# Model arm: reference vs Claude at matched points

- 10 runs per arm per window per provider, 20 agents per run
- model: `us.anthropic.claude-sonnet-4-5-20250929-v1:0`, temperature 0.0
- intent cache: `model.json`, 60 entries
- wall clock 9.4 min

> The reference reasoner carries the swept statistical claim. This arm
> establishes that the protocol works when the reasoning step is a real
> language model, at two windows chosen in advance -- one inside the
> pre-registered `20 <= S <= 45` band, one outside it. METHODOLOGY
> entry 9 states why this is a spot check and not a re-sweep.

## Side by side

| Window | Arm | Provider | Hard limit | Policy ceiling | Mean sum | Conflicts | Revisions |
|---|---|---|---|---|---|---|---|
| 400 ms | C-ops | reference | 0/10 | 10/10 | 80.0 | 205 | 151 |
| 400 ms | C-ops | model | 0/10 | 10/10 | 80.0 | 227 | 148 |
| 400 ms | C | reference | 0/10 | 1/10 | 48.5 | 151 | 151 |
| 400 ms | C | model | 0/10 | 1/10 | 48.5 | 152 | 152 |
| 2500 ms | C-ops | reference | 0/10 | 10/10 | 80.0 | 146 | 80 |
| 2500 ms | C-ops | model | 0/10 | 10/10 | 80.0 | 103 | 66 |
| 2500 ms | C | reference | 0/10 | 9/10 | 76.5 | 76 | 67 |
| 2500 ms | C | model | 0/10 | 7/10 | 69.5 | 91 | 74 |

## The memory-refresh effect, per provider

Change in mean final sum from refreshing memory (C minus C-ops).
Negative is an improvement.

| Window | reference | model |
|---|---|---|
| 400 ms | -31.5 | -31.5 |
| 2500 ms | -3.5 | -10.5 |

## Verdict

**The model reproduces the reference's result at both matched points.** With the reasoning step performed by a language model rather than a function, the memory-refresh effect has the same sign and comparable magnitude at both windows:

- 400 ms (inside the band): reference -31.5, model -31.5
- 2500 ms (outside the band): reference -3.5, model -10.5

That is the claim this arm exists to support, and it is the only claim it supports. Two things it does **not** establish are called out here rather than left for a reader to notice:

- **The effect does not reach zero outside the band here.** At 2500 ms both providers show a non-zero effect (-3.5 and -10.5), where the 10-run reference sweep reported exactly `+0.0`. In-band readings are a necessary condition for the effect, not a sufficient one, so a small residual outside the band is not a contradiction of the boundary -- but it is not a confirmation of it either, and this arm is too small to distinguish the two. The boundary is graded on the full sweep, not here.
- **Agreement of aggregates is not agreement of decisions.** The two providers reach the same final sums while disagreeing on individual readings; `scripts/compare_intents.py` reports 95% agreement with three choices that breach the ceiling the model itself inferred. The aggregates match because those disagreements fall where the scenario's other constraints absorb them, which is a fact about this scenario rather than about the model.

### Model fidelity, measured before replay

- decisions in cache: 60
- readings needing a re-ask (answer outside the action space): 1 (1 re-asks)
- the tool schema's `enum` on `action` is **not** enforced by the API; it is enforced in `scenario/agent.py`, and violations are counted rather than silently coerced
- choices that breach the ceiling the model itself inferred are **not** corrected anywhere: policy breaches are the dependent variable of this experiment, and a harness that fixed them would be reporting its own competence as the model's