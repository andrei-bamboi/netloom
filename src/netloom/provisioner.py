"""Step Functions task worker: performs the actual EC2 work, one step per call.

Design rules that make every step safe to retry:

* DynamoDB is the ledger. Each step re-reads the record and skips work that
  is already recorded (e.g. a VPC id is present).
* Every AWS resource is tagged with ``netloom:network-id`` at creation time.
  If a Lambda dies after EC2 created something but before we recorded it, the
  next attempt *discovers* the orphan by tag instead of creating a duplicate.
  Teardown also discovers by tag, so it cleans up even un-recorded resources.
* Transient failures raise ``RetryableStepError``; the state machine retries
  those with exponential back-off. Anything else fails fast and triggers
  rollback.
"""

from __future__ import annotations

import contextlib
import json
from functools import lru_cache

import boto3
from botocore.exceptions import ClientError

from . import store
from .web import log

TAG_MANAGED = "netloom:managed"
TAG_NETWORK = "netloom:network-id"
TAG_SUBNET = "netloom:subnet-key"
TAG_ROLE = "netloom:role"

_TRANSIENT = {
    "RequestLimitExceeded",
    "Throttling",
    "ThrottlingException",
    "InternalError",
    "ServiceUnavailable",
    "DependencyViolation",  # common during teardown while ENIs/associations settle
    "InvalidVpcID.NotFound",  # eventual consistency right after create
    "InvalidSubnetID.NotFound",
}


class RetryableStepError(Exception):
    """Raised for transient faults; the state machine retries these."""


class StepFailed(Exception):
    """Raised for permanent faults; the state machine rolls back."""


@lru_cache(maxsize=1)
def _ec2():
    return boto3.client("ec2")


def reset_cache() -> None:
    _ec2.cache_clear()


def _code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


def _call(fn, *, ignore: tuple[str, ...] = (), **kwargs):
    """Invoke an EC2 API, classifying errors as ignorable, retryable or fatal."""
    try:
        return fn(**kwargs)
    except ClientError as exc:
        code = _code(exc)
        if code in ignore:
            return None
        if code in _TRANSIENT:
            raise RetryableStepError(f"{code}: {exc}") from exc
        raise StepFailed(f"{code}: {exc.response.get('Error', {}).get('Message', exc)}") from exc


def _tags(network: dict, role: str, name: str, extra: dict | None = None) -> list[dict]:
    tags = {
        **network.get("tags", {}),
        "Name": name,
        TAG_MANAGED: "true",
        TAG_NETWORK: network["id"],
        TAG_ROLE: role,
        **(extra or {}),
    }
    return [{"Key": k, "Value": str(v)} for k, v in tags.items()]


def _by_tag(network_id: str, extra: dict | None = None) -> list[dict]:
    filters = [{"Name": f"tag:{TAG_NETWORK}", "Values": [network_id]}]
    for k, v in (extra or {}).items():
        filters.append({"Name": f"tag:{k}", "Values": [v]})
    return filters


# --------------------------------------------------------------------------- provisioning steps


def step_begin(network: dict) -> None:
    store.transition(network["id"], ("QUEUED",), "PROVISIONING")
    store.add_event(network["id"], "provisioning", "Provisioning started")


def step_vpc(network: dict) -> None:
    ec2, nid = _ec2(), network["id"]
    vpc_id = network.get("vpc_id")
    if not vpc_id:
        found = _call(ec2.describe_vpcs, Filters=_by_tag(nid))["Vpcs"]
        if found:
            vpc_id = found[0]["VpcId"]
            store.add_event(nid, "recovered", f"Adopted previously created VPC {vpc_id}")
        else:
            vpc = _call(
                ec2.create_vpc,
                CidrBlock=network["cidr"],
                TagSpecifications=[{"ResourceType": "vpc", "Tags": _tags(network, "vpc", network["name"])}],
            )["Vpc"]
            vpc_id = vpc["VpcId"]
        store.update_network(nid, vpc_id=vpc_id)

    try:
        ec2.get_waiter("vpc_available").wait(VpcIds=[vpc_id], WaiterConfig={"Delay": 2, "MaxAttempts": 15})
    except Exception as exc:  # waiter errors are not ClientErrors
        raise RetryableStepError(f"VPC {vpc_id} not yet available: {exc}") from exc
    # DNS support lets workloads resolve AWS endpoints and private hosted zones.
    _call(ec2.modify_vpc_attribute, VpcId=vpc_id, EnableDnsSupport={"Value": True})
    _call(ec2.modify_vpc_attribute, VpcId=vpc_id, EnableDnsHostnames={"Value": True})
    store.add_event(nid, "vpc_ready", f"VPC {vpc_id} ({network['cidr']}) is available")


