#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
import zipfile


def _fail(message: str) -> int:
    print(f"nightfalcon release verification failed: {message}", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    args = parser.parse_args()
    root = args.release_dir.resolve(strict=True)
    try:
        release = json.loads((root / "release-manifest.json").read_text())
    except (OSError, json.JSONDecodeError) as error:
        return _fail(f"manifest is unreadable: {error}")
    if not isinstance(release, dict) or set(release) != {
        "schema_version",
        "version",
        "python_requires",
        "payload_manifest_sha256",
        "artifacts",
    }:
        return _fail("manifest schema is invalid")
    payload_digest = release["payload_manifest_sha256"]
    if (
        release["schema_version"] != 2
        or not isinstance(release["version"], str)
        or not isinstance(payload_digest, str)
        or len(payload_digest) != 64
    ):
        return _fail("manifest identity is invalid")
    records = release["artifacts"]
    if not isinstance(records, list) or not records:
        return _fail("artifact inventory is empty")
    expected: dict[str, dict[str, object]] = {}
    folded: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {"name", "size", "sha256"}:
            return _fail("artifact record schema is invalid")
        name = record["name"]
        if (
            not isinstance(name, str)
            or not name
            or PurePosixPath(name).name != name
            or name == "release-manifest.json"
            or name.casefold() in folded
        ):
            return _fail("artifact name is unsafe or ambiguous")
        if (
            isinstance(record["size"], bool)
            or not isinstance(record["size"], int)
            or record["size"] < 0
            or not isinstance(record["sha256"], str)
            or len(record["sha256"]) != 64
        ):
            return _fail(f"artifact metadata is invalid: {name}")
        folded.add(name.casefold())
        expected[name] = record

    actual: set[str] = set()
    try:
        for path in root.iterdir():
            if path.name == "release-manifest.json":
                continue
            if path.is_symlink() or not path.is_file():
                return _fail(f"release contains unsafe entry: {path.name}")
            actual.add(path.name)
    except OSError as error:
        return _fail(f"release directory is unreadable: {error}")
    if actual != set(expected):
        return _fail("artifact inventory mismatch")

    contents: dict[str, bytes] = {}
    for name, record in expected.items():
        try:
            content = (root / name).read_bytes()
        except OSError as error:
            return _fail(f"artifact is unreadable: {name}: {error}")
        digest = hashlib.sha256(content).hexdigest()
        if digest != record["sha256"]:
            return _fail(f"artifact checksum mismatch: {name}")
        if len(content) != record["size"]:
            return _fail(f"artifact size mismatch: {name}")
        contents[name] = content

    artifact_name = f"nightfalcon-{release['version']}.pyz"
    checksum_name = f"{artifact_name}.sha256"
    if artifact_name not in contents or checksum_name not in contents:
        return _fail("universal artifact or checksum is missing")
    digest = hashlib.sha256(contents[artifact_name]).hexdigest()
    if contents[checksum_name].decode("utf-8", "replace") != f"{digest}  {artifact_name}\n":
        return _fail("universal artifact checksum file mismatch")
    try:
        with zipfile.ZipFile(root / artifact_name) as archive:
            names = archive.namelist()
            if names != sorted(names) or len(names) != len(set(names)):
                return _fail("archive member order or uniqueness mismatch")
            for info in archive.infolist():
                path = PurePosixPath(info.filename)
                if path.is_absolute() or ".." in path.parts:
                    return _fail("unsafe archive member path")
                if info.date_time != (1980, 1, 1, 0, 0, 0):
                    return _fail("archive timestamp is not normalized")
            embedded = json.loads(archive.read("nightfalcon/_payload-manifest.json"))
            if not isinstance(embedded, dict) or set(embedded) != {
                "schema_version",
                "files",
                "manifest_sha256",
            }:
                return _fail("embedded payload manifest schema is invalid")
            body = {
                "schema_version": embedded["schema_version"],
                "files": embedded["files"],
            }
            canonical = json.dumps(
                body, sort_keys=True, separators=(",", ":")
            ).encode()
            computed_payload_digest = hashlib.sha256(canonical).hexdigest()
            if (
                embedded["manifest_sha256"] != computed_payload_digest
                or payload_digest != computed_payload_digest
            ):
                return _fail("payload manifest digest mismatch")
    except (OSError, KeyError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        return _fail(f"archive validation failed: {error}")
    print(f"verified {len(expected)} artifacts for NightFalcon {release['version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
