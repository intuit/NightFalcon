import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import initialize_journal_fixture


ROOT = Path(__file__).resolve().parents[1]
DATE = "2026-08-26"
PORTS = {
    "claude": ROOT / "claude",
    "codex": ROOT / "codex/plugins/nightfalcon",
    "cursor": ROOT / "cursor",
}


class ClientSafetyCanaries(unittest.TestCase):
    def _state(self, workspace: Path, phase: str, completed: list[str]) -> None:
        (workspace / "state.json").write_text(
            json.dumps(
                {
                    "schema_version": "2",
                    "date": DATE,
                    "mode": "review",
                    "current_phase": phase,
                    "phase_status": {name: "completed" for name in completed},
                    "repo_slugs": ["demo"],
                    "history": [],
                    "git_checkpoints": [],
                    "multitenant_scope": False,
                }
            )
        )

    def _gate(self, port: Path, workspace: Path, phase: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "bash",
                str(port / "scripts/check-gate.sh"),
                "--next-phase",
                phase,
                "--workspace",
                str(workspace),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_each_client_blocks_phase_two_without_dependency_inventory(self):
        for name, port in PORTS.items():
            with self.subTest(client=name), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                self._state(workspace, "phase-2", ["phase-0", "phase-1"])
                initialize_journal_fixture(
                    workspace, DATE, port / "scripts/agent-journal.py"
                )
                findings = workspace / "findings/demo"
                findings.mkdir(parents=True)
                (findings / f"dataflow-{DATE}.md").write_text(
                    "## External Attack Surface\n\n- endpoint\n"
                )
                result = self._gate(port, workspace, "phase-2")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("dependency-inventory", result.stderr)

    def test_each_client_rejects_incomplete_dependency_manifest_coverage(self):
        for name, port in PORTS.items():
            with self.subTest(client=name), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                self._state(workspace, "phase-1", ["phase-0"])
                initialize_journal_fixture(
                    workspace, DATE, port / "scripts/agent-journal.py"
                )
                findings = workspace / "findings/demo"
                findings.mkdir(parents=True)
                (findings / f"dataflow-{DATE}.md").write_text("## External Attack Surface\n")
                (findings / f"organization-context-{DATE}.md").write_text("No organization controls detected.\n")
                (findings / f"owasp-context-{DATE}.md").write_text("## System Kinds Detected\n")
                (findings / f"dependency-inventory-{DATE}.json").write_text(json.dumps({
                    "schema_version": "1",
                    "dependency_inventory": [],
                    "manifest_coverage": [{
                        "manifest": "package.json",
                        "status": "error",
                        "reason": "JSONDecodeError",
                    }],
                    "manifests_detected": 1,
                    "package_manager_execution": False,
                    "network_access": False,
                }))
                result = subprocess.run(
                    [
                        "bash",
                        str(port / "scripts/complete-phase.sh"),
                        "--phase",
                        "phase-1",
                        "--workspace",
                        str(workspace),
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("dependency inventory invalid", result.stderr)

    def test_each_client_blocks_phase_three_without_reconciled_topology(self):
        for name, port in PORTS.items():
            with self.subTest(client=name), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                self._state(workspace, "phase-3", ["phase-0", "phase-1", "phase-2"])
                initialize_journal_fixture(
                    workspace, DATE, port / "scripts/agent-journal.py"
                )
                findings = workspace / "findings/demo"
                findings.mkdir(parents=True)
                (findings / f"dataflow-{DATE}.md").write_text("## Data Flow Map\n")
                result = self._gate(port, workspace, "phase-3")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("cross-repository-topology", result.stderr)

    def test_each_client_rejects_missing_deep_analysis_fields(self):
        malformed = {
            "schema_version": "1",
            "repo_slug": "demo",
            "date": DATE,
            "flows": [{"flow_id": "Flow-1", "exposure": "EXTERNAL"}],
        }
        for name, port in PORTS.items():
            with self.subTest(client=name), tempfile.TemporaryDirectory() as tmp:
                dataflow = Path(tmp) / "dataflow.json"
                dataflow.write_text(json.dumps(malformed))
                result = subprocess.run(
                    [
                        "python3",
                        str(port / "scripts/validate-findings.py"),
                        "dataflow",
                        "--input",
                        str(dataflow),
                        "--slug",
                        "demo",
                        "--date",
                        DATE,
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("relationship_context", result.stderr)

    def test_each_client_rejects_boolean_bola_selector_substitution(self):
        malformed = {
            "schema_version": "1", "repo_slug": "demo", "date": DATE,
            "analysis_coverage": {
                "authorization": {"status": "analyzed", "rationale": "Authorization selector substitution was traced at the repository boundary.", "flow_ids": ["Flow-1"]},
                "business_logic": {"status": "not-applicable", "rationale": "No stateful business operation exists in this fixture.", "flow_ids": []},
                "cross_repository": {"status": "not-applicable", "rationale": "No outbound repository boundary exists in this fixture.", "flow_ids": []},
            },
            "relationship_context": [{
                "flow_id": "Flow-1", "case_id": "AUTHZ-1", "principal": {},
                "action": "read invoice", "object": {"type": "invoice", "selector_provenance": "path"},
                "relationship": "cross-tenant", "selector_substitution": True,
                "observed_decision": "allow", "expected_decision": "deny",
                "enforcement_points": ["InvoiceController.get"], "evidence": ["controller.py:10"],
            }],
            "business_logic_invariants": [], "outbound_edges": [],
            "flows": [{"flow_id": "Flow-1", "exposure": "EXTERNAL"}],
        }
        for name, port in PORTS.items():
            with self.subTest(client=name), tempfile.TemporaryDirectory() as tmp:
                dataflow = Path(tmp) / "dataflow.json"
                dataflow.write_text(json.dumps(malformed))
                result = subprocess.run(
                    ["python3", str(port / "scripts/validate-findings.py"), "dataflow",
                     "--input", str(dataflow), "--slug", "demo", "--date", DATE],
                    text=True, capture_output=True, check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("selector_substitution", result.stderr)

    def test_each_client_rejects_context_provider_symlink_escape(self):
        for name, port in PORTS.items():
            with self.subTest(client=name), tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
                provider = Path(tmp)
                note = Path(outside) / "private.md"
                note.write_text("outside provider root")
                (provider / "escape.md").symlink_to(note)
                graph = {
                    "schema_version": "2",
                    "controls": [
                        {
                            "name": "escape",
                            "kind": "control",
                            "control_id": "CTRL-0001",
                            "note_path": "escape.md",
                            "fingerprints": [],
                            "applicable_standards": [],
                        }
                    ],
                    "platform_services": [],
                    "standards": [],
                    "fingerprint_index": {},
                }
                source = provider / "_graph.json"
                output = provider / "normalized.json"
                source.write_text(json.dumps(graph))
                result = subprocess.run(
                    [
                        "python3",
                        str(port / "scripts/build-context-graph.py"),
                        "--root",
                        str(provider),
                        "--source",
                        str(source),
                        "--output",
                        str(output),
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("escapes configured root", result.stderr)
                self.assertFalse(output.exists())

    def test_claude_bash_hook_blocks_protected_mutation_and_allows_inspection(self):
        hook = ROOT / "claude/hooks/bash-guard.py"
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._state(workspace, "phase-3", ["phase-0", "phase-1", "phase-2"])
            (workspace / ".nightfalcon-review").write_text("nightfalcon\n")
            env = dict(os.environ)
            env.pop("SECURITY_REVIEW_WORKSPACE", None)
            env["CLAUDE_PLUGIN_ROOT"] = str(ROOT / "claude")

            def run(command: str):
                return subprocess.run(
                    ["python3", str(hook)],
                    input=json.dumps({
                        "tool_name": "Bash", "cwd": str(workspace),
                        "tool_input": {"command": command},
                    }),
                    text=True, capture_output=True, check=False, env=env,
                )

            denied = (
                'sed -i "s/phase-3/done/" state.json',
                f'rm -rf "{workspace}/findings"',
                f'echo x > "{workspace}/output/executive-report-{DATE}.json"',
                "python3 -c \"from pathlib import Path; Path('state.json').write_text('x')\"",
                "python3 -c \"import sys; open(sys.argv[1], 'w').write('x')\" state.json",
                "python3 -c \"import os; os.system('printf x > state.json')\"",
                "awk 'BEGIN {print \"x\" > \"state.json\"}'",
                "bash -c 'truncate -s 0 findings/demo/candidates.json'",
                "printf 'state.json\\n' | xargs rm",
                "git reset --hard HEAD~1",
                "tar xf /tmp/archive.tar",
            )
            for command in denied:
                with self.subTest(command=command):
                    result = run(command)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("BLOCKED", result.stderr)
            for command in ("rg phase state.json", "rg TODO sourcecode", "npm test"):
                with self.subTest(command=command):
                    result = run(command)
                    self.assertEqual(result.returncode, 0, result.stderr)
            lifecycle = run(f'bash "{ROOT / "claude/scripts/check-gate.sh"}" --next-phase phase-3 --workspace "{workspace}"')
            self.assertEqual(lifecycle.returncode, 0, lifecycle.stderr)

    def test_claude_write_gate_denies_direct_state_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._state(workspace, "phase-3", ["phase-0", "phase-1", "phase-2"])
            result = subprocess.run(
                ["bash", str(ROOT / "claude/scripts/check-gate.sh"),
                 "--check-path", str(workspace / "state.json"),
                 "--workspace", str(workspace)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("script-owned", result.stderr)


if __name__ == "__main__":
    unittest.main()
