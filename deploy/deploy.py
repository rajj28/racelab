"""Deploy the write gateway to AWS: role, secret, function, URL, alarm.

Idempotent. Re-running updates the code and configuration rather than failing,
so this is the deploy command and not a one-shot bootstrap.

    python deploy/deploy.py --check      # permissions only, creates nothing
    python deploy/deploy.py              # create or update everything
    python deploy/deploy.py --destroy    # remove what this created

## What it creates, and why each piece is there

    IAM role                 execution identity, least-privilege (below)
    Secrets Manager secret   the CockroachDB DSN, so the credential is rotatable
                             and scoped by IAM instead of living in an env var
    Lambda function          the gateway itself
    Function URL             a public HTTPS endpoint, so there is a demo to call
    Reserved concurrency     a hard ceiling on simultaneous executions
    CloudWatch alarm         fires on any HardLimitViolations datapoint

## The two things worth arguing about

**Reserved concurrency is not a performance setting here, it is a correctness
one.** Lambda will happily run a thousand copies of this; a CockroachDB Basic
cluster will not accept a thousand connections. The default would let AWS
exhaust the memory layer under load. `--concurrency` sets the ceiling explicitly,
and the default is deliberately small.

**The execution role gets four permissions and no more.** Reading one named
secret, invoking two named Bedrock models, writing its own logs, and publishing
metrics in one namespace. Not `SecretsManagerReadWrite`, not `BedrockFullAccess`.
An agent gateway with broad credentials is a worse liability than the race it was
built to prevent.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import pathlib
import sys
import time
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

REPO = pathlib.Path(__file__).resolve().parents[1]

# Load .env before any boto3 client is constructed. Credentials are read at
# client-construction time, so doing this inside a command would be too late.
try:
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
except ImportError:
    pass

FUNCTION = "racelab-gateway"
# Region-suffixed, because IAM is global and the policy attached to it is not.
# Deploying to a second region called put_role_policy on the SAME role with an
# ARN scoped to the new region, which silently revoked the first region's access
# to its own secret. The first function then failed every request with a 500 and
# a fast billed duration -- fast enough that a latency benchmark read it as an
# improvement. A shared role across regions is a trap; one role per region is
# both correct and least-privilege.
def role_name(region: str) -> str:
    return f"racelab-gateway-role-{region}"


ROLE_LEGACY = "racelab-gateway-role"
SECRET = "racelab/crdb-dsn"
ALARM = "racelab-hard-limit-violation"
NAMESPACE = "RaceLab"
RUNTIME = "python3.12"
HANDLER = "lambda_handler.handler"

TRUST = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}


def inline_policy(account: str, region: str) -> dict:
    """Least privilege, written out so a reviewer can check it in one screen."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "OwnLogs",
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream",
                           "logs:PutLogEvents"],
                "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{FUNCTION}*",
            },
            {
                "Sid": "OneSecret",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": f"arn:aws:secretsmanager:{region}:{account}:secret:{SECRET}*",
            },
            {
                "Sid": "NamedModelsOnly",
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel"],
                "Resource": [
                    f"arn:aws:bedrock:{region}::foundation-model/amazon.titan-embed-text-v2:0",
                    f"arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-5-*",
                    f"arn:aws:bedrock:{region}:{account}:inference-profile/us.anthropic.claude-sonnet-4-5-*",
                ],
            },
            {
                "Sid": "OwnMetricsNamespace",
                "Effect": "Allow",
                "Action": ["cloudwatch:PutMetricData"],
                "Resource": "*",
                "Condition": {"StringEquals": {"cloudwatch:namespace": NAMESPACE}},
            },
        ],
    }


REQUIRED_ACTIONS = [
    ("iam", "CreateRole / AttachRolePolicy / PutRolePolicy / PassRole"),
    ("lambda", "CreateFunction / UpdateFunctionCode / CreateFunctionUrlConfig"),
    ("secretsmanager", "CreateSecret / PutSecretValue"),
    ("cloudwatch", "PutMetricAlarm"),
    ("logs", "CreateLogGroup (via the function's own role)"),
]


def _clients(region: str):
    import boto3
    return {
        "iam": boto3.client("iam"),
        "lambda": boto3.client("lambda", region_name=region),
        "sm": boto3.client("secretsmanager", region_name=region),
        "cw": boto3.client("cloudwatch", region_name=region),
        "sts": boto3.client("sts", region_name=region),
    }


