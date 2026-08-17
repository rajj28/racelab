# RaceLab — 3-minute video script

Every number spoken here is in the repository and reproducible. The two moments
that cut against us are in the script on purpose — they are the most persuasive
twenty seconds in it.

**Measured: 529 spoken words. 3:32 at 150 wpm, 3:12 at 165.**

Counted with a script rather than estimated, and the estimating went badly three
times: drafts came in at 758 words (5:03) and 641 (4:16), and both *felt* like
three minutes while reading them. Trust the counter at the bottom of this file,
not your ear.

**To land under 3:00, do one of these — not both:**

- Read at 165 wpm and cut the `2:05` section ("The finding we didn't expect",
  ~95 words). Gets you to ~2:40. This is the recommended cut: it is the only
  section whose removal costs an interesting aside rather than a step in the
  argument.
- Or keep everything and accept ~3:15, if the submission allows any overrun.

Never cut the reveal. If something has to go, it goes from the setup at `0:12`.
Section timings below are targets for pacing, not a budget that sums to the
measured total.

---

## 0:00 – 0:12 · Hook

**On screen:** `docs/demo.gif`. Nothing else. Cut on its final frame.

> Two AI agents check the same budget. Both see room. Both approve.
> The money is gone twice — and the database reports no error at all.

---

## 0:12 – 0:35 · Why the database doesn't stop it

**On screen:** the three numbered steps, revealed one at a time.

> One account, a hundred dollars, twenty agents each wanting forty-five. Three of
> them read the balance before any of them wrote. All three approved. Thirty-five
> dollars over budget, and every agent reasoned correctly.

**Beat.**

> The rule is "everything added up stays under the limit" — a statement about many
> rows at once, so it can't be a constraint on any one row. The database did what
> it was asked. Nobody asked it this.

---

## 0:35 – 1:02 · What stronger isolation does, and doesn't

**On screen:** arm A and arm B rows from `race_arm_comparison`.

> PostgreSQL at default isolation went over budget nine runs in ten and raised
> zero errors — it committed the broken state silently. CockroachDB at
> serializable caught it: six hundred serialization failures, correctly raised.

**Beat. This is the turn.**

> And it still went over budget. Ten runs in ten. Worse than PostgreSQL, on
> average.
>
> Because the retry logic ran the transaction again and submitted the same answer.
> Every retry was another chance for a stale decision to land.

---

## 1:02 – 1:15 · The fix, in one line

**On screen:** the two `ConflictAware` constructors, `re_reason` highlighted.

> So we changed one thing: on a serialization failure, throw the decision away and
> decide again against fresh state. The two versions differ by a single boolean.
> Budget violations went from ten out of ten to zero, at every timing.

---

## 1:15 – 2:05 · The reveal

**On screen:** hold on `$80.00`. Two full seconds of silence before speaking.

> Then this happened.
>
> Halfway through each run we lower the approval cap from eighty dollars to sixty.
> It lives in the notes the agent reads before deciding — and nowhere in the
> database.
>
> The agent that re-reads the balance finished at exactly eighty dollars.
>
> Not about eighty. Exactly eighty — across all five timings we swept, and again
> with Claude Sonnet 4.5 doing the reasoning. Nine measurements, zero variance.

**Beat.**

> Under budget. Re-read the balance perfectly. The database was completely
> satisfied — and it landed precisely on the cap that had been withdrawn while it
> was working.

**On screen:** the two-rule split.

> Re-reading the balance can't help. The balance was right. The rule moved, and
> the rule was never in the database. The version that re-read the notes finished
> at forty-five, inside both limits.
>
> Re-reading state protects the rule in your database. Only re-reading memory
> protects the rule in your agent's head.

---

## 2:05 – 2:28 · The finding we didn't expect

**On screen:** the model's verbatim rationale beside the action it chose.

> With a real language model reasoning, three times in sixty it chose an action
> that broke the cap it had just told us it inferred. In the same response:
>
> *"Allocating forty-five would bring the total to eighty, which exceeds this
> ceiling."*
>
> And then it allocated forty-five.

**Beat.**

> Nothing inside the system catches that. The transaction is perfectly
> serializable. The state was read correctly. And asking the model to check its
> work returns the check it already did.
>
> So the guardrail has to sit outside the model — a library, not a prompt.

---

## 2:28 – 2:45 · What we broke, and published

**On screen:** `docs/METHODOLOGY.md` scrolling, then the four-outcome card.

> Our first full sweep produced exactly the result we predicted. So we checked
> why — and a three-hundred-ninety-one millisecond connection handshake meant the
> arm that was supposed to have stale memory never had any. It agreed with us for
> the wrong reason. We threw it out and re-ran. The discarded data is still in the
> repo.

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

- **The reveal at 1:15 is the whole video.** Two seconds of silence on `$80.00`
  before the first word. Everything before it makes that number mean something;
  everything after explains why it matters.
- **Say "exactly eighty" twice.** The precision is the evidence.
- **Read the adverse findings flat.** "Worse than PostgreSQL, on average" and "it
  agreed with us for the wrong reason" land hardest as plain facts, not
  confessions.
- Avoid "invariant", "write skew", "SQLSTATE" and "READ COMMITTED" in narration.
  They stay in the repo for anyone who wants them; in three minutes they cost
  more than they buy.
- Re-count after any edit:
  `python -c "import pathlib;s=pathlib.Path('docs/VIDEO.md').read_text(encoding='utf-8');print(sum(len(l.strip().lstrip('>').split()) for l in s.splitlines() if l.strip().startswith('>')))"`
