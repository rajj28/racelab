# Sweep results

- 10 runs per arm per window, 20 agents per run
- reasoning provider: **reference**
- hard limit $100; policy ceiling $80 lowered to $60 mid-run
- wall clock 47.0 min

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
| 400 ms | 178 | 156 (88%) | 45 | -31.5 | yes |
| 1000 ms | 99 | 77 (78%) | 45 | -24.5 | yes |
| 1500 ms | 86 | 45 (52%) | 45 | -7.0 | yes |
| 2500 ms | 77 | 34 (44%) | 80 | -10.5 | yes |
| 4000 ms | 66 | 32 (48%) | 80 | -1.5 | yes |

### Verdict

**The pre-registered boundary held.** Every window where the memory-refresh effect was non-zero had re-decision readings inside the predicted band, and every window with no readings in band showed no effect.

A conditional effect whose boundary was derived and written down before the measurement is stronger evidence than an unconditional one: it predicts where the effect must vanish, and that prediction can fail.


## (b) Per-window shape

Registered prediction: **non-monotonic and peaked** — near zero at tight
windows (readings below the band), maximal in the middle (readings inside
it), falling again at wide windows (readings above it).

| Window | memory-refresh effect |
|---|---|
| 400 ms |   -31.5 ############################### |
| 1000 ms |   -24.5 ######################## |
| 1500 ms |    -7.0 ####### |
| 2500 ms |   -10.5 ########## |
| 4000 ms |    -1.5 # |

**Shape did not hold as registered.** The effect is largest at the tightest window (400 ms) rather than in the middle, so the curve is monotonic over the range measured. Reported as a failed prediction; the band itself is graded separately in (a) and is not adjusted to fit this.


## (c) C-vs-B and C-vs-C-ops, separately

Registered prediction: at wide windows the C-vs-C-ops gap closes while
the C-vs-B gap stays large. "Memory refresh stops mattering" must not
be read as "conflict-awareness stops mattering".

| Window | C vs B (mean sum) | C vs C-ops (mean sum) | C vs B (policy breaches) |
|---|---|---|---|
| 400 ms | -208.0 | -31.5 | 1 vs 10 of 10 |
| 1000 ms | -191.5 | -24.5 | 3 vs 10 of 10 |
| 1500 ms | -173.0 | -7.0 | 8 vs 10 of 10 |
| 2500 ms | -161.0 | -10.5 | 7 vs 10 of 10 |
| 4000 ms | -78.0 | -1.5 | 9 vs 7 of 10 |


## (d) Aggregate tables


### Arrival window 400 ms

| Arm | Hard-limit violations | Policy breaches | Mean final sum | Conflicts | Revisions | Committed | Abstained |
|---|---|---|---|---|---|---|---|
| A · postgres RC · naive | 10/10 | 10/10 | 372.5 | 0 | 0 | 83 | 117 |
| A-rc · cockroach RC · naive | 10/10 | 10/10 | 675.0 | 0 | 0 | 150 | 50 |
| B · cockroach · naive | 10/10 | 10/10 | 256.5 | 667 | 0 | 57 | 32 |
| C-ops · cockroach · re-reason, stale memory | 0/10 | 10/10 | 80.0 | 213 | 149 | 20 | 180 |
| C · cockroach · full refresh | 0/10 | 1/10 | 48.5 | 178 | 171 | 11 | 189 |

### Arrival window 1000 ms

| Arm | Hard-limit violations | Policy breaches | Mean final sum | Conflicts | Revisions | Committed | Abstained |
|---|---|---|---|---|---|---|---|
| A · postgres RC · naive | 10/10 | 10/10 | 240.5 | 0 | 0 | 55 | 145 |
| A-rc · cockroach RC · naive | 10/10 | 10/10 | 296.5 | 0 | 0 | 67 | 133 |
| B · cockroach · naive | 10/10 | 10/10 | 247.0 | 397 | 0 | 58 | 83 |
| C-ops · cockroach · re-reason, stale memory | 0/10 | 10/10 | 80.0 | 162 | 98 | 20 | 180 |
| C · cockroach · full refresh | 0/10 | 3/10 | 55.5 | 99 | 96 | 13 | 187 |

