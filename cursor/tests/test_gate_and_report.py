"""Cursor port: verify the portable gate scripts + report builder carry the
self-contained-PoC (schema v4), exposure, and severity-label behavior, with the
PoC tree under output/proof_of_concept/. Mirrors the Codex coverage for the
shared corpus.
Run: python3 -m pytest tests/  (or python3 -m unittest discover -s tests)
"""
import json
import os
import pathlib
import re
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
INIT = ROOT / "scripts" / "init-review.sh"
COMPLETE = ROOT / "scripts" / "complete-phase.sh"
VALIDATE = ROOT / "scripts" / "validate-phase.sh"
CHECK = ROOT / "scripts" / "check-gate.sh"
AGENT_JOURNAL = ROOT / "scripts" / "agent-journal.py"
BUILD = ROOT / "scripts" / "report" / "build.py"
BUILD_SARIF = ROOT / "scripts" / "report" / "build-sarif.py"
DATE = "2026-08-21"
VECTOR = "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N"


def candidate_text():
    return (
        "## CANDIDATE-1 — F-001\n\n"
        "Exposure: EXTERNAL\nFlow: Flow-1\n"
        "CVSS-B Score: 9.3\n"
        f"CVSS Vector: {VECTOR}\n"
        "CVSS Rationale: Fixture metrics.\n"
        f"CVSS Calculator: https://www.first.org/cvss/calculator/4.0#{VECTOR}\n"
        "Tier: P0\nFinal Disposition: CONFIRMED\n"
        "Final CVSS-B Score: 9.3\n"
        f"Final CVSS Vector: {VECTOR}\n"
        "Final CVSS Rationale: Fixture metrics.\n"
        f"Final CVSS Calculator: https://www.first.org/cvss/calculator/4.0#{VECTOR}\n"
        "Final Tier: P0\nValidation: CURRENT\n"
    )


def run(*args, **kw):
    return subprocess.run(args, text=True, capture_output=True, check=False, **kw)


def write_dataflow(ws):
    fd = ws / "findings" / "demo"
    fd.mkdir(parents=True, exist_ok=True)
    (fd / f"dataflow-{DATE}.json").write_text(json.dumps({
        "schema_version": "1", "repo_slug": "demo", "date": DATE,
        "inventory": {"languages": [], "frameworks": [], "key_dependencies": []},
        "analysis_coverage": {
            "authorization": {"status": "not-applicable", "rationale": "No authorization-sensitive path exists in this fixture.", "flow_ids": []},
            "business_logic": {"status": "not-applicable", "rationale": "No stateful business operation exists in this fixture.", "flow_ids": []},
            "cross_repository": {"status": "not-applicable", "rationale": "No outbound repository boundary exists in this fixture.", "flow_ids": []},
        },
        "relationship_context": [],
        "business_logic_invariants": [],
        "outbound_edges": [],
        "flows": [{
            "flow_id": "Flow-1", "finding_type": "flow-based",
            "exposure": "EXTERNAL",
        }],
    }))


