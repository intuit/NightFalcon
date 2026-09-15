import base64
import hashlib
import json
import os
import pathlib
import re
import subprocess
import tempfile
import unittest
import uuid


ROOT = pathlib.Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parents[2]
INIT_REVIEW = ROOT / "scripts" / "init-review.sh"
CHECK_GATE = ROOT / "scripts" / "check-gate.sh"
ALL_JOURNAL_GATES = (
    (
        "claude",
        PROJECT_ROOT / "claude",
        PROJECT_ROOT / "claude" / "scripts" / "check-gate.sh",
    ),
    ("codex", ROOT, CHECK_GATE),
    (
        "cursor",
        PROJECT_ROOT / "cursor",
        PROJECT_ROOT / "cursor" / "scripts" / "check-gate.sh",
    ),
)
JOURNAL_GATES = tuple(
    (name, gate)
    for name, port_root, gate in ALL_JOURNAL_GATES
    if name == "codex" or port_root.exists()
)
COMPLETE_PHASE = ROOT / "scripts" / "complete-phase.sh"
AGENT_JOURNAL = ROOT / "scripts" / "agent-journal.py"
DATE = "2026-07-14"
CVSS_BY_TIER = {
    "P0": (9.3, "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N"),
    "P1": (7.1, "CVSS:4.0/AV:N/AC:L/AT:N/PR:L/UI:N/VC:H/VI:N/VA:N/SC:N/SI:N/SA:N"),
    "P2": (4.8, "CVSS:4.0/AV:L/AC:L/AT:N/PR:L/UI:N/VC:L/VI:N/VA:N/SC:N/SI:N/SA:N"),
    "P3": (1.0, "CVSS:4.0/AV:P/AC:H/AT:P/PR:H/UI:A/VC:L/VI:N/VA:N/SC:N/SI:N/SA:N"),
    "P4": (0.0, "CVSS:4.0/AV:P/AC:H/AT:P/PR:H/UI:A/VC:N/VI:N/VA:N/SC:N/SI:N/SA:N"),
}


def add_cvss_contract(text: str) -> str:
    """Enrich candidate fixtures so debate-marker tests also satisfy CVSS contract."""
    parts = re.split(r"(?=^## CANDIDATE-)", text, flags=re.MULTILINE)
    enriched = []
    for part in parts:
        if not part.startswith("## CANDIDATE-"):
            enriched.append(part)
            continue
        tier_match = re.search(r"(?im)^\*{0,2}Tier:\*{0,2}\s*(P[0-4])\b", part)
        tier = tier_match.group(1) if tier_match else "P0"
        score, vector = CVSS_BY_TIER[tier]
        additions = []
        if "Exposure:" not in part:
            additions.append("Exposure: EXTERNAL")
        if "Flow:" not in part:
            additions.append("Flow: Flow-1")
        if "CVSS-B Score:" not in part:
            additions.extend(
                (
                    f"CVSS-B Score: {score:.1f}",
                    f"CVSS Vector: {vector}",
                    "CVSS Rationale: Fixture metrics match requested tier.",
                    f"CVSS Calculator: https://www.first.org/cvss/calculator/4.0#{vector}",
                )
            )
        if "Final Disposition:" not in part:
            additions.extend(
                (
                    "Final Disposition: CONFIRMED",
                    f"Final CVSS-B Score: {score:.1f}",
                    f"Final CVSS Vector: {vector}",
                    "Final CVSS Rationale: Fixture metrics match requested tier.",
                    f"Final CVSS Calculator: https://www.first.org/cvss/calculator/4.0#{vector}",
                    f"Final Tier: {tier}",
                )
            )
        if "Validation:" not in part:
            disposition_match = re.search(
                r"(?im)^Final Disposition:\s*([A-Z-]+)\s*$", part + "\n" + "\n".join(additions)
            )
            disposition = disposition_match.group(1) if disposition_match else "CONFIRMED"
            validation = (
                "CURRENT"
                if tier in {"P0", "P1", "P2"} and disposition != "DISMISSED"
                else "NOT-RUN"
            )
            additions.append(f"Validation: {validation}")
        enriched.append(part.rstrip() + "\n" + "\n".join(additions) + "\n")
    return "".join(enriched)


def run(*args, cwd=None, env=None):
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


