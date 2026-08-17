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
ROLE = "racelab-gateway-role"
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


def build_package() -> bytes:
    """Zip the handler plus the parts of racelab it imports.

    Dependencies (psycopg, boto3) are expected from a layer or the runtime;
    boto3 ships with Lambda, psycopg needs a layer. `--layer` names one.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(REPO / "deploy" / "lambda_handler.py", "lambda_handler.py")
        for rel in ["racelab/__init__.py", "racelab/conflict.py", "racelab/db.py",
                    "racelab/integrations/__init__.py", "racelab/integrations/aws.py"]:
            z.write(REPO / rel, rel)
    return buf.getvalue()


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
    try:
        role = c["iam"].create_role(
            RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(TRUST),
            Description="Least-privilege execution role for the RaceLab gateway")
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
    print("attached least-privilege inline policy")

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
    c["lambda"].put_function_concurrency(FunctionName=FUNCTION,
                                         ReservedConcurrentExecutions=concurrency)
    print(f"reserved concurrency = {concurrency} (protects the cluster's "
          f"connection budget)")

    # -- public URL ------------------------------------------------------
    try:
        url = c["lambda"].create_function_url_config(
            FunctionName=FUNCTION, AuthType="NONE",
            Cors={"AllowOrigins": ["*"], "AllowMethods": ["POST"]})["FunctionUrl"]
        c["lambda"].add_permission(
            FunctionName=FUNCTION, StatementId="public-invoke",
            Action="lambda:InvokeFunctionUrl", Principal="*",
            FunctionUrlAuthType="NONE")
        print(f"created function URL {url}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("ResourceConflictException",):
            raise
        url = c["lambda"].get_function_url_config(FunctionName=FUNCTION)["FunctionUrl"]
        print(f"function URL {url}")

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
            RoleName=ROLE, PolicyName="racelab-gateway-inline")),
        ("role", lambda: c["iam"].delete_role(RoleName=ROLE)),
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
