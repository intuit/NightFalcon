import json
import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
INIT_REVIEW = ROOT / "scripts" / "init-review.sh"
JOURNAL = ROOT / "scripts" / "agent-journal.py"


class RuntimeScriptTests(unittest.TestCase):
    def isolated_env(self):
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(
            {
                "PLUGIN_ROOT": str(ROOT),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
            }
        )
        return env

    def run_init(self, workspace, *extra_args, check=True):
        return subprocess.run(
            [
                "bash",
                str(INIT_REVIEW),
                "--workspace",
                workspace,
                "--date",
                "2026-07-14",
                *extra_args,
            ],
            env=self.isolated_env(),
            check=check,
            capture_output=True,
            text=True,
        )

    def write_model_pin(self, directory, model="gpt-test", session_id="session-test"):
        path = pathlib.Path(directory, "model-pin.json")
        path.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "session_id": session_id,
                    "model": model,
                    "source": "SessionStart",
                }
            )
        )
        return path

    def run_journal(self, workspace, *arguments):
        return subprocess.run(
            ["/usr/local/bin/python3", str(JOURNAL), *arguments, "--workspace", str(workspace)],
            env=self.isolated_env(),
            check=False,
            capture_output=True,
            text=True,
        )

    def test_init_creates_journal_and_preserves_existing_port_state(self):
        """Missing journal lifecycle initialization breaks this port-state upgrade."""
        with tempfile.TemporaryDirectory() as tmp:
            self.run_init(tmp)
            state_path = pathlib.Path(tmp, "state.json")
            state = json.loads(state_path.read_text())
            state["port_extension"] = {"keep": True}
            state_path.write_text(json.dumps(state, indent=2) + "\n")
            resumed = self.run_init(tmp, check=False)
            after = json.loads(state_path.read_text())
            journal_path = pathlib.Path(tmp, "output", "agent-conversation-2026-07-14.md")
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            self.assertEqual(after["port_extension"], {"keep": True})
            self.assertTrue(journal_path.is_file())

    def test_resume_interrupts_orphaned_agent_and_rejects_old_epoch(self):
        """Without epoch rotation, an interrupted worker can continue recording."""
        with tempfile.TemporaryDirectory() as tmp:
            self.run_init(tmp, "--slug", "demo")
            before = json.loads(pathlib.Path(tmp, "state.json").read_text())["agent_journal"]
            requested = self.run_journal(
                tmp, "record", "--event-type", "agent-spawn-requested",
                "--session-id", before["session_id"], "--phase", before["epoch_phase"],
                "--epoch-id", before["epoch_id"],
                "--agent-id", "66666666-6666-4666-8666-666666666666",
                "--agent-role", "phase-worker", "--action-code", "spawn-agent",
                "--status-code", "requested", "--reason-code", "phase-contract",
            )
            started = self.run_journal(
                tmp, "record", "--event-type", "agent-started",
                "--session-id", before["session_id"], "--phase", before["epoch_phase"],
                "--epoch-id", before["epoch_id"],
                "--agent-id", "66666666-6666-4666-8666-666666666666",
                "--agent-role", "phase-worker", "--action-code", "start-agent",
                "--status-code", "started", "--reason-code", "phase-contract",
            )
            self.assertEqual(requested.returncode, 0, requested.stderr)
            self.assertEqual(started.returncode, 0, started.stderr)
            blocked = self.run_init(tmp, check=False)
            terminal = self.run_journal(
                tmp, "record", "--event-type", "agent-failed",
                "--session-id", before["session_id"], "--phase", before["epoch_phase"],
                "--epoch-id", before["epoch_id"],
                "--agent-id", "66666666-6666-4666-8666-666666666666",
                "--agent-role", "phase-worker", "--action-code", "fail-agent",
                "--status-code", "failed", "--reason-code", "worker-failure",
            )
            self.assertEqual(terminal.returncode, 0, terminal.stderr)
            resumed = self.run_init(tmp, check=False)
            after = json.loads(pathlib.Path(tmp, "state.json").read_text())["agent_journal"]
            validated = self.run_journal(tmp, "validate")
            stale = self.run_journal(
                tmp, "record", "--event-type", "decision-recorded",
                "--session-id", before["session_id"], "--phase", before["epoch_phase"],
                "--epoch-id", before["epoch_id"], "--agent-role", "orchestrator",
                "--action-code", "record-decision", "--decision-code", "deferred",
                "--reason-code", "evidence-gap",
            )

        self.assertEqual(blocked.returncode, 2, blocked.stdout + blocked.stderr)
        self.assertIn("exact terminal events", blocked.stderr)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertNotEqual(after["epoch_id"], before["epoch_id"])
        self.assertEqual(validated.returncode, 0, validated.stderr)
        events = json.loads(validated.stdout)["events"]
        self.assertEqual(sum(event["event_type"] == "run-resumed" for event in events), 1)
        self.assertTrue(any(
            event["event_type"] == "agent-failed"
            and event.get("agent_id") == "66666666-6666-4666-8666-666666666666"
            and event.get("status_code") == "failed"
            and event.get("reason_code") == "worker-failure"
            for event in events
        ))
        self.assertEqual(stale.returncode, 2)
        self.assertIn("stale epoch", stale.stderr)

    def test_record_rejects_reused_agent_attempt_id_after_terminal(self):
        """A terminal attempt ID cannot be recycled into a second lifecycle."""
        with tempfile.TemporaryDirectory() as tmp:
            self.run_init(tmp)
            before = json.loads(pathlib.Path(tmp, "state.json").read_text())["agent_journal"]
            request = (
                "--session-id", before["session_id"], "--phase", before["epoch_phase"],
                "--epoch-id", before["epoch_id"],
                "--agent-id", "77777777-7777-4777-8777-777777777777",
                "--agent-role", "phase-worker", "--reason-code", "phase-contract",
            )
            results = []
            for event_type, action_code, status_code in (
                ("agent-spawn-requested", "spawn-agent", "requested"),
                ("agent-started", "start-agent", "started"),
                ("agent-completed", "complete-agent", "completed"),
            ):
                results.append(self.run_journal(
                    tmp, "record", "--event-type", event_type, *request,
                    "--action-code", action_code, "--status-code", status_code,
                ))
            for result in results:
                self.assertEqual(result.returncode, 0, result.stderr)
            reused = self.run_journal(
                tmp, "record", "--event-type", "agent-spawn-requested", *request,
                "--action-code", "spawn-agent", "--status-code", "requested",
            )

        self.assertEqual(reused.returncode, 2, reused.stderr)
        self.assertIn("globally unique", reused.stderr)

    def test_init_creates_codex_marker_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as pins:
            pin = self.write_model_pin(pins)
            self.run_init(tmp, "--model-pin-file", str(pin))

            state = json.loads(pathlib.Path(tmp, "state.json").read_text())
            manifest = json.loads(
                pathlib.Path(tmp, "output", "session-manifest.json").read_text()
            )
            expected_version = json.loads(
                pathlib.Path(ROOT, ".codex-plugin", "plugin.json").read_text()
            )["version"]
            self.assertEqual(
                pathlib.Path(tmp, ".nightfalcon-review").read_text(),
                "nightfalcon\n",
            )
            self.assertEqual(state["model"], "gpt-test")
            self.assertEqual(state["model_policy"], "user-selected")
            self.assertEqual(state["model_provenance"], "available")
            self.assertNotIn("model_lock", state)
            self.assertNotIn("model_epoch", state)
            self.assertEqual(len(state["model_history"]), 1)
            self.assertNotIn("epoch", state["model_history"][0])
            self.assertEqual(state["model_history"][0]["model"], "gpt-test")
            self.assertEqual(state["model_history"][0]["source"], "SessionStart")
            self.assertRegex(
                state["model_history"][0]["selected_at"], r"^\d{4}-\d{2}-\d{2}T"
            )
            self.assertEqual(state["plugin_version"], expected_version)
            self.assertNotIn("phase_models", state)
            self.assertEqual(manifest["model"], "gpt-test")
            self.assertEqual(manifest["model_policy"], "user-selected")
            self.assertEqual(manifest["model_provenance"], "available")
            self.assertNotIn("model_lock", manifest)
            self.assertNotIn("model_epoch", manifest)
            self.assertEqual(manifest["plugin"]["version"], expected_version)

    def test_init_uses_unknown_unavailable_provenance_when_pin_is_unusable(self):
        cases = (
            "absent",
            "missing",
            "malformed",
            "empty",
            "missing-schema",
            "boolean-schema",
            "integer-schema",
            "unsupported-schema",
        )
        for case in cases:
            with (
                self.subTest(case=case),
                tempfile.TemporaryDirectory() as tmp,
                tempfile.TemporaryDirectory() as pins,
            ):
                pin = pathlib.Path(pins, "model-pin.json")
                arguments = []
                if case == "missing":
                    arguments = ["--model-pin-file", str(pin)]
                elif case == "malformed":
                    pin.write_text("not-json")
                    arguments = ["--model-pin-file", str(pin)]
                elif case == "empty":
                    pin.write_text("{}\n")
                    arguments = ["--model-pin-file", str(pin)]
                elif case in {
                    "missing-schema",
                    "boolean-schema",
                    "integer-schema",
                    "unsupported-schema",
                }:
                    schema_versions = {
                        "missing-schema": None,
                        "boolean-schema": True,
                        "integer-schema": 1,
                        "unsupported-schema": "2",
                    }
                    document = {
                        "session_id": "session-test",
                        "model": "gpt-test",
                        "source": "SessionStart",
                    }
                    if case != "missing-schema":
                        document["schema_version"] = schema_versions[case]
                    pin.write_text(json.dumps(document) + "\n")
                    arguments = ["--model-pin-file", str(pin)]

                result = self.run_init(tmp, *arguments, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                state = json.loads(pathlib.Path(tmp, "state.json").read_text())
                manifest = json.loads(
                    pathlib.Path(tmp, "output", "session-manifest.json").read_text()
                )

                self.assertEqual(state["model"], "unknown")
                self.assertEqual(state["model_policy"], "user-selected")
                self.assertEqual(state["model_provenance"], "unavailable")
                self.assertEqual(state["model_history"], [])
                self.assertEqual(manifest["model"], "unknown")
                self.assertEqual(manifest["model_provenance"], "unavailable")

    def test_existing_state_bypasses_absent_or_malformed_pin_and_preserves_fields(self):
        cases = ("absent", "missing", "malformed", "empty")
        original = {
            "date": "2026-07-14",
            "current_phase": "phase-0",
            "git_checkpoints": [],
            "legacy": True,
            "model_lock": {"mode": "exact"},
        }
        for case in cases:
            with (
                self.subTest(case=case),
                tempfile.TemporaryDirectory() as tmp,
                tempfile.TemporaryDirectory() as pins,
            ):
                state_path = pathlib.Path(tmp, "state.json")
                state_path.write_text(json.dumps(original) + "\n")
                pin = pathlib.Path(pins, "model-pin.json")
                arguments = []
                if case == "missing":
                    arguments = ["--model-pin-file", str(pin)]
                elif case == "malformed":
                    pin.write_text("not-json")
                    arguments = ["--model-pin-file", str(pin)]
                elif case == "empty":
                    pin.write_text("{}\n")
                    arguments = ["--model-pin-file", str(pin)]

                result = self.run_init(tmp, *arguments, check=False)

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                state = json.loads(state_path.read_text())
                self.assertEqual(state["legacy"], True)
                self.assertEqual(state["model_lock"], {"mode": "exact"})
                self.assertIn("agent_journal", state)

    def test_init_json_encodes_pinned_model(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as pins:
            model = "gpt-5.5-cyber-preview"
            pin = self.write_model_pin(pins, model=model)
            self.run_init(tmp, "--model-pin-file", str(pin))

            paths = {
                "state": pathlib.Path(tmp, "state.json"),
                "manifest": pathlib.Path(tmp, "output", "session-manifest.json"),
            }
            documents = {}
            parse_errors = {}
            for name, path in paths.items():
                try:
                    documents[name] = json.loads(path.read_text())
                except json.JSONDecodeError as exc:
                    parse_errors[name] = str(exc)

            self.assertEqual(parse_errors, {})
            self.assertEqual(documents["state"]["model"], model)
            self.assertEqual(documents["manifest"]["model"], model)

    def test_init_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as pins:
            pin = self.write_model_pin(pins)
            self.run_init(tmp, "--model-pin-file", str(pin))
            state_path = pathlib.Path(tmp, "state.json")
            manifest_path = pathlib.Path(tmp, "output", "session-manifest.json")
            original_state = json.loads(state_path.read_text())
            original_manifest = manifest_path.read_bytes()
            original_commits = subprocess.check_output(
                ["git", "-C", tmp, "rev-list", "--count", "HEAD"],
                env=self.isolated_env(),
                text=True,
            )

            self.run_init(tmp, "--model-pin-file", str(pin))

            resumed_state = json.loads(state_path.read_text())
            original_journal = original_state.pop("agent_journal")
            resumed_journal = resumed_state.pop("agent_journal")
            self.assertEqual(resumed_state, original_state)
            self.assertEqual(resumed_journal, original_journal)
            self.assertEqual(manifest_path.read_bytes(), original_manifest)
            self.assertEqual(
                subprocess.check_output(
                    ["git", "-C", tmp, "rev-list", "--count", "HEAD"],
                    env=self.isolated_env(),
                    text=True,
                ),
                original_commits,
            )
            self.assertEqual(
                pathlib.Path(tmp, ".nightfalcon-review").read_text(),
                "nightfalcon\n",
            )

    def test_init_with_different_selected_model_preserves_existing_phase_state(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as pins:
            original = self.write_model_pin(pins, model="gpt-5.5-cyber-preview")
            self.run_init(
                tmp, "--model-pin-file", str(original), "--slug", "demo"
            )
            state_path = pathlib.Path(tmp, "state.json")
            state = json.loads(state_path.read_text())
            state["current_phase"] = "phase-4"
            state["phase_status"] = {"phase-3": "complete"}
            state_path.write_text(json.dumps(state, indent=2) + "\n")
            before = state_path.read_bytes()
            changed = self.write_model_pin(
                pins, model="gpt-5.5", session_id="session-changed"
            )
            result = self.run_init(tmp, "--model-pin-file", str(changed))
            after = state_path.read_bytes()
            preserved = json.loads(after)
        self.assertEqual(result.returncode, 0, result.stderr)
        before_state = json.loads(before)
        after_state = json.loads(after)
        before_journal = before_state.pop("agent_journal")
        after_journal = after_state.pop("agent_journal")
        self.assertEqual(after_state, before_state)
        self.assertNotEqual(after_journal["epoch_id"], before_journal["epoch_id"])
        self.assertEqual(preserved["model"], "gpt-5.5-cyber-preview")
        self.assertEqual(preserved["current_phase"], "phase-4")
        self.assertEqual(preserved["phase_status"], {"phase-3": "complete"})

    def test_ported_runtime_has_no_claude_environment_dependency(self):
        text = "\n".join(
            path.read_text(errors="ignore")
            for path in (ROOT / "scripts").rglob("*")
            if path.is_file()
        )
        self.assertNotIn("CLAUDE_PLUGIN_ROOT", text)
        self.assertNotIn("CLAUDE_MODEL", text)

    def test_context_catalog_sync_excludes_codex_runtime_state(self):
        text = (ROOT / "scripts" / "sync-organization-context.sh").read_text()
        self.assertIn("--exclude='.codex'", text)
        self.assertIn("--exclude='.codex/**'", text)
        self.assertIn("--exclude='.claude'", text)
        self.assertIn("--exclude='.cursor'", text)
        self.assertNotIn("vaults/", text)
        self.assertIn("refusing to import private context into package", text)


if __name__ == "__main__":
    unittest.main()
