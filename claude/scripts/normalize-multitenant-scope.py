#!/usr/bin/env python3
"""Convert phase-0 multitenant-scope run-log tokens to JSON booleans."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys


class ScopeNormalizationError(ValueError):
    """Raised when run-log scope provenance is missing or ambiguous."""


def normalize_multitenant_scope(run_log: pathlib.Path, slug: str) -> bool:
    if re.fullmatch(r"[A-Za-z0-9._-]+", slug) is None:
        raise ScopeNormalizationError("slug contains unsupported characters")
    try:
        text = run_log.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScopeNormalizationError(f"cannot read run log: {exc}") from exc

    pattern = re.compile(
        rf"^\[[^\]\r\n]+\]\s+{re.escape(slug)}:\s+"
        r"multitenant-scope=(YES|NO)(?:\s+(?:—|-)\s+.*)?$"
    )
    tokens = {
        match.group(1)
        for line in text.splitlines()
        if (match := pattern.fullmatch(line)) is not None
    }
    if not tokens:
        raise ScopeNormalizationError(
            f"run log has no multitenant-scope=YES | NO entry for {slug}"
        )
    if len(tokens) != 1:
        raise ScopeNormalizationError(
            f"run log has conflicting multitenant-scope entries for {slug}"
        )
    return next(iter(tokens)) == "YES"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-log", required=True, type=pathlib.Path)
    parser.add_argument("--slug", required=True)
    args = parser.parse_args(argv)
    try:
        normalized = normalize_multitenant_scope(args.run_log, args.slug)
    except ScopeNormalizationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(normalized))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
