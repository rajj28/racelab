# RaceLab — 3-minute video script

Every number spoken here is in the repository and reproducible. The two moments
that cut against us are in the script on purpose — they are the most persuasive
thirty seconds in it.

**Measured length: ~430 spoken words ≈ 2:55 at 150 wpm, including the beats.**
A first draft ran 758 words and would have overrun by two minutes; what follows
is the cut version. Don't restore the trims — protect the reveal instead.

---

## 0:00 – 0:12 · Hook

**On screen:** `docs/demo.gif`. Nothing else. Cut on its final frame.

> Two AI agents check the same budget. Both see room. Both approve.
> The money is gone twice — and the database reports no error at all.

---

## 0:12 – 0:38 · Why the database doesn't stop it

**On screen:** the three numbered steps, revealed one at a time.

> One account, a hundred dollars, twenty agents each wanting forty-five.
>
> An agent reads the balance. Nothing spent. It approves forty-five — correct. At
> the same moment two more read the same balance, see the same nothing, and
> approve too. Also correct, given what they saw.
>
> All three writes land. Thirty-five dollars over budget, and every agent
> reasoned correctly.

**Beat.**

> The rule is "everything added up stays under the limit." That's about many rows
> at once, so it can't be a constraint on any one row. The database did exactly
> what it was asked. Nobody asked it this.

---

## 0:38 – 1:05 · What stronger isolation does, and doesn't

**On screen:** arm A and arm B rows from `race_arm_comparison`.

> So we ran it on PostgreSQL at its default isolation, and on CockroachDB at
> serializable. Same workload, same code.
>
> PostgreSQL went over budget nine runs in ten and raised zero errors. It
> committed the broken state silently. CockroachDB caught it — six hundred
> serialization failures, every one correctly raised.

**Beat. This is the turn.**

> And it still went over budget. Ten runs in ten. Worse than PostgreSQL, on
> average.
>
> Because the retry logic ran the transaction again and submitted the same
> answer. The database said "something changed underneath you." Nobody re-read
> anything. Every retry was another chance for a stale decision to land.

---

## 1:05 – 1:18 · The fix, in one line

**On screen:** the two `ConflictAware` constructors, `re_reason` highlighted.

> So we changed one thing. On a serialization failure, don't retry the decision —
> throw it away and decide again against fresh state.
>
> Same class, same arguments. The two versions differ by a single boolean.
> Budget violations went from ten out of ten to zero, at every timing.

---

## 1:18 – 2:05 · The reveal

**On screen:** hold on `$80.00`. Two full seconds of silence before speaking.

> Then this happened.
>
> Halfway through each run we lower the approval cap from eighty dollars to
> sixty. A policy change. It lives in the notes the agent looks up before
> deciding — and nowhere in the database.
>
> The agent that re-reads the balance finished at exactly eighty dollars.
>
> Not about eighty. Exactly eighty — in all five timings we swept, and again in
> all four cells when we swapped the reasoning step for Claude Sonnet 4.5. Nine
> independent measurements, zero variance.

**Beat.**

> Under budget. Re-read the balance perfectly, every time. The database was
> completely satisfied. And it landed precisely on the cap that had been
> withdrawn while it was working.

**On screen:** the two-rule split.

> Re-reading the balance can't help here. The balance was right. The rule moved,
> and the rule was never in the database.
>
> The version that re-read the notes as well finished at forty-five — inside
> both limits. That's the contribution. Re-reading state protects the rule in
> your database. Only re-reading memory protects the rule in your agent's head.

---

## 2:05 – 2:32 · The finding we didn't expect

**On screen:** the model's verbatim rationale beside the action it chose.

> One more thing, from running the reasoning step as a real language model.
>
> Three times in sixty, the model chose an action that broke the cap it had just
> told us it inferred. In the same response as its answer, it wrote:
>
> *"Allocating forty-five would bring the total to eighty, which exceeds this
> ceiling."*
>
> And then it allocated forty-five.

**Beat.**

> Nothing inside the system catches that. The database can't — the transaction is
> perfectly serializable. Re-reading state can't — the state was read correctly.
> And asking the model to check its work returns the check it already did.
>
> So the guardrail has to sit outside the model. That's the argument for a
> library instead of a prompt.

---

## 2:32 – 2:55 · What we broke, and published

**On screen:** `docs/METHODOLOGY.md` scrolling, then the four-outcome card.

> We wrote our predictions down before measuring, and two failed.
>
> Our first full sweep produced exactly the result we predicted — so we checked
> why. A three-hundred-ninety-one millisecond connection handshake meant the arm
> that was supposed to have stale memory never had any. It agreed with us for the
> wrong reason. We threw it out and re-ran. The discarded data is still in the
> repository.

**Beat.**

> A serialization failure tells your agent something changed. It doesn't tell it
> to think again. That gap is measurable — and it's where agents lose money.

---

## Shot list

| # | Asset | Where it is |
|---|-------|-------------|
| 1 | Demo GIF, `$110` vs `$100` | `docs/demo.gif` |
| 2 | Three numbered steps | inspector page, "What actually happened" |
| 3 | Arm comparison table | `SELECT * FROM race_arm_comparison` via MCP |
| 4 | `re_reason` diff | `racelab/conflict.py` |
| 5 | The `$80.00` card | inspector page, "Re-checks the balance" |
| 6 | Two-rule split | inspector page, "Then it gets harder" |
| 7 | Model rationale vs action | README, "The model's reasoning was correct…" |
| 8 | Methodology log | `docs/METHODOLOGY.md`, entries 10–14 |

## Delivery notes

- **The reveal at 1:18 is the whole video.** Two seconds of silence on `$80.00`
  before the first word. Everything before it makes that number mean something;
  everything after explains why it matters.
- **Say "exactly eighty" twice.** The precision is the evidence.
- **Read the adverse findings flat.** "Worse than PostgreSQL, on average" and "it
  agreed with us for the wrong reason" land hardest as plain facts, not
  confessions.
- Avoid "invariant", "write skew", "SQLSTATE" and "READ COMMITTED" in narration.
  They're in the repo for anyone who wants them; in three minutes they cost more
  than they buy.
- If you overrun, cut from 0:12–0:38 (the setup), never from the reveal.
