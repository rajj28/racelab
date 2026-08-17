# RaceLab — working context

Read this first. It is the durable state of the project: what it claims, what is
proven, what is deployed, what is broken, and what is next.

**Last updated:** 2026-08-18 · 30 commits · 107 tracked files · 10 test suites

---

## 1. What this is, in one line

**Serializable isolation tells your agent that something changed. It does not
tell it to think again. That gap is measurable — and it is where agents lose
money.**

And since the policy compiler landed, a second line carries equal weight:

**The model compiles the rule. The database enforces it.**

RaceLab is a conflict-aware transaction wrapper, the benchmark that is its
evidence, and a deployed policy-enforcing write gateway for agent decisions.

### The two invariants — the whole conceptual contribution

| Rule | Lives in | Recovered by | Expressible in SQL? |
|---|---|---|---|
| `SUM(allocations.amount) <= accounts.hard_limit` | a **column** | re-reading state | **yes** — a guarded `UPDATE` |
| the approval ceiling (`$80` → `$60` mid-run) | **retrieved text** | refreshing memory | **no** — a `WHERE` clause has no column to reference |

Row two is why this is an *agentic-memory* contribution and not a concurrency
library. Arm C-ops re-reads operational state perfectly, never breaks the hard
limit, and still breaches the withdrawn ceiling.

---

## 2. MANDATORY language rules

These are non-negotiable in code comments, UI copy, README, docs and the video
script. They have been enforced all session; do not regress them.

- Never write *"the other agent invalidated the reasoning."* Write **"another
  transaction changed state relevant to the decision."**
- Never write *"40001 means the agent was wrong."* A `40001` means **the
  transaction could not be serialized.** The semantic interpretation is our
  contribution.
- Never write *"Postgres is broken."* Postgres **has** SERIALIZABLE. We compare
  **default** isolation. Say **"READ COMMITTED permits this execution."**
- Never claim this is impossible elsewhere.
- Call it `conflict_edges` / "transaction conflict graph". **Never** "belief
  conflict graph."
- The library is the deliverable; the allocation scenario **demos** it. Invariant,
  memory refresh, operational read and re-reasoning are all **caller-injected**.
- `re_reason=True` is the public default. Naive is the opt-out, documented as
  modeling standard retry middleware — **not** a recommended setting.

---

## 3. The hackathon: CockroachDB × AWS

$8,750 · 1st $5,000 + blog feature · 2nd $2,500 · 3rd $1,250

### Hard requirements

| Requirement | Status |
|---|---|
| ≥2 CockroachDB tools, actually used | ✅ **3 of 4** (below) |
| ≥1 AWS service | ✅ **5** (below) |
| Public repo + detectable OSS license | ✅ MIT, detected in About |
| **URL to a functional demo app** | ✅ https://rajj28.github.io/racelab/ (Pages `built`) |
| **Video <3 min, public on YouTube/Vimeo** | ❌ **MISSING — user is doing this** |
| Identify CockroachDB tools + how | ✅ README "The stack" section |
| Identify AWS services + how | ✅ same section |
| *Optional:* architecture diagram | ✅ `docs/ARCHITECTURE.md` (Mermaid) |
| *Optional:* feedback on CockroachDB AI tools | ✅ `docs/FEEDBACK.md`, 7 entries |

### CockroachDB tools

| Tool | Status | Where |
|---|---|---|
| Distributed Vector Indexing | ✅ deeply used | `VECTOR(1024)` + `CREATE VECTOR INDEX … vector_cosine_ops`; found & fixed a cost-model bug where a redundant `(account_id)` index beat the vector index |
| ccloud CLI | ✅ used | `racelab/integrations/ccloud.py` — control-plane preflight before launching a swarm; read-only by allowlist |
| Managed MCP Server | ✅ **connected & queried** | `scripts/mcp_query.py` — real session, `cockroachdb-cloud 1.0.0`, protocol `2025-06-18`, 12 tools |
| Agent Skills Repo | ⚠️ **authored, NOT submitted** | `contrib/cockroachdb-skills/` — 0 errors against their validator; **no fork, no issue, no PR** |

