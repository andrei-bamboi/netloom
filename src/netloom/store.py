"""DynamoDB persistence (single-table design).

Item layout (pk / sk):

    NET#<id>        META                 the network record (status, AWS ids, owner ...)
    NET#<id>        SUB#<key>            one item per subnet
    NET#<id>        EVT#<iso-ts>#<rnd>   append-only audit trail
    IDEM#<sub>#<k>  IDEM                 idempotency-key reservation (TTL'd)

GSI ``by_kind`` (kind / id) lists every network newest-first. Network ids are
time-ordered, so the id doubles as the sort key.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import secrets
import time
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from .errors import BadRequest, Conflict, NotFound
from .planner import NetworkPlan

IDEMPOTENCY_TTL_SECONDS = 24 * 3600
LIST_INDEX = "by_kind"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@lru_cache(maxsize=1)
def _table():
    return boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])


def reset_cache() -> None:
    """Test hook - drop cached clients after environment changes."""
    _table.cache_clear()


def _plain(value):
    """Convert DynamoDB Decimals back into ints/floats for JSON output."""
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


_INTERNAL = {"pk", "sk", "kind", "entity"}


def _strip(item: dict) -> dict:
    return {k: v for k, v in _plain(item).items() if k not in _INTERNAL}


def request_fingerprint(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# --------------------------------------------------------------------------- writes


def create_network(
    *,
    network_id: str,
    plan: NetworkPlan,
    region: str,
    actor: dict,
    idempotency: tuple[str, str] | None = None,
) -> None:
    """Atomically persist the network, its subnets, the first audit event and
    (optionally) the idempotency reservation."""
    ts = now_iso()
    pk = f"NET#{network_id}"
    items: list[dict] = [
        {
            "Put": {
                "TableName": _table().name,
                "Item": {
                    "pk": pk,
                    "sk": "META",
                    "entity": "network",
                    "kind": "network",
                    "id": network_id,
                    "name": plan.name,
                    "cidr": plan.cidr,
                    "region": region,
                    "tags": plan.tags,
                    "status": "QUEUED",
                    "needs_internet_gateway": plan.needs_internet_gateway,
                    "created_by": actor,
                    "created_at": ts,
                    "updated_at": ts,
                },
                "ConditionExpression": "attribute_not_exists(pk)",
            }
        }
    ]
    for s in plan.subnets:
        items.append(
            {
                "Put": {
                    "TableName": _table().name,
                    "Item": {
                        "pk": pk,
                        "sk": f"SUB#{s.key}",
                        "entity": "subnet",
                        "key": s.key,
                        "name": s.name,
                        "cidr": s.cidr,
                        "zone": s.zone,
                        "tier": s.tier,
                        "usable_ips": s.usable_ips,
                        "state": "planned",
                    },
                }
            }
        )
    items.append({"Put": {"TableName": _table().name, "Item": _event_item(network_id, "requested", "Network requested", actor)}})
    if idempotency:
        key, fingerprint = idempotency
        items.append(
            {
                "Put": {
                    "TableName": _table().name,
                    "Item": {
                        "pk": f"IDEM#{actor['sub']}#{key}",
                        "sk": "IDEM",
                        "entity": "idempotency",
                        "network_id": network_id,
                        "fingerprint": fingerprint,
                        "expires_at": int(time.time()) + IDEMPOTENCY_TTL_SECONDS,
                    },
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            }
        )
    try:
        _table().meta.client.transact_write_items(TransactItems=items)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "TransactionCanceledException":
            raise Conflict("A request with this Idempotency-Key is already being processed.") from exc
        raise


def _event_item(network_id: str, event_type: str, message: str, actor: dict | None) -> dict:
    ts = now_iso()
    return {
        "pk": f"NET#{network_id}",
        "sk": f"EVT#{ts}#{secrets.token_hex(3)}",
        "entity": "event",
        "at": ts,
        "type": event_type,
        "message": message,
        "actor": actor or {"sub": "system", "username": "netloom-workflow"},
    }


def add_event(network_id: str, event_type: str, message: str, actor: dict | None = None) -> None:
    _table().put_item(Item=_event_item(network_id, event_type, message, actor))


def update_network(network_id: str, **fields) -> None:
    _update(f"NET#{network_id}", "META", fields)


def update_subnet(network_id: str, key: str, **fields) -> None:
    _update(f"NET#{network_id}", f"SUB#{key}", fields)


def _update(pk: str, sk: str, fields: dict, condition=None) -> None:
    fields = {**fields}
    if sk == "META":
        fields["updated_at"] = now_iso()
    sets, removes, names, values = [], [], {}, {}
    for i, (k, v) in enumerate(fields.items()):
        names[f"#a{i}"] = k
        if v is None:
            removes.append(f"#a{i}")
        else:
            sets.append(f"#a{i} = :a{i}")
            values[f":a{i}"] = v
    expr = ""
    if sets:
        expr += "SET " + ", ".join(sets)
    if removes:
        expr += " REMOVE " + ", ".join(removes)
    kwargs = {
        "Key": {"pk": pk, "sk": sk},
        "UpdateExpression": expr.strip(),
        "ExpressionAttributeNames": names,
        "ConditionExpression": Attr("pk").exists() & condition if condition is not None else Attr("pk").exists(),
    }
    if values:
        kwargs["ExpressionAttributeValues"] = values
    _table().update_item(**kwargs)


def transition(network_id: str, allowed_from: tuple[str, ...], to: str, **fields) -> None:
    """Move a network to a new status only if it is currently in ``allowed_from``.

    This optimistic guard prevents e.g. two concurrent DELETE calls from both
    starting a teardown."""
    try:
        _update(f"NET#{network_id}", "META", {"status": to, **fields}, Attr("status").is_in(list(allowed_from)))
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            current = get_network_meta(network_id)
            raise Conflict(f"Network is {current['status']}; this operation needs one of {', '.join(allowed_from)}.") from exc
        raise


# --------------------------------------------------------------------------- reads


def find_idempotent(sub: str, key: str) -> dict | None:
    item = _table().get_item(Key={"pk": f"IDEM#{sub}#{key}", "sk": "IDEM"}).get("Item")
    if not item or int(item.get("expires_at", 0)) < time.time():
        return None
    return _plain(item)


def get_network_meta(network_id: str) -> dict:
    item = _table().get_item(Key={"pk": f"NET#{network_id}", "sk": "META"}, ConsistentRead=True).get("Item")
    if not item:
        raise NotFound(f"Network {network_id} does not exist.")
    return _strip(item)


def get_subnets(network_id: str) -> list[dict]:
    resp = _table().query(
        KeyConditionExpression=Key("pk").eq(f"NET#{network_id}") & Key("sk").begins_with("SUB#"),
        ConsistentRead=True,
    )
    return [_strip(i) for i in resp["Items"]]


def get_network(network_id: str) -> dict:
    network = get_network_meta(network_id)
    network["subnets"] = get_subnets(network_id)
    return network


def list_events(network_id: str) -> list[dict]:
    get_network_meta(network_id)  # 404 if unknown
    resp = _table().query(
        KeyConditionExpression=Key("pk").eq(f"NET#{network_id}") & Key("sk").begins_with("EVT#"),
    )
    return [_strip(i) for i in resp["Items"]]


def _encode_cursor(key: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(_plain(key)).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> dict:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        key = json.loads(base64.urlsafe_b64decode(padded))
        if not isinstance(key, dict) or set(key) != {"pk", "sk", "kind", "id"}:
            raise ValueError
        return key
    except (ValueError, binascii.Error, json.JSONDecodeError) as exc:
        raise BadRequest("The 'cursor' parameter is malformed.") from exc


def list_networks(limit: int, cursor: str | None, status: str | None) -> tuple[list[dict], str | None]:
    kwargs: dict = {
        "IndexName": LIST_INDEX,
        "KeyConditionExpression": Key("kind").eq("network"),
        "ScanIndexForward": False,
        "Limit": limit,
    }
    if status:
        kwargs["FilterExpression"] = Attr("status").eq(status)
    if cursor:
        kwargs["ExclusiveStartKey"] = _decode_cursor(cursor)
    resp = _table().query(**kwargs)
    nxt = resp.get("LastEvaluatedKey")
    return [_strip(i) for i in resp["Items"]], (_encode_cursor(nxt) if nxt else None)
