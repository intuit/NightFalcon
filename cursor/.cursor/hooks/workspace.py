#!/usr/bin/env python3
"""Resolve active NightFalcon workspace from a Cursor hook event."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping


def _active(path: Path) -> bool:
    return (path / ".nightfalcon-review").is_file() and (path / "state.json").is_file()


def _resolved(value: object) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(os.path.realpath(os.path.expanduser(value)))


def _contains(root: Path, child: Path) -> bool:
    try:
        child.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_workspace(
    payload: Mapping[str, object], environ: Mapping[str, str] | None = None
) -> Path | None:
    """Return nearest active workspace, bounded by client workspace root when known."""
    env = os.environ if environ is None else environ
    configured = _resolved(env.get("SECURITY_REVIEW_WORKSPACE"))
    if configured and _active(configured):
        return configured

    cwd = _resolved(payload.get("cwd")) or Path(os.path.realpath(os.getcwd()))
    roots: list[Path] = []
    root = _resolved(payload.get("workspace_root"))
    if root is not None:
        roots.append(root)
    raw_roots = payload.get("workspace_roots")
    if isinstance(raw_roots, list):
        roots.extend(item for value in raw_roots if (item := _resolved(value)) is not None)

    containing = [item for item in roots if _contains(item, cwd)]
    bound = max(containing, key=lambda path: len(path.parts), default=None)
    current = cwd if cwd.is_dir() else cwd.parent
    while True:
        if _active(current):
            return current
        if current == bound or current.parent == current:
            break
        current = current.parent

    for item in roots:
        if _active(item):
            return item
    return None


def main() -> int:
    try:
        payload = json.load(os.sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    workspace = resolve_workspace(payload)
    if workspace is not None:
        print(workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
