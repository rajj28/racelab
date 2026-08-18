# Devpost submission — RaceLab

Every field, ready to paste. Four items need **you** to confirm or supply — they
are marked **`⚠️ YOU`**. Everything else is filled from the repository.

---

## 1 · Project name

*(60 char limit — this is 51)*

```
RaceLab — agents that re-decide, instead of retrying
```

**Backups, if you want a different angle:**

| Name | Chars | Angle |
|---|---|---|
| `RaceLab — agents that re-decide, instead of retrying` | 52 | the mechanism |
| `RaceLab — AI agents that think again before they act` | 52 | **plainer**, if the current one reads too abstract |
| `RaceLab — stop AI agents from oversubscribing a shared pool` | 58 | says the outcome outright, and is not money-specific |
| `RaceLab: a write gateway that refused its own policy` | 52 | the surprise |

---

## 2 · Elevator pitch

*(200 char limit — this is 195)*

```
AI agents sharing a pool — a budget, seats, inventory — quietly hand out more than exists. RaceLab stops the write before it lands and makes the agent decide again. 47 failures in 50 became zero.
```

Two deliberate choices here.

**Plain words.** An earlier draft opened with *"Serializable isolation tells your
agent something changed…"* — accurate, and the wrong register for a line a judge
reads in two seconds while scrolling a gallery. No jargon survives: no
*serializable*, no *COMMIT*, no *isolation level*. The repository carries the
precise version for anyone who wants it.

**Not just money.** Naming three different resource types in the first clause
does more work than any adjective could: it tells a reader in six words that this
is about *shared finite things*, not payments. The library genuinely knows
nothing about money — the second scenario grants licence seats, and the binding
spec takes `COUNT(*)` as readily as `SUM(amount)`.

It also does not repeat the thumbnail. The gallery card already shows *"Two
agents checked the budget. Both said yes."* — so the image states the problem and
the pitch supplies the answer, instead of both saying the same thing.

**Backups:**

```
AI agents that share a limited pool quietly give away more than exists, and the database reports no error. RaceLab stops the write and makes the agent think again. 47 failures in 50 runs became zero.
```
*(199 chars — the plainest; drops the examples for a flatter, more universal read)*

```
Give two AI agents one shared pool — a budget, seats, stock — and both approve, silently. RaceLab stops the write before it lands and makes the agent decide again. 47 failures in 50 runs became zero.
```
*(199 chars — dramatises the moment rather than describing the class)*

```
A safety layer for AI agents that draw from shared limits. It blocks the write while it can still be stopped, and makes the agent rethink instead of repeat. 47 failures in 50 runs became zero.
```
*(191 chars — leads with what it is, if you would rather not open on the problem)*

## 3 · About the project

Paste the whole block below into the Project Story field.

---

## Inspiration

AI agents are being handed authority over **shared, finite things**: a budget, a pool of license seats, warehouse stock, rate-limit quota, GPU hours, appointment slots. Anything where many actors draw from one pool that must not be oversubscribed.

So we asked what happens when two agents draw from the same pool at the same moment.

Both read the pool as having room. Both grant. The pool is oversubscribed, **and the database raises no error at all** — because the rule is *"everything added up stays under the cap"*, which is a statement about many rows, so it cannot be a constraint on any single one. The database did exactly what it was asked. Nobody asked it this.

### The simplest version of it

One account. A budget of **$100**. Twenty agents, each wanting to approve **$45**.

| | What the agent sees | What it decides |
|---|---|---|
| **Agent 1** | $0 spent, $100 left | approve $45 ✓ |
| **Agent 2** | $0 spent, $100 left — *it read before agent 1 wrote* | approve $45 ✓ |
| **Agent 3** | $0 spent, $100 left — *same* | approve $45 ✓ |

All three writes land. The account sits at **$135 against a $100 budget**, and
**every agent was right**, given what it saw. There is no bad decision to find
here, no bug to fix in any one agent, and no error raised anywhere.

That is three agents. Run all twenty and the total lands in the *hundreds* —
across 50 runs it went over the cap **45 times**, every one of them silently.

That is the whole problem in one table: correct agents, a correct database, and a
broken outcome. Swap dollars for seats, GPU hours or stock and nothing about it
changes.

The obvious fix is stronger isolation. We tried it, and found something more interesting: it isn't enough on its own.

## What it does

RaceLab is a **conflict-aware transaction wrapper**, the **benchmark that is its evidence**, and a **deployed policy-enforcing write gateway** for agent decisions.