# Every module the handler imports, transitively. Listed explicitly rather than
# by globbing `racelab/`, so that adding a module with a heavy dependency cannot
# quietly enlarge the deployment package -- but the list is VERIFIED against the
# handler's real imports below, because an explicit list is exactly the kind of
# thing that goes stale. It did: wiring in the policy compiler added three
# imports and this list was not updated, which would have shipped a function that
# ImportErrors on cold start.
PACKAGE_MODULES = [
    "racelab/__init__.py",
    "racelab/conflict.py",
    "racelab/db.py",
    "racelab/policy.py",
    "racelab/policy_gate.py",
    "racelab/binding.py",
    "racelab/integrations/__init__.py",
    "racelab/integrations/aws.py",
]


# Run inside the extracted package, by a fresh interpreter that can see only the
# package directory. Blocking `yaml` reproduces the Lambda layer, which carries
# psycopg, certifi and python-dotenv and no YAML parser -- so a binding that can
# only be read as YAML fails here rather than on a cold start in production.
_SMOKE = """
import sys
class NoYaml:
    def find_spec(self, name, path=None, target=None):
        if name == "yaml" or name.startswith("yaml."):
            raise ImportError("the Lambda layer has no PyYAML")
        return None
sys.meta_path.insert(0, NoYaml())

import lambda_handler
from racelab.binding import available, ResourceBinding
names = available()
assert names, "no bindings in the package"
for n in names:
    ResourceBinding.named(n)
assert lambda_handler._gate(lambda_handler.DEFAULT_BINDING) is not None
print("OK " + ",".join(names))
"""


