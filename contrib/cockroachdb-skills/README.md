# Upstream contribution: `retrying-agent-decisions-under-contention`

A skill prepared for [`cockroachlabs/cockroachdb-skills`](https://github.com/cockroachlabs/cockroachdb-skills).
The directory layout here mirrors that repository exactly, so the file can be
copied across unchanged.

    skills/cockroachdb-application-development/
      retrying-agent-decisions-under-contention/
        SKILL.md

## Why this is a complement and not an overlap

We checked the existing skills before writing anything, because
`cockroachdb-application-development` already contains
`designing-application-transactions` and `benchmarking-transaction-patterns`,
and a fourth skill that merely restated them would deserve to be rejected.

Reading `designing-application-transactions` produced a better result than we
expected. **Two pieces of its guidance are individually correct and, composed for
an agent, produce a silent bug.**

- **Step 14** says keep RPC and HTTP calls *outside* the transaction. Correct —
  holding a transaction open across a multi-second model call widens the
  contention window and invites aborts.
- **Step 3** says retry the unit of work on `40001` with backoff. Also correct,
  and its `execute_with_retry(conn, txn_logic)` re-executes the closure, which
  *does* re-derive a deterministic decision.

Put them together when the value being written came from a model, and the model
call is outside the closure by construction. So every retry re-submits a value
derived from a state reading that the `40001` just proved is stale.

That skill's own safety note names the symptom —
*"do not use stale snapshot reads as authoritative preconditions for writes"* —
without saying what to do when the precondition passed through a model. The new
skill covers exactly that, cites the existing one throughout, and sends readers
back to it.

## It also tells readers *not* to use it

The most useful thing in `designing-application-transactions` is step 5, *"Push
Invariants into SQL — Avoid Read-Modify-Write Loops"*, with this pattern:

```sql
UPDATE customer_daily_limits
SET used_total = used_total + $2
WHERE customer_id = $1
  AND day = current_date
  AND used_total + $2 <= daily_limit;
```

That dissolves the read-modify-write race entirely, and it is strictly better
than any retry protocol when it applies. The new skill's "When to Use This Skill"
section leads with it and says plainly: if your rule fits in a `WHERE` clause,
stop reading.

The residual case is the one a guarded `UPDATE` cannot express, because a `WHERE`
clause can only reference columns. An approval ceiling stated in a policy
document, retrieved by vector search and interpreted by a model, has no column to
compare against. That is the whole scope of the contribution, and narrowing it
this hard is what makes it defensible.

## Spec compliance, verified locally

Checked with the repository's own `scripts/validate-spec.py`:

| Check | Result |
|---|---|
| Errors | **0** |
| `name` length | 41 (max 64) |
| `description` length | 755 (max 1024) |
| `name` matches directory | yes |
| Reserved words (`anthropic`, `claude`) | none |
| SKILL.md length | 305 lines (warns above 500) |
| Required sections | When to Use / Prerequisites / Steps / Safety Considerations / References — all present |

Two categories of warning appear and both are understood:

- **Broken internal references** to `../designing-application-transactions/SKILL.md`
  and `../benchmarking-transaction-patterns/SKILL.md`. These are an artifact of
  validating outside a full clone. Re-running with stub siblings in place clears
  them, confirming they resolve in the real repository.
- **Gerund-form suggestion** on the skill name. This fires identically on
  CockroachDB's own `designing-application-transactions` and
  `benchmarking-transaction-patterns` — the check tests whether the name *ends*
  in "ing" and suggests `retrying-...-contentioning`. The name is already a
  gerund. Left as-is to match the existing convention.

## Submitting

`CONTRIBUTING.md` asks for a proposal issue before a PR, so the order is:

1. Open a proposal issue describing the gap above, and let the maintainers say
   whether they would rather see this as a new skill or as a section added to
   `designing-application-transactions`. **Either answer is fine** — the content
   is written to work as both, and the composition problem is worth documenting
   wherever they want it.
2. Fork, branch as
   `add-skill/cockroachdb-application-development/retrying-agent-decisions-under-contention`.
3. Copy `skills/` from this directory into the clone.
4. Run `python scripts/validate-spec.py skills/` and confirm 0 errors.
5. Open the PR referencing the issue.

## Provenance

Every claim in the skill is measured in the parent repository rather than
asserted:

- **Retry replays a stale decision.** Arm B: 730 client-visible `40001`s in one
  configuration, every one correctly raised, zero revised decisions, final totals
  well past the limit.
- **Re-deriving fixes it.** Arms C-ops and C: hard-limit violations 0/10 at every
  arrival window tested.
- **Re-reading state is not enough when the rule lives in a document.** Arm C-ops
  holds the hard limit and still breaches the withdrawn ceiling, landing on
  exactly `$80.00` across nine independent cells with zero variance.
- **A tool-schema `enum` is not enforced.** Claude Sonnet 4.5 returned
  `allocate(30)` against a four-value enum. See `docs/FEEDBACK.md` entry 7.
- **Counting reasoning calls catches what outcome assertions cannot.** The
  arm-collapse guard in `racelab/conflict.py` caught a real bug that every
  final-state assertion passed.
