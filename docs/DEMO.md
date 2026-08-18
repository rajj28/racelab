# RaceLab — demo runbook

What is on screen for each beat of `docs/VIDEO.md`, the exact command that
produces it, and the output to expect. Every block below was copied from a real
run against the live cluster, not written from memory.

**Read this in a second window while recording.** VIDEO.md is what you say; this
is what you show.

---

## Before you record

Run this once. It takes a few minutes and it is the difference between a clean
take and discovering mid-record that the ledger is dirty.

```bash
# 1. Schema and seed data, from scratch
python -m racelab.schema --backend crdb
python scripts/seed.py --reset

# 2. The demo account's policy must be compiled, or the gateway refuses
#    everything. That refusal is beat 7 -- you want it on purpose, not by
#    accident during beat 4.
python scripts/compile_policies.py --show

# 3. The refunds demo resource (beat 8)
python scripts/test_binding.py --create

# 4. Confirm everything is green before you start talking
python scripts/test_all.py
```

**Checklist**

- [ ] `compile_policies.py --show` shows a `*` next to `hero-001`
- [ ] Terminal at **16–18 pt**, window at **1920×1080**. `policy_status: "stale"`
      must be readable without pausing.
- [ ] Nothing else is touching the cluster. A second process running during a
      race will produce numbers that do not match this file.
- [ ] Mic test recorded and played back. Do this before the demo, not after.

> **Do not run the test suite and a race at the same time.** The suite writes to
> `hero-001` and deletes `hero-m5`. We have chased a non-existent bug caused by
> exactly that.

---

## Beat 1 · `0:00` — Title card

**On screen:** one card, four seconds.

```
RaceLab
Agents that re-decide, instead of retrying.

CockroachDB Cloud · AWS Lambda · Bedrock · Secrets Manager · CloudWatch
```

No command. Cut to a terminal on the word "happening".

---

## Beat 2 · `0:16` — The race, live

**Command:**

```bash
python scripts/capture_ui.py
```

Runs all five arms, twenty agents each, against the live cluster. **Takes ~3
minutes — speed it up 8× in the edit and hold on the final table.**

**Expected output** (this is a real run; your totals will differ, the *pattern*
will not):

```
  A     postgres RC     naive                sum  450  limit VIOLATED  policy BREACHED  conflicts   0  agents 20/20
  A-rc  cockroach RC    naive                sum  540  limit VIOLATED  policy BREACHED  conflicts   0  agents 20/20
  B     cockroach       naive                sum  225  limit VIOLATED  policy BREACHED  conflicts  55  agents 20/20
  C-ops cockroach  re-reason, stale memory   sum   80  limit     held  policy BREACHED  conflicts  34  agents 20/20
  C     cockroach  full refresh              sum   45  limit     held  policy     held  conflicts  12  agents 20/20
```

> **The three left-hand totals move every run** — that is contention, not noise,
> and two consecutive captures gave `360/495/180` and `450/540/225`. The two that
> do **not** move are the ones the argument rests on: C-ops lands on the ceiling
> it remembers, C lands inside both limits. Narrate the pattern, never a
> particular number.

**What to point at, in order:**

1. `A … conflicts 0` — hundreds over a $100 budget, **zero errors raised**.
2. `B … conflicts 55` — serializable isolation *sees* it. Still over.
3. `C-ops … limit held` — re-deciding fixes the budget.
4. `C … policy held` — only refreshing memory fixes the *rule*.

That is the whole argument in five lines. If you show one thing, show this.

**If PostgreSQL is not running**, arm A fails and the capture aborts. Either
`docker compose up -d`, or skip to the pre-computed 250-run table:

```bash
python scripts/render_sweep.py results/sweep_controlled.json | head -60
```

---

## Beat 3 · `0:40` — The aggregate, and the CockroachDB Managed MCP Server

**On screen:** ask CockroachDB's own MCP server for the arm comparison. This
ticks "CockroachDB tools, actually used" on camera.

**Command:**

```bash
python scripts/mcp_query.py
```

Or, in Claude Code with `.mcp.json` loaded, ask the `cockroachdb-cloud` server:

> `SELECT * FROM race_arm_comparison`

