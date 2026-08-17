"""Semantic memory: storage and retrieval over the C-SPANN vector index.

Retrieval here has one job that ordinary RAG does not: it must reliably surface
the *current* policy when an older, superseded policy says something contra-
dictory and is worded almost identically. "Temporary authorization ceiling $80"
and "Authorization ceiling reduced to $60" are near-neighbours in embedding
space by construction -- they are the same sentence with a different number.
Nearest-neighbour distance alone cannot be trusted to prefer the newer one, and
if it picks the stale one the conflict-aware policy silently degrades into the
naive one.

So ranking is four things, in order of authority:

1. **Supersession.** A memory explicitly superseded by another retrieved memory
   is dropped. This is deterministic and does not depend on embedding geometry.
2. **Policy currency.** Among memories of kind `policy`, the newest one is
   surfaced first, regardless of distance. See POLICY_CURRENCY_NOTE.
3. **Recency.** A small additive bonus, breaking ties toward newer memories.
4. **Relevance.** Cosine distance from the query, which is what the index
   accelerates and what decides which memories are candidates at all.

The vector index is what makes step 3 a real search rather than a table scan.
Delete it and retrieval still returns answers, which is exactly why the
`EXPLAIN` assertion in `verify_env.py` and the ablation in the experiment
matter: the claim "the index is load-bearing" has to be demonstrated, not
asserted.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

import psycopg

from .embeddings import EMBED_DIMS, Embedder


POLICY_CURRENCY_NOTE = """\
Why policy currency is a rule and not a bigger recency weight.

Measured on the hero pair with Titan V2 embeddings:

    query: "What is the current authorization ceiling and spending policy
            for this account?"

    hero-m1  "Temporary authorization ceiling for this account is $80 ..."
             distance 0.4032   <- STALE, and the CLOSER of the two
    hero-m5  "Authorization ceiling reduced to $60 per billing cycle ..."
             distance 0.5806   <- CURRENT, and the further of the two

Semantic similarity ranks the superseded policy 0.1774 nearer the query than
the policy that replaced it. That is not noise and it is not a bad corpus. It
is systematic: a query asks what the policy *is*, so it matches a memory phrased
as a statement of standing policy. A supersession event is phrased as a change
-- "reduced to", "effective immediately" -- and reads less like an answer to
"what is the policy" than the thing it just overruled. Embedding models will
tend to prefer the stale statement for exactly the reason it is stale.

The additive recency weight was 0.15 and would have needed to exceed 0.1774 to
overturn this pair. Raising it to 0.20 does work. It was also a number chosen
after seeing which number worked, which is fitting a parameter to the test, and
the next corpus with a 0.25 gap would silently break. So it was not raised.

Instead currency is lexicographic within the `policy` kind: of the policy
memories retrieved, the newest is surfaced first whatever the distances are.
This is defensible before seeing any data -- for a statement of policy,
"current" is not a tiebreaker against "similar", it is the entire question --
and it does not depend on a threshold.

