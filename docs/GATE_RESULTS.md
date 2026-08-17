# Phase 1 gate results

The gate exists to establish one thing before any other code is written: that a
serialization failure from a concurrent agent workload actually reaches our
process as a client-visible error. If it does not, the project has nothing to
build on.

No LLM, no vector search, no framework. Two (or twenty) threads, one shared
authorization budget, and the minimal transaction shape of the real scenario.

**Status: both pass conditions PASSED.**

| | Backend | Isolation | N=20 outcome |
|---|---|---|---|
| Pass 1 | PostgreSQL 16.12 | READ COMMITTED | 20 commits, 0 errors, SUM 800 vs limit 100 — **invariant violated silently, 5/5 runs** |
| Pass 2 | CockroachDB 26.2.5 | SERIALIZABLE | 1 commit, **19 client-visible 40001**, SUM 40 — invariant held, 5/5 runs |

Same code, same transaction shape, same barrier, same workload. The only
variable is the backend's default isolation.

---

## The exact transaction shape

Every worker ran precisely this, one statement per round trip:

```
BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE
SHOW transaction_isolation                                    -- recorded as evidence
SELECT COALESCE(SUM(amount), 0) FROM gate_allocations WHERE account_id = $1
<client-side sleep: reasoning_gap_ms>
INSERT INTO gate_allocations (allocation_id, account_id, agent_id, amount, run_id)
     VALUES ($1, $2, $3, $4, $5)                              -- a DIFFERENT row per worker
COMMIT
```

Four properties of this shape are load-bearing, and each was verified rather
than assumed:

| Property | Why it matters | How it was ensured |
|---|---|---|
| The aggregate read is **inside** the transaction | That read is what places other workers' rows into the transaction's refresh span. Without it CockroachDB detects nothing and there is no experiment. | The `SELECT SUM` sits between `BEGIN` and `INSERT`. |
| Each worker inserts **its own row** | Makes this write skew across rows rather than row-lock contention on a shared counter. | Distinct `allocation_id` per worker; no `UPDATE` anywhere. |
| Statements are **not batched** | A single batched round trip would let the cluster resolve the conflict internally, and nothing would surface. | `psycopg` connections run with `autocommit=True` and explicit `BEGIN`/`COMMIT` statements, so the client owns the transaction boundaries outright. One `execute()` per statement. |
| The read's results **reach the client** before the write is sent | A transaction whose results have already been returned to the client cannot be transparently retried server-side. This is what makes the failure client-visible rather than silently resolved. | The `SELECT` is fetched, and `reasoning_gap_ms` elapses in the client, before the `INSERT` is issued. |

Workers connect first, then synchronize on a `threading.Barrier`, so the race
starts from a common instant rather than from staggered connection setup.

---

## Environment

| | |
|---|---|
| CockroachDB | `CockroachDB CCL v26.2.5` (Cloud Basic, `aws-ap-south-1`) |
| Isolation requested | `SERIALIZABLE` |
| Isolation reported by server | `serializable` |
| Driver | `psycopg` 3.3.4 |
| Full environment verification | [`VERIFIED.md`](VERIFIED.md) |

---

## PASS CONDITION 2 — CockroachDB SERIALIZABLE: **PASSED**

> At least one transaction returns a client-visible SQLSTATE 40001 reaching our
> code.

### Sweep over `reasoning_gap_ms` (2 workers, amount 60, hard limit 100)

Five repeats at each gap, 35 trials total.

| `reasoning_gap_ms` | Trials | Mean client-visible 40001 | Mean commits | Invariant violated |
|---|---|---|---|---|
| 0 | 5 | 1.00 | 1.00 | 0/5 |
| 5 | 5 | 1.00 | 1.00 | 0/5 |
| 25 | 5 | 1.00 | 1.00 | 0/5 |
| 50 | 5 | 1.00 | 1.00 | 0/5 |
| 100 | 5 | 1.20 | 0.80 | 0/5 |
| 250 | 5 | 1.00 | 1.00 | 0/5 |
| 500 | 5 | 1.00 | 1.00 | 0/5 |

Totals across the sweep: **35/35 trials produced at least one client-visible
40001**; 36 serialization failures, 34 commits, 0 other errors, 0 invariant
violations.