We also **provide** an MCP server (`racelab/integrations/mcp_server.py`), so the
project is both an MCP client and an MCP server.

### AWS services

| Service | What it does |
|---|---|
| **Bedrock** | Titan `amazon.titan-embed-text-v2:0` (1024-dim) for retrieval; Claude Sonnet 4.5 for reasoning **and** policy compilation |
| **Lambda** | the write gateway, `racelab-gateway` in `ap-south-1` |
| **Secrets Manager** | the CockroachDB DSN — verified in use (`dsn_source: secretsmanager`) |
| **CloudWatch** | one JSON record per decision; 4 metrics; alarm on `HardLimitViolations` |
| **S3** | Lambda layer artifacts |

### Judging criteria — honest self-assessment

| Criterion | Assessment |
|---|---|
| Agentic Memory Design | **very strong** — CockroachDB *is* the memory layer; the thesis is about memory correctness |
| Technical Implementation | **very strong** — deployed, least-privilege, 3 tools used, 10 suites |
| Production Readiness | **very strong** — secrets, IAM scoping, alarm, preflight, 409 semantics, fail-closed policy |
| Creativity & Originality | **very strong** — `reconsider` as an MCP result; policy compilation |
| **Real-World Impact** | **medium — still the weakest.** Synthetic scenarios, no users. #2 below is the fix. |

---

## 4. Startup procedure

**A CockroachDB connection string is the only hard requirement.**

```bash
pip install -r requirements.txt
cp .env.example .env            # fill in RACELAB_CRDB_DSN + AWS creds

python -m racelab.schema --backend crdb
python scripts/seed.py --reset
python scripts/test_all.py                  # all 10 suites
python scripts/test_all.py --skip-bedrock   # no AWS needed
python scripts/test_all.py --only guardrail
```

Arm A (stock PostgreSQL) is **optional** — arm `A-rc` is the READ COMMITTED
control on the same CockroachDB cluster.

```bash
docker compose up -d            # PostgreSQL 16; prefer this
python -m racelab.schema --backend pg
```

**Do not use `scripts/pg_portable.py` unless Docker is unavailable.** The portable
binaries share the console process group, so a `Ctrl-C` anywhere kills the server
mid-run (`WAL writer process … 0xC000013A`). We lost a sweep to it.

### The experiment

```bash
python scripts/run_sweep.py --runs 10 --agents 20 \
  --windows 400 1000 1500 2500 4000 --arms A A-rc B C-ops C --out sweep.md
python scripts/render_sweep.py results/sweep_controlled.json --out sweep.md
python scripts/model_arm.py --runs 10        # Claude at 2 matched windows
```

`run_sweep.py` runs a **ccloud preflight** first and refuses if planned
connections exceed the plan's measured budget.

### Gateway and MCP

```bash
python deploy/lambda_handler.py --account hero-001   # local invoke
python deploy/invoke.py --demo --reset               # SigV4 against deployed
python scripts/mcp_query.py                          # query via CRDB's MCP server
python -m racelab.integrations.mcp_server --allow-writes   # OUR MCP server
```

---

## 5. Database

**Cluster:** `blast-avocet` · CockroachDB **v26.2.5** · plan `SERVERLESS`
(reported; = Basic) · region **`aws-ap-south-1`** · database `racelab`

A second cluster `liquid-shark` (us-east-1, empty) exists as a side effect of
`ccloud quickstart`. Harmless — and it usefully exercises the multi-cluster path.

**Connection budget ≈ 30.** Measured, not documented. 20 agents × (racing +
memory connection) = 40 exhausts it. Racing connections must stay **per-agent**;
memory retrieval is pooled (`ConnectionPool`, size 6).

### Tables

| Table | Purpose | Backends |
|---|---|---|
| `accounts`, `allocations` | operational state the invariant is about | both |
| `race_runs`, `decisions`, `agent_attempts`, `conflict_edges` | telemetry | both |
| `memories` | semantic memory, `VECTOR(1024)` + vector index | **CRDB only** |
| `policy_constraints` | compiled, versioned policy | **CRDB only** |
| `seats`, `orgs` | second scenario | CRDB only |

**Two schema landmines, both deliberate:**

