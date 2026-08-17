"""The reasoning agent: Claude on Bedrock, choosing from the bounded action space.

This is the step the whole project is about. The agent is given retrieved
memories and an operational reading, and must decide what to allocate. It is
*not* given the ceiling. The ceiling exists only inside the retrieved memory
text, so an agent that ignores its memory cannot get the right answer, and an
agent whose memory is refreshed can reach a different one. That is what makes
the vector index part of the mechanism rather than decoration.

## Strict JSON, enforced by the API rather than by parsing

The action space is `{allocate(45), allocate(40), allocate(35), abstain}`.
Rather than asking for JSON in the prompt and hoping, the decision is taken
through a Bedrock `toolConfig` with `toolChoice` forcing the tool -- so the
shape is enforced by the API and an `amount` outside the action space is
rejected before it can reach the database.

## No silent fallback

If the model returns something unusable, this raises. It does not quietly
substitute the deterministic reference implementation from `decide.py`.

That would be the single most damaging shortcut available here: the experiment
compares policies, and an arm that silently swapped a language model for a
deterministic function on some fraction of its decisions would produce a number
that means nothing, with no indication in the output that anything had happened.
Model failures are loud, and because generation happens in a separate stage
(see `intents.py`) they surface before the experiment runs rather than during
it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .decide import ABSTAIN, ALLOCATION_OPTIONS, Decision

DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

SYSTEM_PROMPT = """\
You are an allocation agent operating against a shared authorization budget.

You will be given:
  - memories retrieved from your semantic memory for this account
  - the total already allocated against this account, read from the database
  - the account's hard limit

Your memories may contain an authorization ceiling set by policy. That ceiling
may be lower than the hard limit, or higher. Policy in your memory NEVER
authorizes exceeding the hard limit -- the hard limit always binds.

Your memories may also contain figures that are not authorization ceilings:
historical averages, backlog totals, contact notes. Do not treat those as
authorization.

Choose the largest allocation that keeps the running total within BOTH the
effective ceiling and the hard limit. If no allocation fits, abstain.

Decide only from the information given. Do not assume state you were not told.
"""

USER_TEMPLATE = """\
Retrieved memories for account {account_id}:
{memories}

Already allocated against this account: ${observed_sum}
Hard limit for this account: ${hard_limit}

Choose one action.
"""

TOOL_SPEC = {
    "toolSpec": {
        "name": "submit_allocation_decision",
        "description": "Submit the chosen action for this account.",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [f"allocate({a})" for a in ALLOCATION_OPTIONS] + [ABSTAIN],
                        "description": "The chosen action.",
                    },
                    "inferred_ceiling": {
                        "type": ["integer", "null"],
                        "description": (
                            "The effective authorization ceiling you inferred from "
                            "the retrieved memories, or null if none was stated."
                        ),
                    },
                    "rationale": {
                        "type": "string",
                        "description": "One sentence. Cite the figure you relied on.",
                    },
                },
                "required": ["action", "inferred_ceiling", "rationale"],
            }
        },
    }
}

VALID_ACTIONS = frozenset(
    [f"allocate({a})" for a in ALLOCATION_OPTIONS] + [ABSTAIN]
)


class AgentError(RuntimeError):
    """The model produced something that cannot be used as a decision."""


@dataclass
class ClaudeAgent:
    model_id: str | None = None
    region: str | None = None
    temperature: float = 0.0
    max_tokens: int = 512

    def __post_init__(self) -> None:
        import boto3

        self.model_id = self.model_id or os.environ.get(
            "RACELAB_REASON_MODEL_ID", DEFAULT_MODEL_ID
        )
        self.region = self.region or os.environ.get("AWS_REGION") or "us-east-1"
        self._client = boto3.client("bedrock-runtime", region_name=self.region)

    def decide(
        self,
        *,
        account_id: str,
        memories,
        observed_sum: int,
        hard_limit: int,
    ) -> Decision:
        rendered = "\n".join(f"  - [{m.kind}] {m.text}" for m in memories) or "  (none)"
        user = USER_TEMPLATE.format(
            account_id=account_id,
            memories=rendered,
            observed_sum=observed_sum,
            hard_limit=hard_limit,
        )

        response = self._client.converse(
            modelId=self.model_id,
            system=[{"text": SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            toolConfig={
                "tools": [TOOL_SPEC],
                # Force the tool. Without this the model may answer in prose,
                # which would have to be parsed, which is the failure mode the
                # tool exists to remove.
                "toolChoice": {"tool": {"name": TOOL_SPEC["toolSpec"]["name"]}},
            },
            inferenceConfig={"maxTokens": self.max_tokens, "temperature": self.temperature},
        )

        payload = _extract_tool_input(response)
        action = payload.get("action")
        if action not in VALID_ACTIONS:
            raise AgentError(
                f"model returned action {action!r}, which is outside the action "
                f"space {sorted(VALID_ACTIONS)}"
            )

        amount = None if action == ABSTAIN else int(action[len("allocate("):-1])

        # The hard limit binds regardless of what the model concluded. This is
        # not a correction of the model's reasoning and it is not a safety net
        # that hides errors -- a violation of it is recorded in the rationale
        # and remains visible in telemetry. It is the same rule the reference
        # implementation applies, applied in the same place, so that the two
        # differ only in how the action was chosen.
        if amount is not None and observed_sum + amount > hard_limit:
            payload["rationale"] = (
                f"[hard limit enforced by the scenario, not the model] "
                f"{payload.get('rationale', '')}"
            )

        return Decision(
            action=action,
            amount=amount,
            inferred_ceiling=_as_int_or_none(payload.get("inferred_ceiling")),
            observed_sum=observed_sum,
            rationale=str(payload.get("rationale", ""))[:500],
            memory_ids=tuple(m.memory_id for m in memories),
            source="model",
        )


def _extract_tool_input(response: dict) -> dict:
    content = response.get("output", {}).get("message", {}).get("content", [])
    for block in content:
        if "toolUse" in block:
            return dict(block["toolUse"].get("input", {}))
    raise AgentError(
        "model returned no tool use despite toolChoice forcing it; "
        f"stop reason {response.get('stopReason')!r}, content {json.dumps(content)[:300]}"
    )


def _as_int_or_none(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
