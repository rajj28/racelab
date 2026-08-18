import type { RunState } from "../lib/useRun";
import type { AgentRow } from "../lib/types";

const fmt = (n: number) => `$${n}`;

/** The ledger bar: committed total against the two limits that can break. */
function Bar({ total, hardLimit, ceiling }: {
  total: number; hardLimit: number; ceiling: number | null;
}) {
  // Scale so the bar stays informative when an arm overshoots badly. The two
  // limits must always remain visible, or the picture stops making its point.
  const max = Math.max(hardLimit * 1.6, total * 1.12, (ceiling ?? 0) * 1.6);
  const pct = (v: number) => `${Math.min(100, (v / max) * 100)}%`;

  return (
    <div className="bar-host">
      <div className="bar">
        <div
          className={`bar-fill${total > hardLimit ? " over" : ""}`}
          style={{ width: pct(total) }}
        />
        {ceiling != null && (
          <div className="mark cap" style={{ left: pct(ceiling) }}>
            <span>cap {fmt(ceiling)}</span>
          </div>
        )}
        <div className="mark limit" style={{ left: pct(hardLimit) }}>
          <span>budget {fmt(hardLimit)}</span>
        </div>
      </div>
      <div className="scale">
        <span>$0</span>
        <span>{fmt(Math.round(max))}</span>
      </div>
    </div>
  );
}

/** One row per agent: when it read, how long it thought, and how it ended. */
function Lane({ row, span, policyAt }: {
  row: AgentRow; span: number; policyAt: number | null;
}) {
  const x = (ms: number) => `${Math.min(100, (ms / span) * 100)}%`;
  const last = row.decisions[row.decisions.length - 1];
  const out = row.result;

  const outClass =
    out?.outcome === "committed" ? "commit"
      : out && out.outcome !== "committed" ? "clash"
      : "idle";

  return (
    <div className="lane">
      <span className="lane-id">{row.id.replace("agent-", "agent ")}</span>
      <div className="lane-track">
        {policyAt != null && (
          <div className="policy-line" style={{ left: x(policyAt) }} />
        )}
        {row.decisions.map((d, i) => {
          // A decision that was followed by another attempt is one that lost a
          // race. Drawn from where it was made to where the next one started.
          const next = row.decisions[i + 1];
          const end = next ? next.at_ms : out?.at_ms ?? d.at_ms + 40;
          return (
            <span
              key={i}
              className="tick"
              style={{ left: x(d.at_ms), width: x(Math.max(12, end - d.at_ms)) }}
              title={`attempt ${d.attempt}: saw ${fmt(d.observed)}, chose ${d.action}`}
            />
          );
        })}
        {/* A collision is marked where the *next* attempt began: that is the
            moment the agent learned its answer was stale. */}
        {row.decisions.slice(0, -1).map((_, i) => (
          <span key={`c${i}`} className="dot clash" style={{ left: x(row.decisions[i + 1].at_ms) }} />
        ))}
        {out && (
          <span
            className={`dot ${out.outcome === "committed" ? "commit" : "abstain"}`}
            style={{ left: x(out.at_ms) }}
          />
        )}
      </div>
      <span className={`lane-out ${outClass}`}>
        {out
          ? out.outcome === "committed"
            ? `+${last?.amount ?? ""}${out.revised ? " ↻" : ""}`
            : out.outcome
          : last
          ? "thinking"
          : "—"}
      </span>
    </div>
  );
}

export function Stage({ state }: { state: RunState }) {
  const rows = [...state.agents.values()].sort((a, b) => a.id.localeCompare(b.id));
  const span = Math.max(
    600,
    ...rows.flatMap((r) => [
      ...r.decisions.map((d) => d.at_ms),
      r.result?.at_ms ?? 0,
    ]),
    state.policyMovedAt ?? 0
  ) * 1.04;

  const done = state.done;
  const over = done ? done.over_hard_limit : state.total > state.hardLimit;
  const settled = Boolean(done);

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>Live · {state.arm ? `arm ${state.arm}` : "idle"}</h2>
        <span
          className={`pill ${
            state.running ? "warn" : !settled ? "idle" : over ? "bad" : "ok"
          }`}
        >
          {state.running
            ? "racing"
            : !settled
            ? "ready"
            : over
            ? "over the budget"
            : done!.breached_policy
            ? "budget held, cap breached"
            : "both limits held"}
        </span>
      </div>

      <div className="panel-body">
        <div className="readout">
          <span
            className={`big${over ? " over" : settled && !over ? " held" : ""}`}
          >
            {fmt(state.total)}
          </span>
          <span className="big-label">approved</span>
          {state.runId && (
            <span className="pill idle" title="the real run id, written to race_runs">
              {state.runId}
            </span>
          )}
        </div>

        <Bar total={state.total} hardLimit={state.hardLimit} ceiling={state.ceiling} />

        {rows.length > 0 && (
          <>
            <div className="lanes">
              {rows.map((r) => (
                <Lane key={r.id} row={r} span={span} policyAt={state.policyMovedAt} />
              ))}
            </div>
            <div className="legend">
              <span><i className="sw think" />thinking</span>
              <span><i className="sw commit" />wrote</span>
              <span><i className="sw clash" />lost the race, decided again</span>
              <span><i className="sw policy" />the cap changed here</span>
              <span>↻ changed its mind</span>
            </div>
          </>
        )}

        {rows.length === 0 && !state.running && (
          <p className="note" style={{ marginTop: 20 }}>
            Pick an approach on the left and press <b>Race</b>. Every agent opens
            its own connection to CockroachDB and writes to a real ledger — the
            collisions you see are real serialization failures.
          </p>
        )}
      </div>
    </div>
  );
}