1. **No secondary index on `memories(account_id)`.** Adding it back silently
   disables ANN search — the optimizer estimates 1 row for a 1,200-row span and
   prefers the ordinary index. `_create_vector_index` explicitly `DROP INDEX IF
   EXISTS memories_account_idx`, and `verify_clean_clone.py` asserts the vector
   index is chosen unaided on a fresh database.
2. **`memories` is CRDB-only on purpose.** Retrieval must be identical across
   arms, or outcome differences could be attributed to retrieval quality.

TLS: `sslrootcert=system` has no trust store on Windows. `racelab.db.normalize_tls`
substitutes the certifi bundle. **Any new code path that builds a DSN must call
it** — the Lambda gateway failed its handshake for exactly this reason.

---

## 6. AWS deployment

```bash
python deploy/deploy.py --check                       # probes, creates nothing
python deploy/deploy.py --region ap-south-1 \
  --layer arn:aws:lambda:ap-south-1:946298554578:layer:racelab-psycopg:1
python deploy/deploy.py --region ap-south-1 --destroy
```

Account **946298554578**, IAM user `ruturajsonkamble`.

### Live resources

| Resource | Value |
|---|---|
| Lambda | `racelab-gateway` in **ap-south-1** |
| Function URL | `https://3fbyij2xhlcb2cyjwlusfd6fza0bymsg.lambda-url.ap-south-1.on.aws/` |
| URL auth | **`AWS_IAM`** — unsigned `curl` gets 403 by design |
| Layer | `racelab-psycopg:1` (ap-south-1) — psycopg3 + **certifi + python-dotenv** |
| Secret | `racelab/crdb-dsn` (ap-south-1) |
| Role | `racelab-gateway-role-ap-south-1` |
| Alarm | `racelab-hard-limit-violation` |
| Buckets | `racelab-artifacts-946298554578`, `racelab-artifacts-ap-south-1-946298554578` |

### Rebuilding the layer

```bash
rm -rf build/layer && mkdir -p build/layer/python
pip install "psycopg[binary]>=3.1" "certifi>=2024.2.2" "python-dotenv>=1.0" \
  --platform manylinux2014_x86_64 --only-binary=:all: \
  --python-version 3.12 --implementation cp --target build/layer/python
# zip build/layer as python/…, upload to S3, publish_layer_version from S3
```

Publish **via S3**, not a direct zip upload — the direct upload of 5.5 MB failed
repeatedly with `ConnectionClosedError`. `build/` is gitignored.

### IAM

**Deployment permissions** — `deploy/iam-policy.json`, attached to the user as
inline policy `racelab-deploy`. IAM actions are scoped to
`arn:aws:iam::*:role/racelab-gateway-role*`.

**Runtime role** — created by the deploy script, four permissions only: read
**one** named secret, invoke **two** named models, write its own log group,
publish metrics in **one** namespace (enforced by an IAM `Condition`). Not
`SecretsManagerReadWrite`, not `BedrockFullAccess`.

**Roles are region-suffixed.** IAM is global, the policy is not — deploying to a
second region called `put_role_policy` on the *same* role with ARNs scoped to the
new region and silently revoked the first region's access to its own secret. It
then failed every request with a 500, fast enough that a latency benchmark read
the failure as an improvement.

### Two AWS gotchas

- **Reserved concurrency cannot be set.** The account's total concurrency limit is
  **10**, and AWS refuses a reservation that would drop unreserved capacity below
  its minimum. The account ceiling is currently doing the job and is *stricter*
  than the 8 requested — safe **by accident**. **Re-run the deploy after any
  concurrency limit increase**, which is exactly when nobody would think to.
- **Public function URLs are blocked**, almost certainly by an Organizations SCP.
  `AuthType: NONE` returned 403 despite a correct resource policy. The SCP is
  right: this endpoint writes to a ledger.

---

## 7. Results and claims — use these numbers

Primary metric is a **rate**, not a mean. Means move with deployment latency and
action space; rates do not.

**Controlled sweep** — 5 arms × 5 windows × 10 runs = **250 runs, 5,000 agent
decisions** (`results/sweep_controlled.md`, raw `.json`):

