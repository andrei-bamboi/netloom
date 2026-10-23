#!/usr/bin/env python3
"""A tiny stand-in for API Gateway in front of the LocalStack deployment.

LocalStack's free tier has no API Gateway v2 or Cognito, so this script does
their job for local development. It is not a security boundary.

* It maps METHOD + path to a routeKey, using the same table as the API.
* It invokes the deployed API Lambda inside LocalStack with a payload-v2 event.
* It emulates the JWT authorizer with **dev tokens**: `Authorization: Bearer local-<name>`
  is accepted as user <name>, and anything else on a protected route gets
  401 {"message":"Unauthorized"}, just like API Gateway.

    make local-up local-deploy local-gateway
    open http://localhost:8080/docs   # Swagger UI -> Authorize -> local-alice
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import re
import subprocess
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import boto3
from botocore.config import Config

if sys.version_info < (3, 9):  # noqa: UP036 - keep the gateway usable on older local Pythons
    sys.exit("local_gateway.py needs Python 3.9+")

ROOT = Path(__file__).resolve().parents[1]
SPEC_FILE = ROOT / "src" / "netloom" / "openapi.json"
METHODS = ("get", "post", "put", "patch", "delete")


def load_routes() -> dict:
    """routeKey -> needs_identity, read from the OpenAPI document the Lambda serves.

    tests/test_openapi.py guarantees this matches the Lambda's own route table and
    Terraform, so the gateway needs no import of application code (and works on
    whatever Python the developer's machine has)."""
    spec = json.loads(SPEC_FILE.read_text(encoding="utf-8"))
    return {
        f"{method.upper()} {path}": op.get("security") != []
        for path, item in spec["paths"].items()
        for method, op in item.items()
        if method in METHODS
    }


ROUTES = load_routes()

DEV_TOKEN = re.compile(r"^Bearer local-([A-Za-z0-9._@-]{1,64})$")


def compile_routes() -> list[tuple[str, str, re.Pattern, bool]]:
    """routeKey 'GET /v1/networks/{id}' -> ('GET', routeKey, ^/v1/networks/(?P<id>[^/]+)$, needs_identity)."""
    table = []
    for route_key, needs_identity in ROUTES.items():
        method, path = route_key.split(" ", 1)
        regex = "^" + re.sub(r"\\\{(\w+)\\\}", r"(?P<\1>[^/]+)", re.escape(path)) + "$"
        table.append((method, route_key, re.compile(regex), needs_identity))
    return table


def stack_outputs() -> dict:
    raw = subprocess.run(
        ["terraform", f"-chdir={ROOT / 'infra' / 'localstack'}", "output", "-json"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {k: v["value"] for k, v in json.loads(raw).items()}


class Gateway(BaseHTTPRequestHandler):
    routes = compile_routes()
    lam = None
    function = ""

    def log_message(self, fmt, *args):  # quieter, single-line access log
        sys.stderr.write(f"{self.command:<6} {self.path} -> {args[1] if len(args) > 1 else ''}\n")

    def _send(self, status: int, body: bytes, headers: dict | None = None) -> None:
        self.send_response(status)
        for k, v in (headers or {"content-type": "application/json"}).items():
            self.send_header(k, v)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self) -> None:
        url = urlsplit(self.path)
        if url.path in ("", "/"):
            return self._send(302, b"", {"location": "/docs"})

        match = None
        for method, route_key, pattern, needs_identity in self.routes:
            m = pattern.match(url.path)
            if m and method == self.command:
                match = (route_key, m.groupdict(), needs_identity)
                break
        if match is None:
            return self._send(404, b'{"message":"Not Found"}')
        route_key, path_params, needs_identity = match

        claims = None
        if needs_identity:
            token = DEV_TOKEN.match(self.headers.get("authorization", ""))
            if not token:
                unauthorized = {"content-type": "application/json", "www-authenticate": "Bearer"}
                return self._send(401, b'{"message":"Unauthorized"}', unauthorized)
            user = token.group(1)
            claims = {"sub": f"local-{user}", "username": user, "client_id": "local-gateway"}

        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length else None
        event = {
            "version": "2.0",
            "routeKey": route_key,
            "rawPath": url.path,
            "rawQueryString": url.query,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "queryStringParameters": dict(parse_qsl(url.query)) or None,
            "pathParameters": path_params or None,
            "body": body,
            "isBase64Encoded": False,
            "requestContext": {
                "requestId": str(uuid.uuid4()),
                "domainName": self.headers.get("host", "localhost"),
                "http": {"method": self.command, "path": url.path},
                **({"authorizer": {"jwt": {"claims": claims, "scopes": None}}} if claims else {}),
            },
        }
        resp = self.lam.invoke(FunctionName=self.function, Payload=json.dumps(event).encode())
        payload = json.loads(resp["Payload"].read() or b"{}")
        if resp.get("FunctionError"):
            return self._send(502, json.dumps({"message": "Internal Server Error", "lambda": payload}).encode())
        out = payload.get("body") or ""
        raw = base64.b64decode(out) if payload.get("isBase64Encoded") else out.encode()
        self._send(payload.get("statusCode", 200), raw, payload.get("headers") or {"content-type": "application/json"})

    do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = _handle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    out = stack_outputs()
    Gateway.function = out["api_function_name"]
    Gateway.lam = boto3.client(
        "lambda",
        endpoint_url=out["endpoint"],
        region_name=out["region"],
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(read_timeout=120, retries={"max_attempts": 1}),
    )
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Gateway)
    print(f"netloom local gateway -> {out['api_function_name']} @ {out['endpoint']}")
    print(f"  Swagger UI : http://localhost:{args.port}/docs")
    print("  dev token  : Authorization: Bearer local-alice")
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()