The gap turned out not to be the delicate knob it was expected to be — the
failure surfaces even at `gap=0`, because the client still fetches the
`SELECT` result before issuing the `INSERT`. The one trial at 100 ms where
*both* transactions aborted (hence mean 1.20) is ordinary scheduling variance,
not a different mechanism.

### Scale to N=20 (amount 40, hard limit 100, gap 200 ms)

| Run | Workers | Commits | Client-visible 40001 | Final SUM | Hard limit | Invariant violated | Wall |
|---|---|---|---|---|---|---|---|
| 1 | 20 | 1 | 19 | 40 | 100 | no | 1962 ms |
| 2 | 20 | 1 | 19 | 40 | 100 | no | 2459 ms |
| 3 | 20 | 1 | 19 | 40 | 100 | no | 2221 ms |
| 4 | 20 | 1 | 19 | 40 | 100 | no | 2019 ms |
| 5 | 20 | 1 | 19 | 40 | 100 | no | 2338 ms |

Zero non-serialization errors in any run.

### What the failures actually were

All 131 serialization failures across both experiments were raised at `COMMIT`,
none earlier:

```
SQLSTATE 40001
restart transaction: TransactionRetryWithProtoRefreshError:
  TransactionRetryError: retry txn (RETRY_SERIALIZABLE - failed preemptive
  refresh due to encountered recently written committed value ...)
```

This is the mechanism the project depends on, stated by the database itself:
the transaction read an aggregate, another transaction committed a write into
the span that read covered, and the refresh could not be performed. The
transaction could not be serialized. Nothing here says the losing worker's
decision was *wrong* — the semantic reading of that signal is the project's
contribution, not the database's claim.

---

## PASS CONDITION 1 — PostgreSQL READ COMMITTED: **PASSED**

> Both commit, zero errors, SUM > limit. Invariant violated silently.

| | |
|---|---|
| PostgreSQL | `PostgreSQL 16.12`, stock, run from portable binaries on `127.0.0.1:5432` |
| Isolation requested | `READ COMMITTED` |
| Isolation reported by server | `read committed` |

Nothing about this server is tuned. `default_transaction_isolation` was left at
its shipped value, which is the entire point of the arm: this is what an
application gets when nobody makes a decision about isolation.

### Sweep over `reasoning_gap_ms` (2 workers, amount 60, hard limit 100)

| `reasoning_gap_ms` | Trials | Mean client-visible 40001 | Mean commits | Invariant violated |
|---|---|---|---|---|
| 0 | 5 | 0.00 | 2.00 | **5/5** |
| 5 | 5 | 0.00 | 2.00 | **5/5** |
| 25 | 5 | 0.00 | 2.00 | **5/5** |
| 50 | 5 | 0.00 | 2.00 | **5/5** |
| 100 | 5 | 0.00 | 2.00 | **5/5** |
| 250 | 5 | 0.00 | 2.00 | **5/5** |
| 500 | 5 | 0.00 | 2.00 | **5/5** |

Totals: 35 trials, 70 commits, **0 errors of any kind**, 0 deadlocks, and
**35/35 trials ended with SUM = 120 against a hard limit of 100.**

### Scale to N=20 (amount 40, hard limit 100, gap 200 ms)

| Run | Workers | Commits | Client-visible 40001 | Final SUM | Hard limit | Invariant violated |
|---|---|---|---|---|---|---|
| 1–5 (identical) | 20 | 20 | 0 | **800** | 100 | **YES** |

100 commits across 5 runs. Zero errors. The final state exceeds the hard limit
by 8×, and at no point did the database raise anything.

### The detail that matters most

In every N=20 run, all twenty workers read the same value:

```
observed_sum values seen by each worker: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                                          0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
```

Each worker read a sum of 0, concluded that allocating 40 against a limit of
100 was within budget, and was *individually correct about the state it read*.
Twenty locally-sound decisions composed into a final state that violates the
invariant by 700 units. No worker did anything wrong by its own evidence, and
no error was raised to tell any of them otherwise.

READ COMMITTED permits this execution. Each transaction saw a consistent
snapshot for the duration of its statement, and each insert touched a different
row, so there is no lock conflict to detect and no constraint to trip — the
invariant spans rows that no single transaction wrote. PostgreSQL also offers
SERIALIZABLE, which would refuse this; what is being compared here is default
isolation behaviour, not the ceiling of what each database can do.

