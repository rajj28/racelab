# Methodology log

Every scenario parameter, every change to one, and the reason for it — recorded
at the time the decision is made rather than after results exist. The purpose is
adversarial: if a later reader suspects the scenario was shaped to produce a
favourable gap between arms, this file should let them check, and should already
contain the answer.

Entries are append-only and dated. An entry written before its effect is
measured says so, and the effect is filled in when measured, not replaced.

---

## Pre-registration

Fixed before any experimental run, so the analysis cannot drift toward whatever
looks best.

**Co-primary metric 1 — hard-limit violations.** Runs per 100 where
`SUM(allocations.amount) > accounts.hard_limit` at the end of the run. This is
the structural invariant: it is a property of the database rows, and it is
recoverable by re-reading operational state inside the retried transaction.

**Co-primary metric 2 — policy-ceiling breaches.** Runs per 100 where
`SUM(allocations.amount) > the ceiling currently in force`, which may sit below
the hard limit. This is *not* a softer version of the first metric, and it is
not secondary. The two fail for different reasons and are recovered by different
mechanisms:

| | Where it lives | What recovers it |
|---|---|---|
| hard limit | the `accounts` row | re-reading operational state |
| policy ceiling | a memory, retrieved semantically | refreshing memory |

Promoted to co-primary (2026-08-16) because Entry 7 produced a case — C-ops at
$20 pre-allocated — that passes the first metric and fails the second, by
allocating inside the hard limit but past an authorization that had been lowered
underneath it. Reporting only the first would score that as a success.

**Secondary metric.** Fraction of client-visible serialization failures that are
followed by a *changed* decision — `decision_before != decision_after`. This is
the metric that distinguishes the hypothesis from ordinary retry accounting.


**Explanatory metric.** Decision correction rate: conflicts that would have
produced an invariant-violating final state under the naive policy but did not
under the conflict-aware policy.

**Arms.**

| Arm | Backend | Isolation | Policy |
|---|---|---|---|
| A | PostgreSQL 16 | READ COMMITTED | naive |
| B | CockroachDB | SERIALIZABLE | naive |
| C-ops | CockroachDB | SERIALIZABLE | conflict-aware, operational re-read only (added; see Entry 7) |
| C | CockroachDB | SERIALIZABLE | conflict-aware, full refresh |
| D (optional) | PostgreSQL 16 | SERIALIZABLE | conflict-aware |

**Determinism claim.** Deterministic *workload* — a seed fixes the agent set,
the initial state, and the cached model intents. **Distributional** outcome.
No claim is made that transaction interleaving is reproducible byte-for-byte;
distributed scheduling legitimately varies, and pretending otherwise would be
false.

**Stopping rule.** 100 runs per arm, 20 agents per run, fixed seed set, decided
in advance. Results are reported at that point whatever they show. If arm C does
not beat arm B on the primary metric, the honest numbers get reported as the
finding.

> **Amended — see Entry 13.** This rule was written for a single-condition
> experiment. Entry 7 added a fourth arm and Entry 8 registered a boundary that
> is a claim about a *range* of arrival windows, which cannot be tested at one
> window. The sweep ran 10 runs per arm per window across five windows (50 per
> arm, 200 total) instead of 100 per arm at one. Entry 13 records the trade and
> what the reduced depth costs.
>
> **Narrowed — see Entry 14.** The zero-variance argument holds for **arm C-ops**
> and is stated for arm C-ops only: it ends at exactly `80.0` in all nine cells
> measured across two experiments and both reasoning providers, with no variance
> to resolve. That is the claim this project makes. **Arm C** sits next to a
> decision boundary and does move between samples of the same configuration — we
> ran it twice and published the mismatch rather than the friendlier number.
> Consequence for readers: small differences between adjacent windows in arm C
> are not signal. The 100-run cell remains un-run.

---

## 2026-08-14 — Entry 1: staggered agent arrival replaces the synchronized barrier

**Status: decided before any experimental result exists. Effect not yet
measured.**

### What changes

The Phase 1 gate started all 20 workers from a `threading.Barrier`, so every
transaction issued its `SELECT SUM` at the same instant. For the Phase 4
experiment the barrier is replaced by staggered arrival: agents begin their
transactions spread over a configurable arrival window rather than
simultaneously.

### Why

**Modeling realistic agent arrival — not maximizing conflict rate.**

Agents in a real swarm do not begin reasoning on a shared clock edge. They are
dispatched, they retrieve memory, they call a model, and they arrive at their
transaction at different moments. A synchronized barrier is a synthetic
worst case: it is the right instrument for the gate, where the question was
*"can this signal be produced at all?"* and an adversarial arrangement is
exactly what you want. It is the wrong instrument for the experiment, where the
question is *"what do the two policies do to final-state correctness under a
plausible workload?"*

### The direction this cuts, stated plainly

Staggered arrival will **reduce** the conflict rate, not increase it. The gate
measured 19 client-visible 40001s out of 20 workers under the barrier; spreading
arrival gives earlier transactions time to commit before later ones read, so
fewer transactions will find their refresh spans invalidated.

This makes the experiment **harder** for the hypothesis, not easier. Arm C's
advantage over arm B can only come from conflicts — where there is no conflict,
there is nothing for a conflict-aware policy to do differently, and the two arms
are identical by construction. Reducing the conflict rate reduces the number of
opportunities for arm C to differ from arm B.

That is the honest reason to note the direction now: a change that lowered the
conflict rate would be a strange thing to make in order to manufacture a gap.

### Parameters

| Parameter | Value | Note |
|---|---|---|
| `arrival_window_ms` | to be fixed before the 100-run experiment | Chosen once, from the hero scenario, then frozen across all arms |
| Arrival distribution | uniform over `[0, arrival_window_ms)` | Seeded, so a seed reproduces the same arrival offsets |
| Applies to | Phase 4 experiment only | The gate keeps its barrier; `spike/gate.py` is unchanged and its results stand as reported |

The same arrival offsets are used for every arm at a given seed. Arms must not
differ in their workload — only in backend, isolation, and policy.

### Effect on conflict rate and commit rate — MEASURED 2026-08-14

Measured with the gate's exact transaction shape (fixed amount 40, no
re-decision, no retry) on CockroachDB, 20 workers, `gap_ms=200`, 3 runs per
window. Holding the shape fixed is what makes the windows comparable: the only
thing that changes is when each worker starts.

