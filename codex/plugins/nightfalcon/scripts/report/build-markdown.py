#!/usr/bin/env python3
"""Build canonical per-repository findings Markdown from schema-v2 JSON."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
from typing import Any


TIER_ALIASES = {
    "P0": "Critical",
    "P1": "High",
    "P2": "Medium",
    "P3": "Low",
    "P4": "Informational",
}

TOP_LEVEL_FIELDS = {
    "schema_version",
    "repo_slug",
    "repo_url",
    "commit_sha",
    "multitenant_scope",
    "date",
    "findings",
    "dismissed_findings",
    "poc_coverage",
    "methodology_notes",
    "disclaimer",
}

FINDING_FIELDS = {
    "finding_id",
    "tier",
    "cvss_score",
    "cvss_vector",
    "cwe",
    "cve",
    "exposure",
    "reachability",
    "exploitability",
    "impact",
    "patch_status",
    "title",
    "category",
    "location",
    "what_happens_md",
    "attack_steps_md",
    "evidence_md",
    "fix_md",
    "severity_md",
    "validation",
    "poc",
    "references",
    "controls_in_scope",
    "applicable_policies",
}
CONTROL_FIELDS = {"name", "control_id", "note_path"}
POLICY_FIELDS = {"id", "title", "binding_statement", "note_path"}


def context_catalog_path(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty repository-relative path")
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not value.startswith(
        "references/organization_context/"
    ):
        raise ValueError(f"{label} must be under references/organization_context/")


def validate_metadata(finding: dict[str, Any]) -> None:
    fid = finding.get("finding_id", "<unknown>")
    controls = finding.get("controls_in_scope", [])
    if not isinstance(controls, list):
        raise ValueError(f"finding {fid}: controls_in_scope must be a list")
    seen_controls: set[tuple[str, object]] = set()
    for index, control in enumerate(controls):
        label = f"finding {fid}: controls_in_scope[{index}]"
        if not isinstance(control, dict) or set(control) != CONTROL_FIELDS:
            raise ValueError(f"{label} must contain exactly name, control_id, note_path")
        if not isinstance(control["name"], str) or not control["name"].strip():
            raise ValueError(f"{label}.name must be a non-empty string")
        control_id = control["control_id"]
        if control_id is not None and (
            not isinstance(control_id, str)
            or re.fullmatch(r"CTRL-[1-9][0-9]*", control_id) is None
        ):
            raise ValueError(f"{label}.control_id must be null or CTRL-NNNN")
        context_catalog_path(control["note_path"], f"{label}.note_path")
        identity = (control["name"], control_id)
        if identity in seen_controls:
            raise ValueError(f"finding {fid}: controls_in_scope contains duplicates")
        seen_controls.add(identity)
    policies = finding.get("applicable_policies", [])
    if not isinstance(policies, list):
        raise ValueError(f"finding {fid}: applicable_policies must be a list")
    seen_policies: set[str] = set()
    for index, policy in enumerate(policies):
        label = f"finding {fid}: applicable_policies[{index}]"
        if not isinstance(policy, dict) or set(policy) != POLICY_FIELDS:
            raise ValueError(
                f"{label} must contain exactly id, title, binding_statement, note_path"
            )
        if not isinstance(policy["id"], str) or re.fullmatch(
            r"STD-[0-9]{4}", policy["id"]
        ) is None:
            raise ValueError(f"{label}.id must be STD-NNNN")
        for key in ("title", "binding_statement"):
            if not isinstance(policy[key], str) or not policy[key].strip():
                raise ValueError(f"{label}.{key} must be a non-empty string")
        context_catalog_path(policy["note_path"], f"{label}.note_path")
        if policy["id"] in seen_policies:
            raise ValueError(f"finding {fid}: applicable_policies contains duplicate ids")
        seen_policies.add(policy["id"])


def text(value: Any, fallback: str = "Not provided") -> str:
    if value is None:
        return fallback
    rendered = str(value).strip()
    return rendered or fallback


def json_block(value: Any) -> str:
    return "```json\n" + json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n```"


def append_markdown(lines: list[str], heading: str, value: Any) -> None:
    lines.extend((f"**{heading}:**", text(value), ""))


def render_poc(poc: dict[str, Any]) -> list[str]:
    lines = ["**Proof of concept:**"]
    if poc.get("skipped") is True:
        reason = text(poc.get("reason"))
        note = text(poc.get("note"))
        lines.append(f"Not generated — `{reason}`: {note}")
    else:
        script_path = text(poc.get("script_path"))
        script_type = text(poc.get("script_type"))
        verdict = text(poc.get("verdict"))
        lines.append(
            f"Generated at `{script_path}` ({script_type}; reviewer verdict: "
            f"**{verdict}**)."
        )
    lines.extend(("", "<details>", "<summary>Structured PoC metadata</summary>", ""))
    lines.extend((json_block(poc), "", "</details>", ""))
    return lines


def render_finding(finding: dict[str, Any]) -> list[str]:
    fid = text(finding.get("finding_id"))
    tier = text(finding.get("tier"))
    score = text(finding.get("cvss_score"))
    vector = text(finding.get("cvss_vector"))
    calculator = f"https://www.first.org/cvss/calculator/4.0#{vector}"
    cwe = text(finding.get("cwe"), "No CWE assigned")
    cve = text(finding.get("cve"), "No CVE assigned")
    lines = [
        f"### {fid}: {text(finding.get('title'))}",
        "",
        f"- **Category:** {text(finding.get('category'))}",
        f"- **Exposure:** `{text(finding.get('exposure'))}`",
        f"- **Severity:** {tier} — CVSS v4.0 Base {score} — "
        f"[`{vector}`]({calculator})",
        f"- **Classification:** {cwe}; {cve}",
        f"- **Location:** {text(finding.get('location'))}",
        "",
    ]
    append_markdown(lines, "What happens", finding.get("what_happens_md"))
    append_markdown(lines, "How the attack works, step by step", finding.get("attack_steps_md"))
    append_markdown(lines, "Evidence from the code", finding.get("evidence_md"))
    lines.extend(render_poc(finding["poc"]))
    lines.extend((f"- **Validation:** {text(finding.get('validation'))}", ""))
    append_markdown(lines, "How to fix it", finding.get("fix_md"))
    append_markdown(lines, "Severity, spelled out", finding.get("severity_md"))
    lines.extend(("**References:**",))
    lines.extend(f"- {reference}" for reference in finding["references"])
    lines.append("")
    controls = finding.get("controls_in_scope", [])
    if controls:
        lines.append("**organization controls in scope:**")
        for control in controls:
            suffix = (
                f" ({control['control_id']})"
                if control["control_id"] is not None else ""
            )
            lines.append(
                f"- {control['name']}{suffix} — `{control['note_path']}`"
            )
        lines.append("")
    policies = finding.get("applicable_policies", [])
    if policies:
        lines.append("**Applicable organization policies (standards):**")
        for policy in policies:
            lines.append(
                f"- {policy['id']} — {policy['title']}: "
                f"{policy['binding_statement']} — `{policy['note_path']}`"
            )
        lines.append("")
    return lines


def render(document: dict[str, Any]) -> str:
    if document.get("schema_version") != "2":
        raise ValueError("schema_version must be exactly '2'")
    findings = document.get("findings")
    if not isinstance(findings, list):
        raise ValueError("findings must be a list")
    if any(not isinstance(item, dict) for item in findings):
        raise ValueError("every finding must be an object")
    unknown_top = set(document) - TOP_LEVEL_FIELDS
    if unknown_top:
        raise ValueError(f"unknown top-level fields: {sorted(unknown_top)}")
    for field in ("dismissed_findings", "poc_coverage", "methodology_notes", "disclaimer"):
        if field not in document:
            raise ValueError(f"missing field: {field}")
    if not isinstance(document["dismissed_findings"], list):
        raise ValueError("dismissed_findings must be a list")
    for index, dismissed in enumerate(document["dismissed_findings"]):
        if not isinstance(dismissed, dict) or set(dismissed) != {
            "finding_id", "title", "reason"
        }:
            raise ValueError(
                f"dismissed_findings[{index}] must contain exactly "
                "finding_id, title, reason"
            )
        if any(not isinstance(dismissed[key], str) or not dismissed[key].strip()
               for key in ("finding_id", "title", "reason")):
            raise ValueError("dismissed finding fields must be non-empty strings")
    coverage = document["poc_coverage"]
    if not isinstance(coverage, dict) or set(coverage) != {"generated", "skipped"}:
        raise ValueError("poc_coverage must contain exactly generated and skipped")
    if any(not isinstance(coverage[key], int) or isinstance(coverage[key], bool)
           or coverage[key] < 0 for key in ("generated", "skipped")):
        raise ValueError("poc_coverage values must be non-negative integers")
    if not isinstance(document["methodology_notes"], list) or not document["methodology_notes"]:
        raise ValueError("methodology_notes must be a non-empty list")
    if not isinstance(document["disclaimer"], str) or not document["disclaimer"].strip():
        raise ValueError("disclaimer must be a non-empty string")
    for finding in findings:
        unknown_finding = set(finding) - FINDING_FIELDS
        if unknown_finding:
            raise ValueError(f"unknown finding fields: {sorted(unknown_finding)}")
        if finding.get("tier") not in TIER_ALIASES:
            raise ValueError(f"invalid finding tier: {finding.get('tier')!r}")
        if not isinstance(finding.get("poc"), dict):
            raise ValueError(
                f"finding {finding.get('finding_id', '<unknown>')}: poc must be an object"
            )
        validate_metadata(finding)
        references = finding.get("references")
        if not isinstance(references, list) or not references or any(
            not isinstance(reference, str) or not reference.strip()
            for reference in references
        ):
            raise ValueError("finding references must be a non-empty string list")

    counts = {tier: 0 for tier in TIER_ALIASES}
    for finding in findings:
        counts[finding["tier"]] += 1

    scope = document.get("multitenant_scope")
    if scope is True:
        scope_text = "Yes"
    elif scope is False:
        scope_text = "No"
    else:
        scope_text = text(scope)

    lines = [
        f"# Security Review: {text(document.get('repo_slug'))}",
        "",
        f"- **Date:** {text(document.get('date'))}",
        f"- **Repository:** {text(document.get('repo_url'))}",
        f"- **Commit:** `{text(document.get('commit_sha'))}`",
        f"- **Multitenant scope:** {scope_text}",
        "",
        "## Findings Summary",
        "",
        "| Tier | Count |",
        "|---|---:|",
    ]
    for tier, alias in TIER_ALIASES.items():
        lines.append(f"| {tier} ({alias}) | {counts[tier]} |")
    lines.append("")

    for tier, alias in TIER_ALIASES.items():
        tier_findings = [finding for finding in findings if finding["tier"] == tier]
        if not tier_findings:
            continue
        lines.extend((f"## {tier} ({alias}) Findings", ""))
        for finding in tier_findings:
            lines.extend(render_finding(finding))
    lines.extend(("## Out of Scope / Dismissed", ""))
    if document["dismissed_findings"]:
        for dismissed in document["dismissed_findings"]:
            lines.append(
                f"- **{text(dismissed.get('finding_id'))}: "
                f"{text(dismissed.get('title'))}** — {text(dismissed.get('reason'))}"
            )
    else:
        lines.append("No dismissed findings.")
    lines.extend((
        "",
        "## Proof-of-Concept Coverage",
        "",
        f"Generated: {coverage.get('generated')}; skipped: {coverage.get('skipped')}.",
        "",
        "## Methodology Notes",
        "",
    ))
    lines.extend(f"- {note}" for note in document["methodology_notes"])
    lines.extend(("", "## Disclaimer", "", document["disclaimer"], ""))
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()
    try:
        document = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("input must be a JSON object")
        output = render(document)
        args.output.write_text(output, encoding="utf-8", newline="\n")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
