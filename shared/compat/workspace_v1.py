#!/usr/bin/env python3
"""Read public NightFalcon workspace v1/v2 state without mutating input."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path


def normalize(data: dict) -> dict:
    result = copy.deepcopy(data)
    version = result.get("schema_version", 1)
    if version in ("2", 2):
        if "org_scope" in result:
            raise ValueError("mixed v1/v2 workspace schema")
        result["schema_version"] = 2
        return result
    if version not in ("1", 1):
        raise ValueError(f"unsupported workspace schema_version: {version!r}")
    if "multitenant_scope" in result and "org_scope" in result:
        raise ValueError("mixed v1/v2 workspace schema")
    scope = result.pop("org_scope", False)
    if not isinstance(scope, bool):
        raise ValueError("v1 org_scope must be boolean")
    result["multitenant_scope"] = scope
    result["schema_version"] = 2
    result["compatibility_source"] = "public-workspace-v1"
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding="utf-8"))
    print(json.dumps(normalize(data), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
