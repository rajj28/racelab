"""The conflict-aware transaction wrapper. This is the library.

Everything else in this repository exists to demonstrate or measure what is in
this file. The allocation scenario is a caller of `ConflictAware`, not a part
of it: nothing here knows what an allocation is, what an account is, or that
memory retrieval involves vectors.

## What it does

A transaction that reads state, reasons over what it read, and writes a result
has a window in which another transaction can change the state the reasoning
rested on. Under SERIALIZABLE, that shows up as a client-visible `40001`. The
standard response is to retry the transaction. The response this library
implements is to retry the *decision*:

    conflict -> discard the result
             -> refresh semantic memory
             -> re-read operational state (inside the new transaction)
             -> re-run the reasoning step
             -> emit a possibly different action

The naive policy is the *same wrapper* with the re-reasoning step switched off
(`ConflictAware.naive(...)`). It still restarts the transaction and still
re-reads operational state inside the new one -- that is what competent retry
middleware does, and building a weaker baseline would make the comparison
worthless. What it does not do is re-run the reasoning step: it replays the
action it computed against the state it saw the first time. That single
difference is the independent variable of the whole project.

Note one deviation from the way this is usually described. It is tempting to
express the naive arm as "the wrapper with `reason=None`", and the first
version of this file did. That does not survive contact with the first
attempt: naive still has to *make* a decision before it can replay one, and if
that initial decision came from somewhere else than the conflict-aware arm's,
the two arms would differ in two places instead of one. So both arms take the
same required `reason` callable and use it identically for attempt 0. The flag
controls only whether it is called again after a conflict, which is precisely
the variable under test.

## What a 40001 actually tells you

It tells you the transaction could not be serialized -- that another
transaction changed state relevant to what this one read. It does not tell you
the agent was wrong, and this library does not claim otherwise. Treating that
signal as a reason to re-run reasoning is an interpretation, and the experiment
in `docs/` is the evidence for it.

## Where this does not help

If a retry would produce the same correct result regardless of what changed --
an idempotent write, an insert that depends on nothing it read -- then
re-running the reasoning step is wasted work and `ConflictAware.naive` is the
correct configuration. This is stated in the README as a scope limit rather
than left for a user to discover.
"""

from __future__ import annotations

import datetime
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol, Sequence

import psycopg

SERIALIZATION_FAILURE = "40001"
DEADLOCK_DETECTED = "40P01"
RETRYABLE_SQLSTATES = frozenset({SERIALIZATION_FAILURE, DEADLOCK_DETECTED})


class Policy(str, Enum):
    NAIVE = "naive"
    CONFLICT_AWARE = "conflict-aware"


# --------------------------------------------------------------------------
# What the caller injects
# --------------------------------------------------------------------------


class Proposal(Protocol):
    """The minimum the library needs to know about a decision.

    Deliberately one attribute. `action` is compared across attempts to decide
    whether the agent changed its mind, so it must be a stable string identity
    for the decision -- `"allocate(45)"`, not an object whose repr includes a
    timestamp. Everything else the telemetry records (`inferred_ceiling`,
    `observed_sum`, `amount`, `memory_ids`) is read with `getattr` and is
    optional, so a caller's own decision type works unmodified and the library
    stays ignorant of the domain.
    """

    action: str


@dataclass(frozen=True)
class DecisionContext:
    """Everything the reasoning step is given.

    `previous` is the proposal from the attempt that just failed, and it is
    passed deliberately: a re-reasoning function is allowed to know what it
    decided before. It is *not* allowed to be given only that, which is why
    `memory` and `observed` are refreshed before this is constructed.
    """

    agent_id: str
    attempt_no: int
    memory: Any
    observed: Any
    previous: Proposal | None


# Injected by the caller. Signatures are the entire public contract.
RefreshMemory = Callable[[str], Any]  # agent_id -> memory context
OperationalRead = Callable[[psycopg.Cursor], Any]  # runs INSIDE the transaction
Reason = Callable[[DecisionContext], Proposal]
Apply = Callable[[psycopg.Cursor, Proposal], bool]  # returns "did it write?"
Invariant = Callable[[psycopg.Cursor, Proposal], None]  # raises to abort


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass
class AttemptRecord:
    attempt_no: int
    outcome: str  # committed | abstained | conflict | error
    error_code: str | None = None
    action: str | None = None
    started_at: datetime.datetime | None = None
    ended_at: datetime.datetime | None = None


