# RaceLab — narration for ElevenLabs

Clean text-to-speech input. Every number is spelled out, no markdown, no stage
directions inside the spoken text.

**Generate the seven segments separately, not as one file.** Two reasons: you can
re-record a single beat without redoing everything, and the silences that matter
(the two-second hold on `UNENFORCEABLE`, the beat before the closing line) are
added in your video editor where you can see the frame — not guessed at by a
model.

**Total: 411 words → 2 minutes 29 seconds at 165 wpm.** Counted, not estimated.
The seven pauses below add about twelve seconds, so a finished cut lands near
**2:41** — inside the three-minute limit with real margin.

| Segment | Words | At 165 wpm |
|---|---|---|
| 1 · cold open | 60 | 22 s |
| 2 · the live race | 68 | 25 s |
| 3 · change one thing | 70 | 25 s |
| 4 · the memory is real | 39 | 14 s |
| 5 · the compiler refused us | 89 | 32 s |
| 6 · point it at your table | 45 | 16 s |
| 7 · the close | 40 | 15 s |

## Voice settings

| Setting | Value | Why |
|---|---|---|
| Model | Eleven Multilingual v2 (or Turbo v2.5) | v2 has the steadier pacing for narration |
| Stability | **45–55** | Low enough to keep inflection, high enough not to wander on the numbers |
| Similarity | 75 | |
| Style | **0–15** | This script persuades with facts. Performance undercuts it. |
| Speaker boost | on | |
| Speed | 1.0 | The word count already fits; do not compress |

**Voice choice:** pick a calm, mid-range, unhurried voice. The adverse findings —
*"it still went over"*, *"refused our own policy"* — land hardest read flat. An
energetic delivery makes them sound like spin.

---

## SEGMENT 1 — cold open · plays over the animation · 0:00–0:30

```
Two AI agents check the same budget.

Both see a hundred dollars. Both approve forty-five dollars. Both are right.

The account is now over budget. And the database raised no error at all. It was asked to protect one row at a time. This rule spans all of them.

Give twenty agents one budget, and it ends in the hundreds.
```

*Pauses to add in the editor: 1.0 s after "same budget." · 0.8 s after "Both are right." · **1.2 s after "no error at all."** — that is the hook, let it sit.*

---

## SEGMENT 2 — the live race · 0:30–1:00

```
This is running right now, against the same CockroachDB cluster every number in this project came from. Ten agents, one shared budget, all arriving at once.

Two hundred and twenty-five dollars, against a hundred. Thirty-five collisions, every one of them correctly reported by the database. And it still went over, because the standard fix is to retry, and a retry sends the same answer that just went stale.
```

*Pause: 3–4 s of silence between the two paragraphs while the race plays. Generate them as two clips if that is easier to cut.*

---

## SEGMENT 3 — change one thing · 1:00–1:35

```
Same agents. Same timing. One difference: on a collision, it throws the decision away and works the answer out again against fresh state.

The budget holds. Zero over, every time.

But watch the cap. That one lives in a policy document, not in a column. And it changed while the agents were working. Re-reading the balance cannot catch that, because the balance was never wrong. Only re-reading the notes can.
```

*Pause: 2 s before "But watch the cap." — you are switching arms on screen.*

---

## SEGMENT 4 — the memory is real · 1:35–2:00

```
That cap is found by meaning, through CockroachDB's vector index over a thousand-dimension column, embedded with Amazon Bedrock Titan.

The optimizer picked the vector index with no hint. And we have a test that fails if it ever stops.
```

---

## SEGMENT 5 — the compiler refused our own policy · 2:00–2:25

```
That cap used to be pulled out of the document with a regular expression. A regular expression finds a dollar sign. It cannot find, per billing cycle.

So a model compiles the policy once, into a constraint the database enforces with no model in the loop.

And the first thing it did was refuse our own policy. A billing cycle can start on any day, so it is not a calendar month, and it will not guess. The regular expression read that same sentence and returned a confident eighty.
```

