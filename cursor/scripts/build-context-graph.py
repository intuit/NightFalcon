#!/usr/bin/env python3
"""Validate and atomically normalize a NightFalcon organization-context graph."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def contained(root: Path, candidate: Path) -> Path:
    resolved_root = root.resolve(strict=True)
    resolved = candidate.resolve(strict=True)
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError(f"context path escapes configured root: {candidate}")
    return resolved


def normalize(root: Path, source: Path) -> dict:
    source = contained(root, source)
    data = json.loads(source.read_text(encoding="utf-8"))
    if data.get("schema_version") != "2":
        raise ValueError("organization context schema_version must be '2'")
    for key in ("controls", "platform_services", "standards"):
        if not isinstance(data.get(key), list):
            raise ValueError(f"{key} must be a list")
    if not isinstance(data.get("fingerprint_index"), dict):
        raise ValueError("fingerprint_index must be an object")
    entries = [*data["controls"], *data["platform_services"]]
    names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("control and platform service entries must be objects")
        required = {"name", "kind", "control_id", "fingerprints", "applicable_standards"}
        if not required <= set(entry):
            raise ValueError("context entry is missing required fields")
        name = entry["name"]
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("context entry names must be unique non-empty strings")
        names.add(name)
        if entry["kind"] not in {"control", "platform_service"}:
            raise ValueError(f"invalid context kind for {name}")
        if entry in data["controls"] and entry["kind"] != "control":
            raise ValueError(f"controls entry has wrong kind for {name}")
        if entry in data["platform_services"] and entry["kind"] != "platform_service":
            raise ValueError(f"platform_services entry has wrong kind for {name}")
        control_id = entry["control_id"]
        if control_id is not None and (not isinstance(control_id, str) or re.fullmatch(r"CTRL-[0-9]{4}", control_id) is None):
            raise ValueError(f"invalid control_id for {name}")
        if not isinstance(entry["fingerprints"], list) or not all(isinstance(item, str) and item for item in entry["fingerprints"]):
            raise ValueError(f"fingerprints must be non-empty strings for {name}")
        if not isinstance(entry["applicable_standards"], list):
            raise ValueError(f"applicable_standards must be a list for {name}")
        probes = entry.get("coverage_probes", [])
        if not isinstance(probes, list) or not all(isinstance(item, str) and item for item in probes):
            raise ValueError(f"coverage_probes must be non-empty strings for {name}")
        example = entry.get("remediation_example")
        if example is not None and (
            not isinstance(example, dict)
            or not all(isinstance(example.get(field), str) and example[field] for field in ("summary", "snippet"))
        ):
            raise ValueError(f"remediation_example must contain summary and snippet for {name}")
    standard_ids: set[str] = set()
    for standard in data["standards"]:
        if not isinstance(standard, dict):
            raise ValueError("standard entries must be objects")
        required = {"id", "title", "binding_statement", "canonical_controls"}
        if not required <= set(standard):
            raise ValueError("standard entry is missing required fields")
        standard_id = standard["id"]
        if not isinstance(standard_id, str) or not standard_id.startswith("STD-") or standard_id in standard_ids:
            raise ValueError("standard ids must be unique STD-* strings")
        standard_ids.add(standard_id)
        if not isinstance(standard["binding_statement"], str) or not standard["binding_statement"]:
            raise ValueError(f"binding_statement must be non-empty for {standard_id}")
        if not isinstance(standard["canonical_controls"], list) or not all(isinstance(item, str) and item for item in standard["canonical_controls"]):
            raise ValueError(f"canonical_controls must be a string list for {standard_id}")
        policy_area = standard.get("policy_area")
        if policy_area is not None and (not isinstance(policy_area, str) or not policy_area):
            raise ValueError(f"policy_area must be null or non-empty for {standard_id}")
    for entry in entries:
        unknown = set(entry["applicable_standards"]) - standard_ids
        if unknown:
            raise ValueError(f"context entry references unknown standards: {sorted(unknown)}")
    for fingerprint, targets in data["fingerprint_index"].items():
        if not isinstance(fingerprint, str) or not fingerprint or not isinstance(targets, list):
            raise ValueError("fingerprint_index keys must be strings and values must be lists")
        unknown = set(targets) - names
        if unknown:
            raise ValueError(f"fingerprint references unknown context entry: {sorted(unknown)}")
    for entry in [*entries, *data["standards"]]:
        note = entry.get("note_path")
        if note:
            contained(root, root / note)
    data["generated_at"] = datetime.now(timezone.utc).isoformat()
    data["source_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    return data


def atomic_write(output: Path, data: dict) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, output)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--source", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    package_root = Path(__file__).resolve().parents[1]
    root = (args.root or Path(os.environ.get("NIGHTFALCON_CONTEXT_ROOT", package_root / "references" / "organization_context"))).resolve()
    source = args.source or root / "_graph.json"
    output = args.output or root / "_graph.normalized.json"
    atomic_write(output, normalize(root, source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