The library knows nothing about money, seats, or stock. It knows about a read → reason → write cycle that another transaction can invalidate mid-flight. The caller supplies the resource, the rule, and the reasoning step.

Agents don't write to the ledger. They ask the gateway to write for them. Inside one transaction it reads state, invokes the reasoning step, enforces the rule **after the write and before the commit**, and answers with either a committed decision or a refusal that names the rule and the version of it.

Under `SERIALIZABLE`, the state you verified before committing is the state that becomes durable — or the commit is refused and the whole cycle runs again. **That placement is the entire design.** Checking outside the transaction is racy at any isolation level. Checking inside it is *still* racy under `READ COMMITTED`. Only serializable isolation lets a check performed before a commit still be true after it.

## The measurement

Five arms, five arrival windows, 10 runs per cell, 20 agents per run — **250 runs and 5,000 agent decisions** on a live CockroachDB Cloud cluster. The scenario here is a shared spending pool, because it is the easiest to stte in one line; the next section shows the same result in a completely different shape.

| Arm | | Over the cap | Policy limit breached | Conflicts raised |
|---|---|---|---|---|
| **A** | stock PostgreSQL, default isolation, naive | **45 / 50** | 50 / 50 | **0** |
| **A-rc** | CockroachDB, `READ COMMITTED`, naive | **48 / 50** | 50 / 50 | **0** |
| **B** | CockroachDB, `SERIALIZABLE`, naive retry | **47 / 50** | 47 / 50 | 1,660 |
| **C-ops** | + re-reason over fresh state | **0 / 50** | 49 / 50 | 662 |
| **C** | + refresh memory too | **0 / 50** | 28 / 50 | 506 |

Arm **B** is the point. It is given the signal — 1,660 serialization failures, correctly raised — and it **still oversubscribes the pool in 47 of 50 runs**, because the standard remedy is to retry the transaction, and the retry resubmits the answer that just went stale.

Change one boolean — on a conflict, throw the decision away and *decide again* against fresh state — and cap violations go to **zero at every timing**.

## It reproduces in a completely different shape

One hand-built scenario cannot distinguish *"we found something"* from *"we built something that produces this"*. So there is a second one, deliberately different along three axes:

- **counts of rows**, not a sum of amounts
- a **categorical** action — which *kind* of thing to grant — not a magnitude
- a correction that requires granting a **different kind** of thing, not less of the same thing

Twenty agents grant license seats. The org seat cap is a column; a separate rule caps *premium* seats, lives only in retrieved text, and drops from 5 to 2 partway through the run.

| Arm | Total seats | Premium seats | vs. the cap of `2` then in force |
|---|---|---|---|
| naive | 7 | **7** | breached |
| C-ops | 8 | **3** | breached — but within the `5` it remembered |
| C | 8 | **2** | held |

Arm C granted **more** seats than the naive arm while breaching nothing: 8 seats, of which only 2 premium. **It switched tier.** No numeric clamp produces that — which rules out the reading that re-reasoning merely shrinks a number, and shows the result is about the *decision*, not about arithmetic.

The same wrapper, the same memory store and the same vector index handled a categorical entitlement problem with nothing specialised for it.

## The part that makes this an agentic-memory problem

Look at the **C-ops** row again: `0/50` on the hard cap, but it still breaches the policy limit in `49/50`.

There are two rules in play, and they fail for completely different reasons:

| Rule | Lives in | Recovered by | Expressible in SQL? |
|---|---|---|---|
| the structural cap — total ≤ limit | a **column** | re-reading state | **yes** — a guarded `UPDATE` |
| the policy limit, changed mid-run | **retrieved text** | refreshing memory | **no** — a `WHERE` clause has no column to reference |

The second rule lives in a document, retrieved by vector search over CockroachDB's distributed vector index. **Re-reading the pool cannot recover it, because the pool was never wrong.** The rule moved, and the rule was never in the database.

> Re-reading state protects the rule in your database. Only refreshing memory protects the rule in your agent's head.

That is why CockroachDB is the memory layer here and not merely storage: operational rows, vector-indexed semantic memory, compiled policy and the audit trail are the *same transactional system*, which is what makes the guarantee provable rather than probable.

## Point it at your resource

The enforced resource is a declaration, not code — a table, the column that scopes the limit, the aggregate, where the cap lives, and the actions an agent may propose:

```yaml
resource:     refunds
scope_column: customer_id
aggregate:    SUM(amount)
hard_limit:   customers.refund_pool
policy_limit: compiled
actions:      [50, 100, 250]
```