@dataclass
class RunResult:
    agent_id: str
    policy: Policy
    outcome: str  # committed | abstained | exhausted | error
    action: str | None
    decision_before: str | None
    decision_after: str | None
    conflicts: int
    attempts: list[AttemptRecord] = field(default_factory=list)
    error: str | None = None
    # Counted by the wrapper itself, not reported by the caller. This is the
    # mechanism instrumentation the arm-collapse guard runs on -- see
    # `ArmCollapse` below for why an outcome metric cannot substitute for it.
    reason_calls: int = 0
    attempts_made: int = 0
    # Counted for the same reason as reason_calls: the C-ops ablation differs
    # from C only in whether this happens, so it is instrumented rather than
    # assumed. C-ops must end every run with memory_refreshes == 0.
    memory_refreshes: int = 0

    @property
    def revised(self) -> bool:
        """Did the agent change its mind, as opposed to merely retrying?

        This is the secondary metric of the whole experiment, so the definition
        is worth being exact about: true only when at least one conflict
        occurred AND the final action differs from the first one. A run with no
        conflicts cannot be revised, and a run that conflicted five times and
        arrived back at the same action is a retry, not a revision.
        """
        return (
            self.conflicts > 0
            and self.decision_before is not None
            and self.decision_after is not None
            and self.decision_before != self.decision_after
        )


# --------------------------------------------------------------------------
# The arm-collapse guard
# --------------------------------------------------------------------------


class ArmCollapse(AssertionError):
    """The two arms stopped being two arms.

    This exists because of a specific bug that was found in this wrapper and
    would not have been found by checking results. The serialization failure is
    raised by `COMMIT`, after the decision has been made, so an exception can
    unwind past the decision. In the first implementation it did: the naive arm
    lost the decision it was supposed to replay and called the reasoning
    function again, silently behaving exactly like the conflict-aware arm.

    What makes that bug dangerous is that nothing about the output looks wrong.
    Both arms commit. Both arms produce final sums within range. Both arms
    write plausible telemetry. The experiment would have reported "no
    significant difference between naive and conflict-aware" -- a clean,
    publishable, entirely false null result, caused by the two arms being one
    arm.

    No assertion on outcomes can catch this, because the outcomes are exactly
    what a real null result looks like. The only thing that distinguishes them
    is whether the mechanism ran: how many times the reasoning function was
    actually called. So that is instrumented, on every run, and a run whose
    mechanism did not behave as its policy claims is **voided rather than
    reported**.
    """


def check_arms(naive: "RunResult", aware: "RunResult") -> None:
    """Cross-arm guard: the arms must diverge wherever a conflict occurred.

    Per-run invariants are checked inside `ConflictAware.run`. This is the
    pairwise check that the two policies actually did different amounts of
    thinking on comparable work, and it is the one that would have caught the
    original collapse.
    """
    if naive.policy is not Policy.NAIVE or aware.policy is not Policy.CONFLICT_AWARE:
        raise ArmCollapse(
            f"check_arms expects (naive, conflict-aware); got "
            f"({naive.policy.value}, {aware.policy.value})"
        )

    if naive.reason_calls != 1:
        raise ArmCollapse(
            f"naive arm called the reasoning function {naive.reason_calls} times; "
            f"the naive policy reasons exactly once, at attempt 0, and replays"
        )

    conflicted = naive.conflicts > 0 or aware.conflicts > 0
    if conflicted and aware.reason_calls <= naive.reason_calls:
        raise ArmCollapse(
            f"conflicts occurred ({naive.conflicts} naive, {aware.conflicts} "
            f"conflict-aware) but the conflict-aware arm reasoned "
            f"{aware.reason_calls} times against the naive arm's "
            f"{naive.reason_calls}; the arms have collapsed into one"
        )


# --------------------------------------------------------------------------
# Telemetry
# --------------------------------------------------------------------------


class TelemetrySink(Protocol):
    def decision(self, run_id: str, agent_id: str, attempt_no: int, policy: str,
                 proposal: Proposal, before: str | None, after: str | None,
                 revised: bool) -> None: ...

    def attempt(self, run_id: str, agent_id: str, policy: str,
                record: AttemptRecord, retry_count: int) -> None: ...

    def conflict_edges(self, run_id: str, agent_a: str,
                       partners: Sequence[str], conflict_type: str) -> None: ...


