import json
import pathlib
import re
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD = ROOT / "scripts" / "report" / "build.py"
FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "executive-summary.json"


def build_report_from(report: dict) -> str:
    """Run build.py on an inline report dict and return the HTML."""
    with tempfile.TemporaryDirectory() as tmp:
        inp = pathlib.Path(tmp) / "in.json"
        out = pathlib.Path(tmp) / "report.html"
        inp.write_text(json.dumps(report))
        result = subprocess.run(
            ["python3", str(BUILD), "--input", str(inp), "--output", str(out)],
            text=True, capture_output=True, check=False,
        )
        if result.returncode:
            raise AssertionError(result.stderr or result.stdout)
        return out.read_text()


def build_report() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        output = pathlib.Path(tmp) / "report.html"
        result = subprocess.run(
            ["python3", str(BUILD), "--input", str(FIXTURE), "--output", str(output)],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise AssertionError(result.stderr or result.stdout)
        return output.read_text()


class ReportSafetyTests(unittest.TestCase):
    def test_report_build_is_deterministic_and_escapes_script_terminator(self):
        first = build_report()
        second = build_report()
        self.assertEqual(first, second)
        self.assertNotIn("</script><script>alert(1)</script>", first)
        self.assertIn("NightFalcon", first)

    def _report(self):
        return {
            "report_title": "R", "report_subtitle": "", "footer_text": "",
            "totals": {"P0": 1, "P1": 0, "P2": 0, "P3": 1, "P4": 0, "clean_repos": 0},
            "summary": {"headline_md": "h"},
            "repos": [{
                "slug": "demo", "most_severe": "P0", "summary_note": "n",
                "counts": {"P0": 1, "P1": 0, "P2": 0, "P3": 1, "P4": 0},
                "findings": [
                    {"finding_id": "F-001", "tier": "P0", "title": "ext finding",
                     "category": "c", "exposure": "EXTERNAL", "location": "a.js:1",
                     "cvss_score": 9.8,
                     "cvss_vector": "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N",
                     "cwe": "CWE-798",
                     "what_happens_md": "w", "attack_steps_md": "1. x",
                     "evidence_md": "```\ny\n```", "fix_md": "f", "severity_md": "- s",
                     "poc": {"script_path": "output/proof_of_concept/demo/F-001.sh",
                             "script_type": "curl-shell", "confirm_path": "script",
                             "verdict": "VALID",
                             "guide_path": "output/proof_of_concept/demo/POC-GUIDE-2026-07-14.md",
                             "config_path": "output/proof_of_concept/demo/poc-config.env",
                             "placeholders": ["TARGET_HOST"]}},
                    {"finding_id": "F-002", "tier": "P3", "title": "int finding",
                     "category": "c", "exposure": "INTERNAL-RESTRICTED", "location": "b.js:2",
                     "what_happens_md": "w", "attack_steps_md": "1. x",
                     "evidence_md": "```\ny\n```", "fix_md": "f", "severity_md": "- s",
                     "poc": {"skipped": True, "reason": "not-eligible-tier", "note": "P3"}},
                ],
            }],
        }

    def test_severity_words_render_as_display_labels(self):
        html = build_report_from(self._report())
        self.assertIn("P0 (Critical)", html)
        self.assertIn("P3 (Low)", html)

    def test_exposure_badges_render(self):
        # Finding bodies are embedded in the REPO_DETAIL JSON literal; unescape it.
        html = build_report_from(self._report())
        m = re.search(r"REPO_DETAIL = (\{.*?\});", html, re.DOTALL)
        blob = m.group(1).replace("<\\/", "</")
        detail = json.loads(blob)["demo"]
        self.assertIn("exposure-external", detail)
        self.assertIn("exposure-internal-restricted", detail)

    def test_poc_links_are_report_relative_no_dotdot(self):
        html = build_report_from(self._report())
        m = re.search(r"REPO_DETAIL = (\{.*?\});", html, re.DOTALL)
        detail = json.loads(m.group(1).replace("<\\/", "</"))["demo"]
        # Report lives in output/, PoC under output/proof_of_concept/ → same-dir.
        self.assertIn('href="proof_of_concept/demo/F-001.sh"', detail)
        self.assertNotIn('href="../proof_of_concept', detail)

    def test_cvss_score_vector_calculator_link_and_cwe_render(self):
        html = build_report_from(self._report())
        m = re.search(r"REPO_DETAIL = (\{.*?\});", html, re.DOTALL)
        detail = json.loads(m.group(1).replace("<\\/", "</"))["demo"]
        self.assertIn("CVSS v4.0 Base:", detail)
        self.assertIn("9.8", detail)
        self.assertIn("CVSS:4.0/AV:N/AC:L", detail)
        self.assertIn("first.org/cvss/calculator/4.0#CVSS:4.0/AV:N", detail)
        self.assertIn("CWE-798", detail)

    def test_finding_without_cvss_still_renders(self):
        # Non-Regression: a finding lacking cvss fields must not break the build.
        rep = self._report()
        for k in ("cvss_score", "cvss_vector", "cwe"):
            rep["repos"][0]["findings"][0].pop(k, None)
        html = build_report_from(rep)  # must not raise
        self.assertIn("ext finding", html)

    def test_malformed_cvss_vector_omits_calculator_link(self):
        rep = self._report()
        rep["repos"][0]["findings"][0]["cvss_vector"] = "not-a-vector"
        html = build_report_from(rep)
        m = re.search(r"REPO_DETAIL = (\{.*?\});", html, re.DOTALL)
        detail = json.loads(m.group(1).replace("<\\/", "</"))["demo"]
        # score still shown; no calculator link for a bad vector.
        self.assertIn("9.8", detail)
        self.assertNotIn("calculator/4.0#not-a-vector", detail)


if __name__ == "__main__":
    unittest.main()