`SUM(column)` covers pooled **quantities** — budget, stock, storage, hours. `COUNT(*)` covers pooled **slots** — seats, licences, concurrent sessions, bookings. Swap the table and the gateway enforces something it has never seen.

We proved that the hard way: we pointed the **unmodified deployed Lambda** at a `refunds` table under a policy compiled from that table's own document, and it enforced it — six concurrent writers, real contention, stopped at the compiled limit rather than the larger pool behind it. `grep -rn refund` finds nothing in the handler, and the test suite asserts that.

## How we built it

**CockroachDB Cloud** is the memory layer:
- operational tables — the state the cap is about
- `memories` — `VECTOR(1024)` + a distributed vector index, embedded with Bedrock Titan
- `policy_constraints` — compiled, versioned, fingerprinted policy
- `decisions` / `agent_attempts` / `race_runs` — the audit trail, each decision carrying the policy version it was made under
- SQL views shaped for an agent to query through CockroachDB's **Managed MCP Server**

**AWS** runs it: **Lambda** is the gateway, **Bedrock** supplies Titan embeddings and Claude Sonnet 4.5, **Secrets Manager** holds the DSN, **CloudWatch** takes one JSON record per decision with an alarm on cap violations, **S3** holds the layer artifacts.

The whole read — running total, hard cap, governing policy document, compiled constraint — happens in **one SQL statement inside the transaction**. That is not a latency trick, though it is one. It is what makes the rule provably read at the same timestamp as the state it constrains.

RaceLab is also **an MCP server**, so Claude Code, Cursor or any MCP client gains a write it cannot use to violate its own policy — and a `reconsider` result, which is the thing MCP has no vocabulary for: *your last answer came from state that has since changed; here is the new state, decide again.*

## The model compiles the rule; the database enforces it

Enforcement started by pulling a number out of policy text with a regular expression. That expresses exactly one thing and silently mis-enforces everything else:

| Real policy | The regex |
|---|---|
| *"capped at 250"* | ✅ |
| *"**tier-2** requests capped at 250"* | ❌ finds `250`, applies it to everyone |
| *"250 **per calendar month**"* | ❌ no time window |
| *"250, **excluding** anything already human-approved"* | ❌ no exclusions |

So interpretation moved to a compilation step: Claude reads the policy **once** and emits a structured `Constraint`, which is stored, versioned, and thereafter enforced deterministically inside the transaction with **no model in the loop**. Anything the language cannot express lands in `unsupported`, and an unsupported constraint **authorizes nothing** — a partly-enforced policy is worse than an unenforced one, because it looks safe.

## Challenges we ran into

**The compiler refused our own policy.** The first thing it did, wired into the write path, was return `UNENFORCEABLE` for *our* demo:

```
"Temporary authorization ceiling ... is 80 per billing cycle"
  -> a billing cycle can start on any day, so it is not a calendar month
  -> "pending the quarterly review" has no end date to compile
```

It is right both times. The regex read that same sentence and returned a confident `80`. We did **not** edit the corpus to make the demo work — that would have changed retrieval and invalidated the published sweep. Instead the gateway now refuses to authorize anything on that account until a human says what the rule means, and that reading is compiled, versioned and attributed. A worse demo. A much better system.

**A vector index that was present, correct, and completely unused.** The optimizer costed our filtered ANN query two ways and chose an ordinary secondary index every time — on a 5-row scope and a 1,200-row scope alike — because its cardinality estimate for the span was wildly low. Correct results, no error, no warning, and **no approximate nearest-neighbour search happening anywhere**. Dropping the redundant index makes it choose `• vector search` unaided. A prefix column is *necessary* for a filtered ANN query and *not sufficient*.

**Our own headline was confounded, and we withdrew it.** We had reported that surfacing a conflict to a client that ignores it is worse than not surfacing it. But that comparison spanned two different databases *and* two different network latencies — the same arm on the same seeds gave `196.0` on localhost and `344.0` in Docker. We added arm `A-rc` (CockroachDB at `READ COMMITTED`, same cluster) as an honest control, and the claim reversed: serializable isolation helps even a naive client. The retraction is in the repository.

**A falsification check that failed to fail.** Following our own rule — *a check that cannot fail is not a check* — we removed the guardrail to confirm the invariant could actually break. **It didn't.** A conflict-aware agent held the cap with no guardrail at all. That is not a broken check; it is the C-ops result arriving from another direction. The configuration that breaks it is the *naive* one. So the claim narrowed to what is actually true — the guardrail protects you from a *replaying* agent — instead of the looser version we were about to write.

