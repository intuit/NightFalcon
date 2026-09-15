#!/usr/bin/env python3
"""Build canonical executive JSON from validated schema-v2 findings JSON."""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
from typing import Any


TIERS = ("P0", "P1", "P2", "P3", "P4")
ALIASES = {
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


def require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def context_catalog_path(value: Any, label: str) -> None:
    require_string(value, label)
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
        name = require_string(control["name"], f"{label}.name")
        control_id = control["control_id"]
        if control_id is not None and (
            not isinstance(control_id, str)
            or re.fullmatch(r"CTRL-[1-9][0-9]*", control_id) is None
        ):
            raise ValueError(f"{label}.control_id must be null or CTRL-NNNN")
        context_catalog_path(control["note_path"], f"{label}.note_path")
        identity = (name, control_id)
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
        policy_id = policy["id"]
        if not isinstance(policy_id, str) or re.fullmatch(
            r"STD-[0-9]{4}", policy_id
        ) is None:
            raise ValueError(f"{label}.id must be STD-NNNN")
        require_string(policy["title"], f"{label}.title")
        require_string(policy["binding_statement"], f"{label}.binding_statement")
        context_catalog_path(policy["note_path"], f"{label}.note_path")
        if policy_id in seen_policies:
            raise ValueError(f"finding {fid}: applicable_policies contains duplicate ids")
        seen_policies.add(policy_id)


def validate_source(document: dict[str, Any], expected_date: str) -> None:
    unknown = set(document) - TOP_LEVEL_FIELDS
    if unknown:
        raise ValueError(f"unknown top-level fields: {sorted(unknown)}")
    missing = TOP_LEVEL_FIELDS - set(document)
    if missing:
        raise ValueError(f"missing top-level fields: {sorted(missing)}")
    if document["schema_version"] != "2":
        raise ValueError("schema_version must be exactly '2'")
    require_string(document["repo_slug"], "repo_slug")
    require_string(document["repo_url"], "repo_url")
    require_string(document["commit_sha"], "commit_sha")
    if document["date"] != expected_date:
        raise ValueError("date does not match --date")
    findings = document["findings"]
    if not isinstance(findings, list):
        raise ValueError("findings must be a list")
    generated = 0
    skipped = 0
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise ValueError(f"finding at index {index} must be an object")
        unknown_finding = set(finding) - FINDING_FIELDS
        if unknown_finding:
            raise ValueError(f"unknown finding fields: {sorted(unknown_finding)}")
        missing_finding = {
            "finding_id", "tier", "title", "poc", "references"
        } - set(finding)
        if missing_finding:
            raise ValueError(f"finding missing fields: {sorted(missing_finding)}")
        if finding["tier"] not in TIERS:
            raise ValueError(f"invalid finding tier: {finding['tier']!r}")
        require_string(finding["finding_id"], "finding_id")
        require_string(finding["title"], "title")
        validate_metadata(finding)
        references = finding["references"]
        if not isinstance(references, list) or not references or any(
            not isinstance(reference, str) or not reference.strip()
            for reference in references
        ):
            raise ValueError("references must be a non-empty string list")
        poc = finding["poc"]
        if not isinstance(poc, dict):
            raise ValueError("poc must be an object")
        if "script_path" in poc:
            generated += 1
        elif poc.get("skipped") is True:
            skipped += 1
        else:
            raise ValueError("poc must be the generated or skipped tagged union")
    expected_coverage = {"generated": generated, "skipped": skipped}
    if document["poc_coverage"] != expected_coverage:
        raise ValueError("poc_coverage does not match findings")
    dismissed = document["dismissed_findings"]
    if not isinstance(dismissed, list):
        raise ValueError("dismissed_findings must be a list")
    for index, item in enumerate(dismissed):
        if not isinstance(item, dict) or set(item) != {"finding_id", "title", "reason"}:
            raise ValueError(
                f"dismissed_findings[{index}] must contain exactly finding_id, title, reason"
            )
        for key in ("finding_id", "title", "reason"):
            require_string(item[key], f"dismissed_findings[{index}].{key}")
    notes = document["methodology_notes"]
    if not isinstance(notes, list) or not notes:
        raise ValueError("methodology_notes must be a non-empty string list")
    for note in notes:
        require_string(note, "methodology note")
    require_string(document["disclaimer"], "disclaimer")


def compact_markdown(value: Any) -> str:
    """Collapse finding prose for dense executive-summary bullets."""
    text = str(value or "").strip()
    text = re.sub(r"```[^\n]*\n?", "", text)
    text = re.sub(r"(?m)^\s*(?:[-*+] |\d+[.)] )", "", text)
    return re.sub(r"\s+", " ", text).strip()


def markdown_literal(value: Any) -> str:
    """Protect compacted source text from controlled Markdown parsing."""
    return re.sub(r"([\\`*])", r"\\\1", compact_markdown(value))


def cvss_score(finding: dict[str, Any]) -> float:
    try:
        return float(finding.get("cvss_score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def count_phrase(counts: dict[str, int]) -> str:
    populated = [
        f"{counts[tier]} {ALIASES[tier]}"
        for tier in TIERS
        if counts.get(tier, 0)
    ]
    return ", ".join(populated) if populated else "no reportable findings"


def classification_tags(category: Any) -> list[str]:
    """Return deterministic recurring-risk labels embedded in categories."""
    value = compact_markdown(category)
    patterns = (
        r"OWASP Top 10 2025\s*[—-]\s*A\d{2}-[^;,)]+",
        r"OWASP Business Logic Abuse Top 10 2025\s*[—-]\s*BLA\d+-[^;,)]+",
        r"OWASP Open Source Software Top 10\s*[—-]\s*OSS-RISK\d+-[^;,)]+",
    )
    tags: list[str] = []
    for pattern in patterns:
        tags.extend(match.strip() for match in re.findall(pattern, value))
    return list(dict.fromkeys(tags)) or ([value] if value else ["Uncategorized"])


def priority_findings_markdown(repos: list[dict[str, Any]]) -> str:
    ranked = [
        (repo, finding)
        for repo in repos
        for finding in repo["findings"]
        if finding["tier"] in {"P0", "P1"}
    ]
    if not ranked:
        return "No Critical or High findings require priority action."
    ranked.sort(
        key=lambda item: (
            TIERS.index(item[1]["tier"]),
            -cvss_score(item[1]),
            item[0]["slug"],
            item[1]["finding_id"],
        )
    )
    lines: list[str] = []
    for repo, finding in ranked:
        tier = finding["tier"]
        lines.append(
            f"- **{tier} ({ALIASES[tier]}) — {markdown_literal(finding['title'])}** "
            f"(`{repo['slug']}` / `{finding['finding_id']}`)"
        )
        for label, field in (
            ("Category", "category"),
            ("Location", "location"),
            ("What happens", "what_happens_md"),
            ("Impact", "impact"),
            ("Recommended fix", "fix_md"),
        ):
            value = markdown_literal(finding.get(field))
            if value:
                lines.append(f"  - **{label}:** {value}")
    return "\n".join(lines)


def recurring_patterns(repos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = (
        collections.defaultdict(list)
    )
    for repo in repos:
        for finding in repo["findings"]:
            for tag in classification_tags(finding.get("category")):
                grouped[tag].append((repo, finding))
    patterns: list[dict[str, Any]] = []
    for tag, matches in grouped.items():
        if len(matches) < 2:
            continue
        finding_refs = [
            f"`{repo['slug']}/{finding['finding_id']}` "
            f"({markdown_literal(finding['title'])})"
            for repo, finding in matches
        ]
        fixes = [
            f"- `{repo['slug']}/{finding['finding_id']}`: "
            f"{markdown_literal(finding.get('fix_md'))}"
            for repo, finding in matches
            if compact_markdown(finding.get("fix_md"))
        ]
        patterns.append(
            {
                "tag": tag,
                "title": tag,
                "repos": sorted({repo["slug"] for repo, _ in matches}),
                "finding_count": len(matches),
                "finding_ids": {
                    slug: [
                        finding["finding_id"]
                        for repo, finding in matches
                        if repo["slug"] == slug
                    ]
                    for slug in sorted({repo["slug"] for repo, _ in matches})
                },
                "remediation_md": (
                    "Related findings: " + "; ".join(finding_refs) + ".\n\n"
                    "**Coordinated remediation from validated findings:**\n"
                    + "\n".join(fixes)
                ),
                "highest_tier": min(
                    (finding["tier"] for _, finding in matches),
                    key=TIERS.index,
                ),
            }
        )
    return sorted(
        patterns,
        key=lambda item: (
            TIERS.index(item["highest_tier"]),
            -item["finding_count"],
            item["title"],
        ),
    )


def poc_summary(repos: list[dict[str, Any]]) -> dict[str, Any]:
    generated = [
        finding["poc"]
        for repo in repos
        for finding in repo["findings"]
        if "script_path" in finding["poc"]
    ]
    verdicts = collections.Counter(
        str(poc.get("verdict") or "").strip().upper().replace("_", "-")
        for poc in generated
    )
    return {
        "total": len(generated),
        "valid": verdicts["VALID"],
        "needs_revision": verdicts["NEEDS-REVISION"],
        "invalid": verdicts["INVALID"],
        "unreviewed": sum(
            count
            for verdict, count in verdicts.items()
            if verdict not in {"VALID", "NEEDS-REVISION", "INVALID"}
        ),
        "guide_paths": sorted(
            {str(poc["guide_path"]) for poc in generated if poc.get("guide_path")}
        ),
        "config_paths": sorted(
            {str(poc["config_path"]) for poc in generated if poc.get("config_path")}
        ),
    }


def build(documents: list[dict[str, Any]], date: str) -> dict[str, Any]:
    if not documents:
        raise ValueError("at least one --input is required")
    for document in documents:
        validate_source(document, date)
    documents = sorted(documents, key=lambda document: document["repo_slug"])
    seen: set[str] = set()
    totals = {tier: 0 for tier in TIERS}
    repos: list[dict[str, Any]] = []
    for document in documents:
        slug = document["repo_slug"]
        if slug in seen:
            raise ValueError(f"duplicate repo_slug: {slug}")
        seen.add(slug)
        counts = {tier: 0 for tier in TIERS}
        for finding in document["findings"]:
            counts[finding["tier"]] += 1
            totals[finding["tier"]] += 1
        ordered = sorted(
            document["findings"],
            key=lambda finding: (TIERS.index(finding["tier"]), finding["finding_id"]),
        )
        lead = min(
            ordered,
            key=lambda finding: (
                TIERS.index(finding["tier"]),
                -cvss_score(finding),
                finding["finding_id"],
            ),
        ) if ordered else None
        summary_note = lead["title"] if lead else "No findings recorded."
        most_severe = next((tier for tier in TIERS if counts[tier]), "Clean")
        repos.append(
            {
                "slug": slug,
                "repo_url": document["repo_url"],
                "commit_sha": document["commit_sha"],
                "multitenant_scope": document["multitenant_scope"],
                "most_severe": most_severe,
                "summary_note": summary_note,
                "counts": counts,
                "findings": ordered,
                "dismissed_findings": document["dismissed_findings"],
                "poc_coverage": document["poc_coverage"],
                "methodology_notes": document["methodology_notes"],
                "disclaimer": document["disclaimer"],
            }
        )
    totals["clean_repos"] = sum(1 for repo in repos if repo["most_severe"] == "Clean")
    finding_total = sum(totals[tier] for tier in TIERS)
    most_severe = next((tier for tier in TIERS if totals[tier]), "Clean")
    top_candidates = [
        (repo, finding)
        for repo in repos
        for finding in repo["findings"]
        if finding["tier"] == most_severe
    ]
    top_finding = min(
        top_candidates,
        key=lambda item: (-cvss_score(item[1]), item[0]["slug"], item[1]["finding_id"]),
    ) if top_candidates else None
    action_total = totals["P0"] + totals["P1"]
    headline = (
        f"Review recorded **{finding_total} reportable finding"
        f"{'s' if finding_total != 1 else ''}** across **{len(repos)} repositor"
        f"{'ies' if len(repos) != 1 else 'y'}**: {count_phrase(totals)}. "
    )
    if action_total:
        headline += (
            f"Immediate focus: **{totals['P0']} Critical** and "
            f"**{totals['P1']} High** findings. "
        )
    if top_finding is not None:
        repo, finding = top_finding
        headline += (
            f"Highest-ranked issue: `{repo['slug']}/{finding['finding_id']}` — "
            f"**{markdown_literal(finding['title'])}**."
        )
    else:
        headline += "No reportable security finding remained after validation."

    scoped_repos = sum(1 for repo in repos if repo.get("multitenant_scope") is True)
    what_we_did = (
        f"Reviewed {len(repos)} pinned repositor"
        f"{'ies' if len(repos) != 1 else 'y'} through NightFalcon's adversarial "
        "workflow. This executive view is a deterministic projection of validated "
        "schema-v2 findings, PoC verdicts, dismissed candidates, and recorded "
        "methodology. Findings remain ordered Critical through Informational."
    )
    if scoped_repos:
        what_we_did += (
            f" Multitenant isolation was in scope for {scoped_repos} repositor"
            f"{'ies' if scoped_repos != 1 else 'y'}."
        )

    priority_md = priority_findings_markdown(repos)
    pattern_items = recurring_patterns(repos)
    poc = poc_summary(repos)
    controls_count = sum(
        1
        for repo in repos
        for finding in repo["findings"]
        if finding.get("controls_in_scope")
    )
    policies_count = sum(
        1
        for repo in repos
        for finding in repo["findings"]
        if finding.get("applicable_policies")
    )
    narratives: list[str] = [
        f"Across the reviewed scope, {len(pattern_items)} repeated risk pattern"
        f"{'s connect' if len(pattern_items) != 1 else ' connects'} "
        f"{sum(item['finding_count'] for item in pattern_items)} finding associations. "
        f"PoC review produced {poc['valid']} valid, {poc['needs_revision']} "
        f"needs-revision, and {poc['invalid']} invalid verdicts. "
        f"Recorded organization controls apply to {controls_count} findings and "
        f"recorded policies apply to {policies_count} findings."
    ]
    for repo in repos:
        finding_count = sum(repo["counts"].values())
        if finding_count:
            narratives.append(
                f"- **`{repo['slug']}`:** {finding_count} findings "
                f"({count_phrase(repo['counts'])}). Most severe: "
                f"**{repo['most_severe']} ({ALIASES[repo['most_severe']]})**. "
                f"Lead issue: **{markdown_literal(repo['summary_note'])}**."
            )
        else:
            narratives.append(f"- **`{repo['slug']}`:** no reportable findings.")
    narrative_md = "\n".join(narratives)

    validation_counts = collections.Counter(
        str(finding.get("validation") or "UNSPECIFIED")
        for repo in repos
        for finding in repo["findings"]
    )
    dismissed = [
        (repo["slug"], item)
        for repo in repos
        for item in repo["dismissed_findings"]
    ]
    review_lines = [
        "- **Reported validation states:** "
        + (
            ", ".join(
                f"{state}: {count}" for state, count in sorted(validation_counts.items())
            )
            if validation_counts
            else "none"
        )
        + ".",
        f"- **Dismissed candidates:** {len(dismissed)}.",
        (
            "- **Validation-correction history:** correction history is unavailable "
            "in schema-v2 final findings; no severity-change or compensating-control "
            "rationale was inferred."
        ),
    ]
    review_lines.extend(
        f"  - `{slug}/{item['finding_id']}` — "
        f"**{markdown_literal(item['title'])}**: {markdown_literal(item['reason'])}"
        for slug, item in dismissed
    )
    review_outcomes_md = "\n".join(review_lines)

    methodology_lines: list[str] = []
    disclaimer_lines: list[str] = []
    for repo in repos:
        methodology_lines.append(f"### `{repo['slug']}`")
        methodology_lines.extend(f"- {note}" for note in repo["methodology_notes"])
        disclaimer_lines.append(f"- **`{repo['slug']}`:** {repo['disclaimer']}")
    summary = {
        "what_we_did_md": what_we_did,
        "headline_md": headline,
        "priority_findings_md": priority_md,
        "narrative_md": narrative_md,
        "review_outcomes_md": review_outcomes_md,
        "methodology_md": "\n".join(methodology_lines),
        "disclaimer_md": "\n".join(disclaimer_lines),
    }

    summary_lines = [
        "# Executive Summary",
        "",
        "## Aggregate Findings",
        "",
        "| Tier | Total |",
        "|---|---:|",
    ]
    summary_lines.extend(
        f"| {tier} ({ALIASES[tier]}) | {totals[tier]} |" for tier in TIERS
    )
    summary_lines.extend(
        ("", "## Per-Repository Summary", "", "| Repository | P0 | P1 | P2 | P3 | P4 | Most severe |", "|---|---:|---:|---:|---:|---:|---|")
    )
    for repo in repos:
        summary_lines.append(
            f"| `{repo['slug']}` | "
            + " | ".join(str(repo["counts"][tier]) for tier in TIERS)
            + f" | {repo['most_severe']} |"
        )
    summary_lines.extend(("", "## What we did", "", what_we_did))
    summary_lines.extend(("", "## The headline", "", headline))
    summary_lines.extend(
        ("", "## Priority findings requiring action", "", priority_md)
    )
    summary_lines.extend(("", "## Proof-of-Concept Coverage", ""))
    summary_lines.extend(
        (
            f"- **Generated:** {poc['total']}",
            f"- **Valid:** {poc['valid']}",
            f"- **Needs revision:** {poc['needs_revision']}",
            f"- **Invalid:** {poc['invalid']}",
            f"- **Unreviewed/other verdict:** {poc['unreviewed']}",
        )
    )
    if poc["guide_paths"]:
        summary_lines.append(
            "- **PoC guides:** " + ", ".join(f"`{path}`" for path in poc["guide_paths"])
        )
    if pattern_items:
        summary_lines.extend(("", "## Recurring patterns worth acting on", ""))
        for pattern in pattern_items:
            summary_lines.extend(
                (
                    f"### {pattern['title']}",
                    "",
                    pattern["remediation_md"],
                    "",
                )
            )
    summary_lines.extend(("", "## Executive narrative", "", narrative_md))
    summary_lines.extend(("", "## Review outcomes", "", review_outcomes_md))
    summary_lines.extend(("", "## Methodology", "", summary["methodology_md"]))
    summary_lines.extend(("", "## Disclaimer", "", summary["disclaimer_md"]))
    return {
        "report_title": f"Security Review — {date}",
        "report_subtitle": f"{len(repos)} repos · adversarial methodology · phases 1–8",
        "footer_text": (
            f"Generated {date} · Deterministic projection of validated findings JSON"
        ),
        "totals": totals,
        "summary": summary,
        "summary_markdown": "\n".join(summary_lines).rstrip() + "\n",
        "poc_summary": poc,
        "recurring_patterns": pattern_items,
        "validation_corrections": [],
        "repos": repos,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--input", action="append", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--summary-output", type=pathlib.Path)
    args = parser.parse_args()
    try:
        documents = []
        for path in args.input:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError(f"{path}: input must be a JSON object")
            documents.append(value)
        report = build(documents, args.date)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        if args.summary_output is not None:
            args.summary_output.write_text(
                report["summary_markdown"], encoding="utf-8", newline="\n"
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
