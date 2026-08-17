"""Persist the captured run's per-decision telemetry so MCP can drill into it.

The swept experiment ran with the in-memory `ListTelemetry` sink, which is the
right choice there: writing a telemetry row inside a raced transaction would add
a write to the very refresh span under test, and writing it outside adds latency
to every attempt. The consequence is that `race_runs` has a thousand rows while
`decisions`, `agent_attempts` and `conflict_edges` have none -- so the
decision-level views have nothing to show.

This closes that gap without running a new experiment. `scripts/capture_ui.py`
already recorded one run per arm with full per-agent detail; this writes that
same data into the telemetry tables so the MCP views are queryable.

Two things it is careful about:

  * **It writes arm A's telemetry to CockroachDB even though arm A ran on
    PostgreSQL.** Telemetry is an *observation about* a run, not part of the
    run, so recording all four arms in one place is what makes them comparable
    -- and it is the pattern CockroachDB documents for agent state. The row
    records which arm produced it, so nothing is ambiguous.
  * **It is idempotent and scoped.** Rows are keyed by the captured `run_id`s
    and deleted before insert, so re-running replaces rather than accumulates.
    It never touches rows from the sweep.

Run:  python scripts/capture_ui.py && python scripts/publish_telemetry.py
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from racelab.db import connect

REPO = pathlib.Path(__file__).resolve().parents[1]
DATA = REPO / "docs" / "ui_data.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(DATA))
    args = ap.parse_args()

    path = pathlib.Path(args.data)
    if not path.exists():
        print(f"no captured data at {path}; run scripts/capture_ui.py first",
              file=sys.stderr)
        return 1

    payload = json.loads(path.read_text(encoding="utf-8"))
    meta = payload["meta"]
    run_ids = [a["run_id"] for a in payload["arms"]]

    decisions = 0
    attempts = 0

    with connect("crdb") as conn:
        # Idempotent: clear only these run_ids, never the sweep's rows.
        for table in ("decisions", "agent_attempts", "conflict_edges"):
            conn.execute(
                f"DELETE FROM {table} WHERE run_id = ANY(%s)", (run_ids,)
            )

        for arm in payload["arms"]:
            run_id = arm["run_id"]
            policy = "conflict-aware" if arm["re_reason"] else "naive"

            # The run row, so a drill-down joins back to something. Arm A's own
            # row also exists in PostgreSQL; this is the comparable copy.
            conn.execute(
                """
                INSERT INTO race_runs (run_id, seed, arm, scenario, agent_count,
                                       final_sum, hard_limit, invariant_violated,
                                       ended_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (run_id) DO UPDATE SET
                    final_sum = EXCLUDED.final_sum,
                    invariant_violated = EXCLUDED.invariant_violated
                """,
                (run_id, meta["seed"], arm["id"], "hero-001-captured",
                 meta["agents"], arm["final_sum"], meta["hard_limit"],
                 arm["violated_hard_limit"]),
            )

            for agent in arm["agents"]:
                for step in agent.get("steps", []):
                    conn.execute(
                        """
                        INSERT INTO decisions (
                            decision_id, run_id, agent_id, attempt_no, policy,
                            retrieved_memory_ids, inferred_ceiling, observed_sum,
                            proposed_amount, decision_before, decision_after, revised
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (str(uuid.uuid4()), run_id, agent["agent_id"],
                         step["attempt_no"], policy,
                         list(step.get("memory_ids") or []),
                         step.get("inferred_ceiling"), step.get("observed_sum"),
                         step.get("amount"),
                         agent.get("decision_before"), agent.get("decision_after"),
                         bool(agent.get("revised"))),
                    )
                    decisions += 1

                # One attempt row per attempt the agent actually made. Attempts
                # before the last one ended in a serialization failure by
                # definition -- that is what caused another attempt.
                made = agent.get("attempts_made") or len(agent.get("steps", [])) or 1
                for n in range(made):
                    last = n == made - 1
                    conn.execute(
                        """
                        INSERT INTO agent_attempts (
                            attempt_id, run_id, agent_id, policy, outcome,
                            error_code, retry_count
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (str(uuid.uuid4()), run_id, agent["agent_id"], policy,
                         (agent.get("outcome") or "error") if last else "conflict",
                         None if last else "40001", n),
                    )
                    attempts += 1

        counts = {}
        for table in ("race_runs", "decisions", "agent_attempts"):
            counts[table] = conn.execute(
                f"SELECT count(*) FROM {table}").fetchone()[0]

        print(f"published telemetry for {len(run_ids)} captured runs")
        print(f"  decisions inserted      {decisions}")
        print(f"  attempt rows inserted   {attempts}")
        print()
        print("table totals now:")
        for t, n in counts.items():
            print(f"  {t:16} {n:>7}")
        print()
        print("MCP views:")
        for view in ("race_arm_comparison", "race_run_summary",
                     "race_agent_decisions", "race_conflict_summary"):
            try:
                n = conn.execute(f"SELECT count(*) FROM {view}").fetchone()[0]
                print(f"  {view:22} {n:>7} rows")
            except Exception as exc:  # noqa: BLE001
                print(f"  {view:22} MISSING ({type(exc).__name__})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
