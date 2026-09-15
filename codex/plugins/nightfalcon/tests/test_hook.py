import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "hooks" / "nightfalcon_hook.py"
INIT_REVIEW = ROOT / "scripts" / "init-review.sh"
SPEC = importlib.util.spec_from_file_location("nightfalcon_hook", HOOK_PATH)
HOOK = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HOOK
SPEC.loader.exec_module(HOOK)


class HookTests(unittest.TestCase):
    def mark_workspace(self, workspace):
        pathlib.Path(workspace, ".nightfalcon-review").write_text("nightfalcon\n")

    def write_state(self, workspace, **overrides):
        state = {
            "current_phase": "phase-8",
            "date": "2026-07-14",
            "repo_slugs": [],
            "model": "gpt-selected",
        }
        state.update(overrides)
        pathlib.Path(workspace, "state.json").write_text(json.dumps(state))

    def write_legacy_state(self, workspace, model="gpt-old"):
        self.write_state(
            workspace,
            model=model,
            model_lock={
                "mode": "exact",
                "session_id": "session-test",
                "source": "SessionStart",
            },
        )

    def initialize_journal(self, workspace):
        output = pathlib.Path(workspace, "output")
        output.mkdir(exist_ok=True)
        (output / "session-manifest.json").write_text(json.dumps({
            "date": "2026-07-14",
            "phases_completed": [],
        }))
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "agent-journal.py"),
                "init",
                "--workspace",
                str(workspace),
                "--date",
                "2026-07-14",
                "--port",
                "codex",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def hook_payload(self, event, workspace, model="gpt-selected", **extra):
        return {
            "hook_event_name": event,
            "session_id": "session-test",
            "turn_id": "turn-test",
            "cwd": str(workspace),
            "model": model,
            **extra,
        }

    def assert_model_not_enforced(self, result):
        combined = f"{result.stdout}\n{result.stderr}"
        self.assertNotIn('"continue": false', combined)
        self.assertNotIn("NIGHTFALCON_MODEL_MISMATCH", combined)
        self.assertFalse(
            result.exit_code == 2 and "model" in result.stderr.lower(),
            combined,
        )

    def assert_blind_guard_accepts(self, prompt_name, allowed_fields, hook_result):
        prompt = pathlib.Path(ROOT, "phases", prompt_name).read_text()
        self.assertIn("BLIND-ISOLATION GUARD", prompt)
        self.assertIn("BLIND_VIOLATION", prompt)
        self.assertIn("No model metadata belongs in this blind packet", prompt)
        unexpected_fields = set(allowed_fields) - {
            "prompt",
            "code_excerpt",
            "claim",
            "primary_statement",
            "finding_id",
            "finding_type",
            "round",
            "script",
            "script_type",
            "placeholders",
            "reviewer",
        }
        would_emit_blind_violation = bool(hook_result.stdout or unexpected_fields)
        self.assertFalse(would_emit_blind_violation)

    def test_inert_without_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = HOOK.handle({"hook_event_name": "Stop", "cwd": tmp})
        self.assertEqual(result.exit_code, 0)

    def test_nested_cwd_keeps_stop_pretool_and_subagent_lifecycle_active(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as data:
            workspace = pathlib.Path(tmp)
            plugin_data = pathlib.Path(data)
            self.mark_workspace(workspace)
            self.write_state(workspace)
            self.initialize_journal(workspace)
            nested = workspace / "sourcecode" / "demo" / "nested"
            nested.mkdir(parents=True)
            HOOK.initialize_agent_registry(workspace, "session-test", plugin_data)

            stop = HOOK.handle(
                {"hook_event_name": "Stop", "cwd": str(nested)},
                plugin_root=ROOT,
                plugin_data=plugin_data,
            )
            pretool = HOOK.handle(
                {
                    "hook_event_name": "PreToolUse",
                    "cwd": str(nested),
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(workspace / "state.json")},
                },
                plugin_root=ROOT,
                plugin_data=plugin_data,
            )
            started = HOOK.handle(
                self.hook_payload(
                    "SubagentStart", nested, agent_id="nested-child", agent_type="worker"
                ),
                plugin_root=ROOT,
                plugin_data=plugin_data,
            )
            active_after_start, start_error = HOOK.active_agent_ids(
                workspace, "session-test", plugin_data
            )
            stopped = HOOK.handle(
                self.hook_payload(
                    "SubagentStop", nested, agent_id="nested-child", agent_type="worker"
                ),
                plugin_root=ROOT,
                plugin_data=plugin_data,
            )
            active_after_stop, stop_error = HOOK.active_agent_ids(
                workspace, "session-test", plugin_data
            )

        self.assertEqual(stop.exit_code, 2, stop.stderr)
        self.assertEqual(pretool.exit_code, 2, pretool.stderr)
        self.assertEqual(started.exit_code, 0, started.stderr)
        self.assertEqual((active_after_start, start_error), ({"nested-child"}, None))
        self.assertEqual(stopped.exit_code, 0, stopped.stderr)
        self.assertEqual((active_after_stop, stop_error), (set(), None))

    def test_first_subagent_start_after_unmarked_session_start_and_init_registers(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as data:
            workspace = pathlib.Path(tmp)
            plugin_data = pathlib.Path(data)
            session = HOOK.handle(
                self.hook_payload(
                    "SessionStart",
                    workspace,
                    source="startup",
                    model="gpt-user-selection",
                ),
                plugin_data=plugin_data,
            )
            pin = plugin_data / "model-pins" / "session-test.json"
            environment = {
                key: value for key, value in os.environ.items() if not key.startswith("GIT_")
            }
            environment.update(
                {
                    "PLUGIN_ROOT": str(ROOT),
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_SYSTEM": os.devnull,
                }
            )
            initialized = subprocess.run(
                [
                    "bash",
                    str(INIT_REVIEW),
                    "--workspace",
                    str(workspace),
                    "--date",
                    "2026-07-17",
                    "--model-pin-file",
                    str(pin),
                ],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            started = HOOK.handle(
                self.hook_payload(
                    "SubagentStart",
                    workspace,
                    model="gpt-user-selection",
                    agent_id="child-first",
                    agent_type="worker",
                ),
                plugin_data=plugin_data,
            )
            active, error = HOOK.active_agent_ids(
                workspace, "session-test", plugin_data
            )

        self.assertEqual(session.exit_code, 0)
        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
        self.assertEqual(started.exit_code, 0)
        self.assertEqual((active, error), ({"child-first"}, None))

    def test_successful_subagent_start_adds_no_context_to_either_blind_packet(self):
        blind_packets = {
            "phase-da.md": {
                "prompt",
                "code_excerpt",
                "claim",
                "primary_statement",
                "finding_id",
                "finding_type",
                "round",
            },
            "phase-poc-reviewer.md": {
                "prompt",
                "code_excerpt",
                "claim",
                "script",
                "script_type",
                "placeholders",
                "finding_id",
                "reviewer",
            },
        }
        for prompt_name, packet in blind_packets.items():
            with (
                self.subTest(prompt=prompt_name),
                tempfile.TemporaryDirectory() as tmp,
                tempfile.TemporaryDirectory() as data,
            ):
                workspace = pathlib.Path(tmp)
                plugin_data = pathlib.Path(data)
                self.mark_workspace(workspace)
                HOOK.initialize_agent_registry(workspace, "session-test", plugin_data)
                result = HOOK.handle(
                    self.hook_payload(
                        "SubagentStart",
                        workspace,
                        model="gpt-runtime-only",
                        agent_id=f"child-{prompt_name}",
                        agent_type="worker",
                        unrelated_context="must-not-propagate",
                    ),
                    plugin_data=plugin_data,
                )

                self.assertEqual(result.exit_code, 0)
                self.assertNotIn("NIGHTFALCON_MODEL_SELECTED", result.stdout)
                self.assertNotIn("must-not-propagate", result.stdout)
                self.assert_blind_guard_accepts(prompt_name, packet, result)

    def test_session_start_writes_selected_model_pin(self):
        with tempfile.TemporaryDirectory() as plugin_data:
            result = HOOK.handle(
                {
                    "hook_event_name": "SessionStart",
                    "session_id": "session-test",
                    "source": "startup",
                    "cwd": "/tmp",
                    "model": "gpt-5.5-cyber-preview",
                },
                plugin_data=pathlib.Path(plugin_data),
            )
            pin = pathlib.Path(plugin_data, "model-pins", "session-test.json")
            self.assertTrue(pin.exists())
            document = json.loads(pin.read_text())

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(document["session_id"], "session-test")
        self.assertEqual(document["model"], "gpt-5.5-cyber-preview")
        self.assertIn(
            "NIGHTFALCON_MODEL_SELECTED model=gpt-5.5-cyber-preview",
            result.stdout,
        )
        self.assertIn(str(pin), result.stdout)
        self.assertIn("max_depth = 5", result.stdout)
        self.assertIn("max_threads = 8", result.stdout)

    def test_session_start_without_model_reports_selection_unavailable(self):
        with tempfile.TemporaryDirectory() as plugin_data:
            result = HOOK.handle(
                {
                    "hook_event_name": "SessionStart",
                    "session_id": "session-test",
                    "source": "startup",
                    "cwd": "/tmp",
                },
                plugin_data=pathlib.Path(plugin_data),
            )
        self.assertEqual(result.exit_code, 0)
        self.assertIn("NIGHTFALCON_MODEL_SELECTED model=unavailable", result.stdout)
        self.assertNotIn('"continue": false', result.stdout)

    def test_session_start_rejects_traversal_session_id_without_creating_files(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as data:
            self.mark_workspace(tmp)
            self.write_state(tmp, model="gpt-selected")
            result = HOOK.handle(
                self.hook_payload(
                    "SessionStart",
                    tmp,
                    session_id="../escape",
                    source="startup",
                ),
                plugin_data=pathlib.Path(data),
            )
            files = [path for path in pathlib.Path(data).rglob("*") if path.is_file()]
        self.assertIn("NIGHTFALCON_MODEL_SELECTED model=gpt-selected", result.stdout)
        self.assertIn("pin_file=unavailable", result.stdout)
        self.assertEqual(files, [])

    def test_resume_session_start_preserves_valid_or_corrupt_existing_registry(self):
        cases = (
            ("valid", json.dumps({"schema_version": 1, "active_agent_ids": ["child-old"]}) + "\n", 0),
            ("corrupt", "not-json\n", 2),
        )
        for label, content, expected_exit in cases:
            with (
                self.subTest(case=label),
                tempfile.TemporaryDirectory() as tmp,
                tempfile.TemporaryDirectory() as data,
            ):
                workspace = pathlib.Path(tmp)
                plugin_data = pathlib.Path(data)
                self.mark_workspace(workspace)
                self.write_state(workspace)
                registry, _ = HOOK._registry_paths(
                    workspace, "session-test", plugin_data
                )
                registry.parent.mkdir(parents=True)
                registry.write_text(content)
                before = registry.read_bytes()

                result = HOOK.handle(
                    self.hook_payload(
                        "SessionStart",
                        workspace,
                        source="resume",
                        model="gpt-user-selected",
                    ),
                    plugin_data=plugin_data,
                )

                self.assertEqual(result.exit_code, expected_exit)
                self.assertEqual(registry.read_bytes(), before)
                if label == "corrupt":
                    self.assertIn("active-agent registry", result.stderr)

    def test_recorded_model_accepts_changed_model_across_hook_events(self):
        events = ("SessionStart", "UserPromptSubmit", "SubagentStart", "PreToolUse", "Stop")
        for event in events:
            with (
                self.subTest(event=event),
                tempfile.TemporaryDirectory() as tmp,
                tempfile.TemporaryDirectory() as data,
            ):
                workspace = pathlib.Path(tmp)
                plugin_data = pathlib.Path(data)
                self.mark_workspace(workspace)
                self.write_state(
                    workspace,
                    model="gpt-5.5",
                    model_lock={
                        "mode": "exact",
                        "session_id": "session-old",
                        "source": "SessionStart",
                    },
                )
                payload = self.hook_payload(
                    event,
                    workspace,
                    model="gpt-5.5-cyber-preview",
                )
                if event == "SessionStart":
                    payload["source"] = "startup"
                elif event == "SubagentStart":
                    HOOK.initialize_agent_registry(workspace, "session-test", plugin_data)
                    payload.update(agent_id="child-1", agent_type="worker")
                elif event == "PreToolUse":
                    payload.update(tool_name="Bash", tool_input={"command": "true"})
                elif event == "Stop":
                    output = workspace / "output"
                    output.mkdir()
                    (output / "executive-summary-2026-07-14.md").write_text("summary\n")
                    (output / "executive-report-2026-07-14.html").write_text("<html></html>\n")
                    self.initialize_journal(workspace)

                result = HOOK.handle(
                    payload,
                    plugin_root=ROOT,
                    plugin_data=plugin_data,
                )

            self.assertEqual(result.exit_code, 0, result.stderr)
            self.assert_model_not_enforced(result)

    def test_parent_model_change_updates_external_audit_without_touching_phase_state(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as data:
            workspace = pathlib.Path(tmp)
            plugin_data = pathlib.Path(data)
            self.mark_workspace(workspace)
            prior_history = {
                "model": "gpt-5.5",
                "session_id": "session-old",
                "source": "SessionStart",
                "selected_at": "2026-07-17T00:00:00Z",
            }
            self.write_state(
                workspace,
                model="gpt-5.5",
                model_policy="locked",
                model_epoch=1,
                model_lock={
                    "mode": "exact",
                    "session_id": "session-old",
                    "source": "SessionStart",
                    "epoch": 1,
                },
                model_history=[prior_history],
                current_phase="phase-6",
                phase_status={"phase-5": "complete", "phase-6": "in_progress"},
                history=[{"phase": "phase-5", "completed_at": "2026-07-17T01:00:00Z"}],
            )
            state_path = workspace / "state.json"
            state_before = state_path.read_bytes()
            session = HOOK.handle(
                self.hook_payload(
                    "SessionStart",
                    workspace,
                    model="gpt-5.5",
                    source="startup",
                ),
                plugin_data=plugin_data,
            )
            started = HOOK.handle(
                self.hook_payload(
                    "SubagentStart",
                    workspace,
                    model="gpt-5.5",
                    agent_id="child-1",
                    agent_type="worker",
                ),
                plugin_data=plugin_data,
            )
            changed = HOOK.handle(
                self.hook_payload(
                    "UserPromptSubmit",
                    workspace,
                    model="gpt-5.5-cyber-preview",
                    prompt="continue review",
                ),
                plugin_data=plugin_data,
            )
            active, error = HOOK.active_agent_ids(
                workspace, "session-test", plugin_data
            )
            state_after = state_path.read_bytes()
            self.assertEqual(state_after, state_before)
            audit_path = HOOK._model_audit_path(
                workspace, "session-test", plugin_data
            )
            audit = json.loads(audit_path.read_text())

        self.assert_model_not_enforced(session)
        self.assert_model_not_enforced(started)
        self.assert_model_not_enforced(changed)
        self.assertEqual((active, error), ({"child-1"}, None))
        self.assertEqual(audit["schema_version"], 1)
        self.assertEqual(audit["model_policy"], "user-selected")
        self.assertEqual(audit["current_model"], "gpt-5.5-cyber-preview")
        self.assertEqual(
            [entry["model"] for entry in audit["history"]],
            ["gpt-5.5", "gpt-5.5-cyber-preview"],
        )
        self.assertEqual(audit["history"][-1]["previous_model"], "gpt-5.5")
        self.assertEqual(audit["history"][-1]["source"], "UserPromptSubmit")

    def test_passive_audit_rejects_boolean_schema_version_without_rewriting(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as data:
            workspace = pathlib.Path(tmp)
            plugin_data = pathlib.Path(data)
            self.mark_workspace(workspace)
            audit_path = HOOK._model_audit_path(
                workspace, "session-test", plugin_data
            )
            audit_path.parent.mkdir(parents=True)
            audit_path.write_text(
                json.dumps(
                    {
                        "schema_version": True,
                        "model_policy": "user-selected",
                        "current_model": "gpt-old",
                        "history": [],
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
            before = audit_path.read_bytes()

            result = HOOK.handle(
                self.hook_payload(
                    "UserPromptSubmit",
                    workspace,
                    model="gpt-new",
                    prompt="continue review",
                ),
                plugin_data=plugin_data,
            )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.stdout, "")
            self.assertEqual(audit_path.read_bytes(), before)

    def test_child_with_third_model_is_registered_without_child_context(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as data:
            workspace = pathlib.Path(tmp)
            plugin_data = pathlib.Path(data)
            self.mark_workspace(workspace)
            self.write_state(workspace, model="gpt-5.5")
            HOOK.initialize_agent_registry(workspace, "session-test", plugin_data)
            HOOK.handle(
                self.hook_payload(
                    "SubagentStart",
                    workspace,
                    model="gpt-5.5",
                    agent_id="child-1",
                    agent_type="worker",
                ),
                plugin_data=plugin_data,
            )
            HOOK.handle(
                self.hook_payload(
                    "UserPromptSubmit",
                    workspace,
                    model="gpt-5.5-cyber-preview",
                    prompt="continue review",
                ),
                plugin_data=plugin_data,
            )
            result = HOOK.handle(
                self.hook_payload(
                    "SubagentStart",
                    workspace,
                    model="gpt-6-third-model",
                    agent_id="child-2",
                    agent_type="worker",
                ),
                plugin_data=plugin_data,
            )
            active, error = HOOK.active_agent_ids(
                workspace, "session-test", plugin_data
            )

        self.assert_model_not_enforced(result)
        self.assertEqual((active, error), ({"child-1", "child-2"}, None))
        self.assertEqual(result.stdout, "")

    def test_subagent_start_rejects_missing_registration_metadata(self):
        cases = (
            ("missing-agent-id", {"session_id": "session-test"}, True),
            ("unsafe-session-id", {"session_id": "../escape", "agent_id": "child-1"}, True),
            ("missing-plugin-data", {"session_id": "session-test", "agent_id": "child-1"}, False),
        )
        for label, metadata, configure_plugin_data in cases:
            with (
                self.subTest(case=label),
                tempfile.TemporaryDirectory() as tmp,
                tempfile.TemporaryDirectory() as data,
                mock.patch.dict(os.environ, {"PLUGIN_DATA": ""}),
            ):
                self.mark_workspace(tmp)
                self.write_state(tmp, model="gpt-selected")
                plugin_data = pathlib.Path(data) if configure_plugin_data else None
                if configure_plugin_data and metadata["session_id"] == "session-test":
                    HOOK.initialize_agent_registry(
                        pathlib.Path(tmp), "session-test", plugin_data
                    )
                result = HOOK.handle(
                    self.hook_payload(
                        "SubagentStart",
                        tmp,
                        agent_type="worker",
                        **metadata,
                    ),
                    plugin_data=plugin_data,
                )
                output = json.loads(result.stdout)

            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("NIGHTFALCON_MODEL_SELECTED", context)
            self.assertIn("NIGHTFALCON_CHILD_REGISTRATION_FAILED", context)
            self.assertIn("systemMessage", output)

    def test_subagent_start_rejects_corrupt_registry(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as data:
            workspace = pathlib.Path(tmp)
            plugin_data = pathlib.Path(data)
            self.mark_workspace(workspace)
            self.write_state(workspace, model="gpt-selected")
            HOOK.initialize_agent_registry(workspace, "session-test", plugin_data)
            registry, _ = HOOK._registry_paths(workspace, "session-test", plugin_data)
            registry.write_text("not-json")

            result = HOOK.handle(
                self.hook_payload(
                    "SubagentStart",
                    workspace,
                    agent_id="child-1",
                    agent_type="worker",
                ),
                plugin_data=plugin_data,
            )

        output = json.loads(result.stdout)
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("NIGHTFALCON_MODEL_SELECTED", context)
        self.assertIn("NIGHTFALCON_CHILD_REGISTRATION_FAILED", context)
        self.assertIn("systemMessage", output)

    def test_subagent_start_rejects_boolean_registry_schema_version(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as data:
            workspace = pathlib.Path(tmp)
            plugin_data = pathlib.Path(data)
            self.mark_workspace(workspace)
            self.write_state(workspace, model="gpt-selected")
            HOOK.initialize_agent_registry(workspace, "session-test", plugin_data)
            registry, _ = HOOK._registry_paths(workspace, "session-test", plugin_data)
            registry.write_text(
                json.dumps({"schema_version": True, "active_agent_ids": []})
            )

            result = HOOK.handle(
                self.hook_payload(
                    "SubagentStart",
                    workspace,
                    agent_id="child-1",
                    agent_type="worker",
                ),
                plugin_data=plugin_data,
            )

        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("NIGHTFALCON_MODEL_SELECTED", context)
        self.assertIn("NIGHTFALCON_CHILD_REGISTRATION_FAILED", context)

    def test_subagent_start_rejects_registry_write_failure(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as data:
            workspace = pathlib.Path(tmp)
            plugin_data = pathlib.Path(data)
            self.mark_workspace(workspace)
            self.write_state(workspace, model="gpt-selected")
            HOOK.initialize_agent_registry(workspace, "session-test", plugin_data)

            with mock.patch.object(
                HOOK, "_atomic_write_json", side_effect=OSError("registry is read-only")
            ):
                result = HOOK.handle(
                    self.hook_payload(
                        "SubagentStart",
                        workspace,
                        agent_id="child-1",
                        agent_type="worker",
                    ),
                    plugin_data=plugin_data,
                )

        output = json.loads(result.stdout)
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("NIGHTFALCON_MODEL_SELECTED", context)
        self.assertIn("NIGHTFALCON_CHILD_REGISTRATION_FAILED", context)
        self.assertIn("registry is read-only", context)
        self.assertIn("systemMessage", output)

    def test_subagent_stop_reports_cleanup_failure(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as data:
            workspace = pathlib.Path(tmp)
            plugin_data = pathlib.Path(data)
            self.mark_workspace(workspace)
            self.write_legacy_state(workspace)
            HOOK.initialize_agent_registry(workspace, "session-test", plugin_data)
            start = HOOK.handle(
                self.hook_payload(
                    "SubagentStart",
                    workspace,
                    model="gpt-old",
                    agent_id="child-1",
                    agent_type="worker",
                ),
                plugin_data=plugin_data,
            )
            self.assertEqual(start.stdout, "")

            with mock.patch.object(
                HOOK, "_atomic_write_json", side_effect=OSError("cleanup is read-only")
            ):
                stop = HOOK.handle(
                    self.hook_payload(
                        "SubagentStop",
                        workspace,
                        model="gpt-old",
                        agent_id="child-1",
                        agent_type="worker",
                    ),
                    plugin_data=plugin_data,
                )

        stop_output = json.loads(stop.stdout)
        stop_context = stop_output["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("NIGHTFALCON_ACTIVE_AGENT_UNREGISTERED", stop_context)
        self.assertIn("NIGHTFALCON_ACTIVE_AGENT_UNREGISTER_FAILED", stop_context)
        self.assertIn("systemMessage", stop_output)

    def test_subagent_stop_rejects_missing_metadata(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as data:
            workspace = pathlib.Path(tmp)
            plugin_data = pathlib.Path(data)
            self.mark_workspace(workspace)
            self.write_legacy_state(workspace)
            HOOK.initialize_agent_registry(workspace, "session-test", plugin_data)
            HOOK.handle(
                self.hook_payload(
                    "SubagentStart",
                    workspace,
                    model="gpt-old",
                    agent_id="child-1",
                    agent_type="worker",
                ),
                plugin_data=plugin_data,
            )

            stop = HOOK.handle(
                self.hook_payload(
                    "SubagentStop",
                    workspace,
                    model="gpt-old",
                    agent_id="",
                    agent_type="worker",
                ),
                plugin_data=plugin_data,
            )
            active, error = HOOK.active_agent_ids(
                workspace, "session-test", plugin_data
            )

        stop_output = json.loads(stop.stdout)
        stop_context = stop_output["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("NIGHTFALCON_ACTIVE_AGENT_UNREGISTERED", stop_context)
        self.assertIn("NIGHTFALCON_ACTIVE_AGENT_UNREGISTER_FAILED", stop_context)
        self.assertIn("systemMessage", stop_output)
        self.assertEqual((active, error), ({"child-1"}, None))

    def test_subagent_stop_rejects_unknown_id_without_registry_write(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as data:
            workspace = pathlib.Path(tmp)
            plugin_data = pathlib.Path(data)
            self.mark_workspace(workspace)
            self.write_state(workspace, model="gpt-selected")
            HOOK.initialize_agent_registry(workspace, "session-test", plugin_data)
            HOOK.handle(
                self.hook_payload(
                    "SubagentStart",
                    workspace,
                    agent_id="child-keep",
                    agent_type="worker",
                ),
                plugin_data=plugin_data,
            )
            registry, _ = HOOK._registry_paths(workspace, "session-test", plugin_data)
            before = registry.read_text()

            with mock.patch.object(
                HOOK, "_atomic_write_json", wraps=HOOK._atomic_write_json
            ) as atomic_write:
                stop = HOOK.handle(
                    self.hook_payload(
                        "SubagentStop",
                        workspace,
                        agent_id="child-unknown",
                        agent_type="worker",
                    ),
                    plugin_data=plugin_data,
                )

            after = registry.read_text()
            active, error = HOOK.active_agent_ids(
                workspace, "session-test", plugin_data
            )

        context = json.loads(stop.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("NIGHTFALCON_ACTIVE_AGENT_UNREGISTER_FAILED", context)
        self.assertNotIn("NIGHTFALCON_ACTIVE_AGENT_UNREGISTERED", context)
        atomic_write.assert_not_called()
        self.assertEqual(after, before)
        self.assertEqual((active, error), ({"child-keep"}, None))

    def test_subagent_stop_rejects_duplicate_and_preserves_other_child(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as data:
            workspace = pathlib.Path(tmp)
            plugin_data = pathlib.Path(data)
            self.mark_workspace(workspace)
            self.write_state(workspace, model="gpt-selected")
            HOOK.initialize_agent_registry(workspace, "session-test", plugin_data)
            for agent_id in ("child-remove", "child-keep"):
                HOOK.handle(
                    self.hook_payload(
                        "SubagentStart",
                        workspace,
                        agent_id=agent_id,
                        agent_type="worker",
                    ),
                    plugin_data=plugin_data,
                )
            first_stop = HOOK.handle(
                self.hook_payload(
                    "SubagentStop",
                    workspace,
                    agent_id="child-remove",
                    agent_type="worker",
                ),
                plugin_data=plugin_data,
            )
            registry, _ = HOOK._registry_paths(workspace, "session-test", plugin_data)
            before_duplicate = registry.read_text()

            with mock.patch.object(
                HOOK, "_atomic_write_json", wraps=HOOK._atomic_write_json
            ) as atomic_write:
                duplicate = HOOK.handle(
                    self.hook_payload(
                        "SubagentStop",
                        workspace,
                        agent_id="child-remove",
                        agent_type="worker",
                    ),
                    plugin_data=plugin_data,
                )

            after_duplicate = registry.read_text()
            active, error = HOOK.active_agent_ids(
                workspace, "session-test", plugin_data
            )

        self.assertIn("NIGHTFALCON_ACTIVE_AGENT_UNREGISTERED", first_stop.stdout)
        context = json.loads(duplicate.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("NIGHTFALCON_ACTIVE_AGENT_UNREGISTER_FAILED", context)
        self.assertNotIn("NIGHTFALCON_ACTIVE_AGENT_UNREGISTERED", context)
        atomic_write.assert_not_called()
        self.assertEqual(after_duplicate, before_duplicate)
        self.assertEqual((active, error), ({"child-keep"}, None))

    def test_different_model_child_is_registered_and_can_use_non_file_tool(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as data:
            self.mark_workspace(tmp)
            self.write_state(tmp, model="gpt-old")
            HOOK.initialize_agent_registry(pathlib.Path(tmp), "session-test", pathlib.Path(data))
            start = HOOK.handle(
                self.hook_payload(
                    "SubagentStart",
                    tmp,
                    model="gpt-new",
                    agent_id="child-1",
                    agent_type="worker",
                ),
                plugin_data=pathlib.Path(data),
            )
            active, error = HOOK.active_agent_ids(
                pathlib.Path(tmp), "session-test", pathlib.Path(data)
            )
            tool = HOOK.handle(
                self.hook_payload(
                    "PreToolUse",
                    tmp,
                    model="gpt-new",
                    tool_name="Bash",
                    tool_input={"command": "true"},
                ),
                plugin_root=ROOT,
            )
        self.assertEqual(start.stdout, "")
        self.assertEqual((active, error), ({"child-1"}, None))
        self.assertEqual(tool.exit_code, 0)

    def test_parallel_agent_start_stop_preserves_registry_membership(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as data:
            workspace = pathlib.Path(tmp)
            plugin_data = pathlib.Path(data)
            self.mark_workspace(workspace)
            self.write_state(workspace, model="gpt-selected")
            HOOK.initialize_agent_registry(workspace, "session-test", plugin_data)
            agent_ids = [f"child-{index:02d}" for index in range(24)]

            def run_parallel(event, ids):
                barrier = threading.Barrier(len(ids))
                results = {}

                def invoke(agent_id):
                    barrier.wait()
                    results[agent_id] = HOOK.handle(
                        self.hook_payload(
                            event,
                            workspace,
                            agent_id=agent_id,
                            agent_type="worker",
                        ),
                        plugin_data=plugin_data,
                    )

                threads = [threading.Thread(target=invoke, args=(agent_id,)) for agent_id in ids]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(5)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                return results

            starts = run_parallel("SubagentStart", agent_ids)
            active_after_start, start_error = HOOK.active_agent_ids(
                workspace, "session-test", plugin_data
            )
            removed = agent_ids[::2]
            retained = set(agent_ids[1::2])
            stops = run_parallel("SubagentStop", removed)
            active_after_partial_stop, partial_error = HOOK.active_agent_ids(
                workspace, "session-test", plugin_data
            )
            final_stops = run_parallel("SubagentStop", sorted(retained))
            active_after_final_stop, final_error = HOOK.active_agent_ids(
                workspace, "session-test", plugin_data
            )

        for result in starts.values():
            self.assertEqual(result.stdout, "")
        for result in (*stops.values(), *final_stops.values()):
            self.assertIn("NIGHTFALCON_ACTIVE_AGENT_UNREGISTERED", result.stdout)
        self.assertEqual((active_after_start, start_error), (set(agent_ids), None))
        self.assertEqual((active_after_partial_stop, partial_error), (retained, None))
        self.assertEqual((active_after_final_stop, final_error), (set(), None))

    def test_subagent_start_keeps_inherited_model_out_of_child_context(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as data:
            self.mark_workspace(tmp)
            self.write_state(tmp, model="gpt-5.5-cyber-preview")
            HOOK.initialize_agent_registry(
                pathlib.Path(tmp), "session-test", pathlib.Path(data)
            )
            result = HOOK.handle(
                {
                    "hook_event_name": "SubagentStart",
                    "cwd": tmp,
                    "model": "gpt-5.5-cyber-preview",
                    "session_id": "session-test",
                    "agent_id": "child-test",
                    "agent_type": "worker",
                },
                plugin_data=pathlib.Path(data),
            )
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "")

    def test_pretool_allows_changed_model_for_non_file_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.mark_workspace(tmp)
            self.write_state(tmp)
            self.write_state(tmp, model="gpt-5.5-cyber-preview")
            result = HOOK.handle(
                {
                    "hook_event_name": "PreToolUse",
                    "cwd": tmp,
                    "model": "gpt-5.5",
                    "tool_name": "Bash",
                    "tool_input": {"command": "true"},
                },
                plugin_root=ROOT,
            )
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assert_model_not_enforced(result)

    def test_pretool_missing_model_allows_non_file_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.mark_workspace(tmp)
            self.write_state(tmp, model="gpt-selected")
            result = HOOK.handle(
                {
                    "hook_event_name": "PreToolUse",
                    "cwd": tmp,
                    "tool_name": "web_search",
                    "tool_input": {"query": "safe"},
                },
                plugin_root=ROOT,
            )
        self.assertEqual(result.exit_code, 0, result.stderr)

    def test_pretool_allows_non_file_tools_with_selected_model(self):
        for tool_input in ({"query": "safe"}, None):
            with self.subTest(tool_input=tool_input), tempfile.TemporaryDirectory() as tmp:
                self.mark_workspace(tmp)
                self.write_state(tmp, model="gpt-selected")
                payload = {
                    "hook_event_name": "PreToolUse",
                    "cwd": tmp,
                    "model": "gpt-selected",
                    "tool_name": "web_search",
                }
                if tool_input is not None:
                    payload["tool_input"] = tool_input
                result = HOOK.handle(payload, plugin_root=ROOT)
            self.assertEqual(result.exit_code, 0, result.stderr)

    def test_pretool_preserves_missing_tool_name_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.mark_workspace(tmp)
            self.write_state(tmp, model="gpt-selected")
            result = HOOK.handle(
                {
                    "hook_event_name": "PreToolUse",
                    "cwd": tmp,
                    "model": "gpt-selected",
                    "tool_input": {},
                },
                plugin_root=ROOT,
            )
        self.assertEqual(result.exit_code, 2)
        self.assertIn("tool_name", result.stderr)

    def test_pretool_file_tool_preserves_object_input_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.mark_workspace(tmp)
            self.write_state(tmp, model="gpt-selected")
            result = HOOK.handle(
                {
                    "hook_event_name": "PreToolUse",
                    "cwd": tmp,
                    "model": "gpt-selected",
                    "tool_name": "Write",
                    "tool_input": "output/report.md",
                },
                plugin_root=ROOT,
            )
        self.assertEqual(result.exit_code, 2)
        self.assertIn("tool_input", result.stderr)

    def test_extracts_paths_from_apply_patch(self):
        payload = {
            "patch": "*** Begin Patch\n*** Update File: findings/a.md\n@@\n-x\n+y\n*** Add File: output/b.md\n+z\n*** End Patch"
        }
        self.assertEqual(HOOK.extract_paths("apply_patch", payload), ["findings/a.md", "output/b.md"])

    def test_extracts_paths_from_canonical_apply_patch_command(self):
        payload = {
            "command": "*** Begin Patch\n*** Update File: findings/a.md\n@@\n-x\n+y\n*** Add File: output/b.md\n+z\n*** End Patch"
        }
        self.assertEqual(HOOK.extract_paths("apply_patch", payload), ["findings/a.md", "output/b.md"])

    def test_canonical_apply_patch_command_takes_precedence(self):
        payload = {
            "command": "*** Begin Patch\n*** Add File: output/canonical.md\n+x\n*** End Patch",
            "patch": "*** Begin Patch\n*** Add File: findings/legacy.md\n+x\n*** End Patch",
        }
        self.assertEqual(HOOK.extract_paths("apply_patch", payload), ["output/canonical.md"])

    def test_extracts_direct_write_path(self):
        self.assertEqual(HOOK.extract_paths("Write", {"file_path": "output/a.md"}), ["output/a.md"])

    def test_extracts_edit_path(self):
        self.assertEqual(HOOK.extract_paths("Edit", {"file_path": "output/a b.md"}), ["output/a b.md"])

    def test_patch_paths_preserve_order_and_deduplicate(self):
        payload = {
            "command": "\n".join(
                [
                    "*** Begin Patch",
                    "*** Add File: output/a b.md",
                    "+first",
                    "*** Update File: findings/c.md",
                    "*** Move to: findings/moved c.md",
                    "@@",
                    "-old",
                    "+new",
                    "*** Delete File: output/a b.md",
                    "*** End Patch",
                ]
            )
        }
        self.assertEqual(
            HOOK.extract_paths("apply_patch", payload),
            ["output/a b.md", "findings/c.md", "findings/moved c.md"],
        )

    def test_malformed_active_payload_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.mark_workspace(tmp)
            self.write_state(tmp)
            result = HOOK.handle(
                {
                    "hook_event_name": "PreToolUse",
                    "cwd": tmp,
                    "model": "gpt-selected",
                    "tool_name": "apply_patch",
                    "tool_input": {},
                }
            )
        self.assertEqual(result.exit_code, 2)
        self.assertIn("could not determine target path", result.stderr)

    def test_malformed_inactive_pretool_payload_is_inert(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = HOOK.handle(
                {
                    "hook_event_name": "PreToolUse",
                    "cwd": tmp,
                    "tool_name": "apply_patch",
                    "tool_input": {},
                }
            )
        self.assertEqual(result.exit_code, 0)

    def test_real_gate_allows_phase_output_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.mark_workspace(tmp)
            self.write_state(tmp)
            result = HOOK.handle(
                {
                    "hook_event_name": "PreToolUse",
                    "cwd": tmp,
                    "model": "gpt-selected",
                    "tool_name": "Write",
                    "tool_input": {"file_path": "output/a b.md"},
                },
                plugin_root=ROOT,
            )
        self.assertEqual(result.exit_code, 0)

    def test_real_gate_blocks_disallowed_phase_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.mark_workspace(tmp)
            self.write_state(tmp)
            result = HOOK.handle(
                {
                    "hook_event_name": "PreToolUse",
                    "cwd": tmp,
                    "model": "gpt-selected",
                    "tool_name": "apply_patch",
                    "tool_input": {
                        "command": "*** Begin Patch\n*** Add File: output/allowed.md\n+x\n"
                        "*** Add File: findings/blocked.md\n+y\n*** End Patch"
                    },
                },
                plugin_root=ROOT,
            )
        self.assertEqual(result.exit_code, 2)
        self.assertIn("not permitted to write", result.stderr)

    def test_pretool_write_and_edit_block_canonical_journal_with_recovery_diagnostic(self):
        for tool_name, path_form in (
            ("Write", "relative"),
            ("Edit", "absolute"),
        ):
            with (
                self.subTest(tool_name=tool_name, path_form=path_form),
                tempfile.TemporaryDirectory() as tmp,
            ):
                workspace = pathlib.Path(tmp)
                self.mark_workspace(workspace)
                self.write_state(workspace)
                target = workspace / "output" / "agent-conversation-2026-07-14.md"
                path = str(target) if path_form == "absolute" else target.relative_to(workspace).as_posix()
                result = HOOK.handle(
                    {
                        "hook_event_name": "PreToolUse",
                        "cwd": str(workspace),
                        "model": "gpt-selected",
                        "tool_name": tool_name,
                        "tool_input": {"file_path": path},
                    },
                    plugin_root=ROOT,
                )
            self.assertEqual(result.exit_code, 2)
            self.assertIn("scripts/agent-journal.py", result.stderr)
            self.assertIn("integrity validation", result.stderr.lower())

    def test_real_gate_blocks_disallowed_move_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.mark_workspace(tmp)
            self.write_state(tmp)
            result = HOOK.handle(
                {
                    "hook_event_name": "PreToolUse",
                    "cwd": tmp,
                    "model": "gpt-selected",
                    "tool_name": "apply_patch",
                    "tool_input": {
                        "command": "*** Begin Patch\n*** Update File: output/allowed.md\n"
                        "*** Move to: findings/disallowed.md\n@@\n-old\n+new\n*** End Patch"
                    },
                },
                plugin_root=ROOT,
            )
        self.assertEqual(result.exit_code, 2)
        self.assertIn("findings/disallowed.md", result.stderr)

    def test_real_gate_blocks_apply_patch_traversal_and_move_traversal(self):
        patches = (
            "*** Begin Patch\n*** Add File: findings/../escaped.md\n+x\n*** End Patch",
            "*** Begin Patch\n*** Update File: findings/allowed.md\n"
            "*** Move to: findings/../../escaped.md\n@@\n-old\n+new\n*** End Patch",
        )
        for patch in patches:
            with self.subTest(patch=patch), tempfile.TemporaryDirectory() as tmp:
                self.mark_workspace(tmp)
                self.write_state(tmp, current_phase="phase-1")
                result = HOOK.handle(
                    {
                        "hook_event_name": "PreToolUse",
                        "cwd": tmp,
                        "model": "gpt-selected",
                        "tool_name": "apply_patch",
                        "tool_input": {"command": patch},
                    },
                    plugin_root=ROOT,
                )
            self.assertEqual(result.exit_code, 2, result.stdout)
            self.assertIn("BLOCKED", result.stderr)

    def test_real_gate_blocks_symlink_escape_and_allows_spaces(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            workspace = pathlib.Path(tmp)
            self.mark_workspace(workspace)
            self.write_state(workspace, current_phase="phase-1")
            findings = workspace / "findings"
            findings.mkdir()
            (findings / "linked").symlink_to(outside, target_is_directory=True)
            escaped = HOOK.handle(
                {
                    "hook_event_name": "PreToolUse",
                    "cwd": str(workspace),
                    "model": "gpt-selected",
                    "tool_name": "Write",
                    "tool_input": {"file_path": "findings/linked/escaped.md"},
                },
                plugin_root=ROOT,
            )
            spaced = HOOK.handle(
                {
                    "hook_event_name": "PreToolUse",
                    "cwd": str(workspace),
                    "model": "gpt-selected",
                    "tool_name": "Write",
                    "tool_input": {"file_path": "findings/demo folder/report name.md"},
                },
                plugin_root=ROOT,
            )
        self.assertEqual(escaped.exit_code, 2, escaped.stdout)
        self.assertEqual(spaced.exit_code, 0, spaced.stderr)

    def test_real_gate_allows_only_exact_current_run_log_outside_phase_eight(self):
        for phase in ("triage-ingest", "phase-4"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                self.mark_workspace(tmp)
                self.write_state(tmp, current_phase=phase)

                def check(path):
                    return HOOK.handle(
                        {
                            "hook_event_name": "PreToolUse",
                            "cwd": tmp,
                            "model": "gpt-selected",
                            "tool_name": "Write",
                            "tool_input": {"file_path": path},
                        },
                        plugin_root=ROOT,
                    )

                exact = check("output/run-log-2026-07-14.md")
                adjacent = check("output/run-log-2026-07-14.md.bak")
                wrong_date = check("output/run-log-2026-07-15.md")
                other = check("output/other.md")
            self.assertEqual(exact.exit_code, 0, exact.stderr)
            for result in (adjacent, wrong_date, other):
                self.assertEqual(result.exit_code, 2, result.stdout)

    def test_real_stop_gate_blocks_missing_phase_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.mark_workspace(tmp)
            self.write_state(tmp)
            self.initialize_journal(tmp)
            result = HOOK.handle(
                {"hook_event_name": "Stop", "cwd": tmp},
                plugin_root=ROOT,
            )
        self.assertEqual(result.exit_code, 2)
        self.assertIn("has not written its required output", result.stderr)

    def test_real_stop_gate_allows_complete_phase_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.mark_workspace(tmp)
            self.write_state(tmp)
            output = pathlib.Path(tmp, "output")
            output.mkdir()
            pathlib.Path(output, "executive-summary-2026-07-14.md").write_text("summary\n")
            pathlib.Path(output, "executive-report-2026-07-14.html").write_text("<html></html>\n")
            self.initialize_journal(tmp)
            result = HOOK.handle(
                {"hook_event_name": "Stop", "cwd": tmp},
                plugin_root=ROOT,
            )
        self.assertEqual(result.exit_code, 0)

    def test_active_workspace_rejects_missing_gate_script(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as empty_root:
            self.mark_workspace(tmp)
            self.write_state(tmp)
            result = HOOK.handle(
                {"hook_event_name": "Stop", "cwd": tmp},
                plugin_root=pathlib.Path(empty_root),
            )
        self.assertEqual(result.exit_code, 2)
        self.assertIn("check-gate.sh", result.stderr)

    def test_malformed_json_is_inert_outside_review_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env.pop("PLUGIN_ROOT", None)
            result = subprocess.run(
                [sys.executable, str(HOOK_PATH)],
                input="{",
                cwd=tmp,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_malformed_json_blocks_inside_review_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.mark_workspace(tmp)
            self.write_state(tmp)
            env = os.environ.copy()
            env.pop("PLUGIN_ROOT", None)
            result = subprocess.run(
                [sys.executable, str(HOOK_PATH)],
                input="{",
                cwd=tmp,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("malformed hook JSON input", result.stderr)

    def test_main_emits_parseable_session_start_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, str(HOOK_PATH)],
                input=json.dumps(
                    {"hook_event_name": "SessionStart", "cwd": tmp, "model": "gpt-test"}
                ),
                cwd=tmp,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "SessionStart")

    def test_canonical_apply_patch_stdin_reaches_real_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.mark_workspace(tmp)
            self.write_state(tmp)
            env = os.environ.copy()
            env["PLUGIN_ROOT"] = str(ROOT)
            result = subprocess.run(
                [sys.executable, str(HOOK_PATH)],
                input=json.dumps(
                    {
                        "hook_event_name": "PreToolUse",
                        "cwd": tmp,
                        "model": "gpt-selected",
                        "tool_name": "apply_patch",
                        "tool_input": {
                            "command": "*** Begin Patch\n*** Add File: findings/blocked.md\n+x\n*** End Patch"
                        },
                    }
                ),
                cwd=tmp,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("not permitted to write", result.stderr)

    def test_explicit_unmarked_payload_cwd_is_inert_when_process_cwd_is_marked(self):
        with tempfile.TemporaryDirectory() as marked, tempfile.TemporaryDirectory() as unmarked:
            self.mark_workspace(marked)
            env = os.environ.copy()
            env.pop("PLUGIN_ROOT", None)
            result = subprocess.run(
                [sys.executable, str(HOOK_PATH)],
                input=json.dumps({"hook_event_name": "Stop", "cwd": unmarked}),
                cwd=marked,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_missing_payload_cwd_uses_marked_process_cwd(self):
        with tempfile.TemporaryDirectory() as marked:
            self.mark_workspace(marked)
            self.write_state(marked)
            env = os.environ.copy()
            env.pop("PLUGIN_ROOT", None)
            result = subprocess.run(
                [sys.executable, str(HOOK_PATH)],
                input=json.dumps({"hook_event_name": "Stop"}),
                cwd=marked,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("check-gate.sh", result.stderr)


if __name__ == "__main__":
    unittest.main()