def _zone_map() -> list[str]:
    zones = _call(
        _ec2().describe_availability_zones,
        Filters=[{"Name": "state", "Values": ["available"]}, {"Name": "zone-type", "Values": ["availability-zone"]}],
    )["AvailabilityZones"]
    names = sorted(z["ZoneName"] for z in zones)
    if not names:
        raise StepFailed("No available availability zones in this region.")
    return names


def step_subnets(network: dict) -> None:
    ec2, nid = _ec2(), network["id"]
    vpc_id = network["vpc_id"]
    zones = _zone_map()
    created = 0
    for subnet in store.get_subnets(nid):
        if subnet.get("subnet_id"):
            continue
        az = zones[(ord(subnet["zone"]) - ord("a")) % len(zones)]
        found = _call(ec2.describe_subnets, Filters=_by_tag(nid, {TAG_SUBNET: subnet["key"]}))["Subnets"]
        if found:
            sn = found[0]
        else:
            sn = _call(
                ec2.create_subnet,
                VpcId=vpc_id,
                CidrBlock=subnet["cidr"],
                AvailabilityZone=az,
                TagSpecifications=[
                    {
                        "ResourceType": "subnet",
                        "Tags": _tags(
                            network,
                            f"subnet-{subnet['tier']}",
                            f"{network['name']}/{subnet['name']}",
                            {TAG_SUBNET: subnet["key"]},
                        ),
                    }
                ],
            )["Subnet"]
            created += 1
        if subnet["tier"] == "public":
            _call(ec2.modify_subnet_attribute, SubnetId=sn["SubnetId"], MapPublicIpOnLaunch={"Value": True})
        store.update_subnet(
            nid, subnet["key"], subnet_id=sn["SubnetId"], availability_zone=sn["AvailabilityZone"], state="available"
        )
    store.add_event(nid, "subnets_ready", f"{created} subnet(s) created across {len(zones)} candidate AZs")


def step_routing(network: dict) -> None:
    """Public tier only: Internet Gateway + a route table with a default route."""
    if not network.get("needs_internet_gateway"):
        return
    ec2, nid, vpc_id = _ec2(), network["id"], network["vpc_id"]

    igw_id = network.get("internet_gateway_id")
    if not igw_id:
        found = _call(ec2.describe_internet_gateways, Filters=_by_tag(nid))["InternetGateways"]
        igw_id = (
            found[0]["InternetGatewayId"]
            if found
            else _call(
                ec2.create_internet_gateway,
                TagSpecifications=[{"ResourceType": "internet-gateway", "Tags": _tags(network, "igw", f"{network['name']}-igw")}],
            )["InternetGateway"]["InternetGatewayId"]
        )
        store.update_network(nid, internet_gateway_id=igw_id)
    _call(ec2.attach_internet_gateway, InternetGatewayId=igw_id, VpcId=vpc_id, ignore=("Resource.AlreadyAssociated",))

    rt_id = network.get("public_route_table_id")
    if not rt_id:
        found = _call(ec2.describe_route_tables, Filters=_by_tag(nid, {TAG_ROLE: "public-routes"}))["RouteTables"]
        rt_id = (
            found[0]["RouteTableId"]
            if found
            else _call(
                ec2.create_route_table,
                VpcId=vpc_id,
                TagSpecifications=[
                    {"ResourceType": "route-table", "Tags": _tags(network, "public-routes", f"{network['name']}-public")}
                ],
            )["RouteTable"]["RouteTableId"]
        )
        store.update_network(nid, public_route_table_id=rt_id)
    _call(
        ec2.create_route,
        RouteTableId=rt_id,
        DestinationCidrBlock="0.0.0.0/0",
        GatewayId=igw_id,
        ignore=("RouteAlreadyExists",),
    )
    table = _call(ec2.describe_route_tables, RouteTableIds=[rt_id])["RouteTables"][0]
    associated = {a.get("SubnetId") for a in table.get("Associations", [])}
    for subnet in store.get_subnets(nid):
        if subnet["tier"] == "public" and subnet["subnet_id"] not in associated:
            _call(ec2.associate_route_table, RouteTableId=rt_id, SubnetId=subnet["subnet_id"])
    store.add_event(nid, "routing_ready", f"Internet gateway {igw_id} routed via {rt_id}")


def step_finalize(network: dict) -> None:
    store.transition(network["id"], ("PROVISIONING",), "AVAILABLE")
    store.add_event(network["id"], "available", "Network is available")