| `arrival_window_ms` | Commits | Client-visible 40001 | Final SUM | Invariant violated |
|---|---|---|---|---|
| 0 (barrier, as in the gate) | 1, 1, 1 | 19, 19, 19 | 40 | 0/3 |
| 400 | 1, 2, 2 | 19, 18, 18 | 40, 80, 80 | 0/3 |
| 1500 | 3, 4, 5 | 17, 16, 15 | 120, 160, 200 | **3/3** |
| 4000 | 8, 8, 9 | 12, 12, 11 | 320, 320, 360 | **3/3** |

The predicted direction holds: widening the window lowers the conflict rate
monotonically (19 → 18 → 16 → 11.7 mean) and raises the commit rate (1 → 1.7 →
4 → 8.3 mean). Staggering makes the experiment harder for the hypothesis, as
stated above, and it does so measurably.

### An unanticipated finding that changes how the gate result must be read

**SERIALIZABLE does not protect this invariant. It only surfaces the conflict.**

At windows of 1500 ms and above, CockroachDB under SERIALIZABLE violates the
invariant in every run — final sums of 120 to 360 against a hard limit of 100,
with no errors beyond the serialization failures that were correctly surfaced
and, in this shape, simply not acted on.

The gate reported "the invariant was never violated on CockroachDB, at any gap
or worker count." That statement was true of every configuration the gate
measured, and it is **not true in general**. It was an artifact of the
synchronized barrier: contention was so extreme that only one transaction ever
committed, and a single allocation of 40 cannot exceed 100. Nothing about
SERIALIZABLE was preventing the violation. `docs/GATE_RESULTS.md` has been
corrected accordingly.

This is a better foundation for the project than the original reading, not a
worse one. If SERIALIZABLE alone fixed the problem, the interesting question
would be "which database should you use", and the answer would be a one-line
recommendation. It does not fix it. What SERIALIZABLE provides is a
**client-visible signal that the state a decision was based on has changed** —
and the contribution is what the agent does with that signal. An arm that
receives 19 serialization failures and replays its way through all of them ends
up at 320. That is the comparison the experiment exists to make.

---

## 2026-08-14 — Entry 2: two-stage execution with cached model intents

**Status: decided before any experimental result exists.**

### What it is

Model calls are not made inside the 100-run concurrency experiment. Instead:

- **Stage 1, intent generation.** Bedrock is called once per
  `(scenario, agent, seed)` and the resulting intent is cached to disk.
- **Stage 2, concurrency trial.** The 100 runs replay cached intents.

For the conflict-aware policy, post-conflict re-reasoning decisions are also
pre-generated, keyed by `(scenario, agent, observed_sum_bucket)`, so that a
re-read maps deterministically to a cached revised decision.

### Why

The experiment is a measurement of database and protocol behaviour. Making 2000+
live model calls inside it would add a large, uncontrolled source of variance
that has nothing to do with the hypothesis, and would make the run
irreproducible for anyone without the same Bedrock access.

### What this costs, stated plainly

The experiment does **not** measure whether a language model spontaneously
revises its decision when re-prompted. It measures whether the
conflict-aware *protocol* — refresh memory, refresh operational state,
re-reason, re-decide — produces better final states than replaying a
previously computed action. The re-reasoning function is real and is exercised;
its outputs are just computed ahead of time rather than during the race.

A judge is entitled to ask about this, so it is stated in the README rather than
buried here.

---

## 2026-08-14 — Entry 3: the hero scenario uses a mid-run out-of-band policy update

**Status: decided before any experimental result exists.**

### What it is

A single account, with temporal supersession in the memory corpus:

- `t=0` — memory: *"temporary authorization ceiling $80"*, against a hard limit
  of $100.
- `t=X`, mid-run — an out-of-band policy update writes: *"authorization ceiling
  reduced to $60"*, superseding the earlier memory.

### Why this shape

It makes the memory refresh **causally load-bearing**. If the memory corpus were
static, then re-retrieving it after a conflict would return the same memories,
infer the same ceiling, and produce the same action — the "refresh memory" step
of the conflict-aware policy would be a no-op, and the only thing distinguishing
the arms would be the re-read of the operational aggregate.

With supersession, refreshing semantic memory changes the inferred ceiling,
which changes the proposed amount. That is what makes the vector index part of
the mechanism rather than decoration: a judge can delete the vector search and
watch the conflict-aware arm stop working.

### Requirements this places on retrieval

Retrieval must actually surface the newer memory. Ranking is relevance *and*
recency — a purely nearest-neighbour ranking over near-identical policy
sentences could return either one. This is a correctness requirement on the
retrieval layer and is tested directly: the two corpus states must produce
different proposals. If they do not, the hero scenario does not work and that
gets reported rather than papered over.

### Separation of corpora

The hero scenario is one account and exists to be legible in the UI and the
video. The **experiment corpus** is separate: 4–6 accounts with distinct policy
regimes, providing breadth for the 100-run table. Different job, different data,
no sharing of accounts between them.

---

## 2026-08-16 — Entry 4: how the naive arm is constructed, and one change to it

**Status: decided before any experimental result exists.**

### The change

The naive arm was originally specified as "the same wrapper with
`reason=None`". Implementing it that way does not survive the first attempt:
the naive agent still has to *make* a decision before it has one to replay, and
with no reasoning function it has no way to make it.

The obvious patch — let the caller seed naive's first decision separately — is
worse than it looks. It would mean the two arms derive their initial decision
from different code, so a difference in outcome could be attributed to the
initial decision rather than to the response to a conflict. That is a confound
introduced by an implementation detail, which is the worst kind.

So both arms now take the **same required `reason` callable** and use it
identically for attempt 0. A single boolean, `re_reason`, controls whether it is
called again after a serialization failure:

| | attempt 0 | after a 40001 |
|---|---|---|
| `ConflictAware.naive` | calls `reason` | re-reads state, **replays** the action |
| `ConflictAware.conflict_aware` | calls `reason` | refreshes memory, re-reads state, **calls `reason` again** |

`scripts/test_wrapper.py` asserts that exactly one attribute differs between a
naive and a conflict-aware wrapper built from the same arguments, so this
property is checked rather than claimed.

