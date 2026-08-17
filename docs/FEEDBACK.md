# Product feedback

Written while building RaceLab against CockroachDB Cloud Basic v26.2.5. These
are honest observations from someone who hit each of them, not a wishlist.
Entries are added as they are encountered rather than reconstructed at the end.

---

## 1. A vector index can be present, correct, and silently unused

This cost the most time and was the closest call in the project.

The schema created a vector index the documented way:

```sql
CREATE VECTOR INDEX memories_embedding_idx ON memories (embedding vector_cosine_ops)
```

Retrieval is always scoped to one account, so the query is:

```sql
SELECT ... FROM memories
WHERE account_id = $1
ORDER BY embedding <=> $2
LIMIT 4
```

That plan does **not** use the vector index. The optimizer satisfies the filter
from the secondary index on `account_id` and then scans:

```
└── • scan
      table: memories@memories_account_idx
      spans: [/'hero-001' - /'hero-001']
```

The first fix is a prefix column, which the documentation does describe:

```sql
CREATE VECTOR INDEX memories_embedding_idx ON memories (account_id, embedding vector_cosine_ops)
```

That is **necessary but not sufficient**, which took a second round of the same
mistake to discover. With the prefix column in place the plan reverted to a scan
again as soon as the table had statistics:

```
└── • scan
      table: memories@memories_account_idx
      estimated row count: 1 (4.2% of the table; using stats forecast for 21 hours in the future)
```

The estimate is the tell. That span held **1200 rows**, not 1. With a
cardinality estimate that low, a scan looks nearly free and beats ANN search on
cost every time. Forcing the index confirmed it could serve the query perfectly
well:

```sql
SELECT memory_id FROM memories@memories_embedding_idx
WHERE account_id = $1 ORDER BY embedding <=> $2 LIMIT 4
```
```
└── • vector search
      table: memories@memories_embedding_idx
      prefix spans: [/'scale-probe' - /'scale-probe']
```

The real fix was dropping the ordinary secondary index on `(account_id)` — the
one that looks obviously correct, and the one **the optimizer itself
recommends** for this query. With it gone the optimizer chose `• vector search`
unaided, at 5 rows and at 1200.

So the full shape of the trap is: a filtered ANN query needs the filter column
as a prefix, *and* needs there to be no conventional index that could satisfy
the filter instead. A vector index competes on cost with every other access
path, and it loses to a cheap-looking seek whenever statistics are off — which,
for a table being bulk-loaded and immediately queried, is most of the time.

**Why this is worth reporting.** Nothing failed. No error, no warning, no
degraded result — retrieval returned correct answers the whole time, because a
scan over a small table produces the same rows as an ANN search. On a 22-row
corpus it was also just as fast. The only reason this was caught is that the
project had an explicit assertion that the query plan must contain a vector
search node, and that assertion was written because a judge might reasonably ask
whether the vector index was decorative.

An application that adds a vector index for a filtered query and never inspects
`EXPLAIN` would ship believing it has ANN search, and would find out at whatever
scale makes the scan hurt.

**Suggestions**, roughly in order of how much they would have helped:

- **Do not recommend an index that disables the vector index.** This is the
  strongest suggestion here. For this query the optimizer recommends
  `CREATE INDEX ... (account_id) STORING (embedding)`. Following that
  recommendation guarantees the vector index is never used again, because the
  recommended index is precisely the cheaper access path. The recommendation is
  locally reasonable and globally wrong, and a user who trusts it ends up with
  ANN search that is switched off by their own optimizer's advice. A vector
  index on the table should suppress this recommendation, or at minimum
  annotate it with what it costs.
- An index recommendation for the inverse case: "a vector index exists on this
  column but cannot serve this filtered query; consider a prefix column" would
  be precise and actionable, and would have saved the first round of this.
- Something in `EXPLAIN` that says a vector index was *considered and rejected
  on cost*. The plan currently gives no hint that ANN search was ever an
  option, so the failure is invisible unless you already suspect it.
