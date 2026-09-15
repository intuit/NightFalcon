import copy
import json
import pathlib
import subprocess
import tempfile
import unittest

from tests import initialize_journal_fixture


ROOT = pathlib.Path(__file__).resolve().parents[1]
DATE = "2026-08-25"
PORTS = {
    "claude": ROOT / "claude",
    "codex": ROOT / "codex" / "plugins" / "nightfalcon",
    "cursor": ROOT / "cursor",
}
AGENT_JOURNAL = PORTS["claude"] / "scripts" / "agent-journal.py"
SKILLS = {
    "claude": PORTS["claude"] / "skills" / "nightfalcon" / "SKILL.md",
    "codex": PORTS["codex"] / "skills" / "nightfalcon" / "SKILL.md",
    "cursor": PORTS["cursor"] / ".cursor" / "skills" / "nightfalcon" / "SKILL.md",
}
VECTOR_9_3 = (
    "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/"
    "VC:H/VI:H/VA:H/SC:N/SI:N/SA:N"
)
VECTOR_1_0 = (
    "CVSS:4.0/AV:P/AC:H/AT:P/PR:H/UI:A/"
    "VC:L/VI:N/VA:N/SC:N/SI:N/SA:N"
)
VECTOR_7_1 = (
    "CVSS:4.0/AV:N/AC:L/AT:N/PR:L/UI:N/"
    "VC:H/VI:N/VA:N/SC:N/SI:N/SA:N"
)


def run(*args):
    return subprocess.run(args, text=True, capture_output=True)


def json_mutate(path, mutate):
    document = json.loads(path.read_text())
    mutate(document)
    path.write_text(json.dumps(document) + "\n")


def write_state(workspace, phase, slugs=("demo",), mode="review"):
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "state.json").write_text(
        json.dumps(
            {
                "mode": mode,
                "date": DATE,
                "repo_slugs": list(slugs),
                "current_phase": phase,
                "phase_status": {},
            }
        )
        + "\n"
    )
    initialize_journal_fixture(workspace, DATE, AGENT_JOURNAL)


def finding(slug="demo"):
    return {
        "finding_id": "F-001",
        "tier": "P0",
        "cvss_score": 9.3,
        "cvss_vector": VECTOR_9_3,
        "cwe": "CWE-639",
        "cve": None,
        "exposure": "EXTERNAL",
        "reachability": "Any unauthenticated network caller can reach it.",
        "exploitability": "One request; no special condition is required.",
        "impact": "Attacker can read another tenant's records.",
        "patch_status": "No patch exists in this repository.",
        "title": f"Cross-tenant read in {slug}",
        "category": "Broken access control",
        "location": "src/Handler.java:42-51",
        "what_happens_md": "A customer can read another tenant's records.",
        "attack_steps_md": "1. Send another tenant ID.",
        "evidence_md": "```java\nreturn load(id);\n```",
        "fix_md": "1. Verify tenant ownership.",
        "severity_md": (
            "- **Reachability.** Any network caller.\n"
            "- **Impact on success.** Cross-tenant read.\n"
            "- **Exploit difficulty.** One request.\n"
            "- **Patch status.** No patch in this repository."
        ),
        "validation": "CURRENT",
        "references": [
            "https://owasp.org/Top10/A01_2021-Broken_Access_Control/",
            "src/Handler.java:42-51",
        ],
        "controls_in_scope": [
            {
                "name": "Authorization Control",
                "control_id": "CTRL-1234",
                "note_path": "references/organization_context/controls/authorization.md",
            }
        ],
        "applicable_policies": [
            {
                "id": "STD-0054",
                "title": "Encryption key management",
                "binding_statement": "Keys must be managed by the approved platform.",
                "note_path": "references/organization_context/standards/encryption-key-management.md",
            }
        ],
        "poc": {
            "skipped": True,
            "reason": "generation-failed",
            "note": "fixture generation failure",
        },
    }


def findings_doc(slug="demo"):
    return {
        "schema_version": "2",
        "repo_slug": slug,
        "repo_url": f"https://github.com/org/{slug}",
        "commit_sha": "a" * 40,
        "multitenant_scope": True,
        "date": DATE,
        "dismissed_findings": [],
        "poc_coverage": {"generated": 0, "skipped": 1},
        "methodology_notes": [
            "Validated candidates were debated and checked against repository evidence."
        ],
        "disclaimer": (
            "This review is evidence-based and does not guarantee the absence of defects."
        ),
        "findings": [finding(slug)],
    }


