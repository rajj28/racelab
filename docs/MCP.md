# Inspecting RaceLab through CockroachDB Cloud's MCP Server

This experiment is queryable by an agent, and **we did not write an MCP server to
make that true.** CockroachDB Cloud ships a Managed MCP Server that already
exposes `list_clusters`, `list_databases`, `list_tables`, `get_table_schema`,
`select_query`, `explain_query`, `show_statement` and `show_running_queries`.
Writing another one would have been duplication.

What was actually missing was a **schema shaped for an agent to query**. That is
what `MCP_VIEWS` in `racelab/schema.py` provides, and it is designed around the
managed server's documented limits rather than in spite of them.

## Connect

Add to your MCP client config (Claude Code, Cursor, VS Code):

```json
{
  "mcpServers": {
    "cockroachdb-cloud": {
      "type": "http",
      "url": "https://cockroachlabs.cloud/mcp"
    }
  }
}
```

To scope it to one cluster, add the header:

```json
"headers": { "mcp-cluster-id": "<your-cluster-id>" }
```

Authentication is OAuth (browser flow, short-lived tokens) or a service-account
API key as a bearer token. Either needs Cluster Admin or Operator on the cluster.
**Use OAuth.** A long-lived key in a config file is the wrong trade for a demo.

## The four views

| View | Rows | What it answers |
| ---- | ---- | --------------- |
| `race_arm_comparison` | 4 | How did the four approaches differ? Start here. |
| `race_run_summary` | ~1000 | One row per run, newest first. |
| `race_agent_decisions` | ~120 | Per reasoning step: what was read, what ceiling was believed, what was chosen. |
| `race_conflict_summary` | 0 — see below | Transaction conflict graph, per run. |

### Designed around the server's limits

These are real constraints, and they shaped the views:

- **10 KiB responses, `SELECT` defaults to `LIMIT 25`.** Every view is narrow and
  **carries its own `ORDER BY`**. Without that, an agent issuing a bare
  `SELECT * FROM race_run_summary` would receive 25 arbitrary rows out of a
  thousand. With it, the 25 it receives are the most recent 25.
- **`crdb_internal`, `system` and `pg_catalog` are unreachable, and
  `EXPLAIN ANALYZE` is unavailable.** Nothing here needs them — every number is
  aggregated from our own telemetry tables.
- **20-second query timeout.** Aggregation is kept to grouped scans over indexed
  columns.

### One design fix found by using it

`race_agent_decisions` joins `arm` in rather than leaving it to the caller. The
arm ids are `C` and `C-ops`, so any attempt to derive the arm from the `run_id`
string — `split_part(run_id, '-', 1)` being the obvious move — collapses the two
into one and **silently merges the exact pair the ablation compares.** We hit
this while testing these views. A view that an agent must join correctly in order
to avoid a wrong answer is a badly shaped view, so the join moved into the view.

## Three queries that carry the result

**1. How did the four approaches compare?**

```sql
SELECT * FROM race_arm_comparison;
```

```
arm     runs  runs_over_hard_limit  avg_final_sum  worst_final_sum  hard_limit
A          1                     1          135.0              135         100
B        298                   286          197.1              330         100
C-ops    363                     0           71.5               80         100
C        343                     0           58.7               80         100
```

Arm B exceeded the hard limit in **286 of 298 runs**. C-ops and C: **zero**.

**2. Which ceiling did each approach actually reason with?**

This is the ablation, in one query. `$80` is the withdrawn ceiling; `$60` is the
one in force.

```sql
SELECT arm, inferred_ceiling, count(*) AS decisions
FROM race_agent_decisions
WHERE arm IS NOT NULL
GROUP BY arm, inferred_ceiling
ORDER BY arm, inferred_ceiling;
```

```
C-ops    60    19
C-ops    80    31     <-- reasoned with the withdrawn ceiling 31 times
C        60    24
C        80     8      <-- refreshing memory cut that to 8
```

**3. Where did each approach top out?**

```sql
SELECT arm, max(resulting_total) AS highest_total_reached
FROM race_agent_decisions
WHERE arm IS NOT NULL
GROUP BY arm ORDER BY arm;
```

```
A       135
B        45
C        45
C-ops    80     <-- exactly the withdrawn ceiling
```

Arm B's `45` is worth pausing on: **every individual naive decision looks
correct.** Each one read a total of `0` and proposed `45`, which fits. The
violation is not in any single decision — it is in the sum of three of them. That
is what makes this class of bug hard to see from a log of decisions.

## What is empty, and why

`race_conflict_summary` returns **0 rows**, and that is not an oversight.

The swept experiment ran with the in-memory `ListTelemetry` sink deliberately:
writing a telemetry row inside a raced transaction would add a write to the very
refresh span under test, and the experiment would be measuring its own
instrumentation. `conflict_edges` is populated only by `SqlTelemetry`, which no
reported run used.

The per-decision rows that *are* here come from `scripts/publish_telemetry.py`,
which writes the run captured by `scripts/capture_ui.py` — data we already had,
not a new experiment. Pairwise conflict edges were not captured, and we will not
synthesise them to fill a table.

To populate it, run the wrapper with `SqlTelemetry` on an autocommit connection
and accept that the run is then instrumented differently from the reported ones.

## One honest limitation

**Arm A does not appear in `race_run_summary` for the swept runs.**
`_record_run` writes each run's row to the backend that ran it, and arm A runs on
PostgreSQL by definition — it is the READ COMMITTED control. So the swept arm A
rows live in the local Postgres, and the CockroachDB-side comparison covers B,
C-ops and C. The single arm A row visible in `race_arm_comparison` is the
captured run, published here so all four are comparable in one place.

The full four-arm comparison is in `results/sweep_fixed.md`.
