# RaceLab — the 3-minute record

Everything you need to sit down and record: the narration to read, the exact
commands to run, what to point at, and the animation brief for the opening.

**Budget: 0:00–0:30 is the hook. 0:30–3:00 is the demo.** The first thirty
seconds decide whether anyone watches the rest, so they get their own asset and
their own script, below.

---

# PART 1 · The golden 30 seconds

## What plays

An animated cold open — no talking head, no logo reveal, no agenda. It states
the problem visually and lands on a number. The brief for generating it is at
the very bottom of this file (**§ Animation prompt**), ready to paste.

## Narration for 0:00–0:30

*(66 words → ~24 s at 165 wpm, leaving beats)*

> Two AI agents check the same budget.
>
> **[beat — both tokens light up]**
>
> Both see a hundred dollars. Both approve forty-five. Both are right.
>
> **[beat — the writes land]**
>
> The account is now over budget, and the database raised no error at all.
> It was asked to protect one row at a time. This rule spans all of them.
>
> Give twenty agents one budget, and it ends in the hundreds.
>
> **[hard cut to the live app]**

## Delivery

- **Do not explain anything yet.** No "serializable", no "transaction". The
  words *budget*, *approve*, *over* carry the whole idea.
- The three "Both…" clauses are a rhythm — same cadence, no pause between them,
  then a full stop before "The account is now over budget."
- Land hard on **"raised no error at all"**. That is the hook: not that
  something broke, but that nothing complained.
- Cut to the live app **on the word "hundreds"**, mid-breath. Do not let the
  animation resolve gently.

---

# PART 2 · The demo, 0:30 – 3:00

Two windows, pre-arranged before you hit record:

| | |
|---|---|
| **Window A** | Chrome, full screen, <https://racelab.fly.dev> |
| **Window B** | Terminal, 1920×1080, font at 16–18 pt |

## Before you record

```bash
# 1. Wake the app so the first click is not a cold start (Fly suspends it)
curl -s -o /dev/null -w "%{http_code}\n" https://racelab.fly.dev/api/state

# 2. Reset the demo account so the ledger starts at $0
curl -s -X POST https://racelab.fly.dev/api/reset

# 3. Local checks, for the terminal beats
python scripts/compile_policies.py --show
```

- [ ] `racelab.fly.dev` loads and the arm cards render
- [ ] Terminal at 16–18 pt — `policy_status: "stale"` must be readable without pausing
- [ ] Mic recorded and played back
- [ ] Nothing else is hitting the cluster

---

## Beat 1 · `0:30` — "Here it is, live"

**Screen:** Window A. Arm **B — Told, and ignores it** selected. Agents 10,
arrival window 0 ms. Press **Race**.

> This is running right now, against the same CockroachDB cluster every number
> in this project came from. Ten agents, one shared budget, all arriving at
> once.

**Let it play.** The lanes fill, the bar goes red, the number climbs past $100.

> Two hundred and twenty-five dollars against a hundred. Thirty-five collisions,
> every one of them correctly reported by the database — and it still went over,
> because the standard fix is to retry, and a retry sends the same answer that
> just went stale.

**Point at:** the red bar crossing the `budget $100` marker, and the collision
count in the *What happened* panel.

---

## Beat 2 · `1:00` — "Change one thing"

**Screen:** same window. Click **C-ops — Works it out again**. Press **Race**.

> Same agents, same timing. One difference: on a collision it throws the
> decision away and works the answer out again against fresh state.

**Beat while it runs.**

> The budget holds. Zero over, every time.

**Then click C — Works it out and re-reads the notes.** Press **Race**.

> But watch the cap. That one lives in a policy document, not a column — and it
> changed while the agents were working. Re-reading the balance cannot catch
> that, because the balance was never wrong. Only re-reading the notes can.

**Point at:** the *notes* panel on the right — `demo-m5 · ARRIVES MID-RUN` in
red — and then the green "both limits held".

---

## Beat 3 · `1:35` — The memory is real

**Screen:** Window B.