### The naive arm is still not a strawman

It restarts the transaction, and it re-reads the operational aggregate inside
the restarted transaction — which is exactly what standard retry middleware
does, and it is what a competent engineer would write. The measured behaviour on
a forced conflict:

| Policy | Conflicts | `reason` calls | Decision | Final sum (limit 100) |
|---|---|---|---|---|
| naive | 1 | 1 | `allocate(45)` → `allocate(45)` | **110** |
| conflict-aware | 1 | 2 | `allocate(45)` → `allocate(35)` | 100 |

The naive agent read 20, correctly concluded 45 fits, was told its transaction
could not serialize, retried, re-read state — and committed the action it had
computed against the earlier reading. It committed cleanly, with no error, past
the limit. Nothing it did was unreasonable; it answered a question that had been
asked about state which no longer existed.

### A bug this shape of testing caught

The serialization failure is raised by `COMMIT`, after the decision has been
made. In the first implementation the exception unwound past `_attempt`'s return
value, so the naive arm *lost* the decision it was supposed to replay and
re-reasoned instead — silently behaving as conflict-aware, which would have
collapsed the difference between the arms and produced a null result for
reasons having nothing to do with the hypothesis. It was caught by asserting on
the number of calls to the reasoning function rather than on the outcome. The
fix publishes each proposal as soon as it exists rather than only on the success
path.

Recording it here because a null result from an experiment whose two arms are
secretly the same arm is the specific failure this log exists to make
detectable.

---

## 2026-08-16 — Entry 5: the arm-collapse guard

**Status: decided before any experimental result exists.**

### What it is

The reason-call counting described in Entry 4 is promoted from a test assertion
to a **runtime invariant checked on every experimental run**, in
`racelab/conflict.py`. The wrapper counts calls to the reasoning function
itself — not the caller — and validates them before returning:

| Policy | Invariant |
|---|---|
| naive | `reason_calls == 1`, however many attempts it made |
| conflict-aware | `reason_calls == attempts_made` |

`check_arms(naive, aware)` adds the pairwise check: where any conflict
occurred, the conflict-aware arm must have reasoned strictly more than the
naive arm. A violation raises `ArmCollapse`, and the harness **voids the run
rather than reporting it**.

### Why an outcome assertion is not enough

This is the important part, and it is why the guard is a runtime invariant
rather than a test.

When the arms collapse, nothing about the output looks wrong. Both arms commit.
Both produce final sums in a plausible range. Both write well-formed telemetry
with sensible `decision_before` / `decision_after` values. The experiment
reports "no significant difference between naive and conflict-aware" — a clean,
publishable null result that is entirely false, produced by measuring one arm
twice.

A null result is a legitimate outcome here; the pre-registration commits to
reporting one if that is what the data shows. That is exactly what makes this
failure mode dangerous: **a collapsed experiment is indistinguishable from an
honest refutation of the hypothesis**, and it fails in the direction that looks
like integrity. The only signal that separates them is whether the mechanism
ran, so the mechanism is what gets instrumented.

`scripts/test_wrapper.py` checks the guard against a fabricated collapsed run
and asserts two things: that the guard rejects it, and that the same run passes
every outcome-based check — committed, revised, plausible action.

---

## 2026-08-16 — Entry 6: retrieval ranking, measured under Titan

**Status: measured. This entry changed the retrieval implementation, and the
change is described here with the number that motivated it.**

### The question

Entry 3 placed a requirement on retrieval: it must surface the newer `$60`
policy above the older `$80` one, when the two sentences differ by one number.
That requirement was stated before the corpus was embedded with a real model,
with the note that if it failed it would be reported rather than papered over.

### The measurement

Titan V2, `amazon.titan-embed-text-v2:0`, 1024 dimensions, cosine:

| Memory | Status | Cosine distance to the query |
|---|---|---|
| `hero-m1` "Temporary authorization ceiling for this account is $80…" | **superseded** | **0.4032** |
| `hero-m5` "Authorization ceiling reduced to $60 per billing cycle…" | current | 0.5806 |

**Relevance ranks the stale policy 0.1774 closer to the query than the policy
that replaced it.** The requirement in Entry 3 is not met by relevance, and it
is not met by relevance plus the recency weight either: that weight was 0.15 and
would have had to exceed 0.1774.

So the honest answer to "does ranking surface the newer ceiling" is **no, not on
semantic similarity**. Retrieval was returning the right answer only because of
the explicit `supersedes` edge in the corpus.

### Why this is systematic rather than a bad corpus

The query asks what the policy **is**. A standing policy is phrased as a
statement of fact — "the ceiling for this account is $80" — and matches that
question closely. A supersession event is phrased as a change — "reduced to
$60, effective immediately" — and reads less like an answer to "what is the
policy" than the sentence it overrules.

An embedding model will therefore tend to prefer the stale statement *for the
same reason it is stale*. This is a property of how policy changes are written,
not of this corpus, and any agent memory system doing similarity search over
policy history has it.

### The change, and the change that was rejected

Raising `recency_weight` from 0.15 to 0.20 fixes this pair. It was **not**
adopted. 0.20 was chosen after seeing which value worked, which is fitting a
parameter to the test, and a corpus whose gap happened to be 0.25 would break it
silently. Recording the rejected option because the tempting fix is the one a
reader should be able to check was considered.

What was adopted instead: **policy currency is lexicographic, not weighted.**
Among retrieved memories of kind `policy`, the newest is surfaced first
regardless of distance. This is defensible without reference to any measurement
— for a statement of policy, "current" is not a tiebreaker against "similar",
it is the entire question — and it depends on no threshold.

Relevance still decides which memories become candidates at all, which is what
the vector index accelerates. Currency only decides which of the retrieved
policies is authoritative.

### Effect

| Corpus condition | Before | After |
|---|---|---|
| `supersedes` edge present | $60 ✓ | $60 ✓ |
| `supersedes` edge absent | **$80 ✗** | $60 ✓ |

The second row is the one that matters. Retrieval previously depended on
someone having written an explicit supersession link; a real corpus frequently
will not have one. The system now gets the current policy either way, and the
dependency is no longer invisible.

### What this does not fix

If two policy memories carry the same timestamp, or if a policy is superseded by
a memory not classified as `policy`, currency has nothing to order. Those cases
are not handled and are not present in either corpus.