- A note in the `CREATE VECTOR INDEX` documentation at the point where prefix
  columns are introduced, stating explicitly that a filtered ANN query *requires*
  the filter column as a prefix, rather than presenting prefix columns primarily
  as a way to narrow the search space.
- Optionally, a warning at index-creation time when a vector index is created on
  a table that already has secondary indexes suggesting a common filter pattern.

## 2. `gc.ttlseconds` defaults are not what the documentation examples imply

The cluster reported `gc.ttlseconds = 4500` (1.25 h) for `RANGE default`.
Documentation examples variously show `14400` and `100000`, and community posts
cite other values again. For a feature like `AS OF SYSTEM TIME`, where exceeding
the window is a runtime error rather than a degraded result, the actual value
matters and the only reliable way to learn it is to read it from the cluster.

Reading it is easy once you know to. The friction is not knowing that the
documented examples do not describe your cluster. A line in the `AS OF SYSTEM
TIME` documentation saying "check your cluster's actual GC window with
`SHOW ZONE CONFIGURATION FOR RANGE default`, it varies by deployment and
release" would close this.

## 3. `AS OF SYSTEM TIME` rejects bound parameters

```sql
SELECT count(*) FROM t AS OF SYSTEM TIME %s        -- fails
```
```
SQLSTATE XXUUU: AS OF SYSTEM TIME: only constant expressions,
with_min_timestamp, with_max_staleness, or follower_read_timestamp are allowed
```

Understandable given that the timestamp has to be resolved before planning. But
it pushes callers into string-interpolating a timestamp into SQL, which is the
one habit every other part of the stack trains you out of. In this project the
value is a `DECIMAL` read back from `cluster_logical_timestamp()` and is
re-parsed as a `Decimal` before interpolation specifically to keep that path
safe — which is the sort of thing every caller now has to invent for themselves.

A parameter form that accepts a placeholder resolved at prepare time, or a
documented helper in the drivers, would remove a small but real footgun.

## 4. Client-visible versus internally retried 40001 is hard to observe

The project depends on the distinction between serialization failures the
cluster resolves internally and those that reach the client. Getting a
client-visible one reliably required understanding that a transaction whose
results have already been returned to the client cannot be transparently
retried — which is true, documented in places, and not obvious when you are
trying to work out why your carefully constructed race produces no errors.

What would have helped: a way to see, per transaction or in aggregate, how many
retries the cluster absorbed internally. `crdb_internal` exposes a great deal,
but a straightforward "this statement was internally retried N times" signal
would make it possible to distinguish "my workload has no contention" from "my
workload has plenty of contention and the cluster is handling all of it", which
currently look identical from the client.

## 5. Connecting with `sslmode=verify-full` from a Python client on Windows

`sslrootcert=system` is the natural choice and it fails against the OpenSSL
bundled in the `psycopg[binary]` wheel on Windows, because there is no system
trust store for it to consult:

```
SSL error: certificate verify failed
```

The cluster's certificate is signed by a public CA, so the fix is to point
`sslrootcert` at the `certifi` bundle. That is a two-line fix once diagnosed,
but the error message points at the certificate rather than at the trust store,
and the obvious escape hatch — dropping to `sslmode=require` — both weakens
security and is itself refused when combined with `sslrootcert=system`:

```
weak sslmode "require" may not be used with sslrootcert=system (use "verify-full")
```

That second message is genuinely good; it stops the user doing the wrong thing.
The connection-string examples in the Cloud console could go one step further
and mention the `certifi` path for Python clients, which is where a fair number
of users will land.

## 6. Connection count is the binding constraint for agent-swarm workloads on Cloud Basic

An observation rather than a complaint, because the limit is presumably
deliberate and the tier is inexpensive.

An agent in this experiment needs two connections, and the reason is structural
rather than sloppy:

- one **racing** connection, which must be its own — separate connections are
  what make concurrent transactions concurrent, and pooling them would serialise
  the interleaving the experiment exists to measure;
- one **memory** connection for semantic retrieval, which happens outside the
  transaction.

At 20 agents that is 40 concurrent connections for what is, in application
terms, a very small workload — twenty agents doing one decision each. A full
sweep failed part-way through with `connection timeout expired` at exactly the
point concurrency peaked.

