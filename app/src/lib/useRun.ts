import { useCallback, useRef, useState } from "react";
import type { AgentRow, RunEvent } from "./types";

export type RunState = {
  running: boolean;
  runId: string | null;
  arm: string | null;
  agents: Map<string, AgentRow>;
  /** Committed total over time, so the ledger bar can move as writes land. */
  total: number;
  hardLimit: number;
  ceiling: number | null;
  policyMovedAt: number | null;
  events: RunEvent[];
  done: Extract<RunEvent, { type: "done" }> | null;
  error: string | null;
  elapsed: number;
};

const EMPTY: RunState = {
  running: false, runId: null, arm: null, agents: new Map(), total: 0,
  hardLimit: 100, ceiling: null, policyMovedAt: null, events: [],
  done: null, error: null, elapsed: 0,
};

/**
 * Subscribes to a live run.
 *
 * Every event here came off a real agent on a real cluster -- the server drives
 * `run_once`, the same function the published sweep calls, and streams its
 * observer hooks. Nothing is interpolated or smoothed: if the ledger jumps, a
 * write landed.
 */
export function useRun() {
  const [state, setState] = useState<RunState>(EMPTY);
  const source = useRef<EventSource | null>(null);
  const startedAt = useRef<number>(0);

  const stop = useCallback(() => {
    source.current?.close();
    source.current = null;
    setState((s) => ({ ...s, running: false }));
  }, []);

  const start = useCallback((arm: string, agents: number, window: number) => {
    source.current?.close();
    startedAt.current = performance.now();
    setState({ ...EMPTY, agents: new Map(), running: true, arm });

    const es = new EventSource(
      `/api/run?arm=${encodeURIComponent(arm)}&agents=${agents}&window=${window}`
    );
    source.current = es;

    es.onmessage = (msg) => {
      let ev: RunEvent;
      try {
        ev = JSON.parse(msg.data) as RunEvent;
      } catch {
        return;
      }

      setState((s) => {
        const next: RunState = {
          ...s,
          events: [...s.events, ev],
          elapsed: performance.now() - startedAt.current,
        };

        switch (ev.type) {
          case "release": {
            next.runId = ev.run_id;
            next.arm = ev.arm;
            next.hardLimit = ev.hard_limit;
            next.ceiling = ev.stale_ceiling;
            // Seed one row per agent from the arrival offsets, so the lanes
            // exist before anyone has decided anything and the grid does not
            // reflow as results trickle in.
            const seeded = new Map<string, AgentRow>();
            ev.offsets.forEach((_, i) => {
              const id = `agent-${String(i).padStart(2, "0")}`;
              seeded.set(id, { id, decisions: [] });
            });
            next.agents = seeded;
            break;
          }
          case "policy": {
            next.policyMovedAt = ev.at_ms;
            break;
          }
          case "decision": {
            const agents = new Map(s.agents);
            const row = agents.get(ev.agent_id) ?? { id: ev.agent_id, decisions: [] };
            agents.set(ev.agent_id, {
              ...row,
              decisions: [
                ...row.decisions,
                {
                  at_ms: ev.at_ms, attempt: ev.attempt, observed: ev.observed,
                  action: ev.action, amount: ev.amount, ceiling: ev.ceiling,
                },
              ],
            });
            next.agents = agents;
            // The ceiling an agent inferred is the rule it is actually under.
            if (ev.ceiling != null) next.ceiling = ev.ceiling;
            break;
          }
          case "result": {
            const agents = new Map(s.agents);
            const row = agents.get(ev.agent_id) ?? { id: ev.agent_id, decisions: [] };
            agents.set(ev.agent_id, {
              ...row,
              result: {
                at_ms: ev.at_ms, outcome: ev.outcome, action: ev.action,
                conflicts: ev.conflicts, revised: ev.revised,
              },
            });
            next.agents = agents;
            if (ev.outcome === "committed" && ev.action) {
              const m = /allocate\((\d+)\)/.exec(ev.action);
              if (m) next.total = s.total + Number(m[1]);
            }
            break;
          }
          case "done": {
            next.done = ev;
            // Trust the server's final read over our running sum: it is the
            // committed total from the database, not an accumulation of events.
            next.total = ev.final_sum;
            next.ceiling = ev.ceiling;
            break;
          }
          case "error": {
            next.error = ev.error;
            next.running = false;
            break;
          }
          case "end": {
            next.running = false;
            break;
          }
        }
        return next;
      });

      if (ev.type === "end" || ev.type === "error") {
        es.close();
        source.current = null;
      }
    };

    es.onerror = () => {
      setState((s) =>
        s.done || !s.running
          ? { ...s, running: false }
          : {
              ...s,
              running: false,
              error:
                "lost the connection to the API. Is `python -m racelab.server` still running?",
            }
      );
      es.close();
      source.current = null;
    };
  }, []);

  return { state, start, stop };
}
