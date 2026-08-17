"""A second scenario, so the finding is not an artifact of one contrived setup.

Everything reported so far comes from one hand-built allocation scenario: agents
adding dollar amounts to a running sum against a numeric ceiling. A reviewer's
fair objection is that a single scenario cannot distinguish "we found something
about agents and transactions" from "we built something that produces this".

This is deliberately different along three axes:

  arithmetic   COUNTS of rows, not a SUM of amounts
  action       a CATEGORICAL choice (which tier to grant), not a magnitude
  correction   the right response to a refreshed policy is to grant a DIFFERENT
               KIND of seat, not a smaller number

That last one matters most. In the allocation scenario a stale agent overshoots
and a fresh one picks a smaller amount, so "re-reasoning helped" could be
explained as arithmetic clamping. Here the fresh agent must switch categories --
premium to standard -- which no clamp produces.

## The scenario

An organisation has a seat cap of 12, stored as a column. Twenty agents each try
to grant a seat to a waiting user, preferring premium.

Separately, a policy states how many premium seats are allowed. It lives only in
retrieved text, and it is lowered from 5 to 2 part-way through the run, the same
way the allocation ceiling moves.

  hard limit    COUNT(seats) <= orgs.seat_cap        -- structural, in the database
  policy limit  COUNT(premium seats) <= premium_cap  -- only in retrieved memory

Self-contained: it creates its own tables and its own memories under a separate
account id, then cleans up the allocations it made. It reuses the same vector
index and the same `ConflictAware` wrapper, which is the point -- nothing in the
library or the memory store is specialised to allocation.

Run:  python scripts/test_scenario_seats.py
"""

from __future__ import annotations

import datetime
import pathlib
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import psycopg

from racelab.conflict import ConflictAware, DecisionContext
from racelab.db import ConnectionPool, connect, dsn_for
from racelab.embeddings import get_embedder
from racelab.memory import MemoryStore

ORG = "seats-org-001"
SEAT_CAP = 12          # the hard limit, a column
STALE_PREMIUM_CAP = 5  # what the old policy said
FRESH_PREMIUM_CAP = 2  # what the new policy says
AGENTS = 20
WINDOW_MS = 1000
GAP_MS = 200.0

QUERY = "How many premium seats may this organisation grant under current policy?"
_CAP = re.compile(r"premium seats?[^.]*?(\d+)", re.I)

SEED_MEMORIES = [
    ("seat-m1", "policy",
     f"Premium seats for this organisation are limited to {STALE_PREMIUM_CAP} "
     f"under the current agreement."),
    ("seat-m2", "history",
     "Historically this organisation has requested premium seats for roughly "
     "half of its onboarded users."),
    ("seat-m3", "note",
     "The billing contact for this organisation prefers seat changes to be "
     "batched at month end."),
]
UPDATE_MEMORY = (
    "seat-m4", "policy",
    f"Premium seats reduced to {FRESH_PREMIUM_CAP} pending contract renewal, "
    f"effective immediately, superseding the prior limit.",
    "seat-m1",
)

PASS, FAIL = "  [PASS]", "  [FAIL]"
_results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((PASS if ok else FAIL, name, detail))
    print(f"{PASS if ok else FAIL} {name}" + (f" -- {detail}" if detail else ""))


@dataclass
class Grant:
    action: str
    tier: str | None
    inferred_cap: int | None


