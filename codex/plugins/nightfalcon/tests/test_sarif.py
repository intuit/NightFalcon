import json
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD_SARIF = ROOT / "scripts" / "report" / "build-sarif.py"


def _findings_doc(**overrides):
    doc = {
        "schema_version": "2",
        "repo_slug": "org-demo",
        "repo_url": "https://github.com/org/demo",
        "commit_sha": "a" * 40,
        "multitenant_scope": True,
        "date": "2026-08-25",
        "findings": [
            {
                "finding_id": "F-001",
                "title": "Cross-tenant read via IDOR",
                "tier": "P0",
                "cvss_score": 9.3,
                "cvss_vector": "CVSS:4.0/AV:N/AC:L/AT:N/PR:L/UI:N/VC:H/VI:N/VA:N/SC:H/SI:N/SA:N",
                "cwe": "CWE-639",
                "cve": "n/a",
                "exposure": "EXTERNAL",
                "reachability": "Reachable from the public API gateway",
                "exploitability": "Trivial; increment the tenant id",
                "impact": "Read any tenant's invoices",
                "patch_status": "unpatched",
                "location": "src/api/InvoiceController.java:88-140",
                "category": "Broken Object Level Authorization",
            },
            {
                "finding_id": "F-002",
                "title": "Verbose error leak",
                "tier": "P3",
                "cvss_score": 3.1,
                "cvss_vector": "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:L/VI:N/VA:N/SC:N/SI:N/SA:N",
                "cwe": "CWE-209",
                "cve": "n/a",
                "exposure": "INTERNAL",
                "location": "src/util/Err.py:12",
            },
        ],
    }
    doc.update(overrides)
    return doc


def build_sarif(*docs):
    """Run build-sarif.py on one or more inline findings docs; return SARIF dict."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        args = ["python3", str(BUILD_SARIF)]
        for i, d in enumerate(docs):
            p = tmp / f"findings-{i}.json"
            p.write_text(json.dumps(d))
            args += ["--input", str(p)]
        out = tmp / "out.sarif"
        args += ["--output", str(out)]
        result = subprocess.run(args, text=True, capture_output=True, check=False)
        if result.returncode:
            raise AssertionError(result.stderr or result.stdout)
        return json.loads(out.read_text())


class SarifShapeTests(unittest.TestCase):
    def test_version_and_schema(self):
        s = build_sarif(_findings_doc())
        self.assertEqual(s["version"], "2.1.0")
        self.assertIn("sarif-2.1.0", s["$schema"])

    def test_one_run_per_input_and_result_per_finding(self):
        s = build_sarif(_findings_doc(), _findings_doc(repo_slug="org-b"))
        self.assertEqual(len(s["runs"]), 2)
        for run in s["runs"]:
            self.assertEqual(run["tool"]["driver"]["name"], "NightFalcon")
            self.assertEqual(len(run["results"]), 2)

    def test_level_maps_from_tier(self):
        run = build_sarif(_findings_doc())["runs"][0]
        by_id = {r["properties"]["finding_id"]: r for r in run["results"]}
        self.assertEqual(by_id["F-001"]["level"], "error")   # P0
        self.assertEqual(by_id["F-002"]["level"], "note")    # P3

    def test_security_severity_is_cvss_score(self):
        run = build_sarif(_findings_doc())["runs"][0]
        by_id = {r["properties"]["finding_id"]: r for r in run["results"]}
        self.assertEqual(by_id["F-001"]["properties"]["security-severity"], "9.3")

    def test_ruleid_prefers_cwe_then_cve_then_finding(self):
        doc = _findings_doc()
        doc["findings"][0]["cwe"] = None
        doc["findings"][0]["cve"] = "CVE-2024-1234"
        doc["findings"][1]["cwe"] = None
        doc["findings"][1]["cve"] = None
        run = build_sarif(doc)["runs"][0]
        ids = [r["ruleId"] for r in run["results"]]
        self.assertIn("CVE-2024-1234", ids)
        self.assertIn("NIGHTFALCON.F-002", ids)

    def test_location_parsed_to_physical_location(self):
        run = build_sarif(_findings_doc())["runs"][0]
        f1 = run["results"][0]
        phys = f1["locations"][0]["physicalLocation"]
        self.assertEqual(phys["artifactLocation"]["uri"], "src/api/InvoiceController.java")
        self.assertEqual(phys["region"]["startLine"], 88)

    def test_version_control_provenance_carries_repo_and_sha(self):
        run = build_sarif(_findings_doc())["runs"][0]
        vcp = run["versionControlProvenance"][0]
        self.assertEqual(vcp["repositoryUri"], "https://github.com/org/demo")
        self.assertEqual(vcp["revisionId"], "a" * 40)

    def test_triage_sha_omits_revision_id(self):
        doc = _findings_doc(commit_sha="n/a (triage)")
        run = build_sarif(doc)["runs"][0]
        vcp = run["versionControlProvenance"][0]
        self.assertEqual(vcp["repositoryUri"], "https://github.com/org/demo")
        self.assertNotIn("revisionId", vcp)

    def test_exposure_enum_preserved_in_properties(self):
        run = build_sarif(_findings_doc())["runs"][0]
        by_id = {r["properties"]["finding_id"]: r for r in run["results"]}
        self.assertEqual(by_id["F-001"]["properties"]["exposure"], "EXTERNAL")
        self.assertEqual(by_id["F-002"]["properties"]["exposure"], "INTERNAL")

    def test_rules_deduped(self):
        doc = _findings_doc()
        # both findings CWE-639 -> single rule
        doc["findings"][1]["cwe"] = "CWE-639"
        run = build_sarif(doc)["runs"][0]
        rule_ids = [r["id"] for r in run["tool"]["driver"]["rules"]]
        self.assertEqual(rule_ids.count("CWE-639"), 1)

    def test_empty_findings_yields_empty_results(self):
        run = build_sarif(_findings_doc(findings=[]))["runs"][0]
        self.assertEqual(run["results"], [])
        self.assertEqual(run["tool"]["driver"]["rules"], [])


if __name__ == "__main__":
    unittest.main()
