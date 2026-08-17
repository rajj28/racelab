"""The CockroachDB Cloud control plane, as something the agent itself consults.

## Why an agent needs the control plane, and not just SQL

This wrapper exists because of a failure we actually hit. The first full sweep
died mid-run with `connection timeout expired`, and we spent a while looking at
TLS and latency before working out the real cause: twenty agents each holding a
racing connection *and* a memory connection is forty concurrent connections, and
CockroachDB Cloud Basic declines that. The error did not say so
(`docs/FEEDBACK.md`, entry 6).

An agent swarm that can read the control plane does not have to guess. Before
launching N agents it can ask what plan the cluster is on and whether N is
sane for it; when connections start failing it can ask whether the cluster is
degraded rather than assuming its own networking is at fault. Those are different
incidents with different responses, and SQL cannot tell them apart -- a cluster
that is unreachable answers no queries about its own health.

So the control plane is consulted for exactly the questions SQL cannot answer:

    preflight   is the cluster there, healthy, and on a plan that supports the
                concurrency I am about to create?
    triage      my connections are failing -- is that the cluster or is it me?

## Safety

**Read-only by construction.** `_ALLOWED` is an allowlist of `ccloud` subcommands,
and `run()` refuses anything not on it. `ccloud` can create and delete clusters;
nothing here can reach those verbs even if a caller asks, because an agent that
can delete its own memory layer is a worse problem than any it was built to fix.

Every call uses `--output json`, which is the property that makes this CLI usable
by a program at all: consistent noun-verb commands and machine-readable output on
every command, rather than prose that has to be scraped.

## Authentication

`ccloud auth login` is a browser flow, so it is a human action performed once.
This module never handles credentials: it shells out to a CLI that has its own
session, and reports clearly when there is not one. That is deliberate -- an
agent should not be holding control-plane credentials it could be talked into
using.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
from dataclasses import dataclass, field

# Read-only verbs only. Anything that provisions, mutates or destroys is absent
# by design, not by convention.
_ALLOWED: dict[str, list[str]] = {
    "whoami": ["auth", "whoami"],
    "clusters": ["cluster", "list"],
    "cluster": ["cluster", "info"],
    "regions": ["cluster", "regions"],
}

# Connections a plan will comfortably serve. Basic's real ceiling is not
# published per-cluster (FEEDBACK entry 6), so this is our measured experience
# rather than a documented limit, and it says so where it is used.
_PLAN_CONNECTION_BUDGET = {
    "BASIC": 30,
    "STANDARD": 100,
    "ADVANCED": 100,
}


class CcloudUnavailable(RuntimeError):
    """ccloud is not installed, or there is no session."""


def binary() -> str | None:
    """Locate `ccloud`, including the Windows install path the docs recommend."""
    found = shutil.which("ccloud")
    if found:
        return found
    candidates = [
        pathlib.Path(os.environ.get("APPDATA", "")) / "ccloud" / "ccloud.exe",
        pathlib.Path.home() / ".local" / "bin" / "ccloud",
        pathlib.Path("/usr/local/bin/ccloud"),
    ]
    for c in candidates:
        if c and c.exists():
            return str(c)
    return None


def run(command: str, *args: str, timeout: int = 45) -> object:
    """Run one allowlisted read-only ccloud command and parse its JSON."""
    if command not in _ALLOWED:
        raise ValueError(
            f"{command!r} is not an allowlisted read-only command; "
            f"allowed: {sorted(_ALLOWED)}"
        )
    exe = binary()
    if exe is None:
        raise CcloudUnavailable(
            "ccloud is not installed. See "
            "https://www.cockroachlabs.com/docs/cockroachcloud/ccloud-get-started"
        )

    argv = [exe, *_ALLOWED[command], *args, "--output", "json", "--quiet"]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        if "not logged in" in err.lower():
            raise CcloudUnavailable(
                "no ccloud session. A human runs `ccloud auth login` once; this "
                "module deliberately does not handle control-plane credentials."
            )
        raise CcloudUnavailable(f"ccloud {command} failed: {err[:300]}")
    text = (proc.stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CcloudUnavailable(
            f"ccloud {command} returned output that is not JSON: {text[:200]}"
        ) from exc


@dataclass
class Preflight:
    """What the swarm learned before deciding whether to start."""

    ok: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cluster_name: str | None = None
    cluster_state: str | None = None
    plan: str | None = None
    regions: list[str] = field(default_factory=list)
    connection_budget: int | None = None

    def explain(self) -> str:
        head = "cluster ready" if self.ok else "NOT ready"
        bits = [f"{head}: {self.cluster_name or '(unknown)'} "
                f"state={self.cluster_state} plan={self.plan}"]
        for r in self.reasons:
            bits.append(f"  blocked: {r}")
        for w in self.warnings:
            bits.append(f"  warning: {w}")
        return "\n".join(bits)


def preflight(*, cluster_name: str | None = None, planned_connections: int = 0
              ) -> Preflight:
    """Ask the control plane whether it is sane to launch a swarm right now.

    Answers the question a `SELECT 1` cannot: not "can I reach the database"
    but "is this cluster in a state, and on a plan, that supports what I am
    about to do to it".
    """
    out = Preflight(ok=False)
    try:
        clusters = run("clusters")
    except CcloudUnavailable as exc:
        out.reasons.append(str(exc))
        return out

    rows = clusters if isinstance(clusters, list) else (clusters or {}).get("clusters", [])
    if not rows:
        out.reasons.append("the organization has no clusters visible to this session")
        return out

    chosen = None
    if cluster_name:
        chosen = next((c for c in rows if c.get("name") == cluster_name), None)
        if chosen is None:
            out.reasons.append(
                f"no cluster named {cluster_name!r}; visible: "
                f"{[c.get('name') for c in rows]}")
            return out
    else:
        chosen = rows[0]
        if len(rows) > 1:
            out.warnings.append(
                f"{len(rows)} clusters visible and none named; used "
                f"{chosen.get('name')!r}")

    out.cluster_name = chosen.get("name")
    state = (chosen.get("state") or chosen.get("status") or "").upper()
    out.cluster_state = state or None
    plan = (chosen.get("plan") or chosen.get("config", {}).get("plan") or "").upper()
    out.plan = plan or None

    regions = chosen.get("regions") or []
    out.regions = [r.get("name", str(r)) if isinstance(r, dict) else str(r)
                   for r in regions]

    if state and state not in {"CREATED", "ACTIVE", "READY"}:
        out.reasons.append(f"cluster state is {state}, not a running state")

    budget = _PLAN_CONNECTION_BUDGET.get(plan)
    out.connection_budget = budget
    if budget and planned_connections > budget:
        # The lesson from FEEDBACK entry 6, encoded so it cannot be relearned
        # the hard way: refuse rather than discover it mid-sweep.
        out.reasons.append(
            f"planned {planned_connections} concurrent connections on plan "
            f"{plan}, which we have measured to decline around {budget}. Pool "
            f"everything that is not the raced transaction, or raise the plan."
        )
    elif budget and planned_connections > budget * 0.6:
        out.warnings.append(
            f"planned {planned_connections} connections against a measured "
            f"budget near {budget} on {plan}; little headroom")

    out.ok = not out.reasons
    return out


def triage_connection_failure() -> str:
    """After repeated connect failures, say whether the cluster is the suspect.

    The distinction matters because the responses differ: a degraded cluster is
    something to wait out or escalate, and a local networking fault is something
    to fix. Guessing wrong wastes the outage.
    """
    try:
        clusters = run("clusters")
    except CcloudUnavailable as exc:
        return (
            "control plane unreachable or unauthenticated, so the cluster cannot "
            f"be cleared or blamed: {exc}"
        )
    rows = clusters if isinstance(clusters, list) else (clusters or {}).get("clusters", [])
    bad = [c for c in rows
           if (c.get("state") or c.get("status") or "").upper()
           not in {"CREATED", "ACTIVE", "READY"}]
    if bad:
        names = ", ".join(f"{c.get('name')}={c.get('state') or c.get('status')}"
                          for c in bad)
        return f"cluster-side: {names}. Do not retry harder; escalate or wait."
    return (
        "control plane reports every cluster running, so connection failures are "
        "most likely local: connection count, IP allowlist, DNS or TLS trust. "
        "Check the concurrency budget before adding retries."
    )