**Expected shape** (accumulated across every run on the cluster):

```
 arm    runs  runs_over_hard_limit  avg_final_sum  worst_final_sum  hard_limit
 A-rc     74                    72          374.1              810         100
 A         1                     1          135.0              135         100
 B       372                   357          203.5              330         100
 C-ops   536                     0           72.7               80         100
 C       516                     0           58.4               80         100
```

**Say the honest thing about `A`:** it has one row because arm A runs on
PostgreSQL and this view is on CockroachDB. See "Why arm A is not the control"
below — a judge may ask.

The two `0` values in `runs_over_hard_limit`, across **1,052 runs**, are the
result.

---

## Beat 4 · `0:50` — Memory, visibly: store → retrieve → refuse

**This is the beat the judging notes ask for by name.** Do not narrate it over a
slide — show the tool calls.

**Setup:** Claude Code, in this repo, with `.mcp.json` already wiring up the
`racelab` server. Type these three prompts and let the tool calls render.

**Prompt 1 — store:**

> Use the racelab MCP server to remember, for account `demo-live-001`, this
> policy: "The authorization ceiling for this account is $60. This is a
> cumulative total across all allocations, with no reset period."

**Prompt 2 — retrieve:**

> Now use `recall` on `demo-live-001` to find the spending ceiling.

**Prompt 3 — act, and be stopped:**

> Using `decide_and_write`, allocate 45 to `demo-live-001` twice.

The first commits. The second is **refused**:

```json
{
 "status": "refused",
 "why": "constraint refused 1 action(s) (allocate(45)); nothing written: 90 exceeds the policy limit of 60 (policy v1: SUM(amount) over allocations <= 60)",
 "your_action": "allocate(45)",
 "total_now": 45,
 "policy_now": { "policy_status": "compiled", "policy_version": 1, "policy_limit": 60 },
 "still_permitted": [],
 "guidance": "your action was not written. Choose one of [] or abstain."
}
```

**On-screen overlay for this beat:**
`CockroachDB VECTOR(1024) + vector index · AWS Bedrock Titan embeddings`

**Point at:** `nothing written`, `policy_version: 1`, and `still_permitted: []`.
The write was stopped *inside the transaction, before the commit* — not detected
afterwards.

> `remember` needs the account to exist. If `demo-live-001` is not seeded, use
> `hero-001`, which is.

---

## Beat 5 · `1:10` — The vector index is real

Ten seconds, and it forecloses "is that actually ANN or just a filtered scan?".

**Command:**

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

**Expected — the two lines that matter:**

```
└── • vector search
      table: memories@memories_embedding_idx
      prefix spans: [/'hero-001' - /'hero-001']
```

**Say:** the optimizer chose the vector index with no hint. `scripts/verify_clean_clone.py`
asserts that on a *fresh* database, because adding an ordinary index on
`account_id` silently wins on cost and turns ANN search back into a scan.

---

## Beats 6 + 7 · `1:20` – `2:05` — One continuous sequence

These two beats are a single take: Legal changes the rule, everything stops, and
getting it started again is the story. **The whole sequence below was run end to
end; every output is copied from it.**

> **Why not `seed.py --apply-hero-update`?** Because both hero policies are
> already compiled, so applying the update lands on a compiled constraint and you
> get `compiled`, not `stale`. You need a policy document that has *never* been
> compiled. That is what step 1 does.

### 6a · Legal writes a new rule

**For the video, do this through the agent's own memory tool** — it is the same
beat as the `remember` call and it shows the loop closing. In Claude Code:

> Use the racelab MCP server to `remember` a policy for `hero-001`:
> "Authorization ceiling reduced to $40 per billing cycle, effective
> immediately." It supersedes `hero-m5`.

The tool tells you what it just did to the account:

```json
{
 "status": "stored", "kind": "policy",
 "enforcement": "this is now the governing policy document. It is NOT yet enforceable:
   run `python scripts/compile_policies.py --account hero-001` to compile it. Until
   then decide_and_write refuses every write against this account with policy_status
   'stale' or 'uncompiled'."
}
```

Terminal fallback, if you would rather not cut to Claude Code:

