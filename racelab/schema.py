"""Schema for the allocation scenario and its telemetry.

Two groups of tables live here and they answer different questions.

The **operational** tables (`accounts`, `allocations`) are the state the
invariant is about: `SUM(allocations.amount) <= accounts.hard_limit`. These are
mirrored on both backends, because they are what the arms actually race on.

The **telemetry** tables (`decisions`, `agent_attempts`, `race_runs`,
`conflict_edges`) are the experiment's dataset. They are written by the library
wrapper as a normal feature, not by a separate benchmark harness. `decisions` in
particular carries `decision_before` / `decision_after` / `revised`, which is
the only place the hypothesis can actually be tested: without it we would be
measuring database retries rather than whether an agent changed its mind.

`memories` is CockroachDB-only, and that is a deliberate design decision rather
than a limitation we settled for. See MEMORY_BACKEND_NOTE below.
"""

from __future__ import annotations

import argparse
import sys

import psycopg

from .db import BACKENDS, Dialect, connect, dialect_of

EMBED_DIMS = 1024  # amazon.titan-embed-text-v2:0

MEMORY_BACKEND_NOTE = """\
The `memories` table lives only on CockroachDB, and every arm retrieves from it.

Two reasons, and the second is the important one:

1. Stock PostgreSQL 16 has no VECTOR type. pgvector is an extension, and the
   portable EDB distribution used for the control arm does not ship it.

2. More importantly, retrieval must be IDENTICAL across arms. If arm A ran
   brute-force cosine over an array column while arms B and C ran approximate
   nearest-neighbour search over a C-SPANN index, the arms would differ in what
   context their agents retrieved -- and any difference in outcome could be
   attributed to retrieval quality rather than to isolation and policy. Sharing
   one memory store removes that confound entirely.

This does not weaken the comparison. What the arms race on is the allocation
transaction, and that is mirrored faithfully on both backends. Semantic memory
retrieval happens before the transaction opens and is not part of what is
being tested.
"""


# --------------------------------------------------------------------------
# DDL
# --------------------------------------------------------------------------