---

## 2026-08-16 — Entry 7: a fourth arm, decomposing our own claim

**Status: arm added before the 100-run experiment. A deterministic
single-conflict result is recorded below; the swept result is not yet run.**

### The arm

| Arm | Backend | Isolation | On a conflict |
|---|---|---|---|
| A | PostgreSQL 16 | READ COMMITTED | conflict not surfaced; replay |
| B | CockroachDB | SERIALIZABLE | re-read operational state, replay the action |
| **C-ops** | CockroachDB | SERIALIZABLE | re-read operational state, **re-reason**, memory **not** refreshed |
| C | CockroachDB | SERIALIZABLE | refresh memory **and** operational state, re-reason |

### Why

The conflict-aware policy does two things at once — re-reads operational state
and refreshes semantic memory. C against B alone would show that the pair helps
while saying nothing about which one did the work, and would leave "the vector
index is load-bearing" resting on an argument rather than a measurement.

C-ops isolates it. **C-ops − B** is the contribution of re-reasoning over fresh
operational state; **C − C-ops** is the contribution of refreshing semantic
memory. C-ops differs from C in exactly one injected setting, and
`ConflictAware` raises `ArmCollapse` if a C-ops run ever refreshes memory, for
the same reason the naive arm's reason-calls are counted.

Both outcomes were declared reportable in advance: if C-ops already holds, the
memory refresh is redundant in this scenario and we say so.

### Deterministic result, ahead of the sweep

Single forced conflict on the hero account. Hard limit $100; policy ceiling $80,
dropping to $60 mid-decision; agent-2 commits $45 in the window.

| Pre-allocated | Arm | Final sum | Hard limit | Policy ceiling |
|---|---|---|---|---|
| $0 | B | 90 | held | **breached** |
| $0 | C-ops | 80 | held | **breached** |
| $0 | C | 45 | held | held |
| $20 | B | 110 | **violated** | **breached** |
| $20 | C-ops | 65 | held | **breached** |
| $20 | C | 65 | held | **breached** |

Decomposition of the final sum:

| Contribution | $0 pre-allocated | $20 pre-allocated |
|---|---|---|
| isolation surfaces the conflict (B − A) | 0 | 0 |
| re-reasoning over fresh operational state (C-ops − B) | −10 | −45 |
| refreshing semantic memory (C − C-ops) | **−35** | **0** |

### What this says, including the part that is inconvenient

**The value of refreshing semantic memory is conditional, and we can say exactly
on what.** It can only change an outcome where the old and new ceilings disagree
about the reading the agent lands on after the conflict. At $20 the agent
re-reads $65, which both an $80 and a $60 ceiling refuse, so the memory refresh
provably cannot matter and contributes zero. At $0 the agent re-reads $45, which
an $80 ceiling permits and a $60 ceiling does not, and the refresh is worth $35.

Both configurations are run and both are reported. Running only the second would
have shown a null effect for the memory refresh; running only the first would
have overstated it. Neither alone is the truth.

### A metric this exposed

C-ops at $20 stays inside the hard limit and still allocates to $65 against a
policy ceiling of $60. The invariant column reports that as a success. It is
not one: the agent allocated past an authorization that had been lowered
underneath it, and the only reason the database is content is that the hard
limit is a separate and looser constraint.

So **policy adherence is now measured and reported alongside invariant
violations**, as a distinct column. An agent reasoning over stale memory tends
to fail this one while passing the other, which is precisely the failure a
final-sum comparison hides.

### Scope of this entry

These are single-conflict deterministic numbers, chosen because they reproduce
exactly rather than depending on a lucky interleaving. They establish the
mechanism and its decomposition. They are **not** the experimental result: the
statistical claim comes from the swept 100-run experiment across both action
spaces and the full arrival-window range, which has not been run.

---

## 2026-08-16 — Entry 8: pre-registered prediction for the ablation boundary

**Status: written BEFORE the sweep is run. The sweep is reported against this
entry, whatever it shows.**

This entry exists because a conditional effect with a *derivable* boundary is
stronger evidence than an unconditional one. An unconditional effect is
consistent with almost any mechanism, including a confound. An effect that
appears exactly where theory says it must and vanishes exactly where theory says
it cannot is hard to produce by accident. The conditionality is the evidence,
not a caveat on it.

### The prediction

Let `S` be the operational sum an agent reads *after* a conflict, `H = 80` the
stale ceiling in memory, `N = 60` the current ceiling, and

    f(r) = the largest of {45, 40, 35} that is <= r, otherwise abstain

C-ops decides `f(H − S)`; C decides `f(N − S)`. Refreshing memory changes the
outcome **iff those differ**:

| `S` | `f(80 − S)` | `f(60 − S)` | Memory refresh |
|---|---|---|---|
| 0 – 15 | 45 | 45 | **cannot** contribute |
| 20 | 45 | 40 | contributes |
| 25 – 45 | 45 / 40 / 35 | abstain | contributes |
| ≥ 50 | abstain | abstain | **cannot** contribute |

**Predicted band: `20 ≤ S ≤ 45`.** Continuously, `S ∈ (N − 45, H − 35]`.

Outside it the prediction is not "small effect", it is **exactly zero**, because
both ceilings map to the same action and no difference is representable.

### A correction to the interval this was going to be registered with

The boundary was first proposed as `60 < S ≤ 80` — between the new and old
ceilings. That is wrong, and the deterministic results already in Entry 7
falsify it: `S = 45` sits outside that interval and produced the **largest**
observed effect (−$35), while `S = 65` sits inside it and produced **zero**.

The error is treating the ceilings as the thresholds. They are not. The
thresholds are where the *remaining budget* crosses a step of the bounded action
space, which is shifted down from each ceiling by the option sizes. Recording
this because a pre-registration that quietly fixed its own prediction after
seeing which one matched would be worthless.

### Predicted shape across the arrival-window sweep

The arrival window controls how many transactions commit before a given agent
re-reads, and therefore the distribution of `S`. So the memory-refresh
contribution should be **non-monotonic and peaked**:

| Arrival window | Typical `S` | Predicted memory-refresh contribution |
|---|---|---|
| tight (0–400 ms) | near 0 — extreme contention, almost nothing commits | ≈ 0, `S` below the band |
| middle (1000–2000 ms) | spreads into 20–45 | **maximum** |
| wide (4000 ms+) | routinely > 45 | falls back toward 0, `S` above the band |

