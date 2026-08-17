"""Embeddings for the memory corpus.

The real provider is Amazon Titan Text Embeddings V2
(`amazon.titan-embed-text-v2:0`, 1024 dimensions, normalized). Titan V2 returns
unit-length vectors when `normalize` is set, which is why the vector index uses
`vector_cosine_ops` -- cosine distance is the metric the model was trained for.

There is also a deterministic local provider. It exists so the retrieval and
scenario logic can be developed and tested without Bedrock access, and it is
**not** a silent substitute: it must be requested explicitly via
`RACELAB_EMBED_PROVIDER=hash`, it announces itself on every construction, and
every artifact it produces records which provider generated it. Any result
reported as a RaceLab finding uses Titan. A hash-embedded corpus can prove that
retrieval plumbing works; it cannot prove that retrieval is semantically
meaningful, and nothing in this repo will claim otherwise.

Embeddings are cached to disk by (provider, model, text) because they are
deterministic and Bedrock calls cost money.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import struct
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CACHE_DIR = REPO_ROOT / "data" / "embed_cache"

EMBED_DIMS = 1024
TITAN_MODEL_ID = "amazon.titan-embed-text-v2:0"


class Embedder:
    """Interface. `name` ends up in artifacts so provenance is never ambiguous."""

    name: str = "abstract"
    dims: int = EMBED_DIMS

    def embed(self, text: str) -> list[float]:
        raise NotImplementedError

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


class TitanEmbedder(Embedder):
    name = "titan-v2"

    def __init__(self, model_id: str | None = None, region: str | None = None,
                 dims: int = EMBED_DIMS):
        import boto3

        self.model_id = model_id or os.environ.get("RACELAB_EMBED_MODEL_ID", TITAN_MODEL_ID)
        self.region = region or os.environ.get("AWS_REGION") or "us-east-1"
        self.dims = dims
        self._client = boto3.client("bedrock-runtime", region_name=self.region)

    def embed(self, text: str) -> list[float]:
        cached = _cache_get(self.name, self.model_id, text)
        if cached is not None:
            return cached

        body = json.dumps({
            "inputText": text,
            "dimensions": self.dims,
            # Titan V2 returns unit vectors when normalized, which is what makes
            # cosine the right index metric.
            "normalize": True,
        })
        resp = self._client.invoke_model(modelId=self.model_id, body=body)
        payload = json.loads(resp["body"].read())
        vec = [float(x) for x in payload["embedding"]]
        if len(vec) != self.dims:
            raise RuntimeError(
                f"{self.model_id} returned {len(vec)} dimensions, expected {self.dims}"
            )
        _cache_put(self.name, self.model_id, text, vec)
        return vec


class HashEmbedder(Embedder):
    """Deterministic, offline, and semantically meaningless beyond word overlap.

    Words are hashed into buckets and the vector is L2-normalized, so texts
    sharing vocabulary land near each other. That is enough to exercise
    retrieval plumbing and ranking code. It is not a semantic model, and a
    corpus embedded with it cannot support any claim about retrieval quality.
    """

    name = "hash-local"

    def __init__(self, dims: int = EMBED_DIMS, quiet: bool = False):
        self.dims = dims
        if not quiet:
            print(
                "WARNING: using the local hash embedder, not Titan. Results from "
                "this provider are for plumbing tests only.",
                file=sys.stderr,
            )

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dims
        words = [w for w in _tokenize(text) if w]
        for word in words:
            digest = hashlib.sha256(word.encode("utf-8")).digest()
            for k in range(4):
                idx = struct.unpack_from(">I", digest, k * 4)[0] % self.dims
                sign = 1.0 if digest[16 + k] & 1 else -1.0
                vec[idx] += sign
        norm = sum(v * v for v in vec) ** 0.5
        if norm == 0:
            return vec
        return [v / norm for v in vec]


def _tokenize(text: str) -> list[str]:
    out, cur = [], []
    for ch in text.lower():
        if ch.isalnum():
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------


def _cache_key(provider: str, model: str, text: str) -> str:
    h = hashlib.sha256(f"{provider}\x00{model}\x00{text}".encode("utf-8")).hexdigest()
    return h[:32]


def _cache_get(provider: str, model: str, text: str) -> list[float] | None:
    path = CACHE_DIR / f"{_cache_key(provider, model, text)}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))["embedding"]
    except Exception:
        return None


def _cache_put(provider: str, model: str, text: str, vec: list[float]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{_cache_key(provider, model, text)}.json"
    path.write_text(
        json.dumps({"provider": provider, "model": model, "text": text, "embedding": vec}),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------


def get_embedder(provider: str | None = None) -> Embedder:
    """Pick a provider. Defaults to Titan; the local one must be asked for."""
    choice = (provider or os.environ.get("RACELAB_EMBED_PROVIDER") or "titan").lower()
    if choice in ("titan", "titan-v2", "bedrock"):
        return TitanEmbedder()
    if choice in ("hash", "hash-local", "local"):
        return HashEmbedder()
    raise SystemExit(f"unknown embedding provider {choice!r}")


def bedrock_available() -> tuple[bool, str]:
    """Report whether Bedrock embeddings can actually be called right now."""
    try:
        import boto3
    except ImportError:
        return False, "boto3 is not installed"
    session = boto3.Session()
    if session.get_credentials() is None:
        return False, "no AWS credentials found (env, shared config, or instance role)"
    region = os.environ.get("AWS_REGION") or session.region_name
    if not region:
        return False, "no AWS region configured (set AWS_REGION)"
    try:
        emb = TitanEmbedder()
        vec = emb.embed("racelab connectivity probe")
        return True, f"{emb.model_id} in {emb.region} returned {len(vec)} dimensions"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"


if __name__ == "__main__":
    ok, detail = bedrock_available()
    print(("AVAILABLE: " if ok else "UNAVAILABLE: ") + detail)
    sys.exit(0 if ok else 1)
