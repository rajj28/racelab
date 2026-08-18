"""Deploy the PUBLIC demo endpoint.

Separate from `deploy/deploy.py` on purpose. That script owns the IAM-signed
write gateway; this one owns an endpoint anyone on the internet can call, and
the two should not share a blast radius or a deployment step.

    python deploy/deploy_demo.py --region ap-south-1 \
        --layer arn:aws:lambda:ap-south-1:946298554578:layer:racelab-psycopg:1
    python deploy/deploy_demo.py --region ap-south-1 --destroy

The package self-verifies before upload, the same way the gateway's does:
extract it, put only that directory on the path, and import the handler in a
fresh interpreter. A missing module fails the build rather than the cold start.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import pathlib
import subprocess
import sys
import time
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
REPO = pathlib.Path(__file__).resolve().parents[1]

try:
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
except ImportError:
    pass

FUNCTION = "racelab-demo"
ROLE = "racelab-gateway-role"          # region-suffixed at call time
SECRET = "racelab/crdb-dsn"
POLICY = "racelab-gateway-inline"   # the role's inline policy, as deploy.py names it

# The exact closure `deploy/demo_handler.py` imports, verified below.
PACKAGE_MODULES = [
    "racelab/__init__.py",
    "racelab/arms.py",
    "racelab/conflict.py",
    "racelab/db.py",
    "racelab/embeddings.py",
    "racelab/experiment.py",
    "racelab/integrations/__init__.py",
    "racelab/integrations/aws.py",
    "racelab/memory.py",
    "scenario/__init__.py",
    "scenario/corpus.py",
    "scenario/decide.py",
]

_SMOKE = """
import sys
class NoYaml:
    def find_spec(self, name, path=None, target=None):
        if name == "yaml" or name.startswith("yaml."):
            raise ImportError("the Lambda layer has no PyYAML")
        return None
sys.meta_path.insert(0, NoYaml())
import demo_handler
assert callable(demo_handler.handler)
print("OK")
"""


def build() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(REPO / "deploy" / "demo_handler.py", "demo_handler.py")
        for rel in PACKAGE_MODULES:
            z.write(REPO / rel, rel)
    pkg = buf.getvalue()

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(io.BytesIO(pkg)) as z:
            z.extractall(tmp)
        env = dict(os.environ, PYTHONPATH=tmp)
        proc = subprocess.run([sys.executable, "-c", _SMOKE], cwd=tmp, env=env,
                              capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise SystemExit("the demo package does not import on its own:\n"
                         + (proc.stderr or proc.stdout)[-1200:])
    print(f"  package {len(pkg)/1024:.0f} KB, verified in a clean interpreter")
    return pkg


def main() -> int:
    import boto3
    from botocore.exceptions import ClientError

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--region", default="ap-south-1")
    ap.add_argument("--layer", help="psycopg layer ARN (required on create)")
    ap.add_argument("--destroy", action="store_true")
    args = ap.parse_args()

    lam = boto3.client("lambda", region_name=args.region)
    iam = boto3.client("iam")
    acct = boto3.client("sts", region_name=args.region).get_caller_identity()["Account"]
    role_name = f"{ROLE}-{args.region}"
    role_arn = f"arn:aws:iam::{acct}:role/{role_name}"

    if args.destroy:
        for call, kw in ((lam.delete_function_url_config, {"FunctionName": FUNCTION}),
                         (lam.delete_function, {"FunctionName": FUNCTION})):
            try:
                call(**kw)
                print(f"  removed {call.__name__}")
            except ClientError as e:
                print(f"  {call.__name__}: {e.response['Error']['Code']}")
        return 0

    pkg = build()

    # The demo shares the gateway's runtime role, so its log group needs adding
    # to the same inline policy. Least privilege is still least privilege with
    # two named log groups in it.
    try:
        pol = iam.get_role_policy(RoleName=role_name, PolicyName=POLICY)
        doc = pol["PolicyDocument"]
        changed = False
        for st in doc.get("Statement", []):
            if "logs:PutLogEvents" in (st.get("Action") or []):
                res = st.get("Resource")
                res = res if isinstance(res, list) else [res]
                want = f"arn:aws:logs:{args.region}:{acct}:log-group:/aws/lambda/{FUNCTION}:*"
                if want not in res:
                    st["Resource"] = res + [want]
                    changed = True
        if changed:
            iam.put_role_policy(RoleName=role_name, PolicyName=POLICY,
                                PolicyDocument=json.dumps(doc))
            print("  runtime role: added the demo's log group")
        else:
            print("  runtime role: already covers the demo's log group")
    except ClientError as e:
        print(f"  runtime role: could not update ({e.response['Error']['Code']}); "
              f"the demo will run but may not log")

    env = {"Variables": {"RACELAB_DSN_SECRET": SECRET, "AWS_REGION_NAME": args.region}}
    common = dict(Timeout=60, MemorySize=1024, Environment=env)

    try:
        lam.get_function(FunctionName=FUNCTION)
        lam.update_function_code(FunctionName=FUNCTION, ZipFile=pkg)
        waiter = lam.get_waiter("function_updated_v2")
        waiter.wait(FunctionName=FUNCTION)
        cfg = dict(common)
        if args.layer:
            cfg["Layers"] = [args.layer]
        lam.update_function_configuration(FunctionName=FUNCTION, **cfg)
        waiter.wait(FunctionName=FUNCTION)
        print(f"  updated {FUNCTION}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        if not args.layer:
            raise SystemExit("--layer is required when creating the function")
        lam.create_function(
            FunctionName=FUNCTION, Runtime="python3.12", Role=role_arn,
            Handler="demo_handler.handler", Code={"ZipFile": pkg},
            Layers=[args.layer], **common)
        lam.get_waiter("function_active_v2").wait(FunctionName=FUNCTION)
        print(f"  created {FUNCTION}")

    # A PUBLIC function URL. Verified on this account: AuthType NONE is
    # permitted -- the earlier 403 was a missing resource policy, not an SCP.
    url_cfg = dict(
        AuthType="NONE",
        Cors={"AllowOrigins": ["*"], "AllowMethods": ["*"],
              "AllowHeaders": ["content-type"], "MaxAge": 3600})
    try:
        u = lam.create_function_url_config(FunctionName=FUNCTION, **url_cfg)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceConflictException":
            raise
        u = lam.update_function_url_config(FunctionName=FUNCTION, **url_cfg)
    url = u["FunctionUrl"]

    # Without this the URL exists and answers 403 to everyone. This is the piece
    # that was missing the first time public access was attempted.
    try:
        lam.add_permission(
            FunctionName=FUNCTION, StatementId="public-invoke",
            Action="lambda:InvokeFunctionUrl", Principal="*",
            FunctionUrlAuthType="NONE")
        print("  added the public invoke permission")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceConflictException":
            raise
        print("  public invoke permission already present")

    print(f"\n  PUBLIC URL  {url}")
    print(f"  curl -s -X POST {url} -H 'content-type: application/json' "
          f"-d '{{\"arm\":\"C\",\"agents\":6}}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
