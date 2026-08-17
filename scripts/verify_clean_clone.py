"""Verify the schema a judge actually gets on a first run against an empty database.

This exists because of a specific way the project has already been wrong twice.
`racelab/schema.py` is the source of truth for the schema, but what matters is
the schema that ends up *applied*, and the two came apart both times:

  1. A vector index was created correctly and could not serve the filtered
     query, because `account_id` was not a prefix column.
  2. With the prefix column added, the vector index was still not used, because
     an ordinary secondary index on `(account_id)` beat it on cost -- the index
     CockroachDB's own optimizer recommends for this query.

Neither failure produced an error, a warning, or a wrong answer. Both were
invisible in the DDL and visible only in `EXPLAIN`. So "the migration drops the
index" is not the claim worth making. The claim worth making is: **on a fresh
database, after running exactly what the README tells you to run, the optimizer
chooses approximate nearest-neighbour search without being asked to.**

That is what this asserts, on a throwaway database created and dropped here so
it cannot be contaminated by any earlier state.

    python scripts/verify_clean_clone.py

Needs no AWS credentials: the vectors are random rather than embedded, because
what is under test is the query plan, not retrieval quality.
"""

from __future__ import annotations

import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import psycopg

from racelab.db import dsn_for
from racelab.schema import EMBED_DIMS, create_all

CLEANROOM_DB = "racelab_cleanroom"
PROBE_ACCOUNT = "cleanroom-probe"
PROBE_ROWS = 400

failures: list[str] = []


def check(ok: bool, description: str, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {description}")
    if not ok:
        if detail:
            print(f"        {detail}")
        failures.append(description)
    return ok


def random_vector(rng: random.Random) -> str:
    vec = [rng.gauss(0, 1) for _ in range(EMBED_DIMS)]
    norm = sum(v * v for v in vec) ** 0.5
    return "[" + ",".join(f"{v / norm:.5f}" for v in vec) + "]"


def cleanroom_dsn(base: str) -> str:
    """Point the base DSN at the throwaway database, preserving TLS settings."""
    head, sep, tail = base.partition("?")
    root, _, _ = head.rpartition("/")
    return f"{root}/{CLEANROOM_DB}{sep}{tail}"


def main() -> int:
    base = dsn_for("crdb")
    print("Clean-clone schema verification")
    print("=" * 66)
    print(f"\nCreating a throwaway database: {CLEANROOM_DB}")

    with psycopg.connect(base, autocommit=True, connect_timeout=30) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {CLEANROOM_DB} CASCADE")
        admin.execute(f"CREATE DATABASE {CLEANROOM_DB}")

    try:
        with psycopg.connect(cleanroom_dsn(base), autocommit=True, connect_timeout=30) as conn:
            print("\n1. Applying the schema exactly as `make schema` does")
            create_all(conn, verbose=False)
            print("  applied")

            # -- what is actually in the database, not what the DDL says -----
            print("\n2. The applied schema, read back from the cluster")
            indexes = conn.execute(
                "SELECT index_name, column_name, seq_in_index "
                "FROM [SHOW INDEXES FROM memories] ORDER BY index_name, seq_in_index"
            ).fetchall()
            names = {row[0] for row in indexes}

            check(
                "memories_embedding_idx" in names,
                "the vector index exists",
                f"indexes present: {sorted(names)}",
            )
            check(
                "memories_account_idx" not in names,
                "no ordinary secondary index on (account_id)",
                "memories_account_idx is present and will cost-beat the vector "
                "index, silently disabling ANN search",
            )

            prefix = [
                row[1] for row in indexes
                if row[0] == "memories_embedding_idx" and row[2] == 1
            ]
            check(
                prefix == ["account_id"],
                "account_id is the vector index prefix column",
                f"first indexed column is {prefix}; without account_id as a "
                f"prefix the index cannot serve a filtered ANN query at all",
            )

            # -- and the part that only EXPLAIN can answer -------------------
            print(f"\n3. Loading {PROBE_ROWS} rows and asking the optimizer, unaided")
            rng = random.Random(20260816)
            rows = [
                (f"probe-{i}", PROBE_ACCOUNT, f"probe memory {i}",
                 random_vector(rng), "note", None)
                for i in range(PROBE_ROWS)
            ]
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO memories (memory_id, account_id, text, embedding, "
                    "kind, supersedes) VALUES (%s, %s, %s, %s, %s, %s)",
                    rows,
                )
            # Statistics are the reason this failed the second time: with a bad
            # cardinality estimate a scan looks free. Collect them, so the
            # optimizer is making a fully informed choice rather than an
            # uninformed one that happens to go our way.
            conn.execute("ANALYZE memories")

            query_vec = random_vector(rng)
            plan = "\n".join(
                str(r[0]) for r in conn.execute(
                    "EXPLAIN SELECT memory_id FROM memories WHERE account_id = %s "
                    f"ORDER BY embedding <=> %s::VECTOR({EMBED_DIMS}) LIMIT 4",
                    (PROBE_ACCOUNT, query_vec),
                ).fetchall()
            )

            uses_ann = "vector search" in plan.lower()
            check(
                uses_ann,
                "the optimizer chooses vector search with no index hint",
                plan.encode("ascii", "replace").decode(),
            )
            if uses_ann:
                for line in plan.splitlines():
                    if "vector search" in line.lower() or "memories@" in line:
                        print(f"        | {line.strip().encode('ascii', 'replace').decode()}")
    finally:
        print(f"\nDropping {CLEANROOM_DB}")
        with psycopg.connect(base, autocommit=True, connect_timeout=30) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {CLEANROOM_DB} CASCADE")

    print("\n" + "=" * 66)
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("A fresh clone gets a schema whose vector index the optimizer actually uses.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
