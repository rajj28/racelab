"""Seed accounts on both backends and the memory corpus on CockroachDB.

Accounts and allocations are mirrored across backends because they are what the
arms race on. Memories live only on CockroachDB and are shared by every arm --
see `racelab.schema.MEMORY_BACKEND_NOTE` for why that is a design decision
rather than a compromise.

    python scripts/seed.py --reset
    python scripts/seed.py --apply-hero-update      # the mid-run policy change
    python scripts/seed.py --embed-provider hash    # plumbing only, not for results
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import sys

# Run from a clone without installing anything.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from racelab.db import connect  # noqa: E402
from racelab.embeddings import get_embedder  # noqa: E402
from racelab.memory import MemoryStore  # noqa: E402
from scenario.corpus import ALL_ACCOUNTS, HERO  # noqa: E402


def seed_accounts(backend: str, reset: bool) -> None:
    with connect(backend) as conn:
        if reset:
            conn.execute("DELETE FROM allocations")
        for acct in ALL_ACCOUNTS:
            conn.execute(
                """
                INSERT INTO accounts (account_id, name, hard_limit)
                VALUES (%s, %s, %s)
                ON CONFLICT (account_id) DO UPDATE
                    SET name = EXCLUDED.name, hard_limit = EXCLUDED.hard_limit
                """,
                (acct.account_id, acct.name, acct.hard_limit),
            )
        print(f"  {backend}: {len(ALL_ACCOUNTS)} accounts seeded"
              + (", allocations cleared" if reset else ""))


def seed_memories(provider: str | None, reset: bool) -> None:
    embedder = get_embedder(provider)
    with connect("crdb") as conn:
        store = MemoryStore(conn, embedder)
        # Timestamps are explicit and spaced, so the recency component of
        # ranking has something real to work with and the ordering does not
        # depend on how fast the inserts happened to run.
        base = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)
        total = 0
        for acct in ALL_ACCOUNTS:
            if reset:
                store.clear_account(acct.account_id)
            for i, mem in enumerate(acct.memories):
                store.add(
                    memory_id=mem.memory_id,
                    account_id=acct.account_id,
                    text=mem.text,
                    kind=mem.kind,
                    supersedes=mem.supersedes,
                    created_at=base + datetime.timedelta(hours=i),
                )
                total += 1
        print(f"  crdb: {total} memories embedded with {embedder.name}")


def apply_hero_update(provider: str | None) -> None:
    """The out-of-band policy update, written mid-run in the live demo."""
    embedder = get_embedder(provider)
    with connect("crdb") as conn:
        store = MemoryStore(conn, embedder)
        now = datetime.datetime.now(datetime.timezone.utc)
        for mem in HERO.update:
            store.add(
                memory_id=mem.memory_id,
                account_id=HERO.account_id,
                text=mem.text,
                kind=mem.kind,
                supersedes=mem.supersedes,
                created_at=now,
            )
            print(f"  crdb: applied {mem.memory_id} (supersedes {mem.supersedes})")


def revert_hero_update() -> None:
    with connect("crdb") as conn:
        for mem in HERO.update:
            conn.execute("DELETE FROM memories WHERE memory_id = %s", (mem.memory_id,))
            print(f"  crdb: removed {mem.memory_id}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset", action="store_true",
                    help="clear allocations and re-seed memories from scratch")
    ap.add_argument("--embed-provider", default=None,
                    help="titan (default) or hash (plumbing tests only)")
    ap.add_argument("--apply-hero-update", action="store_true",
                    help="write the superseding policy memory")
    ap.add_argument("--revert-hero-update", action="store_true",
                    help="remove the superseding policy memory")
    ap.add_argument("--accounts-only", action="store_true")
    args = ap.parse_args()

    if args.apply_hero_update:
        apply_hero_update(args.embed_provider)
        return 0
    if args.revert_hero_update:
        revert_hero_update()
        return 0

    print("seeding:")
    for backend in ("crdb", "pg"):
        try:
            seed_accounts(backend, args.reset)
        except SystemExit as exc:
            print(f"  {backend}: skipped ({exc})", file=sys.stderr)
    if not args.accounts_only:
        seed_memories(args.embed_provider, args.reset)
        # The hero corpus starts in its pre-update state; the update is applied
        # explicitly, mid-run, by the demo or the experiment driver.
        revert_hero_update()
    return 0


if __name__ == "__main__":
    sys.exit(main())
