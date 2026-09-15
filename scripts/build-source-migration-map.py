#!/usr/bin/env python3
"""Build or validate source-to-public provenance for all NightFalcon ports."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "source-migration-map.json"
SOURCE_REVISION = "0c3183e02ec1f1500115233402cabdf7808ec4e6"
PORTS = {
    "claude": ("NightFalcon-Claude", ROOT / "claude"),
    "codex": ("NightFalcon-Codex", ROOT / "codex"),
    "cursor": ("NightFalcon-Cursor", ROOT / "cursor"),
}
CLASSIFICATIONS = {"UNCHANGED", "ADAPTED", "PUBLIC_ADDITION"}
DESTINATION_EXCLUSIONS = {
    "claude": set(),
    "codex": {"verification-results.json", "docs/verification-report.md"},
    "cursor": set(),
}
GENERATED_DIRECTORY_EXCLUSIONS = {"__pycache__", ".terragraph"}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def eligible_files(root: Path, exclusions: set[str]) -> list[Path]:
    return [
        path for path in sorted(root.rglob("*"))
        if path.is_file()
        and path.relative_to(root).as_posix() not in exclusions
        and not GENERATED_DIRECTORY_EXCLUSIONS.intersection(path.parts)
        and path.suffix != ".pyc"
    ]


def record(relative: str, digest: str) -> bytes:
    return relative.encode() + b"\0" + digest.encode() + b"\n"


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
        raise ValueError("source checkout must be clean before provenance generation")


def tracked_files(source: Path, source_relative: str) -> list[Path]:
    output = subprocess.check_output(
        ["git", "-C", str(source), "ls-files", "--", source_relative], text=True
    )
    prefix = source_relative + "/"
    source_root = source / source_relative
    return [source_root / item.removeprefix(prefix) for item in sorted(output.splitlines())]


def build_port(source: Path, source_relative: str, destination_root: Path, exclusions: set[str]) -> dict:
    source_root = source / source_relative
    source_paths = tracked_files(source, source_relative)
    destination_paths = eligible_files(destination_root, exclusions)
    destinations = {path.relative_to(destination_root).as_posix(): path for path in destination_paths}
    entries = []
    covered = set()
    excluded = hashlib.sha256()
    excluded_count = 0
    for source_path in source_paths:
        relative = source_path.relative_to(source_root).as_posix()
        source_digest = sha256(source_path)
        destination = destinations.get(relative)
        if destination is None:
            excluded.update(record(relative, source_digest))
            excluded_count += 1
            continue
        destination_digest = sha256(destination)
        classification = "UNCHANGED" if source_digest == destination_digest else "ADAPTED"
        entries.append({
            "source": relative,
            "destination": relative,
            "classification": classification,
            "source_sha256": source_digest,
            "destination_sha256": destination_digest,
        })
        covered.add(relative)
    for relative, destination in destinations.items():
        if relative in covered:
            continue
        entries.append({
            "source": None,
            "destination": relative,
            "classification": "PUBLIC_ADDITION",
            "source_sha256": None,
            "destination_sha256": sha256(destination),
        })
    return {
        "source_file_count": len(source_paths),
        "included_source_file_count": len(source_paths) - excluded_count,
        "excluded_source_file_count": excluded_count,
        "excluded_source_sha256": excluded.hexdigest(),
        "destination_scope_exclusions": sorted(exclusions),
        "destination_file_count": len(destination_paths),
        "files": sorted(entries, key=lambda item: (item["destination"], item["source"] or "")),
    }


def create(source: Path) -> dict:
    verify_source(source)
    return {
        "schema_version": "1",
        "source_revision": SOURCE_REVISION,
        "ports": {
            name: build_port(source, source_relative, destination, DESTINATION_EXCLUSIONS[name])
            for name, (source_relative, destination) in PORTS.items()
        },
    }


def validate() -> None:
    payload = json.loads(OUTPUT.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "1" or payload.get("source_revision") != SOURCE_REVISION:
        raise ValueError("source migration map does not bind completed source revision")
    ports = payload.get("ports")
    if not isinstance(ports, dict) or set(ports) != set(PORTS):
        raise ValueError("source migration map must cover Claude, Codex, and Cursor")
    for name, (_, destination_root) in PORTS.items():
        port = ports[name]
        if port.get("destination_scope_exclusions") != sorted(DESTINATION_EXCLUSIONS[name]):
            raise ValueError(f"{name} destination exclusions do not match policy")
        if port.get("source_file_count") != port.get("included_source_file_count") + port.get("excluded_source_file_count") + len(port.get("post_migration_exclusions", [])):
            raise ValueError(f"{name} source counts do not balance")
        if not isinstance(port.get("excluded_source_sha256"), str) or len(port["excluded_source_sha256"]) != 64:
            raise ValueError(f"{name} excluded source commitment is invalid")
        expected = {
            path.relative_to(destination_root).as_posix()
            for path in eligible_files(destination_root, DESTINATION_EXCLUSIONS[name])
        }
        covered = set()
        sources = set()
        included = 0
        for item in port.get("files", []):
            destination = item.get("destination")
            classification = item.get("classification")
            if classification not in CLASSIFICATIONS or not isinstance(destination, str):
                raise ValueError(f"{name} contains invalid mapping")
            path = destination_root / destination
            if not path.is_file() or sha256(path) != item.get("destination_sha256"):
                raise ValueError(f"{name} destination hash mismatch: {destination}")
            covered.add(destination)
            source = item.get("source")
            if classification == "PUBLIC_ADDITION":
                if source is not None or item.get("source_sha256") is not None:
                    raise ValueError(f"{name} public addition falsely claims source")
            else:
                if not isinstance(source, str) or source in sources:
                    raise ValueError(f"{name} contains invalid or duplicate source")
                sources.add(source)
                included += 1
                if classification == "UNCHANGED" and item.get("source_sha256") != item.get("destination_sha256"):
                    raise ValueError(f"{name} unchanged hashes differ")
        if covered != expected or port.get("destination_file_count") != len(expected):
            raise ValueError(f"{name} public destination coverage is incomplete")
        # Preserve the original excluded-source commitment; later removals
        # retain their individual source hashes separately.
        for item in port.get("post_migration_exclusions", []):
            source = item.get("source")
            digest = item.get("source_sha256", "")
            if not isinstance(source, str) or source in sources:
                raise ValueError(f"{name} invalid post-migration exclusion")
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError(f"{name} invalid excluded source hash")
            sources.add(source)
        if port.get("included_source_file_count") != included:
            raise ValueError(f"{name} included source count mismatch")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    args = parser.parse_args()
    if args.source is None:
        validate()
        print("Validated frozen three-port source migration map.")
        return 0
    payload = create(args.source.resolve())
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    validate()
    print("Created three-port source migration map.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
