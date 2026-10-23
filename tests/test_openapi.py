"""The OpenAPI document is a contract: it must match the code and the infrastructure."""

import json
import re
import subprocess
import sys
from pathlib import Path

from openapi_spec_validator import validate

from netloom import api, docs

ROOT = Path(__file__).resolve().parents[1]
SPEC = docs.openapi_document()
METHODS = {"get", "post", "put", "patch", "delete"}


def operations():
    for path, item in SPEC["paths"].items():
        for method, op in item.items():
            if method in METHODS:
                yield path, method, op


def spec_routes() -> set[str]:
    return {f"{method.upper()} {path}" for path, method, _ in operations()}


def terraform_routes() -> dict[str, bool]:
    text = (ROOT / "infra" / "modules" / "netloom" / "api.tf").read_text()
    return {k: v == "true" for k, v in re.findall(r'"((?:GET|POST|DELETE) [^"]+)"\s*=\s*(true|false)', text)}


def test_spec_is_valid_openapi():
    validate(SPEC)


def test_bundled_json_is_in_sync_with_yaml():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_openapi.py"), "--check"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_spec_code_and_terraform_agree_on_routes():
    assert spec_routes() == set(api.ROUTES) == set(terraform_routes())


def test_public_routes_agree_everywhere():
    public_in_code = {k for k, (_, needs_identity) in api.ROUTES.items() if not needs_identity}
    public_in_tf = {k for k, needs_jwt in terraform_routes().items() if not needs_jwt}
    public_in_spec = {f"{m.upper()} {p}" for p, m, op in operations() if op.get("security") == []}
    assert public_in_code == public_in_tf == public_in_spec == {"GET /v1/health", "GET /v1/openapi.json", "GET /docs"}


def test_every_operation_is_documented_for_swagger():
    for path, method, op in operations():
        assert op.get("operationId") and op.get("tags") and op.get("summary"), f"{method} {path}"


def test_openapi_endpoint(aws):
    resp = api.handler({"routeKey": "GET /v1/openapi.json", "requestContext": {}})
    assert resp["statusCode"] == 200 and resp["headers"]["content-type"] == "application/json"
    served = json.loads(resp["body"])
    assert served["servers"] == [{"url": "/", "description": "This deployment"}]
    assert served["openapi"].startswith("3.1")


def test_swagger_ui_page(aws):
    resp = api.handler({"routeKey": "GET /docs", "requestContext": {}})
    assert resp["statusCode"] == 200 and resp["headers"]["content-type"].startswith("text/html")
    assert "SwaggerUIBundle" in resp["body"] and '"/v1/openapi.json"' in resp["body"]
    assert f"swagger-ui-dist@{docs.SWAGGER_UI_VERSION}" in resp["body"]
