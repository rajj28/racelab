"""Connection helper shared by the Phase 0 probe and the Phase 1 gate.

CockroachDB Cloud presents a certificate signed by a public CA, so `verify-full`
is the right mode and we keep it. The wrinkle is that `sslrootcert=system` asks
libpq for the operating system's trust store, and the OpenSSL bundled into the
psycopg binary wheel on Windows has no store to point at -- verification fails
even though the certificate is perfectly valid.

Rather than downgrade to a weaker sslmode or paste a machine-specific path into
.env, we hand libpq the `certifi` CA bundle. Same trust anchors, portable
across machines, and the connection stays fully verified.
"""

from __future__ import annotations

import os
import pathlib

from dotenv import load_dotenv

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")


def resolve_dsn(env_key: str) -> str:
    """Read a DSN from the environment and make its TLS settings usable here."""
    dsn = os.environ.get(env_key, "").strip()
    if not dsn:
        raise SystemExit(
            f"{env_key} is not set. Copy .env.example to .env and fill it in."
        )

    if "sslmode=verify-full" not in dsn:
        return dsn

    # An explicit, readable cert file already in the DSN wins.
    if "sslrootcert=" in dsn and "sslrootcert=system" not in dsn:
        return dsn

    try:
        import certifi
    except ImportError:  # pragma: no cover
        return dsn

    bundle = certifi.where()
    if "sslrootcert=system" in dsn:
        return dsn.replace("sslrootcert=system", f"sslrootcert={bundle}")
    sep = "&" if "?" in dsn else "?"
    return f"{dsn}{sep}sslrootcert={bundle}"
