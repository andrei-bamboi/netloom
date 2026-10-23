"""Resilience properties of the workflow worker."""

import json

import boto3
import pytest

from conftest import call, run_workflow
from netloom import provisioner, store

BODY = {"name": "resilient", "cidr": "10.9.0.0/16", "layout": {"tiers": ["public", "private"], "zones": 2}}


def _create():
    return call("POST /v1/networks", body=BODY)[1]["id"]


def test_steps_are_idempotent(aws):
    nid = _create()
    run_workflow(nid)
    # Re-running resource steps must not create duplicates.
    for step in ("vpc", "subnets", "routing"):
        provisioner.handler({"step": step, "network_id": nid})
    ec2 = boto3.client("ec2")
    tag = [{"Name": "tag:netloom:network-id", "Values": [nid]}]
    assert len(ec2.describe_vpcs(Filters=tag)["Vpcs"]) == 1
    assert len(ec2.describe_subnets(Filters=tag)["Subnets"]) == 4
    assert len(ec2.describe_internet_gateways(Filters=tag)["InternetGateways"]) == 1


def test_orphaned_vpc_is_adopted_not_duplicated(aws):
    """Simulates a crash after CreateVpc succeeded but before the id was saved."""
    nid = _create()
    provisioner.handler({"step": "begin", "network_id": nid})
    provisioner.handler({"step": "vpc", "network_id": nid})
    vpc_id = store.get_network_meta(nid)["vpc_id"]
    store.update_network(nid, vpc_id=None)  # "lose" the write

    provisioner.handler({"step": "vpc", "network_id": nid})
    assert store.get_network_meta(nid)["vpc_id"] == vpc_id
    assert any(e["type"] == "recovered" for e in store.list_events(nid))


def test_rollback_and_fail_after_error(aws):
    nid = _create()
    provisioner.handler({"step": "begin", "network_id": nid})
    provisioner.handler({"step": "vpc", "network_id": nid})
    provisioner.handler({"step": "subnets", "network_id": nid})

    provisioner.handler({"step": "rollback", "network_id": nid})
    error = {"Error": "StepFailed", "Cause": json.dumps({"errorMessage": "VpcLimitExceeded: too many VPCs"})}
    provisioner.handler({"step": "fail", "network_id": nid, "error": error})

    _, net, _ = call("GET /v1/networks/{id}", path={"id": nid})
    assert net["status"] == "FAILED"
    assert net["error"] == {"code": "StepFailed", "message": "VpcLimitExceeded: too many VPCs"}
    tag = [{"Name": "tag:netloom:network-id", "Values": [nid]}]
    assert boto3.client("ec2").describe_vpcs(Filters=tag)["Vpcs"] == []

    # A FAILED network can be deleted (cleans up anything left) ...
    assert call("DELETE /v1/networks/{id}", path={"id": nid})[0] == 202
    run_workflow(nid, "delete")
    assert store.get_network_meta(nid)["status"] == "DELETED"


def test_error_classification(aws):
    from botocore.exceptions import ClientError

    def boom(code):
        def fn(**_):
            raise ClientError({"Error": {"Code": code, "Message": code}}, "Op")

        return fn

    with pytest.raises(provisioner.RetryableStepError):
        provisioner._call(boom("RequestLimitExceeded"))
    with pytest.raises(provisioner.StepFailed):
        provisioner._call(boom("VpcLimitExceeded"))
    assert provisioner._call(boom("RouteAlreadyExists"), ignore=("RouteAlreadyExists",)) is None


def test_unknown_step(aws):
    nid = _create()
    with pytest.raises(provisioner.StepFailed):
        provisioner.handler({"step": "explode", "network_id": nid})
