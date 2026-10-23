#!/usr/bin/env python3
"""Compile docs/openapi.yaml (human-edited) into src/netloom/openapi.json (shipped in the Lambda).

The Lambda runtime has no YAML parser, so the API serves a pre-built JSON copy.
`make openapi` regenerates it; tests/test_openapi.py fails if it is stale.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "openapi.yaml"
TARGET = ROOT / "src" / "netloom" / "openapi.json"


def render() -> str:
    return json.dumps(yaml.safe_load(SOURCE.read_text(encoding="utf-8")), indent=1, ensure_ascii=False) + "\n"


def main() -> None:
    content = render()
    if "--check" in sys.argv:
        if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != content:
            sys.exit(f"{TARGET.relative_to(ROOT)} is stale - run `make openapi`.")
        print("openapi.json is up to date")
        return
    TARGET.write_text(content, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
