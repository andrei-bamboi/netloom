#!/usr/bin/env python3
"""End-to-end run against the LocalStack deployment (infra/localstack).

Exercises the real deployed artefacts - API Lambda, Step Functions workflow,
worker Lambda, DynamoDB, EC2 - inside LocalStack. The free LocalStack tier
has no Cognito or API Gateway v2, so this script plays API Gateway's role: it
invokes the API Lambda with the exact payload-v2 event API Gateway sends after
it has verified a JWT, including the verified claims.

    make local-up local-deploy local-smoke
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config

STACK = Path(__file__).resolve().parents[1] / "infra" / "localstack"
TIMEOUT_S = 240


def outputs() -> dict:
    raw = subprocess.run(["terraform", f"-chdir={STACK}", "output", "-json"], capture_output=True, text=True, check=True).stdout
    return {k: v["value"] for k, v in json.loads(raw).items()}


OUT = outputs()
SESSION = boto3.Session(aws_access_key_id="test", aws_secret_access_key="test", region_name=OUT["region"])
CFG = Config(read_timeout=120, retries={"max_attempts": 2})


def client(name: str):
    return SESSION.client(name, endpoint_url=OUT["endpoint"], config=CFG)


LAMBDA, EC2, SFN = client("lambda"), client("ec2"), client("stepfunctions")


def call(route_key: str, *, user: str | None = "alice", body=None, path=None, query=None, headers=None):
    """Invoke the API Lambda exactly as API Gateway (HTTP API, JWT authorizer) would."""
    claims = {"sub": f"local-{user}", "username": user, "client_id": "local-client"} if user else None
    event = {
        "version": "2.0",
        "routeKey": route_key,
        "rawPath": route_key.split(" ", 1)[1],
        "headers": {k.lower(): v for k, v in (headers or {}).items()},
        "queryStringParameters": query,
        "pathParameters": path,
        "isBase64Encoded": False,
        "requestContext": {"authorizer": {"jwt": {"claims": claims, "scopes": None}}} if claims else {},
    }
    if body is not None:
        event["body"] = json.dumps(body)
    resp = LAMBDA.invoke(FunctionName=OUT["api_function_name"], Payload=json.dumps(event).encode())
    payload = json.loads(resp["Payload"].read() or b"null")
    if resp.get("FunctionError"):
        sys.exit(f"Lambda error on {route_key}: {json.dumps(payload, indent=2)}")
    return payload["statusCode"], json.loads(payload.get("body") or "null"), payload.get("headers", {})


def step(msg: str) -> None:
    print(f"\n\033[1m==> {msg}\033[0m")


def check(cond: bool, msg: str) -> None:
    if not cond:
        sys.exit(f"   FAILED: {msg}")
    print(f"   ok: {msg}")


def wait_for(nid: str, wanted: str) -> dict:
    deadline = time.time() + TIMEOUT_S
    last = None
    while time.time() < deadline:
        _, net, _ = call("GET /v1/networks/{id}", path={"id": nid})
        if net["status"] != last:
            print(f"   status={net['status']}")
            last = net["status"]
        if net["status"] == wanted:
            return net
        if net["status"] == "FAILED":
            _, ev, _ = call("GET /v1/networks/{id}/events", path={"id": nid})
            print(json.dumps(ev["items"], indent=2))
            history = SFN.get_execution_history(executionArn=net["execution_arn"], reverseOrder=True, maxResults=5)
            print(json.dumps(history["events"], indent=2, default=str))
            sys.exit(f"   FAILED: network ended {net['status']}: {net.get('error')}")
        time.sleep(2)
    sys.exit(f"   FAILED: timed out waiting for {wanted} (last {last})")


def main() -> None:
    print(f"LocalStack {OUT['endpoint']}  region {OUT['region']}  function {OUT['api_function_name']}")
    body = {
        "name": "local-demo",
        "cidr": "10.88.0.0/16",
        "layout": {"tiers": ["public", "private"], "zones": 2, "prefix": 24},
        "tags": {"purpose": "localstack-smoke"},
    }

    step("health (first call also warms the Lambda container)")
    status, health, _ = call("GET /v1/health", user=None)
    check(status == 200, f"health {health}")

    step("unauthenticated request is refused")
    status, err, _ = call("GET /v1/networks", user=None)
    check(status == 401, f"401 {err['error']['code']}")

    step("dry-run plan")
    status, plan, _ = call("POST /v1/plans", body=body)
    check(status == 200 and plan["plan"]["summary"]["subnet_count"] == 4, "4 subnets planned")
    for s in plan["plan"]["subnets"]:
        print(f"   {s['name']:<10} {s['cidr']:<15} zone={s['zone']} tier={s['tier']}")

    step("validation errors are reported together")
    overlapping_and_outside = [{"cidr": "10.0.0.0/24"}, {"cidr": "10.0.0.0/25"}, {"cidr": "10.9.0.0/24"}]
    bad = {"name": "bad", "cidr": "10.0.0.0/16", "subnets": overlapping_and_outside}
    status, err, _ = call("POST /v1/networks", body=bad)
    check(status == 422 and len(err["error"]["details"]) == 2, "422 with 2 problems")

    step("create network (async)")
    key = f"local-smoke-{int(time.time())}"
    status, net, headers = call("POST /v1/networks", body=body, headers={"Idempotency-Key": key})
    check(status == 202 and net["status"] == "QUEUED", f"202 QUEUED, Location {headers.get('location')}")
    nid = net["id"]

    step("workflow runs in Step Functions")
    net = wait_for(nid, "AVAILABLE")
    check(net["vpc_id"].startswith("vpc-"), f"vpc {net['vpc_id']}, igw {net.get('internet_gateway_id')}")

    step("resources really exist in (emulated) EC2")
    tag = [{"Name": "tag:netloom:network-id", "Values": [nid]}]
    vpcs = EC2.describe_vpcs(Filters=tag)["Vpcs"]
    subnets = EC2.describe_subnets(Filters=tag)["Subnets"]
    check(len(vpcs) == 1 and vpcs[0]["CidrBlock"] == "10.88.0.0/16", "1 VPC 10.88.0.0/16")
    check(len(subnets) == 4, "4 subnets")

    step("retrieve stored data")
    status, sub, _ = call("GET /v1/networks/{id}/subnets", path={"id": nid})
    for s in sub["items"]:
        print(f"   {s['name']:<10} {s['cidr']:<15} {s['availability_zone']:<15} {s['subnet_id']}")
    check(status == 200 and all(s["state"] == "available" for s in sub["items"]), "subnets recorded")

    step("idempotent retry returns the same network")
    status, again, headers = call("POST /v1/networks", body=body, headers={"Idempotency-Key": key})
    check(again["id"] == nid and headers.get("idempotent-replayed") == "true", "same id, replayed")

    step("any authenticated user can see it")
    _, listing, _ = call("GET /v1/networks", user="bob")
    check(nid in [n["id"] for n in listing["items"]], "bob sees alice's network")

    step("delete (by another user)")
    status, net, _ = call("DELETE /v1/networks/{id}", path={"id": nid}, user="bob")
    check(status == 202 and net["status"] == "DECOMMISSIONING", "202 DECOMMISSIONING")
    wait_for(nid, "DELETED")
    check(EC2.describe_vpcs(Filters=tag)["Vpcs"] == [], "VPC removed from EC2")

    step("audit trail")
    _, ev, _ = call("GET /v1/networks/{id}/events", path={"id": nid})
    for e in ev["items"]:
        print(f"   {e['at']}  {e['type']:<22} {e['actor']['username']:<16} {e['message']}")

    print("\n\033[32mLocalStack smoke test passed.\033[0m")


if __name__ == "__main__":
    main()
