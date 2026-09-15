import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PREFLIGHT_PATH = ROOT / "scripts" / "preflight.py"
SPEC = importlib.util.spec_from_file_location("preflight", PREFLIGHT_PATH)
PREFLIGHT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PREFLIGHT
SPEC.loader.exec_module(PREFLIGHT)


class PreflightTests(unittest.TestCase):
    def test_project_depth_overrides_personal_depth(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            home = base / "home"
            workspace = base / "workspace"
            home.mkdir()
            (workspace / ".codex").mkdir(parents=True)
            (home / "config.toml").write_text("[agents]\nmax_depth = 2\n")
            (workspace / ".codex" / "config.toml").write_text("[agents]\nmax_depth = 5\n")
            depth, source = PREFLIGHT.effective_max_depth(workspace, home)
        self.assertEqual(depth, 5)
        self.assertTrue(source.endswith("workspace/.codex/config.toml"))

    def test_missing_depth_blocks_with_exact_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            result = PREFLIGHT.run_preflight(
                base / "workspace", base / "home", which=lambda name: f"/bin/{name}"
            )
        self.assertFalse(result.ok)
        self.assertIn("[agents]\nmax_depth = 5", "\n".join(result.errors))

    def test_depth_below_five_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            home = base / "home"
            home.mkdir()
            (home / "config.toml").write_text("[agents]\nmax_depth = 4\n")
            result = PREFLIGHT.run_preflight(
                base / "workspace", home, which=lambda name: f"/bin/{name}"
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.max_depth, 4)

    def test_depth_above_five_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            home = base / "home"
            home.mkdir()
            (home / "config.toml").write_text("[agents]\nmax_depth = 6\n")
            result = PREFLIGHT.run_preflight(
                base / "workspace", home, which=lambda name: f"/bin/{name}"
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.max_depth, 6)

    def test_project_threads_override_personal_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            home = base / "home"
            workspace = base / "workspace"
            home.mkdir()
            (workspace / ".codex").mkdir(parents=True)
            (home / "config.toml").write_text("[agents]\nmax_threads = 4\n")
            (workspace / ".codex" / "config.toml").write_text(
                "[agents]\nmax_threads = 8\n"
            )
            threads, source = PREFLIGHT.effective_max_threads(workspace, home)
        self.assertEqual(threads, 8)
        self.assertTrue(source.endswith("workspace/.codex/config.toml"))

    def test_missing_threads_uses_codex_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            home = base / "home"
            home.mkdir()
            (home / "config.toml").write_text("[agents]\nmax_depth = 5\n")
            result = PREFLIGHT.run_preflight(
                base / "workspace", home, which=lambda name: f"/bin/{name}"
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.max_threads, 6)
        self.assertEqual(result.max_threads_source, "Codex default")

    def test_threads_below_three_block_nested_reviewer_pairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            home = base / "home"
            home.mkdir()
            (home / "config.toml").write_text(
                "[agents]\nmax_depth = 5\nmax_threads = 2\n"
            )
            result = PREFLIGHT.run_preflight(
                base / "workspace", home, which=lambda name: f"/bin/{name}"
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.max_threads, 2)
        self.assertIn("max_threads = 3", "\n".join(result.errors))

    def test_invalid_toml_blocks_with_structured_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            home = base / "home"
            home.mkdir()
            (home / "config.toml").write_text("[agents\nmax_depth = 5\n")
            result = PREFLIGHT.run_preflight(
                base / "workspace", home, which=lambda name: f"/bin/{name}"
            )
        self.assertFalse(result.ok)
        self.assertTrue(any("Invalid Codex config" in error for error in result.errors))
        self.assertTrue(result.config_source.endswith("home/config.toml"))

    def test_non_integer_depth_types_block(self):
        for value in ('"5"', "true"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                base = pathlib.Path(tmp)
                home = base / "home"
                home.mkdir()
                (home / "config.toml").write_text(f"[agents]\nmax_depth = {value}\n")
                result = PREFLIGHT.run_preflight(
                    base / "workspace", home, which=lambda name: f"/bin/{name}"
                )
            self.assertFalse(result.ok)
            self.assertIsNone(result.max_depth)
            self.assertTrue(any("expected an integer" in error for error in result.errors))

    def test_invalid_project_depth_does_not_fall_back_to_personal_depth(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            home = base / "home"
            workspace = base / "workspace"
            home.mkdir()
            (workspace / ".codex").mkdir(parents=True)
            (home / "config.toml").write_text("[agents]\nmax_depth = 5\n")
            (workspace / ".codex" / "config.toml").write_text(
                '[agents]\nmax_depth = "invalid"\n'
            )
            result = PREFLIGHT.run_preflight(
                workspace, home, which=lambda name: f"/bin/{name}"
            )
        self.assertFalse(result.ok)
        self.assertIsNone(result.max_depth)
        self.assertTrue(result.config_source.endswith("workspace/.codex/config.toml"))

    def test_missing_required_executable_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            home = base / "home"
            home.mkdir()
            (home / "config.toml").write_text("[agents]\nmax_depth = 5\n")
            result = PREFLIGHT.run_preflight(
                base / "workspace",
                home,
                which=lambda name: None if name == "jq" else f"/bin/{name}",
            )
        self.assertFalse(result.ok)
        self.assertIn("Missing required executable: jq", result.errors)

    def test_hook_trust_warning_requires_manual_hooks_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            home = base / "home"
            home.mkdir()
            (home / "config.toml").write_text("[agents]\nmax_depth = 5\n")
            result = PREFLIGHT.run_preflight(
                base / "workspace", home, which=lambda name: f"/bin/{name}"
            )
        self.assertTrue(result.ok)
        self.assertTrue(any("/hooks" in warning for warning in result.warnings))
        self.assertTrue(any("task-start" in warning for warning in result.warnings))
        self.assertTrue(any("profile" in warning for warning in result.warnings))

    def test_cli_json_blocks_with_exit_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            workspace = base / "workspace"
            workspace.mkdir()
            result = subprocess.run(
                [
                    sys.executable,
                    str(PREFLIGHT_PATH),
                    "--codex-home",
                    str(base / "home"),
                    "--json",
                ],
                cwd=workspace,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(
            set(payload),
            {
                "ok",
                "max_depth",
                "config_source",
                "max_threads",
                "max_threads_source",
                "errors",
                "warnings",
            },
        )
        self.assertEqual(payload["max_threads"], 6)
        self.assertEqual(payload["max_threads_source"], "Codex default")
        self.assertIn("/hooks", "\n".join(payload["warnings"]))

    def test_cli_text_prints_source_errors_and_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            workspace = base / "workspace"
            workspace.mkdir()
            result = subprocess.run(
                [
                    sys.executable,
                    str(PREFLIGHT_PATH),
                    "--codex-home",
                    str(base / "home"),
                ],
                cwd=workspace,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Checked agents.max_depth: not configured", result.stdout)
        self.assertIn("Checked agents.max_threads: 6 (Codex default)", result.stdout)
        self.assertIn("ERROR:", result.stdout)
        self.assertIn("WARNING:", result.stdout)
        self.assertIn("/hooks", result.stdout)

    def test_cli_help_has_no_workspace_option(self):
        result = subprocess.run(
            [sys.executable, str(PREFLIGHT_PATH), "--help"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("--workspace", result.stdout)

    def test_cli_rejects_workspace_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "workspace"
            workspace.mkdir()
            result = subprocess.run(
                [
                    sys.executable,
                    str(PREFLIGHT_PATH),
                    "--workspace",
                    str(workspace),
                ],
                cwd=workspace,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("unrecognized arguments: --workspace", result.stderr)

    def test_cli_inspects_process_cwd_for_project_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            home = base / "home"
            workspace = base / "workspace"
            home.mkdir()
            (workspace / ".codex").mkdir(parents=True)
            (home / "config.toml").write_text("[agents]\nmax_depth = 2\n")
            (workspace / ".codex" / "config.toml").write_text("[agents]\nmax_depth = 5\n")
            result = subprocess.run(
                [
                    sys.executable,
                    str(PREFLIGHT_PATH),
                    "--codex-home",
                    str(home),
                    "--json",
                ],
                cwd=workspace,
                text=True,
                capture_output=True,
                check=False,
            )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["max_depth"], 5)
        self.assertTrue(payload["config_source"].endswith("workspace/.codex/config.toml"))


if __name__ == "__main__":
    unittest.main()