```bash
python -c "
import sys, datetime; sys.path.insert(0,'.')
from racelab.db import connect
with connect('crdb') as c:
    c.execute(\"INSERT INTO memories (memory_id, account_id, text, kind, supersedes, created_at) VALUES ('hero-m6','hero-001','Authorization ceiling reduced to \$40 per billing cycle, effective immediately.','policy','hero-m5',%s)\", (datetime.datetime.now(datetime.timezone.utc),))
print('Legal lowered the ceiling to \$40')"
```

### 6b · Everything stops

```bash
python deploy/lambda_handler.py --account hero-001
```

```json
{
 "outcome": "refused",
 "policy_status": "stale",
 "governing_memory": "hero-m6",
 "policy_detail": "the newest compiled policy for 'hero-001' is v2, compiled from 'hero-m1', but 'hero-m6' now governs. The rule changed and was not recompiled; nothing will be authorized against the withdrawn version. Run scripts/compile_policies.py --account hero-001",
 "still_permitted": []
}
```

**Say:** the rule moved and nothing has been compiled from it, so the gateway
authorizes *nothing*. The dollar-figure regex could not have this state — it
re-read the text on every request and always produced a number.

### 7a · The compiler refuses our own policy

**The most credible fifteen seconds in the video.** Unedited.

```bash
python scripts/compile_policies.py --account hero-001
```

```
  hero-001: SUM(amount) over allocations <= 40 UNENFORCEABLE: per billing cycle -
    cannot map to available windows without knowing if billing cycle aligns with
    calendar_month or other defined period
    NOT STORED. The gateway will refuse writes against hero-001 until this is resolved.
    Fix the policy text, or run again with --resolve "<what the rule means>".

0/1 account(s) are now enforceable
```

**Hold on `UNENFORCEABLE`.**

### 7b · A person says what it means, and it is recorded

```bash
python scripts/compile_policies.py --account hero-001 \
  --resolve "The authorization ceiling for this account is \$40. This is a cumulative total across all allocations, with no reset period."
```

```
  hero-001: SUM(amount) over allocations <= 40
    stored as v3  fingerprint 78ee214e05bb647e

1/1 account(s) are now enforceable
```

```bash
python deploy/lambda_handler.py --account hero-001
```

```
committed  allocate(40)  | policy_status: compiled | policy_limit: 40 | policy_version: 3
```

Then `--show`, and point at the two things that matter:

```bash
python scripts/compile_policies.py --show
```

```
account                v  enforceable fingerprint       from         compiled_by
 hero-001              1  True        7ced29bd3bb1d0bc  hero-m5      us.anthropic.claude-sonnet-4-5…
 hero-001              2  True        bfad7959c42f1392  hero-m1      us.anthropic.claude-sonnet-4-5…
*hero-001              3  True        78ee214e05bb647e  hero-m6      …claude-sonnet-4-5 via operator:INDIA
```

**Point at:** `*` — the version actually in force is the one compiled from the
document *currently governing*, not the highest number — and `via operator:`, a
human reading, recorded and attributable.

### 7c · Reset before the next take

```bash
python -c "
import sys; sys.path.insert(0,'.')
from racelab.db import connect
with connect('crdb') as c:
    c.execute(\"DELETE FROM memories WHERE memory_id='hero-m6'\")
    c.execute(\"DELETE FROM policy_constraints WHERE account_id='hero-001' AND source_memory_id='hero-m6'\")
    c.execute(\"DELETE FROM allocations WHERE account_id='hero-001'\")
print('corpus restored')"
```

---

## Beat 8 · `2:05` — Point it at your table

**On screen:** the YAML, then the suite.

```bash
cat bindings/refunds.yaml
grep -rn "refund" racelab/ deploy/ | grep -v "\.md" | grep -v '"""'   # expect: nothing
python scripts/test_binding.py
```

**Expected tail:**

```
3. 6 concurrent writers, and the invariant holds
  [PASS] no run was voided by the collapse guard
     6 writers -> {'committed': 2, 'abstained': 4}, 9 conflicts, total $300
  [PASS] the hard limit was never broken -- $300 <= $500
  [PASS] the compiled policy limit was never broken -- $300 <= $300
  [PASS] no refused action was ever committed -- 0 violations

31/31 passed
```