The two zeros have different causes and should not be reported as one result:
at tight windows nothing has committed yet so both ceilings permit the same
allocation; at wide windows so much has committed that both ceilings refuse it.

### A prediction that is not about the ablation

At wide windows, the C-vs-B gap should stay large even as the C-vs-C-ops gap
closes. B keeps replaying allocations into a total that has already moved; both
C arms abstain. So "memory refresh stops mattering" must not be read as
"conflict-awareness stops mattering" — they are different comparisons and the
sweep reports them separately.

### What would falsify this

- A non-zero memory-refresh contribution at any window where the observed `S`
  values lie wholly outside `[20, 45]`. That would mean something other than the
  stated mechanism is producing the difference, and the decomposition would not
  be trustworthy.
- A monotonic contribution curve. That would suggest the effect tracks conflict
  count rather than the reading distribution, which is a different claim from
  the one made here.

Observed `S` values are logged per run so both checks are possible after the
fact rather than argued.

---

## 2026-08-16 — Entry 9: the model arm is a spot check, by design

**Status: decided before the model arm was run, while Bedrock access to the
Anthropic model was still pending. Recorded now so it cannot look like a
rationalisation of whatever the model turns out to do.**

### The decision

The swept experiment uses the deterministic reference reasoner. The model arm —
Claude on Bedrock — runs at **two window values only**: one where the
pre-registered band predicts the memory refresh matters, one where it predicts
it cannot. Reference and model are reported side by side at those matched
points.

The full sweep is **not** re-run through the model.

### Why this is a choice and not a shortcut

The hypothesis is about a protocol: on a serialization failure, refresh memory,
re-read operational state, re-reason, re-decide. It is not about whether a
particular language model is good at subtraction. Pushing every cell of the
sweep through a model would add generation variance to the primary result while
answering a question nobody asked.

What the model arm needs to establish is narrower and more useful: that the
protocol survives contact with a real LLM — that a model given refreshed memory
actually does change its decision, and that the wrapper's plumbing works when
the reasoning step is a network call to a model rather than a function. Two
clean matched comparisons answer that better than a noisy sweep.

Splitting it this way also keeps the two claims separable. "The protocol reduces
policy breaches" and "an LLM will follow the protocol" are different statements
with different evidence, and a single blended number would let a reader mistake
one for the other.

### Committed in advance

- **Divergence is a finding, not noise.** If the model and the reference reach
  different decisions at the matched points, that is reported as a result. It is
  not averaged away, and the matched points are not moved.
- **The band is not re-fitted to the model.** The `20 ≤ S ≤ 45` boundary is a
  property of the ceilings and the action space, not of the reasoner. If the
  model's behaviour does not respect it, the correct conclusion is that the
  model is not applying the ceilings as the reference does — which is itself
  worth reporting — not that the boundary was wrong.
- **Provenance is enforced, not documented.** `IntentCache.require_provider`
  raises rather than letting a reference-built cache be reported as a model
  result.

---

## 2026-08-17 — Entry 10: the first sweep measured its own connection latency, not the arrival window

The sweep in Entry 8 reported a memory-refresh effect that peaked at a 1000 ms
arrival window and vanished at the tails, which is the non-monotonic shape that
had been registered. The shape was right and the reason given for it was wrong,
so the result did not survive being checked.

### What was checked, and why

The registered explanation for the near-zero effect at tight windows was that
post-conflict operational readings would fall *below* the `20 ≤ S ≤ 45` band.
They did not. At the 400 ms window, 126 of 129 re-decision readings were inside
the band and the effect was still exactly `+0.0`. Being in the band is a
necessary condition, not a sufficient one, so this did not contradict the
prediction as literally written — but it did mean the mechanism producing the
zero was not the mechanism that had been predicted, and an unexamined agreement
between a prediction and a number is worth less than a disagreement that gets
chased down.

### What was actually happening

Instrumenting the ceiling each agent inferred showed that in the C-ops arm —
the arm defined by reasoning over **stale** memory — 100% of decisions at both
400 ms and 1000 ms inferred the *fresh* `$60` ceiling. The arm was never stale.

Two candidate explanations were measured and both were eliminated:

- **Arrival timing.** Rejected on the sweep's own numbers: at a 1000 ms window
  the update lands at 500 ms, so an agent arriving at 50 ms should read `$80`.
- **Retrieval cost.** Rejected by measurement: a warm retrieval is 1.1 ms of
  embedding (memoised) plus ~86 ms of vector query, leaving roughly 30% of even
  a 400 ms window nominally able to hold stale memory.

The cause was a fixed startup cost that had never been on the ledger. Each
agent opens its own connection to CockroachDB Cloud before `ConflictAware.run`
begins, and that TLS handshake costs **391 ms** (median of 8 samples, range
337–429 ms). Retrieval happens at the top of `run()`, so the floor before any
agent can read memory is ~477 ms. The observed first retrieval was 530 ms.

Against a policy update scheduled at `arrival_window_ms × 0.5`:

| window | update written | earliest retrieval | stale retrievals |
| ------ | -------------- | ------------------ | ---------------- |
| 400    | 299 ms         | 530 ms             | 0 / 20           |
| 1000   | 612 ms         | 669 ms             | 0 / 20           |
| 2500   | 2142 ms        | 2724 ms            | 0 / 20           |

At every window swept, the superseding memory was already committed before the
*earliest* agent retrieved. The staleness condition the C-vs-C-ops ablation
exists to vary was never established.

### What this invalidates, and what it does not

Invalidated: the C-vs-C-ops gap, the memory-refresh contribution, and the
boundary sweep of Entry 8. The `−28.0` effects at 1000 ms and 1500 ms are
run-to-run jitter in a ~400 ms race between connect cost and the updater, not a
response to the arrival window. Reported as such rather than kept.

Not invalidated, because none of it depends on memory staleness:

- **A vs B** — isolation surfacing the conflict at all. Purely operational.
- **B vs C-ops** — re-reasoning over refreshed operational state holding the
  hard limit. Purely operational; this is the result C-ops was added to isolate
  and it stands.
- **The retrieval-ranking finding** — measured offline against the corpus, with
  no arrival window involved.

