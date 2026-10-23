"""Interactive API documentation: the OpenAPI document and a Swagger UI page.

Both routes are public - documentation is not a secret - but every operation
you *try* from Swagger UI still needs a valid bearer token (Authorize button).

``openapi.json`` is generated from ``docs/openapi.yaml`` by
``scripts/build_openapi.py`` and shipped inside the Lambda package; a test
fails if the two drift apart.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from . import __version__

SWAGGER_UI_VERSION = "5.17.14"
_CDN = f"https://cdn.jsdelivr.net/npm/swagger-ui-dist@{SWAGGER_UI_VERSION}"


@lru_cache(maxsize=1)
def _spec_text() -> str:
    return (Path(__file__).with_name("openapi.json")).read_text(encoding="utf-8")


def openapi_document() -> dict:
    spec = json.loads(_spec_text())
    spec["info"]["version"] = __version__
    # Relative server: "Try it out" calls whatever host served the spec, so the
    # same artefact works on API Gateway, a custom domain or the local gateway.
    spec["servers"] = [{"url": "/", "description": "This deployment"}]
    return spec


SWAGGER_HTML = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>netloom API</title>
  <link rel="stylesheet" href="{_CDN}/swagger-ui.css">
  <style>
    body {{ margin: 0; background: #fafafa; }}
    .topbar {{ display: none; }}
    .nl-banner {{ font: 14px/1.5 system-ui, sans-serif; padding: 12px 24px; background: #ede9fe; color: #3b0764; }}
    .nl-banner code {{ background: #fff; padding: 1px 6px; border-radius: 4px; }}
  </style>
</head>
<body>
  <div class="nl-banner">
    <strong>netloom</strong> - click <em>Authorize</em> and paste an access token:
    <code>make token EMAIL=you@example.com</code> on AWS, or <code>local-alice</code> on the local gateway.
    Every route except <code>/v1/health</code> and these docs requires it.
  </div>
  <div id="swagger-ui"></div>
  <script src="{_CDN}/swagger-ui-bundle.js" crossorigin></script>
  <script>
    window.ui = SwaggerUIBundle({{
      url: "/v1/openapi.json",
      dom_id: "#swagger-ui",
      deepLinking: true,
      persistAuthorization: true,
      displayRequestDuration: true,
      tryItOutEnabled: true,
      defaultModelsExpandDepth: 0,
      docExpansion: "list"
    }});
  </script>
</body>
</html>
"""