class GateRegressionTests(unittest.TestCase):
    def init_workspace(
        self,
        base: pathlib.Path,
        slugs=("demo",),
        *,
        mode="review",
    ) -> pathlib.Path:
        workspace = base / "workspace"
        model_pin = base / "model-pin.json"
        model_pin.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "session_id": "gate-test-session",
                    "model": "gpt-test",
                    "source": "SessionStart",
                }
            )
        )
        env = os.environ | {"PLUGIN_ROOT": str(ROOT)}
        command = [
            "bash",
            str(INIT_REVIEW),
            "--workspace",
            str(workspace),
            "--date",
            DATE,
            "--model-pin-file",
            str(model_pin),
        ]
        if mode == "triage":
            command.extend(("--mode", "triage", "--slug", slugs[0]))
        result = run(*command, cwd=base, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        state_path = workspace / "state.json"
        state = json.loads(state_path.read_text())
        state["repo_slugs"] = list(slugs)
        state_path.write_text(json.dumps(state, indent=2) + "\n")
        self.record_intent(workspace)
        return workspace

    def record_intent(self, workspace: pathlib.Path):
        recorded = run(
            "python3", str(AGENT_JOURNAL), "record-intent",
            "--workspace", str(workspace),
        )
        self.assertEqual(recorded.returncode, 0, recorded.stderr)

    def set_phase(self, workspace: pathlib.Path, phase: str, completed=()):
        state_path = workspace / "state.json"
        state = json.loads(state_path.read_text())
        state["current_phase"] = phase
        state["phase_status"] = {name: "completed" for name in completed}
        state["agent_journal"]["epoch_id"] = str(uuid.uuid4())
        state["agent_journal"]["epoch_phase"] = phase
        state["agent_journal"]["checkpoint_uuid"] = str(uuid.uuid4())
        state_path.write_text(json.dumps(state, indent=2) + "\n")
        self.record_intent(workspace)

    def candidates(self, workspace: pathlib.Path, slug: str, text: str):
        path = workspace / "findings" / slug / f"candidates-{DATE}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(add_cvss_contract(text))
        dataflow = path.parent / f"dataflow-{DATE}.json"
        if not dataflow.exists():
            dataflow.write_text(json.dumps({
                "schema_version": "1", "repo_slug": slug, "date": DATE,
                "analysis_coverage": {
                    "authorization": {"status": "not-applicable", "rationale": "No authorization-sensitive path exists in this fixture.", "flow_ids": []},
                    "business_logic": {"status": "not-applicable", "rationale": "No stateful business operation exists in this fixture.", "flow_ids": []},
                    "cross_repository": {"status": "not-applicable", "rationale": "No outbound repository boundary exists in this fixture.", "flow_ids": []},
                },
                "relationship_context": [],
                "business_logic_invariants": [],
                "outbound_edges": [],
                "flows": [{"flow_id": "Flow-1", "exposure": "EXTERNAL"}],
            }))

    def phase_one_outputs(self, workspace: pathlib.Path, slug: str):
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
        (directory / f"dependency-inventory-{DATE}.json").write_text(json.dumps({
            "schema_version": "1",
            "dependency_inventory": [],
            "manifest_coverage": [],
            "manifests_detected": 0,
            "package_manager_execution": False,
            "network_access": False,
        }))

    def test_initial_gate_allows_phase_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(pathlib.Path(tmp))
            result = run(
                "bash",
                str(CHECK_GATE),
                "--next-phase",
                "phase-0",
                "--workspace",
                str(workspace),
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_wrong_phase_returns_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(pathlib.Path(tmp))
            result = run(
                "bash",
                str(CHECK_GATE),
                "--next-phase",
                "phase-1",
                "--workspace",
                str(workspace),
            )
        self.assertEqual(result.returncode, 2)

    def test_direct_phase_eight_from_phase_zero_blocks_even_with_fake_prerequisites(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(pathlib.Path(tmp))
            state_path = workspace / "state.json"
            state = json.loads(state_path.read_text())
            state["phase_status"]["phase-7"] = "completed"
            state_path.write_text(json.dumps(state, indent=2) + "\n")
            findings = workspace / "findings" / "demo" / f"findings-{DATE}.md"
            findings.parent.mkdir(parents=True, exist_ok=True)
            findings.write_text("## Findings Summary\n")
            result = run(
                "bash",
                str(CHECK_GATE),
                "--next-phase",
                "phase-8",
                "--workspace",
                str(workspace),
            )
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("current_phase", result.stderr)

    def test_phase_one_gate_blocks_while_current_phase_is_zero_even_with_prerequisites(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(pathlib.Path(tmp))
            state_path = workspace / "state.json"
            state = json.loads(state_path.read_text())
            state["phase_status"]["phase-0"] = "completed"
            state_path.write_text(json.dumps(state, indent=2) + "\n")
            (workspace / "sourcecode" / "demo").mkdir(parents=True)
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

    def test_complete_phase_replay_blocks_before_artifact_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(pathlib.Path(tmp))
            (workspace / "sourcecode" / "demo").mkdir(parents=True)
            (workspace / "output" / f"run-log-{DATE}.md").write_text("clone complete\n")
            first = run(
                "bash",
                str(COMPLETE_PHASE),
                "--phase",
                "phase-0",
                "--workspace",
                str(workspace),
            )
            before = json.loads((workspace / "state.json").read_text())
            replay = run(
                "bash",
                str(COMPLETE_PHASE),
                "--phase",
                "phase-0",
                "--workspace",
                str(workspace),
            )
            after = json.loads((workspace / "state.json").read_text())
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(replay.returncode, 2, replay.stdout)
        self.assertIn("completed", replay.stderr)
        self.assertEqual(after, before)

    def test_normal_review_phase_zero_progression_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(pathlib.Path(tmp))
            (workspace / "sourcecode" / "demo").mkdir(parents=True)
            (workspace / "output" / f"run-log-{DATE}.md").write_text("clone complete\n")
            start = run(
                "bash",
                str(CHECK_GATE),
                "--next-phase",
                "phase-0",
                "--workspace",
                str(workspace),
            )
            complete = run(
                "bash",
                str(COMPLETE_PHASE),
                "--phase",
                "phase-0",
                "--workspace",
                str(workspace),
            )
            next_gate = run(
                "bash",
                str(CHECK_GATE),
                "--next-phase",
                "phase-1",
                "--workspace",
                str(workspace),
            )
            state = json.loads((workspace / "state.json").read_text())
        self.assertEqual(start.returncode, 0, start.stderr)
        self.assertEqual(complete.returncode, 0, complete.stderr)
        self.assertEqual(next_gate.returncode, 0, next_gate.stderr)
        self.assertEqual(state["current_phase"], "phase-1")

    def test_uninterrupted_phase_transitions_seed_current_epoch_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(pathlib.Path(tmp))
            (workspace / "sourcecode" / "demo").mkdir(parents=True)
            (workspace / "output" / f"run-log-{DATE}.md").write_text(
                "clone complete\n"
            )

            phase_zero = run(
                "bash", str(COMPLETE_PHASE), "--phase", "phase-0",
                "--workspace", str(workspace),
            )
            phase_one_state = json.loads((workspace / "state.json").read_text())
            self.phase_one_outputs(workspace, "demo")
            phase_one = run(
                "bash", str(COMPLETE_PHASE), "--phase", "phase-1",
                "--workspace", str(workspace),
            )
            phase_two_state = json.loads((workspace / "state.json").read_text())
            journal = (
                workspace / "output" / phase_two_state["agent_journal"]["filename"]
            ).read_text()
            events = [
                json.loads(raw)
                for raw in re.findall(r"```json\n(.*?)\n```", journal, re.DOTALL)
            ]

        self.assertEqual(phase_zero.returncode, 0, phase_zero.stderr)
        self.assertEqual(phase_one.returncode, 0, phase_one.stderr)
        self.assertEqual(phase_two_state["current_phase"], "phase-2")
        self.assertNotEqual(
            phase_one_state["agent_journal"]["epoch_id"],
            phase_two_state["agent_journal"]["epoch_id"],
        )
        for state in (phase_one_state, phase_two_state):
            journal_state = state["agent_journal"]
            intents = [
                event for event in events
                if event["event_type"] == "user-intent-recorded"
                and event["session_id"] == journal_state["session_id"]
                and event["phase"] == journal_state["epoch_phase"]
                and event["epoch_id"] == journal_state["epoch_id"]
            ]
            self.assertEqual(len(intents), 1)

    def test_check_slug_validates_one_resume_item_without_advancing_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(
                pathlib.Path(tmp), slugs=("done", "pending")
            )
            self.set_phase(workspace, "phase-1", completed=("phase-0",))
            self.phase_one_outputs(workspace, "done")
            before = json.loads((workspace / "state.json").read_text())

            done = run(
                "bash",
                str(COMPLETE_PHASE),
                "--phase",
                "phase-1",
                "--workspace",
                str(workspace),
                "--check-slug",
                "done",
            )
            pending = run(
                "bash",
                str(COMPLETE_PHASE),
                "--phase",
                "phase-1",
                "--workspace",
                str(workspace),
                "--check-slug",
                "pending",
            )
            after = json.loads((workspace / "state.json").read_text())

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("valid for slug 'done'", done.stdout)
        self.assertEqual(pending.returncode, 2)
        self.assertIn("pending", pending.stderr)
        self.assertEqual(after, before)

    def test_phase_one_requires_static_dependency_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(pathlib.Path(tmp))
            self.set_phase(workspace, "phase-1", completed=("phase-0",))
            self.phase_one_outputs(workspace, "demo")
            inventory = workspace / "findings" / "demo" / f"dependency-inventory-{DATE}.json"
            inventory.unlink()
            missing = run(
                "bash", str(COMPLETE_PHASE), "--phase", "phase-1",
                "--workspace", str(workspace),
            )
            inventory.write_text(json.dumps({
                "schema_version": "1", "dependency_inventory": [],
                "package_manager_execution": True, "network_access": False,
            }))
            unsafe = run(
                "bash", str(COMPLETE_PHASE), "--phase", "phase-1",
                "--workspace", str(workspace),
            )
        self.assertEqual(missing.returncode, 2)
        self.assertIn("dependency-inventory", missing.stderr)
        self.assertEqual(unsafe.returncode, 2)
        self.assertIn("dependency inventory invalid", unsafe.stderr)

    def test_check_slug_rejects_phase_shortcuts_and_preserves_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(pathlib.Path(tmp))
            self.set_phase(
                workspace,
                "phase-3",
                completed=("phase-0", "phase-1", "phase-2"),
            )
            self.candidates(workspace, "demo", "NO_CANDIDATES\n")
            before = (workspace / "state.json").read_text()
            result = run(
                "bash",
                str(COMPLETE_PHASE),
                "--phase",
                "phase-3",
                "--workspace",
                str(workspace),
                "--check-slug",
                "demo",
                "--skip-candidates",
            )
            after = (workspace / "state.json").read_text()
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("read-only", result.stderr)
        self.assertEqual(after, before)

    def test_normal_triage_ingest_progression_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(
                pathlib.Path(tmp),
                mode="triage",
            )
            (workspace / "input" / "triage-backlog.json").write_text("[]\n")
            start = run(
                "bash",
                str(CHECK_GATE),
                "--next-phase",
                "triage-ingest",
                "--workspace",
                str(workspace),
            )
            self.candidates(
                workspace,
                "demo",
                "## CANDIDATE-F-001\n\nImported finding: YES\nTier: P2\nDebate required: YES\n",
            )
            complete = run(
                "bash",
                str(COMPLETE_PHASE),
                "--phase",
                "triage-ingest",
                "--workspace",
                str(workspace),
            )
            next_gate = run(
                "bash",
                str(CHECK_GATE),
                "--next-phase",
                "phase-4",
                "--workspace",
                str(workspace),
            )
            state = json.loads((workspace / "state.json").read_text())
        self.assertEqual(start.returncode, 0, start.stderr)
        self.assertEqual(complete.returncode, 0, complete.stderr)
        self.assertEqual(next_gate.returncode, 0, next_gate.stderr)
        self.assertEqual(state["current_phase"], "phase-4")

    def test_uninterrupted_triage_transition_seeds_phase_four_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(pathlib.Path(tmp), mode="triage")
            (workspace / "input" / "triage-backlog.json").write_text("[]\n")
            self.candidates(workspace, "demo", "NO_CANDIDATES\n")

            ingest = run(
                "bash", str(COMPLETE_PHASE), "--phase", "triage-ingest",
                "--workspace", str(workspace),
            )
            phase_four_state = json.loads((workspace / "state.json").read_text())
            debate = run(
                "bash", str(COMPLETE_PHASE), "--phase", "phase-4",
                "--workspace", str(workspace), "--no-debate-needed",
            )
            journal = (
                workspace / "output" / phase_four_state["agent_journal"]["filename"]
            ).read_text()
            events = [
                json.loads(raw)
                for raw in re.findall(r"```json\n(.*?)\n```", journal, re.DOTALL)
            ]
            current = phase_four_state["agent_journal"]
            intents = [
                event for event in events
                if event["event_type"] == "user-intent-recorded"
                and event["session_id"] == current["session_id"]
                and event["phase"] == "phase-4"
                and event["epoch_id"] == current["epoch_id"]
            ]

        self.assertEqual(ingest.returncode, 0, ingest.stderr)
        self.assertEqual(debate.returncode, 0, debate.stderr)
        self.assertEqual(len(intents), 1)

    def test_acceptance_crash_recovery_preserves_seeded_next_epoch_intent(self):
        failpoints = (
            "after-intent",
            "after-prepared-append",
            "after-state-replace",
            "before-git-commit",
            "after-git-commit-before-ref",
        )
        for failpoint in failpoints:
            with self.subTest(failpoint=failpoint), tempfile.TemporaryDirectory() as tmp:
                workspace = self.init_workspace(pathlib.Path(tmp))
                (workspace / "sourcecode" / "demo").mkdir(parents=True)
                (workspace / "output" / f"run-log-{DATE}.md").write_text(
                    "clone complete\n"
                )
                failed = run(
                    "bash", str(COMPLETE_PHASE), "--phase", "phase-0",
                    "--workspace", str(workspace),
                    env=os.environ | {"NIGHTFALCON_JOURNAL_FAILPOINT": failpoint},
                )
                recovered = run(
                    "python3", str(AGENT_JOURNAL), "validate",
                    "--workspace", str(workspace),
                )
                state = json.loads((workspace / "state.json").read_text())
                journal = (
                    workspace / "output" / state["agent_journal"]["filename"]
                ).read_text()
                events = [
                    json.loads(raw)
                    for raw in re.findall(
                        r"```json\n(.*?)\n```", journal, re.DOTALL
                    )
                ]
                current = state["agent_journal"]
                intents = [
                    event for event in events
                    if event["event_type"] == "user-intent-recorded"
                    and event["phase"] == "phase-1"
                    and event["epoch_id"] == current["epoch_id"]
                ]

                self.assertEqual(failed.returncode, 2, failed.stdout)
                self.assertIn("injected acceptance failure", failed.stderr)
                self.assertEqual(recovered.returncode, 0, recovered.stderr)
                self.assertEqual(state["current_phase"], "phase-1")
                self.assertEqual(len(intents), 1)

    def test_v2_transaction_rejects_seed_state_mismatch_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(pathlib.Path(tmp))
            (workspace / "sourcecode" / "demo").mkdir(parents=True)
            (workspace / "output" / f"run-log-{DATE}.md").write_text(
                "clone complete\n"
            )
            failed = run(
                "bash", str(COMPLETE_PHASE), "--phase", "phase-0",
                "--workspace", str(workspace),
                env=os.environ | {
                    "NIGHTFALCON_JOURNAL_FAILPOINT": "after-intent"
                },
            )
            transaction_path = (
                workspace / ".nightfalcon-journal-transaction.json"
            )
            transaction = json.loads(transaction_path.read_text())
            new_state = json.loads(base64.b64decode(
                transaction["state"]["new_base64"], validate=True
            ))
            new_state["repo_slugs"] = ["demo", "second"]
            new_state_bytes = (
                json.dumps(new_state, sort_keys=True, indent=2) + "\n"
            ).encode()
            transaction["state"]["new_base64"] = base64.b64encode(
                new_state_bytes
            ).decode("ascii")
            transaction["state"]["new_sha256"] = hashlib.sha256(
                new_state_bytes
            ).hexdigest()
            transaction_path.write_text(json.dumps(transaction) + "\n")
            state_before = (workspace / "state.json").read_bytes()
            manifest_path = workspace / "output" / "session-manifest.json"
            manifest_before = manifest_path.read_bytes()
            journal_path = workspace / "output" / f"agent-conversation-{DATE}.md"
            journal_before = journal_path.read_bytes()

            recovered = run(
                "python3", str(AGENT_JOURNAL), "validate",
                "--workspace", str(workspace),
            )
            state_after = (workspace / "state.json").read_bytes()
            manifest_after = manifest_path.read_bytes()
            journal_after = journal_path.read_bytes()

        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertEqual(recovered.returncode, 2, recovered.stdout)
        self.assertIn("next-epoch intent differs", recovered.stderr)
        self.assertEqual(state_after, state_before)
        self.assertEqual(manifest_after, manifest_before)
        self.assertEqual(journal_after, journal_before)

    def test_complete_phase_rejects_unknown_mode_invalid_done_and_invalid_current(self):
        cases = (
            ("review", "phase-0", "unknown-phase"),
            ("review", "triage-ingest", "triage-ingest"),
            ("review", "done", "phase-8"),
            ("review", "unknown-phase", "phase-0"),
            ("invalid-mode", "phase-0", "phase-0"),
        )
        for mode, current, requested in cases:
            with self.subTest(mode=mode, current=current, requested=requested), tempfile.TemporaryDirectory() as tmp:
                workspace = self.init_workspace(pathlib.Path(tmp))
                state_path = workspace / "state.json"
                state = json.loads(state_path.read_text())
                state["mode"] = mode
                state["current_phase"] = current
                state_path.write_text(json.dumps(state, indent=2) + "\n")
                result = run(
                    "bash",
                    str(COMPLETE_PHASE),
                    "--phase",
                    requested,
                    "--workspace",
                    str(workspace),
                )
            self.assertEqual(result.returncode, 2, result.stdout)

    def test_next_gate_rejects_unknown_done_and_mode_invalid_state(self):
        cases = (
            ("review", "unknown-phase", "unknown-phase"),
            ("review", "done", "phase-8"),
            ("triage", "phase-0", "phase-0"),
            ("invalid-mode", "phase-0", "phase-0"),
        )
        for mode, current, requested in cases:
            with self.subTest(mode=mode, current=current, requested=requested), tempfile.TemporaryDirectory() as tmp:
                workspace = self.init_workspace(pathlib.Path(tmp))
                state_path = workspace / "state.json"
                state = json.loads(state_path.read_text())
                state["mode"] = mode
                state["current_phase"] = current
                state_path.write_text(json.dumps(state, indent=2) + "\n")
                result = run(
                    "bash",
                    str(CHECK_GATE),
                    "--next-phase",
                    requested,
                    "--workspace",
                    str(workspace),
                )
            self.assertEqual(result.returncode, 2, result.stdout)

    def test_disallowed_write_returns_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(pathlib.Path(tmp))
            result = run(
                "bash",
                str(CHECK_GATE),
                "--check-path",
                "/tmp/outside.md",
                "--workspace",
                str(workspace),
            )
        self.assertEqual(result.returncode, 2)

    def test_agent_journal_direct_writes_are_blocked_in_every_phase(self):
        """A broad output allowlist must never permit canonical journal mutation."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(pathlib.Path(tmp))
            for phase in (
                "phase-0", "phase-1", "phase-2", "phase-3", "phase-4",
                "phase-5", "phase-6", "phase-7", "phase-8",
            ):
                with self.subTest(phase=phase):
                    self.set_phase(workspace, phase)
                    for port, gate in JOURNAL_GATES:
                        with self.subTest(port=port):
                            result = run(
                                "bash", str(gate), "--check-path",
                                f"output/agent-conversation-{DATE}.md",
                                "--workspace", str(workspace),
                            )
                            self.assertEqual(result.returncode, 2, result.stdout)
                            self.assertIn("agent-journal.py record", result.stderr)

                            for alias in (
                                f"output/agent-conversation-{DATE}.md.bak",
                                "output/agent-conversation-2026-07-15.md",
                                f"output/agent_conversation-{DATE}.md",
                                "output/agent-conversation.md",
                            ):
                                denied = run(
                                    "bash", str(gate), "--check-path", alias,
                                    "--workspace", str(workspace),
                                )
                                self.assertEqual(denied.returncode, 2, denied.stdout)
                            if phase == "phase-8":
                                unrelated = run(
                                    "bash", str(gate), "--check-path", "output/unrelated.md",
                                    "--workspace", str(workspace),
                                )
                                self.assertEqual(unrelated.returncode, 0, unrelated.stderr)

    def test_next_phase_and_enforce_output_fail_closed_for_invalid_journal(self):
        """Skipping journal validation would allow corrupted run provenance through a gate."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(pathlib.Path(tmp))
            journal = workspace / "output" / f"agent-conversation-{DATE}.md"
            journal.write_text("corrupt\n")
            next_phase = run(
                "bash", str(CHECK_GATE), "--next-phase", "phase-0",
                "--workspace", str(workspace),
            )
            enforce_output = run(
                "bash", str(CHECK_GATE), "--enforce-output",
                "--workspace", str(workspace),
            )
        self.assertEqual(next_phase.returncode, 2)
        self.assertIn("journal validation failed", next_phase.stderr)
        self.assertEqual(enforce_output.returncode, 2)
        self.assertIn("journal validation failed", enforce_output.stderr)

    def test_check_path_enforces_canonical_workspace_containment(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            workspace = self.init_workspace(base)
            self.set_phase(workspace, "phase-1")
            outside = base / "outside"
            outside.mkdir()
            sibling = base / "workspace-sibling"
            sibling.mkdir()
            (workspace / "findings" / "symlink-parent").symlink_to(
                outside,
                target_is_directory=True,
            )
            blocked_paths = (
                "findings/../escape.md",
                str(outside / "absolute.md"),
                str(sibling / "findings" / "sibling-prefix.md"),
                "findings/symlink-parent/escaped.md",
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
                    self.assertIn("BLOCKED", result.stderr)

            allowed = run(
                "bash",
                str(CHECK_GATE),
                "--check-path",
                "findings/demo folder/report name.md",
                "--workspace",
                str(workspace),
            )
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_exact_dated_run_log_is_allowed_in_triage_and_later_phases_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(pathlib.Path(tmp))
            for phase in ("triage-ingest", "phase-4", "phase-7"):
                with self.subTest(phase=phase):
                    self.set_phase(workspace, phase)
                    exact = run(
                        "bash",
                        str(CHECK_GATE),
                        "--check-path",
                        f"output/run-log-{DATE}.md",
                        "--workspace",
                        str(workspace),
                    )
                    self.assertEqual(exact.returncode, 0, exact.stderr)
                    for adjacent in (
                        f"output/run-log-{DATE}.md.bak",
                        "output/run-log-2026-07-15.md",
                        "output/other.md",
                    ):
                        denied = run(
                            "bash",
                            str(CHECK_GATE),
                            "--check-path",
                            adjacent,
                            "--workspace",
                            str(workspace),
                        )
                        self.assertEqual(denied.returncode, 2, denied.stdout)

    def test_no_poc_needed_creates_empty_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(pathlib.Path(tmp))
            self.set_phase(workspace, "phase-6", completed=("phase-4",))
            self.candidates(workspace, "demo", "NO_CANDIDATES\n")
            result = run(
                "bash",
                str(COMPLETE_PHASE),
                "--phase",
                "phase-6",
                "--workspace",
                str(workspace),
                "--no-poc-needed",
            )
            manifest = (
                workspace
                / "output"
                / "proof_of_concept"
                / "demo"
                / f"poc-manifest-{DATE}.json"
            )
            payload = json.loads(manifest.read_text())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["pocs"], [])

    def test_all_empty_contract_advances_through_no_poc_stub(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(pathlib.Path(tmp), ("alpha", "beta"))
            self.set_phase(
                workspace,
                "phase-3",
                completed=("phase-0", "phase-1", "phase-2"),
            )
            for slug in ("alpha", "beta"):
                self.candidates(workspace, slug, "NO_CANDIDATES\n")

            commands = (
                ("bash", str(COMPLETE_PHASE), "--phase", "phase-3", "--workspace", str(workspace), "--skip-candidates"),
                ("bash", str(CHECK_GATE), "--next-phase", "phase-4", "--workspace", str(workspace)),
                ("bash", str(COMPLETE_PHASE), "--phase", "phase-4", "--workspace", str(workspace), "--no-debate-needed"),
                ("bash", str(CHECK_GATE), "--next-phase", "phase-5", "--workspace", str(workspace)),
                ("bash", str(COMPLETE_PHASE), "--phase", "phase-5", "--workspace", str(workspace), "--no-validation-needed"),
                ("bash", str(CHECK_GATE), "--next-phase", "phase-6", "--workspace", str(workspace)),
                ("bash", str(COMPLETE_PHASE), "--phase", "phase-6", "--workspace", str(workspace), "--no-poc-needed"),
                ("bash", str(CHECK_GATE), "--next-phase", "phase-7", "--workspace", str(workspace)),
            )
            for command in commands:
                if str(COMPLETE_PHASE) in command:
                    self.record_intent(workspace)
                result = run(*command)
                self.assertEqual(result.returncode, 0, result.stderr)

            state = json.loads((workspace / "state.json").read_text())
            debate_paths = [
                workspace / "findings" / slug / f"debate-{DATE}.md"
                for slug in ("alpha", "beta")
            ]
            self.assertTrue(all(path.is_file() for path in debate_paths))
            debate_texts = [path.read_text() for path in debate_paths]
            manifests = [
                json.loads(
                    (
                        workspace
                        / "output"
                        / "proof_of_concept"
                        / slug
                        / f"poc-manifest-{DATE}.json"
                    ).read_text()
                )
                for slug in ("alpha", "beta")
            ]
        self.assertEqual(state["current_phase"], "phase-7")
        self.assertTrue(all("NO_DEBATE_CANDIDATES" in text for text in debate_texts))
        self.assertTrue(all("Final Disposition: NEEDS-REVIEW" in text for text in debate_texts))
        self.assertTrue(all(item["pocs"] == [] for item in manifests))

    def test_mixed_phase_three_outputs_accept_candidates_and_empty_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(pathlib.Path(tmp), ("clean", "risky"))
            self.set_phase(
                workspace,
                "phase-3",
                completed=("phase-0", "phase-1", "phase-2"),
            )
            self.candidates(workspace, "clean", "NO_CANDIDATES\n")
            self.candidates(
                workspace,
                "risky",
                "## CANDIDATE-F-001\n\nTier: P2\nDebate required: YES\n",
            )
            result = run(
                "bash",
                str(COMPLETE_PHASE),
                "--phase",
                "phase-3",
                "--workspace",
                str(workspace),
            )
            gate = run(
                "bash",
                str(CHECK_GATE),
                "--next-phase",
                "phase-4",
                "--workspace",
                str(workspace),
            )
            clean_debate = workspace / "findings" / "clean" / f"debate-{DATE}.md"
            clean_debate.write_text(
                "NO_DEBATE_CANDIDATES\nFinal Disposition: NEEDS-REVIEW\n"
            )
            debate = workspace / "findings" / "risky" / f"debate-{DATE}.md"
            debate.write_text("Final Disposition: CONFIRMED\n")
            self.record_intent(workspace)
            complete_debate = run(
                "bash",
                str(COMPLETE_PHASE),
                "--phase",
                "phase-4",
                "--workspace",
                str(workspace),
            )
            validation_gate = run(
                "bash",
                str(CHECK_GATE),
                "--next-phase",
                "phase-5",
                "--workspace",
                str(workspace),
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(gate.returncode, 0, gate.stderr)
        self.assertEqual(complete_debate.returncode, 0, complete_debate.stderr)
        self.assertEqual(validation_gate.returncode, 0, validation_gate.stderr)

    def test_no_debate_flag_blocks_when_any_real_candidate_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(pathlib.Path(tmp), ("clean", "risky"))
            self.set_phase(workspace, "phase-4", completed=("phase-3",))
            self.candidates(workspace, "clean", "NO_CANDIDATES\n")
            self.candidates(
                workspace,
                "risky",
                "## CANDIDATE-F-001\n\nTier: P0\nDebate required: YES\n",
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
            state = json.loads((workspace / "state.json").read_text())
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires debate", result.stderr)
        self.assertEqual(state["current_phase"], "phase-4")

    def test_normal_phase_four_requires_debate_when_candidate_omits_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(pathlib.Path(tmp))
            self.set_phase(workspace, "phase-4", completed=("phase-3",))
            self.candidates(
                workspace,
                "demo",
                "## CANDIDATE-F-001\n\nTier: P0\n",
            )
            result = run(
                "bash",
                str(COMPLETE_PHASE),
                "--phase",
                "phase-4",
                "--workspace",
                str(workspace),
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("debate", result.stderr.lower())

    def test_phase_three_blocks_noncanonical_debate_required_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(pathlib.Path(tmp))
            self.set_phase(
                workspace,
                "phase-3",
                completed=("phase-0", "phase-1", "phase-2"),
            )
            self.candidates(
                workspace,
                "demo",
                "## CANDIDATE-F-001\n\nTier: P0\nDebate required: MAYBE\n",
            )
            result = run(
                "bash",
                str(COMPLETE_PHASE),
                "--phase",
                "phase-3",
                "--workspace",
                str(workspace),
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Debate required", result.stderr)

    def test_phase_three_accepts_documented_debate_required_suffixes(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(pathlib.Path(tmp))
            self.set_phase(
                workspace,
                "phase-3",
                completed=("phase-0", "phase-1", "phase-2"),
            )
            self.candidates(
                workspace,
                "demo",
                "## CANDIDATE-F-001\n\nTier: P2\nDebate required: YES (P0/P1/P2)\n"
                "## CANDIDATE-F-002\n\nTier: P3\n**Debate required:** NO (P3/P4)\n",
            )
            result = run(
                "bash",
                str(COMPLETE_PHASE),
                "--phase",
                "phase-3",
                "--workspace",
                str(workspace),
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_phase_three_blocks_missing_duplicate_and_bad_debate_suffixes(self):
        invalid_records = {
            "missing": "Tier: P0\n",
            "duplicate": "Tier: P0\nDebate required: YES\nDebate required: YES\n",
            "mismatched-yes": "Tier: P0\nDebate required: YES (P3/P4)\n",
            "mismatched-no": "Tier: P3\nDebate required: NO (P0/P1/P2)\n",
            "unknown": "Tier: P0\nDebate required: YES (P0)\n",
        }
        for label, record in invalid_records.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                workspace = self.init_workspace(pathlib.Path(tmp))
                self.set_phase(
                    workspace,
                    "phase-3",
                    completed=("phase-0", "phase-1", "phase-2"),
                )
                self.candidates(
                    workspace,
                    "demo",
                    f"## CANDIDATE-F-001\n\n{record}",
                )
                result = run(
                    "bash",
                    str(COMPLETE_PHASE),
                    "--phase",
                    "phase-3",
                    "--workspace",
                    str(workspace),
                )
            self.assertEqual(result.returncode, 2)
            self.assertIn("Debate required", result.stderr)

    def test_phase_three_rejects_no_candidates_mixed_with_candidate_sections(self):
        for skip in (False, True):
            with self.subTest(skip=skip), tempfile.TemporaryDirectory() as tmp:
                workspace = self.init_workspace(pathlib.Path(tmp))
                self.set_phase(
                    workspace,
                    "phase-3",
                    completed=("phase-0", "phase-1", "phase-2"),
                )
                self.candidates(
                    workspace,
                    "demo",
                    "NO_CANDIDATES\n## CANDIDATE-F-001\n\nTier: P0\nDebate required: YES\n",
                )
                command = [
                    "bash",
                    str(COMPLETE_PHASE),
                    "--phase",
                    "phase-3",
                    "--workspace",
                    str(workspace),
                ]
                if skip:
                    command.append("--skip-candidates")
                result = run(*command)
            self.assertEqual(result.returncode, 2)
            self.assertIn("NO_CANDIDATES", result.stderr)

    def test_phase_four_blocks_noncanonical_final_disposition(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(pathlib.Path(tmp))
            self.set_phase(workspace, "phase-4", completed=("phase-3",))
            self.candidates(
                workspace,
                "demo",
                "## CANDIDATE-F-001\n\nTier: P0\nDebate required: YES\n",
            )
            debate = workspace / "findings" / "demo" / f"debate-{DATE}.md"
            debate.write_text("Final Disposition: CONFIRMED-ish\n")
            result = run(
                "bash",
                str(COMPLETE_PHASE),
                "--phase",
                "phase-4",
                "--workspace",
                str(workspace),
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("non-canonical", result.stderr)

    def _write_v4_poc_workspace(self, workspace: pathlib.Path, *, catalog_keys):
        """Build a self-contained (schema v4) phase-6 workspace for one repo
        under output/proof_of_concept/demo/. `catalog_keys` is the set of keys
        the reference catalog documents; the PoC declares TARGET_HOST+AUTH_TOKEN."""
        slug = "demo"
        self.set_phase(workspace, "phase-6", completed=("phase-4",))
        self.candidates(workspace, slug, "## CANDIDATE-F-001\n\nTier: P0\n")
        poc_dir = workspace / "output" / "proof_of_concept" / slug
        poc_dir.mkdir(parents=True, exist_ok=True)
        (poc_dir / "F-001-mint.sh").write_text(
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
        (poc_dir / f"POC-GUIDE-{DATE}.md").write_text(
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
        (poc_dir / "poc-config.env").write_text(catalog)
        (workspace / "output" / "proof_of_concept" / "run-all.sh").write_text(
            "#!/usr/bin/env bash\n"
        )
        manifest = {
            "schema_version": "4",
            "repo_slug": slug,
            "date": DATE,
            "config_path": f"output/proof_of_concept/{slug}/poc-config.env",
            "guide_path": f"output/proof_of_concept/{slug}/POC-GUIDE-{DATE}.md",
            "skipped": [],
            "pocs": [
                {
                    "finding_id": "F-001",
                    "tier": "P0",
                    "disposition": "CONFIRMED",
                    "title": "t",
                    "script_path": f"output/proof_of_concept/{slug}/F-001-mint.sh",
                    "script_type": "curl-shell",
                    "confirm_path": "script",
                    "confirm_note": None,
                    "placeholders": ["TARGET_HOST", "AUTH_TOKEN"],
                    "generated_by": "unknown",
                    "verdict": "VALID",
                    "regenerated": False,
                    "reviewer_envelopes": [
                        {
                            "reviewer": reviewer,
                            "verdict": "VALID",
                            "reasoning": "The script is accurate, safe, and complete.",
                            "accuracy_ok": True,
                            "safety_ok": True,
                            "completeness_ok": True,
                            "suggested_fix": None,
                        }
                        for reviewer in ("A", "B")
                    ],
                }
            ],
        }
        (poc_dir / f"poc-manifest-{DATE}.json").write_text(json.dumps(manifest))

    def test_phase_six_accepts_self_contained_v4_manifest_under_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(pathlib.Path(tmp))
            self._write_v4_poc_workspace(
                workspace, catalog_keys=("TARGET_HOST", "AUTH_TOKEN")
            )
            result = run(
                "bash", str(COMPLETE_PHASE), "--phase", "phase-6",
                "--workspace", str(workspace),
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_phase_six_blocks_when_catalog_omits_a_used_parameter(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self.init_workspace(pathlib.Path(tmp))
            # Catalog documents only TARGET_HOST; the PoC also uses AUTH_TOKEN.
            self._write_v4_poc_workspace(workspace, catalog_keys=("TARGET_HOST",))
            result = run(
                "bash", str(COMPLETE_PHASE), "--phase", "phase-6",
                "--workspace", str(workspace),
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("AUTH_TOKEN", result.stderr)

    def _phase7_ws(self, base, *, cvss_score, tier):
        """Build a minimal phase-7 workspace with one finding carrying the
        given cvss_score + tier, ready for `complete-phase.sh --phase phase-7`."""
        workspace = self.init_workspace(base)
        self.set_phase(workspace, "phase-7", completed=("phase-6",))
        output = workspace / "output"
        output.mkdir(parents=True, exist_ok=True)
        (output / f"run-log-{DATE}.md").write_text(
            f"[{DATE}T12:00:00Z] demo: multitenant-scope=YES — fixture\n"
        )
        self.candidates(workspace, "demo", "## CANDIDATE-F-001\n\nTier: P0\n")
        fd = workspace / "findings" / "demo"
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
                    "cvss_score": cvss_score,
                    "cvss_vector": "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N",
                    "tier": tier,
                },
            }],
        }
        (fd / f"receipt-{DATE}.json").write_text(json.dumps(receipt))
        (fd / f"pattern-tags-{DATE}.json").write_text(
            json.dumps({"schema_version": "1", "repo_slug": "demo", "tags": []}))
        finding = {
            "finding_id": "F-001", "tier": tier, "cvss_score": cvss_score,
            "cvss_vector": "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N",
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
        # Phase-7 also emits the SARIF projection; build it mechanically so the
        # gate's .sarif check is satisfied for the happy-path fixtures.
        build_sarif = ROOT / "scripts" / "report" / "build-sarif.py"
        run("python3", str(build_sarif),
            "--input", str(fd / f"findings-{DATE}.json"),
            "--output", str(fd / f"findings-{DATE}.sarif"))
        return workspace

    def test_phase7_consistent_cvss_and_tier_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._phase7_ws(pathlib.Path(tmp), cvss_score=9.3, tier="P0")
            r = run("bash", str(COMPLETE_PHASE), "--phase", "phase-7", "--workspace", str(ws))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_phase7_tier_score_mismatch_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._phase7_ws(pathlib.Path(tmp), cvss_score=4.2, tier="P0")
            r = run("bash", str(COMPLETE_PHASE), "--phase", "phase-7", "--workspace", str(ws))
        self.assertEqual(r.returncode, 2)
        self.assertIn("tier band", r.stderr)

    def test_phase7_out_of_range_cvss_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._phase7_ws(pathlib.Path(tmp), cvss_score=11.5, tier="P0")
            r = run("bash", str(COMPLETE_PHASE), "--phase", "phase-7", "--workspace", str(ws))
        self.assertEqual(r.returncode, 2)
        self.assertIn("0.0-10.0", r.stderr)

    def test_phase7_missing_sarif_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._phase7_ws(pathlib.Path(tmp), cvss_score=9.3, tier="P0")
            (ws / "findings" / "demo" / f"findings-{DATE}.sarif").unlink()
            r = run("bash", str(COMPLETE_PHASE), "--phase", "phase-7", "--workspace", str(ws))
        self.assertEqual(r.returncode, 2)
        self.assertIn(".sarif", r.stderr)

    def test_phase7_sarif_result_count_mismatch_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._phase7_ws(pathlib.Path(tmp), cvss_score=9.3, tier="P0")
            sf = ws / "findings" / "demo" / f"findings-{DATE}.sarif"
            s = json.loads(sf.read_text())
            s["runs"][0]["results"] = []  # drop the result -> count mismatch
            sf.write_text(json.dumps(s))
            r = run("bash", str(COMPLETE_PHASE), "--phase", "phase-7", "--workspace", str(ws))
        self.assertEqual(r.returncode, 2)
        self.assertIn(".sarif", r.stderr)


if __name__ == "__main__":
    unittest.main()