### The fix, and what is deliberately *not* being changed

The connect handshake is not part of an agent's decision. A real agent in a
swarm holds a connection or leases one from a pool; paying a TLS handshake
inside the measured window is an artifact of the harness, not a property of the
workload. So per-agent connections are opened before the threads are released,
which takes ~391 ms of unmodelled jitter off the critical path and lets the
seeded arrival offsets actually control when each agent retrieves. The same
argument applies to the updater: the embedding for the superseding policy text
is computed before the run starts, so the updater's scheduled time is when the
write lands rather than when the write begins.

Racing connections remain strictly per-agent. Pre-opening them changes *when*
they are created, not whether they are shared; a pooled racing connection would
alter the refresh span and is still forbidden (see `ConnectionPool`).

**The pre-registration is not touched.** The `20 ≤ S ≤ 45` band and the
non-monotonic shape prediction stand exactly as written in Entry 8, and the
corrected sweep is reported against them. Fixing a broken instrument and then
re-running a prediction is legitimate; editing the prediction because the first
run of a broken instrument happened to agree with it is not.

---

## 2026-08-17 — Entry 11: the boundary held; the shape prediction was falsified

The corrected sweep (10 runs/arm/window, 20 agents, 23.2 min,
`results/sweep_fixed.json`) graded both pre-registered predictions from Entry 8.
One survived and one did not.

### The boundary held, in both directions

| Window | re-decision reads | in band `[20, 45]` | median | memory-refresh effect |
| ------ | ----------------- | ------------------ | ------ | --------------------- |
| 400 ms  | 131 | 131 (100%) | 45 | **−35.0** |
| 1000 ms |  98 |  70 (71%)  | 45 | **−24.5** |
| 1500 ms |  82 |  16 (20%)  | 80 | +0.0 |
| 2500 ms |  55 |   9 (16%)  | 80 | +0.0 |
| 4000 ms |  48 |  12 (25%)  | 80 | +0.0 |

The effect is non-zero at exactly the two windows where readings are
predominantly in band, and exactly zero at the three where the median reading is
`80` — above the band. The verdict check now tests both directions, so the cell
that exposed Entry 10 (high in-band share with zero effect) would be flagged
rather than passed. It does not fire.

### The shape prediction was falsified

Observed effects, tight to wide: `−35.0, −24.5, +0.0, +0.0, +0.0`. Monotonically
decreasing. The registered prediction was *peaked* — near zero at tight windows,
maximal in the middle. **The effect is largest at the tightest window.**

Recorded as a failed sub-prediction and not adjusted. The high-end tail was
predicted correctly: the effect does vanish as readings reach the old ceiling.
The low-end tail was wrong, and it was wrong for the same reason Entry 10 was —
the prediction assumed tight-window readings would fall *below* the band, and
they sit at `45`, the top of it. Tight windows produce more conflicts, therefore
more re-decisions, therefore readings concentrated inside the band, therefore
the maximum effect. The registered reasoning had the mechanism inverted.

The boundary claim does not depend on the shape claim. The boundary says *where*
memory refresh can matter; the shape was a guess about how conflict density
distributes readings across that interval, and the guess was wrong.

### A scope limit the sweep exposed: retrospective breaches

Arm C also breaches the policy ceiling 10/10 at windows ≥ 1500 ms, and no
refinement of the protocol would prevent it. At a wide window the sum reaches
`$80` before the update exists: one agent commits 45, a second reads a remaining
35 under the then-current `$80` and commits it, both uncontested and both
correct at the time. The ceiling is then lowered *underneath already-committed
state*.

Re-reasoning cannot revoke a commit that was valid when it happened. This is a
limit on what the contribution claims, and it is a sharper statement of it: the
protocol protects decisions that are *in flight* when state changes. It is not a
mechanism for retroactive compliance, and a system that needs the latter needs
compensation or reversal, which is a different problem.

This is also the honest explanation for the effect vanishing at wide windows,
and it is better than the registered one ("readings above the band"). Readings
above the band are the symptom; uncontested early commits under the old ceiling
are the cause.

### One result that cannot be labelled an improvement

The decomposition reports `B − A` as **positive** at every window (+33.5 to
+105.0): CockroachDB with naive retry has a *worse* mean final sum than
PostgreSQL READ COMMITTED with naive retry.

This is not READ COMMITTED performing better. Arm A violates the hard limit
8–10 times out of 10 with **zero conflicts** — it commits the violating
execution silently, and the application is never told. Arm B is told, up to five
times per agent, and replays the same decision each time; retry middleware
amplifies the overshoot precisely because the signal arrives and is discarded.

Stated carefully, because it is easy to misreport in either direction:
surfacing a serialization failure to a client that ignores it produced a worse
final state here than never surfacing it. That strengthens the thesis — the
signal is worthless without the reinterpretation — but `B − A` must not be
presented as a benefit of SERIALIZABLE. The benefit appears at `C-ops − B`,
which is −109 to −175 across the sweep.

---

## 2026-08-17 — Entry 12: reconciling Entry 1's 0/3 against arm B's 10/10

Two of our own tables disagreed about the same workload. Entry 1, on CockroachDB
at a 400 ms window with 20 workers, reported **0/3 hard-limit violations** with
final sums of 40–80. The swept arm B, at the same backend, window and worker
count, reported **10/10 violations** with a mean final sum of 229.5.

`scripts/reconcile_gate.py` varies six factors one at a time from the gate
configuration toward arm B. Output in `results/reconcile_gate.txt`.

| # | configuration | mean | violated |
| - | ------------- | ---- | -------- |
| 1 | gate: one attempt, fixed $40, no abstain | 80.0 | 0/3 |
| 2 | + retry up to 5 attempts | 240.0 | 3/3 |
| 3 | + memory-driven amount | 231.7 | 3/3 |
| 4 | + abstention | 80.0 | 0/3 |
| 5 | + decision carried across restarts (= arm B naive) | 230.0 | 3/3 |
| 6 | + pre-opened connections (= arm B as swept) | 226.7 | 3/3 |

Configuration 6 reproduces the swept arm B (226.7 against 229.5), so the
disagreement is fully accounted for by configuration and not by an error in
either measurement.

### Why no "dominant factor" is named