Relevance still decides which memories are candidates at all, which is what the
vector index accelerates. Currency only decides which of the retrieved policies
is authoritative.
"""


@dataclass(frozen=True)
class Memory:
    memory_id: str
    account_id: str
    text: str
    kind: str
    supersedes: str | None
    created_at: datetime.datetime
    distance: float | None = None
    score: float | None = None


def to_vector_literal(vec: list[float]) -> str:
    """CockroachDB accepts a VECTOR as a bracketed literal string."""
    return "[" + ",".join(f"{v:.8f}" for v in vec) + "]"


class MemoryStore:
    def __init__(self, conn: psycopg.Connection, embedder: Embedder):
        self.conn = conn
        self.embedder = embedder

    # -- writing ----------------------------------------------------------

    def add(
        self,
        memory_id: str,
        account_id: str,
        text: str,
        kind: str,
        supersedes: str | None = None,
        created_at: datetime.datetime | None = None,
    ) -> None:
        vec = self.embedder.embed(text)
        self.conn.execute(
            """
            INSERT INTO memories
                (memory_id, account_id, text, embedding, kind, supersedes, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, now()))
            ON CONFLICT (memory_id) DO UPDATE SET
                text = EXCLUDED.text,
                embedding = EXCLUDED.embedding,
                kind = EXCLUDED.kind,
                supersedes = EXCLUDED.supersedes,
                created_at = EXCLUDED.created_at
            """,
            (memory_id, account_id, text, to_vector_literal(vec), kind, supersedes, created_at),
        )

    def clear_account(self, account_id: str) -> None:
        self.conn.execute("DELETE FROM memories WHERE account_id = %s", (account_id,))

    def count(self, account_id: str) -> int:
        return int(
            self.conn.execute(
                "SELECT count(*) FROM memories WHERE account_id = %s", (account_id,)
            ).fetchone()[0]
        )

    # -- reading ----------------------------------------------------------

    def retrieve(
        self,
        account_id: str,
        query: str,
        k: int = 4,
        candidate_multiplier: int = 3,
        recency_weight: float = 0.15,
    ) -> list[Memory]:
        """Vector search, then supersession filtering, then recency-weighted rank.

        `candidate_multiplier` widens the ANN fetch so that a superseding memory
        and the memory it supersedes are both likely to be in the candidate set.
        If only the stale one were fetched, the supersession filter would have
        nothing to compare against and could not do its job.
        """
        qvec = to_vector_literal(self.embedder.embed(query))
        limit = max(k * candidate_multiplier, k + 4)

        rows = self.conn.execute(
            f"""
            SELECT memory_id, account_id, text, kind, supersedes, created_at,
                   embedding <=> %s::VECTOR({EMBED_DIMS}) AS distance
            FROM memories
            WHERE account_id = %s
            ORDER BY embedding <=> %s::VECTOR({EMBED_DIMS})
            LIMIT %s
            """,
            (qvec, account_id, qvec, limit),
        ).fetchall()

        candidates = [
            Memory(
                memory_id=r[0], account_id=r[1], text=r[2], kind=r[3],
                supersedes=r[4], created_at=r[5], distance=float(r[6]),
            )
            for r in rows
        ]
        return self._rank(candidates, k=k, recency_weight=recency_weight)

    def _rank(self, candidates: list[Memory], k: int, recency_weight: float) -> list[Memory]:
        superseded = {m.supersedes for m in candidates if m.supersedes}
        live = [m for m in candidates if m.memory_id not in superseded]
        if not live:
            return []

        times = [m.created_at.timestamp() for m in live]
        t_min, t_max = min(times), max(times)
        span = (t_max - t_min) or 1.0

        scored = []
        for m in live:
            relevance = 1.0 - (m.distance or 0.0)
            recency = (m.created_at.timestamp() - t_min) / span
            scored.append(
                Memory(
                    memory_id=m.memory_id, account_id=m.account_id, text=m.text,
                    kind=m.kind, supersedes=m.supersedes, created_at=m.created_at,
                    distance=m.distance,
                    score=relevance + recency_weight * recency,
                )
            )
        scored.sort(key=lambda m: m.score, reverse=True)
        return self._apply_policy_currency(scored)[:k]

    @staticmethod
    def _apply_policy_currency(ranked: list[Memory]) -> list[Memory]:
        """Promote the newest `policy` memory above any older one.

        See POLICY_CURRENCY_NOTE for why this is a rule rather than a weight.

        Only the relative order of policy memories changes; everything else
        keeps the position relevance gave it. `infer_ceiling` reads the first
        policy memory it sees, so this is exactly the guarantee it needs -- and
        it holds whether or not anyone remembered to write the `supersedes`
        edge, which is the realistic case this defends against.
        """
        positions = [i for i, m in enumerate(ranked) if m.kind == "policy"]
        if len(positions) < 2:
            return ranked

        policies = sorted(
            (ranked[i] for i in positions), key=lambda m: m.created_at, reverse=True
        )
        out = list(ranked)
        for slot, memory in zip(positions, policies):
            out[slot] = memory
        return out

    def explain_retrieval(self, account_id: str, query: str) -> str:
        """Return the query plan, to prove retrieval is index-backed."""
        qvec = to_vector_literal(self.embedder.embed(query))
        rows = self.conn.execute(
            f"""
            EXPLAIN SELECT memory_id FROM memories
            WHERE account_id = %s
            ORDER BY embedding <=> %s::VECTOR({EMBED_DIMS})
            LIMIT 4
            """,
            (account_id, qvec),
        ).fetchall()
        return "\n".join(str(r[0]) for r in rows)
