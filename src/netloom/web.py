"""Thin helpers around the API Gateway HTTP API (payload v2.0) event format."""

from __future__ import annotations

import base64
import json
import logging
import os

from .errors import BadRequest

log = logging.getLogger("netloom")
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

MAX_BODY_BYTES = 32 * 1024


def respond(status: int, body: object | None = None, headers: dict | None = None) -> dict:
    out = {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store", **(headers or {})},
    }
    if body is not None:
        out["body"] = json.dumps(body, separators=(",", ":"), default=str)
    return out


def json_body(event: dict) -> object:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8", errors="replace")
    if len(raw.encode()) > MAX_BODY_BYTES:
        raise BadRequest(f"Request body exceeds {MAX_BODY_BYTES} bytes.")
    if not raw.strip():
        raise BadRequest("A JSON request body is required.")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BadRequest(f"Request body is not valid JSON: {exc.msg} (line {exc.lineno}).") from exc


def header(event: dict, name: str) -> str | None:
    headers = event.get("headers") or {}
    return headers.get(name.lower()) or headers.get(name)


def query(event: dict, name: str) -> str | None:
    return (event.get("queryStringParameters") or {}).get(name)


def path_param(event: dict, name: str) -> str:
    return (event.get("pathParameters") or {}).get(name, "")


def claims(event: dict) -> dict:
    return ((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt", {}).get("claims") or {}


def actor(event: dict) -> dict | None:
    """Who is calling, derived from the JWT verified by API Gateway."""
    c = claims(event)
    sub = c.get("sub")
    if not sub:
        return None
    return {
        "sub": sub,
        "username": c.get("username") or c.get("cognito:username") or c.get("email") or sub,
        "client_id": c.get("client_id") or c.get("aud"),
    }