class ListTelemetry:
    """In-memory sink, for tests and for runs that should not touch a database."""

    def __init__(self) -> None:
        self.decisions: list[dict] = []
        self.attempts: list[dict] = []
        self.edges: list[dict] = []

    def decision(self, run_id, agent_id, attempt_no, policy, proposal,
                 before, after, revised) -> None:
        self.decisions.append({
            "run_id": run_id, "agent_id": agent_id, "attempt_no": attempt_no,
            "policy": policy, "action": proposal.action,
            "decision_before": before, "decision_after": after, "revised": revised,
        })

    def attempt(self, run_id, agent_id, policy, record, retry_count) -> None:
        self.attempts.append({
            "run_id": run_id, "agent_id": agent_id, "policy": policy,
            "outcome": record.outcome, "error_code": record.error_code,
            "retry_count": retry_count,
        })

    def conflict_edges(self, run_id, agent_a, partners, conflict_type) -> None:
        for b in partners:
            self.edges.append({
                "run_id": run_id, "agent_a": agent_a, "agent_b": b,
                "conflict_type": conflict_type,
            })


class SqlTelemetry:
    """Writes the telemetry tables.

    **On its own connection, and that is not incidental.** Telemetry written
    inside the raced transaction would be rolled back by the very conflict it
    is trying to record -- the rows describing failed attempts would be exactly
    the rows that never survive. It would also widen the transaction's refresh
    span, so the act of measuring would change what is measured. So the sink
    takes a separate connection in autocommit and never shares one with the
    transaction under test.
    """

    def __init__(self, conn: psycopg.Connection):
        if not conn.autocommit:
            raise ValueError(
                "SqlTelemetry needs an autocommit connection of its own; sharing the "
                "raced connection would roll telemetry back with the conflict it records"
            )
        self.conn = conn

    def decision(self, run_id, agent_id, attempt_no, policy, proposal,
                 before, after, revised) -> None:
        memory_ids = list(getattr(proposal, "memory_ids", ()) or ())
        self.conn.execute(
            """
            INSERT INTO decisions (decision_id, run_id, agent_id, attempt_no, policy,
                                   retrieved_memory_ids, inferred_ceiling, observed_sum,
                                   proposed_amount, decision_before, decision_after, revised)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(uuid.uuid4()), run_id, agent_id, attempt_no, policy,
                memory_ids,
                getattr(proposal, "inferred_ceiling", None),
                getattr(proposal, "observed_sum", None),
                getattr(proposal, "amount", None),
                before, after, revised,
            ),
        )

    def attempt(self, run_id, agent_id, policy, record, retry_count) -> None:
        self.conn.execute(
            """
            INSERT INTO agent_attempts (attempt_id, run_id, agent_id, policy, outcome,
                                        error_code, retry_count, txn_started_at, txn_ended_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(uuid.uuid4()), run_id, agent_id, policy, record.outcome,
                record.error_code, retry_count, record.started_at, record.ended_at,
            ),
        )

    def conflict_edges(self, run_id, agent_a, partners, conflict_type) -> None:
        for b in partners:
            if b == agent_a:
                continue
            self.conn.execute(
                """
                INSERT INTO conflict_edges (run_id, agent_a, agent_b, conflict_type)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (run_id, agent_a, agent_b) DO NOTHING
                """,
                (run_id, agent_a, b, conflict_type),
            )


# --------------------------------------------------------------------------
# The wrapper
# --------------------------------------------------------------------------


