"""Lambda entry point behind API Gateway (HTTP API, JWT authorizer).

Authentication is enforced by API Gateway before this code runs: every route
except ``GET /v1/health`` requires a valid Cognito access token. Authorization
is deliberately open - any authenticated user may create, read and delete any
network - but every write records *who* did it in the audit trail.

Dispatch is done on ``routeKey`` which API Gateway resolves for us, so the
routing table below mirrors the routes declared in Terraform one-to-one.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from functools import lru_cache

import boto3
from botocore.exceptions import ClientError

from . import __version__, docs, store
from .errors import BadRequest, NetloomError, NotFound, ValidationFailed
from .ids import is_network_id, new_network_id
from .planner import build_plan
from .web import actor, header, json_body, log, path_param, query, respond, respond_text

_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,64}$")
STATUSES = {"QUEUED", "PROVISIONING", "AVAILABLE", "FAILED", "DECOMMISSIONING", "DELETED"}


@lru_cache(maxsize=1)
def _sfn():
    return boto3.client("stepfunctions")


def _max_subnets() -> int:
    return int(os.environ.get("MAX_SUBNETS", "16"))


def _region() -> str:
    return os.environ.get("AWS_REGION", "us-east-1")


def _base() -> str:
    return "/v1/networks"


def _present(network: dict) -> dict:
    nid = network["id"]
    network.pop("needs_internet_gateway", None)
    network["links"] = {
        "self": f"{_base()}/{nid}",
        "subnets": f"{_base()}/{nid}/subnets",
        "events": f"{_base()}/{nid}/events",
    }
    return network


def _network_id(event: dict) -> str:
    nid = path_param(event, "id")
    if not is_network_id(nid):
        raise NotFound(f"Network {nid!r} does not exist.")
    return nid


def _start_workflow(network_id: str, operation: str, suffix: str) -> str:
    resp = _sfn().start_execution(
        stateMachineArn=os.environ["STATE_MACHINE_ARN"],
        name=f"{network_id}-{suffix}"[:80],
        input=json.dumps({"operation": operation, "network_id": network_id}),
    )
    return resp["executionArn"]


# --------------------------------------------------------------------------- handlers


def health(_event: dict, _who: dict | None) -> dict:
    return respond(200, {"status": "ok", "version": __version__})


def openapi_spec(_event: dict, _who: dict | None) -> dict:
    return respond_text(200, json.dumps(docs.openapi_document()), "application/json")


def swagger_ui(_event: dict, _who: dict | None) -> dict:
    return respond_text(200, docs.SWAGGER_HTML, "text/html; charset=utf-8")


def whoami(_event: dict, who: dict) -> dict:
    return respond(200, {"authenticated": True, **who})


def plan_network(event: dict, _who: dict) -> dict:
    plan = build_plan(json_body(event), max_subnets=_max_subnets())
    return respond(200, {"dry_run": True, "plan": plan.to_dict()})


def create_network(event: dict, who: dict) -> dict:
    body = json_body(event)
    plan = build_plan(body, max_subnets=_max_subnets())

    idem_key = header(event, "Idempotency-Key")
    idempotency = None
    if idem_key is not None:
        if not _IDEMPOTENCY_KEY_RE.match(idem_key):
            raise BadRequest("Idempotency-Key must be 8-64 characters of [A-Za-z0-9_.:-].")
        fingerprint = store.request_fingerprint(body)
        previous = store.find_idempotent(who["sub"], idem_key)
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise ValidationFailed("This Idempotency-Key was already used with a different request body.")
            network = _present(store.get_network(previous["network_id"]))
            return respond(202, network, {"location": network["links"]["self"], "idempotent-replayed": "true"})
        idempotency = (idem_key, fingerprint)

    network_id = new_network_id()
    store.create_network(network_id=network_id, plan=plan, region=_region(), actor=who, idempotency=idempotency)
    try:
        execution_arn = _start_workflow(network_id, "create", "create")
        store.update_network(network_id, execution_arn=execution_arn)
    except ClientError as exc:
        log.exception("could not start provisioning workflow")
        store.update_network(network_id, status="FAILED", error={"code": "WorkflowStartFailed", "message": str(exc)})
        store.add_event(network_id, "failed", "Provisioning workflow could not be started")
        raise

    network = _present(store.get_network(network_id))
    log.info(json.dumps({"msg": "network.requested", "network_id": network_id, "by": who["username"]}))
    return respond(202, network, {"location": network["links"]["self"]})


def list_networks(event: dict, _who: dict) -> dict:
    raw_limit = query(event, "limit") or "20"
    if not raw_limit.isdigit() or not 1 <= int(raw_limit) <= 100:
        raise BadRequest("'limit' must be an integer between 1 and 100.")
    status = query(event, "status")
    if status is not None and status.upper() not in STATUSES:
        raise BadRequest(f"'status' must be one of {', '.join(sorted(STATUSES))}.")
    items, cursor = store.list_networks(int(raw_limit), query(event, "cursor"), status.upper() if status else None)
    for item in items:
        _present(item)
    return respond(200, {"items": items, "next_cursor": cursor})


def get_network(event: dict, _who: dict) -> dict:
    return respond(200, _present(store.get_network(_network_id(event))))


def get_subnets(event: dict, _who: dict) -> dict:
    nid = _network_id(event)
    store.get_network_meta(nid)
    return respond(200, {"network_id": nid, "items": store.get_subnets(nid)})


def get_events(event: dict, _who: dict) -> dict:
    nid = _network_id(event)
    return respond(200, {"network_id": nid, "items": store.list_events(nid)})


def delete_network(event: dict, who: dict) -> dict:
    nid = _network_id(event)
    store.transition(nid, ("AVAILABLE", "FAILED"), "DECOMMISSIONING", error=None)
    store.add_event(nid, "decommission_requested", "Teardown requested", who)
    try:
        arn = _start_workflow(nid, "delete", f"delete-{store.now_iso().replace(':', '').replace('.', '')}")
        store.update_network(nid, execution_arn=arn)
    except ClientError:
        store.update_network(nid, status="FAILED", error={"code": "WorkflowStartFailed", "message": "teardown not started"})
        raise
    network = _present(store.get_network(nid))
    return respond(202, network, {"location": network["links"]["self"]})


Handler = Callable[[dict, dict], dict]
ROUTES: dict[str, tuple[Handler, bool]] = {
    # routeKey: (handler, requires_identity)
    "GET /v1/health": (health, False),
    "GET /v1/openapi.json": (openapi_spec, False),
    "GET /docs": (swagger_ui, False),
    "GET /v1/me": (whoami, True),
    "POST /v1/plans": (plan_network, True),
    "POST /v1/networks": (create_network, True),
    "GET /v1/networks": (list_networks, True),
    "GET /v1/networks/{id}": (get_network, True),
    "GET /v1/networks/{id}/subnets": (get_subnets, True),
    "GET /v1/networks/{id}/events": (get_events, True),
    "DELETE /v1/networks/{id}": (delete_network, True),
}


def handler(event: dict, _context: object = None) -> dict:
    route = event.get("routeKey", "")
    entry = ROUTES.get(route)
    if entry is None:
        return respond(404, NotFound(f"No route for {route or 'request'}.").to_body())
    fn, needs_identity = entry
    who = actor(event)
    if needs_identity and who is None:
        # Defence in depth: API Gateway should already have rejected this.
        return respond(401, {"error": {"code": "unauthenticated", "message": "A valid bearer token is required."}})
    try:
        return fn(event, who)
    except NetloomError as exc:
        return respond(exc.status, exc.to_body())
    except Exception:  # noqa: BLE001 - last-resort 500 with no internals leaked
        log.exception("unhandled error on %s", route)
        return respond(500, {"error": {"code": "internal_error", "message": "Unexpected server error."}})