```bash
python -c "
import sys; sys.path.insert(0,'.')
from racelab.db import connect
from racelab.embeddings import get_embedder
from racelab.memory import to_vector_literal
from racelab.schema import EMBED_DIMS
v = to_vector_literal(get_embedder('titan').embed('What is the current authorization ceiling?'))
with connect('crdb') as c:
    for r in c.execute(f'EXPLAIN SELECT memory_id FROM memories WHERE account_id = %s ORDER BY embedding <=> %s::VECTOR({EMBED_DIMS}) LIMIT 12', ('hero-001', v)):
        print(r[0])
"
```

**Expect — the two lines that matter:**

```
└── • vector search
      table: memories@memories_embedding_idx
      prefix spans: [/'hero-001' - /'hero-001']
```

> That cap is found by meaning, through CockroachDB's vector index over a
> thousand-dimension column, embedded with Amazon Bedrock Titan. The optimizer
> picked the vector index with no hint — and we have a test that fails if it
> ever stops.

---

## Beat 4 · `2:00` — The compiler refused our own policy

**The most credible fifteen seconds in the video.** Unedited.

```bash
python scripts/compile_policies.py --account hero-001
```

**Expect:**

```
  hero-001: SUM(amount) over allocations <= 60 UNENFORCEABLE: per billing cycle -
    cannot map to available windows without knowing if billing cycle aligns with
    calendar_month or other defined period
    NOT STORED. The gateway will refuse writes against hero-001 until this is resolved.
```

> The cap used to be pulled out of that document with a regular expression. A
> regex finds a dollar sign. It cannot find "per billing cycle".
>
> So a model compiles the policy once, into a constraint the database enforces
> with no model in the loop. And the first thing it did was refuse our own
> policy — a billing cycle can start on any day, so it isn't a calendar month,
> and it won't guess. The regex read that same sentence and returned a confident
> eighty.

**Hold on the word `UNENFORCEABLE` for two full seconds.**

---

## Beat 5 · `2:25` — Point it at your table

```bash
cat bindings/refunds.yaml
grep -rn "refund" racelab/ deploy/ | grep -v '"""' | wc -l    # expect 0
```

> None of this is about our scenario. The resource is twenty lines of YAML — a
> table, a scope column, an aggregate, where the limit lives. That's a refunds
> table. There is no refund code in this project, and the test asserts it.

---

## Beat 6 · `2:40` — The stack, and the close

**Screen:** the slate. Hold three full seconds — judges screenshot this frame.

```
CockroachDB                          AWS
  Distributed vector indexing          Bedrock — Titan embeddings, Claude Sonnet 4.5
  SERIALIZABLE isolation               Lambda — the deployed write gateway
  Managed MCP Server                   Secrets Manager — the database credential
  ccloud CLI                           CloudWatch — one record per decision, alarmed
  (we also PROVIDE an MCP server)      S3 — layer artifacts

  Live:  racelab.fly.dev          Code:  github.com/rajj28/racelab
```

> Twelve test suites. Two of our own predictions were falsified, and both are
> published.
>
> **[beat]**
>
> Serializable isolation tells your agent something changed. It doesn't tell it
> to think again. That gap is measurable — and it's where agents lose money.

---

## Word count

Re-run after any edit:

```bash
python -c "import pathlib;s=pathlib.Path('docs/RECORDING.md').read_text(encoding='utf-8');\
w=sum(len(l.strip().lstrip('>').split()) for l in s.splitlines() if l.strip().startswith('>'));\
print(f'{w} words -> {w/150:.2f} min at 150 wpm, {w/165:.2f} at 165')"
```

## If it breaks on camera

| Symptom | Fix |
|---|---|
| First click is slow | Fly suspended the machine — the pre-record `curl` wakes it |
| "another race is running" | someone else is on the demo; wait ~15 s |
| Totals differ from this file | contention varies per run — **narrate the pattern, never a number** |
| `409 uncompiled` locally | `python scripts/compile_policies.py --account hero-001` |
| Live app unreachable | fall back to <https://rajj28.github.io/racelab/>, which is static |

---

