# Paste-ready prompt — RaceLab system architecture diagram

Copy **everything inside the fenced block below** into a fresh Claude conversation
(claude.ai, or Claude Design). It is self-contained: every fact the diagram needs
is in it, so the model has nothing to invent.

Two notes before you paste:

- **It asks for one HTML artifact.** That renders in-chat, exports clean at 2×,
  and drops straight into a Devpost gallery or a slide.
- **If the first result is close but not right**, do not re-paste the whole
  thing. Reply with one specific change ("make the transaction boundary the
  loudest thing on the page", "the refusal states are getting lost"). Iterating
  beats regenerating.

---

````text
You are a senior technical illustrator and information designer. I need a system
architecture diagram that will be the hero image of a hackathon submission
(CockroachDB × AWS). It has to survive two very different viewers: a judge who
gives it eight seconds, and an engineer who zooms in looking for something wrong.

Deliver **one self-contained HTML artifact** containing an inline SVG diagram.
No external images, no icon CDNs, no web fonts, no JS libraries. Everything
inline so it renders offline and exports cleanly.

────────────────────────────────────────────────────────────────────────
WHAT THE SYSTEM IS
────────────────────────────────────────────────────────────────────────

RaceLab is a policy-enforcing write gateway for AI agents that spend money —
refund bots, procurement approvers, quota grantors.

The problem: when two agents read the same budget before either writes, both see
room, both approve, and the money goes out twice. At default isolation the
database raises no error, because the rule ("everything added up stays under the
limit") spans many rows and cannot be a constraint on any single one.

RaceLab's answer has two halves, and the diagram must make both legible:

1. Agents do not write to the ledger. They ask a gateway to write for them, and
   the gateway checks the rule INSIDE the transaction, after the write and
   before the COMMIT. Under SERIALIZABLE, the state verified before COMMIT is
   the state that becomes durable — or the commit is refused and the whole cycle
   re-runs, RE-DECIDING rather than replaying.

2. The rule that matters often is not in the database at all. It lives in a
   policy document. So a model compiles that document ONCE into a structured
   constraint, which the database then enforces with no model in the loop.

────────────────────────────────────────────────────────────────────────
THE SINGLE IDEA THE DIAGRAM MUST SELL
────────────────────────────────────────────────────────────────────────

**Four things are read in ONE SQL statement, inside ONE transaction:**

    the running total  ·  the hard limit  ·  the governing policy document
    ·  the compiled constraint

That is not a performance trick. It is what makes the guarantee provable: the
rule a write is checked against provably shares a read timestamp with the state
it is checked over. Reading the policy outside the transaction would let it move
between the check and the commit.

**Make the transaction boundary the most visually dominant element on the page.**
Everything else is context around it. If a viewer takes away one thing, it should
be "the check happens inside the transaction that does the write."

────────────────────────────────────────────────────────────────────────
COMPONENTS — draw all of these, grouped as shown
────────────────────────────────────────────────────────────────────────

GROUP A · CLIENTS (left edge)
  • AI agents (show ~3, implying many) — refund bot, procurement agent, quota grantor
  • Claude Code / Cursor / any MCP client
  Both reach the same enforcement path. Label the two entry points:
  → HTTPS POST, SigV4-signed (AWS_IAM auth; an unsigned request gets 403 by design)
  → MCP tool call over stdio

GROUP B · COMPILATION — draw this as a VISUALLY SEPARATE lane, and label it
          "once per policy — NEVER on the write path"
  • Policy document ("Tier-2 refunds capped at $250 per billing cycle…")
    — written by Legal, retrieved by vector search
  • compile_policy() → AWS Bedrock, Claude Sonnet 4.5
  • Human operator, dashed line, labelled "--resolve, only when the policy is
    ambiguous"
  • Output: a structured, versioned, fingerprinted Constraint → stored in the
    policy_constraints table
  This lane must read as OFFLINE and DELIBERATE. Slower line weight, cooler or
  desaturated treatment versus the hot write path. The whole architectural point
  is that interpretation is slow/ambiguous/reviewable while enforcement is
  fast/deterministic/unattended — the drawing should show that separation before
  anyone reads a label.

GROUP C · THE WRITE PATH (centre — this is the hero)
  • AWS Lambda "racelab-gateway" (region ap-south-1), behind a Function URL
  • binding.py + bindings/*.yaml — "which table, declared not coded"
    (a 6-line YAML spec: resource, scope_column, aggregate, hard_limit,
     policy_limit, actions)
  • policy_gate.py — "which constraint governs?" — 7 states, 4 of which refuse
  • conflict.py — the ConflictAware wrapper: the read→reason→enforce→commit loop
  Inside a clearly drawn TRANSACTION BOUNDARY box, show this ordered cycle:
      1. BEGIN
      2. ONE statement reads: total + hard limit + governing doc + compiled constraint
      3. reason  →  the agent chooses an action
      4. apply   →  the row is written
      5. CHECK   →  hard limit first, always; then the compiled policy
      6. COMMIT  —  or 40001, discard the decision, and loop back to step 1
  Draw step 6's failure edge as a real loop back to BEGIN, labelled
  "40001 → re-decide (not replay)". This loop is the library's whole thesis;
  do not reduce it to a small arrow.

GROUP D · COCKROACHDB CLOUD — the memory layer (right)
  Label the group "CockroachDB Cloud · SERIALIZABLE by default"
  • accounts, allocations           — operational state (the $100 hard limit)
  • customers, refunds              — a SECOND resource, enforced from YAML with
                                      zero code. Worth calling out on the canvas.
  • memories                        — VECTOR(1024) + distributed vector index,
                                      embedded with Bedrock Titan v2.
                                      The policy document lives here.
  • policy_constraints              — compiled · versioned · fingerprinted
  • decisions, agent_attempts, race_runs, conflict_edges — telemetry,
                                      with policy_version on every decision
  • SQL views for agent inspection  — race_arm_comparison, race_agent_decisions

GROUP E · AWS SERVICES (label each with what it actually does — judges verify
          from this)
  • Lambda           — the write gateway
  • Bedrock          — Titan embeddings (retrieval) + Claude Sonnet 4.5
                       (reasoning AND policy compilation)
  • Secrets Manager  — the CockroachDB DSN, rotatable, IAM-scoped
  • CloudWatch       — one JSON record per decision, 4 metrics,
                       alarm on HardLimitViolations
  • S3               — Lambda layer artifacts

GROUP F · OPERATOR / INSPECTION (bottom or corner, deliberately quiet)
  • CockroachDB Managed MCP Server → the SQL views (an operator or another agent
    inspecting the experiment)
  • ccloud CLI → connection-budget preflight before launching a swarm
  Note on the canvas: RaceLab is both an MCP **client** and an MCP **server**.

────────────────────────────────────────────────────────────────────────
THE POLICY GATE — render this as a compact legend/table beside the gate node
────────────────────────────────────────────────────────────────────────

  none            ALLOW   no policy document; only the hard limit binds
  compiled        ALLOW   current, enforceable, compiled from the governing doc
  not_in_force    ALLOW   dated policy outside its window; hard limit only
  uncompiled      REFUSE  a policy exists and nothing was compiled from it
  stale           REFUSE  the document moved and nobody recompiled
  unenforceable   REFUSE  clauses the constraint language cannot express
  mismatched      REFUSE  compiled for a different resource

Use a clear green/red (or ink/red) split so ALLOW vs REFUSE is readable at a
glance. Add this caption near it:

  "uncompiled and stale are states a regular expression could not have: it
   re-read the text every request and always produced a number."

Also state, adjacent to the CHECK step: **the hard limit is checked first and in
every state.** A policy that cannot authorize is not a reason to stop enforcing
the one rule the database can enforce unaided.

────────────────────────────────────────────────────────────────────────
TWO RULES, ONE CALLOUT BOX — this is the conceptual contribution
────────────────────────────────────────────────────────────────────────

Include a small, high-contrast callout:

  RULE 1 · lives in a COLUMN        SUM(allocations.amount) <= accounts.hard_limit
           recovered by re-reading state          expressible in SQL: YES

  RULE 2 · lives in RETRIEVED TEXT  the approval ceiling ($80 → $60, mid-run)
           recovered only by refreshing memory    expressible in SQL: NO
                                                  (a WHERE clause has no column
                                                   to reference)

Caption: "Re-reading state protects the rule in your database. Only refreshing
memory protects the rule in your agent's head."

────────────────────────────────────────────────────────────────────────
RESULTS STRIP — a slim band along the bottom
────────────────────────────────────────────────────────────────────────

Five arms · 250 runs · 5,000 agent decisions · live CockroachDB Cloud cluster

  A      stock PostgreSQL, default isolation, naive     45/50 over limit ·     0 conflicts
  A-rc   CockroachDB, READ COMMITTED, naive             48/50 over limit ·     0 conflicts
  B      CockroachDB, SERIALIZABLE, naive               47/50 over limit · 1,660 conflicts
  C-ops  + re-reason over fresh state                    0/50 over limit ·   662 conflicts
  C      + refresh memory too                            0/50 over limit ·   506 conflicts

Emphasise the two 0/50 cells — that is the result. Small print alongside:
"Gateway 62 ms p50 warm · 12 test suites · every prediction pre-registered and
graded, including the two that were falsified."

────────────────────────────────────────────────────────────────────────
VISUAL DESIGN DIRECTION
────────────────────────────────────────────────────────────────────────

TONE: technical editorial. Think a diagram from a Stripe or Cloudflare
engineering post, or a well-made systems paper figure — NOT a marketing slide,
NOT clip-art cloud icons, NOT a generic AWS reference architecture.
Restraint reads as competence here.

PALETTE — use these exact values.

  Light (primary):
    canvas          #FAFAF9      page background
    surface         #FFFFFF      node fill
    ink             #18181B      primary text, primary strokes
    ink-2           #52525B      secondary text
    ink-3           #A1A1AA      tertiary text, axis labels
    hairline        #E4E4E7      borders, dividers

  Accents — semantic, one job each. Do not decorate with them.
    transaction     #4338CA      indigo. THE transaction boundary + the write
                                 path. The one saturated colour on the page.
    data            #0F766E      teal. CockroachDB / the memory layer.
    compile         #7C3AED      violet. The compilation lane (model-touched).
    aws             #B45309      amber. AWS service chips only.
    allow           #15803D      green. ALLOW states, the 0/50 cells.
    refuse          #B91C1C      red. REFUSE states, limit violations.

  Dark mode (support it — define light on :root, override in a
  prefers-color-scheme block AND a [data-theme="dark"] block):
    canvas #0B0B0F · surface #16161A · ink #F4F4F5 · ink-2 #A1A1AA
    hairline #27272A · lift each accent ~15% for contrast on dark.

  Rule: at most ONE saturated accent dominant per region. The page should read
  as ink-on-paper with colour used to mean something.

TYPOGRAPHY
  • UI/labels: system sans stack
    (ui-sans-serif, -apple-system, "Segoe UI", Inter, Roboto, sans-serif)
  • Every identifier — table names, file names, states, SQL — in monospace
    (ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace).
    This is the single highest-leverage choice for looking credible.
  • Title 30–36px/600 · group labels 11px/600 UPPERCASE letter-spacing .1em ·
    node titles 14–15px/600 · body 12–13px · captions 11px
  • No text smaller than 10px. Judges may view this scaled down.

LAYOUT
  • Landscape, roughly 1600×1000 viewBox, scaling to container width.
  • Left→right primary flow: clients → gateway → database.
  • The compilation lane sits ABOVE or BESIDE the write path, visually detached,
    with a dashed boundary and its "never on the write path" label. The
    separation should be obvious before any label is read.
  • Group containers: 1px hairline, 12px radius, generous 24–32px internal
    padding, tinted 3–4% with their group accent. Never heavy fills.
  • Whitespace is the main tool. Crowding is the most common way this kind of
    diagram fails.

EDGES — the usual failure point, so be deliberate:
  • Orthogonal routing (right angles) with 4px corner radii. No diagonals across
    the canvas, no crossings you can route around.
  • EVERY edge carries a short label. An unlabelled arrow is a guess.
  • Weight = importance: transaction-internal flow 2.5px solid; normal calls
    1.5px solid; async/offline/inspection 1.5px dashed 4-3.
  • Arrowheads: small, sharp, filled — 8px, not the default bulky marker.
  • Number the steps inside the transaction (1–6) in small filled circles, so
    the order is unambiguous.

STRUCTURE OF THE ARTIFACT
  • Title + one-line thesis at top:
    "Serializable isolation tells your agent that something changed.
     It does not tell it to think again."
  • The diagram.
  • Below it: the two-rules callout, the gate legend, the results strip.
  • A compact legend for line weights and accent meanings.
  • Footer: repository + live demo links —
    github.com/rajj28/racelab · rajj28.github.io/racelab

TECHNICAL REQUIREMENTS
  • Single HTML file, inline SVG, inline CSS. No external requests of any kind.
  • Responsive: SVG scales with the container, never overflows horizontally.
  • Dark mode via the token blocks described above; give <body> an explicit
    background so it never inherits a host page's colour.
  • Use <title> and <desc> in the SVG and aria-labels on groups.
  • Crisp at 2× for export/screenshot: no raster effects, no blur, no drop
    shadows heavier than a 1px hairline. Vector only.

────────────────────────────────────────────────────────────────────────
DO NOT
────────────────────────────────────────────────────────────────────────
  ✗ Generic cloud/server/database clip-art icons, or fake AWS service logos.
    Use labelled rectangles with monospace names; it looks more professional
    than icon soup.
  ✗ Gradients, glows, 3-D, drop shadows, neon-on-black "cyber" styling.
  ✗ Rainbow palettes. Six accents, each with one job. That is the budget.
  ✗ Unlabelled arrows, or arrows that cross when routing would avoid it.
  ✗ Burying the transaction boundary. If it is not the most prominent structure
    on the page, the diagram has failed.
  ✗ Drawing the compilation lane as if it were part of the request path. It is
    explicitly not, and that is a core claim.
  ✗ Inventing components. Everything you draw is listed above.

────────────────────────────────────────────────────────────────────────
SUCCESS TEST
────────────────────────────────────────────────────────────────────────
An engineer who has never seen this project should be able to answer all five
from the image alone, without reading prose:

  1. Where is the check performed, and why does that placement matter?
  2. What is read in that single statement, and why together?
  3. What happens on a serialization failure? (re-decide, not replay)
  4. Which rule lives in a column and which lives in a document?
  5. When does the gateway refuse to authorize anything at all?

Build it now. Prioritise clarity over density: if something must be dropped to
keep the page breathing, drop detail from Groups E and F — never from the
transaction boundary, the single-statement read, or the two-rules callout.
````

---

## After you get the first version

Good follow-up asks, in the order they usually help:

1. *"Make the transaction boundary heavier — it should be the first thing the eye lands on."*
2. *"The compilation lane still reads as part of the request flow. Detach it further."*
3. *"Tighten the vertical rhythm; there's dead space between the gate and the database group."*
4. *"Give me a 1200×630 crop of just the transaction boundary for the Devpost thumbnail."*

For Devpost specifically: screenshot at 2×, and check it still reads at
**~600px wide**, which is roughly how the gallery renders it.