class ConflictAware:
    """Run a read-reason-write transaction, re-deciding when it cannot serialize.

    Parameters that are callables are the whole point: the library supplies the
    protocol and the telemetry, the caller supplies the domain.

    Prefer the `naive` and `conflict_aware` constructors over calling this
    directly; they differ in one argument and nothing else, which is the
    property the experiment depends on.

    `re_reason` defaults to True. The library ships the behaviour it argues
    for, and the baseline is the thing you have to ask for.
    """

    @classmethod
    def naive(cls, **kwargs) -> "ConflictAware":
        """Reason once, then replay that action through every restart.

        **This is a baseline, not a recommended setting.** It models what
        standard transaction-retry middleware does: restart on a serialization
        failure, re-read state inside the new transaction, and re-issue the
        work. That is correct for an idempotent write or one that depends on
        nothing it read, and it is what most applications already have.

        It is wrong precisely when the decision was derived from the state that
        changed, because the re-read state is fetched and then ignored. Use it
        to measure that gap, or in the cases described under "Where this does
        not help" above -- not as a default.
        """
        return cls(re_reason=False, **kwargs)

    @classmethod
    def conflict_aware(cls, **kwargs) -> "ConflictAware":
        """Treat a serialization failure as a reason to decide again.

        The same as constructing `ConflictAware(...)` directly, named so that
        call sites in the experiment state their arm explicitly.
        """
        return cls(re_reason=True, **kwargs)

    def __init__(
        self,
        *,
        operational_read: OperationalRead,
        apply: Apply,
        reason: Reason,
        re_reason: bool = True,
        refresh_memory: RefreshMemory | None = None,
        # Splitting "retrieve memory" from "refresh memory on conflict" is what
        # makes the C-ops ablation expressible. With this False, memory is
        # retrieved once before the first attempt and then held stale, so
        # re-reasoning happens over fresh operational state and stale semantic
        # context -- isolating which of the two refreshes does the work. See
        # racelab/arms.py.
        refresh_memory_on_conflict: bool = True,
        invariant: Invariant | None = None,
        conflict_partners: Callable[[psycopg.Connection], Sequence[str]] | None = None,
        isolation: str | None = None,
        max_attempts: int = 5,
        reasoning_gap_ms: float = 0.0,
        telemetry: TelemetrySink | None = None,
    ):
        self.operational_read = operational_read
        self.apply = apply
        self.reason = reason
        self.re_reason = re_reason
        self.refresh_memory = refresh_memory
        self.refresh_memory_on_conflict = refresh_memory_on_conflict
        self.invariant = invariant
        self.conflict_partners = conflict_partners
        self.isolation = isolation
        self.max_attempts = max_attempts
        # Stands in for the latency of a live reasoning call. The 100-run
        # experiment replays cached model intents (METHODOLOGY entry 2), so
        # without this the read-to-write window would be microseconds and would
        # not resemble an agent that actually thinks. Held identical across arms.
        self.reasoning_gap_ms = reasoning_gap_ms
        self.telemetry = telemetry

    @property
    def policy(self) -> Policy:
        return Policy.CONFLICT_AWARE if self.re_reason else Policy.NAIVE

    def run(self, conn: psycopg.Connection, *, agent_id: str, run_id: str) -> RunResult:
        if not conn.autocommit:
            raise ValueError(
                "ConflictAware manages BEGIN/COMMIT itself and needs an autocommit "
                "connection; an implicit transaction would collapse the read and the "
                "write into one statement batch and hide the race"
            )

        policy = self.policy
        result = RunResult(
            agent_id=agent_id, policy=policy, outcome="error", action=None,
            decision_before=None, decision_after=None, conflicts=0,
        )

        memory = self.refresh_memory(agent_id) if self.refresh_memory else None
        proposal: Proposal | None = None

        # Count calls to the reasoning function here, inside the library, so
        # the arm-collapse guard runs on what the mechanism actually did rather
        # than on what the caller believes it configured.
        calls = {"n": 0}

        def counted(ctx: DecisionContext) -> Proposal:
            calls["n"] += 1
            return self.reason(ctx)

        # The serialization failure is raised by COMMIT, which is *after* the
        # decision has been made -- so an exception unwinds past the return
        # value and the decision would be lost. The naive policy is defined by
        # carrying that decision forward, so it cannot be lost. `_attempt`
        # therefore publishes each proposal here as soon as it exists, rather
        # than only on the success path.
        holder: dict[str, Proposal] = {}

        for attempt_no in range(self.max_attempts):
            record = AttemptRecord(
                attempt_no=attempt_no,
                outcome="error",
                started_at=datetime.datetime.now(datetime.timezone.utc),
            )
            try:
                result.attempts_made = attempt_no + 1
                proposal, wrote = self._attempt(
                    conn, agent_id=agent_id, run_id=run_id, attempt_no=attempt_no,
                    memory=memory, carried=proposal, result=result, holder=holder,
                    reason=counted,
                )
            except psycopg.Error as exc:
                self._rollback(conn)
                record.ended_at = datetime.datetime.now(datetime.timezone.utc)
                record.error_code = exc.sqlstate
                computed = holder.get("proposal")
                record.action = computed.action if computed else None

                if exc.sqlstate not in RETRYABLE_SQLSTATES:
                    record.outcome = "error"
                    result.outcome = "error"
                    result.error = f"{exc.sqlstate}: {exc}"
                    self._emit_attempt(run_id, agent_id, policy, record, attempt_no)
                    return self._finish(result, calls["n"])

                # A serialization failure. Another transaction changed state
                # relevant to this decision; it does not follow that this
                # decision was wrong.
                record.outcome = "conflict"
                result.conflicts += 1
                self._emit_attempt(run_id, agent_id, policy, record, attempt_no)
                self._emit_edges(conn, run_id, agent_id)

                if self.re_reason:
                    # Conflict-aware: discard the result and refresh BOTH
                    # sources the decision drew on. Memory is refreshed here,
                    # outside the transaction; the operational aggregate is
                    # re-read inside the next one. Dropping `proposal` is what
                    # forces the reasoning step to run again.
                    if self.refresh_memory and self.refresh_memory_on_conflict:
                        memory = self.refresh_memory(agent_id)
                        result.memory_refreshes += 1
                    proposal = None
                else:
                    # Naive: carry the decision into the next attempt unchanged.
                    # That transaction still re-reads operational state -- it
                    # just does not let the new reading change the action.
                    proposal = holder.get("proposal")
                continue

            record.ended_at = datetime.datetime.now(datetime.timezone.utc)
            record.action = proposal.action if proposal else None
            record.outcome = "committed" if wrote else "abstained"
            result.outcome = record.outcome
            result.action = record.action
            self._emit_attempt(run_id, agent_id, policy, record, attempt_no)
            return self._finish(result, calls["n"])

        result.outcome = "exhausted"
        result.action = result.decision_after
        return self._finish(result, calls["n"])

    def _finish(self, result: RunResult, reason_calls: int) -> RunResult:
        """Record how much thinking happened, and refuse to return a collapsed arm.

        The per-run invariant is exact rather than approximate, which is what
        makes it useful:

          naive           reasons once, at attempt 0, however many times it
                          restarts -- so `reason_calls == 1`
          conflict-aware  reasons once per attempt, because every restart
                          re-decides -- so `reason_calls == attempts_made`

        A violation is raised rather than logged. A run whose mechanism did not
        match its declared policy is not a result, and the harness is expected
        to void it. Silently reporting it is the failure mode this whole guard
        exists to prevent.
        """
        result.reason_calls = reason_calls

        if result.policy is Policy.NAIVE and reason_calls != 1:
            raise ArmCollapse(
                f"naive run for {result.agent_id} called the reasoning function "
                f"{reason_calls} times across {result.attempts_made} attempts; "
                f"the naive policy reasons exactly once and replays. It is "
                f"behaving as conflict-aware, so the arms are not distinct."
            )

        if result.policy is Policy.CONFLICT_AWARE and reason_calls != result.attempts_made:
            raise ArmCollapse(
                f"conflict-aware run for {result.agent_id} called the reasoning "
                f"function {reason_calls} times across {result.attempts_made} "
                f"attempts; every attempt must re-decide. It is replaying like "
                f"the naive policy, so the arms are not distinct."
            )

        # The C-ops ablation is defined by NOT refreshing memory. If it ever
        # does, it has silently become the C arm, and the decomposition it
        # exists to produce -- how much of the effect is the operational
        # re-read, how much is the memory refresh -- becomes meaningless while
        # still producing numbers.
        if not self.refresh_memory_on_conflict and result.memory_refreshes:
            raise ArmCollapse(
                f"run for {result.agent_id} refreshed memory "
                f"{result.memory_refreshes} times with refresh_memory_on_conflict "
                f"disabled; the ablation arm has collapsed into the full arm."
            )

        expected_refreshes = (
            result.conflicts
            if (self.re_reason and self.refresh_memory and self.refresh_memory_on_conflict)
            else 0
        )
        if result.memory_refreshes != expected_refreshes:
            raise ArmCollapse(
                f"run for {result.agent_id} refreshed memory "
                f"{result.memory_refreshes} times against {result.conflicts} "
                f"conflicts; expected {expected_refreshes}. The memory refresh "
                f"is not tracking the conflicts it is supposed to respond to."
            )

        return result

    # -- one transaction --------------------------------------------------

    def _attempt(
        self,
        conn: psycopg.Connection,
        *,
        agent_id: str,
        run_id: str,
        attempt_no: int,
        memory: Any,
        carried: Proposal | None,
        result: RunResult,
        holder: dict[str, Proposal],
        reason: Reason,
    ) -> tuple[Proposal, bool]:
        begin = "BEGIN"
        if self.isolation:
            begin = f"BEGIN TRANSACTION ISOLATION LEVEL {self.isolation}"
        conn.execute(begin)

        with conn.cursor() as cur:
            # The operational read is inside the transaction, always, for both
            # policies. This is what makes the conflict a real one: the read
            # joins the transaction's refresh span, so a write that lands
            # against it afterwards is what the database refuses to serialize.
            observed = self.operational_read(cur)

            if self.reasoning_gap_ms:
                time.sleep(self.reasoning_gap_ms / 1000.0)

            if carried is not None:
                # Naive replay. The fresh `observed` is discarded on purpose --
                # documenting the exact thing the baseline gets wrong.
                proposal = carried
            else:
                proposal = reason(DecisionContext(
                    agent_id=agent_id, attempt_no=attempt_no,
                    memory=memory, observed=observed, previous=result_previous(result),
                ))

            holder["proposal"] = proposal

            # `decision_before` is written once and never again: it has to hold
            # the ORIGINAL action, or comparing it against `decision_after`
            # stops answering "did the agent change its mind" and starts
            # answering nothing at all. An earlier version of this overwrote it
            # on every re-reason, which silently forced `revised` to false and
            # turned the experiment into a count of database retries.
            if result.decision_before is None:
                result.decision_before = proposal.action
            result.decision_after = proposal.action

            self._emit_decision(run_id, agent_id, attempt_no, proposal, result)

            wrote = self.apply(cur, proposal)
            if self.invariant is not None:
                self.invariant(cur, proposal)

        conn.execute("COMMIT")
        return proposal, wrote

    # -- plumbing ---------------------------------------------------------

    @staticmethod
    def _rollback(conn: psycopg.Connection) -> None:
        try:
            conn.execute("ROLLBACK")
        except psycopg.Error:
            pass  # the server may already have aborted it

    def _emit_decision(self, run_id, agent_id, attempt_no, proposal, result) -> None:
        if self.telemetry is None:
            return
        revised = (
            attempt_no > 0
            and result.decision_before is not None
            and result.decision_before != proposal.action
        )
        self.telemetry.decision(
            run_id, agent_id, attempt_no, self.policy.value, proposal,
            result.decision_before, proposal.action, revised,
        )

    def _emit_attempt(self, run_id, agent_id, policy, record, retry_count) -> None:
        if self.telemetry is not None:
            self.telemetry.attempt(run_id, agent_id, policy.value, record, retry_count)

    def _emit_edges(self, conn, run_id, agent_id) -> None:
        """Record who this transaction could not serialize against.

        The partner lookup is supplied by the caller because only the caller
        knows what "contended with" means for its own data. It is an
        *inference* from what committed during our window, not a reading of the
        cluster's internal contention record -- CockroachDB knows the real
        answer and does not readily expose it per-transaction to the client
        (see docs/FEEDBACK.md, entry 4). The edge type says so, so that nothing
        downstream mistakes an inference for a measurement.
        """
        if self.telemetry is None or self.conflict_partners is None:
            return
        try:
            partners = self.conflict_partners(conn)
        except psycopg.Error:
            return
        self.telemetry.conflict_edges(run_id, agent_id, partners, "inferred_write_overlap")


def result_previous(result: RunResult) -> Proposal | None:
    # The reasoning step is told what was decided last, as a string identity
    # only -- it re-derives everything else from refreshed state rather than
    # inheriting it.
    return None if result.decision_after is None else _PreviousAction(result.decision_after)


@dataclass(frozen=True)
class _PreviousAction:
    action: str
