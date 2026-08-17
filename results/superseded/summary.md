# Sweep results

- 10 runs per arm per window, 20 agents per run
- reasoning provider: **reference**
- hard limit $100; policy ceiling $80 lowered to $60 mid-run
- wall clock 15.6 min

> The reasoning step is the deterministic reference implementation,
> not a language model. This measures the protocol, which is what the
> hypothesis is about, and it is stated here rather than implied.
> The model arm is a spot check at two matched windows; see
> METHODOLOGY entry 9 for why it is not a full re-sweep.

The pre-registered checks come first, before any aggregate table, so
that the predictions are graded against the data rather than the data
summarised and the predictions consulted afterwards.


## (a) Pre-registered boundary check

METHODOLOGY entry 8, written before this ran: refreshing memory can
change an outcome **only** where the post-conflict reading falls in
`[20, 45]`, and contributes **exactly zero** outside it.

| Window | re-decision reads | in band | median read | memory-refresh effect | consistent |
|---|---|---|---|---|---|
| 400 ms | 129 | 126 (98%) | 45 | +0.0 | yes |
| 1000 ms | 48 | 48 (100%) | 45 | -28.0 | yes |
| 1500 ms | 45 | 39 (87%) | 45 | -28.0 | yes |
| 2500 ms | 45 | 10 (22%) | 80 | +0.0 | yes |
| 4000 ms | 36 | 11 (31%) | 80 | +0.0 | yes |

### Verdict

**The pre-registered boundary held.** Every window where the memory-refresh effect was non-zero had re-decision readings inside the predicted band, and every window with no readings in band showed no effect.

A conditional effect whose boundary was derived and written down before the measurement is stronger evidence than an unconditional one: it predicts where the effect must vanish, and that prediction can fail.


## (b) Per-window shape

Registered prediction: **non-monotonic and peaked** — near zero at tight
windows (readings below the band), maximal in the middle (readings inside
it), falling again at wide windows (readings above it).

| Window | memory-refresh effect |
|---|---|
| 400 ms |    +0.0  |
| 1000 ms |   -28.0 ############################ |
| 1500 ms |   -28.0 ############################ |
| 2500 ms |    +0.0  |
| 4000 ms |    +0.0  |

**Shape held.** The effect peaks at 1000 ms with tails on both sides, as registered.


## (c) C-vs-B and C-vs-C-ops, separately

Registered prediction: at wide windows the C-vs-C-ops gap closes while
the C-vs-B gap stays large. "Memory refresh stops mattering" must not
be read as "conflict-awareness stops mattering".

| Window | C vs B (mean sum) | C vs C-ops (mean sum) | C vs B (policy breaches) |
|---|---|---|---|
| 400 ms | -175.5 | +0.0 | 0 vs 10 of 10 |
| 1000 ms | -144.0 | -28.0 | 0 vs 10 of 10 |
| 1500 ms | -126.0 | -28.0 | 2 vs 10 of 10 |
| 2500 ms | -109.5 | +0.0 | 10 vs 9 of 10 |
| 4000 ms | -111.5 | +0.0 | 10 vs 10 of 10 |


## (d) Aggregate tables


### Arrival window 400 ms

| Arm | Hard-limit violations | Policy breaches | Mean final sum | Conflicts | Revisions | Committed | Abstained |
|---|---|---|---|---|---|---|---|
| A · postgres RC · naive | 10/10 | 10/10 | 240.0 | 0 | 0 | 56 | 144 |
| B · cockroach · naive | 10/10 | 10/10 | 220.5 | 373 | 0 | 49 | 96 |
| C-ops · cockroach · re-reason, stale memory | 0/10 | 0/10 | 45.0 | 159 | 130 | 10 | 190 |
| C · cockroach · full refresh | 0/10 | 0/10 | 45.0 | 129 | 126 | 10 | 190 |

### Arrival window 1000 ms

| Arm | Hard-limit violations | Policy breaches | Mean final sum | Conflicts | Revisions | Committed | Abstained |
|---|---|---|---|---|---|---|---|
| A · postgres RC · naive | 10/10 | 10/10 | 214.5 | 0 | 0 | 49 | 151 |
| B · cockroach · naive | 10/10 | 10/10 | 189.0 | 202 | 0 | 42 | 133 |
| C-ops · cockroach · re-reason, stale memory | 0/10 | 8/10 | 73.0 | 93 | 62 | 18 | 182 |
| C · cockroach · full refresh | 0/10 | 0/10 | 45.0 | 48 | 48 | 10 | 190 |

### Arrival window 1500 ms

| Arm | Hard-limit violations | Policy breaches | Mean final sum | Conflicts | Revisions | Committed | Abstained |
|---|---|---|---|---|---|---|---|
| A · postgres RC · naive | 9/10 | 10/10 | 193.0 | 0 | 0 | 44 | 156 |
| B · cockroach · naive | 9/10 | 10/10 | 178.0 | 88 | 0 | 40 | 155 |
| C-ops · cockroach · re-reason, stale memory | 0/10 | 10/10 | 80.0 | 59 | 34 | 20 | 180 |
| C · cockroach · full refresh | 0/10 | 2/10 | 52.0 | 45 | 42 | 12 | 188 |

### Arrival window 2500 ms

| Arm | Hard-limit violations | Policy breaches | Mean final sum | Conflicts | Revisions | Committed | Abstained |
|---|---|---|---|---|---|---|---|
| A · postgres RC · naive | 9/10 | 10/10 | 143.0 | 0 | 0 | 34 | 166 |
| B · cockroach · naive | 9/10 | 9/10 | 189.5 | 119 | 0 | 45 | 145 |
| C-ops · cockroach · re-reason, stale memory | 0/10 | 10/10 | 80.0 | 55 | 38 | 20 | 180 |
| C · cockroach · full refresh | 0/10 | 10/10 | 80.0 | 45 | 38 | 20 | 180 |

### Arrival window 4000 ms

| Arm | Hard-limit violations | Policy breaches | Mean final sum | Conflicts | Revisions | Committed | Abstained |
|---|---|---|---|---|---|---|---|
| A · postgres RC · naive | 7/10 | 10/10 | 112.5 | 0 | 0 | 27 | 173 |
| B · cockroach · naive | 10/10 | 10/10 | 191.5 | 79 | 0 | 47 | 151 |
| C-ops · cockroach · re-reason, stale memory | 0/10 | 10/10 | 80.0 | 38 | 29 | 20 | 180 |
| C · cockroach · full refresh | 0/10 | 10/10 | 80.0 | 36 | 28 | 20 | 180 |


### Full decomposition

Change in mean final sum. Negative is an improvement.

| Window | isolation surfaces conflict (B-A) | re-reason over fresh state (C-ops-B) | refresh memory (C-C-ops) |
|---|---|---|---|
| 400 ms | -19.5 | -175.5 | +0.0 |
| 1000 ms | -25.5 | -116.0 | -28.0 |
| 1500 ms | -15.0 | -98.0 | -28.0 |
| 2500 ms | +46.5 | -109.5 | +0.0 |
| 4000 ms | +79.0 | -111.5 | +0.0 |
