"""Connections and dialect handling.

Note on the duplication with `spike/conn.py`: the Phase 1 gate deliberately
does not import this package. The gate is the artifact that establishes the
project's central empirical claim, and it is worth more if it can be read and
re-run as a self-contained script with no framework behind it. Thirty lines of
overlap is a fair price for that.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import queue
import time
from enum import Enum

import psycopg
from dotenv import load_dotenv

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")


class Dialect(str, Enum):
    COCKROACH = "cockroach"
    POSTGRES = "postgres"


BACKENDS = {
    "crdb": {"env": "RACELAB_CRDB_DSN", "label": "CockroachDB", "isolation": "SERIALIZABLE"},
    "pg": {"env": "RACELAB_PG_DSN", "label": "PostgreSQL", "isolation": "READ COMMITTED"},
}


def resolve_dsn(env_key: str) -> str:
    """Read a DSN from the environment and make its TLS settings usable.

    CockroachDB Cloud presents a certificate signed by a public CA, so
    `verify-full` is correct and we keep it. But `sslrootcert=system` asks libpq
    for the OS trust store, and the OpenSSL bundled into the psycopg binary
    wheel on Windows has none to point at. Handing it the `certifi` bundle keeps
    the connection fully verified without pinning a machine-specific path into
    `.env`.
    """
    dsn = os.environ.get(env_key, "").strip()
    if not dsn:
        raise SystemExit(f"{env_key} is not set. Copy .env.example to .env and fill it in.")

    if "sslmode=verify-full" not in dsn:
        return dsn
    if "sslrootcert=" in dsn and "sslrootcert=system" not in dsn:
        return dsn

    try:
        import certifi
    except ImportError:
        return dsn

    bundle = certifi.where()
    if "sslrootcert=system" in dsn:
        return dsn.replace("sslrootcert=system", f"sslrootcert={bundle}")
    sep = "&" if "?" in dsn else "?"
    return f"{dsn}{sep}sslrootcert={bundle}"


def dsn_for(backend: str) -> str:
    if backend not in BACKENDS:
        raise SystemExit(f"unknown backend {backend!r}; expected one of {sorted(BACKENDS)}")
    return resolve_dsn(BACKENDS[backend]["env"])


def connect(
    backend: str,
    autocommit: bool = True,
    attempts: int = 3,
    connect_timeout: int = 10,
) -> psycopg.Connection:
    """Open a connection, retrying transient connect failures.

    A retry here is **not** the retry this project is about. Failing to
    *establish* a connection is unrelated to serialization, carries no decision,
    and says nothing about the hypothesis -- it is a cluster, quota or network
    condition. It is retried because the alternative is losing a swept
    experiment to one dropped handshake, which is what happened on the first
    full run.

    The timeout is deliberately short. An earlier version used four attempts at
    thirty seconds each, so a backend that was simply *not running* took two
    minutes to say so -- turning a one-line diagnosis into a stall. A backend
    that is up answers in milliseconds; one that is down should say so quickly.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return psycopg.connect(
                dsn_for(backend), autocommit=autocommit, connect_timeout=connect_timeout
            )
        except psycopg.OperationalError as exc:
            last = exc
            if attempt < attempts - 1:
                time.sleep(0.5 * (2 ** attempt))
    raise last  # type: ignore[misc]


class ConnectionPool:
    """A small fixed pool, because a cluster will refuse unbounded connections.

    Twenty agents each holding a racing connection *and* a memory connection is
    forty concurrent connections per run, which CockroachDB Cloud Basic will
    decline. Racing connections have to be per-agent -- that is what makes them
    race -- so the memory connections are pooled instead.

    Pooling those is safe precisely because retrieval never happens inside the
    raced transaction: memory is read before `BEGIN` and after `ROLLBACK`, never
    between them, so a pooled connection cannot join a transaction's refresh
    span. It is also why the pool must never be used for the racing connection.
    """

    def __init__(self, backend: str, size: int = 6):
        self.backend = backend
        self._free: queue.LifoQueue = queue.LifoQueue()
        self._all: list[psycopg.Connection] = []
        for _ in range(size):
            conn = connect(backend)
            self._all.append(conn)
            self._free.put(conn)

    @contextlib.contextmanager
    def lease(self, timeout: float = 60.0):
        conn = self._free.get(timeout=timeout)
        try:
            yield conn
        except psycopg.Error:
            # A connection that raised may be in an unusable state. Replace it
            # rather than handing the fault to the next caller.
            try:
                conn.close()
            except psycopg.Error:
                pass
            conn = connect(self.backend)
            raise
        finally:
            self._free.put(conn)

    def close(self) -> None:
        for conn in self._all:
            try:
                conn.close()
            except psycopg.Error:
                pass


def dialect_of(conn: psycopg.Connection) -> Dialect:
    version = conn.execute("SELECT version()").fetchone()[0]
    return Dialect.COCKROACH if "CockroachDB" in version else Dialect.POSTGRES