| Arm | Over the hard limit | Policy breached | Conflicts |
|---|---|---|---|
| A postgres RC, naive | 45/50 | 50/50 | **0** |
| A-rc cockroach RC, naive | 48/50 | 50/50 | **0** |
| B cockroach SERIALIZABLE, naive | 47/50 | 47/50 | 1,660 |
| **C-ops** + re-reason fresh state | **0/50** | 49/50 | 662 |
| **C** + refresh memory | **0/50** | 28/50 | 506 |

**Model arm** (Claude Sonnet 4.5, 2 matched windows): 57/60 agreement with the
reference; memory-refresh effect `−31.5` inside the band for **both** providers.

**Gateway performance:** warm **62 ms** p50 (was 3,543 ms) · cold 1,784 ms.

### Claims that were RETRACTED — do not reuse them

- ❌ *"Surfacing a conflict to a client that ignores it is worse than not
  surfacing it"* (`B − A` positive). **Confounded** — arm A is a different
  database *and* latency. Same arm, same seeds gave `196.0` on localhost and
  `344.0` in Docker, and `B − A` flips sign across windows. Controlled,
  `B − A-rc` = **−418.5, −49.5, −4.0, +18.0, −67.5**: serializable isolation
  helps even a naive client. METHODOLOGY entry 15.
- ❌ *"C-ops ends at exactly $80.00, zero variance across nine cells."*
  Arithmetic we chose (`45+35=80`), and the controlled sweep found `78.0`.
  The general claim — a greedy agent fills to the cap it *remembers* — is tested
  across random action spaces: C-ops totals `67, 68, 73, 74, 76`. Entry 16.
- ❌ *"Memory refresh guarantees the current ceiling holds."* It is a **rate**:
  C-ops 14/14 breaches vs C 7/14. Halved, not eliminated — a commit made legally
  before the policy moved cannot be revoked.
- ❌ The **shape** prediction (non-monotonic) was **falsified**. The boundary
  prediction held. Both are reported.

### Scope limits to state, never hide

- **No protocol can revoke a valid commit.** At wide windows C breaches the
  ceiling because the sum reached `$80` before the policy moved.
- The library does **not** help when a retry would produce the same correct
  result — idempotent writes, decisions with no dependency on what was read.
- If the rule fits in a `WHERE` clause, **push it into SQL** and skip all of this.

---

## 8. Test suites (10)

```
schema        verify_clean_clone.py       vector index chosen unaided on a fresh DB
wrapper       test_wrapper.py             23 checks; arms differ by one flag; collapse guard
memory        test_memory_causality.py    retrieval is index-backed and causal
arms          test_arms.py                four arms, ablation decomposed
guardrail     test_guardrail.py           16 — constraint enforced, not observed
action-space  test_action_space.py        finding survives random action spaces
seats         test_scenario_seats.py      9 — second scenario: counts + categorical
policy        test_policy_compiler.py     36 — compiler capability, fail-closed, injection
mcp-server    test_mcp_server.py          18 — driven as a real MCP client over stdio
langchain     test_langchain.py           9 — the tool re-decides instead of replaying
```

**Do not run benchmarks while the suite is running.** A ledger reset to zero read
back `67` and the ceiling flip-flopped `60/80` because the suite writes to
`hero-001` and deletes `hero-m5`. I nearly "fixed" a non-existent code bug.

---

## 9. Active bugs and known issues

| # | Issue | Severity |
|---|---|---|
| 1 | **Two orphaned IAM roles** — `racelab-gateway-role` (legacy) and `racelab-probe-delete-me` (my permission probe). `iam:DeleteRole` is not granted, so **the user must delete these in the console.** | cosmetic, but it's our mess |
| 2 | **The gateway and the MCP server still use the regex ceiling**, not the compiled constraint from `racelab/policy.py`. The compiler exists and is tested but is **not wired into either write path.** | the main follow-through |
| 3 | `race_conflict_summary` returns **0 rows** — `conflict_edges` is only written by `SqlTelemetry`, which no reported run used. Documented, not hidden. Do **not** synthesise edges to fill it. | by design |
| 4 | Arm A is absent from CockroachDB-side views — `_record_run` writes to the arm's own backend and arm A runs on PostgreSQL. | by design |
| 5 | `mcp-server` suite flaked once under `test_all` (all 5 racers can lose). Round is now retried; watch for recurrence. | low |
| 6 | The UI (`docs/index.html`) has **never been visually reviewed by a human.** Pages is `built` at https://rajj28.github.io/racelab/ | worth one look |
| 7 | The compiler is **non-deterministic on genuinely ambiguous policy text**. Mitigated by the language declaring defaults; a fingerprint-stability test guards it. | inherent, documented |

