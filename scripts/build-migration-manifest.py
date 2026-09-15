#!/usr/bin/env python3
"""Create reproducible release and excluded-source commitments without path disclosure."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import defaultdict
from pathlib import Path


PORTS = ("claude", "codex", "cursor")
SOURCE_REVISION = "0c3183e02ec1f1500115233402cabdf7808ec4e6"
REQUIRED_EXCLUSION_CATEGORIES = {
    "private_context", "internal_work_products", "generated_evidence",
}
RELEASE_EXCLUSIONS = {
    "migration-manifest.json",
    "codex/verification-results.json",
    "codex/docs/verification-report.md",
}
GENERATED_DIRECTORY_EXCLUSIONS = {"__pycache__", ".terragraph"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_record(relative: str, file_digest: str) -> bytes:
    return relative.encode() + b"\0" + file_digest.encode() + b"\n"


def eligible_release_files(root: Path) -> list[Path]:
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        if (
            relative in RELEASE_EXCLUSIONS
            or GENERATED_DIRECTORY_EXCLUSIONS.intersection(path.parts)
            or path.suffix == ".pyc"
        ):
            continue
        files.append(path)
    return files


def release_digests(root: Path) -> tuple[int, str, dict[str, str]]:
    aggregate = hashlib.sha256()
    port_digests = {port: hashlib.sha256() for port in PORTS}
    files = eligible_release_files(root)
    for path in files:
        relative = path.relative_to(root).as_posix()
        record = canonical_record(relative, digest(path))
        aggregate.update(record)
        top_level = relative.split("/", 1)[0]
        if top_level in port_digests:
            port_digests[top_level].update(record)
    return len(files), aggregate.hexdigest(), {
        port: value.hexdigest() for port, value in port_digests.items()
    }


def confined(path: Path, source: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(source)
    return resolved


def commitment_for_roots(roots: list[Path], source: Path) -> dict:
    result = hashlib.sha256()
    count = 0
    for index, root in enumerate(roots):
        root = confined(root, source)
        candidates = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in candidates:
            if path.is_symlink():
                raise ValueError("excluded commitment refuses symbolic links")
            if not path.is_file():
                continue
            relative = "." if root.is_file() else path.relative_to(root).as_posix()
            result.update(canonical_record(f"{index}/{relative}", digest(path)))
            count += 1
    return {"file_count": count, "sha256": result.hexdigest()}


def cache_commitment(source: Path) -> dict:
    result = hashlib.sha256()
    count = 0
    for path in sorted(source.rglob("*")):
        if not path.is_file() or not ("__pycache__" in path.parts or path.suffix == ".pyc"):
            continue
        result.update(canonical_record(path.relative_to(source).as_posix(), digest(path)))
        count += 1
    return {"file_count": count, "sha256": result.hexdigest()}


def verify_source(source: Path) -> None:
    revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != SOURCE_REVISION:
        raise ValueError(f"source revision {revision} does not match completed source {SOURCE_REVISION}")
    status = subprocess.check_output(
        ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"], text=True
    )
    if status.strip():
        raise ValueError("source checkout must be clean before migration attestation")


def build(root: Path, source: Path, excluded_roots: dict[str, list[Path]]) -> dict:
    verify_source(source)
    missing = REQUIRED_EXCLUSION_CATEGORIES - excluded_roots.keys()
    if missing:
        raise ValueError(f"missing exclusion categories: {', '.join(sorted(missing))}")
    release_count, release_sha, port_digests = release_digests(root)
    category_commitments = {
        category: commitment_for_roots(paths, source)
        for category, paths in sorted(excluded_roots.items())
    }
    exclusions = {
        "policy": "exclude organization-private context, internal work products, generated source evidence, and caches",
        "category_commitments": category_commitments,
        "cache_artifacts": cache_commitment(source),
    }
    excluded_commitment = hashlib.sha256(
        json.dumps(exclusions, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema_version": "2",
        "source_commit": SOURCE_REVISION,
        "ports": list(PORTS),
        "release_file_count": release_count,
        "release_sha256": release_sha,
        "release_scope_exclusions": sorted(RELEASE_EXCLUSIONS),
        "port_sha256": port_digests,
        "exclusions": exclusions,
        "excluded_commitment_sha256": excluded_commitment,
        "migration_properties": {
            "private_context_removed": True,
            "organization_context_provider": "optional-public-schema-v2",
            "legacy_workspace_adapter": "read-only-non-mutating",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument(
        "--excluded-root", action="append", default=[], metavar="CATEGORY=PATH",
        help="Source path committed under a category without disclosing path names in output.",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root, source = args.root.resolve(), args.source.resolve()
    if not root.is_dir() or not source.is_dir():
        parser.error("root and source must be directories")
    excluded_roots: dict[str, list[Path]] = defaultdict(list)
    for value in args.excluded_root:
        category, separator, raw_path = value.partition("=")
        if not separator or not category or not raw_path:
            parser.error("--excluded-root must use CATEGORY=PATH")
        excluded_roots[category].append(Path(raw_path))
    output = args.output or root / "migration-manifest.json"
    try:
        payload = build(root, source, excluded_roots)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