class GateV4Tests(unittest.TestCase):
    def _init(self, base):
        ws = base / "workspace"
        env = os.environ | {"PLUGIN_ROOT": str(ROOT)}
        r = run("bash", str(INIT), "--workspace", str(ws), "--date", DATE, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        st = ws / "state.json"
        s = json.loads(st.read_text())
        s["repo_slugs"] = ["demo"]
        s["current_phase"] = "phase-6"
        s["phase_status"] = {"phase-4": "completed"}
        s["agent_journal"]["epoch_phase"] = "phase-6"
        st.write_text(json.dumps(s, indent=2) + "\n")
        intent = run(
            "python3", str(AGENT_JOURNAL), "record-intent",
            "--workspace", str(ws),
        )
        self.assertEqual(intent.returncode, 0, intent.stderr)
        return ws

    def _write_v4(self, ws, catalog_keys):
        (ws / "findings" / "demo").mkdir(parents=True, exist_ok=True)
        write_dataflow(ws)
        (ws / "findings" / "demo" / f"candidates-{DATE}.md").write_text(
            candidate_text())
        d = ws / "output" / "proof_of_concept" / "demo"
        d.mkdir(parents=True, exist_ok=True)
        (d / "F-001-mint.sh").write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            "# ⚠️  SAFETY: test environments only; this script is SELF-CONTAINED.\n"
            "# Expected observation on success: the response body is saved.\n"
            "TARGET_HOST=\"${TARGET_HOST:-}\"  # FILL: non-prod host\n"
            "AUTH_TOKEN=\"${AUTH_TOKEN:-}\"  # FILL: test credential\n"
            "SCRIPT_DIR=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")\" && pwd)\"\n"
            "POC_OUTPUT_DIR=\"${POC_OUTPUT_DIR:-$SCRIPT_DIR/output}\"\n"
            "mkdir -p \"$POC_OUTPUT_DIR\"\n"
            "curl -sS \"https://${TARGET_HOST}/mint\" -o \"$POC_OUTPUT_DIR/result.txt\"\n"
        )
        (d / f"POC-GUIDE-{DATE}.md").write_text(
            "# PoC Guide — demo\n\n## F-001 — Mint (`F-001-mint.sh`, curl-shell)\n\n"
            "| Parameter | Fill? | What it is | Used how in this PoC |\n"
            "|---|---|---|---|\n"
            "| `TARGET_HOST` | FILL (blank) | Non-prod host | Request target |\n"
            "| `AUTH_TOKEN` | FILL (blank) | Test credential | Authorization |\n"
        )
        catalog = "# REFERENCE ONLY. Scripts do NOT source this file.\n"
        for key in catalog_keys:
            catalog += (
                f"# What: fixture value\n# Used by: F-001\n# Why: exercise PoC\n{key}=\"\"\n"
            )
        (d / "poc-config.env").write_text(catalog)
        (ws / "output" / "proof_of_concept" / "run-all.sh").write_text("#!/usr/bin/env bash\n")
        (d / f"poc-manifest-{DATE}.json").write_text(json.dumps({
            "schema_version": "4", "repo_slug": "demo", "date": DATE,
            "config_path": f"output/proof_of_concept/demo/poc-config.env",
            "guide_path": f"output/proof_of_concept/demo/POC-GUIDE-{DATE}.md",
            "skipped": [],
            "pocs": [{
                "finding_id": "F-001", "tier": "P0", "disposition": "CONFIRMED",
                "title": "t",
                "script_path": "output/proof_of_concept/demo/F-001-mint.sh",
                "script_type": "curl-shell", "confirm_path": "script",
                "confirm_note": None, "placeholders": ["TARGET_HOST", "AUTH_TOKEN"],
                "generated_by": "unknown", "verdict": "VALID", "regenerated": False,
                "reviewer_envelopes": [
                    {"reviewer": reviewer, "verdict": "VALID",
                     "reasoning": "The script is accurate, safe, and complete.",
                     "accuracy_ok": True, "safety_ok": True,
                     "completeness_ok": True, "suggested_fix": None}
                    for reviewer in ("A", "B")
                ],
            }],
        }))

    def test_v4_self_contained_manifest_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._init(pathlib.Path(tmp))
            self._write_v4(ws, ("TARGET_HOST", "AUTH_TOKEN"))
            r = run("bash", str(COMPLETE), "--phase", "phase-6", "--workspace", str(ws))
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_v4_catalog_missing_key_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._init(pathlib.Path(tmp))
            self._write_v4(ws, ("TARGET_HOST",))  # AUTH_TOKEN undocumented
            r = run("bash", str(COMPLETE), "--phase", "phase-6", "--workspace", str(ws))
            self.assertEqual(r.returncode, 2)
            self.assertIn("AUTH_TOKEN", r.stderr)

    def test_phase_one_gate_greps_external_attack_surface_header(self):
        # The gate still depends on the exact '## External Attack Surface'
        # header even though the section now also holds internal surface.
        self.assertIn("## External Attack Surface", CHECK.read_text())
        self.assertIn("## External Attack Surface", VALIDATE.read_text())


