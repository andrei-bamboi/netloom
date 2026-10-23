import json
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

REGION = "eu-central-1"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    for k, v in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "TABLE_NAME": "netloom-test",
        "MAX_SUBNETS": "16",
    }.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def aws(aws_env):
    """Moto-backed AWS with the same table + state machine Terraform creates."""
    from netloom import api, provisioner, store

    with mock_aws():
        ddb = boto3.client("dynamodb")
        ddb.create_table(
            TableName=os.environ["TABLE_NAME"],
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "kind", "AttributeType": "S"},
                {"AttributeName": "id", "AttributeType": "S"},
            ],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "by_kind",
                    "KeySchema": [
                        {"AttributeName": "kind", "KeyType": "HASH"},
                        {"AttributeName": "id", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
        )
        iam = boto3.client("iam")
        role = iam.create_role(RoleName="sfn", AssumeRolePolicyDocument="{}")["Role"]["Arn"]
        sm = boto3.client("stepfunctions").create_state_machine(
            name="netloom", definition=json.dumps({"StartAt": "x", "States": {"x": {"Type": "Pass", "End": True}}}), roleArn=role
        )
        os.environ["STATE_MACHINE_ARN"] = sm["stateMachineArn"]
        store.reset_cache()
        provisioner.reset_cache()
        api._sfn.cache_clear()
        yield
        store.reset_cache()
        provisioner.reset_cache()
        api._sfn.cache_clear()


def http_event(route_key: str, *, body=None, path=None, query=None, headers=None, user="alice"):
    claims = {"sub": f"sub-{user}", "username": user, "client_id": "client123"} if user else None
    event = {
        "version": "2.0",
        "routeKey": route_key,
        "headers": headers or {},
        "queryStringParameters": query,
        "pathParameters": path,
        "isBase64Encoded": False,
        "requestContext": {"authorizer": {"jwt": {"claims": claims}}} if claims else {},
    }
    if body is not None:
        event["body"] = body if isinstance(body, str) else json.dumps(body)
    return event


def call(route_key: str, **kwargs):
    from netloom import api

    resp = api.handler(http_event(route_key, **kwargs))
    return resp["statusCode"], json.loads(resp.get("body") or "null"), resp["headers"]


def run_workflow(network_id: str, operation: str = "create"):
    """Mimic the state machine: run the same steps in the same order."""
    from netloom import provisioner

    steps = ["begin", "vpc", "subnets", "routing", "finalize"] if operation == "create" else ["teardown", "retire"]
    for step in steps:
        provisioner.handler({"step": step, "network_id": network_id})
