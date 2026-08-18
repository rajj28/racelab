# RaceLab — working context

Read this first. It is the durable state of the project: what it claims, what is
proven, what is deployed, what is broken, and what is next.

**Last updated:** 2026-08-18 · 31 commits · 113 tracked files · 12 test suites

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
| **URL to a functional demo app** | ✅ **https://racelab.fly.dev** — live, races the real cluster. Static fallback: https://rajj28.github.io/racelab/ |
| **Video <3 min, public on YouTube/Vimeo** | ❌ **MISSING — user is doing this.** Script: `docs/VIDEO.md` (462 words, 2:48 at 165 wpm). Runbook: `docs/DEMO.md` |
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
| Agent Skills Repo | ✅ **SUBMITTED 2026-08-18** | proposal [issue #25](https://github.com/cockroachlabs/cockroachdb-skills/issues/25) → [PR #26](https://github.com/cockroachlabs/cockroachdb-skills/pull/26), branch `add-skill/application-development/retrying-agent-decisions-under-contention`. 0 errors against their validator; 1 warning that is a false positive (the gerund heuristic reads the *trailing* word, and fires on several already-merged skills) |

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
| Technical Implementation | **very strong** — deployed, least-privilege, 3 tools used, 12 suites |
| Production Readiness | **very strong** — secrets, IAM scoping, alarm, preflight, 409 semantics, fail-closed policy, five policy states of which four refuse |
| Creativity & Originality | **very strong** — `reconsider` as an MCP result; policy compilation; the compiler refusing our own policy |
| **Real-World Impact** | **medium-to-strong.** Still synthetic and still no users, but declarative resource binding (§12) makes it "point it at your table" rather than "here is our scenario", and the property proof covers interleavings nobody designed. |

---

## 4. Startup procedure

**A CockroachDB connection string is the only hard requirement.**

```bash
pip install -r requirements.txt
cp .env.example .env            # fill in RACELAB_CRDB_DSN + AWS creds

python -m racelab.schema --backend crdb
python scripts/seed.py --reset
python scripts/test_all.py                  # all 12 suites (~430 s)
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

**Both write paths refuse to authorize until the account's policy is compiled.**
That is by design (§12) — run this first, or every call comes back `409` with
`policy_status: "uncompiled"`.

```bash
python scripts/compile_policies.py --show            # what governs what
python scripts/compile_policies.py --account hero-001

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
| `customers`, `refunds` | the declared-binding demo resource (§12); created by `test_binding.py --create`, **not** in `DROP_ORDER` | CRDB only |

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
| **Live demo app** | **https://racelab.fly.dev** — Fly.io, region `sin`, one machine, app `racelab` |
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
guardrail     test_guardrail.py           26 — constraint enforced; all 7 gate states
action-space  test_action_space.py        finding survives random action spaces
seats         test_scenario_seats.py      9 — second scenario: counts + categorical
policy        test_policy_compiler.py     36 — compiler capability, fail-closed, injection
binding       test_binding.py             31 — the gateway enforces a table with no code
mcp-server    test_mcp_server.py          29 — real MCP client; compiled policy; stale refuses
langchain     test_langchain.py           9 — the tool re-decides instead of replaying
property      test_property_concurrency.py hypothesis: P1/P2/P3 + a falsification check
```

Whole suite last run **2026-08-18: 12/12 PASS** (~430 s in one foreground run).
`binding` and `mcp-server` need Bedrock; `property` does not (it constructs
constraints rather than compiling them).

**Run the suite in the FOREGROUND, and touch nothing else while it runs.** A
backgrounded `test_all.py` reported 8 of 12 failing, with `arms` at 10,930 s and
`guardrail` at 14,351 s — times that are not real work. Every one of those suites
passed when re-run individually in the foreground. Two causes, both avoidable:
the concurrent gateway calls being made against the same cluster (the §8 warning
below, which applies to the gateway as much as to benchmarks), and a backgrounded
run whose subprocess output does not reach the log, so there is nothing to
diagnose from. A failure you cannot see the output of is not a result.

**Do not run benchmarks while the suite is running.** A ledger reset to zero read
back `67` and the ceiling flip-flopped `60/80` because the suite writes to
`hero-001` and deletes `hero-m5`. I nearly "fixed" a non-existent code bug.

---

## 9. Active bugs and known issues

| # | Issue | Severity |
|---|---|---|
| 1 | **Two orphaned IAM roles** — `racelab-gateway-role` (legacy) and `racelab-probe-delete-me` (my permission probe). `iam:DeleteRole` is not granted, so **the user must delete these in the console.** | cosmetic, but it's our mess |
| 2 | ~~The gateway and MCP server use the regex ceiling~~ **FIXED 2026-08-18.** Both write paths resolve policy through `racelab/policy_gate.py`. See §12. | resolved |
| 3 | `race_conflict_summary` returns **0 rows** — `conflict_edges` is only written by `SqlTelemetry`, which no reported run used. Documented, not hidden. Do **not** synthesise edges to fill it. | by design |
| 4 | Arm A is absent from CockroachDB-side views — `_record_run` writes to the arm's own backend and arm A runs on PostgreSQL. | by design |
| 5 | `mcp-server` suite flaked once under `test_all` (all 5 racers can lose). Round is now retried; watch for recurrence. | low |
| 6 | The UI (`docs/index.html`) has **never been visually reviewed by a human.** Pages is `built` at https://rajj28.github.io/racelab/ | worth one look |
| 7 | The compiler is **non-deterministic on genuinely ambiguous policy text**. Mitigated by the language declaring defaults; a fingerprint-stability test guards it. | inherent, documented |

---

## 10. Next steps, in order

**Items #1–#4 below were completed on 2026-08-18. See §12 for what they became,
including the three findings they produced. What follows is the remaining work.**

### Documentation sweep

- ✅ **README**: compiler, the policy gate's five states, resource binding and
  the property test are all folded in; layout table and quickstart updated.
- ✅ **`docs/METHODOLOGY.md`**: entry 17 written — four findings, including the
  falsification check that failed to fail.
- ✅ **`docs/MCP_SERVER.md`**: "which rule is enforced", 29/29, binding scope.
- ✅ **`docs/ARCHITECTURE.md`**: `policy_gate.py` and `binding.py` in the
  diagram, a five-state table, and six new failure paths.
- ❌ **`docs/index.html`** (Pages): regenerate via `capture_ui.py` →
  `build_ui.py`; still the older 4-arm capture.
- ❌ **`docs/VIDEO.md`**: 529 words / ~3:32; the recommended cut is the `2:05`
  section. The strongest new material for the reveal is *"the compiler refused
  our own policy"* — see §12.
- ❌ **`docs/MCP.md`** / **`MCP_SERVER.md`**: cross-link them; one is us as
  client, the other us as server.

### Awaiting the user

- **Video** — script ready in `docs/VIDEO.md`, runbook in `docs/DEMO.md`. **The
  last hard requirement still outstanding.**
- ~~**Upstream PR** to `cockroachlabs/cockroachdb-skills`~~ **DONE 2026-08-18.**
  [Issue #25](https://github.com/cockroachlabs/cockroachdb-skills/issues/25) → [PR #26](https://github.com/cockroachlabs/cockroachdb-skills/pull/26).
  Working clone at `~/work/cockroachdb-skills` with
  `upstream` remote configured. Note the branch convention is
  `add-skill/<domain-without-prefix>/<skill>` — *not* the
  `cockroachdb-`-prefixed form previously written here; the directory keeps the
  prefix, the branch does not. **Watch for review comments; push new commits
  rather than force-pushing, per their guide.**
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

---

## 12. The policy gate, resource binding and the property proof (2026-08-18)

§10 items #1–#4, done. Four new/changed modules, two new suites, three findings.

### What was built

| File | What it is |
|---|---|
| `racelab/policy_gate.py` | **New.** Resolves which compiled constraint governs a write. Shared by *both* write paths so they cannot drift. |
| `racelab/binding.py` | **New.** Declarative resource binding: the enforced table is a YAML declaration, not SQL in the handler. |
| `bindings/allocations.yaml` | **New.** Our own scenario, declared. Load-bearing — the demo runs the general path. |
| `bindings/refunds.yaml` | **New.** A resource with no code anywhere. |
| `scripts/compile_policies.py` | **New.** Compile a policy once, off the write path, versioned. `--show`, `--resolve`, `--accept-unenforceable`. |
| `scripts/test_binding.py` | **New suite**, 31 checks. |
| `scripts/test_property_concurrency.py` | **New suite**, `hypothesis`. |
| `deploy/lambda_handler.py` | Regex ceiling removed; reads the gate; `binding` in the request. |
| `racelab/integrations/mcp_server.py` | Same gate. Also now writes `SqlTelemetry` — it never did, so `audit_decisions` read a table its own writes never reached. |
| `racelab/schema.py` | `decisions.policy_version` (+ `ALTER … IF NOT EXISTS`); views dropped before create so column changes propagate. |

### The five policy states — memorise this table

| State | Write | Meaning |
|---|---|---|
| `none` | ✅ | no policy document; only the hard limit binds |
| `compiled` | ✅ | current, enforceable, compiled from the governing document |
| `uncompiled` | ❌ | a policy exists, nothing compiled from it |
| `stale` | ❌ | the document moved and nobody recompiled |
| `unenforceable` | ❌ | clauses the constraint language cannot express |
| `mismatched` | ❌ | compiled for a different resource |
| `not_in_force` | ✅ | dated policy outside its window; hard limit only |

`AUTHORIZING` is an **allowlist**, so a state added later fails closed by default.

**The governing constraint is the one compiled from the document in force, not
the newest version row.** Keyed on recency, a reverted policy would leave a newer
version in charge of a document it never read.

The hard limit is checked **first and always**, including in refusing states.

### Three findings — these are the reportable ones

1. **Our own hero policy does not compile.** `"$80 per billing cycle, pending the
   quarterly review"` and `"reduced to $60 per billing cycle"` are both
   `unenforceable` — a billing cycle can start on any day. The regex read the
   same sentences and produced a confident number. **The corpus was NOT edited**
   (that would change embeddings → retrieval → the sweep); `--resolve` records an
   operator's reading instead, versioned and attributed.
2. **The falsification check failed to fail.** Removing the guardrail from a
   *conflict-aware* agent left the invariant intact — the C-ops result appearing
   from another direction. The configuration that breaks P1 is the **naive** one:
   `$270` vs a `$60` limit with the guardrail off, `$45` with it on. So the claim
   is *the guardrail protects you from a replaying agent*, not the looser
   "the guardrail keeps the invariant."
3. **A second resource found a real bug.** The handler took the *first* action
   that fit — greedy only because `allocations` lists `[45, 40, 35]` descending.
   An ascending binding would have produced a *minimal* agent from the same code,
   with no error anywhere. Fixed with `sorted(..., reverse=True)`.

### P3 is deliberately weak

*"A total never exceeds the highest ceiling that was ever in force"* — not "the
current ceiling". The stronger form is false and §7 already says so: no protocol
can revoke a valid commit. Do not "strengthen" it.

### Operating notes

```bash
python scripts/compile_policies.py --show
python scripts/compile_policies.py --account hero-001          # -> unenforceable
python scripts/compile_policies.py --account hero-001 --resolve "<what it means>"
python deploy/lambda_handler.py --account hero-001 --binding allocations
python scripts/test_binding.py --create        # build the refunds demo tables
python scripts/test_property_concurrency.py --examples 40 --random
```

- **hero-001 currently has v1 (from `hero-m5`, $60) and v2 (from `hero-m1`, $80)
  compiled.** Both resolutions are operator wording. Flipping the hero update
  with `seed.py --apply/--revert-hero-update` switches which one governs, with no
  recompile needed — that is the document-keyed lookup working.
- New tables: `customers`, `refunds` (demo resource), created by
  `test_binding.py --create`. They are **not** in `schema.py`'s `DROP_ORDER`.
- `scenario/decide.py` **still uses the regex, deliberately.** It is the
  reference agent — the independent variable — modelling an agent reading a
  document. Only the *enforcement* path changed. Do not "fix" it.
- `hypothesis` and `pyyaml` added to `requirements.txt`, both optional.
- The Lambda gateway does **not** write `decisions` rows; its audit trail is
  CloudWatch, which now carries `policy_version` and `policy_status`. The MCP
  server writes `decisions`. Both carry the version; the surfaces differ.

### Deployment — read before touching the handler

**`build_package()` no longer just zips five files, and the reason matters.**
Wiring the compiler in added three transitive imports, and `PACKAGE_MODULES` was
not updated. Nothing would have caught it: the tests import from the repo, where
every module is present. The first sign would have been an `ImportError` on a
cold start of the deployed function.

So the build now **verifies the real condition**: it extracts the zip, puts only
that directory on `PYTHONPATH`, blocks `yaml` (the layer has psycopg, certifi and
python-dotenv — no PyYAML), and imports the handler in a fresh interpreter. A
missing module fails the *build*. Proven to fail: dropping `policy_gate.py` from
the list refuses the build with the offending import line.

**Bindings are converted to JSON at package time.** Authored as YAML because a
spec someone edits should be readable; shipped as JSON because the layer has no
YAML parser and rebuilding it for a flat mapping is not worth the ceremony.
`ResourceBinding.load` tries `.yaml`, `.yml`, `.json` in that order, so the
deployed function finds the JSON and never imports a parser.

**The deployed function has not been redeployed with any of this.** Run
`python deploy/deploy.py --region ap-south-1 --layer <arn>` when you want the
live gateway to match.

### Fixed in the same pass

| Was | Now |
|---|---|
| Deployment package missing `policy.py`, `policy_gate.py`, `binding.py`, bindings | shipped, and the build verifies it in a clean interpreter |
| Only 3 of 7 gate states tested | all of them — `test_guardrail.py` group 5, **26/26**, no Bedrock needed |
| Unknown scope silently got a `$100` budget | raises; both write paths pass no fallback. An unknown `account_id` gets a `409` |
| `customers`/`refunds` not in `DROP_ORDER` | dropped on `--drop` |
| Dead `read_hard_limit()` | removed; `hard_limit_sql()` is now the single definition the gate folds into its CTE |
| `governing_text` stored and never read | surfaced in a refusal, so a `409` quotes the document that needs compiling |
| `docs/ARCHITECTURE.md` unaware of the new modules | diagram, the five-state table and 6 new failure paths |

### Known gaps that remain

- Only `hero-001`, `mcp-demo-001`, `cust-demo-001` and `gate-states-001` have
  compiled policies. The six `exp-*` accounts do not — harmless, since the sweep
  does not use the gate, but `compile_policies.py --all` would report six
  unenforceable "per billing cycle" policies.
- `docs/index.html` still shows the older 4-arm capture.
- `docs/VIDEO.md` does not mention the compiler or the gate.

---

## 13. Submission pass (2026-08-18) — docs, video, UI

### The one that mattered: the video script asserted retracted claims

`docs/VIDEO.md` — the artefact a judge sees *first* — was still built on two
claims §7 lists as **RETRACTED**:

- *"And it still went over budget… Worse than PostgreSQL, on average."* (`B − A`,
  confounded, entry 15)
- *"Exactly eighty… nine measurements, zero variance."* (entry 16)

A judge who watched the video and then read the README would have found our own
repository contradicting it. **Rewritten from scratch.** The retracted lines are
kept in a table at the top of the file, labelled, so an old take cannot creep
back in unnoticed.

New script: **462 spoken words → 2:48 at 165 wpm.** Word count is measured by the
command at the bottom of the file, not estimated — three earlier drafts all
*felt* like three minutes and ran over four. Structure follows the hackathon's
own video guidance: problem in one sentence, live terminal by `0:16`, AWS and
CockroachDB named on screen, memory shown being stored/retrieved/acted on rather
than narrated.

### `docs/DEMO.md` — new

Beat-by-beat runbook: the exact command, the real expected output, and what to
point at. Every block was copied from a run against the live cluster. Also
carries the pre-record checklist, the "if it breaks on camera" table, and the
**arm A answer** (below), which a judge is likely to ask.

### Two real UI bugs, found by actually looking at the page

CLAUDE.md listed "the UI has never been visually reviewed by a human" as issue
#6. It has now been opened in a browser, and it was hiding two defects that no
test could have caught:

1. **The `A-rc` card rendered as a raw arm id with no description**, while every
   other card had a plain-English name. `HUMAN` in `ui_template.html` was never
   given an `A-rc` entry when the arm was added. It looked like a broken card.
   Fixed, plus a `console.warn` so the next missing arm announces itself.
2. **The technical footer printed `at null`** for the isolation level. Arms that
   take the backend default store `isolation: null`, and it was interpolated
   raw. Now resolved to `SERIALIZABLE (default)` / `READ COMMITTED (default)`.

### And a capture bug the UI made visible

`capture_ui.py` read the memory corpus **before** the runs, under a comment
claiming it captured "every memory that exists — including the one that does not
exist yet when the run starts". It could not: the superseding policy is written
*mid-run*. So `docs/ui_data.json` never contained `hero-m5`, and
`DATA.corpus` was **unused by the page entirely** (`grep -c corpus
ui_template.html` → `0`). The UI argued about a policy change while displaying
none of it.

Now read after the runs, with `supersedes` carried through, and there is a new
**"The notes the agents actually read"** section rendering the real rows —
policy/history/note, the superseded one struck through, the mid-run arrival
flagged. This is the section that answers the judging note *"show the
CockroachDB memory in action visibly."*

The capture also warns if `update_memory_id` is missing from the corpus.

### UI state

Re-captured with **all five arms** (it was a stale four-arm capture; `A-rc` was
missing). PostgreSQL must be up or the capture aborts on arm A.
`docs/index.html` is rebuilt and verified 13/13 on a static check
(self-contained, no external refs, five arms, corpus with supersession).

**Still not done:** the page was reviewed at 1366px only. The Chrome extension
lost host permission for the local preview server partway through, so **narrow
/ mobile widths were never looked at**. Worth one pass.

### Doc sweep

| File | State |
|---|---|
| `docs/VIDEO.md` | rewritten (above) |
| `docs/DEMO.md` | new |
| `README.md` | links table at the top with the **permanent demo + gateway URLs**; five-arm table replacing the four-arm one; `29/29` MCP; the internal contradiction at the old line 1078 (`exactly 80.0 in all fifty runs` vs entry 16's `78.0`) corrected |
| `docs/ARCHITECTURE.md` | compiler, gate and binding in the diagram; five-state table; 6 new failure paths |
| `docs/METHODOLOGY.md` | entry 17 |
| `docs/MCP.md` / `MCP_SERVER.md` | cross-linked both ways; `reconsider` payload updated to the real shape; race outcome corrected to `{committed: 1, reconsider: 4}` |
| `docs/index.html` | rebuilt from the 5-arm capture |

### Why arm A is not the control — the answer to give

**Arm A is still reported** (45/50 over the hard limit, 0 errors). What it is
*not* is the basis of any causal claim.

`B − A` varies **two things at once**: the database *and* the deployment
latency. The same arm on the same seeds gave `196.0` on localhost and `344.0` in
Docker, so "arm A" was not a fixed quantity, and `B − A` flipped sign across
windows. The headline built on it was withdrawn (entry 15).

`A-rc` is the fix: CockroachDB at READ COMMITTED, **same cluster, same latency**,
only the isolation level differs. Controlled, `B − A-rc` =
`−418.5, −49.5, −4.0, +18.0, −67.5`.

So arm A is an **external-validity check** — does this reproduce on stock
PostgreSQL at default isolation? yes — and it is excluded from the comparison
`A-rc` exists to make. It is also why PostgreSQL is optional to run.

Consequence to state rather than hide: arm A is nearly absent from the
CockroachDB-side MCP views, because each run's telemetry goes to the backend
that ran it.

### Deployment — the live function now matches the repo (2026-08-18)

`racelab-gateway` in `ap-south-1` was **redeployed** and is running the compiled
-policy code. It had been stale: last modified `2026-08-17 20:18`, 24,390 bytes,
i.e. the regex path, while the README advertised the URL and the repo described
the gate.

| | before | after |
|---|---|---|
| CodeSize | 24,390 B | **45,018 B** |
| LastModified | 2026-08-17 20:18 | **2026-08-18 10:22** |

Verified against the deployed function, not locally:

```
200 committed  allocate(45)  limit=60  policy=compiled v1  dsn=secretsmanager
403                                     unsigned curl, by design (AWS_IAM)
200 committed  allocate(250) resource=refunds limit=300 v=1 status=compiled
200 committed  allocate(50)  resource=refunds limit=300 v=1 status=compiled
200 abstained  abstain       resource=refunds limit=300 v=1 status=compiled
```

The last three are the important ones: **the deployed gateway enforced a table
this repository has no code for**, from `bindings/refunds.yaml` alone.
`dsn_source: secretsmanager` confirms the credential path.

The build now self-verifies before upload — unzip, path set to the package only,
`yaml` blocked to mimic the layer, import. Reserved concurrency still cannot be
set (account limit is 10, stricter than the 8 requested — safe by accident);
**re-run the deploy after any concurrency increase.**

### Pre-push hygiene, checked

- No AWS keys, DSNs or tokens in tracked files; `.env` is gitignored;
  `.env.example` is placeholders only.
- No tracked file over 2 MB. MIT `LICENSE` present.
- **Low severity, not fixed:** the cluster *names* `blast-avocet` /
  `liquid-shark` appear in `CLAUDE.md`, `docs/MCP.md`, `ccloud.py` and
  `results/`. They are identifiers, not credentials — a DSN still needs user and
  password — but they do identify the user's cluster, and they are already in
  git history, so removing them now would not erase them.