class Phase7CvssGateTests(unittest.TestCase):
    """The phase-7 findings-json gate enforces cvss_score↔tier consistency."""

    def _phase7_ws(self, base, *, cvss_score, tier):
        ws = base / "workspace"
        env = os.environ | {"PLUGIN_ROOT": str(ROOT)}
        r = run("bash", str(INIT), "--workspace", str(ws), "--date", DATE, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        st = ws / "state.json"; s = json.loads(st.read_text())
        s["repo_slugs"] = ["demo"]; s["current_phase"] = "phase-7"
        s["phase_status"] = {"phase-6": "completed"}
        s["agent_journal"]["epoch_phase"] = "phase-7"
        st.write_text(json.dumps(s, indent=2) + "\n")
        intent = run(
            "python3", str(AGENT_JOURNAL), "record-intent",
            "--workspace", str(ws),
        )
        self.assertEqual(intent.returncode, 0, intent.stderr)
        output = ws / "output"; output.mkdir(parents=True, exist_ok=True)
        (output / f"run-log-{DATE}.md").write_text(
            f"[{DATE}T12:00:00Z] demo: multitenant-scope=YES — fixture\n")
        fd = ws / "findings" / "demo"; fd.mkdir(parents=True, exist_ok=True)
        write_dataflow(ws)
        (fd / f"candidates-{DATE}.md").write_text(candidate_text())
        (fd / f"findings-{DATE}.md").write_text(
            "# Findings\n\n## Findings Summary\n\n| Tier | Count |\n|---|---|\n| P0 | 1 |\n")
        receipt = {
            "schema_version": "1", "repo_slug": "demo",
            "repo_url": "https://github.com/org/demo", "commit_sha": "a" * 40,
            "multitenant_scope": True, "review_date": DATE,
            "analyzed_files": [],
            "findings": [{
                "id": "F-001", "tier_final": tier,
                "scoring_final": {
                    "cvss_score": cvss_score, "cvss_vector": VECTOR, "tier": tier,
                },
            }],
        }
        (fd / f"receipt-{DATE}.json").write_text(json.dumps(receipt))
        (fd / f"pattern-tags-{DATE}.json").write_text(
            json.dumps({"schema_version": "1", "repo_slug": "demo", "tags": []}))
        finding = {
            "finding_id": "F-001", "tier": tier,
            "cvss_score": cvss_score,
            "cvss_vector": VECTOR,
            "cwe": "CWE-639", "cve": None, "exposure": "EXTERNAL",
            "reachability": "Any network caller.", "exploitability": "One request.",
            "impact": "Cross-tenant read.", "patch_status": "No patch.",
            "title": "t", "category": "c", "location": "a:1",
            "what_happens_md": "w", "attack_steps_md": "1. x",
            "evidence_md": "```\ny\n```", "fix_md": "f", "severity_md": "- s",
            "validation": "CURRENT",
            "poc": {
                "skipped": True, "reason": "generation-failed",
                "note": "fixture generation failure",
            },
            "references": ["https://example.com/advisory"],
        }
        (fd / f"findings-{DATE}.json").write_text(json.dumps(
            {"schema_version": "2", "repo_slug": "demo", "repo_url": "https://github.com/org/demo", "commit_sha": "a"*40, "multitenant_scope": True, "date": DATE,
             "dismissed_findings": [], "poc_coverage": {"generated": 0, "skipped": 1},
             "methodology_notes": ["Fixture review."], "disclaimer": "Fixture disclaimer.",
             "findings": [finding]}))
        markdown = run(
            "python3", str(ROOT / "scripts" / "report" / "build-markdown.py"),
            "--input", str(fd / f"findings-{DATE}.json"),
            "--output", str(fd / f"findings-{DATE}.md"),
        )
        self.assertEqual(markdown.returncode, 0, markdown.stderr)
        poc_dir = output / "proof_of_concept" / "demo"
        poc_dir.mkdir(parents=True, exist_ok=True)
        (poc_dir / f"poc-manifest-{DATE}.json").write_text(json.dumps({
            "schema_version": "4", "repo_slug": "demo", "date": DATE,
            "config_path": None, "guide_path": None, "pocs": [],
            "skipped": [{
                "finding_id": "F-001", "tier": "P0",
                "disposition": "CONFIRMED", "reason": "generation-failed",
                "note": "fixture generation failure",
            }],
        }))
        # The phase-7 gate now also requires the SARIF projection built
        # mechanically from findings-<DATE>.json by build-sarif.py.
        sr = run("python3", str(BUILD_SARIF),
                 "--input", str(fd / f"findings-{DATE}.json"),
                 "--output", str(fd / f"findings-{DATE}.sarif"))
        self.assertEqual(sr.returncode, 0, sr.stderr)
        return ws

    def test_consistent_cvss_and_tier_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._phase7_ws(pathlib.Path(tmp), cvss_score=9.3, tier="P0")
            r = run("bash", str(COMPLETE), "--phase", "phase-7", "--workspace", str(ws))
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_tier_score_mismatch_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._phase7_ws(pathlib.Path(tmp), cvss_score=4.2, tier="P0")
            r = run("bash", str(COMPLETE), "--phase", "phase-7", "--workspace", str(ws))
            self.assertEqual(r.returncode, 2)
            self.assertIn("tier band", r.stderr)

    def test_out_of_range_score_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._phase7_ws(pathlib.Path(tmp), cvss_score=11.5, tier="P0")
            r = run("bash", str(COMPLETE), "--phase", "phase-7", "--workspace", str(ws))
            self.assertEqual(r.returncode, 2)
            self.assertIn("0.0-10.0", r.stderr)


class ReportTests(unittest.TestCase):
    REPORT = {
        "report_title": "R", "report_subtitle": "", "footer_text": "",
        "totals": {"P0": 1, "P1": 0, "P2": 0, "P3": 1, "P4": 0, "clean_repos": 0},
        "summary": {"headline_md": "h"},
        "repos": [{
            "slug": "demo", "most_severe": "P0", "summary_note": "n",
            "counts": {"P0": 1, "P1": 0, "P2": 0, "P3": 1, "P4": 0},
            "findings": [
                {"finding_id": "F-001", "tier": "P0", "title": "ext", "category": "c",
                 "exposure": "EXTERNAL", "location": "a:1", "what_happens_md": "w",
                 "cvss_score": 9.8,
                 "cvss_vector": "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N",
                 "cwe": "CWE-798",
                 "attack_steps_md": "1. x", "evidence_md": "```\ny\n```",
                 "fix_md": "f", "severity_md": "- s",
                 "poc": {"script_path": "output/proof_of_concept/demo/F-001.sh",
                         "script_type": "curl-shell", "confirm_path": "script",
                         "verdict": "VALID",
                         "guide_path": "output/proof_of_concept/demo/POC-GUIDE-2026-08-21.md",
                         "config_path": "output/proof_of_concept/demo/poc-config.env",
                         "placeholders": ["TARGET_HOST"]}},
                {"finding_id": "F-002", "tier": "P3", "title": "int", "category": "c",
                 "exposure": "INTERNAL-RESTRICTED", "location": "b:2", "what_happens_md": "w",
                 "attack_steps_md": "1. x", "evidence_md": "```\ny\n```",
                 "fix_md": "f", "severity_md": "- s",
                 "poc": {"skipped": True, "reason": "not-eligible-tier", "note": "P3"}},
            ],
        }],
    }

    def _build(self):
        with tempfile.TemporaryDirectory() as tmp:
            inp = pathlib.Path(tmp) / "in.json"
            out = pathlib.Path(tmp) / "r.html"
            inp.write_text(json.dumps(self.REPORT))
            r = run("python3", str(BUILD), "--input", str(inp), "--output", str(out))
            self.assertEqual(r.returncode, 0, r.stderr)
            return out.read_text()

    def test_severity_words_and_exposure_and_relative_href(self):
        html = self._build()
        self.assertIn("P0 (Critical)", html)
        self.assertIn("P3 (Low)", html)
        m = re.search(r"REPO_DETAIL = (\{.*?\});", html, re.DOTALL)
        detail = json.loads(m.group(1).replace("<\\/", "</"))["demo"]
        self.assertIn("exposure-external", detail)
        self.assertIn("exposure-internal-restricted", detail)
        self.assertIn('href="proof_of_concept/demo/F-001.sh"', detail)
        self.assertNotIn('href="../proof_of_concept', detail)

    def test_cvss_score_vector_calculator_and_cwe_render(self):
        html = self._build()
        m = re.search(r"REPO_DETAIL = (\{.*?\});", html, re.DOTALL)
        detail = json.loads(m.group(1).replace("<\\/", "</"))["demo"]
        self.assertIn("CVSS v4.0 Base:", detail)
        self.assertIn("9.8", detail)
        self.assertIn("CVSS:4.0/AV:N/AC:L", detail)
        self.assertIn("first.org/cvss/calculator/4.0#CVSS:4.0/AV:N", detail)
        self.assertIn("CWE-798", detail)

    def test_cvss_policy_reference_and_phase3_use_cvss(self):
        policy = ROOT / "references" / "cvss-policy.md"
        self.assertTrue(policy.exists())
        self.assertIn("CVSS v4.0 Base", policy.read_text())
        phase3 = (ROOT / "phases" / "phase-3.md").read_text()
        self.assertIn("CVSS v4.0 Base", phase3)
        self.assertNotIn("## Severity Rubric", phase3)
        self.assertNotIn("Total = D1 + D2 + D3 + D4", phase3)


class JournalGateTests(unittest.TestCase):
    def test_journal_direct_write_is_denied_including_phase_eight(self):
        """The phase-8 output allowlist cannot bypass the append-only journal."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "workspace"
            env = os.environ | {"PLUGIN_ROOT": str(ROOT)}
            initialized = run(
                "bash", str(INIT), "--workspace", str(workspace), "--date", DATE,
                env=env,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            state_path = workspace / "state.json"
            for phase in ("phase-0", "phase-4", "phase-8"):
                state = json.loads(state_path.read_text())
                state["current_phase"] = phase
                state_path.write_text(json.dumps(state, indent=2) + "\n")
                result = run(
                    "bash", str(CHECK), "--check-path",
                    f"output/agent-conversation-{DATE}.md",
                    "--workspace", str(workspace),
                )
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("agent-journal.py record", result.stderr)


def _doc(**ov):
    d = {"schema_version": "2", "repo_slug": "org-demo",
         "repo_url": "https://github.com/org/demo", "commit_sha": "a" * 40,
         "multitenant_scope": True, "date": "2026-08-25", "findings": [
             {"finding_id": "F-001", "title": "IDOR", "tier": "P0", "cvss_score": 9.3,
              "cvss_vector": "CVSS:4.0/AV:N/AC:L/AT:N/PR:L/UI:N/VC:H/VI:N/VA:N/SC:H/SI:N/SA:N",
              "cwe": "CWE-639", "cve": "n/a", "exposure": "EXTERNAL", "location": "a.java:88-140"},
             {"finding_id": "F-002", "title": "Leak", "tier": "P3", "cvss_score": 3.1,
              "cvss_vector": "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:L/VI:N/VA:N/SC:N/SI:N/SA:N",
              "cwe": "CWE-209", "cve": "n/a", "exposure": "INTERNAL", "location": "b.py:12"}]}
    d.update(ov)
    return d


def _sarif(*docs):
    with tempfile.TemporaryDirectory() as t:
        t = pathlib.Path(t)
        args = ["python3", str(BUILD_SARIF)]
        for i, d in enumerate(docs):
            p = t / f"f{i}.json"
            p.write_text(json.dumps(d))
            args += ["--input", str(p)]
        o = t / "o.sarif"
        args += ["--output", str(o)]
        r = subprocess.run(args, text=True, capture_output=True)
        if r.returncode:
            raise AssertionError(r.stderr or r.stdout)
        return json.loads(o.read_text())


class SarifTests(unittest.TestCase):
    def test_version(self):
        self.assertEqual(_sarif(_doc())["version"], "2.1.0")

    def test_run_per_input(self):
        self.assertEqual(len(_sarif(_doc(), _doc())["runs"]), 2)

    def test_level_from_tier(self):
        run = _sarif(_doc())["runs"][0]
        by = {r["properties"]["finding_id"]: r for r in run["results"]}
        self.assertEqual(by["F-001"]["level"], "error")
        self.assertEqual(by["F-002"]["level"], "note")

    def test_security_severity(self):
        run = _sarif(_doc())["runs"][0]
        by = {r["properties"]["finding_id"]: r for r in run["results"]}
        self.assertEqual(by["F-001"]["properties"]["security-severity"], "9.3")

    def test_vcp(self):
        vcp = _sarif(_doc())["runs"][0]["versionControlProvenance"][0]
        self.assertEqual(vcp["repositoryUri"], "https://github.com/org/demo")
        self.assertEqual(vcp["revisionId"], "a" * 40)

    def test_triage_sha_no_revision(self):
        vcp = _sarif(_doc(commit_sha="n/a (triage)"))["runs"][0]["versionControlProvenance"][0]
        self.assertNotIn("revisionId", vcp)


if __name__ == "__main__":
    unittest.main()
