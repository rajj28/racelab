# Sweep results

- 10 runs per arm per window, 20 agents per run
- reasoning provider: **reference**
- hard limit $100; policy ceiling $80 lowered to $60 mid-run
- wall clock 23.2 min

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
| 400 ms | 131 | 131 (100%) | 45 | -35.0 | yes |
| 1000 ms | 98 | 70 (71%) | 45 | -24.5 | yes |
| 1500 ms | 82 | 16 (20%) | 80 | +0.0 | yes |
| 2500 ms | 55 | 9 (16%) | 80 | +0.0 | yes |
| 4000 ms | 48 | 12 (25%) | 80 | +0.0 | yes |

### Verdict

**The pre-registered boundary held.** Every window where the memory-refresh effect was non-zero had re-decision readings inside the predicted band, and every window with no readings in band showed no effect.

A conditional effect whose boundary was derived and written down before the measurement is stronger evidence than an unconditional one: it predicts where the effect must vanish, and that prediction can fail.


## (b) Per-window shape

Registered prediction: **non-monotonic and peaked** — near zero at tight
windows (readings below the band), maximal in the middle (readings inside
it), falling again at wide windows (readings above it).

| Window | memory-refresh effect |
|---|---|
| 400 ms |   -35.0 ################################### |
| 1000 ms |   -24.5 ######################## |
| 1500 ms |    +0.0  |
| 2500 ms |    +0.0  |
| 4000 ms |    +0.0  |

**Shape did not hold as registered.** The effect is largest at the tightest window (400 ms) rather than in the middle, so the curve is monotonic over the range measured. Reported as a failed prediction; the band itself is graded separately in (a) and is not adjusted to fit this.


## (c) C-vs-B and C-vs-C-ops, separately

Registered prediction: at wide windows the C-vs-C-ops gap closes while
the C-vs-B gap stays large. "Memory refresh stops mattering" must not
be read as "conflict-awareness stops mattering".

| Window | C vs B (mean sum) | C vs C-ops (mean sum) | C vs B (policy breaches) |
|---|---|---|---|
| 400 ms | -184.5 | -35.0 | 0 vs 10 of 10 |
| 1000 ms | -199.0 | -24.5 | 3 vs 10 of 10 |
| 1500 ms | -168.0 | +0.0 | 10 vs 10 of 10 |
| 2500 ms | -134.5 | +0.0 | 10 vs 10 of 10 |
| 4000 ms | -109.0 | +0.0 | 10 vs 10 of 10 |


## (d) Aggregate tables


### Arrival window 400 ms

| Arm | Hard-limit violations | Policy breaches | Mean final sum | Conflicts | Revisions | Committed | Abstained |
|---|---|---|---|---|---|---|---|
| A · postgres RC · naive | 9/10 | 10/10 | 196.0 | 0 | 0 | 46 | 154 |
| B · cockroach · naive | 10/10 | 10/10 | 229.5 | 600 | 0 | 51 | 50 |
| C-ops · cockroach · re-reason, stale memory | 0/10 | 10/10 | 80.0 | 183 | 114 | 20 | 180 |
| C · cockroach · full refresh | 0/10 | 0/10 | 45.0 | 131 | 131 | 10 | 190 |

### Arrival window 1000 ms

| Arm | Hard-limit violations | Policy breaches | Mean final sum | Conflicts | Revisions | Committed | Abstained |
|---|---|---|---|---|---|---|---|
| A · postgres RC · naive | 10/10 | 10/10 | 190.0 | 0 | 0 | 44 | 156 |
| B · cockroach · naive | 10/10 | 10/10 | 254.5 | 407 | 0 | 59 | 82 |
| C-ops · cockroach · re-reason, stale memory | 0/10 | 10/10 | 80.0 | 138 | 97 | 20 | 180 |
| C · cockroach · full refresh | 0/10 | 3/10 | 55.5 | 98 | 98 | 13 | 187 |

### Arrival window 1500 ms

| Arm | Hard-limit violations | Policy breaches | Mean final sum | Conflicts | Revisions | Committed | Abstained |
|---|---|---|---|---|---|---|---|
| A · postgres RC · naive | 10/10 | 10/10 | 143.0 | 0 | 0 | 34 | 166 |
| B · cockroach · naive | 10/10 | 10/10 | 248.0 | 326 | 0 | 60 | 97 |
| C-ops · cockroach · re-reason, stale memory | 0/10 | 10/10 | 80.0 | 127 | 74 | 20 | 180 |
| C · cockroach · full refresh | 0/10 | 10/10 | 80.0 | 82 | 75 | 20 | 180 |

### Arrival window 2500 ms

| Arm | Hard-limit violations | Policy breaches | Mean final sum | Conflicts | Revisions | Committed | Abstained |
|---|---|---|---|---|---|---|---|
| A · postgres RC · naive | 9/10 | 10/10 | 147.5 | 0 | 0 | 35 | 165 |
| B · cockroach · naive | 10/10 | 10/10 | 214.5 | 253 | 0 | 53 | 112 |
| C-ops · cockroach · re-reason, stale memory | 0/10 | 10/10 | 80.0 | 95 | 56 | 20 | 180 |
| C · cockroach · full refresh | 0/10 | 10/10 | 80.0 | 55 | 47 | 20 | 180 |

### Arrival window 4000 ms

| Arm | Hard-limit violations | Policy breaches | Mean final sum | Conflicts | Revisions | Committed | Abstained |
|---|---|---|---|---|---|---|---|
| A · postgres RC · naive | 8/10 | 10/10 | 125.0 | 0 | 0 | 30 | 170 |
| B · cockroach · naive | 10/10 | 10/10 | 189.0 | 89 | 0 | 46 | 149 |
| C-ops · cockroach · re-reason, stale memory | 0/10 | 10/10 | 80.0 | 49 | 34 | 20 | 180 |
| C · cockroach · full refresh | 0/10 | 10/10 | 80.0 | 48 | 38 | 20 | 180 |


### Full decomposition

Change in mean final sum. Negative is an improvement.

| Window | isolation surfaces conflict (B-A) | re-reason over fresh state (C-ops-B) | refresh memory (C-C-ops) |
|---|---|---|---|
| 400 ms | +33.5 | -149.5 | -35.0 |
| 1000 ms | +64.5 | -174.5 | -24.5 |
| 1500 ms | +105.0 | -168.0 | +0.0 |
| 2500 ms | +67.0 | -134.5 | +0.0 |
| 4000 ms | +64.0 | -109.0 | +0.0 |
