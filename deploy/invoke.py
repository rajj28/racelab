"""Call the deployed gateway, with a signed request.

The function URL uses `AWS_IAM` auth, so an unsigned `curl` gets a 403. That is
the intended behaviour: the endpoint writes to a ledger, and an open write
endpoint is not a demo convenience.

Two ways in, both here:

    python deploy/invoke.py                    # via the SigV4-signed URL
    python deploy/invoke.py --via-sdk          # via lambda:Invoke

Use `--demo` to run the sequence that shows the protocol: repeated calls until
the policy ceiling stops the agent, then a report of what landed.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

try:
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
except ImportError:
    pass

FUNCTION = "racelab-gateway"


def signed_post(url: str, payload: dict, region: str) -> tuple[int, dict]:
    """POST with SigV4. Written out rather than hidden behind a helper.

    `requests` is not a dependency of this project, so this uses botocore's
    signer with urllib -- which also makes the signing step visible, since it is
    the thing a reader is most likely to want to copy.
    """
    import urllib.error
    import urllib.request

    import boto3
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    body = json.dumps(payload)
    request = AWSRequest(method="POST", url=url, data=body,
                         headers={"content-type": "application/json"})
    creds = boto3.Session().get_credentials().get_frozen_credentials()
    SigV4Auth(creds, "lambda", region).add_auth(request)

    req = urllib.request.Request(url, data=body.encode(),
                                 headers=dict(request.headers), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:300]}


def via_sdk(payload: dict, region: str) -> tuple[int, dict]:
    import boto3
    lam = boto3.client("lambda", region_name=region)
    r = lam.invoke(FunctionName=FUNCTION,
                   Payload=json.dumps({"body": json.dumps(payload)}).encode())
    out = json.loads(r["Payload"].read())
    if "statusCode" not in out:
        return 500, out
    return out["statusCode"], json.loads(out["body"])


def url_for(region: str) -> str:
    import boto3
    lam = boto3.client("lambda", region_name=region)
    return lam.get_function_url_config(FunctionName=FUNCTION)["FunctionUrl"]


def describe(status: int, body: dict) -> str:
    outcome = body.get("outcome", "?")
    policy = (f"policy={body.get('policy_status')}"
              + (f" v{body['policy_version']}" if body.get("policy_version") else ""))
    if status == 409:
        lines = [f"  {status} REFUSED  {body.get('reason', '')[:100]}", f"       {policy}"]
        if body.get("refused_actions"):
            lines.append(f"       proposed and rejected: {body['refused_actions']}")
        return "\n".join(lines)
    bits = [f"  {status} {outcome:<10} action={body.get('action')}"]
    if body.get("conflicts"):
        bits.append(f"conflicts={body['conflicts']}")
    if body.get("revised"):
        bits.append("REVISED")
    bits.append(f"limit={body.get('policy_limit')}")
    bits.append(policy)
    bits.append(f"dsn={body.get('dsn_source')}")
    return "  ".join(bits)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account", default="hero-001")
    ap.add_argument("--agent", default="signed-client")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--via-sdk", action="store_true",
                    help="use lambda:Invoke instead of the signed URL")
    ap.add_argument("--demo", action="store_true",
                    help="call repeatedly until the policy ceiling stops it")
    ap.add_argument("--reset", action="store_true",
                    help="clear the account's ledger first")
    args = ap.parse_args()

    if args.reset:
        from racelab.db import connect
        with connect("crdb") as conn:
            conn.execute("DELETE FROM allocations WHERE account_id = %s",
                         (args.account,))
        print(f"cleared the ledger for {args.account}\n")

    url = None
    if not args.via_sdk:
        url = url_for(args.region)
        print(f"POST {url}  (SigV4-signed; unsigned requests get 403)\n")

    calls = 4 if args.demo else 1
    for i in range(calls):
        payload = {"account_id": args.account,
                   "agent_id": f"{args.agent}-{i + 1}" if args.demo else args.agent}
        status, body = (via_sdk(payload, args.region) if args.via_sdk
                        else signed_post(url, payload, args.region))
        print(describe(status, body))

    if args.demo:
        from racelab.db import connect
        with connect("crdb") as conn:
            rows = conn.execute(
                "SELECT agent_id, amount FROM allocations WHERE account_id = %s "
                "ORDER BY agent_id", (args.account,)).fetchall()
            policy = conn.execute(
                "SELECT version, enforceable, source_memory_id FROM policy_constraints "
                "WHERE account_id = %s ORDER BY version DESC LIMIT 1",
                (args.account,)).fetchone()
        total = sum(r[1] for r in rows)
        print(f"\nledger: {rows}")
        print(f"total ${total}")
        if policy:
            print(f"newest compiled policy: v{policy[0]} from {policy[2]} "
                  f"(enforceable={policy[1]})")
        else:
            print("no compiled policy for this account -- the gateway refuses "
                  "every write until scripts/compile_policies.py has run")
        print("\nThe agents that could not fit under the compiled ceiling abstained")
        print("rather than overshooting, and every decision is in CloudWatch with")
        print("the policy version it was made under.")
        print("\nIf a call was refused with policy_status=stale, that is the point:")
        print("the policy document moved and nothing was compiled from it, so")
        print("nothing is authorized until someone recompiles. Run:")
        print(f"    python scripts/compile_policies.py --account {args.account}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
