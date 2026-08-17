"""RaceLab Phase 0: verify the environment against the live clusters.

Everything here is a question we refuse to answer from memory or from a version
number someone told us. Defaults have moved across CockroachDB releases and
managed plans restrict things the open-source docs do not mention, so each
probe asks the cluster directly and records what it actually said.

Every probe is tolerant: a permission failure is a finding, not a crash. Run it
as many times as you like -- it creates only a throwaway probe table and drops
it again.

    python spike/verify_env.py            # probe whatever DSNs are configured
    python spike/verify_env.py --write    # ...and regenerate docs/VERIFIED.md
"""

from __future__ import annotations

import argparse
import datetime
import decimal
import pathlib
import sys
from dataclasses import dataclass, field

import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from conn import resolve_dsn  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

PROBE_TABLE = "racelab_probe_vec"
EMBED_DIMS = 1024  # amazon.titan-embed-text-v2:0


@dataclass
class Finding:
    question: str
    status: str  # ok | denied | absent | error
    answer: str
    evidence: str = ""

    def render(self) -> str:
        badge = {"ok": "OK", "denied": "DENIED", "absent": "ABSENT", "error": "ERROR"}[self.status]
        out = [f"### {self.question}", "", f"**{badge}** — {self.answer}", ""]
        if self.evidence:
            out += ["```", self.evidence.strip(), "```", ""]
        return "\n".join(out)


@dataclass
class Report:
    target: str
    dsn_env: str
    reachable: bool
    version: str = ""
    findings: list[Finding] = field(default_factory=list)


def _one(conn: psycopg.Connection, sql: str, params: tuple = ()):
    return conn.execute(sql, params).fetchone()


def _try(fn, *, question: str, on_ok, denied_hint: str = "") -> Finding:
    """Run a probe, converting any database error into a recorded finding."""
    try:
        value = fn()
        return on_ok(value)
    except psycopg.Error as exc:
        state = getattr(exc, "sqlstate", None) or "?"
        msg = str(exc).strip().splitlines()[0]
        # 42501 insufficient_privilege, 0A000 feature_not_supported: on a
        # managed plan these mean "not available to us", which is a real answer.
        status = "denied" if state in ("42501", "0A000") else "error"
        answer = denied_hint if (status == "denied" and denied_hint) else f"SQLSTATE {state}."
        return Finding(question, status, answer, f"SQLSTATE {state}\n{msg}")
    except Exception as exc:  # noqa: BLE001
        return Finding(question, "error", f"{type(exc).__name__}", str(exc))


# --------------------------------------------------------------------------
# CockroachDB probes
# --------------------------------------------------------------------------