**Pointing it at a second resource found a real bug.** The gateway took the first action that fit, which was greedy only because our binding happens to list its actions descending. A binding listing them ascending would have produced a *minimal* agent from the same code, under the same name, with no error anywhere — and the entire finding is about a *greedy* agent.

## Accomplishments we're proud of

- **`0/50` cap violations**, at every arrival window, from a one-flag change.
- **It generalises, and we tested that rather than asserting it** — a second scenario using counts and categories instead of sums and magnitudes, and a third resource enforced by the deployed gateway with no code written for it.
- **Deployed and least-privilege**: the Lambda role reads exactly one secret, invokes exactly two models, and publishes metrics in one namespace enforced by an IAM condition. Not `SecretsManagerReadWrite`. Not `BedrockFullAccess`.
- **The guarantee holds under interleavings nobody designed** — `hypothesis` generates agent counts, action spaces, limits, policy-change timings and arrival orders, and asserts three properties on each.
- **12 test suites**, and a methodology log that grades every prediction we pre-registered — including the two that were falsified.

## What we learned

**Instrument the mechanism, not just the outcome.** An early bug made the two experimental arms silently identical — both committed, both produced plausible totals, both wrote clean telemetry. The experiment would have reported "no significant difference": a clean, publishable, entirely false null result. No assertion on outcomes catches that, because the outcomes are exactly what a real null result looks like. Only counting *how many times the reasoning function actually ran* does.

> A collapsed experiment is indistinguishable from an honest refutation, and it fails in the direction that looks like integrity.

**State claims in the weaker form that is true.** Our property test asserts a total never exceeds the *highest cap ever in force* — not the current one. The stronger version is false, and we had already published why: **no protocol can revoke a valid commit.** An agent that legally committed under the old cap has not misbehaved when the cap later drops.

**And when a test fails, find out why before changing it.** Twice the *test* was wrong and the code was right. Once a "performance improvement" turned out to be a fast failure.

## What's next

Property-based coverage of the compiler itself; more binding shapes (time-windowed aggregates are partly in); and getting this in front of a real workload, because the honest weakness of this project is that the scenarios are still synthetic.

The skill we wrote about this failure mode is submitted upstream to `cockroachlabs/cockroachdb-skills` as PR #26.

---

## 4 · Built with

*(25 tag limit — 24 used, comma-separated for pasting)*

```
cockroachdb, aws-lambda, amazon-bedrock, claude, python, sql, psycopg3,
model-context-protocol, mcp, aws-secrets-manager, amazon-cloudwatch, amazon-s3,
vector-search, vector-database, embeddings, amazon-titan, serializable-isolation,
distributed-sql, langchain, hypothesis, boto3, docker, postgresql, yaml
```

---

## 5 · "Try it out" links

| Label | URL |
|---|---|
| **Live demo — race it yourself** | `https://racelab.fly.dev` |
| Static demo (works offline) | `https://rajj28.github.io/racelab/` |
| Source code | `https://github.com/rajj28/racelab` |
| Architecture | `https://github.com/rajj28/racelab/blob/main/docs/ARCHITECTURE.md` |
| Methodology log (every prediction, graded) | `https://github.com/rajj28/racelab/blob/main/docs/METHODOLOGY.md` |

---

## 5b · Image gallery

All four images are in **`docs/thumbnails/`**, already cropped to 3:2 and named
in upload order. See `docs/thumbnails/README.md` for what each one carries.

| Order | File | Role |
|---|---|---|
| 1 | `1-hook-thumbnail.png` | **thumbnail** — readable down to 300px |
| 2 | `2-architecture-transaction-boundary.png` | the mechanism |
| 3 | `3-results-five-arms.png` | the evidence |
| 4 | `4-memory-in-action.png` | CockroachDB memory, visibly |

Every one is a real screenshot of the running project — no generated art.

---

## 6 · Video demo link

⚠️ **YOU** — not yet recorded. Script is in `docs/VIDEO.md` (468 words, 2:50 at 165 wpm); shot-by-shot runbook in `docs/DEMO.md`.

```
https://youtu.be/________________
```

Must be **public or unlisted**, playable without a login, **under 3 minutes**. Upload early — then open your own Devpost page and watch it back from there.

---

# Additional info (judges and organizers)

## URL to your functional demo application

```
https://racelab.fly.dev
```

