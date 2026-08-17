"""Measure the deployed gateway, and attribute the latency rather than total it.

A single end-to-end number tells you nothing you can act on. This separates the
parts, because they have different fixes:

    cold start      Lambda init + module import + layer load
    secret          Secrets Manager round trip (once per container)
    connect         TLS handshake to CockroachDB Cloud
    per query       one round trip to the cluster
    warm request    what a caller actually experiences after the first

The suspected bottleneck is geographic and worth measuring before fixing: the
function runs in `us-east-1` and the cluster lives in `ap-south-1`, so every
round trip crosses an ocean. The gateway makes several per request -- connect,
read the total, read the policy, insert, check the constraint, commit -- and
they are serial.

Run:  python scripts/benchmark_gateway.py
      python scripts/benchmark_gateway.py --n 10
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

try:
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
except ImportError:
    pass

FUNCTION = "racelab-gateway"


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = min(int(round((p / 100) * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[k]


def summarize(label: str, values: list[float], unit: str = "ms") -> None:
    if not values:
        print(f"  {label:<28} (no samples)")
        return
    print(f"  {label:<28} n={len(values):<3} "
          f"min {min(values):7.0f}  p50 {statistics.median(values):7.0f}  "
          f"p95 {pct(values, 95):7.0f}  max {max(values):7.0f} {unit}")


def measure_local_parts(reps: int) -> dict:
    """Attribute the pieces the Lambda pays, measured from here.

    Measured locally rather than inside the function because the point is the
    breakdown, and a local run isolates each step. The absolute numbers will
    differ from Lambda's; the *ratios* and the geography will not.
    """
    import psycopg
    from racelab.db import dsn_for

    dsn = dsn_for("crdb")
    out: dict[str, list[float]] = {"connect": [], "read_total": [],
                                   "read_policy": [], "commit_cycle": []}
    for _ in range(reps):
        t = time.perf_counter()
        conn = psycopg.connect(dsn, autocommit=True, connect_timeout=15)
        out["connect"].append((time.perf_counter() - t) * 1000)
        try:
            with conn.cursor() as cur:
                t = time.perf_counter()
                cur.execute("SELECT COALESCE(SUM(amount),0) FROM allocations "
                            "WHERE account_id = %s", ("hero-001",))
                cur.fetchone()
                out["read_total"].append((time.perf_counter() - t) * 1000)

                t = time.perf_counter()
                cur.execute("SELECT memory_id, text FROM memories "
                            "WHERE account_id = %s AND kind = 'policy' "
                            "ORDER BY created_at DESC LIMIT 4", ("hero-001",))
                cur.fetchall()
                out["read_policy"].append((time.perf_counter() - t) * 1000)

            t = time.perf_counter()
            conn.execute("BEGIN")
            conn.execute("COMMIT")
            out["commit_cycle"].append((time.perf_counter() - t) * 1000)
        finally:
            conn.close()
    return out


def measure_lambda(n: int, region: str) -> dict:
    import boto3
    lam = boto3.client("lambda", region_name=region)

    # Force a cold start by changing configuration, which replaces containers.
    cfg = lam.get_function_configuration(FunctionName=FUNCTION)
    env = cfg.get("Environment", {}).get("Variables", {})
    env["RACELAB_BENCH_NONCE"] = str(int(time.time()))
    lam.update_function_configuration(FunctionName=FUNCTION,
                                      Environment={"Variables": env})
    lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)

    payload = json.dumps({"body": json.dumps(
        {"account_id": "hero-001", "agent_id": "bench"})}).encode()

    cold = []
    warm = []
    billed = []
    init = []
    for i in range(n):
        t = time.perf_counter()
        r = lam.invoke(FunctionName=FUNCTION, LogType="Tail", Payload=payload)
        elapsed = (time.perf_counter() - t) * 1000
        import base64
        log = base64.b64decode(r["LogResult"]).decode()
        is_cold = "Init Duration" in log
        (cold if is_cold else warm).append(elapsed)
        for line in log.splitlines():
            if line.startswith("REPORT"):
                for part in line.split("\t"):
                    if part.startswith("Billed Duration"):
                        billed.append(float(part.split()[-2]))
                    if part.startswith("Init Duration"):
                        init.append(float(part.split()[-2]))
    return {"cold": cold, "warm": warm, "billed": billed, "init": init}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--reps", type=int, default=4)
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--skip-lambda", action="store_true")
    args = ap.parse_args()

    from racelab.integrations import ccloud
    cluster = ccloud.cluster_name_from_dsn()
    import re
    from racelab.db import dsn_for
    host = re.search(r"@([^:/?]+)", dsn_for("crdb"))
    cluster_region = "unknown"
    if host:
        m = re.search(r"\.(aws|gcp|azure)-([a-z0-9-]+)\.", host.group(1))
        if m:
            cluster_region = m.group(2)

    print("Gateway performance")
    print("=" * 84)
    print(f"  lambda region : {args.region}")
    print(f"  cluster       : {cluster} in {cluster_region}")
    if cluster_region not in (args.region, "unknown"):
        print(f"  ** the function and its memory layer are in different regions **")
    print()

    print("Per-operation cost against the cluster (measured locally):")
    parts = measure_local_parts(args.reps)
    for k in ("connect", "read_total", "read_policy", "commit_cycle"):
        summarize(k, parts[k])
    rt = statistics.median(parts["read_total"]) if parts["read_total"] else 0
    print(f"\n  One round trip is about {rt:.0f} ms. The gateway makes six per")
    print(f"  request -- connect, read total, read policy, insert, constraint")
    print(f"  check, commit -- and they are serial.")
    print()

    if not args.skip_lambda:
        print("Deployed function:")
        lam = measure_lambda(args.n, args.region)
        summarize("cold invocations", lam["cold"])
        summarize("warm invocations", lam["warm"])
        summarize("init duration (billed)", lam["init"])
        summarize("billed duration", lam["billed"])
        if lam["warm"]:
            print(f"\n  A caller sees ~{statistics.median(lam['warm']):.0f} ms warm.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