---

## 10. Next steps, in order

### Immediate — the follow-through on #1

**Wire the compiler into the write paths.** `racelab/policy.py` is tested but
unused in production code. Replace the regex ceiling in:
- `deploy/lambda_handler.py` — `_read_state` / `constraint`
- `racelab/integrations/mcp_server.py` — same
Load the current compiled constraint from `policy_constraints`, and **fail closed
when it is unenforceable**. Add `policy_version` to the response and to
`decisions`, so a decision traces to the policy version it was made under.

### #2 — Declarative resource binding *(biggest remaining lever on Real-World Impact)*

A spec so the gateway enforces **any** table without code:

```yaml
resource: refunds
scope_column: customer_id
aggregate: SUM(amount)
hard_limit: customers.refund_pool
policy_limit: compiled
actions: [50, 100, 250]
```

This is what converts "here is our scenario" into "point it at yours". ~1–2 h.

### #3 — Property-based concurrency proof

`hypothesis` generating random agent counts, arrival orders, amounts and
policy-change timings, asserting **the invariant never breaks and a refused
action never commits.** Upgrades the guarantee from "tested in scenarios" to
"holds under interleavings we did not design." ~1 h.

### #4 — Policy version on every decision

`decisions.policy_version` + the version in the `reconsider` payload. Answers
*"which decisions were made under the old cap?"* — nearly free once the compiler
is wired in. ~45 min.

### Documentation sweep

- **README**: fold in the compiler + `reconsider`; verify every number matches
  §7; make sure no retracted claim survives anywhere.
- **`docs/ARCHITECTURE.md`**: add the compiler to the diagram and failure table.
- **`docs/index.html`** (Pages): regenerate via `capture_ui.py` → `build_ui.py`
  after any results change; it currently shows the older 4-arm capture.
- **`docs/METHODOLOGY.md`**: entry 17 for the compiler (the two compiler findings
  are already written in the commit message and need a home here).
- **`docs/VIDEO.md`**: 529 words / ~3:32; the recommended cut is the `2:05`
  section. Consider adding the compiler to the reveal.
- **`docs/MCP.md`** / **`MCP_SERVER.md`**: cross-link them; one is us as client,
  the other us as server.

### Awaiting the user

- **Video** — script ready in `docs/VIDEO.md`.
- **Upstream PR** to `cockroachlabs/cockroachdb-skills` — skill validated at 0
  errors in `contrib/`. Needs: proposal issue → fork → branch
  `add-skill/cockroachdb-application-development/retrying-agent-decisions-under-contention`
  → validate → PR. **Not started; it is the one irreversible outward action.**
- Delete the two orphaned IAM roles.

---

## 11. How to work on this

The standard this project holds itself to, which is most of why it is credible:

- **Pre-register predictions**, then grade them. Two were falsified and both are
  published.
- **Instrument the mechanism, not just the outcome.** The arm-collapse guard
  caught a real bug that every final-state assertion passed. *"A collapsed
  experiment is indistinguishable from an honest refutation, and it fails in the
  direction that looks like integrity."*
- **Void runs rather than reporting them.** A failed policy update makes arm C
  indistinguishable from C-ops; that run is voided, not averaged in.
- **No silent fallback.** A malformed model response raises. A quiet substitution
  would report our arithmetic as the model's.
- **When a test fails, find out why before changing it.** Twice the *test* was
  wrong, not the code — and once a "performance improvement" was a fast failure.
- **Check what a call returned, not just how long it took.**
- **Record what would have falsified a claim.** A check that cannot fail is not a
  check.