*(The static, offline-capable version is at <https://rajj28.github.io/racelab/> — use that one if the live app is ever down.)*

## Testing credentials or instructions

```
No credentials needed. Nothing to install. Open it and press Race.

https://racelab.fly.dev runs a REAL race: pick one of the four approaches, choose how
many agents and how tightly they arrive, and twenty-agent-style contention plays
out live against the same CockroachDB Cloud cluster every published number came
from. Each agent opens its own connection; the collisions are real serialization
failures; the rows land in a real table. Events stream to the browser over
Server-Sent Events as they happen.

Try this order, it takes ninety seconds:
  1. "Told, and ignores it"  -> goes hundreds of dollars over a $100 budget
  2. "Works it out again"    -> budget holds, the policy cap does not
  3. "Works it out and re-reads the notes" -> both hold
Set the arrival window to 0 ms for a thundering herd.

It races `demo-live-001`, an account seeded for this purpose and reset before
every run -- never the account the published measurements used, so a visitor
cannot corrupt the evidence. Runs are serialised (the cluster's connection
budget is ~30 and each race holds one per agent), so if someone else is racing
you get a clear "another race is running" rather than a failure.

The static demo at https://rajj28.github.io/racelab/ is fully self-contained -- every
number on the page came back from a live CockroachDB Cloud cluster and a local
PostgreSQL 16 instance, captured by scripts/capture_ui.py and embedded in the
page. It has no external requests and works offline. Pick any of the five
approaches to see inside that run; "Show all 20 agents" expands the timeline.

THE DEPLOYED WRITE GATEWAY is intentionally NOT publicly callable:

  https://3fbyij2xhlcb2cyjwlusfd6fza0bymsg.lambda-url.ap-south-1.on.aws/

Its Function URL uses AWS_IAM auth, so an unsigned request returns 403 by
design. This endpoint writes to a financial ledger; an open write endpoint is
not a demo convenience. (A public Function URL is additionally blocked by an
Organizations SCP on our account, which we consider correct.)

TO RUN THE FULL SYSTEM YOURSELF -- a CockroachDB connection string is the only
hard requirement, and this takes about five minutes:

  git clone https://github.com/rajj28/racelab && cd racelab
  pip install -r requirements.txt
  cp .env.example .env          # fill in RACELAB_CRDB_DSN (+ AWS creds for the
                                # Bedrock-dependent suites)
  python -m racelab.schema --backend crdb
  python scripts/seed.py --reset
  python scripts/test_all.py    # all 12 suites
  python scripts/test_all.py --skip-bedrock   # no AWS credentials needed

To exercise the write path locally, exactly as the deployed Lambda does:

  python scripts/compile_policies.py --account hero-001   # compile the policy
  python deploy/lambda_handler.py --account hero-001      # invoke the handler

To see it enforce a table the codebase has no code for:

  python scripts/test_binding.py --create
  python scripts/test_binding.py

PostgreSQL (arm A) is OPTIONAL -- arm A-rc is the READ COMMITTED control on the
same CockroachDB cluster, so the full comparison runs without it.

Happy to provide signed-request access or a live walkthrough on request.
```

## URL to your open source and public code repository

```
https://github.com/rajj28/racelab
```

## URL to your open-source license file

```
https://github.com/rajj28/racelab/blob/main/LICENSE
```

*(MIT, detected by GitHub and shown in the About section.)*

## Which CockroachDB tools are used?

- [x] **Cloud Managed MCP Server**
- [x] **Distributed Vector Indexing**
- [x] **ccloud CLI**
- [x] **Agent Skills Repo**

> **On the fourth box:** the first three are *consumed by* the project. The Agent Skills Repo is a **contribution back** — we authored a skill and submitted it upstream (issue #25 → PR #26). The explanation below says so plainly. If you would rather claim only what the project *depends on*, untick it; three already exceeds the required two.

## Which AWS Services are used?

- [x] **Amazon Bedrock**
- [x] **AWS Lambda**
- [x] **Amazon S3**
- [x] **Other AWS service** → *AWS Secrets Manager, Amazon CloudWatch, AWS IAM*

## How the components were meaningfully integrated

```
COCKROACHDB IS THE MEMORY LAYER, NOT THE STORAGE LAYER. The project's thesis is
about memory correctness under concurrency, so the database is the subject of
the experiment rather than a dependency of it.

DISTRIBUTED VECTOR INDEXING -- deeply load-bearing. The `memories` table is
VECTOR(1024) with a CREATE VECTOR INDEX ... vector_cosine_ops on
(account_id, embedding). Every agent, on every decision, retrieves the policy
that constrains it by meaning rather than keyword. The mid-run policy change
($80 -> $60) is a superseding row that retrieval must surface over the row it
supersedes -- that is what makes the memory refresh causally load-bearing rather
than a no-op, and it is the difference between arms C-ops and C in our results.

We also found and fixed a real cost-model issue here: with a redundant secondary
index on (account_id) present, the optimizer chose a scan over the vector index
every time -- on a 5-row account and a 1,200-row account alike -- producing
correct results, no error, no warning, and no ANN search anywhere. Dropping that
index makes the optimizer choose `• vector search` unaided.
scripts/verify_clean_clone.py asserts that on a FRESH database, so the finding
cannot silently regress. Written up as entry 1 of docs/FEEDBACK.md.

CLOUD MANAGED MCP SERVER -- connected and queried against a real session
(server `cockroachdb-cloud 1.0.0`, protocol `2025-06-18`, 12 tools). We
deliberately did NOT write our own inspection server, because the Managed MCP
Server already exposes select_query, explain_query, list_tables and
get_table_schema. What was missing was a SCHEMA SHAPED FOR AN AGENT TO QUERY, so
we built four SQL views (race_arm_comparison, race_run_summary,
race_agent_decisions, race_conflict_summary) designed around its documented
limits: responses cap at 10 KiB and SELECT defaults to LIMIT 25, so every view
is narrow and carries its own ORDER BY -- without that, an agent issuing
`SELECT * FROM v` gets 25 arbitrary rows out of a thousand instead of the 25
worth having. See scripts/mcp_query.py and docs/MCP.md.

CCLOUD CLI -- a control-plane preflight. Before launching a swarm of 20
concurrent agents, run_sweep.py shells out to ccloud to read the cluster's plan
and REFUSES TO START if the planned connection count exceeds the plan's measured
budget (~30 on Basic). This is a real guardrail we needed: 20 agents x (racing +
memory connection) = 40 exhausts it, which is why racing connections stay
per-agent while memory retrieval is pooled. Read-only, by explicit allowlist:
racelab/integrations/ccloud.py.

AGENT SKILLS REPO -- a contribution back, not a dependency. Reading
`designing-application-transactions` first revealed that two pieces of its
guidance are individually correct and, composed for an agent, produce a silent
bug: step 14 says keep RPC calls outside the transaction (right), step 3 says
retry the unit of work (right), and together they mean the retry re-executes the
write with the decision the model made against the PREVIOUS attempt's read. We
wrote `retrying-agent-decisions-under-contention` for that gap, validated it at
0 errors against the repository's own scripts/validate-spec.py, and submitted it
upstream: issue #25 -> PR #26 on cockroachlabs/cockroachdb-skills.

WE ARE ALSO AN MCP SERVER. racelab/integrations/mcp_server.py gives Claude Code,
Cursor, or any MCP client a guarded write it cannot use to violate the policy it
retrieved -- including `reconsider`, a first-class result for the case MCP has no
vocabulary for: "your last answer came from state that has since changed; here
is the new state, decide again." 29/29 checks, driven as a real MCP client over
stdio with five concurrent writers to force genuine contention.

--- AWS ---

AMAZON BEDROCK -- two models, two distinct jobs.
  * amazon.titan-embed-text-v2:0 (1024-dim) embeds every memory and every
    retrieval query. This is what the vector index indexes.
  * Claude Sonnet 4.5 does BOTH the agent reasoning (the model arm: 57/60
    agreement with our deterministic reference, and the memory-refresh effect
    reproduced within band for both providers) AND the policy compilation.
    Compilation is the more interesting use: the model reads a natural-language
    policy ONCE and emits a structured constraint, which the database then
    enforces with no model in the loop. That split removes a failure we
    measured -- Claude chose allocate(45) while writing "allocating $45 would
    bring the total to $80, which exceeds this ceiling" in the same response,
    3 times in 60. With compilation the model is not in the enforcement path at
    all, so it cannot smuggle a different reading of the rule into each
    decision. The bug class is gone rather than mitigated.

AWS LAMBDA -- the deployed write gateway (`racelab-gateway`, ap-south-1), behind
a Function URL with AWS_IAM auth. The unit of work is short, bursty and
stateless: read, reason, enforce, commit. State lives in CockroachDB, which is
the point -- the compute is disposable and the memory is not. One connection per
CONTAINER, held across warm invocations and never pooled: the TLS handshake
costs ~580ms and was the largest single term in a 3.5s response; a pool inside a
container is never used (Lambda gives each one concurrent request) while a pool
multiplied by AWS concurrency is precisely how a cluster's connection budget is
exhausted. Warm p50 is now 62ms, down from 3,543ms.

AWS SECRETS MANAGER -- holds the CockroachDB DSN, resolved once per container
and verified in use (responses report dsn_source: secretsmanager). The
credential is rotatable and scoped by IAM rather than sitting in an environment
variable.

AMAZON CLOUDWATCH -- one structured JSON record per decision, queryable in Logs
Insights, carrying what the agent observed, the constraint it was held to, the
action it chose, and the POLICY VERSION it was made under. Four metrics, with an
alarm on HardLimitViolations. Metric publication never raises: an observability
outage is not a correctness outage, and conflating them would let a logging
failure block a write the policy permits.

AMAZON S3 -- Lambda layer artifacts. Publishing via S3 rather than direct upload
is not incidental: the direct upload of a 5.5MB layer failed repeatedly with
ConnectionClosedError.

AWS IAM -- least privilege, written out. The runtime role has four permissions:
read ONE named secret, invoke TWO named models, write its own log group, and
publish metrics in ONE namespace (enforced by an IAM Condition). Not
SecretsManagerReadWrite. Not BedrockFullAccess. An agent gateway with broad
credentials is a worse liability than the race it was built to prevent.

--- AND THE INTEGRATION THAT MATTERS MOST ---

The running total, the hard limit, the governing policy document and the
compiled constraint are read in ONE SQL STATEMENT INSIDE THE TRANSACTION. That
is not a latency optimisation, though it is one. It is what makes the guarantee
provable: the rule a write is checked against provably shares a read timestamp
with the state it is checked over, and under SERIALIZABLE that timestamp is the
one the commit lands at -- or there is no commit. Reading the policy outside the
transaction would let it move between the check and the commit; reading it in a
second statement merely made them LIKELY to match.
```

## What date did you start this project?

```
08-17-26
```

*First commit `536e57c`, 2026-08-17 13:48 — verifiable in the public git history. 34 commits, 117 tracked files as of submission.*

## Pre-existing code or work incorporated

```
None. Every line in this repository was written during the submission period,
starting 08-17-26. The full commit history is public and unsquashed, so the
build order is auditable.

DISCLOSURES:

1. AI CODING ASSISTANT. The project was built with Claude Code (Claude Opus and
   Sonnet 4.5) as a pair-programming assistant throughout -- architecture,
   implementation, tests and documentation. Every design decision, experiment
   and retraction recorded in docs/METHODOLOGY.md was reviewed and directed by a
   human. This is disclosed in full rather than minimised.

2. STANDARD OPEN-SOURCE DEPENDENCIES, unmodified, from requirements.txt:
   psycopg3, boto3, python-dotenv, certifi, langchain-core (optional adapter
   only), mcp (optional server only), PyYAML, hypothesis. No vendored or
   modified third-party source.

3. NO STARTER TEMPLATE, no scaffold, no forked project. The one file that
   deliberately duplicates logic -- spike/conn.py, ~30 lines overlapping
   racelab/db.py -- is duplicated so the Phase 1 gate can be read and re-run as
   a self-contained script with no framework behind it. That is documented in
   the file itself.

4. PRIOR ART WE READ AND CREDIT: CockroachDB's own
   `designing-application-transactions` skill, which we cite in our upstream
   contribution as the thing that revealed the failure mode we then measured.
   No code was taken from it.
```

## Optional: architectural diagram

Upload the render produced from `docs/ARCHITECTURE_PROMPT.md`. `docs/ARCHITECTURE.md` also has a Mermaid version that renders on GitHub.

## Optional: feedback on the CockroachDB AI tools

```
Seven entries, written while building rather than reconstructed afterwards:
https://github.com/rajj28/racelab/blob/main/docs/FEEDBACK.md

The three most useful:

1. A VECTOR INDEX CAN BE PRESENT, CORRECT, AND SILENTLY UNUSED. Documented as
   CREATE VECTOR INDEX ... ON memories (embedding vector_cosine_ops), then
   queried with a WHERE account_id = $1 filter -- the shape essentially every
   real retrieval has. The optimizer satisfied the filter from an ordinary
   secondary index and scanned, at both 5 rows and 1,200 rows, because its
   cardinality estimate for the span was wildly low (it estimated 1 row for a
   span holding 1,200). Correct results, no error, no warning, no ANN search.
   Two lessons: a prefix column is NECESSARY for a filtered ANN query, and it is
   NOT SUFFICIENT -- the vector index competes on cost with every other index
   that could satisfy the filter, and often loses. A warning when a vector index
   exists but is not chosen for a query that could use it would have saved us
   the most time of anything in this project.

2. CLIENT-VISIBLE VS INTERNALLY-RETRIED 40001 IS HARD TO OBSERVE. CockroachDB
   knows which transactions it retried internally; the client cannot readily
   ask per-transaction. We wanted to report the transaction conflict graph as a
   measurement and had to settle for an INFERENCE from what committed during our
   window -- labelled `inferred_write_overlap` in our schema so nothing
   downstream mistakes it for a reading of the cluster's contention record.

3. CONNECTION COUNT IS THE BINDING CONSTRAINT FOR AGENT-SWARM WORKLOADS ON
   BASIC. The measured budget was ~30, which we discovered by exhausting it: 20
   agents x (racing + memory connection) = 40. Racing connections must stay
   per-agent to race at all, so memory retrieval had to be pooled. A documented
   per-plan connection number would have been worth a lot -- agent workloads are
   unusually connection-hungry compared to the request/response services these
   limits appear to be sized for.

Also entry 7, on Bedrock rather than CockroachDB: the `enum` in a tool's
inputSchema is guidance to the model, not a validation boundary. We measured a
model returning a value outside a declared enum, which is why our compiler
re-validates every field it gets back.
```

## Submitter type

```
Individual
```
⚠️ **YOU** — change if you're submitting as a team or organization.

## Submitter country of residence

```
India
```
⚠️ **YOU** — inferred from your environment; confirm.

## Organization name

```
(leave blank)
```

## Which AI tools have you leveraged?

```
Claude Code (Claude Opus and Claude Sonnet 4.5) -- used throughout as a
pair-programming assistant: architecture, implementation, test design,
documentation, and the analysis that produced two retracted claims.

Claude Sonnet 4.5 via Amazon Bedrock -- inside the product itself, in two
distinct roles: the agent reasoning step (the model arm of the experiment) and
the policy compiler, which reads a natural-language policy once and emits a
structured constraint the database enforces without a model in the loop.

Amazon Titan Text Embeddings V2 via Amazon Bedrock -- 1024-dimensional
embeddings for every memory and every retrieval query, indexed by CockroachDB's
distributed vector index.

CockroachDB Cloud's Managed MCP Server -- used with an MCP client to inspect the
experiment's own telemetry through SQL views built for that purpose.
```

## Level of learning derived

```
Significant / High
```
⚠️ **YOU** — pick whatever the dropdown's top option is. Justification if there's a free-text box:

> The most valuable lesson was methodological rather than technical: instrument the *mechanism*, not just the outcome. A bug made our two experimental arms silently identical — both committed, both produced plausible totals, both wrote clean telemetry — and the experiment would have reported a clean, publishable, entirely false null result. No assertion on outcomes catches that, because the outcomes are exactly what a real null result looks like. We also learned to state claims in the weaker form that is actually true, and published two retractions when our own data contradicted us.

## Did you gain AI value you can use in your career?

```
Yes
```

> Concretely: separating *interpretation* from *enforcement*. Asking a model to interpret a rule at every write is slow, non-deterministic, and lets a different reading authorize each decision — we measured Claude choosing an action while writing that the action exceeded the ceiling, in the same response, 3 times in 60. Compiling the rule once, into a structure a database enforces deterministically, removes that bug class rather than mitigating it. That pattern generalizes to any place an LLM's judgment currently sits in a hot path.

---

# ⚠️ Four things only you can supply

1. **Video URL** — the last hard requirement. `docs/VIDEO.md` + `docs/DEMO.md` are ready.
2. **Submitter type** — filled as *Individual*.
3. **Country** — filled as *India*.
4. **Architecture diagram** — generate from `docs/ARCHITECTURE_PROMPT.md`, export at 2×.

## Pre-submit checklist

- [ ] Video is **public or unlisted**, under 3:00, plays without a login
- [ ] Watch the video back **from your own Devpost page**, not from YouTube
- [ ] Repo is public, MIT license visible in the **About** sidebar
- [ ] `https://rajj28.github.io/racelab/` loads in a private window
- [ ] Gallery images are **3:2**, under 5 MB
- [ ] At least **2** CockroachDB tools and **1** AWS service ticked
- [ ] Submit early — you can keep editing until the deadline