The fix was straightforward once diagnosed: pool the memory connections, keep
the racing ones per-agent. But the reasoning needed to get there is not obvious
in advance, and the failure surfaces as a timeout rather than as anything
naming connection limits — so the natural first hypothesis is a network problem
or a slow cluster, not a quota.

What would have helped, in order:

- An error that names the limit. `connection timeout expired` sent us looking at
  latency and TLS before connection count.
- A visible current-versus-maximum connection count in the Cloud console, or a
  `crdb_internal` view exposing it. We could not find the effective ceiling for
  our cluster without hitting it.
- A line in the Cloud Basic documentation about what connection counts the tier
  supports, framed for the case that is now common: many short-lived agent
  workers rather than a few long-lived application servers. Agent frameworks
  default to a connection per worker, and that assumption scales differently
  from a web app with a shared pool.

This is a good constraint to know about early. It is also a genuine design input
for anyone building agent swarms: it pushes you toward pooling everything that
is not the transaction itself, which is the right architecture anyway.

---

*Sections on MCP read-only ergonomics to follow once that surface is in use.*

---

## 7. Bedrock `converse` tool use: two things that cost us a debugging cycle

Both are small, both are the kind of thing that is obvious once you have hit it,
and neither is where a reader of the API reference would expect to look.

### The `enum` in a tool's `inputSchema` is not a validation boundary

We declared the action space as a four-value `enum` on a required string property,
and forced the tool with
`toolChoice: {"tool": {"name": "submit_allocation_decision"}}`.

`toolChoice` behaved exactly as documented — every response was a tool call. The
`enum` did not constrain the value. Claude Sonnet 4.5 returned `allocate(30)`
where the four permitted values were `allocate(45)`, `allocate(40)`,
`allocate(35)` and `abstain`. In context this was a *reasonable* answer — `$30`
was the exact remaining headroom — which is what makes it easy to miss: it looks
like a valid decision rather than a schema violation.

We are not reporting this as a bug. Constrained decoding against an arbitrary
JSON Schema is a hard guarantee to offer, and the model choosing a sensible
out-of-set value is understandable. The feedback is about **expectation setting**:
a schema author reasonably reads `enum` as enforced, because that is what `enum`
means everywhere else it appears. A sentence in the tool-use documentation saying
that `inputSchema` is guidance to the model rather than a validated contract, and
that callers must validate the returned input themselves, would have saved us the
cycle. As it stands the safe default — validate every field you constrained — is
discoverable only by being burned.

### A correction after a forced tool call must be a `toolResult`, not a text turn

Once we were validating, the natural repair is to re-ask with the violation named.
Our first attempt appended the assistant's tool-use message followed by a normal
user text turn explaining the problem. That fails:

```
ValidationException: The model returned the following errors: messages.2:
`tool_use` ids were found without `tool_result` blocks immediately after:
tooluse_ygDzP0Pqs4MRhXwf5dEYCK. Each `tool_use` block must have a
corresponding `tool_result` block in the next message.
```

The error message is genuinely good — it names the offending message index, the
specific `toolUseId`, and the rule. Credit where due; this is what a useful API
error looks like, and it took one read to fix.

The fix is to answer the tool call on its own terms, carrying the id back:

```python
{"role": "user", "content": [{
    "toolResult": {
        "toolUseId": tool_use_id,      # from the assistant's toolUse block
        "status": "error",
        "content": [{"text": "...why the answer was rejected..."}],
    }
}]}
```

That means `_extract_tool_use` has to return the `toolUseId` alongside the input,
which is easy once you know you need it and invisible until you do. Worth a short
"correcting a tool call" example in the docs next to the tool-use walkthrough —
validation failure is a common path, and the shape of the correction turn is not
guessable from the happy-path example.

### Net

Both of these are ergonomics rather than capability, and the underlying feature —
forcing a specific tool to get strictly-shaped output instead of parsing prose —
did the main job well. Out of 60 generations, one needed a repair, and the repair
succeeded on the first re-ask.
