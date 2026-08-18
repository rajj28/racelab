"""AWS-side plumbing: secrets, structured logs, and custom metrics.

Three things a production agent needs that a demo usually skips, kept together
because they share one property — each has a working local fallback, so the
repository runs on a laptop with no AWS deployment and the same code path runs in
Lambda with real AWS services behind it.

## Secrets, not environment variables

The database credential is the highest-value secret this system holds: it is
write access to the agent's memory. `.env` is fine for a laptop and wrong for
anything with an IAM role available, because a file cannot be rotated, scoped or
audited.

`resolve_dsn` prefers **Secrets Manager**, falls back to the environment, and says
which it used. That ordering matters: a deployment that silently fell back to an
environment variable when the secret was unreachable would look healthy while
using a credential nobody can rotate.

## Logs an agent's operator can actually query

One JSON object per decision, on one line. CloudWatch Logs Insights can query
that directly; prose cannot be queried at all. Every record carries the run id,
the agent id, the attempt number, and — the field that matters most here — the
constraint verdict, so "how often did the guardrail refuse a write" is a query
rather than an investigation.

## Metrics that answer the operational question

Four counters, because these are the numbers you would put on a dashboard and
alert on:

    ConflictsObserved     contention, expected and normal
    DecisionsRevised      the protocol doing its job
    ConstraintRefusals    an agent proposing something its own policy forbids
    HardLimitViolations   should be zero; alert if it is not

`ConstraintRefusals` climbing is the interesting signal: it means agents are
consistently proposing writes that violate policy, which is a reasoning problem
the guardrail is currently absorbing. That is exactly the thing you want to know
before it becomes an incident.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass

NAMESPACE = "RaceLab"


def _tls(dsn: str) -> str:
    """Apply the same TLS normalization the rest of the library uses.

    Without this the gateway resolved a correct DSN and then failed the
    handshake, because `sslrootcert=system` has no trust store to point at on
    Windows. Secrets Manager returning a valid DSN is not the same as that DSN
    being connectable from wherever this happens to be running.
    """
    try:
        from ..db import normalize_tls
        return normalize_tls(dsn)
    except Exception as exc:  # noqa: BLE001
        # Loud. This was silent once and cost a deployment: `racelab.db` imports
        # python-dotenv at module scope, that was missing from the Lambda layer,
        # and the ImportError was swallowed here -- so the gateway ran with an
        # unmodified DSN and failed the TLS handshake looking for a root.crt
        # that does not exist in a Lambda sandbox. A fallback that hides why it
        # fell back is not a fallback, it is a trap.
        log_event("tls_normalization_unavailable", level="WARN",
                  error=f"{type(exc).__name__}: {exc}",
                  consequence="DSN used as-is; verify-full will need a CA bundle")
        return dsn


def _client(service: str):
    """A boto3 client, or None when boto3/credentials are unavailable."""
    try:
        import boto3
    except ImportError:
        return None
    try:
        return boto3.client(service, region_name=os.environ.get("AWS_REGION", "us-east-1"))
    except Exception:  # noqa: BLE001 - absence is a supported state
        return None


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedSecret:
    value: str
    source: str  # "secretsmanager" | "environment"

    @property
    def is_managed(self) -> bool:
        return self.source == "secretsmanager"


def resolve_dsn(
    secret_id: str | None = None,
    env_key: str = "RACELAB_CRDB_DSN",
    json_key: str = "dsn",
) -> ResolvedSecret:
    """Prefer Secrets Manager; fall back to the environment; report which.

    `secret_id` defaults to `RACELAB_DSN_SECRET_ID`, so a Lambda sets one
    environment variable naming the secret rather than carrying the credential.
    """
    secret_id = secret_id or os.environ.get("RACELAB_DSN_SECRET_ID")
    if secret_id:
        sm = _client("secretsmanager")
        if sm is not None:
            try:
                raw = sm.get_secret_value(SecretId=secret_id)["SecretString"]
                try:
                    parsed = json.loads(raw)
                    value = parsed.get(json_key) if isinstance(parsed, dict) else raw
                except json.JSONDecodeError:
                    value = raw
                if value:
                    return ResolvedSecret(value=_tls(value), source="secretsmanager")
            except Exception as exc:  # noqa: BLE001
                # Loud, not silent. A deployment that fell through to an env var
                # here would be running on an unrotatable credential while
                # looking fine.
                log_event("secret_resolution_failed", secret_id=secret_id,
                          error=f"{type(exc).__name__}: {exc}", level="ERROR")

    from_env = os.environ.get(env_key, "").strip()
    if not from_env:
        # Local development only. There is no .env in a Lambda package, so this
        # is a no-op there and the Secrets Manager path above is the real one.
        try:
            from dotenv import load_dotenv
            root = os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))))
            load_dotenv(os.path.join(root, ".env"))
            from_env = os.environ.get(env_key, "").strip()
        except ImportError:
            pass
    if not from_env:
        raise RuntimeError(
            f"no DSN available: Secrets Manager id {secret_id!r} did not resolve "
            f"and {env_key} is unset"
        )
    return ResolvedSecret(value=_tls(from_env), source="environment")


# --------------------------------------------------------------------------
# Structured logging
# --------------------------------------------------------------------------


def log_event(event: str, *, level: str = "INFO", **fields) -> None:
    """One JSON object per line, to stdout.

    stdout rather than the `logging` module on purpose: in Lambda stdout goes to
    CloudWatch Logs already, and locally it is just readable. No handler
    configuration to get wrong in either place.
    """
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": level,
        "event": event,
    }
    record.update({k: v for k, v in fields.items() if v is not None})
    sys.stdout.write(json.dumps(record, default=str) + "\n")
    sys.stdout.flush()


def log_decision(*, run_id: str, agent_id: str, attempt_no: int, observed,
                 inferred_constraint=None, action: str | None = None,
                 refused: str | None = None, retrieved_ids=None,
                 policy_version: int | None = None,
                 policy_status: str | None = None) -> None:
    """The record that makes an agent's decision auditable after the fact.

    `inferred_constraint` is the field worth having: without it you cannot tell
    a model that reasoned badly from a model that reasoned correctly over a stale
    document, and those need opposite fixes.

    `policy_version` and `policy_status` are what make that answerable *later*.
    A Logs Insights query can now separate the decisions taken under v2 from
    those taken under v3, and can count the requests refused because the policy
    could not be enforced at all -- which looks nothing like a limit breach and
    would otherwise be filed as one.
    """
    log_event(
        "agent_decision",
        run_id=run_id,
        agent_id=agent_id,
        attempt_no=attempt_no,
        observed_state=observed,
        inferred_constraint=inferred_constraint,
        policy_version=policy_version,
        policy_status=policy_status,
        action=action,
        constraint_refused=refused,
        retrieved_ids=list(retrieved_ids) if retrieved_ids else None,
        level="WARN" if refused else "INFO",
    )


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

_METRIC_NAMES = (
    "ConflictsObserved",
    "DecisionsRevised",
    "ConstraintRefusals",
    "HardLimitViolations",
)


def publish_metrics(counts: dict, *, arm: str | None = None,
                    namespace: str = NAMESPACE) -> bool:
    """Send the four operational counters to CloudWatch. False if unavailable.

    Never raises. Metrics failing must not fail the agent's actual work -- an
    observability outage is not a correctness outage, and conflating them turns
    a dashboard problem into a write outage.
    """
    data = []
    dims = [{"Name": "Arm", "Value": arm}] if arm else []
    for name in _METRIC_NAMES:
        if name in counts:
            data.append({
                "MetricName": name,
                "Value": float(counts[name]),
                "Unit": "Count",
                "Dimensions": dims,
            })
    if not data:
        return False

    cw = _client("cloudwatch")
    if cw is None:
        log_event("metrics_skipped", reason="no cloudwatch client", count=len(data))
        return False
    try:
        cw.put_metric_data(Namespace=namespace, MetricData=data)
        return True
    except Exception as exc:  # noqa: BLE001
        log_event("metrics_failed", error=f"{type(exc).__name__}: {exc}",
                  level="WARN")
        return False


def metrics_from_result(result, *, hard_limit_violated: bool = False) -> dict:
    """Map a `RunResult` onto the four counters."""
    return {
        "ConflictsObserved": getattr(result, "conflicts", 0),
        "DecisionsRevised": 1 if getattr(result, "revised", False) else 0,
        "ConstraintRefusals": getattr(result, "refusals", 0),
        "HardLimitViolations": 1 if hard_limit_violated else 0,
    }
