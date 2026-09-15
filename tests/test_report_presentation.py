import copy
import json
import pathlib
import re
import subprocess
import tempfile
import unittest

from tests.journal_reporting_fixture import findings_doc


ROOT = pathlib.Path(__file__).resolve().parents[1]
PORTS = {
    "claude": ROOT / "claude",
    "codex": ROOT / "codex" / "plugins" / "nightfalcon",
    "cursor": ROOT / "cursor",
}
TIERS = ("P0", "P1", "P2", "P3", "P4")


def run_script(script: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(script), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def mixed_severity_document() -> dict:
    document = findings_doc()
    base = document["findings"][0]
    findings = []
    for index, tier in enumerate(("P3", "P1", "P4", "P0", "P2"), start=1):
        finding = copy.deepcopy(base)
        finding["finding_id"] = f"F-{index:03d}"
        finding["tier"] = tier
        finding["title"] = f"{tier} finding"
        finding["cvss_score"] = {
            "P0": 9.3,
            "P1": 8.0,
            "P2": 6.0,
            "P3": 3.0,
            "P4": 0.0,
        }[tier]
        findings.append(finding)
    document["findings"] = findings
    document["poc_coverage"] = {"generated": 0, "skipped": len(findings)}
    return document


def unique_severity_document() -> dict:
    document = findings_doc()
    finding = document["findings"][0]
    values = {
        "reachability": "REACHABILITY-UNIQUE",
        "exploitability": "EXPLOITABILITY-UNIQUE",
        "impact": "IMPACT-UNIQUE",
        "patch_status": "PATCH-STATUS-UNIQUE",
    }
    finding.update(values)
    finding["severity_md"] = (
        f"- **Reachability.** {values['reachability']}\n"
        f"- **Exploitability.** {values['exploitability']}\n"
        f"- **Impact on success.** {values['impact']}\n"
        f"- **Patch status.** {values['patch_status']}"
    )
    return document


def detailed_summary_document() -> dict:
    document = mixed_severity_document()
    document["dismissed_findings"] = [
        {
            "finding_id": "F-099",
            "title": "Rejected fixture candidate",
            "reason": "Repository evidence proves the candidate path is unreachable.",
        }
    ]
    for index, finding in enumerate(document["findings"][:2], start=1):
        finding["category"] = "Repeated authorization weakness"
        finding["fix_md"] = f"Apply coordinated control FIX-{index}."
        finding["poc"] = {
            "script_path": f"output/proof_of_concept/demo/F-00{index}.sh",
            "guide_path": "output/proof_of_concept/demo/POC-GUIDE.md",
            "verdict": "VALID" if index == 1 else "NEEDS-REVISION",
        }
    document["poc_coverage"] = {"generated": 2, "skipped": 3}
    return document


class ReportPresentationTests(unittest.TestCase):
    def _build_outputs(self, port: pathlib.Path, document: dict, directory: pathlib.Path):
        source = directory / "findings.json"
        executive = directory / "executive.json"
        summary = directory / "summary.md"
        html = directory / "report.html"
        markdown = directory / "findings.md"
        source.write_text(json.dumps(document) + "\n", encoding="utf-8")

        result = run_script(
            port / "scripts" / "report" / "build-executive.py",
            "--date", document["date"],
            "--input", str(source),
            "--output", str(executive),
            "--summary-output", str(summary),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        result = run_script(
            port / "scripts" / "report" / "build.py",
            "--input", str(executive),
            "--output", str(html),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        result = run_script(
            port / "scripts" / "report" / "build-markdown.py",
            "--input", str(source),
            "--output", str(markdown),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return executive, html, markdown

    def test_findings_are_presented_critical_to_informational(self):
        document = mixed_severity_document()
        for name, port in PORTS.items():
            with self.subTest(port=name), tempfile.TemporaryDirectory() as tmp:
                executive, html_path, markdown_path = self._build_outputs(
                    port, document, pathlib.Path(tmp)
                )
                report = json.loads(executive.read_text(encoding="utf-8"))
                self.assertEqual(
                    [finding["tier"] for finding in report["repos"][0]["findings"]],
                    list(TIERS),
                )

                html = html_path.read_text(encoding="utf-8")
                match = re.search(r"REPO_DETAIL = (\{.*?\});", html, re.DOTALL)
                self.assertIsNotNone(match)
                detail = json.loads(match.group(1).replace("<\\/", "</"))["demo"]
                positions = [detail.index(f'>{tier} (') for tier in TIERS]
                self.assertEqual(positions, sorted(positions))

                markdown = markdown_path.read_text(encoding="utf-8")
                positions = [markdown.index(f"## {tier} (") for tier in TIERS]
                self.assertEqual(positions, sorted(positions))

    def test_severity_explanations_render_once_under_spelled_out(self):
        document = unique_severity_document()
        for name, port in PORTS.items():
            with self.subTest(port=name), tempfile.TemporaryDirectory() as tmp:
                _, html_path, markdown_path = self._build_outputs(
                    port, document, pathlib.Path(tmp)
                )
                html = html_path.read_text(encoding="utf-8")
                match = re.search(r"REPO_DETAIL = (\{.*?\});", html, re.DOTALL)
                self.assertIsNotNone(match)
                detail = json.loads(match.group(1).replace("<\\/", "</"))["demo"]
                self.assertIn("Severity, spelled out", detail)
                self.assertNotIn('class="meta-row"', detail)
                markdown = markdown_path.read_text(encoding="utf-8")
                self.assertIn("**Severity, spelled out:**", markdown)
                self.assertNotIn("- **Reachability:**", markdown)
                self.assertNotIn("- **Exploitability:**", markdown)
                self.assertNotIn("- **Impact:**", markdown)
                self.assertNotIn("- **Patch status:**", markdown)
                for value in (
                    "REACHABILITY-UNIQUE",
                    "EXPLOITABILITY-UNIQUE",
                    "IMPACT-UNIQUE",
                    "PATCH-STATUS-UNIQUE",
                ):
                    self.assertEqual(detail.count(value), 1)
                    self.assertEqual(markdown.count(value), 1)

    def test_executive_summary_preserves_detailed_decision_context(self):
        document = detailed_summary_document()
        expected_headings = (
            "Aggregate Findings",
            "Per-Repository Summary",
            "What we did",
            "The headline",
            "Priority findings requiring action",
            "Proof-of-Concept Coverage",
            "Recurring patterns worth acting on",
            "Executive narrative",
            "Review outcomes",
            "Methodology",
            "Disclaimer",
        )
        for name, port in PORTS.items():
            with self.subTest(port=name), tempfile.TemporaryDirectory() as tmp:
                executive, html_path, _ = self._build_outputs(
                    port, document, pathlib.Path(tmp)
                )
                report = json.loads(executive.read_text(encoding="utf-8"))
                summary = report["summary"]
                for field in (
                    "what_we_did_md",
                    "headline_md",
                    "priority_findings_md",
                    "narrative_md",
                    "review_outcomes_md",
                    "methodology_md",
                    "disclaimer_md",
                ):
                    self.assertTrue(summary[field], field)
                self.assertEqual(report["poc_summary"]["total"], 2)
                self.assertEqual(report["poc_summary"]["valid"], 1)
                self.assertEqual(report["poc_summary"]["needs_revision"], 1)
                self.assertTrue(
                    any(
                        item["title"] == "Repeated authorization weakness"
                        for item in report["recurring_patterns"]
                    )
                )
                pattern = next(
                    item
                    for item in report["recurring_patterns"]
                    if item["title"] == "Repeated authorization weakness"
                )
                self.assertIn("FIX-1", pattern["remediation_md"])
                self.assertIn("FIX-2", pattern["remediation_md"])
                self.assertIn(
                    "correction history is unavailable",
                    summary["review_outcomes_md"].lower(),
                )

                summary_markdown = report["summary_markdown"]
                for heading in expected_headings:
                    self.assertIn(f"## {heading}", summary_markdown)
                self.assertIn("P0 finding", summary_markdown)
                self.assertIn("Rejected fixture candidate", summary_markdown)

                html = html_path.read_text(encoding="utf-8")
                for heading in expected_headings:
                    self.assertIn(heading.lower(), html.lower())
                self.assertIn("P0 finding", html)
                self.assertIn("Rejected fixture candidate", html)

    def test_legacy_summary_markdown_is_not_hidden_by_automatic_tables(self):
        report = {
            "report_title": "Legacy summary",
            "report_subtitle": "Compatibility fixture",
            "footer_text": "Fixture",
            "totals": {tier: 0 for tier in TIERS},
            "repos": [],
            "summary_markdown": (
                "# Executive Summary\n\n"
                "## Legacy detailed narrative\n\n"
                "This content must remain visible.\n"
            ),
        }
        for name, port in PORTS.items():
            with self.subTest(port=name), tempfile.TemporaryDirectory() as tmp:
                directory = pathlib.Path(tmp)
                source = directory / "executive.json"
                output = directory / "report.html"
                source.write_text(json.dumps(report) + "\n", encoding="utf-8")
                result = run_script(
                    port / "scripts" / "report" / "build.py",
                    "--input", str(source),
                    "--output", str(output),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                html = output.read_text(encoding="utf-8")
                self.assertIn("Legacy detailed narrative", html)
                self.assertIn("This content must remain visible.", html)

    def test_partial_structured_summary_uses_complete_legacy_markdown(self):
        report = {
            "report_title": "Partial summary",
            "report_subtitle": "Compatibility fixture",
            "footer_text": "Fixture",
            "totals": {tier: 0 for tier in TIERS},
            "repos": [],
            "summary": {"headline_md": "Short structured headline."},
            "summary_markdown": (
                "# Executive Summary\n\n"
                "## Complete legacy narrative\n\n"
                "Complete compatibility content.\n"
            ),
        }
        for name, port in PORTS.items():
            with self.subTest(port=name), tempfile.TemporaryDirectory() as tmp:
                directory = pathlib.Path(tmp)
                source = directory / "executive.json"
                output = directory / "report.html"
                source.write_text(json.dumps(report) + "\n", encoding="utf-8")
                result = run_script(
                    port / "scripts" / "report" / "build.py",
                    "--input", str(source),
                    "--output", str(output),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                html = output.read_text(encoding="utf-8")
                self.assertIn("Complete legacy narrative", html)
                self.assertIn("Complete compatibility content.", html)

    def test_headline_ranks_same_tier_by_cvss_and_preserves_code_literals(self):
        document = findings_doc()
        lower = copy.deepcopy(document["findings"][0])
        lower.update(
            finding_id="F-001",
            tier="P0",
            cvss_score=9.0,
            title="Lower-score P0",
        )
        higher = copy.deepcopy(document["findings"][0])
        higher.update(
            finding_id="F-999",
            tier="P0",
            cvss_score=10.0,
            title="Higher-score P0",
            fix_md="Set limit to 64L * 1024 * 1024 bytes.",
        )
        document["findings"] = [lower, higher]
        document["poc_coverage"] = {"generated": 0, "skipped": 2}
        for name, port in PORTS.items():
            with self.subTest(port=name), tempfile.TemporaryDirectory() as tmp:
                executive, html_path, _ = self._build_outputs(
                    port, document, pathlib.Path(tmp)
                )
                report = json.loads(executive.read_text(encoding="utf-8"))
                self.assertIn("Higher-score P0", report["summary"]["headline_md"])
                html = html_path.read_text(encoding="utf-8")
                summary_html = html.split('<section id="tab-repos"', 1)[0]
                self.assertIn("64L * 1024 * 1024", summary_html)
                self.assertNotIn("64L <em> 1024 </em> 1024", summary_html)

    def test_informational_only_report_does_not_call_p4_priority_action(self):
        document = findings_doc()
        document["findings"][0].update(
            tier="P4",
            cvss_score=0.0,
            title="Informational inventory note",
        )
        for name, port in PORTS.items():
            with self.subTest(port=name), tempfile.TemporaryDirectory() as tmp:
                executive, _, _ = self._build_outputs(port, document, pathlib.Path(tmp))
                report = json.loads(executive.read_text(encoding="utf-8"))
                priority = report["summary"]["priority_findings_md"]
                self.assertIn("No Critical or High findings", priority)
                self.assertNotIn("Informational inventory note", priority)


if __name__ == "__main__":
    unittest.main()