The script originally printed one. It no longer does. These deltas are steps
along a single arbitrary path through a six-dimensional space, they are not
independent, and the largest single step — adding retry to a worker that cannot
abstain — is an artifact of the ordering rather than a fact about the workload.
Naming it would have been a stronger claim than the measurement supports.

### The contrast that is clean

Configurations 4 and 5 differ in exactly one thing:

| | mean | violated |
| - | ---- | -------- |
| re-decides on every attempt | 80.0 | 0/3 |
| carries the first decision  | 230.0 | 3/3 |

Both retry. Both re-read the sum inside the new transaction. Both may abstain.
The only difference is whether the new reading is permitted to change the
action — and that difference is the entire gap between holding the invariant and
violating it in every repeat. This reproduces the project's central claim in a
standalone script with no part of the `racelab` package involved, which is worth
more than the same claim measured by the library that embodies it.

### What Entry 1's 0/3 actually was

Not evidence of safety. The gate made one attempt and recorded the conflict; a
transaction that abandons its work on a serialization failure cannot violate
anything, and 54 of its 60 workers exhausted without committing. The 0/3 is the
signature of giving up. Entry 1's question was whether a client-visible 40001
could be produced at all, and for that question abandoning the work is fine.
Arm B answers a different question and must not give up, which is the point of
it as a baseline.

### A correction inside this entry

The first version of this script omitted the carry-over factor entirely: its
worker recomputed the amount from the fresh reading on every attempt. That made
its final configuration a reconstruction of **C-ops**, not of B — so it
"reconciled" the gate against the wrong arm and reported a total change of
`+0.0`, i.e. that nothing distinguished the two measurements. It was caught
because reproducing arm B was an explicit success criterion with a number
attached, rather than a narrative that could accommodate any result.

The connection pre-open (factor 6) moved the mean by only −3.3 here, which is
consistent with Entry 10 rather than in tension with it: connect cost broke the
*memory staleness* condition, and this script has no memory in it. The same
artifact is harmless for a purely operational invariant and fatal for a
memory-dependent one.

---

## 2026-08-17 — Entry 13: the run count changed, and why

The pre-registered stopping rule says **100 runs per arm**. The sweep that is
reported ran **10 runs per arm per window** — five windows, four arms, so 50 runs
per arm and 200 runs in total. That is half the registered depth per arm, and it
is a deliberate design change rather than an early stop, so it is recorded here
with its reasoning.

### What changed between the pre-registration and the sweep

The stopping rule was written for a **single-condition** experiment: one arrival
window, three arms. Two later decisions changed the shape of the thing being
measured:

- **Entry 7 added a fourth arm** (C-ops), to decompose our own claim rather than
  merely confirm it.
- **Entry 8 pre-registered a boundary that is a claim about a *range* of arrival
  windows** — that memory refresh contributes only where the post-conflict
  reading falls in `[20, 45]`, and cannot contribute outside it.

A boundary prediction cannot be tested at one window. It is a statement about
where an effect appears and where it must vanish, so it needs the sweep. That
turned one cell into twenty.

### Why breadth was chosen over depth

At 100 runs per arm per window the sweep is 2,000 runs. The corrected sweep of
200 runs took 23.2 minutes, so 2,000 runs is roughly four hours, on a Cloud Basic
cluster whose connection limit already forced the memory pool in Entry 10. That
cost buys tighter confidence intervals on quantities that turn out to have almost
no variance to resolve:

- **C-ops has a mean final sum of exactly `80.0` in all five cells.** Not
  approximately — every one of its 50 runs ended at exactly 80, because the
  action space and the remembered ceiling make the outcome deterministic (45 then
  35, then abstain).
- **C has a mean of exactly `45.0` at the 400 ms window**, all ten runs identical.
- Where C does vary, it varies discretely and legibly: its `55.5` at 1000 ms is
  seven runs at 45 and three at 80, and those three runs are exactly the three
  policy breaches.

The arms carrying the ablation claim therefore have **zero within-cell variance**
in four of five cells. Adding runs to a cell whose ten samples are identical
buys nothing. For arms A and B the outcomes do vary, but the effect sizes are
100+ units of mean final sum against a hard limit of 100, and the violation
counts are 8–10 out of 10 — not quantities that 10 runs leaves in doubt.

So the trade was made in favour of testing the pre-registered boundary across
five windows rather than reporting one window to a precision the data does not
need.

### What this costs, stated plainly

- **Per-cell confidence intervals are wider than the pre-registration promised**,
  and for arms A and B they are wide enough that small differences between
  adjacent windows should not be read as signal. The A-arm violation counts of
  9, 10, 10, 9, 8 across windows are consistent with a flat rate; they are not
  evidence of a downward trend, and are not reported as one.
- **The registered 100-run depth is still available and was not run.**
  `python scripts/run_sweep.py --runs 100 --windows 1500` produces the
  single-cell version of the original stopping rule. It is not required for any
  claim currently made, and it is named here so that its absence is a stated gap
  rather than a silent one.
- **This entry is not a licence to stop early elsewhere.** The change is to the
  experimental design (one condition becomes twenty), not to the rule that
  results are reported at the planned point whatever they show. Nothing was
  stopped once results started arriving; the sweep ran every cell it was
  configured for, and the falsified shape prediction in Entry 11 is reported
  because of that.

---

## 2026-08-17 — Entry 14: the model arm, and two findings about the model

Bedrock access to `us.anthropic.claude-sonnet-4-5-20250929-v1:0` cleared, so the
model arm ran as pre-committed in Entry 9: **two matched arrival windows, not a
re-sweep.** 400 ms is inside the pre-registered `20 <= S <= 45` band, 2500 ms is
outside it. Arms C-ops and C only — the pair whose difference is the memory
refresh.

Arms A and B were deliberately not re-run with the model, for a reason rather
than for convenience: naive replay never calls the reasoning step a second time,
so there is no re-reasoning behaviour for a model to exhibit there, and the sums
those arms reach exceed the readings stage 1 enumerated. Running them would have
required either ~300 further model calls or a fallback, and a fallback inside a
result labelled "model" is precisely what `require_provider` exists to prevent.

### Result: the protocol survives contact with a real LLM

10 runs per arm per window per provider, 20 agents per run, same seeds.

