import boto3

from conftest import call, http_event, run_workflow

LAYOUT_BODY = {"name": "demo", "cidr": "10.42.0.0/16", "layout": {"tiers": ["public", "private"], "zones": 2, "prefix": 24}}


def test_health_is_public(aws):
    status, body, _ = call("GET /v1/health", user=None)
    assert status == 200 and body["status"] == "ok"


def test_missing_identity_is_rejected(aws):
    status, body, _ = call("GET /v1/networks", user=None)
    assert status == 401 and body["error"]["code"] == "unauthenticated"


def test_whoami(aws):
    status, body, _ = call("GET /v1/me", user="bob")
    assert status == 200 and body["username"] == "bob"


def test_unknown_route(aws):
    assert call("PATCH /v1/networks/{id}")[0] == 404


def test_plan_is_dry_run(aws):
    status, body, _ = call("POST /v1/plans", body=LAYOUT_BODY)
    assert status == 200 and body["dry_run"] is True
    assert body["plan"]["summary"]["subnet_count"] == 4
    assert call("GET /v1/networks")[1]["items"] == []
    assert boto3.client("ec2").describe_vpcs(Filters=[{"Name": "cidr", "Values": ["10.42.0.0/16"]}])["Vpcs"] == []


def test_create_returns_202_and_queues_workflow(aws):
    status, body, headers = call("POST /v1/networks", body=LAYOUT_BODY)
    assert status == 202
    assert body["status"] == "QUEUED"
    assert headers["location"] == f"/v1/networks/{body['id']}"
    assert body["created_by"]["username"] == "alice"
    assert len(body["subnets"]) == 4 and all(s["state"] == "planned" for s in body["subnets"])
    assert body["execution_arn"].startswith("arn:aws:states:")


def test_full_lifecycle_against_ec2(aws):
    _, created, _ = call("POST /v1/networks", body=LAYOUT_BODY)
    nid = created["id"]
    run_workflow(nid)

    status, net, _ = call("GET /v1/networks/{id}", path={"id": nid}, user="carol")  # any user may read
    assert status == 200 and net["status"] == "AVAILABLE"
    assert net["vpc_id"].startswith("vpc-") and net["internet_gateway_id"].startswith("igw-")

    ec2 = boto3.client("ec2")
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [net["vpc_id"]]}])["Subnets"]
    assert sorted(s["CidrBlock"] for s in subnets) == ["10.42.0.0/24", "10.42.1.0/24", "10.42.2.0/24", "10.42.3.0/24"]
    assert len({s["AvailabilityZone"] for s in subnets}) == 2
    public = [s for s in subnets if s["MapPublicIpOnLaunch"]]
    assert len(public) == 2

    rt = ec2.describe_route_tables(RouteTableIds=[net["public_route_table_id"]])["RouteTables"][0]
    assert {a["SubnetId"] for a in rt["Associations"]} == {s["SubnetId"] for s in public}
    assert any(r.get("GatewayId") == net["internet_gateway_id"] for r in rt["Routes"])

    status, sub, _ = call("GET /v1/networks/{id}/subnets", path={"id": nid})
    assert status == 200 and all(s["subnet_id"].startswith("subnet-") for s in sub["items"])

    status, events, _ = call("GET /v1/networks/{id}/events", path={"id": nid})
    assert [e["type"] for e in events["items"]][:1] == ["requested"]
    assert "available" in [e["type"] for e in events["items"]]

    # Delete by a different authenticated user - authorization is open.
    status, deleting, _ = call("DELETE /v1/networks/{id}", path={"id": nid}, user="dave")
    assert status == 202 and deleting["status"] == "DECOMMISSIONING"
    assert call("DELETE /v1/networks/{id}", path={"id": nid})[0] == 409  # already in progress
    run_workflow(nid, "delete")

    _, gone, _ = call("GET /v1/networks/{id}", path={"id": nid})
    assert gone["status"] == "DELETED" and all(s["state"] == "deleted" for s in gone["subnets"])
    assert ec2.describe_vpcs(Filters=[{"Name": "tag:netloom:network-id", "Values": [nid]}])["Vpcs"] == []


def test_private_only_network_has_no_gateway(aws):
    body = {"name": "private", "cidr": "192.168.0.0/22", "subnets": [{"cidr": "192.168.0.0/24"}, {"cidr": "192.168.1.0/24"}]}
    _, created, _ = call("POST /v1/networks", body=body)
    run_workflow(created["id"])
    _, net, _ = call("GET /v1/networks/{id}", path={"id": created["id"]})
    assert net["status"] == "AVAILABLE" and "internet_gateway_id" not in net


def test_validation_error_shape(aws):
    status, body, _ = call("POST /v1/networks", body={"name": "x", "cidr": "8.8.8.0/24", "layout": {}})
    assert status == 422 and body["error"]["code"] == "validation_failed"
    assert body["error"]["details"][0]["field"] == "cidr"


def test_malformed_json(aws):
    status, body, _ = call("POST /v1/networks", body="{nope")
    assert status == 400 and "not valid JSON" in body["error"]["message"]


def test_idempotency_key_replays(aws):
    headers = {"idempotency-key": "demo-create-0001"}
    s1, first, _ = call("POST /v1/networks", body=LAYOUT_BODY, headers=headers)
    s2, second, h2 = call("POST /v1/networks", body=LAYOUT_BODY, headers=headers)
    assert s1 == s2 == 202 and first["id"] == second["id"] and h2["idempotent-replayed"] == "true"
    assert len(call("GET /v1/networks")[1]["items"]) == 1

    other = {**LAYOUT_BODY, "name": "different"}
    assert call("POST /v1/networks", body=other, headers=headers)[0] == 422
    # Keys are scoped per user.
    assert call("POST /v1/networks", body=LAYOUT_BODY, headers=headers, user="zoe")[1]["id"] != first["id"]


def test_list_pagination_and_filter(aws):
    ids = [call("POST /v1/networks", body={**LAYOUT_BODY, "name": f"n{i}"})[1]["id"] for i in range(3)]
    run_workflow(ids[0])

    status, page1, _ = call("GET /v1/networks", query={"limit": "2"})
    assert status == 200 and [n["id"] for n in page1["items"]] == ids[::-1][:2]  # newest first
    _, page2, _ = call("GET /v1/networks", query={"limit": "2", "cursor": page1["next_cursor"]})
    assert [n["id"] for n in page2["items"]] == [ids[0]]

    _, available, _ = call("GET /v1/networks", query={"status": "available"})
    assert [n["id"] for n in available["items"]] == [ids[0]]

    assert call("GET /v1/networks", query={"limit": "0"})[0] == 400
    assert call("GET /v1/networks", query={"cursor": "garbage"})[0] == 400
    assert call("GET /v1/networks", query={"status": "nope"})[0] == 400


def test_unknown_network(aws):
    assert call("GET /v1/networks/{id}", path={"id": "net-0000000000000000000000"})[0] == 404
    assert call("GET /v1/networks/{id}", path={"id": "../etc"})[0] == 404


def test_cannot_delete_while_provisioning(aws):
    _, created, _ = call("POST /v1/networks", body=LAYOUT_BODY)
    status, body, _ = call("DELETE /v1/networks/{id}", path={"id": created["id"]})
    assert status == 409 and "QUEUED" in body["error"]["message"]


def test_base64_body(aws):
    import base64
    import json

    from netloom import api

    event = http_event("POST /v1/plans", body=base64.b64encode(json.dumps(LAYOUT_BODY).encode()).decode())
    event["isBase64Encoded"] = True
    assert api.handler(event)["statusCode"] == 200
