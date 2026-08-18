# Devpost — the three remaining text boxes

Paste each block exactly as it is. Nothing here needs editing.

---

## 1 · "Please provide any necessary testing credentials or instructions for your functional demo app."

```
No credentials. Nothing to install. Open it and press Race.

    https://racelab.fly.dev

WHAT IT ACTUALLY DOES

This is not a recording or a simulation. Pressing Race starts a real race
against the same CockroachDB Cloud cluster that produced every number in our
submission. Each agent opens its own connection, the collisions are real
serialization failures, the rows land in a real table, and the events stream
to your browser as they happen.

NINETY SECONDS THAT SHOW THE WHOLE RESULT

Set "Agents" to 10 and "Arrival window" to 0 ms, then race these three in order
and watch the number at the top:

  1. "Told, and ignores it"
     Goes hundreds of dollars over a $100 budget, with dozens of collisions --
     every one correctly reported by the database. It still overshoots, because
     the standard fix is to retry, and a retry re-sends the answer that just
     went stale.

  2. "Works it out again"
     Same agents, same timing, one difference: on a collision it re-decides
     instead of replaying. The budget holds -- but watch the cap. It breaks.

  3. "Works it out and re-reads the notes"
     Both hold. The difference between #2 and #3 is one boolean.

The right-hand panel shows the actual rows from the `memories` table on
CockroachDB, found by meaning through a VECTOR(1024) index and embedded with
Amazon Bedrock Titan. One of them arrives MID-RUN and supersedes another -- that
is the cap changing while the agents are working, and it is why #2 fails and #3
does not. The cap is in that text and in no column anywhere, which is the whole
point of the project.

THINGS YOU MIGHT HIT, AND WHAT THEY MEAN

  * First click slow (5-10s). The host suspends the machine when idle; it wakes
    on the first request. Subsequent races are immediate.
  * "Another race is running." Not an error. Races are serialised on purpose:
    the cluster's measured connection budget is about 30 and each race holds one
    connection per agent. Wait a few seconds.
  * Numbers differ from our video. Expected, and honest -- contention varies per
    run. What never varies is the pattern: the first approach goes over, the
    third holds.
  * "Arm A" is absent. It runs on stock PostgreSQL, which only exists on a
    developer's machine. It is in the repo and in the results table.

IT CANNOT DAMAGE THE EVIDENCE

The demo races `demo-live-001`, an account seeded for this purpose and reset
before every run. It never touches the account our published measurements and
test suite use, so no amount of clicking can corrupt a reported result.

STATIC FALLBACK, IF THE LIVE APP IS EVER DOWN

    https://rajj28.github.io/racelab/

Fully self-contained -- no network, no external scripts, works offline. Every
number on it came back from a live cluster and was captured by
scripts/capture_ui.py.

RUN THE WHOLE SYSTEM YOURSELF (about five minutes)

A CockroachDB connection string is the only hard requirement.

    git clone https://github.com/rajj28/racelab && cd racelab
    pip install -r requirements.txt
    cp .env.example .env            # fill in RACELAB_CRDB_DSN
    python -m racelab.schema --backend crdb
    python scripts/seed.py --reset
    python scripts/test_all.py                  # all 12 suites
    python scripts/test_all.py --skip-bedrock   # no AWS credentials needed

To run the live app locally:

    python -m racelab.server         # then open http://127.0.0.1:8000

To see the gateway enforce a table the codebase has no code for:

    python scripts/test_binding.py --create
    python scripts/test_binding.py

Happy to give a live walkthrough or signed access to the IAM-protected write
gateway on request.
```

---

## 2 · "Which AI tools have you leveraged while working on this project?"

```
CLAUDE CODE (Claude Opus and Claude Sonnet 4.5) -- used throughout as a
pair-programming assistant: architecture, implementation, test design,
documentation, and the analysis that led us to retract two of our own claims.
Every design decision, experiment and retraction recorded in
docs/METHODOLOGY.md was reviewed and directed by a human. Disclosed in full
rather than minimised.

CLAUDE SONNET 4.5 VIA AMAZON BEDROCK -- inside the product itself, in two
distinct roles:

  * The agent reasoning step. This is the "model arm" of our experiment: 57 of
    60 decisions agreed with our deterministic reference, and the memory-refresh
    effect reproduced within band for both. It also surfaced a failure we
    report rather than hide -- the model chose an action while writing, in the
    same response, that the action exceeded the ceiling it had just inferred.
    Three times in sixty.

  * The policy compiler. A model reads a natural-language policy ONCE and emits
    a structured constraint that the database then enforces with no model in the
    loop. That split is what removes the failure above: the model is not in the
    enforcement path at all, so it cannot smuggle a different reading of the rule
    into each decision. The bug class is gone rather than mitigated.

AMAZON TITAN TEXT EMBEDDINGS V2 VIA AMAZON BEDROCK -- 1024-dimensional
embeddings for every memory and every retrieval query, indexed by CockroachDB's
distributed vector index. This is what makes the spending cap findable by
meaning rather than by keyword.

COCKROACHDB CLOUD'S MANAGED MCP SERVER -- used with an MCP client to inspect the
experiment's own telemetry through SQL views we shaped for that purpose. We
deliberately did not write our own inspection server; what was missing was a
schema an agent could query, not another server.

WE ALSO SHIPPED AN MCP SERVER. racelab/integrations/mcp_server.py gives Claude
Code, Cursor or any MCP client a guarded write it cannot use to violate the
policy it retrieved -- including a "reconsider" result, which is the case MCP has
no vocabulary for: your last answer came from state that has since changed, here
is the new state, decide again.

And a contribution back: we wrote an Agent Skill about this failure mode and
submitted it upstream to CockroachDB's own skills repository.
  https://github.com/cockroachlabs/cockroachdb-skills/issues/25
  https://github.com/cockroachlabs/cockroachdb-skills/pull/26
```