def receipt_doc(slug="demo"):
    return {
        "schema_version": "1",
        "repo_slug": slug,
        "repo_url": f"https://github.com/org/{slug}",
        "commit_sha": "a" * 40,
        "multitenant_scope": True,
        "review_date": DATE,
        "analyzed_files": [],
        "findings": [
            {
                "id": "F-001",
                "tier_final": "P0",
                "scoring_final": {
                    "cvss_score": 9.3,
                    "cvss_vector": VECTOR_9_3,
                    "tier": "P0",
                },
            }
        ],
    }


def dataflow_doc(slug="demo", *, exposure="EXTERNAL"):
    return {
        "schema_version": "1",
        "repo_slug": slug,
        "date": DATE,
        "inventory": {"languages": [], "frameworks": [], "key_dependencies": []},
        "relationship_context": [],
        "business_logic_invariants": [],
        "outbound_edges": [],
        "analysis_coverage": {
            "authorization": {
                "status": "not-applicable",
                "rationale": "Fixture contains no authorization relationship records.",
                "flow_ids": [],
            },
            "business_logic": {
                "status": "not-applicable",
                "rationale": "Fixture contains no business-logic invariant records.",
                "flow_ids": [],
            },
            "cross_repository": {
                "status": "not-applicable",
                "rationale": "Fixture contains no cross-repository dependency edges.",
                "flow_ids": [],
            },
        },
        "flows": [
            {
                "flow_id": "Flow-1",
                "finding_type": "flow-based",
                "exposure": exposure,
            }
        ],
    }


def write_dataflow(workspace, *, exposure="EXTERNAL"):
    fd = workspace / "findings" / "demo"
    fd.mkdir(parents=True, exist_ok=True)
    (fd / f"dataflow-{DATE}.json").write_text(
        json.dumps(dataflow_doc(exposure=exposure)) + "\n"
    )


def candidate_record(
    *,
    fid="F-001",
    exposure="EXTERNAL",
    flow="Flow-1",
    disposition="CONFIRMED",
    include_final_cvss=True,
    score=9.3,
    vector=VECTOR_9_3,
    tier="P0",
    dismissed_title=None,
    dismissal_reason=None,
    validation="CURRENT",
):
    final = ""
    if include_final_cvss:
        final = (
            f"Final CVSS-B Score: {score:.1f}\n"
            f"Final CVSS Vector: {vector}\n"
            "Final CVSS Rationale: Network reachable with high vulnerable-system impact.\n"
            f"Final CVSS Calculator: https://www.first.org/cvss/calculator/4.0#{vector}\n"
        )
    dismissed_fields = ""
    if disposition == "DISMISSED":
        dismissed_fields = (
            f"Dismissed Finding Title: {dismissed_title or 'Admin-only diagnostic path'}\n"
            "Dismissal Reason: "
            f"{dismissal_reason or 'The route is unreachable outside the trusted admin network.'}\n"
        )
    validation_field = "" if validation is None else f"Validation: {validation}\n"
    return (
        f"## CANDIDATE-{fid}\n\n"
        f"Finding ID: {fid}\n"
        f"Exposure: {exposure}\n"
        f"Flow: {flow}\n"
        f"CVSS-B Score: {score:.1f}\n"
        f"CVSS Vector: {vector}\n"
        "CVSS Rationale: Network reachable with high vulnerable-system impact.\n"
        f"CVSS Calculator: https://www.first.org/cvss/calculator/4.0#{vector}\n"
        f"Tier: {tier}\n"
        f"Debate required: {'YES' if tier in {'P0', 'P1', 'P2'} else 'NO'}\n"
        f"Final Disposition: {disposition}\n"
        f"{dismissed_fields}"
        f"{final}"
        f"Final Tier: {tier}\n"
        f"{validation_field}"
    )


def manifest_doc(*, skipped=None, pocs=None, slug="demo"):
    return {
        "schema_version": "4",
        "repo_slug": slug,
        "date": DATE,
        "config_path": None,
        "guide_path": None,
        "pocs": [] if pocs is None else pocs,
        "skipped": (
            [
                {
                    "finding_id": "F-001",
                    "tier": "P0",
                    "disposition": "CONFIRMED",
                    "reason": "generation-failed",
                    "note": "fixture generation failure",
                }
            ]
            if skipped is None
            else skipped
        ),
    }


def reviewer_envelope(label="A", verdict="VALID"):
    return {
        "reviewer": label,
        "verdict": verdict,
        "reasoning": "The script accurately and safely demonstrates the candidate claim.",
        "accuracy_ok": True,
        "safety_ok": True,
        "completeness_ok": True,
        "suggested_fix": None,
    }