This is the silent failure the project exists to make visible.

---

## Observations that matter for later phases

Recorded now, before any experimental result exists, so that nothing looks
retrofitted later.

1. **At N=20 with a synchronized start, only 1 of 20 transactions commits.**
   The barrier makes all twenty read the same instant and write into the same
   refresh span, which is the most adversarial arrangement possible. It is
   ideal for the gate — it proves the signal is abundant — but it means the
   experiment's arms will be dominated by retry behaviour rather than by the
   allocation decision. Phase 4 will likely need staggered agent arrival so
   that a realistic number of allocations succeed. Any such change gets logged
   in `METHODOLOGY.md` with its effect.

2. **Every failure surfaced at `COMMIT`, not at the `INSERT`.** The
   conflict-aware wrapper must therefore treat the commit call as a decision
   point, not just the statements before it.

3. ~~**The invariant was never violated on CockroachDB, at any gap or worker
   count.** Under SERIALIZABLE the aborts did the work.~~

   **CORRECTED 2026-08-14.** This was true of every configuration measured
   above, and false in general. The statement was an artifact of the
   synchronized barrier: contention was so extreme that only one transaction
   ever committed, and one allocation of 40 cannot exceed a limit of 100.
   SERIALIZABLE was not what kept the invariant.

   With staggered arrival — the arrangement the experiment uses, decided
   independently and for unrelated reasons — CockroachDB under SERIALIZABLE
   violates the invariant in every run at arrival windows of 1500 ms and above,
   reaching final sums of 120 to 360 against a limit of 100. Measurements are
   in `METHODOLOGY.md`, Entry 1.

   **SERIALIZABLE surfaces the conflict; it does not resolve it.** That makes
   the project's question sharper rather than weaker: if choosing the database
   were sufficient, the contribution would be a recommendation rather than a
   protocol. What SERIALIZABLE provides is a client-visible signal that the
   state a decision rested on has changed. What the agent does with that signal
   is the thing under test.

4. **`gc.ttlseconds` on this cluster is 4500 (1.25 h)**, not the 14400 or
   100000 quoted in various documentation. The `AS OF SYSTEM TIME` view in
   Phase 5 must read timestamps that are seconds old, never hours old.

5. **The naive baseline is not going to be a strawman, and the gate already
   shows why.** On PostgreSQL every worker read `0` and every worker was right
   about what it read. A retry wrapper that re-reads inside a new transaction
   and replays the same decision is a genuinely reasonable thing for an
   engineer to build — it fixes the transaction, and on many workloads that is
   the correct and complete fix. The hypothesis is specifically about the case
   where the decision itself derived from the state that changed. Arm B has to
   be implemented well enough that this stays a fair comparison.

6. **The two arms differ by 760 units of final state on identical input.**
   PostgreSQL READ COMMITTED reaches SUM 800; CockroachDB SERIALIZABLE reaches
   SUM 40 with 19 conflicts surfaced. Neither number is the interesting one on
   its own — arm C's job is to convert those 19 conflicts into *revised
   decisions* rather than merely into aborts, and that is what Phase 4 has to
   measure.

---

## Reproducing

```bash
# control arm: stock PostgreSQL 16, no Docker required
python scripts/pg_portable.py init        # or: make pg-up  (Docker)

python spike/gate.py trial --backend pg   --workers 2  --gap-ms 200
python spike/gate.py sweep --backend pg   --repeats 5
python spike/gate.py scale --backend pg   --workers 20 --repeats 5

python spike/gate.py trial --backend crdb --workers 2  --gap-ms 200
python spike/gate.py sweep --backend crdb --repeats 5
python spike/gate.py scale --backend crdb --workers 20 --repeats 5
```

The PostgreSQL 16 control arm runs from the EDB binaries-only distribution
unpacked into `vendor/`, with its data directory in `data/pg`. Nothing is
installed and no service is registered; `python scripts/pg_portable.py stop`
and deleting `data/pg` removes it entirely. This was used because Docker
Desktop's WSL2 backend was not available on the build machine — the equivalent
`docker compose` path is still in the repo and `make pg-up` uses stock
`postgres:16`.

Raw per-worker records, including every error string and observed sum, are in
`results/gate/*.json`.