# Shared by both backends. The invariant these support spans rows, which is
# exactly why it cannot be a CHECK constraint -- see the README.
OPERATIONAL = [
    """
    CREATE TABLE IF NOT EXISTS accounts (
        account_id  TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        hard_limit  INT  NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS allocations (
        allocation_id UUID PRIMARY KEY,
        account_id    TEXT NOT NULL,
        agent_id      TEXT NOT NULL,
        amount        INT  NOT NULL,
        run_id        TEXT NOT NULL,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS allocations_account_idx ON allocations (account_id)",
    "CREATE INDEX IF NOT EXISTS allocations_run_idx ON allocations (run_id)",
]

TELEMETRY = [
    """
    CREATE TABLE IF NOT EXISTS race_runs (
        run_id             TEXT PRIMARY KEY,
        seed               INT  NOT NULL,
        arm                TEXT NOT NULL,
        scenario           TEXT NOT NULL,
        agent_count        INT  NOT NULL,
        final_sum          INT,
        hard_limit         INT  NOT NULL,
        invariant_violated BOOL,
        started_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
        ended_at           TIMESTAMPTZ
    )
    """,
    # One row per reasoning step. An agent that conflicts and re-reasons writes
    # two rows: attempt_no 0 and attempt_no 1, and the second carries
    # revised = true when the action actually changed.
    """
    CREATE TABLE IF NOT EXISTS decisions (
        decision_id          UUID PRIMARY KEY,
        run_id               TEXT NOT NULL,
        agent_id             TEXT NOT NULL,
        attempt_no           INT  NOT NULL,
        policy               TEXT NOT NULL,
        retrieved_memory_ids TEXT[],
        inferred_ceiling     INT,
        observed_sum         INT,
        proposed_amount      INT,
        decision_before      TEXT,
        decision_after       TEXT,
        revised              BOOL NOT NULL DEFAULT false,
        created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS decisions_run_idx ON decisions (run_id)",
    # One row per transaction attempt, whatever its outcome.
    """
    CREATE TABLE IF NOT EXISTS agent_attempts (
        attempt_id     UUID PRIMARY KEY,
        run_id         TEXT NOT NULL,
        agent_id       TEXT NOT NULL,
        policy         TEXT NOT NULL,
        outcome        TEXT NOT NULL,
        error_code     TEXT,
        retry_count    INT NOT NULL DEFAULT 0,
        txn_started_at TIMESTAMPTZ,
        txn_ended_at   TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS agent_attempts_run_idx ON agent_attempts (run_id)",
    # A transaction conflict graph. These edges are database dependency edges --
    # transaction A could not serialize against transaction B's committed write.
    # They are not claims that two agents semantically disagreed.
    """
    CREATE TABLE IF NOT EXISTS conflict_edges (
        run_id        TEXT NOT NULL,
        agent_a       TEXT NOT NULL,
        agent_b       TEXT NOT NULL,
        conflict_type TEXT NOT NULL,
        PRIMARY KEY (run_id, agent_a, agent_b)
    )
    """,
]

# CockroachDB only.
MEMORIES = [
    f"""
    CREATE TABLE IF NOT EXISTS memories (
        memory_id  TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        text       TEXT NOT NULL,
        embedding  VECTOR({EMBED_DIMS}),
        kind       TEXT NOT NULL,
        -- Supersession is explicit rather than inferred from created_at alone,
        -- so the UI can draw the temporal chain and retrieval can reason about
        -- it directly. NULL means "not superseded by anything known".
        supersedes TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # There is deliberately NO secondary index on (account_id) here, and adding
    # one back would silently disable ANN search. See NO_ACCOUNT_INDEX_NOTE.
]

NO_ACCOUNT_INDEX_NOTE = """\
Why `memories` has no secondary index on (account_id).

The obvious index to add is `CREATE INDEX ON memories (account_id)` -- every
retrieval filters by account, so it looks like exactly the right index. It is
also the index CockroachDB's own optimizer recommends for this query.

Adding it stops the vector index from being used.

With both indexes present, the optimizer costs the filtered ANN query two ways:
seek the account index and scan the matching rows, or search the vector index.
It chose the scan every time -- on a 5-row account and on a 1200-row account
alike -- because its cardinality estimate for the account span was wildly low
(it estimated 1 row for a span holding 1200). The plan was:

    scan  table: memories@memories_account_idx

Correct results, no error, no warning, and no approximate nearest-neighbour
search happening anywhere. Forcing the index with `memories@memories_embedding_idx`
produced `• vector search` with `prefix spans`, proving the index could serve
the query perfectly well and was simply losing on cost.

Dropping the redundant index makes the optimizer choose `• vector search` at
both scales, with no hint.

Two lessons, and the second is the one that generalises:

1. A prefix column is NECESSARY for a filtered ANN query -- without
   `account_id` as a prefix, the vector index cannot serve this query at all.
2. It is NOT SUFFICIENT. A vector index competes on cost with every other index
   that could satisfy the filter, and a conventional secondary index on the
   filter column will often win, especially where statistics are off. The
   vector index has to be the only reasonable way to answer the query.

The cost of not having it: `clear_account` and `count` scan the table instead
of seeking. On a corpus this size that is irrelevant, and it is the right trade
-- those two run during seeding, while the ANN query runs on every agent's
every retrieval.
"""

# Built separately: a vector index must be created while the table is empty to
# avoid blocking writes, and it needs the feature flag on.
#
# `account_id` is a PREFIX COLUMN, and it has to be. Every retrieval in this
# system is scoped to one account, and an index on `(embedding)` alone cannot
# serve `WHERE account_id = $1 ORDER BY embedding <=> $2` -- the optimizer will
# satisfy the filter from the account index and then scan, which was exactly
# what it did on the first version of this schema. The vector index was present,
# and unused. A prefix column makes the index answer the query that is actually
# asked, which is the difference between a load-bearing index and a decorative
# one.
MEMORIES_VECTOR_INDEX = (
    "CREATE VECTOR INDEX IF NOT EXISTS memories_embedding_idx "
    "ON memories (account_id, embedding vector_cosine_ops)"
)

DROP_ORDER = [
    "conflict_edges",
    "agent_attempts",
    "decisions",
    "race_runs",
    "allocations",
    "accounts",
    "memories",
]


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------


def create_all(conn: psycopg.Connection, *, verbose: bool = True) -> None:
    dialect = dialect_of(conn)
    steps = list(OPERATIONAL) + list(TELEMETRY)
    if dialect is Dialect.COCKROACH:
        steps += list(MEMORIES)

    for stmt in steps:
        conn.execute(stmt)
        if verbose:
            print(f"  ok  {_first_line(stmt)}")

    if dialect is Dialect.COCKROACH:
        _create_vector_index(conn, verbose=verbose)


def _create_vector_index(conn: psycopg.Connection, *, verbose: bool) -> None:
    """Create the C-SPANN index, tolerating a cluster that will not enable it.

    Phase 0 found `feature.vector_index.enabled` already true on our cluster and
    the SET permitted, but a different plan may refuse the SET -- so the attempt
    is best-effort and a failure to enable is not fatal here. A failure to
    create the index IS fatal, because retrieval would silently degrade to a
    full scan and the vector index would stop being load-bearing.
    """
    try:
        conn.execute("SET CLUSTER SETTING feature.vector_index.enabled = true")
        if verbose:
            print("  ok  feature.vector_index.enabled = true")
    except psycopg.Error as exc:
        if verbose:
            print(f"  --  could not SET feature.vector_index.enabled ({exc.sqlstate}); "
                  f"relying on the cluster default")

    conn.execute(MEMORIES_VECTOR_INDEX)
    if verbose:
        print(f"  ok  {_first_line(MEMORIES_VECTOR_INDEX)}")

    # Older versions of this schema created a secondary index on (account_id).
    # It out-competes the vector index on cost and silently disables ANN search,
    # so drop it on any database that still carries one. See
    # NO_ACCOUNT_INDEX_NOTE.
    conn.execute("DROP INDEX IF EXISTS memories_account_idx")
    if verbose:
        print("  ok  dropped memories_account_idx if present (see NO_ACCOUNT_INDEX_NOTE)")


def drop_all(conn: psycopg.Connection, *, verbose: bool = True) -> None:
    for table in DROP_ORDER:
        conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        if verbose:
            print(f"  dropped {table}")


def _first_line(stmt: str) -> str:
    for line in stmt.strip().splitlines():
        line = line.strip()
        if line and not line.startswith("--"):
            return line.rstrip("(").strip()
    return stmt[:60]


def main() -> int:
    ap = argparse.ArgumentParser(description="create or drop the RaceLab schema")
    ap.add_argument("--backend", choices=sorted(BACKENDS), required=True)
    ap.add_argument("--drop", action="store_true", help="drop before creating")
    ap.add_argument("--drop-only", action="store_true")
    args = ap.parse_args()

    with connect(args.backend) as conn:
        label = BACKENDS[args.backend]["label"]
        print(f"{label}:")
        if args.drop or args.drop_only:
            drop_all(conn)
        if not args.drop_only:
            create_all(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
