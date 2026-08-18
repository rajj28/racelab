# RaceLab — 3-minute submission video script

**Read `docs/DEMO.md` beside this.** This file is *what you say*; DEMO.md is
*what is on screen*, with the exact commands that produce it.

**Measured: 468 spoken words → 3:07 at 150 wpm, 2:50 at 165.** Counted with the
command at the bottom, not estimated — three earlier drafts all *felt* like three
minutes and ran over four. Re-count after any edit.

**To land under 3:00, do one of these:**

- **Read at 160–165 wpm** — a normal presenting pace — and you finish at ~2:50
  with the beats intact. This is the recommended option.
- Or cut the `2:05` binding section (51 words → ~2:44 at 150 wpm). It is the
  designated cut because it is the only section whose removal costs a *second*
  proof of generality rather than a step in the argument. It costs you the
  "point it at your table" story, so cut it last.

Never cut `1:30`. The compiler refusing our own policy is the most credible
fifteen seconds in the video.

---

## Why this script was rewritten

The previous version could not be recorded. It asserted two claims this project
has since **publicly retracted**, and a judge who read `README.md` afterwards
would have found them contradicted by our own repository:

| Old line | Status |
|---|---|
| *"And it still went over budget… Worse than PostgreSQL, on average."* | **RETRACTED.** `B − A` was confounded — arm A is a different database *and* a different network latency. Controlled against `A-rc`, serializable isolation *helps* even a naive client. METHODOLOGY entry 15. |
| *"Exactly eighty… nine measurements, zero variance."* | **RETRACTED.** `45 + 35 = 80` is arithmetic we chose, and the controlled sweep found `78.0`. Entry 16. |

Neither appears below. Every number spoken here is in the repository at the
values in README §Results, or in the capture the demo actually runs.

## What the format demands, and where each is met

| Requirement | Where |
|---|---|
| Problem + who it's for, one sentence, up front | `0:00` |
| Live demo within 20–30 s | `0:16` — real terminal, real cluster |
| AWS services named **on screen** | lower third from `0:16`, full slate at `2:25` |
| CockroachDB features named **on screen** | same |
| Memory visibly stored, retrieved, acted on | `0:50` — `remember` → `recall` → refusal |
| How a real user interacts | the demo *is* the product: an MCP tool and a signed HTTP gateway |
| Under 3:00 | 2:50 at 165 wpm; designated cut if slower |

---

## 0:00 – 0:16 · The problem, then straight in

**On screen:** one title card, four seconds. Then cut to a terminal. No logo
animation, no team intro, no agenda slide.

> Companies are handing AI agents the power to spend money. When two agents check
> the same budget at once, both see room, both approve, and it goes out twice.
>
> Here it is happening.

**Lower third, from here to the end:**
`CockroachDB Cloud · AWS Lambda · Bedrock · Secrets Manager · CloudWatch`

---

## 0:16 – 0:50 · Live: the race, and the one-line fix

**On screen:** twenty agents racing, running total climbing. DEMO.md beat 2.

> Twenty agents, a hundred-dollar budget. On default isolation the ledger ends
> hundreds of dollars over — and the database raises zero errors. It did what it
> was asked. Nobody asked it this.

**Beat. Switch pane.**

> Serializable isolation catches it — sixteen hundred serialization failures
> across the sweep. But the standard fix is to retry the transaction, and a retry
> resubmits the answer that just went stale.
>
> So we changed one thing. On a conflict, throw the decision away and decide again
> against fresh state. One boolean.

**On screen:** the arm table, the `0/50` column highlighted.

> Fifty runs over the limit, down to zero.

---

## 0:50 – 1:30 · The memory, visibly

**On screen:** Claude Code driving the RaceLab MCP server. Real tool calls, real
JSON. Not a slide.

> But the rule that matters isn't in the database. This account's cap lives in a
> policy document, found by vector search. Watch an agent store it, retrieve it,
> and get stopped by it.

**Beat — let all three tool calls play: `remember`, `recall`, `decide_and_write`.**