| Window | Arm | Provider | Hard limit | Policy ceiling | Mean sum |
| ------ | --- | -------- | ---------- | -------------- | -------- |
| 400 ms  | C-ops | reference | 0/10 | 10/10 | 80.0 |
| 400 ms  | C-ops | **model** | 0/10 | 10/10 | **80.0** |
| 400 ms  | C     | reference | 0/10 | 1/10  | 48.5 |
| 400 ms  | C     | **model** | 0/10 | 1/10  | **48.5** |
| 2500 ms | C-ops | reference | 0/10 | 10/10 | 80.0 |
| 2500 ms | C-ops | **model** | 0/10 | 10/10 | **80.0** |
| 2500 ms | C     | reference | 0/10 | 9/10  | 76.5 |
| 2500 ms | C     | **model** | 0/10 | 7/10  | 69.5 |

Memory-refresh effect (C minus C-ops): 400 ms — reference `−31.5`, model `−31.5`.
2500 ms — reference `−3.5`, model `−10.5`. Same sign at both points, identical
inside the band.

**The load-bearing framing result reproduces exactly.** C-ops ends at a mean final
sum of exactly `80.0` in every cell of both providers, holding the hard limit
0/10 while breaching the policy ceiling 10/10. Across the full sweep and this arm
that is now **nine independent cells, all exactly 80.0**. An agent that re-reads
operational state perfectly still lands on the ceiling that was withdrawn,
whether the reasoning is a function or Claude.

What this arm does not establish: the effect did not reach zero outside the band
here (`−3.5` and `−10.5` against the sweep's `+0.0`), so it neither confirms nor
contradicts the boundary. The boundary is graded on the full sweep.

### Finding 1: the tool schema's `enum` is not enforced

The decision is taken through a Bedrock `toolConfig` with `toolChoice` forcing the
tool, and `action` is declared as an `enum` of four values. `toolChoice` reliably
forced a tool call. **The `enum` did not constrain the output.** Asked to allocate
against a $60 ceiling with $30 already spent, the model returned
`allocate(30)` — the exact remaining headroom, and not one of the four permitted
values.

This is worth stating because a reader may reasonably assume a JSON-schema `enum`
in a tool definition is validated server-side. It is not. Validation lives in
`scenario/agent.py`, out-of-space answers are re-asked with the violation named,
and the repair count is recorded on every `Decision` — a rate of zero is a claim
to be checked, not assumed. One of 60 readings needed a repair, and the re-ask
produced the correct `abstain`.

### Finding 2: the chosen action contradicted the model's own rationale

More interesting than the format failure. In three of 60 readings the model chose
an action that breaches the ceiling **it had just reported inferring**, and in two
of those the rationale field states the violation explicitly:

| reading | inferred ceiling | action | rationale (verbatim) |
| ------- | ---------------- | ------ | -------------------- |
| $35 spent | 60 | `allocate(45)` | "allocating $45 would bring the total to $80, **which exceeds this ceiling**" |
| $40 spent | 60 | `allocate(40)` | "with $40 already allocated, I can allocate **up to $20 more** to reach that ceiling" |
| $50 spent | 80 | `allocate(35)` | (total $85, over its own $80) |

The reasoning is correct and the selected action does not follow from it. All
three sit in the same structural position: headroom is positive but smaller than
the smallest available allocation, where the correct answer is `abstain`. Where
headroom is $15 or less the model abstains correctly every time.

Overall agreement with the reference reasoner is **57/60 (95%)**, measured by
`scripts/compare_intents.py`.

**None of these three are corrected anywhere in the pipeline.** Policy-ceiling
breaches are the dependent variable of this experiment. A harness that fixed them
would be performing the reasoning the experiment claims to measure and reporting
its own competence as the model's. The hard limit is different — structural, a
column in the database, and it binds regardless, which `agent.py` records in the
rationale rather than hiding.

This also retrospectively justifies the no-silent-fallback decision. Had a
malformed response quietly fallen through to `decide.py`, we would have reported
a clean model result while the model was emitting self-contradictory decisions,
with nothing in the output to show it.

### Finding 3: arm C disagreed with itself across two samples, and we published it

The model arm re-ran the **reference** provider at identical seeds, agent count,
gap and windows. It did not reproduce the sweep's numbers for arm C:

| cell | sweep | model-arm reference |
| ---- | ----- | ------------------- |
| C @ 400 ms  | 0/10 breaches, mean 45.0 | 1/10 breaches, mean 48.5 |
| C @ 2500 ms | 10/10 breaches, mean 80.0 | 9/10 breaches, mean 76.5 |

Two independent 10-run samples of the same configuration disagree by one run out
of ten in each cell. This is recorded here because we measured the same thing
twice and are publishing the mismatch rather than the friendlier number; a reader
who only saw the sweep would have no way to know the second sample existed.

**Why it is confined to arm C.** Arm C sits directly against a decision boundary.
Under the fresh `$60` ceiling the first commit of `45` leaves `$15` of headroom,
which is below the smallest allocation, so every subsequent agent abstains and the
run ends at 45. But an agent that commits *before* the ceiling drops can take the
total to 80 instead, and whether any given agent lands on the early or late side
of a 200 ms policy update is exactly the kind of thing distributed scheduling
decides. One run in ten crossing that line moves the cell.

**Arm C-ops has no such boundary to sit against, and no variance.** Reasoning over
the remembered `$80` ceiling, `45 + 35 = 80` exactly exhausts the headroom
regardless of arrival order, so the run ends at 80 whatever the interleaving. It
did so in **all nine cells measured** — five in the sweep, four in the model arm —
across both the reference reasoner and Claude. That is the claim this project
makes, and it is the one the framing rests on.

Consequence stated plainly for anyone reading the tables: **small differences
between adjacent windows in arm C are not signal.** Its per-cell breach counts
carry roughly one run of sampling noise, so a change from 9/10 to 10/10 between
neighbouring windows means nothing on its own. The C-vs-C-ops direction and the
C-vs-B magnitudes are large enough to survive that; individual arm C cells are
not. The 100-run cell named in Entry 13 remains un-run, and this finding
strengthens the case for eventually running it.

The pre-registration's determinism claim already covered this — deterministic
*workload*, distributional *outcome*, no claim that interleaving is reproducible
byte-for-byte. What is new is the measurement of how much that costs, and where.
