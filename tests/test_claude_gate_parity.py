"""Behavioral parity tests for the active gate scripts."""

import json
import pathlib
import subprocess
import tempfile
import unittest

from tests import initialize_journal_fixture


ROOT = pathlib.Path(__file__).resolve().parents[1]
CLAUDE_ROOT = ROOT / "claude"
CHECK_GATE = CLAUDE_ROOT / "scripts" / "check-gate.sh"
COMPLETE_PHASE = CLAUDE_ROOT / "scripts" / "complete-phase.sh"
AGENT_JOURNAL = CLAUDE_ROOT / "scripts" / "agent-journal.py"
NEXT_GATE_PORTS = {
    "claude": CHECK_GATE,
    "cursor": ROOT / "cursor" / "scripts" / "check-gate.sh",
    "codex": ROOT
    / "codex"
    / "plugins"
    / "nightfalcon"
    / "scripts"
    / "check-gate.sh",
}
DATE = "2026-08-26"
CVSS = {
    "P0": (9.3, "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N"),
    "P3": (1.0, "CVSS:4.0/AV:P/AC:H/AT:P/PR:H/UI:A/VC:L/VI:N/VA:N/SC:N/SI:N/SA:N"),
    "P4": (0.0, "CVSS:4.0/AV:P/AC:H/AT:P/PR:H/UI:A/VC:N/VI:N/VA:N/SC:N/SI:N/SA:N"),
}


def scored_candidate(fid, tier, *, disposition=None):
    score, vector = CVSS[tier]
    text = (
        f"## CANDIDATE-{fid}\nExposure: EXTERNAL\nFlow: Flow-1\n"
        f"CVSS-B Score: {score:.1f}\nCVSS Vector: {vector}\n"
        "CVSS Rationale: Fixture metrics.\n"
        f"CVSS Calculator: https://www.first.org/cvss/calculator/4.0#{vector}\n"
        f"Tier: {tier}\nDebate required: {'YES' if tier in {'P0', 'P1', 'P2'} else 'NO'}\n"
    )
    if disposition is not None:
        text += (
            f"Final Disposition: {disposition}\nFinal CVSS-B Score: {score:.1f}\n"
            f"Final CVSS Vector: {vector}\nFinal CVSS Rationale: Fixture metrics.\n"
            f"Final CVSS Calculator: https://www.first.org/cvss/calculator/4.0#{vector}\n"
            f"Final Tier: {tier}\n"
        )
        if disposition == "DISMISSED":
            text += (
                f"Dismissed Finding Title: Dismissed fixture {fid}\n"
                "Dismissal Reason: Debate disproved the fixture candidate.\n"
            )
        validation = (
            "CURRENT"
            if tier in {"P0", "P1", "P2"} and disposition != "DISMISSED"
            else "NOT-RUN"
        )
        text += f"Validation: {validation}\n"
    return text


def run(*args):
    return subprocess.run(args, text=True, capture_output=True, check=False)


