import { useCallback, useEffect, useState } from "react";
import { Controls, Feed, MemoryPanel } from "./components/Panels";
import { Stage } from "./components/Stage";
import { useRun } from "./lib/useRun";
import type { Arm, LedgerState } from "./lib/types";

export default function App() {
  const [arms, setArms] = useState<Arm[]>([]);
  const [bounds, setBounds] = useState({ min: 2, max: 14 });
  const [selected, setSelected] = useState("B");
  const [agents, setAgents] = useState(8);
  const [window_, setWindow] = useState(600);
  const [ledger, setLedger] = useState<LedgerState | null>(null);
  const [apiDown, setApiDown] = useState(false);

  const { state, start } = useRun();

  const loadLedger = useCallback(() => {
    fetch("/api/state")
      .then((r) => r.json())
      .then((d) => { setLedger(d); setApiDown(false); })
      .catch(() => setApiDown(true));
  }, []);

  useEffect(() => {
    fetch("/api/arms")
      .then((r) => r.json())
      .then((d) => {
        setArms(d.arms);
        setBounds({ min: d.min_agents, max: d.max_agents });
        setApiDown(false);
      })
      .catch(() => setApiDown(true));
    loadLedger();
  }, [loadLedger]);

  // The ledger and the notes both change during a run, so re-read them when it
  // settles rather than leaving a stale panel beside a finished race.
  useEffect(() => { if (state.done) loadLedger(); }, [state.done, loadLedger]);

  const reset = useCallback(() => {
    fetch("/api/reset", { method: "POST" }).then(loadLedger).catch(() => setApiDown(true));
  }, [loadLedger]);

  const run = useCallback(() => {
    fetch("/api/reset", { method: "POST" })
      .then(() => { loadLedger(); start(selected, agents, window_); })
      .catch(() => setApiDown(true));
  }, [selected, agents, window_, start, loadLedger]);

  return (
    <div className="shell">
      <header className="masthead">
        <div>
          <div className="brand">
            <h1>RaceLab</h1>
            <span className="tag">live · real cluster</span>
          </div>
          <p>
            Agents that share a limited pool quietly hand out more than exists.
            Pick how they should behave when two of them collide, then watch a
            real race against CockroachDB — real connections, real serialization
            failures, real rows.
          </p>
        </div>
      </header>

      {apiDown && (
        <div className="err" style={{ marginBottom: 18 }}>
          Can't reach the API. Start it with{" "}
          <code>python -m racelab.server</code> — it needs a CockroachDB
          connection string in <code>.env</code>.
        </div>
      )}

      <div className="layout">
        <div>
          <Controls
            arms={arms}
            selected={selected}
            onSelect={setSelected}
            agents={agents}
            setAgents={setAgents}
            window={window_}
            setWindow={setWindow}
            bounds={bounds}
            onRun={run}
            onReset={reset}
            busy={state.running}
          />
        </div>

        <div>
          <Stage state={state} />
          <Feed state={state} />
        </div>

        <div>
          <MemoryPanel ledger={ledger} policyMoved={state.policyMovedAt != null} />
          <div className="panel">
            <div className="panel-head"><h2>Try this</h2></div>
            <div className="panel-body">
              <p className="note">
                <b>1.</b> Race <b>B — told, and ignores it</b>. It is told about
                every collision and still overshoots, because retrying re-sends
                the answer that just went stale.
              </p>
              <p className="note" style={{ marginTop: 10 }}>
                <b>2.</b> Race <b>C-ops</b>. The budget now holds — but watch the
                cap. It re-read the balance, not the notes.
              </p>
              <p className="note" style={{ marginTop: 10 }}>
                <b>3.</b> Race <b>C</b>. Both hold. That is the whole result, and
                the difference between the last two is one boolean.
              </p>
              <p className="note" style={{ marginTop: 10 }}>
                Narrow the arrival window to <b>0 ms</b> for a thundering herd.
              </p>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
