#!/usr/bin/env python3
"""Build deterministic cross-repository topology from current Phase 2 artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse


def load_documents(root: Path, date: str, slugs: list[str]) -> tuple[list[dict], list[dict]]:
    root = root.resolve()
    documents: list[dict] = []
    sources: list[dict] = []
    for slug in slugs:
        path = root / slug / f"dataflow-{date}.json"
        if path.is_symlink():
            raise ValueError(f"refusing symbolic-link dataflow: {slug}")
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
        data = json.loads(resolved.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("repo_slug") != slug or data.get("date") != date:
            raise ValueError(f"dataflow provenance mismatch: {slug}")
        if not isinstance(data.get("outbound_edges"), list):
            raise ValueError(f"dataflow outbound_edges missing: {slug}")
        documents.append(data)
        sources.append({
            "repo_slug": slug,
            "path": str(path.relative_to(root)),
            "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
        })
    return documents, sources


def aliases(document: dict) -> set[str]:
    values = document.get("service_aliases", [])
    inventory = document.get("inventory")
    if isinstance(inventory, dict):
        values = [*values, *inventory.get("service_aliases", [])]
    return {value.lower().rstrip(".") for value in values if isinstance(value, str) and value.strip()}


def target_parts(target: str) -> tuple[str, str]:
    parsed = urlparse(target if "://" in target else f"//{target}")
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path.strip("/")
    repo_hint = path.split("/")[-1].removesuffix(".git").lower() if path else ""
    if not host:
        first, _, remainder = target.partition("/")
        host = first.lower().rstrip(".")
        repo_hint = remainder.split("/")[-1].removesuffix(".git").lower() if remainder else first.lower()
    return host, repo_hint


def resolve_match(edge: dict, repos: dict[str, dict], alias_index: dict[str, set[str]]) -> str | None:
    explicit = edge.get("target_repo_slug")
    target = edge.get("target") or edge.get("url") or edge.get("host") or ""
    if not isinstance(target, str):
        return None
    normalized_explicit = explicit.lower() if isinstance(explicit, str) and explicit.lower() in repos else None
    if not target.strip():
        return normalized_explicit
    host, repo_hint = target_parts(target)
    candidates: set[str] = set()
    for value in {target.lower().strip("/"), repo_hint, host}:
        if value in repos:
            candidates.add(value)
        candidates.update(alias_index.get(value, set()))
    for alias, owners in alias_index.items():
        if host == alias or host.endswith(f".{alias}"):
            candidates.update(owners)
    independently_resolved = next(iter(candidates)) if len(candidates) == 1 else None
    if normalized_explicit is not None and independently_resolved != normalized_explicit:
        return None
    return independently_resolved


def reconcile(root: Path, date: str, slugs: list[str]) -> dict:
    normalized = [slug.lower() for slug in slugs]
    if len(set(normalized)) != len(normalized):
        raise ValueError("repo slugs must be unique case-insensitively")
    documents, sources = load_documents(root, date, slugs)
    repos = {slug.lower(): document for slug, document in zip(slugs, documents)}
    alias_index: dict[str, set[str]] = {}
    for slug, document in repos.items():
        for alias in aliases(document):
            alias_index.setdefault(alias, set()).add(slug)
    edges = []
    for slug, document in repos.items():
        for edge in document["outbound_edges"]:
            target = edge.get("target") or edge.get("url") or edge.get("host") or ""
            host, _ = target_parts(target) if isinstance(target, str) else ("", "")
            matched = resolve_match(edge, repos, alias_index)
            classification = "matched" if matched else "external" if edge.get("declared_external") is True else "unresolved"
            edges.append({
                "source_repo": slug, "flow_id": edge.get("flow_id"), "target": target,
                "target_host": host, "classification": classification, "matched_repo": matched,
                "auth_context": edge.get("auth_context", "unknown"),
                "tenant_binding": edge.get("tenant_binding", "unknown"),
                "evidence": edge.get("evidence", []),
            })
    return {
        "schema_version": "2", "date": date, "repo_slugs": slugs,
        "source_artifacts": sources,
        "cross_repository_topology": {
            "repositories": slugs, "edges": edges,
            "phase_order": "after-all-phase-2-before-phase-3",
        },
    }


def validate(document: dict, root: Path, date: str, slugs: list[str]) -> None:
    expected = reconcile(root, date, slugs)
    if document != expected:
        raise ValueError("stored topology does not match deterministic reconciliation of current Phase 2 dataflows")
    if document.get("schema_version") != "2" or document.get("date") != date or document.get("repo_slugs") != slugs:
        raise ValueError("topology provenance does not match current run")
    sources = document.get("source_artifacts")
    topology = document.get("cross_repository_topology")
    if not isinstance(sources, list) or {item.get("repo_slug") for item in sources if isinstance(item, dict)} != set(slugs):
        raise ValueError("topology source artifact set is incomplete")
    _, current_sources = load_documents(root, date, slugs)
    if sources != current_sources:
        raise ValueError("topology source artifact digests do not match current Phase 2 dataflows")
    if not isinstance(topology, dict) or topology.get("repositories") != slugs:
        raise ValueError("topology repository set is incomplete")
    if not isinstance(topology.get("edges"), list) or any(edge.get("classification") not in {"matched", "external", "unresolved"} for edge in topology["edges"]):
        raise ValueError("topology edge classification is invalid")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--date", required=True)
    parser.add_argument("--slug", action="append", required=True)
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    if not args.input_dir.is_dir():
        parser.error(f"input directory does not exist: {args.input_dir}")
    if args.validate:
        validate(json.loads(args.output.read_text(encoding="utf-8")), args.input_dir, args.date, args.slug)
        return 0
    result = reconcile(args.input_dir, args.date, args.slug)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