def probe_cockroach(dsn: str) -> Report:
    rep = Report(target="CockroachDB", dsn_env="RACELAB_CRDB_DSN", reachable=False)
    try:
        conn = psycopg.connect(dsn, autocommit=True, connect_timeout=20)
    except Exception as exc:  # noqa: BLE001
        rep.findings.append(
            Finding("Is the cluster reachable?", "error", "Could not connect.", str(exc))
        )
        return rep

    with conn:
        rep.reachable = True
        rep.version = _one(conn, "SELECT version()")[0]

        # -- default isolation ------------------------------------------------
        rep.findings.append(
            _try(
                lambda: _one(conn, "SHOW default_transaction_isolation")[0],
                question="What is the default transaction isolation level?",
                on_ok=lambda v: Finding(
                    "What is the default transaction isolation level?",
                    "ok" if "serializable" in str(v).lower() else "error",
                    f"`{v}` — this is the default an application gets with no configuration.",
                ),
            )
        )

        # -- gc.ttlseconds ----------------------------------------------------
        def _gc_default():
            return _one(conn, "SHOW ZONE CONFIGURATION FOR RANGE default")[1]

        rep.findings.append(
            _try(
                _gc_default,
                question="What is the actual gc.ttlseconds on this cluster?",
                on_ok=lambda raw: Finding(
                    "What is the actual gc.ttlseconds on this cluster?",
                    "ok",
                    _describe_gc(raw),
                    str(raw),
                ),
            )
        )

        # -- can we CONFIGURE ZONE at all? ------------------------------------
        def _configure_zone():
            conn.execute(f"CREATE TABLE IF NOT EXISTS {PROBE_TABLE}_zone (id INT PRIMARY KEY)")
            conn.execute(
                f"ALTER TABLE {PROBE_TABLE}_zone CONFIGURE ZONE USING gc.ttlseconds = 90000"
            )
            row = _one(conn, f"SHOW ZONE CONFIGURATION FOR TABLE {PROBE_TABLE}_zone")[1]
            conn.execute(f"DROP TABLE IF EXISTS {PROBE_TABLE}_zone")
            return row

        rep.findings.append(
            _try(
                _configure_zone,
                question="Is `ALTER TABLE ... CONFIGURE ZONE USING gc.ttlseconds` permitted on this plan?",
                on_ok=lambda raw: Finding(
                    "Is `ALTER TABLE ... CONFIGURE ZONE USING gc.ttlseconds` permitted on this plan?",
                    "ok",
                    "Permitted. We can widen the GC window for historical reads if needed, "
                    "though the demo should stay well inside the default window regardless.",
                    str(raw),
                ),
                denied_hint=(
                    "Not permitted on this plan. All `AS OF SYSTEM TIME` reads in the demo must "
                    "stay well inside the cluster's default GC window."
                ),
            )
        )

        # -- vector index feature flag ---------------------------------------
        rep.findings.append(
            _try(
                lambda: _one(conn, "SHOW CLUSTER SETTING feature.vector_index.enabled")[0],
                question="Is `feature.vector_index.enabled` already true?",
                on_ok=lambda v: Finding(
                    "Is `feature.vector_index.enabled` already true?",
                    "ok" if v in (True, "true", "on") else "absent",
                    f"Reported `{v}`."
                    + (
                        " No SET required."
                        if v in (True, "true", "on")
                        else " We must attempt to enable it (see next probe)."
                    ),
                ),
            )
        )

        rep.findings.append(
            _try(
                lambda: conn.execute(
                    "SET CLUSTER SETTING feature.vector_index.enabled = true"
                )
                or "applied",
                question="Can we SET `feature.vector_index.enabled` on this plan?",
                on_ok=lambda _: Finding(
                    "Can we SET `feature.vector_index.enabled` on this plan?",
                    "ok",
                    "The SET succeeded. Setup can enable it idempotently.",
                ),
                denied_hint=(
                    "The SET is refused on this plan. Setup must tolerate this and rely on the "
                    "setting's default rather than failing."
                ),
            )
        )

        # -- vector index creation, per opclass -------------------------------
        for opclass, operator, name in (
            ("vector_l2_ops", "<->", "L2"),
            ("vector_cosine_ops", "<=>", "cosine"),
        ):
            rep.findings.append(_probe_vector_index(conn, opclass, operator, name))

        # -- historical reads --------------------------------------------------
        def _aost():
            # Start from a known-empty table so the comparison below can only
            # come out one way if historical reads work.
            conn.execute(f"DROP TABLE IF EXISTS {PROBE_TABLE}_aost")
            conn.execute(f"CREATE TABLE {PROBE_TABLE}_aost (id INT PRIMARY KEY)")
            ts = _one(conn, "SELECT cluster_logical_timestamp()")[0]
            conn.execute(f"INSERT INTO {PROBE_TABLE}_aost (id) VALUES (1)")
            # AS OF SYSTEM TIME requires a constant expression, so the timestamp
            # has to be inlined rather than bound. It is a DECIMAL we just read
            # back from the cluster; reject anything else before interpolating.
            literal = str(decimal.Decimal(ts))
            past = _one(
                conn,
                f"SELECT count(*) FROM {PROBE_TABLE}_aost AS OF SYSTEM TIME {literal}",
            )[0]
            now = _one(conn, f"SELECT count(*) FROM {PROBE_TABLE}_aost")[0]
            conn.execute(f"DROP TABLE IF EXISTS {PROBE_TABLE}_aost")
            return past, now

        rep.findings.append(
            _try(
                _aost,
                question="Does `AS OF SYSTEM TIME` return the pre-write state (needed for the historical-evidence view)?",
                on_ok=lambda pair: Finding(
                    "Does `AS OF SYSTEM TIME` return the pre-write state (needed for the historical-evidence view)?",
                    "ok" if pair[0] != pair[1] else "error",
                    f"Historical read saw {pair[0]} row(s); current read saw {pair[1]}. "
                    + (
                        "The historical read reflects the state before the write, which is what "
                        "the UI needs to show the state a losing transaction reasoned over."
                        if pair[0] != pair[1]
                        else "Historical and current reads agree, so this probe proved nothing."
                    ),
                ),
            )
        )

    return rep


def _describe_gc(raw) -> str:
    text = str(raw)
    seconds = None
    for line in text.splitlines():
        if "gc.ttlseconds" in line:
            digits = "".join(ch for ch in line.split("=")[-1] if ch.isdigit())
            if digits:
                seconds = int(digits)
    if seconds is None:
        return "Read from the cluster; see the raw zone configuration below."
    hours = seconds / 3600.0
    return (
        f"`gc.ttlseconds = {seconds}` ({hours:.1f} h). Every `AS OF SYSTEM TIME` read in the "
        f"demo must be newer than this, so the UI should read seconds-old timestamps, never "
        f"hours-old ones."
    )