def generated_poc_manifest(slug="demo"):
    manifest = manifest_doc(
        slug=slug,
        skipped=[],
        pocs=[
            {
                "finding_id": "F-001",
                "tier": "P0",
                "disposition": "CONFIRMED",
                "title": f"Cross-tenant read in {slug}",
                "script_path": f"output/proof_of_concept/{slug}/F-001-read.sh",
                "script_type": "curl-shell",
                "confirm_path": "script",
                "confirm_note": None,
                "placeholders": ["TARGET_HOST"],
                "generated_by": "unknown",
                "verdict": "VALID",
                "regenerated": False,
                "reviewer_envelopes": [reviewer_envelope("A"), reviewer_envelope("B")],
            }
        ],
    )
    manifest["config_path"] = f"output/proof_of_concept/{slug}/poc-config.env"
    manifest["guide_path"] = (
        f"output/proof_of_concept/{slug}/POC-GUIDE-{DATE}.md"
    )
    return manifest


def write_generated_poc_artifacts(workspace, slug="demo"):
    poc_dir = workspace / "output" / "proof_of_concept" / slug
    poc_dir.mkdir(parents=True, exist_ok=True)
    (poc_dir / "F-001-read.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "# ⚠️  SAFETY: test environments only; this script is SELF-CONTAINED.\n"
        "# Expected observation on success: the response body is saved.\n"
        "TARGET_HOST=\"${TARGET_HOST:-}\"  # FILL: non-prod host\n"
        "[[ -n \"$TARGET_HOST\" ]] || { echo 'TARGET_HOST required' >&2; exit 1; }\n"
        "SCRIPT_DIR=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")\" && pwd)\"\n"
        "POC_OUTPUT_DIR=\"${POC_OUTPUT_DIR:-$SCRIPT_DIR/output}\"\n"
        "mkdir -p \"$POC_OUTPUT_DIR\"\n"
        "curl -sS \"https://${TARGET_HOST}/records\" -o \"$POC_OUTPUT_DIR/result.txt\"\n"
    )
    (poc_dir / f"POC-GUIDE-{DATE}.md").write_text(
        "# PoC Guide — fixture\n\n"
        "Every PoC is self-contained. The catalog is reference-only and never sourced.\n\n"
        "## F-001 — Cross-tenant read (`F-001-read.sh`, curl-shell)\n\n"
        "| Parameter | Fill? | What it is | Used how in this PoC |\n"
        "|---|---|---|---|\n"
        "| `TARGET_HOST` | FILL (blank) | Non-prod host | Request target |\n"
    )
    (poc_dir / "poc-config.env").write_text(
        "# REFERENCE ONLY. Scripts do NOT source this file.\n"
        "# What: non-prod host\n# Used by: F-001\n# Why: request target\nTARGET_HOST=\"\"\n"
    )
    root = workspace / "output" / "proof_of_concept"
    (root / "run-all.sh").write_text("#!/usr/bin/env bash\n")
    return poc_dir


def dismissed_contract():
    title = "Admin-only diagnostic path"
    reason = "The route is unreachable outside the trusted admin network."
    candidates = candidate_record() + candidate_record(
        fid="F-099",
        disposition="DISMISSED",
        validation="NOT-RUN",
        dismissed_title=title,
        dismissal_reason=reason,
    )
    manifest = manifest_doc(
        skipped=manifest_doc()["skipped"]
        + [
            {
                "finding_id": "F-099",
                "tier": "P0",
                "disposition": "DISMISSED",
                "reason": "not-eligible-disposition",
                "note": "Dismissed during adversarial debate.",
            }
        ]
    )
    item = {"finding_id": "F-099", "title": title, "reason": reason}
    return candidates, manifest, item