# § Animation prompt — paste this into Claude

For the 0:00–0:30 cold open. Ask for an HTML artifact, screen-record it, and cut
on the final frame.

````text
Build a single self-contained HTML artifact: a 30-second looping animation I will
screen-record as the cold open of a hackathon submission video. No external
assets, no libraries, no fonts from a CDN — inline SVG and CSS only. 1920×1080,
and it must look deliberate and expensive at full screen.

THE STORY, in five beats. Time them exactly; the whole thing runs 30 seconds.

  0.0–4.0s   A single horizontal bar, centred, labelled "BUDGET  $100". Calm.
             Empty. A thin dashed marker sits at its right end.

  4.0–9.0s   Two small rounded-square tokens fade in on the left, labelled
             "AGENT 1" and "AGENT 2". A thin line reaches from each to the bar.
             Both lines arrive at the SAME point — the left edge of the empty
             bar. Above each, a small readout counts up to "$100 available".
             They must read as simultaneous, not sequential.

  9.0–13.0s  Both tokens turn green and stamp "APPROVE $45". Hold. This is the
             moment that should feel calm and correct — nothing is wrong yet.

  13.0–18.0s Two fills sweep into the bar, one after the other, fast. The bar
             fills past its own right-hand marker and KEEPS GOING, overflowing
             beyond the container's rounded edge. The overflow region is a
             muted red. A counter above snaps to "$135".

  18.0–23.0s Everything else dims to 20% opacity. One line of type fades up,
             centred, large:  "The database reported no error."
             Hold it. This is the beat the whole animation exists for.

  23.0–30.0s The two tokens multiply into twenty, arranged in a tight grid, all
             flashing "APPROVE" at once. The bar's overflow races far past the
             marker. The counter spins up and lands hard on "$450". A final
             short line fades in beneath: "Every agent was right."
             Hold the last frame for 2 full seconds — I cut here.

PALETTE — use these exact values, nothing else:
  canvas      #0B0B0F   (near-black, the whole background)
  surface     #16161A   (the empty bar)
  ink         #F4F4F5   (primary type)
  ink-dim     #71717A   (labels, secondary type)
  approve     #22C55E   (the tokens when they approve — used sparingly)
  over        #EF4444   (the overflow, and $450)
  hairline    #27272A   (the bar's border, the dashed marker)

TYPOGRAPHY
  Every number and label in a monospace stack (ui-monospace, "SF Mono",
  "Cascadia Mono", Consolas, monospace) — the numbers are the point, and mono
  makes them read as instrumentation rather than marketing.
  The two sentences at 18s and 23s in a clean sans, 300 weight, generous
  letter-spacing, ~44px. Numbers: tabular figures, 72px, weight 700.

MOTION
  Restrained and mechanical. cubic-bezier(.4, 0, .2, 1) for everything. No
  bounce, no elastic, no spin, no particles, no glow, no parallax, no
  3-D. The fills sweep; the counters tick in discrete steps rather than
  smoothly interpolating. It should feel like a system diagram coming to life,
  not a title sequence.

  The ONE moment of drama is the overflow at 13s: it should visibly break the
  bar's boundary rather than stopping neatly at it.

REQUIREMENTS
  - One `<div>` root at 1920×1080 with `transform: scale()` so it fits any
    viewport; no horizontal scroll ever.
  - Pure CSS keyframes driven by one master timeline, so a screen recording is
    frame-accurate and it loops cleanly.
  - Respect `prefers-reduced-motion` by jumping between the five states with no
    tweening.
  - No audio. No captions burned in beyond the two sentences named above.
  - Add a small "restart" affordance in a corner, styled at 30% opacity, so I
    can re-run takes without reloading.

DO NOT
  - Do not add a logo, a product name, a tagline, or a call to action.
  - Do not use gradients, drop shadows, blurs, or neon.
  - Do not add explanatory captions — the five beats carry it.
  - Do not use more than the seven colours listed.

The test: someone who knows nothing about databases should watch it once and be
able to say "two things checked the same budget, both said yes, and nothing
noticed."
````
