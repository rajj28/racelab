/** The wire format. Mirrors `racelab/server.py`; keep the two in step. */

export type Arm = {
  id: string;
  name: string;
  blurb: string;
  backend: string;
  isolation: string;
  re_reason: boolean;
  refresh_memory: boolean;
  needs_postgres: boolean;
};

export type Memory = {
  memory_id: string;
  kind: string;
  text: string;
  supersedes: string | null;
  is_superseded: boolean;
};

export type LedgerState = {
  account: string;
  total: number;
  hard_limit: number;
  memories: Memory[];
};

/** Events streamed while a run is in flight. */
export type RunEvent =
  | {
      type: "release";
      run_id: string;
      arm: string;
      offsets: number[];
      hard_limit: number;
      stale_ceiling: number;
      current_ceiling: number;
    }
  | { type: "policy"; at_ms: number }
  | {
      type: "decision";
      agent_id: string;
      attempt: number;
      at_ms: number;
      observed: number;
      action: string;
      amount: number | null;
      ceiling: number | null;
      rationale: string;
      memories: { memory_id: string; kind: string; text: string }[];
    }
  | {
      type: "result";
      agent_id: string;
      at_ms: number;
      outcome: string;
      action: string | null;
      conflicts: number;
      revised: boolean;
      attempts: number;
      reason_calls: number;
      memory_refreshes: number;
    }
  | {
      type: "done";
      run_id: string;
      arm: string;
      final_sum: number;
      hard_limit: number;
      ceiling: number;
      over_hard_limit: boolean;
      breached_policy: boolean;
      conflicts: number;
      voided: boolean;
      void_reason: string | null;
    }
  | { type: "error"; error: string }
  | { type: "end" };

/** What the UI keeps per agent while a run plays out. */
export type AgentRow = {
  id: string;
  decisions: {
    at_ms: number;
    attempt: number;
    observed: number;
    action: string;
    amount: number | null;
    ceiling: number | null;
  }[];
  result?: {
    at_ms: number;
    outcome: string;
    action: string | null;
    conflicts: number;
    revised: boolean;
  };
};
