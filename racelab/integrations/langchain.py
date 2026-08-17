"""A LangChain tool that treats a serialization failure as a reason to re-decide.

## The gap this fills

LangChain and LangGraph agents act through tools. A tool that reads state,
returns it to the model, and later writes a decision derived from it has exactly
the problem this project measures: another agent can change that state in
between. The framework has retry and error handling, and neither of them helps
here, because from the framework's point of view nothing went wrong -- a
`40001` is just an exception to retry, and retrying replays the same tool call
with the same arguments.

There is no notion in LangChain of *"the state your last answer was derived from
has changed; reconsider."* That is what this adds.

## What it does

`ConflictAwareTool` wraps the read-decide-write cycle in a single tool call and
runs it under `ConflictAware`. When the database reports that the transaction
could not be serialized, it does not replay the decision: it re-reads state and
invokes the caller's reasoning step again, which may be a LangChain `Runnable`
and therefore an actual model call. Only when the reasoning step has produced a
decision against current state does the tool return.

So the retry loop lives *below* the tool boundary rather than above it. That is
deliberate: an agent that sees a tool raise and calls it again has already lost
the read it reasoned about, and LangGraph would checkpoint a state that never
committed.

## What is deliberately not here

No vector store and no chat history. `langchain-cockroachdb` already provides
`AsyncCockroachDBVectorStore`, `CockroachDBChatMessageHistory` and
`CockroachDBSaver`, and reimplementing them would be duplication. This composes
with them: use their vector store for retrieval, their checkpointer for graph
state, and this for the write that must not be replayed blind.

## Install

    pip install langchain-core          # this module
    pip install langchain-cockroachdb   # optional, for the vector store

## Use

    from racelab.integrations.langchain import ConflictAwareTool

    tool = ConflictAwareTool(
        name="allocate_budget",
        description="Allocate against the shared account budget.",
        connect=lambda: psycopg.connect(dsn, autocommit=True),
        operational_read=read_running_total,   # (cursor) -> int
        decide=chain,                          # Runnable | callable
        apply=write_allocation,                # (cursor, decision) -> bool
    )

    agent = create_agent(model, tools=[tool])

`decide` receives a dict describing what is currently true and returns the
decision. It is re-invoked on every conflict, so if it is a `Runnable` backed by
a model, the model genuinely reconsiders.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable

from ..conflict import ConflictAware, DecisionContext, RunResult

_MISSING = (
    "racelab.integrations.langchain needs langchain-core.\n"
    "    pip install langchain-core"
)

try:
    from langchain_core.tools import BaseTool
except ImportError as exc:  # pragma: no cover - exercised by absence
    raise ImportError(_MISSING) from exc


@dataclasses.dataclass
class _Proposal:
    """Adapts whatever `decide` returned to the wrapper's `Proposal` protocol.

    The protocol needs one attribute, `action`, for telemetry and for the
    arm-collapse guard. The payload is carried through untouched so `apply` sees
    exactly what the reasoning step produced.
    """

    action: str
    payload: Any

    @classmethod
    def of(cls, value: Any) -> "_Proposal":
        if value is None:
            return cls(action="abstain", payload=None)
        # A LangChain Runnable commonly returns a message or a dict. Prefer an
        # explicit `action`, fall back to a readable repr rather than guessing.
        if isinstance(value, dict) and "action" in value:
            return cls(action=str(value["action"]), payload=value)
        action = getattr(value, "action", None)
        if action is not None:
            return cls(action=str(action), payload=value)
        content = getattr(value, "content", None)
        return cls(action=str(content if content is not None else value)[:200],
                   payload=value)


class ConflictAwareTool(BaseTool):
    """A tool whose write is re-decided, not replayed, when state moves.

    Set `return_result=True` to get the full `RunResult` back instead of the
    action string. The result carries the counts that make the behaviour
    auditable -- `conflicts`, `reason_calls`, `revised` -- and an agent framework
    that hides those is an agent framework in which this bug is invisible.
    """

    name: str = "conflict_aware_write"
    description: str = (
        "Read shared state, decide, and write atomically. If another writer "
        "changes the state this decision depended on, the decision is made "
        "again against current state rather than replayed."
    )

    connect: Callable[[], Any]
    operational_read: Callable[..., Any]
    decide: Any
    apply: Callable[..., bool]

    max_attempts: int = 5
    isolation: str | None = None
    refresh_context: Callable[[str], Any] | None = None
    return_result: bool = False
    agent_id: str = "langchain-agent"

    # BaseTool is a pydantic model; these are plain callables and Runnables.
    model_config = {"arbitrary_types_allowed": True}

    def _invoke_decide(self, ctx: DecisionContext) -> Any:
        """Call the reasoning step, whether it is a Runnable or a callable.

        The payload names what is true *now*, not what was true when the tool
        was called, which is the entire point of re-invoking it.
        """
        payload = {
            "observed_state": ctx.observed,
            "attempt_no": ctx.attempt_no,
            "context": ctx.memory,
            "retry_after_conflict": ctx.attempt_no > 0,
        }
        invoke = getattr(self.decide, "invoke", None)
        if callable(invoke):
            return invoke(payload)
        return self.decide(payload)

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        wrapper = ConflictAware(
            operational_read=self.operational_read,
            apply=lambda cur, proposal: self.apply(cur, proposal.payload),
            reason=lambda ctx: _Proposal.of(self._invoke_decide(ctx)),
            re_reason=True,  # the whole reason this class exists
            refresh_memory=self.refresh_context,
            refresh_memory_on_conflict=self.refresh_context is not None,
            isolation=self.isolation,
            max_attempts=self.max_attempts,
        )

        conn = self.connect()
        try:
            result: RunResult = wrapper.run(
                conn, agent_id=self.agent_id, run_id=f"lc-{self.name}"
            )
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - closing must not mask the result
                pass

        if self.return_result:
            return result
        if result.outcome == "error":
            # Surfaced, not swallowed. A tool that reports success on an error
            # would let the agent build on a write that never landed.
            raise RuntimeError(f"{self.name} failed: {result.error}")
        return result.action if result.action is not None else "abstain"


def describe(result: RunResult) -> str:
    """A one-line, model-readable summary of what the protocol did.

    Worth returning to the model when the tool re-decided: an agent told only
    the final action cannot know its first answer was discarded, and may narrate
    a plan it no longer executed.
    """
    if result.conflicts == 0:
        return f"{result.action} (committed first try)"
    changed = "changed" if result.revised else "unchanged"
    return (
        f"{result.action} after {result.conflicts} serialization "
        f"failure(s); decision {changed} "
        f"({result.decision_before} -> {result.decision_after}); "
        f"reasoned {result.reason_calls} time(s)"
    )