def ensure_schema(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orgs (
            org_id   TEXT PRIMARY KEY,
            seat_cap INT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seats (
            seat_id    UUID PRIMARY KEY,
            org_id     TEXT NOT NULL,
            user_id    TEXT NOT NULL,
            tier       TEXT NOT NULL,
            run_id     TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS seats_org_idx ON seats (org_id, tier)")
    conn.execute(
        "INSERT INTO orgs (org_id, seat_cap) VALUES (%s,%s) "
        "ON CONFLICT (org_id) DO UPDATE SET seat_cap = EXCLUDED.seat_cap",
        (ORG, SEAT_CAP))


def seed_memories(embedder) -> None:
    with connect("crdb") as conn:
        store = MemoryStore(conn, embedder)
        conn.execute("DELETE FROM memories WHERE account_id = %s", (ORG,))
        base = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)
        for i, (mid, kind, text) in enumerate(SEED_MEMORIES):
            store.add(memory_id=mid, account_id=ORG, text=text, kind=kind,
                      created_at=base + datetime.timedelta(hours=i))


def reset_run() -> None:
    with connect("crdb") as conn:
        conn.execute("DELETE FROM seats WHERE org_id = %s", (ORG,))
        conn.execute("DELETE FROM memories WHERE memory_id = %s", (UPDATE_MEMORY[0],))


def counts(cur) -> tuple[int, int]:
    cur.execute(
        "SELECT count(*), count(*) FILTER (WHERE tier = 'premium') "
        "FROM seats WHERE org_id = %s", (ORG,))
    row = cur.fetchone()
    return int(row[0]), int(row[1])


def final_counts() -> tuple[int, int]:
    with connect("crdb") as conn:
        with conn.cursor() as cur:
            return counts(cur)


def infer_cap(memories) -> int | None:
    """Read the premium cap out of the highest-authority policy memory."""
    for m in memories:
        if m.kind != "policy":
            continue
        found = _CAP.search(m.text)
        if found:
            return int(found.group(1))
    return None


def decide(ctx: DecisionContext) -> Grant:
    """Prefer premium; fall back to standard; abstain when the org is full.

    The correction a refreshed policy forces here is categorical: not "grant
    fewer" but "grant a different kind".
    """
    total, premium = ctx.observed
    cap = infer_cap(ctx.memory or [])
    effective = STALE_PREMIUM_CAP if cap is None else cap

    if total >= SEAT_CAP:
        return Grant("abstain", None, cap)
    if premium < effective:
        return Grant("grant(premium)", "premium", cap)
    return Grant("grant(standard)", "standard", cap)


def make_apply(run_id: str, agent_id: str):
    def apply(cur, proposal) -> bool:
        if proposal.tier is None:
            return False
        cur.execute(
            "INSERT INTO seats (seat_id, org_id, user_id, tier, run_id) "
            "VALUES (%s,%s,%s,%s,%s)",
            (str(uuid.uuid4()), ORG, f"user-{agent_id}", proposal.tier, run_id))
        return True
    return apply


def seat_cap_constraint(cur, proposal) -> str | None:
    """The structural rule, verified before commit: never exceed the seat cap."""
    total, _ = counts(cur)
    if total > SEAT_CAP:
        return f"{total} seats would exceed the cap of {SEAT_CAP}"
    return None


def run_arm(*, re_reason: bool, refresh_memory: bool, embedder, pool,
            constraint=None) -> dict:
    reset_run()
    run_id = f"seats-{uuid.uuid4().hex[:8]}"
    started = threading.Event()
    lock = threading.Lock()
    tally = {"committed": 0, "abstained": 0, "refused": 0, "exhausted": 0,
             "errors": 0, "conflicts": 0, "reason_calls": 0, "revised": 0}

    conns = [psycopg.connect(dsn_for("crdb"), autocommit=True) for _ in range(AGENTS)]
    updater = psycopg.connect(dsn_for("crdb"), autocommit=True)
    rng_offsets = [(i / AGENTS) * WINDOW_MS / 1000.0 for i in range(AGENTS)]

    def refresh(agent_id: str):
        with pool.lease() as c:
            return MemoryStore(c, embedder).retrieve(ORG, QUERY, k=4)

    def agent(i: int) -> None:
        started.wait()
        time.sleep(rng_offsets[i])
        agent_id = f"agent-{i:02d}"
        wrapper = ConflictAware(
            operational_read=counts,
            apply=make_apply(run_id, agent_id),
            reason=decide,
            re_reason=re_reason,
            refresh_memory=refresh,
            refresh_memory_on_conflict=refresh_memory,
            constraint=constraint,
            max_attempts=5,
            reasoning_gap_ms=GAP_MS,
        )
        try:
            res = wrapper.run(conns[i], agent_id=agent_id, run_id=run_id)
            with lock:
                tally[res.outcome] = tally.get(res.outcome, 0) + 1
                tally["conflicts"] += res.conflicts
                tally["reason_calls"] += res.reason_calls
                tally["revised"] += 1 if res.revised else 0
        except Exception:  # noqa: BLE001
            with lock:
                tally["errors"] += 1

    def updater_thread() -> None:
        started.wait()
        time.sleep(WINDOW_MS * 0.5 / 1000.0)
        mid, kind, text, supersedes = UPDATE_MEMORY
        MemoryStore(updater, embedder).add(
            memory_id=mid, account_id=ORG, text=text, kind=kind,
            supersedes=supersedes,
            created_at=datetime.datetime.now(datetime.timezone.utc))

    threads = [threading.Thread(target=agent, args=(i,)) for i in range(AGENTS)]
    threads.append(threading.Thread(target=updater_thread))
    for t in threads:
        t.start()
    started.set()
    for t in threads:
        t.join(timeout=120)
    for c in (*conns, updater):
        try:
            c.close()
        except psycopg.Error:
            pass

    total, premium = final_counts()
    tally["total_seats"] = total
    tally["premium_seats"] = premium
    return tally


def main() -> int:
    print("Second scenario: license seats, counts and a categorical choice")
    print("=" * 80)
    print(f"  org seat cap {SEAT_CAP} (a column)   premium cap "
          f"{STALE_PREMIUM_CAP} -> {FRESH_PREMIUM_CAP} (retrieved text only)")
    print(f"  {AGENTS} agents, {WINDOW_MS}ms arrival window")
    print("  a fresh agent must grant a DIFFERENT TIER, not a smaller number")
    print()

    embedder = get_embedder("titan")
    with connect("crdb") as conn:
        ensure_schema(conn)
    seed_memories(embedder)
    pool = ConnectionPool("crdb", size=6)

    try:
        print("1. Retrieval surfaces the superseding policy at all")
        reset_run()
        with connect("crdb") as conn:
            store = MemoryStore(conn, embedder)
            before = infer_cap(store.retrieve(ORG, QUERY, k=4))
            mid, kind, text, supersedes = UPDATE_MEMORY
            store.add(memory_id=mid, account_id=ORG, text=text, kind=kind,
                      supersedes=supersedes,
                      created_at=datetime.datetime.now(datetime.timezone.utc))
            after = infer_cap(store.retrieve(ORG, QUERY, k=4))
        check("stale corpus yields the old cap", before == STALE_PREMIUM_CAP,
              f"inferred {before}")
        check("fresh corpus yields the new cap", after == FRESH_PREMIUM_CAP,
              f"inferred {after}")

        print("\n2. Naive: replays its grant")
        naive = run_arm(re_reason=False, refresh_memory=False,
                        embedder=embedder, pool=pool)
        print(f"     seats={naive['total_seats']} premium={naive['premium_seats']} "
              f"conflicts={naive['conflicts']} revised={naive['revised']}")

        print("\n3. C-ops: re-reasons over fresh counts, stale policy")
        cops = run_arm(re_reason=True, refresh_memory=False,
                       embedder=embedder, pool=pool)
        print(f"     seats={cops['total_seats']} premium={cops['premium_seats']} "
              f"conflicts={cops['conflicts']} revised={cops['revised']}")

        print("\n4. C: re-reasons over fresh counts AND fresh policy")
        c = run_arm(re_reason=True, refresh_memory=True,
                    embedder=embedder, pool=pool)
        print(f"     seats={c['total_seats']} premium={c['premium_seats']} "
              f"conflicts={c['conflicts']} revised={c['revised']}")

        print("\n5. The same pattern as the allocation scenario")
        check("naive over-granted premium seats",
              naive["premium_seats"] > FRESH_PREMIUM_CAP,
              f"{naive['premium_seats']} > {FRESH_PREMIUM_CAP}")
        check("C-ops still over-granted premium, reasoning over stale policy",
              cops["premium_seats"] > FRESH_PREMIUM_CAP,
              f"{cops['premium_seats']} premium, stale cap was {STALE_PREMIUM_CAP}")
        check("C-ops stayed within the cap it REMEMBERED",
              cops["premium_seats"] <= STALE_PREMIUM_CAP,
              f"{cops['premium_seats']} <= {STALE_PREMIUM_CAP}")
        check("C respected the refreshed policy",
              c["premium_seats"] <= FRESH_PREMIUM_CAP,
              f"{c['premium_seats']} <= {FRESH_PREMIUM_CAP}")
        check("C switched tier rather than granting fewer seats",
              c["total_seats"] > c["premium_seats"],
              f"{c['total_seats']} seats of which {c['premium_seats']} premium")
        check("no arm exceeded the structural seat cap",
              all(x["total_seats"] <= SEAT_CAP for x in (naive, cops, c)),
              f"{naive['total_seats']}, {cops['total_seats']}, {c['total_seats']} "
              f"<= {SEAT_CAP}")

        print("\n6. The guardrail also generalises")
        guarded = run_arm(re_reason=True, refresh_memory=True, embedder=embedder,
                          pool=pool, constraint=seat_cap_constraint)
        print(f"     seats={guarded['total_seats']} premium={guarded['premium_seats']} "
              f"refused={guarded.get('refused', 0)}")
        check("the seat cap held with a constraint supplied",
              guarded["total_seats"] <= SEAT_CAP,
              f"{guarded['total_seats']} <= {SEAT_CAP}")
    finally:
        pool.close()
        with connect("crdb") as conn:
            conn.execute("DELETE FROM seats WHERE org_id = %s", (ORG,))

    print("\n" + "=" * 80)
    failed = [r for r in _results if r[0] == FAIL]
    print(f"{len(_results) - len(failed)}/{len(_results)} passed")
    if failed:
        for _, name, detail in failed:
            print(f"  FAILED: {name} {detail}")
        return 1
    print("\nDifferent domain, counts instead of sums, and a categorical correction")
    print("rather than a numeric clamp -- and the same three-way split appears.")
    print("The finding is about stale retrieved context, not about arithmetic.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