*Pause: **2.0 s** after "refuse our own policy." while `UNENFORCEABLE` holds on screen. This is the most credible moment in the video — do not rush it.*

---

## SEGMENT 6 — point it at your table · 2:25–2:40

```
None of this is about our scenario. The resource is twenty lines of configuration: a table, a scope column, an aggregate, and where the limit lives.

That is a refunds table. There is no refund code anywhere in this project, and the test asserts it.
```

---

## SEGMENT 7 — the close · 2:40–3:00

```
Twelve test suites. Two of our own predictions were falsified, and both are published.

Serializable isolation tells your agent something changed. It does not tell it to think again. That gap is measurable. And it is where agents lose money.
```

*Pause: 1.5 s before "Serializable isolation tells your agent…" — the closing line needs air in front of it.*

---

## One continuous block

If you would rather generate a single file, paste this. You lose per-beat
re-recording and you will have to cut silences in yourself.

```
Two AI agents check the same budget. Both see a hundred dollars. Both approve forty-five dollars. Both are right.

The account is now over budget. And the database raised no error at all. It was asked to protect one row at a time. This rule spans all of them. Give twenty agents one budget, and it ends in the hundreds.

This is running right now, against the same CockroachDB cluster every number in this project came from. Ten agents, one shared budget, all arriving at once.

Two hundred and twenty-five dollars, against a hundred. Thirty-five collisions, every one of them correctly reported by the database. And it still went over, because the standard fix is to retry, and a retry sends the same answer that just went stale.

Same agents. Same timing. One difference: on a collision, it throws the decision away and works the answer out again against fresh state. The budget holds. Zero over, every time.

But watch the cap. That one lives in a policy document, not in a column. And it changed while the agents were working. Re-reading the balance cannot catch that, because the balance was never wrong. Only re-reading the notes can.

That cap is found by meaning, through CockroachDB's vector index over a thousand-dimension column, embedded with Amazon Bedrock Titan. The optimizer picked the vector index with no hint. And we have a test that fails if it ever stops.

That cap used to be pulled out of the document with a regular expression. A regular expression finds a dollar sign. It cannot find, per billing cycle. So a model compiles the policy once, into a constraint the database enforces with no model in the loop.

And the first thing it did was refuse our own policy. A billing cycle can start on any day, so it is not a calendar month, and it will not guess. The regular expression read that same sentence and returned a confident eighty.

None of this is about our scenario. The resource is twenty lines of configuration: a table, a scope column, an aggregate, and where the limit lives. That is a refunds table. There is no refund code anywhere in this project, and the test asserts it.

Twelve test suites. Two of our own predictions were falsified, and both are published.

Serializable isolation tells your agent something changed. It does not tell it to think again. That gap is measurable. And it is where agents lose money.
```

---

## Things I deliberately changed for the voice

| Written | Spoken | Why |
|---|---|---|
| `$225` | "two hundred and twenty-five dollars" | TTS reads bare currency symbols unreliably |
| `VECTOR(1024)` | "a thousand-dimension column" | Parentheses become a stumble |
| `YAML` | "twenty lines of configuration" | Read as "yam-ul" or spelled out, both bad |
| `SQL`, `40001` | *cut entirely* | Nothing in the argument needs them |
| *"per billing cycle"* | preceded by a comma | Forces the quoting pause, so it reads as a quotation |
| "it's", "doesn't" | "it is", "does not" | Contractions are where synthetic voices most often slip |

## Check before you commit to a take

- [ ] Listen for **"forty-five"** — some voices clip it to "forty five" as two numbers
- [ ] Listen for **"CockroachDB"** — if it lands badly, spell it "Cockroach D B" in the input
- [ ] Listen for **"Serializable"** — four syllables, ser-ee-al-ize-able; regenerate if it slurs
- [ ] Play it back at the video's volume, not headphone volume
- [ ] Total runtime under **2:55** before you add music