**Overlay:** `CockroachDB VECTOR(1024) + vector index · Bedrock Titan embeddings`

> `remember` writes the policy. `recall` finds it by meaning, through
> CockroachDB's vector index. `decide_and_write` refuses — stopped inside the
> transaction, before the commit, naming the rule and its version.

**Beat.**

> Now Legal lowers the cap. An agent that only re-reads the balance never
> notices — the balance was never wrong. The rule moved, and the rule was never in
> the database.
>
> Re-reading state protects the rule in your database. Only refreshing memory
> protects the rule in your agent's head.

---

## 1:30 – 2:05 · The part we did not expect

**On screen:** the compiler refusing our own policy. Real terminal output.

> That cap used to be pulled from the document with a regular expression. A regex
> finds a dollar sign. It cannot find "per billing cycle", or "tier two only".
>
> So a model compiles the policy once, into a constraint the database enforces
> with no model in the loop.

**Beat. Hold on the word UNENFORCEABLE.**

> The first thing it did was refuse our own policy.
>
> "Eighty dollars per billing cycle" — a cycle starts on any day, so it isn't a
> calendar month, and the compiler won't guess. The regex read that same sentence
> and returned a confident eighty.

**On screen:** the gateway returning `409`, `policy_status` visible.

> The gateway now authorizes nothing there until a person says what the rule
> means — and that reading is compiled, versioned and attributed. A worse demo. A
> much better system.

---

## 2:05 – 2:25 · Point it at your table

**On screen:** `bindings/refunds.yaml`, then the suite passing.

> None of this is about our scenario. The resource is twenty lines of YAML: a
> table, a scope column, an aggregate, where the limit lives.
>
> That's a refunds table. There is no refund code in this project — the test
> asserts it. Six concurrent writers, real contention, stopped at the compiled
> limit rather than the bigger pool behind it.

---

## 2:25 – 2:50 · The stack, and the close

**On screen:** the full slate. Hold three seconds — judges verify from this frame.

```
CockroachDB                    AWS
  Distributed vector indexing    Bedrock — Titan embeddings, Claude Sonnet 4.5
  SERIALIZABLE isolation         Lambda — the deployed write gateway
  Managed MCP Server             Secrets Manager — the database credential
  ccloud CLI                     CloudWatch — one record per decision, alarmed
                                 S3 — layer artifacts
```

> Every decision is in CloudWatch with the policy version it was made under.
> Twelve test suites. Two of our own predictions were falsified, and both are
> published.

**Beat.**

> Serializable isolation tells your agent something changed. It doesn't tell it to
> think again. That gap is measurable — and it's where agents lose money.

---

## Delivery notes

- **Be in the terminal by 0:16.** If you are still on a slide at 0:30, cut the
  opening paragraph — never the demo.
- **Never speak a retracted number.** Not "worse than PostgreSQL", not "exactly
  eighty, zero variance". They are tabled at the top of this file so you can
  catch them if an old take creeps back in.
- **The two moments that sell it are the refusals**: `decide_and_write` at `0:50`
  and the compiler refusing *our own policy* at `1:30`. Read both flat. A system
  that says no to its authors is the most credible thing in the video.
- Hold the slate at `2:25` for three full seconds. Someone is screenshotting it
  to tick requirements.
- Avoid in narration: "invariant", "write skew", "SQLSTATE", "40001", "READ
  COMMITTED". Say "default isolation" and "serialization failure".
- **Recording:** 1920×1080, terminal at 16–18 pt. `policy_status: "stale"` must
  be readable without pausing.
- **Upload early.** Public or unlisted, playable without a login, then open your
  own Devpost page and watch it back from there.

Re-count after any edit:

```bash
python -c "import pathlib;s=pathlib.Path('docs/VIDEO.md').read_text(encoding='utf-8');\
w=sum(len(l.strip().lstrip('>').split()) for l in s.splitlines() if l.strip().startswith('>'));\
print(f'{w} words -> {w/150:.2f} min at 150 wpm, {w/165:.2f} at 165')"
```