**Say:** twenty lines of YAML, no refund code anywhere, and the suite asserts
that.

---

## Beat 9 · `2:25` — The stack slate

**Hold three full seconds.** A judge is screenshotting this to tick
requirements.

```
CockroachDB                          AWS
  Distributed vector indexing          Bedrock — Titan embeddings, Claude Sonnet 4.5
  SERIALIZABLE isolation               Lambda — the deployed write gateway
  Managed MCP Server                   Secrets Manager — the database credential
  ccloud CLI                           CloudWatch — one record per decision, alarmed
  (we also PROVIDE an MCP server)      S3 — layer artifacts
```

---

## Beat 10 · the UI, wherever you want it

**Live:** <https://rajj28.github.io/racelab/>

Good as a B-roll cutaway during beat 2 or as the final frame. It is fully
self-contained — no network, no external scripts — so it works even if the venue
Wi-Fi does not.

```bash
python scripts/capture_ui.py && python scripts/build_ui.py
start docs/index.html          # Windows;  open docs/index.html on macOS
```

---

## Terminal · DB · UI, at a glance

| Beat | Terminal | Database | UI |
|---|---|---|---|
| 2 race | `capture_ui.py` five-arm table | `allocations` climbing past `hard_limit` | timeline, arm cards |
| 3 aggregate | `mcp_query.py` | `race_arm_comparison` | arm comparison |
| 4 memory | Claude Code MCP tool calls | `memories` insert + ANN read | corpus panel, superseded memory |
| 5 index | `EXPLAIN` → `• vector search` | `memories@memories_embedding_idx` | — |
| 6 stale | gateway `409 stale` | new `memories` row vs `policy_constraints` | — |
| 7 compiler | `UNENFORCEABLE` → `--resolve` → commit | `policy_constraints` gains v3 | — |
| 8 binding | `test_binding.py` 31/31 | `refunds`, `customers` | — |

---

## Why arm A is not the control — have this answer ready

A judge who reads the results table will ask why PostgreSQL is barely used.

**Arm A is still reported** — it is in the 250-run table and in every sweep.
What it is *not* is the thing any causal claim rests on, and that is deliberate.

`B − A` varies **two things at once**: the database (PostgreSQL vs CockroachDB)
*and* the deployment (local Docker vs CockroachDB Cloud in `ap-south-1`). We
found this the hard way — the same arm on the same seeds produced `196.0` on
localhost and `344.0` in Docker, so "arm A" was not a fixed quantity, and
`B − A` flipped sign across arrival windows. A headline claim built on it was
**withdrawn** (METHODOLOGY entry 15).

The fix is **arm `A-rc`**: CockroachDB at READ COMMITTED, on the *same cluster*,
same latency, same everything. The only difference from arm B is the isolation
level, which is the variable the claim is about. Controlled that way,
`B − A-rc` = `−418.5, −49.5, −4.0, +18.0, −67.5` — serializable isolation helps
even a naive client.

So arm A is kept as an **external-validity check** — does this reproduce on
stock PostgreSQL at default isolation? Yes, 45/50 runs over the hard limit with
zero errors raised — and it is excluded from the causal comparison, which is
what `A-rc` is for. It is also why PostgreSQL is optional to run at all.

One consequence to state rather than hide: arm A barely appears in the
CockroachDB-side MCP views, because each run's telemetry is written to the
backend that ran it, and arm A's backend is PostgreSQL.

---

## If something goes wrong on camera

| Symptom | Cause | Fix |
|---|---|---|
| Every gateway call returns `409 uncompiled` | no compiled policy for the account | `python scripts/compile_policies.py --account <id>` |
| `409 stale` when you wanted a commit | the policy document moved | recompile, or `seed.py --revert-hero-update` |
| Totals do not match this file | another process is on the cluster, or the ledger is dirty | stop it, then `seed.py --reset` |
| `capture_ui.py` aborts on arm A | PostgreSQL is down | `docker compose up -d`, or use `render_sweep.py` |
| `decide_and_write` returns `forbidden` | server started read-only | restart with `--allow-writes` |
| MCP server prints to stdout and the session breaks | something wrote to stdout | stdout **is** the protocol on stdio; logs go to stderr |
