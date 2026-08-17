"""The memory corpora.

Two corpora with different jobs, deliberately kept separate.

**Hero** is one account, built to be legible in the UI and the video. It carries
a temporal supersession: an initial policy ceiling, and a superseding policy
written mid-run by an out-of-band update. That supersession is what makes the
memory refresh in the conflict-aware policy causally load-bearing rather than a
no-op -- refreshing a static corpus would return the same memories, infer the
same ceiling, and produce the same action.

**Experiment** is six accounts with distinct policy regimes, providing breadth
for the 100-run table. No account is shared with the hero scenario.

Every account carries distractor memories alongside its policy memory. Without
them retrieval would be trivial -- one memory per account means any retrieval
function, including a broken one, returns the right answer. The distractors are
plausible operational notes, and one of them deliberately mentions a dollar
figure that is NOT a ceiling, so that a retrieval or inference bug that grabs
the first number it sees is caught rather than hidden.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MemorySeed:
    memory_id: str
    text: str
    kind: str  # policy | history | note
    supersedes: str | None = None


@dataclass(frozen=True)
class AccountSeed:
    account_id: str
    name: str
    hard_limit: int
    memories: tuple[MemorySeed, ...]
    # Applied mid-run by an out-of-band policy update, not present at t=0.
    update: tuple[MemorySeed, ...] = ()
    expected_ceiling: int | None = None
    expected_ceiling_after_update: int | None = None


# --------------------------------------------------------------------------
# hero scenario
# --------------------------------------------------------------------------

HERO_ACCOUNT_ID = "hero-001"

HERO = AccountSeed(
    account_id=HERO_ACCOUNT_ID,
    name="Northwind Procurement",
    hard_limit=100,
    expected_ceiling=80,
    expected_ceiling_after_update=60,
    memories=(
        MemorySeed(
            memory_id="hero-m1",
            kind="policy",
            text=(
                "Temporary authorization ceiling for this account is $80 per billing "
                "cycle, pending completion of the quarterly review."
            ),
        ),
        MemorySeed(
            memory_id="hero-m2",
            kind="history",
            text=(
                "Historical allocations against this account averaged $45 per request "
                "during the previous quarter."
            ),
        ),
        MemorySeed(
            memory_id="hero-m3",
            kind="note",
            text=(
                "The primary contact for this account is the procurement team, who "
                "prefer approvals to be batched rather than issued individually."
            ),
        ),
        MemorySeed(
            memory_id="hero-m4",
            kind="note",
            text=(
                "This account was migrated from the legacy billing system in March "
                "and its historical records before that date are incomplete."
            ),
        ),
    ),
    update=(
        MemorySeed(
            memory_id="hero-m5",
            kind="policy",
            supersedes="hero-m1",
            text=(
                "Authorization ceiling reduced to $60 per billing cycle, effective "
                "immediately, superseding the prior temporary ceiling."
            ),
        ),
    ),
)


# --------------------------------------------------------------------------
# experiment corpus
# --------------------------------------------------------------------------


def _standard_distractors(prefix: str, detail: str) -> tuple[MemorySeed, ...]:
    return (
        MemorySeed(
            memory_id=f"{prefix}-n1",
            kind="note",
            text=detail,
        ),
        MemorySeed(
            memory_id=f"{prefix}-n2",
            kind="history",
            text=(
                f"Reconciliation for this account cleared a $250 backlog of unbilled "
                f"line items last cycle; the backlog figure is historical and is not "
                f"an authorization limit."
            ),
        ),
    )


EXPERIMENT_ACCOUNTS: tuple[AccountSeed, ...] = (
    AccountSeed(
        account_id="exp-standard",
        name="Standard regime",
        hard_limit=100,
        expected_ceiling=90,
        memories=(
            MemorySeed(
                "exp-standard-p", kind="policy",
                text="Authorization ceiling for this account is $90 per billing cycle "
                     "under the standard approval regime.",
            ),
            *_standard_distractors(
                "exp-standard",
                "This account is reviewed annually and has no outstanding exceptions.",
            ),
        ),
    ),
    AccountSeed(
        account_id="exp-restricted",
        name="Restricted regime",
        hard_limit=100,
        expected_ceiling=60,
        memories=(
            MemorySeed(
                "exp-restricted-p", kind="policy",
                text="Authorization ceiling for this account is restricted to $60 per "
                     "billing cycle following a compliance review.",
            ),
            *_standard_distractors(
                "exp-restricted",
                "A compliance review was opened on this account and remains in progress.",
            ),
        ),
    ),
    AccountSeed(
        account_id="exp-unrestricted",
        name="Unrestricted regime",
        hard_limit=100,
        expected_ceiling=100,
        memories=(
            MemorySeed(
                "exp-unrestricted-p", kind="policy",
                text="Authorization ceiling for this account is $100 per billing cycle, "
                     "matching the account's full limit with no additional restriction.",
            ),
            *_standard_distractors(
                "exp-unrestricted",
                "This account is on the unrestricted approval track and requires no "
                "secondary sign-off.",
            ),
        ),
    ),
    AccountSeed(
        account_id="exp-tight",
        name="Tight regime",
        hard_limit=100,
        expected_ceiling=45,
        memories=(
            MemorySeed(
                "exp-tight-p", kind="policy",
                text="Authorization ceiling for this account is $45 per billing cycle "
                     "while the account remains in probationary status.",
            ),
            *_standard_distractors(
                "exp-tight",
                "This account entered probationary status after a disputed invoice.",
            ),
        ),
    ),
    AccountSeed(
        account_id="exp-moderate",
        name="Moderate regime",
        hard_limit=100,
        expected_ceiling=75,
        memories=(
            MemorySeed(
                "exp-moderate-p", kind="policy",
                text="Authorization ceiling for this account is $75 per billing cycle "
                     "under the moderate approval regime.",
            ),
            *_standard_distractors(
                "exp-moderate",
                "This account has a stable payment history over eight cycles.",
            ),
        ),
    ),
    # The interesting one: policy claims more headroom than the account has.
    # The hard limit must still bind, and an agent that treats the memory as
    # authorization is wrong in a way the database then has to catch.
    AccountSeed(
        account_id="exp-overpermissive",
        name="Over-permissive policy",
        hard_limit=100,
        expected_ceiling=120,
        memories=(
            MemorySeed(
                "exp-overpermissive-p", kind="policy",
                text="Authorization ceiling for this account is $120 per billing cycle "
                     "as granted by the expanded procurement agreement.",
            ),
            *_standard_distractors(
                "exp-overpermissive",
                "An expanded procurement agreement was signed but the account's "
                "enforced limit has not been raised to match.",
            ),
        ),
    ),
)


ALL_ACCOUNTS: tuple[AccountSeed, ...] = (HERO,) + EXPERIMENT_ACCOUNTS