class ClaudeGateParityTests(unittest.TestCase):
    def make_workspace(
        self,
        base: pathlib.Path,
        *,
        current_phase="phase-0",
        mode="review",
        slugs=("demo",),
        completed=(),
    ) -> pathlib.Path:
        workspace = base / "workspace"
        for directory in ("input", "sourcecode", "findings", "output"):
            (workspace / directory).mkdir(parents=True, exist_ok=True)
        (workspace / "state.json").write_text(
            json.dumps(
                {
                    "date": DATE,
                    "mode": mode,
                    "current_phase": current_phase,
                    "phase_status": {phase: "completed" for phase in completed},
                    "repo_slugs": list(slugs),
                    "git_checkpoints": [],
                    "history": [],
                },
                indent=2,
            )
            + "\n"
        )
        initialize_journal_fixture(workspace, DATE, AGENT_JOURNAL)
        return workspace

    def test_retained_fixture_passes_strict_journal_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.make_workspace(pathlib.Path(tmp))
            result = run(
                "/usr/local/bin/python3",
                str(AGENT_JOURNAL),
                "validate",
                "--workspace",
                str(workspace),
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def phase_one_outputs(self, workspace: pathlib.Path, slug="demo"):
        directory = workspace / "findings" / slug
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"dataflow-{DATE}.md").write_text(
            "# Scope\n\n## External Attack Surface\n\n- endpoint\n"
        )
        (directory / f"owasp-context-{DATE}.md").write_text(
            "## System Kinds Detected\n\n- web\n"
        )
        (directory / f"organization-context-{DATE}.md").write_text(
            "No organization controls detected.\n"
        )
        (directory / f"dependency-inventory-{DATE}.json").write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "dependency_inventory": [],
                    "manifest_coverage": [],
                    "manifests_detected": 0,
                    "package_manager_execution": False,
                    "network_access": False,
                }
            )
            + "\n"
        )

    def candidates(self, workspace: pathlib.Path, text: str, slug="demo"):
        directory = workspace / "findings" / slug
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"dataflow-{DATE}.json").write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "repo_slug": slug,
                    "date": DATE,
                    "inventory": {
                        "languages": [],
                        "frameworks": [],
                        "key_dependencies": [],
                    },
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
                            "exposure": "EXTERNAL",
                        }
                    ],
                }
            )
            + "\n"
        )
        (directory / f"candidates-{DATE}.md").write_text(text)

    def test_next_gate_blocks_out_of_order_phase_even_with_prerequisites(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.make_workspace(pathlib.Path(tmp), completed=("phase-0",))
            (workspace / "sourcecode" / "demo").mkdir()
            result = run(
                "bash",
                str(CHECK_GATE),
                "--next-phase",
                "phase-1",
                "--workspace",
                str(workspace),
            )
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("current_phase", result.stderr)

    def test_next_gate_blocks_current_phase_already_marked_completed(self):
        for port, gate in NEXT_GATE_PORTS.items():
            with self.subTest(port=port), tempfile.TemporaryDirectory() as tmp:
                workspace = self.make_workspace(
                    pathlib.Path(tmp), completed=("phase-0",)
                )
                state_path = workspace / "state.json"
                before = state_path.read_bytes()
                result = run(
                    "bash",
                    str(gate),
                    "--next-phase",
                    "phase-0",
                    "--workspace",
                    str(workspace),
                )
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("completed", result.stderr)
                self.assertEqual(state_path.read_bytes(), before)

    def test_complete_phase_blocks_out_of_order_and_replayed_phases(self):
        for label, current, completed, requested in (
            ("out-of-order", "phase-0", (), "phase-1"),
            ("replay", "phase-1", ("phase-0",), "phase-0"),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                workspace = self.make_workspace(
                    pathlib.Path(tmp), current_phase=current, completed=completed
                )
                self.phase_one_outputs(workspace)
                (workspace / "output" / f"run-log-{DATE}.md").write_text(
                    "clone complete\n"
                )
                before = (workspace / "state.json").read_text()
                result = run(
                    "bash",
                    str(COMPLETE_PHASE),
                    "--phase",
                    requested,
                    "--workspace",
                    str(workspace),
                )
                after = (workspace / "state.json").read_text()
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertEqual(after, before)

    def test_gate_scripts_block_invalid_mode_phase_combinations(self):
        cases = (
            (CHECK_GATE, "--next-phase", "review", "triage-ingest", "triage-ingest"),
            (CHECK_GATE, "--next-phase", "invalid", "phase-0", "phase-0"),
            (COMPLETE_PHASE, "--phase", "triage", "phase-0", "phase-0"),
        )
        for script, option, mode, current, requested in cases:
            with self.subTest(mode=mode, requested=requested), tempfile.TemporaryDirectory() as tmp:
                workspace = self.make_workspace(
                    pathlib.Path(tmp),
                    mode="review" if mode == "invalid" else mode,
                    current_phase=current,
                )
                if mode == "invalid":
                    state_path = workspace / "state.json"
                    state = json.loads(state_path.read_text())
                    state["mode"] = mode
                    state_path.write_text(json.dumps(state, indent=2) + "\n")
                result = run(
                    "bash",
                    str(script),
                    option,
                    requested,
                    "--workspace",
                    str(workspace),
                )
                self.assertEqual(result.returncode, 2, result.stdout)

    def test_shortcut_flags_require_their_no_work_condition(self):
        cases = (
            (
                "phase-3",
                "--skip-candidates",
                "NO_CANDIDATES\n## CANDIDATE-F-001\nTier: P0\nDebate required: YES\n",
            ),
            (
                "phase-4",
                "--no-debate-needed",
                "## CANDIDATE-F-001\nTier: P0\nDebate required: YES\n",
            ),
        )
        for phase, shortcut, candidates in cases:
            with self.subTest(shortcut=shortcut), tempfile.TemporaryDirectory() as tmp:
                workspace = self.make_workspace(
                    pathlib.Path(tmp), current_phase=phase
                )
                self.candidates(workspace, candidates)
                result = run(
                    "bash",
                    str(COMPLETE_PHASE),
                    "--phase",
                    phase,
                    "--workspace",
                    str(workspace),
                    shortcut,
                )
                self.assertEqual(result.returncode, 2, result.stdout)

    def test_no_validation_shortcut_blocks_eligible_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.make_workspace(
                pathlib.Path(tmp), current_phase="phase-5", completed=("phase-4",)
            )
            self.candidates(
                workspace,
                "## CANDIDATE-F-001\n"
                "Final Tier: P1\n"
                "Final Disposition: CONFIRMED\n",
            )
            before = (workspace / "state.json").read_text()
            result = run(
                "bash",
                str(COMPLETE_PHASE),
                "--phase",
                "phase-5",
                "--workspace",
                str(workspace),
                "--no-validation-needed",
            )
            after = (workspace / "state.json").read_text()
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("eligible", result.stderr)
        self.assertEqual(after, before)

    def test_no_validation_shortcut_accepts_only_ineligible_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.make_workspace(
                pathlib.Path(tmp), current_phase="phase-5", completed=("phase-4",)
            )
            self.candidates(
                workspace,
                scored_candidate("F-001", "P3", disposition="CONFIRMED")
                + scored_candidate("F-002", "P0", disposition="DISMISSED"),
            )
            result = run(
                "bash",
                str(COMPLETE_PHASE),
                "--phase",
                "phase-5",
                "--workspace",
                str(workspace),
                "--no-validation-needed",
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_poc_shortcut_blocks_eligible_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.make_workspace(
                pathlib.Path(tmp), current_phase="phase-6", completed=("phase-5",)
            )
            self.candidates(
                workspace,
                "## CANDIDATE-F-001\n"
                "Final Tier: P2\n"
                "Final Disposition: CONFIRMED-MODIFIED\n",
            )
            manifest = (
                workspace
                / "output"
                / "proof_of_concept"
                / "demo"
                / f"poc-manifest-{DATE}.json"
            )
            before = (workspace / "state.json").read_text()
            result = run(
                "bash",
                str(COMPLETE_PHASE),
                "--phase",
                "phase-6",
                "--workspace",
                str(workspace),
                "--no-poc-needed",
            )
            after = (workspace / "state.json").read_text()
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("eligible", result.stderr)
        self.assertFalse(manifest.exists())
        self.assertEqual(after, before)

    def test_no_poc_shortcut_blocks_needs_review_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.make_workspace(
                pathlib.Path(tmp), current_phase="phase-6", completed=("phase-5",)
            )
            self.candidates(
                workspace,
                "## CANDIDATE-F-001\n"
                "Final Tier: P1\n"
                "Final Disposition: NEEDS-REVIEW\n",
            )
            manifest = (
                workspace
                / "output"
                / "proof_of_concept"
                / "demo"
                / f"poc-manifest-{DATE}.json"
            )
            before = (workspace / "state.json").read_text()
            result = run(
                "bash",
                str(COMPLETE_PHASE),
                "--phase",
                "phase-6",
                "--workspace",
                str(workspace),
                "--no-poc-needed",
            )
            after = (workspace / "state.json").read_text()
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("eligible", result.stderr)
        self.assertFalse(manifest.exists())
        self.assertEqual(after, before)

    def test_no_poc_shortcut_writes_complete_skip_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.make_workspace(
                pathlib.Path(tmp), current_phase="phase-6", completed=("phase-5",)
            )
            self.candidates(
                workspace,
                scored_candidate("F-001", "P3", disposition="CONFIRMED")
                + scored_candidate("F-002", "P0", disposition="DISMISSED"),
            )
            result = run(
                "bash",
                str(COMPLETE_PHASE),
                "--phase",
                "phase-6",
                "--workspace",
                str(workspace),
                "--no-poc-needed",
            )
            manifest_path = (
                workspace
                / "output"
                / "proof_of_concept"
                / "demo"
                / f"poc-manifest-{DATE}.json"
            )
            manifest = json.loads(manifest_path.read_text())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(manifest["pocs"], [])
        self.assertEqual(
            {
                entry.get("finding_id"): (
                    entry.get("tier"), entry.get("disposition"), entry.get("reason")
                )
                for entry in manifest["skipped"]
            },
            {
                "F-001": ("P3", "CONFIRMED", "not-eligible-tier"),
                "F-002": ("P0", "DISMISSED", "not-eligible-disposition"),
            },
        )

    def test_no_debate_shortcut_accepts_only_p3_p4_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.make_workspace(
                pathlib.Path(tmp), current_phase="phase-4", completed=("phase-3",)
            )
            self.candidates(
                workspace,
                scored_candidate("F-001", "P3")
                + scored_candidate("F-002", "P4"),
            )
            result = run(
                "bash",
                str(COMPLETE_PHASE),
                "--phase",
                "phase-4",
                "--workspace",
                str(workspace),
                "--no-debate-needed",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            debate = (
                workspace / "findings" / "demo" / f"debate-{DATE}.md"
            ).read_text()
            finalized = (
                workspace / "findings" / "demo" / f"candidates-{DATE}.md"
            ).read_text()
        self.assertIn("NO_DEBATE_CANDIDATES", debate)
        self.assertEqual(finalized.count("Final Disposition: NEEDS-REVIEW"), 2)
        self.assertIn("Final Tier: P3", finalized)
        self.assertIn("Final Tier: P4", finalized)

    def test_shortcut_flags_are_rejected_outside_their_supported_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.make_workspace(pathlib.Path(tmp))
            (workspace / "output" / f"run-log-{DATE}.md").write_text(
                "clone complete\n"
            )
            result = run(
                "bash",
                str(COMPLETE_PHASE),
                "--phase",
                "phase-0",
                "--workspace",
                str(workspace),
                "--skip-candidates",
            )
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("--skip-candidates", result.stderr)

    def test_check_slug_validates_one_repo_without_advancing_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.make_workspace(
                pathlib.Path(tmp),
                current_phase="phase-1",
                slugs=("done", "pending"),
                completed=("phase-0",),
            )
            self.phase_one_outputs(workspace, "done")
            before = (workspace / "state.json").read_text()
            result = run(
                "bash",
                str(COMPLETE_PHASE),
                "--phase",
                "phase-1",
                "--workspace",
                str(workspace),
                "--check-slug",
                "done",
            )
            after = (workspace / "state.json").read_text()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("state unchanged", result.stdout)
        self.assertEqual(after, before)

    def test_write_guard_blocks_traversal_and_prefix_bypasses(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.make_workspace(pathlib.Path(tmp), current_phase="phase-1")
            sibling = pathlib.Path(tmp) / "workspace-sibling"
            sibling.mkdir()
            blocked_paths = (
                "findings/../../outside.md",
                "findings/../findings-sibling/outside.md",
                str(sibling / "findings" / "outside.md"),
            )
            for path in blocked_paths:
                with self.subTest(path=path):
                    result = run(
                        "bash",
                        str(CHECK_GATE),
                        "--check-path",
                        path,
                        "--workspace",
                        str(workspace),
                    )
                    self.assertEqual(result.returncode, 2, result.stdout)

    def test_write_guard_requires_directory_boundary_for_prefixes(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.make_workspace(pathlib.Path(tmp), current_phase="phase-0")
            result = run(
                "bash",
                str(CHECK_GATE),
                "--check-path",
                "output/run-logger.md",
                "--workspace",
                str(workspace),
            )
        self.assertEqual(result.returncode, 2, result.stdout)


if __name__ == "__main__":
    unittest.main()