def write_phase7_workspace(
    port,
    workspace,
    doc=None,
    receipt=None,
    mode="review",
    candidates_text=None,
    manifest=None,
):
    write_state(workspace, "phase-7", mode=mode)
    receipt = receipt if receipt is not None else receipt_doc()
    if mode == "review":
        output = workspace / "output"
        output.mkdir(parents=True, exist_ok=True)
        run_log = output / f"run-log-{DATE}.md"
        if not run_log.exists():
            token = "YES" if receipt.get("multitenant_scope") in (True, "YES") else "NO"
            run_log.write_text(
                f"[2026-08-25T12:00:00Z] demo: multitenant-scope={token} — fixture\n"
            )
    fd = workspace / "findings" / "demo"
    fd.mkdir(parents=True)
    if mode == "review":
        write_dataflow(workspace)
    (fd / f"candidates-{DATE}.md").write_text(
        candidate_record() if candidates_text is None else candidates_text
    )
    poc_dir = workspace / "output" / "proof_of_concept" / "demo"
    poc_dir.mkdir(parents=True, exist_ok=True)
    (poc_dir / f"poc-manifest-{DATE}.json").write_text(
        json.dumps(manifest_doc() if manifest is None else manifest) + "\n"
    )
    (fd / f"receipt-{DATE}.json").write_text(
        json.dumps(receipt) + "\n"
    )
    (fd / f"pattern-tags-{DATE}.json").write_text(
        json.dumps({"schema_version": "1", "repo_slug": "demo", "tags": []}) + "\n"
    )
    doc = copy.deepcopy(doc if doc is not None else findings_doc())
    findings_path = fd / f"findings-{DATE}.json"
    findings_path.write_text(json.dumps(doc) + "\n")
    markdown_result = run(
        "python3",
        str(port / "scripts" / "report" / "build-markdown.py"),
        "--input",
        str(findings_path),
        "--output",
        str(fd / f"findings-{DATE}.md"),
    )
    if markdown_result.returncode:
        # Malformed-input tests deliberately construct JSON the renderer rejects.
        # The phase gate validates JSON before comparing this placeholder.
        (fd / f"findings-{DATE}.md").write_text("# invalid fixture\n")
    result = run(
        "python3",
        str(port / "scripts" / "report" / "build-sarif.py"),
        "--input",
        str(findings_path),
        "--output",
        str(fd / f"findings-{DATE}.sarif"),
    )
    if result.returncode:
        raise AssertionError(result.stderr)


def complete(port, workspace, phase):
    return run(
        "bash",
        str(port / "scripts" / "complete-phase.sh"),
        "--phase",
        phase,
        "--workspace",
        str(workspace),
    )


def write_phase8_workspace(port, workspace, slugs=("demo",), duplicate_runs=False):
    write_state(workspace, "phase-8", slugs=slugs)
    output = workspace / "output"
    output.mkdir(parents=True, exist_ok=True)
    input_paths = []
    for slug in slugs:
        doc = findings_doc(slug)
        fd = workspace / "findings" / slug
        fd.mkdir(parents=True)
        (fd / f"dataflow-{DATE}.json").write_text(
            json.dumps(dataflow_doc(slug=slug)) + "\n"
        )
        (fd / f"candidates-{DATE}.md").write_text(candidate_record())
        (fd / f"receipt-{DATE}.json").write_text(
            json.dumps(receipt_doc(slug)) + "\n"
        )
        poc_dir = workspace / "output" / "proof_of_concept" / slug
        poc_dir.mkdir(parents=True, exist_ok=True)
        (poc_dir / f"poc-manifest-{DATE}.json").write_text(
            json.dumps(manifest_doc(slug=slug)) + "\n"
        )
        fp = fd / f"findings-{DATE}.json"
        fp.write_text(json.dumps(doc) + "\n")
        input_paths.append(fp)
    (output / f"run-log-{DATE}.md").write_text(
        "".join(
            f"[2026-08-25T12:00:00Z] {slug}: multitenant-scope=YES — fixture\n"
            for slug in slugs
        )
    )
    report_path = output / f"executive-report-{DATE}.json"
    executive_args = [
        "python3",
        str(port / "scripts" / "report" / "build-executive.py"),
        "--date",
        DATE,
    ]
    for path in input_paths:
        executive_args += ["--input", str(path)]
    executive_args += [
        "--output",
        str(report_path),
        "--summary-output",
        str(output / f"executive-summary-{DATE}.md"),
    ]
    executive_result = run(*executive_args)
    if executive_result.returncode:
        raise AssertionError(executive_result.stderr)
    html_result = run(
        "python3",
        str(port / "scripts" / "report" / "build.py"),
        "--input",
        str(report_path),
        "--output",
        str(output / f"executive-report-{DATE}.html"),
    )
    if html_result.returncode:
        raise AssertionError(html_result.stderr)
    sarif_args = ["python3", str(port / "scripts" / "report" / "build-sarif.py")]
    selected = [input_paths[0]] * len(input_paths) if duplicate_runs else input_paths
    for path in selected:
        sarif_args += ["--input", str(path)]
    sarif_args += ["--output", str(output / f"executive-report-{DATE}.sarif")]
    sarif_result = run(*sarif_args)
    if sarif_result.returncode:
        raise AssertionError(sarif_result.stderr)
