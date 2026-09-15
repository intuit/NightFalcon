#!/usr/bin/env python3
"""Build or validate source-to-public Codex distribution provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import subprocess


WRAPPER = pathlib.Path(__file__).resolve().parents[1]
PLUGIN = WRAPPER / "plugins" / "nightfalcon"
PUBLIC_SOURCE_REVISION = "0c3183e02ec1f1500115233402cabdf7808ec4e6"
CLASSIFICATIONS = {"UNCHANGED", "ADAPTED", "PUBLIC_ADDITION"}


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def public_files() -> list[pathlib.Path]:
    return [
        path for path in sorted(PLUGIN.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    ]


def source_checkout(source_root: pathlib.Path) -> tuple[pathlib.Path, str]:
    source_root = source_root.resolve(strict=True)
    repository = pathlib.Path(subprocess.check_output(
        ["git", "-C", str(source_root), "rev-parse", "--show-toplevel"], text=True
    ).strip())
    revision = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != PUBLIC_SOURCE_REVISION:
        raise RuntimeError(
            f"source checkout revision {revision} does not match completed source {PUBLIC_SOURCE_REVISION}"
        )
    relative_root = source_root.relative_to(repository).as_posix()
    status = subprocess.check_output(
        ["git", "-C", str(repository), "status", "--porcelain", "--untracked-files=all", "--", relative_root],
        text=True,
    )
    if status.strip():
        raise RuntimeError("source plugin is dirty; provenance generation requires committed bytes")
    return repository, relative_root


def tracked_source_files(source_root: pathlib.Path) -> list[pathlib.Path]:
    repository, relative_root = source_checkout(source_root)
    output = subprocess.check_output(
        ["git", "-C", str(repository), "ls-files", "--", relative_root], text=True
    )
    prefix = relative_root + "/"
    return [source_root / item.removeprefix(prefix) for item in sorted(output.splitlines())]


def canonical_record(relative: str, digest: str) -> bytes:
    return relative.encode() + b"\0" + digest.encode() + b"\n"


def create_inventory(source_root: pathlib.Path) -> dict:
    source_paths = tracked_source_files(source_root)
    destination_paths = public_files()
    destination_by_relative = {
        path.relative_to(PLUGIN).as_posix(): path for path in destination_paths
    }
    files: list[dict] = []
    covered_destinations: set[str] = set()
    excluded = hashlib.sha256()
    excluded_count = 0

    for source_path in source_paths:
        relative = source_path.relative_to(source_root).as_posix()
        source_digest = sha256(source_path)
        destination_path = destination_by_relative.get(relative)
        if destination_path is None:
            excluded.update(canonical_record(relative, source_digest))
            excluded_count += 1
            continue
        destination_digest = sha256(destination_path)
        classification = "UNCHANGED" if source_digest == destination_digest else "ADAPTED"
        files.append({
            "source": relative,
            "destination": relative,
            "classification": classification,
            "reason": "Copied byte-for-byte." if classification == "UNCHANGED" else "Adapted for public distribution.",
            "sha256": source_digest,
            "destination_sha256": destination_digest,
        })
        covered_destinations.add(relative)

    for relative, destination_path in destination_by_relative.items():
        if relative in covered_destinations:
            continue
        files.append({
            "source": None,
            "destination": relative,
            "classification": "PUBLIC_ADDITION",
            "reason": "Added by public migration; no private-source file is claimed.",
            "sha256": None,
            "destination_sha256": sha256(destination_path),
        })

    included_count = len(source_paths) - excluded_count
    return {
        "schema_version": "2",
        "source_revision": PUBLIC_SOURCE_REVISION,
        "source_runtime_file_count": len(source_paths),
        "source_included_file_count": included_count,
        "source_excluded_file_count": excluded_count,
        "source_excluded_sha256": excluded.hexdigest(),
        "destination_file_count": len(destination_paths),
        "files": sorted(files, key=lambda item: (item["destination"], item["source"] or "")),
    }


def validate_frozen_inventory() -> None:
    payload = json.loads((WRAPPER / "migration-map.json").read_text(encoding="utf-8"))
    if payload.get("schema_version") != "2":
        raise ValueError("frozen inventory schema_version must be 2")
    if payload.get("source_revision") != PUBLIC_SOURCE_REVISION:
        raise ValueError("frozen inventory does not bind completed source revision")
    for field in (
        "source_runtime_file_count", "source_included_file_count",
        "source_excluded_file_count", "destination_file_count",
    ):
        if not isinstance(payload.get(field), int) or payload[field] < 0:
            raise ValueError(f"frozen inventory {field} must be a non-negative integer")
    if payload["source_runtime_file_count"] != payload["source_included_file_count"] + payload["source_excluded_file_count"]:
        raise ValueError("frozen inventory source counts do not balance")
    excluded_digest = payload.get("source_excluded_sha256")
    if not isinstance(excluded_digest, str) or len(excluded_digest) != 64:
        raise ValueError("frozen inventory excluded commitment must be SHA-256")

    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("frozen inventory files must be a non-empty list")
    seen_sources: set[str] = set()
    covered_destinations: set[str] = set()
    included_sources = 0
    for item in files:
        source = item.get("source")
        destination = item.get("destination")
        classification = item.get("classification")
        if classification not in CLASSIFICATIONS:
            raise ValueError(f"invalid frozen classification: {classification!r}")
        if not isinstance(destination, str) or not destination:
            raise ValueError("frozen inventory destination must be a non-empty path")
        destination_path = PLUGIN / destination
        if not destination_path.is_file() or sha256(destination_path) != item.get("destination_sha256"):
            raise ValueError(f"frozen inventory destination mismatch: {destination}")
        covered_destinations.add(destination)
        if classification == "PUBLIC_ADDITION":
            if source is not None or item.get("sha256") is not None:
                raise ValueError(f"public addition falsely claims source: {destination}")
            continue
        if not isinstance(source, str) or not source or source in seen_sources:
            raise ValueError(f"invalid or duplicate frozen source: {source!r}")
        seen_sources.add(source)
        included_sources += 1
        if classification == "UNCHANGED" and item.get("sha256") != item.get("destination_sha256"):
            raise ValueError(f"UNCHANGED hash mismatch: {destination}")

    expected_destinations = {path.relative_to(PLUGIN).as_posix() for path in public_files()}
    if covered_destinations != expected_destinations:
        raise ValueError("frozen inventory does not cover every public destination")
    if payload["destination_file_count"] != len(expected_destinations):
        raise ValueError("frozen inventory destination count mismatch")
    if payload["source_included_file_count"] != included_sources:
        raise ValueError("frozen inventory included source count mismatch")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=pathlib.Path)
    args = parser.parse_args()
    if args.source_root is None:
        validate_frozen_inventory()
        print("Validated frozen source-to-public migration inventory.")
        return 0
    payload = create_inventory(args.source_root)
    (WRAPPER / "migration-map.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    validate_frozen_inventory()
    print("Created source-to-public migration inventory.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