# --------------------------------------------------------------------------- teardown


def _teardown(network: dict) -> list[str]:
    """Delete every resource netloom created for this network (recorded or tagged)."""
    ec2, nid = _ec2(), network["id"]
    removed: list[str] = []

    igws = {network.get("internet_gateway_id")} | {
        g["InternetGatewayId"] for g in _call(ec2.describe_internet_gateways, Filters=_by_tag(nid))["InternetGateways"]
    }
    vpc_ids = {network.get("vpc_id")} | {v["VpcId"] for v in _call(ec2.describe_vpcs, Filters=_by_tag(nid))["Vpcs"]}
    vpc_ids.discard(None)
    for igw in filter(None, igws):
        for vpc_id in vpc_ids:
            _call(
                ec2.detach_internet_gateway,
                InternetGatewayId=igw,
                VpcId=vpc_id,
                ignore=("Gateway.NotAttached", "InvalidInternetGatewayID.NotFound"),
            )
        _call(ec2.delete_internet_gateway, InternetGatewayId=igw, ignore=("InvalidInternetGatewayID.NotFound",))
        removed.append(igw)

    for rt in _call(ec2.describe_route_tables, Filters=_by_tag(nid))["RouteTables"]:
        for assoc in rt.get("Associations", []):
            if not assoc.get("Main"):
                _call(
                    ec2.disassociate_route_table,
                    AssociationId=assoc["RouteTableAssociationId"],
                    ignore=("InvalidAssociationID.NotFound",),
                )
        _call(ec2.delete_route_table, RouteTableId=rt["RouteTableId"], ignore=("InvalidRouteTableID.NotFound",))
        removed.append(rt["RouteTableId"])

    for sn in _call(ec2.describe_subnets, Filters=_by_tag(nid))["Subnets"]:
        _call(ec2.delete_subnet, SubnetId=sn["SubnetId"], ignore=("InvalidSubnetID.NotFound",))
        removed.append(sn["SubnetId"])
    for subnet in store.get_subnets(nid):
        store.update_subnet(nid, subnet["key"], state="deleted")

    for vpc_id in vpc_ids:
        _call(ec2.delete_vpc, VpcId=vpc_id, ignore=("InvalidVpcID.NotFound",))
        removed.append(vpc_id)
    return removed


def step_teardown(network: dict) -> None:
    removed = _teardown(network)
    store.add_event(network["id"], "resources_deleted", f"Deleted {len(removed)} resource(s): {', '.join(removed) or 'none'}")


def step_retire(network: dict) -> None:
    store.transition(network["id"], ("DECOMMISSIONING",), "DELETED", deleted_at=store.now_iso())
    store.add_event(network["id"], "deleted", "Network decommissioned")


def step_rollback(network: dict) -> None:
    """Best-effort cleanup after a failed create - never raises, so the
    failure we report is the original one, not a secondary cleanup error."""
    try:
        removed = _teardown(network)
        store.add_event(network["id"], "rolled_back", f"Rolled back {len(removed)} resource(s)")
    except Exception as exc:  # noqa: BLE001
        log.exception("rollback incomplete")
        store.add_event(network["id"], "rollback_incomplete", f"Rollback incomplete: {exc}. Retry with DELETE.")


def step_fail(network: dict, error: dict | None) -> None:
    error = error or {}
    cause = error.get("Cause", "")
    with contextlib.suppress(ValueError, AttributeError):  # Lambda errors arrive as JSON in Cause
        cause = json.loads(cause).get("errorMessage", cause)
    detail = {"code": error.get("Error", "Unknown"), "message": str(cause)[:1000]}
    store.update_network(network["id"], status="FAILED", error=detail)
    store.add_event(network["id"], "failed", f"{detail['code']}: {detail['message']}")


STEPS = {
    "begin": step_begin,
    "vpc": step_vpc,
    "subnets": step_subnets,
    "routing": step_routing,
    "finalize": step_finalize,
    "rollback": step_rollback,
    "teardown": step_teardown,
    "retire": step_retire,
}


def handler(event: dict, _context: object = None) -> dict:
    step, nid = event["step"], event["network_id"]
    log.info(json.dumps({"msg": "step.start", "step": step, "network_id": nid}))
    network = store.get_network_meta(nid)
    if step == "fail":
        step_fail(network, event.get("error"))
    elif step in STEPS:
        STEPS[step](network)
    else:
        raise StepFailed(f"Unknown step {step!r}")
    log.info(json.dumps({"msg": "step.done", "step": step, "network_id": nid}))
    return {"network_id": nid, "step": step, "ok": True}
