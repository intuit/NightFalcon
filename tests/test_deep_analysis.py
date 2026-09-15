import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import initialize_journal_fixture


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


class DeepAnalysisContractTests(unittest.TestCase):
    def test_relationship_and_concurrency_fixtures(self):
        bola = json.loads((ROOT / "tests/fixtures/authorization/bola-cross-tenant.json").read_text())["relationship_context"]
        logic = json.loads((ROOT / "tests/fixtures/business-logic/dual-coupon-redemption.json").read_text())["business_logic_invariants"]
        self.assertEqual(bola["relationship"], "cross-tenant")
        self.assertEqual((bola["observed_decision"], bola["expected_decision"]), ("allow", "deny"))
        self.assertFalse(logic["atomicity"]["transaction"])
        self.assertGreaterEqual(len(logic["interleavings"][0]), 4)
        self.assertIn("BLA2:2025", logic["owasp_bla"])

    def test_dependency_inventory_preserves_conditional_groups(self):
        script = ROOT / "shared/scripts/dependency-inventory.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(json.dumps({
                "dependencies": {"core": "1.0.0"},
                "optionalDependencies": {"native-addon": "2.0.0"},
                "peerDependencies": {"framework": "^3"},
            }))
            (root / "pyproject.toml").write_text(
                '[project]\ndependencies=["requests>=2"]\n'
                '[project.optional-dependencies]\npostgres=["psycopg[binary]>=3"]\n'
            )
            (root / "Cargo.toml").write_text(
                '[dependencies]\nserde="1"\n[workspace.dependencies]\ntokio="1"\n'
                '[features]\ntls=["dep:rustls"]\n'
                '[target.\'cfg(target_os = "linux")\'.dependencies]\nnix="0.29"\n'
            )
            (root / "go.mod").write_text(
                "module example.com/demo\nrequire example.com/single v1.2.3\n"
            )
            (root / "pom.xml").write_text(
                '<project><dependencies><dependency><groupId>org.example</groupId>'
                '<artifactId>core-lib</artifactId><version>1.2.3</version>'
                '<scope>runtime</scope></dependency></dependencies></project>'
            )
            (root / "demo.csproj").write_text(
                '<Project><ItemGroup Condition="windows"><PackageReference Include="Native.Lib" Version="4.0" />'
                '</ItemGroup></Project>'
            )
            result = subprocess.run(
                ["python3", str(script), str(root)], check=True, capture_output=True, text=True
            )
            data = json.loads(result.stdout)
            groups = {row["group"] for row in data["dependency_inventory"]}
            self.assertTrue({"runtime", "optional", "peer", "optional:postgres", "feature:tls"} <= groups)
            self.assertIn("workspace", groups)
            self.assertTrue(any(group.startswith("target:") for group in groups))
            ecosystems = {row["ecosystem"] for row in data["dependency_inventory"]}
            self.assertTrue({"maven", "nuget"} <= ecosystems)
            self.assertTrue(any(row["name"] == "example.com/single" and row["selector"] == "v1.2.3" for row in data["dependency_inventory"]))
            self.assertTrue(any(row["name"] == "tokio" and row["group"] == "workspace" for row in data["dependency_inventory"]))
            self.assertTrue(any(row["name"] == "org.example:core-lib" for row in data["dependency_inventory"]))
            self.assertTrue(any(row["name"] == "Native.Lib" and row["group"].startswith("condition:") for row in data["dependency_inventory"]))
            self.assertTrue(all(row["reachability"] == "unknown" for row in data["dependency_inventory"]))
            self.assertFalse(data["package_manager_execution"])
            self.assertFalse(data["network_access"])

    def test_dependency_inventory_reads_lockfiles_without_following_symlinks(self):
        script = ROOT / "shared/scripts/dependency-inventory.py"
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            secret = Path(outside) / "package.json"
            secret.write_text(json.dumps({"dependencies": {"private-secret-name": "9.9.9"}}))
            (root / "package.json").symlink_to(secret)
            (root / "Cargo.lock").write_text('[[package]]\nname = "serde"\nversion = "1.0.0"\n')
            (root / "composer.lock").write_text(json.dumps({
                "packages": [{"name": "vendor/core", "version": "2.1.0"}],
                "packages-dev": [],
            }))
            result = subprocess.run(
                ["python3", str(script), str(root)], check=True, capture_output=True, text=True
            )
            data = json.loads(result.stdout)
            names = {row.get("name") for row in data["dependency_inventory"]}
            self.assertNotIn("private-secret-name", names)
            self.assertTrue({"serde", "vendor/core"} <= names)
            self.assertTrue(all(row.get("resolution_status") == "resolved" for row in data["dependency_inventory"]))
            rejected = [entry for entry in data["manifest_coverage"] if entry["status"] == "rejected"]
            self.assertEqual(rejected, [{"manifest": "package.json", "status": "rejected", "reason": "symbolic links are not read"}])

    def test_dependency_inventory_reads_poetry_dependency_tables(self):
        script = ROOT / "shared/scripts/dependency-inventory.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                '[tool.poetry.dependencies]\npython = ">=3.11"\nrequests = "^2.32"\n'
                'uvicorn = {version = "^0.30", extras = ["standard"]}\n'
                '[tool.poetry.dev-dependencies]\npytest = "^8"\n'
                '[tool.poetry.group.docs.dependencies]\nmkdocs = "^1.6"\n'
                '[tool.poetry.group.dev.dependencies]\nruff = "^0.6"\n'
            )
            result = subprocess.run(
                ["python3", str(script), str(root)], check=True, capture_output=True, text=True
            )
            data = json.loads(result.stdout)
            by_name = {row["name"]: row for row in data["dependency_inventory"]}
            self.assertNotIn("python", by_name)
            self.assertEqual(by_name["requests"]["group"], "runtime")
            self.assertEqual(by_name["uvicorn"]["selector"]["version"], "^0.30")
            self.assertEqual(by_name["pytest"]["group"], "development")
            self.assertEqual(by_name["ruff"]["group"], "development")
            self.assertEqual(by_name["mkdocs"]["group"], "poetry:docs")
            coverage = next(item for item in data["manifest_coverage"] if item["manifest"] == "pyproject.toml")
            self.assertEqual(coverage, {"manifest": "pyproject.toml", "status": "parsed", "records": 5})

    def test_topology_reconciliation_classifies_every_edge(self):
        script = ROOT / "shared/scripts/reconcile-topology.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "topology.json"
            date = "2026-08-26"
            for slug in ("service-a", "service-b"):
                (root / slug).mkdir()
            (root / "service-a" / f"dataflow-{date}.json").write_text(json.dumps({
                "repo_slug": "service-a", "date": date,
                "outbound_edges": [
                    {"flow_id": "Flow-1", "target": "service-b/api", "target_repo_slug": "service-b"},
                    {"flow_id": "Flow-1", "target": "api.vendor.test", "declared_external": True},
                    {"flow_id": "Flow-1", "target": "mystery.internal"},
                ],
            }))
            (root / "service-b" / f"dataflow-{date}.json").write_text(json.dumps({"repo_slug": "service-b", "date": date, "outbound_edges": []}))
            (root / "service-a" / "dataflow-2025-01-01.json").write_text("not current JSON")
            subprocess.run(
                ["python3", str(script), "--input-dir", str(root), "--output", str(out),
                 "--date", date, "--slug", "service-a", "--slug", "service-b"], check=True
            )
            document = json.loads(out.read_text())
            topology = document["cross_repository_topology"]
            self.assertEqual([edge["classification"] for edge in topology["edges"]], ["matched", "external", "unresolved"])
            self.assertEqual(document["date"], date)
            self.assertEqual(len(document["source_artifacts"]), 2)
            self.assertTrue(all(len(item["sha256"]) == 64 for item in document["source_artifacts"]))
            self.assertEqual(topology["phase_order"], "after-all-phase-2-before-phase-3")

            tampered = json.loads(out.read_text())
            tampered["cross_repository_topology"]["edges"][0]["classification"] = "external"
            tampered["cross_repository_topology"]["edges"][0]["matched_repo"] = None
            out.write_text(json.dumps(tampered))
            invalid = subprocess.run(
                ["python3", str(script), "--input-dir", str(root), "--output", str(out),
                 "--date", date, "--slug", "service-a", "--slug", "service-b", "--validate"],
                text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("deterministic reconciliation", invalid.stderr)
            subprocess.run(
                ["python3", str(script), "--input-dir", str(root), "--output", str(out),
                 "--date", date, "--slug", "service-a", "--slug", "service-b"], check=True
            )

            (root / "service-a" / f"dataflow-{date}.json").write_text(json.dumps({
                "repo_slug": "service-a", "date": date, "outbound_edges": []
            }))
            stale = subprocess.run(
                ["python3", str(script), "--input-dir", str(root), "--output", str(out),
                 "--date", date, "--slug", "service-a", "--slug", "service-b", "--validate"],
                text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(stale.returncode, 0)
            self.assertIn("deterministic reconciliation", stale.stderr)

    def test_topology_rejects_explicit_slug_that_contradicts_target_evidence(self):
        script = ROOT / "shared/scripts/reconcile-topology.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "topology.json"
            date = "2026-08-26"
            for slug in ("service-a", "service-b"):
                directory = root / slug
                directory.mkdir()
                edges = ([{
                    "flow_id": "Flow-1",
                    "target": "api.vendor.test/v1",
                    "target_repo_slug": "service-b",
                    "declared_external": True,
                }] if slug == "service-a" else [])
                (directory / f"dataflow-{date}.json").write_text(json.dumps({
                    "repo_slug": slug, "date": date, "outbound_edges": edges,
                }))
            subprocess.run(
                ["python3", str(script), "--input-dir", str(root), "--output", str(out),
                 "--date", date, "--slug", "service-a", "--slug", "service-b"], check=True
            )
            edge = json.loads(out.read_text())["cross_repository_topology"]["edges"][0]
            self.assertEqual(edge["classification"], "external")
            self.assertIsNone(edge["matched_repo"])

    def test_phase_two_gate_builds_topology_before_phase_three(self):
        complete = ROOT / "codex/plugins/nightfalcon/scripts/complete-phase.sh"
        date = "2026-08-26"
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "state.json").write_text(json.dumps({
                "date": date, "mode": "review", "current_phase": "phase-2",
                "phase_status": {"phase-0": "completed", "phase-1": "completed"},
                "repo_slugs": ["service-a", "service-b"], "history": [], "git_checkpoints": [],
            }))
            initialize_journal_fixture(
                workspace,
                date,
                ROOT / "codex/plugins/nightfalcon/scripts/agent-journal.py",
            )
            for slug, edges in (
                ("service-a", [{"flow_id": "Flow-1", "target": "service-b/api", "target_repo_slug": "service-b", "evidence": ["client.py:10"]}]),
                ("service-b", []),
            ):
                directory = workspace / "findings" / slug
                directory.mkdir(parents=True)
                (directory / f"dataflow-{date}.md").write_text("## Data Flow Map\n")
                (directory / f"dataflow-{date}.json").write_text(json.dumps({
                    "schema_version": "1", "repo_slug": slug, "date": date,
                    "analysis_coverage": {
                        "authorization": {"status": "not-applicable", "rationale": "No authorization-sensitive path exists in this fixture.", "flow_ids": []},
                        "business_logic": {"status": "not-applicable", "rationale": "No stateful business operation exists in this fixture.", "flow_ids": []},
                        "cross_repository": {"status": "analyzed" if edges else "not-applicable", "rationale": "Outbound repository calls were traced from the client implementation." if edges else "No outbound repository boundary exists in this fixture.", "flow_ids": ["Flow-1"] if edges else []},
                    },
                    "relationship_context": [], "business_logic_invariants": [],
                    "outbound_edges": edges,
                    "flows": [{"flow_id": "Flow-1", "exposure": "INTERNAL"}],
                }))
            result = subprocess.run(
                ["bash", str(complete), "--phase", "phase-2", "--workspace", str(workspace)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            topology = json.loads((workspace / "findings" / f"cross-repository-topology-{date}.json").read_text())
            edge = topology["cross_repository_topology"]["edges"][0]
            self.assertEqual((edge["classification"], edge["matched_repo"]), ("matched", "service-b"))
            self.assertEqual(json.loads((workspace / "state.json").read_text())["current_phase"], "phase-3")

    def test_context_provider_accepts_external_root_and_rejects_symlink_escape(self):
        module = load_module("context_graph", ROOT / "shared/scripts/build-context-graph.py")
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            graph = {
                "schema_version": "2", "controls": [], "platform_services": [],
                "standards": [], "fingerprint_index": {},
            }
            source = root / "_graph.json"
            source.write_text(json.dumps(graph))
            self.assertEqual(module.normalize(root, source)["schema_version"], "2")
            note = Path(outside) / "secret.md"
            note.write_text("not in configured provider root")
            (root / "escape.md").symlink_to(note)
            graph["controls"] = [{
                "name": "escape", "kind": "control", "control_id": "CTRL-0001",
                "note_path": "escape.md", "fingerprints": [], "applicable_standards": [],
            }]
            source.write_text(json.dumps(graph))
            with self.assertRaisesRegex(ValueError, "escapes configured root"):
                module.normalize(root, source)
            graph["controls"] = []
            graph["fingerprint_index"] = {"import.example": ["missing-control"]}
            source.write_text(json.dumps(graph))
            with self.assertRaisesRegex(ValueError, "unknown context entry"):
                module.normalize(root, source)

    def test_workspace_adapter_fails_closed_on_mixed_schema(self):
        module = load_module("workspace_compat", ROOT / "shared/compat/workspace_v1.py")
        with self.assertRaisesRegex(ValueError, "mixed"):
            module.normalize({"schema_version": 1, "org_scope": True, "multitenant_scope": False})
        with self.assertRaisesRegex(ValueError, "unsupported"):
            module.normalize({"schema_version": 99})

    def test_context_provider_and_multitenant_scope_are_independent(self):
        context = load_module("context_policy_graph", ROOT / "shared/scripts/build-context-graph.py")
        workspace = load_module("workspace_policy_compat", ROOT / "shared/compat/workspace_v1.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for configured in (False, True):
                provider = base / str(configured)
                provider.mkdir()
                graph = {
                    "schema_version": "2", "controls": ([{
                        "name": "local", "kind": "control", "control_id": "CTRL-0001",
                        "note_path": None, "fingerprints": ["local.import"], "applicable_standards": [],
                    }] if configured else []),
                    "platform_services": [], "standards": [], "fingerprint_index": {},
                }
                source = provider / "_graph.json"
                source.write_text(json.dumps(graph))
                normalized_context = context.normalize(provider, source)
                for multitenant in (False, True):
                    state = workspace.normalize({"schema_version": 1, "org_scope": multitenant})
                    self.assertEqual(state["multitenant_scope"], multitenant)
                    self.assertEqual(bool(normalized_context["controls"]), configured)


if __name__ == "__main__":
    unittest.main()