### Arrival window 1500 ms

| Arm | Hard-limit violations | Policy breaches | Mean final sum | Conflicts | Revisions | Committed | Abstained |
|---|---|---|---|---|---|---|---|
| A · postgres RC · naive | 9/10 | 10/10 | 263.0 | 0 | 0 | 60 | 140 |
| A-rc · cockroach RC · naive | 10/10 | 10/10 | 250.0 | 0 | 0 | 56 | 144 |
| B · cockroach · naive | 10/10 | 10/10 | 246.0 | 299 | 0 | 60 | 103 |
| C-ops · cockroach · re-reason, stale memory | 0/10 | 10/10 | 80.0 | 122 | 88 | 20 | 180 |
| C · cockroach · full refresh | 0/10 | 8/10 | 73.0 | 86 | 75 | 18 | 182 |

### Arrival window 2500 ms

| Arm | Hard-limit violations | Policy breaches | Mean final sum | Conflicts | Revisions | Committed | Abstained |
|---|---|---|---|---|---|---|---|
| A · postgres RC · naive | 10/10 | 10/10 | 169.0 | 0 | 0 | 40 | 160 |
| A-rc · cockroach RC · naive | 9/10 | 10/10 | 212.5 | 0 | 0 | 49 | 151 |
| B · cockroach · naive | 10/10 | 10/10 | 230.5 | 186 | 0 | 57 | 125 |
| C-ops · cockroach · re-reason, stale memory | 0/10 | 10/10 | 80.0 | 94 | 65 | 20 | 180 |
| C · cockroach · full refresh | 0/10 | 7/10 | 69.5 | 77 | 73 | 17 | 183 |

### Arrival window 4000 ms

| Arm | Hard-limit violations | Policy breaches | Mean final sum | Conflicts | Revisions | Committed | Abstained |
|---|---|---|---|---|---|---|---|
| A · postgres RC · naive | 6/10 | 10/10 | 116.5 | 0 | 0 | 27 | 173 |
| A-rc · cockroach RC · naive | 9/10 | 10/10 | 222.0 | 0 | 0 | 50 | 150 |
| B · cockroach · naive | 7/10 | 7/10 | 154.5 | 111 | 0 | 43 | 147 |
| C-ops · cockroach · re-reason, stale memory | 0/10 | 9/10 | 78.0 | 71 | 52 | 20 | 180 |
| C · cockroach · full refresh | 0/10 | 9/10 | 76.5 | 66 | 45 | 19 | 181 |


### Full decomposition

Change in mean final sum. Negative is an improvement.

`B-A` crosses PostgreSQL and CockroachDB at different network latencies,
so it confounds isolation level with deployment. `B-A-rc` is the
vendor-controlled version: same cluster, only the isolation level differs.
Where the two disagree, trust the controlled one.

| Window | B-A (confounded) | B-A-rc (controlled) | re-reason over fresh state (C-ops-B) | refresh memory (C-C-ops) |
|---|---|---|---|---|
| 400 ms | -116.0 | -418.5 | -176.5 | -31.5 |
| 1000 ms | +6.5 | -49.5 | -167.0 | -24.5 |
| 1500 ms | -17.0 | -4.0 | -166.0 | -7.0 |
| 2500 ms | +61.5 | +18.0 | -150.5 | -10.5 |
| 4000 ms | +38.0 | -67.5 | -76.5 | -1.5 |


### Hard-limit violation rate, all windows pooled

The primary metric. A rate, not a mean, so it does not move with the
action space or with how fast the backend answers.

| Arm | Runs over the hard limit | Runs |
|---|---|---|
| A · postgres RC · naive | 45 | 50 |
| A-rc · cockroach RC · naive | 48 | 50 |
| B · cockroach · naive | 47 | 50 |
| C-ops · cockroach · re-reason, stale memory | 0 | 50 |
| C · cockroach · full refresh | 0 | 50 |
