import type { Arm, LedgerState } from "../lib/types";
import type { RunState } from "../lib/useRun";

/** Arm picker, agent count, arrival window, and the two buttons. */
export function Controls({
  arms, selected, onSelect, agents, setAgents, window, setWindow,
  bounds, onRun, onReset, busy,
}: {
  arms: Arm[];
  selected: string;
  onSelect: (id: string) => void;
  agents: number;
  setAgents: (n: number) => void;
  window: number;
  setWindow: (n: number) => void;
  bounds: { min: number; max: number };
  onRun: () => void;
  onReset: () => void;
  busy: boolean;
}) {
  return (
    <div className="panel">
      <div className="panel-head"><h2>What should an agent do?</h2></div>
      <div className="panel-body">
        <div className="arms">
          {arms.map((a) => (
            <button
              key={a.id}
              className={`arm${a.id === selected ? " on" : ""}`}
              onClick={() => onSelect(a.id)}
              disabled={busy}
              title={a.needs_postgres
                ? "Runs on stock PostgreSQL — needs `docker compose up -d`"
                : `${a.backend} · ${a.isolation}`}
            >
              <span className="arm-top">
                <span className="arm-id">{a.id}</span>
                <span className="arm-name">{a.name}</span>
              </span>
              <span className="arm-blurb">{a.blurb}</span>
            </button>
          ))}
        </div>

        <div className="field">
          <label className="field-label" htmlFor="agents">
            <span>Agents</span><b>{agents}</b>
          </label>
          <input
            id="agents" type="range" min={bounds.min} max={bounds.max}
            value={agents} disabled={busy}
            onChange={(e) => setAgents(Number(e.target.value))}
          />
        </div>

        <div className="field">
          <label className="field-label" htmlFor="window">
            <span>Arrival window</span><b>{window} ms</b>
          </label>
          <input
            id="window" type="range" min={0} max={2500} step={100}
            value={window} disabled={busy}
            onChange={(e) => setWindow(Number(e.target.value))}
          />
          <p className="note" style={{ marginTop: 8, fontSize: 11.5 }}>
            How tightly the agents arrive. Narrower means more of them read
            before anyone writes — which is exactly when this goes wrong.
          </p>
        </div>

        <div className="actions">
          <button className="primary" onClick={onRun} disabled={busy}>
            {busy ? "Racing…" : "Race"}
          </button>
          <button className="ghost" onClick={onReset} disabled={busy}>
            Reset
          </button>
        </div>
      </div>
    </div>
  );
}

/** The retrieved policy — the rule that is not in any column. */
export function MemoryPanel({ ledger, policyMoved }: {
  ledger: LedgerState | null;
  policyMoved: boolean;
}) {
  return (
    <div className="panel">
      <div className="panel-head">
        <h2>The notes the agents read</h2>
        {policyMoved && <span className="pill warn">changed mid-run</span>}
      </div>
      <div className="panel-body">
        <p className="note" style={{ marginBottom: 14 }}>
          Real rows from <code>memories</code> on CockroachDB, found by meaning
          through a <code>VECTOR(1024)</code> index. The spending cap lives here
          and <b>nowhere in any column</b>.
        </p>
        {(ledger?.memories ?? []).map((m) => (
          <div
            key={m.memory_id}
            className={`mem ${m.kind === "policy" ? "policy" : ""} ${
              m.is_superseded ? "gone" : ""
            } ${m.supersedes ? "fresh" : ""}`}
          >
            <div className="mem-top">
              <span className="mem-id">{m.memory_id}</span>
              <span className={`mem-kind ${m.kind === "policy" ? "policy" : ""}`}>
                {m.kind}
              </span>
              {m.is_superseded && <span className="mem-flag">superseded</span>}
              {m.supersedes && <span className="mem-flag">arrived mid-run</span>}
            </div>
            <div className="mem-text">{m.text}</div>
          </div>
        ))}
        {!ledger && <p className="note">loading…</p>}
      </div>
    </div>
  );
}

/** Live event feed, and the verdict once the run settles. */
export function Feed({ state }: { state: RunState }) {
  const lines: { text: string; cls?: string }[] = [];
  for (const ev of state.events) {
    if (ev.type === "release")
      lines.push({ text: `${ev.offsets.length} agents released · arm ${ev.arm}` });
    else if (ev.type === "policy")
      lines.push({ text: `⚠ the cap moved at ${ev.at_ms}ms — in the notes, not the data`, cls: "w" });
    else if (ev.type === "decision" && ev.attempt > 0)
      lines.push({
        text: `${ev.agent_id} lost the race, decided again: saw $${ev.observed} → ${ev.action}`,
        cls: "c",
      });
    else if (ev.type === "result" && ev.outcome === "committed")
      lines.push({
        text: `${ev.agent_id} wrote ${ev.action}${ev.revised ? " (changed its mind)" : ""}`,
        cls: "g",
      });
    else if (ev.type === "result")
      lines.push({ text: `${ev.agent_id} ${ev.outcome}` });
    else if (ev.type === "error") lines.push({ text: `error: ${ev.error}`, cls: "c" });
  }

  const d = state.done;
  return (
    <div className="panel">
      <div className="panel-head"><h2>What happened</h2></div>
      <div className="panel-body">
        {d && (
          <>
            <p className="verdict">
              {d.over_hard_limit ? (
                <>
                  <span className="bad">${d.final_sum} against a ${d.hard_limit} budget.</span>{" "}
                  Every agent read the balance correctly and the database raised no
                  error — it was asked to protect one row at a time, and this rule
                  spans all of them.
                </>
              ) : d.breached_policy ? (
                <>
                  <span className="good">Inside the ${d.hard_limit} budget</span>, but{" "}
                  <span className="bad">past the ${d.ceiling} cap</span> that was in
                  force by the end. Re-reading the balance cannot catch this: the
                  balance was never wrong, the <i>rule</i> moved.
                </>
              ) : (
                <>
                  <span className="good">Both limits held.</span> The only arm that
                  gets here is the one that refreshes the retrieved notes as well as
                  the balance.
                </>
              )}
            </p>
            <div className="stats">
              <span>
                <b className="stat-n">{d.conflicts}</b>
                <span className="stat-l">collisions</span>
              </span>
              <span>
                <b className="stat-n">${d.final_sum}</b>
                <span className="stat-l">final total</span>
              </span>
              <span>
                <b className="stat-n">${d.ceiling}</b>
                <span className="stat-l">cap at the end</span>
              </span>
            </div>
          </>
        )}

        {state.error && (
          <div className="err" style={{ marginBottom: 12 }}>
            {state.error}
          </div>
        )}

        <div className="feed" style={{ marginTop: d ? 16 : 0 }}>
          {lines.length === 0 && <div>waiting…</div>}
          {lines.map((l, i) => (
            <div key={i} className={l.cls}>{l.text}</div>
          ))}
        </div>
      </div>
    </div>
  );
}