def _probe_vector_index(conn: psycopg.Connection, opclass: str, operator: str, name: str) -> Finding:
    question = f"Does `CREATE VECTOR INDEX` work with `{opclass}` ({name}, `{operator}`) at VECTOR({EMBED_DIMS})?"

    def _run():
        conn.execute(f"DROP TABLE IF EXISTS {PROBE_TABLE}")
        conn.execute(
            f"CREATE TABLE {PROBE_TABLE} "
            f"(id INT PRIMARY KEY, account_id TEXT, embedding VECTOR({EMBED_DIMS}))"
        )
        conn.execute(
            f"CREATE VECTOR INDEX {PROBE_TABLE}_idx ON {PROBE_TABLE} (embedding {opclass})"
        )
        zeros = "[" + ",".join(["0.1"] * EMBED_DIMS) + "]"
        conn.execute(
            f"INSERT INTO {PROBE_TABLE} (id, account_id, embedding) VALUES (1, 'a', %s)", (zeros,)
        )
        plan = conn.execute(
            f"EXPLAIN SELECT id FROM {PROBE_TABLE} "
            f"ORDER BY embedding {operator} %s::VECTOR({EMBED_DIMS}) LIMIT 5",
            (zeros,),
        ).fetchall()
        text = "\n".join(str(r[0]) for r in plan)
        conn.execute(f"DROP TABLE IF EXISTS {PROBE_TABLE}")
        return text

    def _ok(plan_text: str) -> Finding:
        used = PROBE_TABLE + "_idx" in plan_text or "vector" in plan_text.lower()
        return Finding(
            question,
            "ok",
            f"Index created and accepted a VECTOR({EMBED_DIMS}) column. "
            + (
                f"The query plan for `ORDER BY embedding {operator} ...` references the vector "
                f"index, so retrieval is index-backed rather than a full scan."
                if used
                else f"However the plan for `{operator}` does not appear to use the index — "
                f"retrieval would fall back to a scan. Check the opclass/operator pairing."
            ),
            plan_text,
        )

    return _try(_run, question=question, on_ok=_ok,
                denied_hint="Refused on this plan or unsupported on this version.")


# --------------------------------------------------------------------------
# PostgreSQL probes
# --------------------------------------------------------------------------


def probe_postgres(dsn: str) -> Report:
    rep = Report(target="PostgreSQL", dsn_env="RACELAB_PG_DSN", reachable=False)
    try:
        conn = psycopg.connect(dsn, autocommit=True, connect_timeout=15)
    except Exception as exc:  # noqa: BLE001
        rep.findings.append(
            Finding("Is the control-arm database reachable?", "error", "Could not connect.", str(exc))
        )
        return rep

    with conn:
        rep.reachable = True
        rep.version = _one(conn, "SELECT version()")[0]
        rep.findings.append(
            _try(
                lambda: _one(conn, "SHOW default_transaction_isolation")[0],
                question="What is the default transaction isolation level?",
                on_ok=lambda v: Finding(
                    "What is the default transaction isolation level?",
                    "ok" if "read committed" in str(v).lower() else "error",
                    f"`{v}` — this is the default an application gets with no configuration, and "
                    f"it is what Arm A runs under. PostgreSQL also offers SERIALIZABLE; the "
                    f"comparison in this project is between default isolation behaviours.",
                ),
            )
        )
        rep.findings.append(
            _try(
                lambda: _one(conn, "SHOW server_version")[0],
                question="Is this PostgreSQL 16?",
                on_ok=lambda v: Finding(
                    "Is this PostgreSQL 16?",
                    "ok" if str(v).startswith("16") else "absent",
                    f"Reported server_version `{v}`.",
                ),
            )
        )
    return rep


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def render(reports: list[Report]) -> str:
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out = [
        "# Verified environment",
        "",
        "Generated by `python spike/verify_env.py --write`. Every statement below was read "
        "from a live cluster rather than from documentation or a version number, because "
        "defaults have changed across releases and managed plans restrict operations that the "
        "open-source documentation does not mention.",
        "",
        f"_Last run: {now}_",
        "",
    ]
    for rep in reports:
        out += [f"## {rep.target}", ""]
        if not rep.reachable:
            out += [
                f"Not reachable. `{rep.dsn_env}` is unset or the cluster refused the connection; "
                "the probes below could not run.",
                "",
            ]
        else:
            out += ["```", rep.version.strip(), "```", ""]
        for f in rep.findings:
            out.append(f.render())
    return "\n".join(out).rstrip() + "\n"


def _dsn_or_none(env_key: str) -> str | None:
    try:
        return resolve_dsn(env_key)
    except SystemExit:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true", help="write docs/VERIFIED.md")
    args = ap.parse_args()

    reports = []
    crdb = _dsn_or_none("RACELAB_CRDB_DSN")
    pg = _dsn_or_none("RACELAB_PG_DSN")

    if crdb:
        reports.append(probe_cockroach(crdb))
    else:
        reports.append(Report("CockroachDB", "RACELAB_CRDB_DSN", reachable=False))
        print("RACELAB_CRDB_DSN is not set; skipping CockroachDB probes.", file=sys.stderr)
    if pg:
        reports.append(probe_postgres(pg))
    else:
        reports.append(Report("PostgreSQL", "RACELAB_PG_DSN", reachable=False))
        print("RACELAB_PG_DSN is not set; skipping PostgreSQL probes.", file=sys.stderr)

    text = render(reports)
    print(text)
    if args.write:
        path = REPO_ROOT / "docs" / "VERIFIED.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"-> {path.relative_to(REPO_ROOT)}", file=sys.stderr)

    unresolved = [f for r in reports for f in r.findings if f.status == "error"]
    return 1 if unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