---

## 3 · Image gallery — five files, in this order

All in `docs/thumbnails/`, all exactly 3:2, all well under the 5 MB cap.

| # | File | Role |
|---|---|---|
| 1 | `1-hook-thumbnail.png` | **thumbnail** — readable down to 300 px |
| 2 | `5-demo-animation-3x2.gif` | the animation — motion in a grid of stills earns a second look |
| 3 | `2-architecture-transaction-boundary.png` | the mechanism |
| 4 | `3-results-five-arms.png` | the evidence |
| 5 | `4-memory-in-action.png` | CockroachDB memory, visibly |

`docs/demo.gif` was 900×520 (1.73:1). I padded it to 900×600 with its own
background colour rather than letting Devpost letterbox or centre-crop it —
a crop would have cut the `$110` / `$100` figures that are the whole point of
the animation.

---

## 4 · Architecture diagram — prompt for the optional upload

Devpost asks for "an architectural diagram showing how CockroachDB, AWS
services, and your agent interact". That is a narrower brief than the full
system diagram, so this is a **deliberately smaller prompt**: three actors, one
loop, nothing else. Paste into Claude.

````text
Create a clean architecture diagram as a single self-contained HTML artifact
with an inline SVG. No external assets, no icon libraries, no web fonts. It will
be screenshotted at 2x and uploaded to a hackathon submission, so it must be
legible at 1200px wide and must not look like generic cloud clip-art.

It answers exactly one question: how do the agent, AWS, and CockroachDB
interact? Three columns, left to right. Nothing else on the canvas.

COLUMN 1 - THE AGENT (left)
  Three small identical tokens stacked vertically, labelled "AI agent".
  One arrow from them to column 2, labelled "propose a write".
  Caption underneath: "agents never write to the ledger themselves"

COLUMN 2 - AWS (centre, the widest column)
  A container labelled "AWS". Inside it, stacked:
    - "Lambda - racelab-gateway"  (the largest box; this is the hub)
    - "Bedrock - Titan embeddings + Claude Sonnet 4.5"
    - "Secrets Manager - the database credential"
    - "CloudWatch - one record per decision"
  Inside the Lambda box, draw a clearly-bounded inner region labelled
  "ONE TRANSACTION" containing four numbered steps down the left edge:
    1 read      2 reason      3 enforce      4 commit
  Draw a return arrow from step 4 back to step 1, curving outside the numbered
  list, labelled "conflict -> decide again, not retry". This loop must be the
  most visually prominent thing in the whole diagram.

COLUMN 3 - COCKROACHDB (right)
  A container labelled "CockroachDB Cloud - SERIALIZABLE". Inside it, four
  cylinders:
    - "allocations, accounts"        annotate: "the budget - a column"
    - "memories - VECTOR(1024)"      annotate: "the policy - retrieved text"
    - "policy_constraints"           annotate: "compiled, versioned"
    - "decisions"                    annotate: "audit trail"

THE ARROWS BETWEEN COLUMN 2 AND 3 - these carry the argument
  A single THICK bidirectional arrow from the ONE TRANSACTION region to the
  first two cylinders together, labelled across two lines:
      "one statement reads the balance AND the policy"
      "same transaction, same timestamp"
  A thin dashed arrow from Bedrock to the memories cylinder, labelled
  "embeddings".
  A thin arrow from Lambda to decisions, labelled "append".

ONE CALLOUT, bottom centre, in a bordered box:
  "The budget is a column - re-reading state recovers it.
   The cap is a document - only refreshing memory does."

PALETTE - use these exact values and no others:
  canvas #FAFAF9 | surface #FFFFFF | ink #18181B | muted #52525B
  hairline #E4E4E7
  transaction #4338CA (indigo - the ONE TRANSACTION region and its loop ONLY)
  data #0F766E (teal - CockroachDB)
  aws #B45309 (amber - the AWS container label only)
Support dark mode: define the light palette on bare :root, then override the
tokens in both a prefers-color-scheme block and a [data-theme="dark"] block.
Give body an explicit background colour.

TYPE
  Labels in a system sans stack. EVERY identifier - table names, service names,
  VECTOR(1024) - in monospace; that single choice is what makes it read as
  engineering rather than marketing. Nothing below 11px.

LINES
  Orthogonal routing only, right angles with small corner radii, no diagonals,
  no crossings. Small sharp filled arrowheads, 8px. Weight carries meaning:
  2.5px for the transaction loop, 1.5px solid for calls, 1.5px dashed for
  asynchronous or offline paths. Every arrow carries a label.

DO NOT
  - No cloud icons, no service logos, no clip-art. Labelled rectangles only.
  - No gradients, glows, shadows, or 3-D.
  - No components beyond the ones listed - do not invent a queue, a cache or a
    load balancer.
  - Do not let the ONE TRANSACTION region become a small detail. If a viewer
    cannot see at a glance that the check happens inside the transaction, the
    diagram has failed.

Keep it sparse. Generous whitespace beats density; this is a diagram someone
reads in ten seconds, not a reference.
````