def _verify_package(package: bytes) -> None:
    """Import the packaged handler in a fresh interpreter, or fail the build.

    Written after this list went stale exactly once: wiring the policy compiler
    into the handler added three transitive imports and `PACKAGE_MODULES` was not
    updated. Nothing here would have noticed -- the tests import from the repo,
    where every module is present. The only thing that would have caught it is
    the cold start, in production, on the deployed function.

    So the check is the real condition rather than an approximation of it: unzip
    what would be uploaded, put *only* that on the path, block the dependencies
    the layer does not carry, and import. A source scan would miss the
    transitive case entirely -- `binding.py` importing `policy.py` is not visible
    anywhere in `lambda_handler.py`.
    """
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(io.BytesIO(package)) as z:
            z.extractall(tmp)
        env = dict(os.environ)
        # Only the package. Not the repo, which has every module whether or not
        # it was shipped -- that is precisely the confusion being ruled out.
        env["PYTHONPATH"] = tmp
        proc = subprocess.run([sys.executable, "-c", _SMOKE], cwd=tmp, env=env,
                              capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise SystemExit(
            "the deployment package does not import on its own:\n"
            + (proc.stderr or proc.stdout).strip()[-1200:]
            + "\n\nAdd the missing module to PACKAGE_MODULES in deploy/deploy.py, "
              "or the missing dependency to the layer.")
    print(f"  package verified in a clean interpreter: {proc.stdout.strip()}")


def build_package() -> bytes:
    """Zip the handler, the racelab modules it imports, and the bindings.

    Dependencies (psycopg, boto3) are expected from a layer or the runtime;
    boto3 ships with Lambda, psycopg needs a layer. `--layer` names one.

    **Bindings are converted to JSON here.** They are authored as YAML because a
    spec someone edits should be readable, but the Lambda layer has no PyYAML and
    rebuilding it to add one would be a lot of ceremony for a flat mapping.
    `ResourceBinding.load` already prefers `.yaml` and falls back to `.json`, so
    the deployed function finds the JSON copy and never imports a YAML parser.
    The conversion goes through `ResourceBinding`, so a binding that does not
    parse fails the build rather than the cold start.
    """
    import json as _json

    from racelab.binding import BINDINGS_DIR, ResourceBinding, available

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(REPO / "deploy" / "lambda_handler.py", "lambda_handler.py")
        for rel in PACKAGE_MODULES:
            z.write(REPO / rel, rel)

        names = available()
        if not names:
            raise SystemExit(
                f"no bindings found in {BINDINGS_DIR}; the gateway enforces a "
                f"declared resource and has nothing to enforce")
        for name in names:
            source = ResourceBinding.load(BINDINGS_DIR / name)   # parses or raises
            raw = _read_spec(BINDINGS_DIR, name)
            z.writestr(f"bindings/{name}.json", _json.dumps(raw, indent=1))
            print(f"  packaged binding {name}: {source.describe()}")

    package = buf.getvalue()
    _verify_package(package)
    return package


def _read_spec(directory, name: str) -> dict:
    """The binding's raw mapping, whatever file format it was written in."""
    import json as _json
    for suffix in (".yaml", ".yml", ".json"):
        path = directory / f"{name}{suffix}"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if suffix == ".json":
            return _json.loads(text)
        import yaml
        return yaml.safe_load(text)
    raise SystemExit(f"binding {name!r} vanished between listing and reading")


def check(region: str) -> int:
    """Report what is permitted without creating anything."""
    from botocore.exceptions import ClientError
    c = _clients(region)
    ident = c["sts"].get_caller_identity()
    print(f"account {ident['Account']}  identity {ident['Arn']}\n")

    probes = [
        ("iam:ListRoles", lambda: c["iam"].list_roles(MaxItems=1)),
        ("lambda:ListFunctions", lambda: c["lambda"].list_functions(MaxItems=1)),
        ("secretsmanager:ListSecrets", lambda: c["sm"].list_secrets(MaxResults=1)),
        ("cloudwatch:ListMetrics", lambda: c["cw"].list_metrics(Namespace=NAMESPACE)),
    ]
    missing = []
    for name, fn in probes:
        try:
            fn()
            print(f"  OK    {name}")
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            print(f"  {code:<22} {name}")
            missing.append(name)
    if missing:
        print("\nDeployment needs these. Attach the policy in "
              "deploy/iam-policy.json to this identity, then re-run --check.")
        return 1
    print("\nAll probes passed; `python deploy/deploy.py` can run.")
    return 0


def deploy(region: str, concurrency: int, layer: str | None) -> int:
    from botocore.exceptions import ClientError
    c = _clients(region)
    account = c["sts"].get_caller_identity()["Account"]

    dsn = os.environ.get("RACELAB_CRDB_DSN", "").strip()
    if not dsn:
        from dotenv import load_dotenv
        load_dotenv(REPO / ".env")
        dsn = os.environ.get("RACELAB_CRDB_DSN", "").strip()
    if not dsn:
        print("RACELAB_CRDB_DSN is not set; nothing to put in the secret.",
              file=sys.stderr)
        return 2

    # -- secret ----------------------------------------------------------
    try:
        c["sm"].create_secret(Name=SECRET, SecretString=json.dumps({"dsn": dsn}),
                              Description="CockroachDB DSN for the RaceLab gateway")
        print(f"created secret {SECRET}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceExistsException":
            raise
        c["sm"].put_secret_value(SecretId=SECRET,
                                 SecretString=json.dumps({"dsn": dsn}))
        print(f"updated secret {SECRET}")

    # -- role ------------------------------------------------------------
    ROLE = role_name(region)
    try:
        role = c["iam"].create_role(
            RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(TRUST),
            Description=f"Least-privilege execution role for the RaceLab gateway "
                        f"in {region}")
        role_arn = role["Role"]["Arn"]
        print(f"created role {ROLE}")
        time.sleep(10)  # IAM propagation, before Lambda will accept the role
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        role_arn = c["iam"].get_role(RoleName=ROLE)["Role"]["Arn"]
        print(f"role {ROLE} exists")

    c["iam"].put_role_policy(RoleName=ROLE, PolicyName="racelab-gateway-inline",
                             PolicyDocument=json.dumps(inline_policy(account, region)))
    print(f"attached least-privilege inline policy scoped to {region}")

    # -- function --------------------------------------------------------
    package = build_package()
    env = {"Variables": {
        "RACELAB_DSN_SECRET_ID": SECRET,
        "AWS_REGION_NAME": region,
        "RACELAB_REASON_MODEL_ID": os.environ.get(
            "RACELAB_REASON_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
    }}
    layers = [layer] if layer else []
    try:
        c["lambda"].create_function(
            FunctionName=FUNCTION, Runtime=RUNTIME, Role=role_arn, Handler=HANDLER,
            Code={"ZipFile": package}, Timeout=30, MemorySize=512,
            Environment=env, Layers=layers,
            Description="Policy-enforcing write gateway for agent decisions")
        print(f"created function {FUNCTION}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceConflictException":
            raise
        c["lambda"].update_function_code(FunctionName=FUNCTION, ZipFile=package)
        waiter = c["lambda"].get_waiter("function_updated_v2")
        waiter.wait(FunctionName=FUNCTION)
        c["lambda"].update_function_configuration(
            FunctionName=FUNCTION, Role=role_arn, Handler=HANDLER, Timeout=30,
            MemorySize=512, Environment=env, Layers=layers)
        print(f"updated function {FUNCTION}")

    # Correctness, not tuning: Lambda scales past what the cluster accepts.
    try:
        c["lambda"].put_function_concurrency(
            FunctionName=FUNCTION, ReservedConcurrentExecutions=concurrency)
        print(f"reserved concurrency = {concurrency} (protects the cluster's "
              f"connection budget)")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "InvalidParameterValueException":
            raise
        # A new AWS account ships with a total concurrency limit of 10 and
        # refuses to let any of it be reserved, because doing so would drop
        # unreserved capacity below the minimum it insists on keeping.
        #
        # Reported rather than swallowed, and reported accurately: the cap is
        # not gone, it has just moved. The account's own ceiling is now doing
        # the job, and it happens to be *stricter* than what we asked for. That
        # is safe here by accident and would stop being safe the moment the
        # account limit is raised, which is exactly when nobody would think to
        # re-check this.
        try:
            limit = c["lambda"].get_account_settings()["AccountLimit"][
                "ConcurrentExecutions"]
        except Exception:  # noqa: BLE001
            limit = "unknown"
        print(f"  reserved concurrency NOT set: the account's total concurrency "
              f"limit is {limit}, and AWS will not let it drop below its minimum "
              f"unreserved value.")
        print(f"  The cluster is currently protected by that account limit "
              f"({limit}) instead, which is stricter than the {concurrency} we "
              f"asked for. RE-RUN THIS AFTER ANY CONCURRENCY LIMIT INCREASE -- "
              f"the protection disappears the moment the account ceiling rises.")

    # -- public URL ------------------------------------------------------
    # AuthType AWS_IAM, deliberately.
    #
    # This started as NONE, for the convenience of a clickable demo, and the
    # environment refused it -- a 403 AccessDeniedException on every request
    # despite a correct resource policy, which is the signature of an
    # Organizations SCP forbidding public function URLs.
    #
    # That SCP is right and the original choice was wrong. This endpoint writes
    # to a financial ledger. An unauthenticated public URL that anyone can POST
    # to is not a demo convenience, it is an open write endpoint, and no
    # production-readiness argument survives it. Callers sign with SigV4;
    # `deploy/invoke.py` does exactly that.
    auth_type = "AWS_IAM"
    try:
        url = c["lambda"].create_function_url_config(
            FunctionName=FUNCTION, AuthType=auth_type,
            Cors={"AllowOrigins": ["*"], "AllowMethods": ["POST"]})["FunctionUrl"]
        print(f"created function URL {url}  (auth {auth_type})")
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("ResourceConflictException",):
            raise
        c["lambda"].update_function_url_config(
            FunctionName=FUNCTION, AuthType=auth_type,
            Cors={"AllowOrigins": ["*"], "AllowMethods": ["POST"]})
        url = c["lambda"].get_function_url_config(FunctionName=FUNCTION)["FunctionUrl"]
        print(f"function URL {url}  (auth {auth_type})")
    # Remove the public-invoke grant if an earlier run added one.
    try:
        c["lambda"].remove_permission(FunctionName=FUNCTION,
                                      StatementId="public-invoke")
        print("removed the earlier public-invoke grant")
    except ClientError:
        pass

    # -- alarm -----------------------------------------------------------
    try:
        c["cw"].put_metric_alarm(
            AlarmName=ALARM, Namespace=NAMESPACE, MetricName="HardLimitViolations",
            Statistic="Sum", Period=300, EvaluationPeriods=1, Threshold=0,
            ComparisonOperator="GreaterThanThreshold", TreatMissingData="notBreaching",
            AlarmDescription="Any structural invariant violation reaching the ledger")
        print(f"alarm {ALARM} armed (fires on any HardLimitViolations datapoint)")
    except ClientError as exc:
        print(f"alarm not created: {exc.response['Error']['Code']}")

    print("\nendpoint:")
    print(f"  curl -s -X POST {url} -H 'content-type: application/json' \\")
    print("       -d '{\"account_id\":\"hero-001\",\"agent_id\":\"curl\"}'")
    return 0


def destroy(region: str) -> int:
    from botocore.exceptions import ClientError
    c = _clients(region)
    for label, fn in [
        ("function url", lambda: c["lambda"].delete_function_url_config(FunctionName=FUNCTION)),
        ("function", lambda: c["lambda"].delete_function(FunctionName=FUNCTION)),
        ("alarm", lambda: c["cw"].delete_alarms(AlarmNames=[ALARM])),
        ("inline policy", lambda: c["iam"].delete_role_policy(
            RoleName=role_name(region), PolicyName="racelab-gateway-inline")),
        ("role", lambda: c["iam"].delete_role(RoleName=role_name(region))),
        ("secret", lambda: c["sm"].delete_secret(
            SecretId=SECRET, ForceDeleteWithoutRecovery=True)),
    ]:
        try:
            fn()
            print(f"deleted {label}")
        except ClientError as exc:
            print(f"skip {label}: {exc.response['Error']['Code']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--concurrency", type=int, default=8,
                    help="reserved concurrent executions; a cluster connection budget")
    ap.add_argument("--layer", default=None,
                    help="ARN of a layer providing psycopg")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--destroy", action="store_true")
    args = ap.parse_args()

    if args.check:
        return check(args.region)
    if args.destroy:
        return destroy(args.region)
    return deploy(args.region, args.concurrency, args.layer)


if __name__ == "__main__":
    sys.exit(main())
