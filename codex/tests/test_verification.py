import importlib.util
import inspect
import hashlib
import contextlib
import io
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent
RUNNER_PATH = ROOT / "scripts" / "run-verification.py"
WRITER_PATH = ROOT / "scripts" / "write-verification-report.py"
DISTRIBUTION_PATH = ROOT / "scripts" / "distribution_manifest.py"
VERIFY_PATH = ROOT / "scripts" / "verify-port.sh"
VALIDATORS = ROOT / "scripts" / "validators"
FULL_BASE_REVISION = "86015f536797e2ed7168f1cd1c2a0dbdaf35bf83"


def load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = load_module("nightfalcon_verification", RUNNER_PATH)
WRITER = load_module("nightfalcon_verification_report", WRITER_PATH)
DISTRIBUTION = load_module("nightfalcon_distribution_manifest_tests", DISTRIBUTION_PATH)

CORE_COMMAND_NAMES = (
    "wrapper-tests",
    "plugin-tests",
    "plugin-validator",
    "skill-validator",
    "shell-syntax",
    "python-compile",
    "json-validation",
    "inventory-regeneration",
    "inventory-stability",
    "port-scope",
)
INSTALL_COMMAND_NAMES = (
    "codex-marketplace-add",
    "codex-plugin-add",
    "codex-plugin-list",
    "codex-plugin-remove",
    "codex-marketplace-remove",
)


class VerificationDriverTests(unittest.TestCase):
    def _copy_scope_runner(self, distribution_root: pathlib.Path) -> pathlib.Path:
        scripts = distribution_root / "scripts"
        scripts.mkdir(parents=True)
        runner = scripts / RUNNER_PATH.name
        shutil.copy2(RUNNER_PATH, runner)
        shutil.copy2(DISTRIBUTION_PATH, scripts / DISTRIBUTION_PATH.name)
        (distribution_root / "verification-scope.json").write_text(
            (ROOT / "verification-scope.json").read_text()
        )
        return runner

    def test_standalone_prompt_contracts_keep_local_codex_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = pathlib.Path(tmp) / "codex"
            shutil.copytree(
                ROOT,
                fixture,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    str(fixture / "plugins" / "nightfalcon" / "tests"),
                    "-p",
                    "test_prompts.py",
                    "-v",
                ],
                cwd=fixture,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "test_orchestrators_record_agent_lifecycle_but_never_replay_journal",
            result.stderr,
        )

    def test_standalone_gate_contracts_keep_local_codex_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = pathlib.Path(tmp) / "codex"
            shutil.copytree(
                ROOT,
                fixture,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            tests = fixture / "plugins" / "nightfalcon" / "tests"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "test_gates.GateRegressionTests.test_agent_journal_direct_writes_are_blocked_in_every_phase",
                    "-v",
                ],
                cwd=tests,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Ran 1 test", result.stderr)

    def _canonical_history_source(self) -> pathlib.Path:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            self.skipTest("canonical repository history is unavailable")
        source = pathlib.Path(result.stdout.strip()).resolve()
        result = subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "cat-file",
                "-e",
                f"{FULL_BASE_REVISION}^{{commit}}",
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            self.skipTest("canonical base object is unavailable")
        return source

    def _seed_full_base_and_shadow_short_ref(self, repo: pathlib.Path) -> None:
        source = self._canonical_history_source()
        subprocess.run(
            [
                "git",
                "fetch",
                "-q",
                "--no-tags",
                str(source),
                FULL_BASE_REVISION,
            ],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            ["git", "tag", "-f", "2da50fe", "HEAD"], cwd=repo, check=True
        )

    def _scope_repository(
        self,
        root: pathlib.Path,
        *,
        legacy_deletion_exceptions=(),
        legacy_rename_source_files=(),
        legacy_rename_source_directories=(),
        baseline_paths=(),
    ):
        wrapper = root / "codex"
        wrapper.mkdir(parents=True)
        (wrapper / "verification-scope.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "allowed_files": [".gitignore", "CLAUDE.md", "README.md"],
                    "allowed_directories": [
                        "claude",
                        "codex",
                        "cursor",
                        "docs",
                        "tests",
                    ],
                    "legacy_deletion_exceptions": list(legacy_deletion_exceptions),
                    "legacy_rename_source_files": list(legacy_rename_source_files),
                    "legacy_rename_source_directories": list(
                        legacy_rename_source_directories
                    ),
                }
            )
        )
        for relative in baseline_paths:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"baseline: {relative}\n")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=NightFalcon Tests",
                "-c",
                "user.email=nightfalcon-tests@example.invalid",
                "commit",
                "-qm",
                "baseline",
            ],
            cwd=root,
            check=True,
        )
        base_revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()
        return wrapper, base_revision

    def _command_results(self, names):
        return [
            {
                "name": name,
                "command": ["<COMMAND>", name],
                "cwd": "<WRAPPER>",
                "env": (
                    {"CODEX_HOME": "<VERIFY_TEMP_ROOT>/codex-home"}
                    if name in INSTALL_COMMAND_NAMES
                    else {}
                ),
                "required_stdout": None,
                "exit_code": 0,
                "passed": True,
                "stdout_bytes": 0,
                "stdout_sha256": hashlib.sha256(b"").hexdigest(),
                "stderr_bytes": 0,
                "stderr_sha256": hashlib.sha256(b"").hexdigest(),
            }
            for name in names
        ]

    def _matrix_digest(self, commands):
        subject = {
            "format_version": 2,
            "commands": [
                {
                    "name": command["name"],
                    "command": command["command"],
                    "cwd": command["cwd"],
                    "env": command.get("env", {}),
                    "required_stdout": command.get("required_stdout"),
                }
                for command in commands
            ],
        }
        canonical = json.dumps(
            subject, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        return hashlib.sha256(canonical).hexdigest()

    def _digest_repository(self, root: pathlib.Path) -> pathlib.Path:
        scope = RUNNER.load_verification_scope(ROOT / "verification-scope.json")
        for relative in scope.allowed_files:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"source: {relative}\n")
        for relative in scope.allowed_directories:
            (root / relative).mkdir(parents=True, exist_ok=True)
        wrapper = root / "codex"
        (wrapper / "verification-scope.json").write_text(
            (ROOT / "verification-scope.json").read_text()
        )
        (wrapper / "wrapper-source.txt").write_text("wrapper source\n")
        (root / "claude" / "claude-source.txt").write_text(
            "claude source\n"
        )
        (root / "cursor" / "cursor-source.txt").write_text(
            "cursor source\n"
        )
        (root / "docs" / "release.md").write_text("release docs\n")
        (root / "tests" / "release_test.py").write_text("release test\n")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        return wrapper

    def _scope_bound_run(self, wrapper, run_id="run-one", names=CORE_COMMAND_NAMES):
        manifest = RUNNER.source_manifest(wrapper)
        commands = self._command_results(names)
        return RUNNER.build_run_record(
            run_id=run_id,
            timestamp="2026-07-17T12:00:00Z",
            source_manifest=manifest,
            matrix_kind=(
                "core+isolated-install"
                if set(INSTALL_COMMAND_NAMES).issubset(names)
                else "core-only"
            ),
            command_results=commands,
        )

    def _validator_environment(self):
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHON_BIN": sys.executable,
                "YAML_PYTHON": RUNNER.find_yaml_python(
                    [sys.executable, *RUNNER.yaml_python_candidates()]
                ),
                "PLUGIN_CREATOR": str(
                    RUNNER.locate_validator(
                        "plugin-creator", "validate_plugin.py", "PLUGIN_CREATOR"
                    )
                ),
                "SKILL_CREATOR": str(
                    RUNNER.locate_validator(
                        "skill-creator", "quick_validate.py", "SKILL_CREATOR"
                    )
                ),
            }
        )
        return environment

    def test_yaml_python_selection_uses_first_interpreter_that_imports_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp).resolve()
            reject = base / "python-reject"
            accept = base / "python-accept"
            reject.write_text("#!/usr/bin/env bash\nexit 1\n")
            accept.write_text("#!/usr/bin/env bash\nexit 0\n")
            reject.chmod(0o755)
            accept.chmod(0o755)
            selected = RUNNER.find_yaml_python([str(reject), str(accept)])
        self.assertEqual(selected, str(accept))

    def test_yaml_python_selection_fails_clearly_when_none_import_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            reject = pathlib.Path(tmp) / "python-reject"
            reject.write_text("#!/usr/bin/env bash\nexit 1\n")
            reject.chmod(0o755)
            with self.assertRaisesRegex(RUNNER.VerificationError, "PyYAML"):
                RUNNER.find_yaml_python([str(reject)])

    def test_validator_location_falls_back_to_vendored_snapshots(self):
        self.assertIn("wrapper", inspect.signature(RUNNER.locate_validator).parameters)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                "HOME": tmp,
                "CODEX_HOME": str(pathlib.Path(tmp) / "isolated-codex"),
            },
            clear=False,
        ):
            os.environ.pop("PLUGIN_CREATOR", None)
            os.environ.pop("SKILL_CREATOR", None)
            plugin_validator = RUNNER.locate_validator(
                "plugin-creator", "validate_plugin.py", "PLUGIN_CREATOR", wrapper=ROOT
            )
            skill_validator = RUNNER.locate_validator(
                "skill-creator", "quick_validate.py", "SKILL_CREATOR", wrapper=ROOT
            )
        self.assertEqual(plugin_validator, VALIDATORS / "validate_plugin.py")
        self.assertEqual(skill_validator, VALIDATORS / "quick_validate.py")

    def test_workflow_pins_yaml_and_provisions_local_validators(self):
        workflow_path = ROOT / ".github" / "workflows" / "validate.yml"
        text = workflow_path.read_text()
        yaml_python = RUNNER.find_yaml_python(RUNNER.yaml_python_candidates())
        parsed = subprocess.run(
            [
                yaml_python,
                "-c",
                "import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))",
                str(workflow_path),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(parsed.returncode, 0, parsed.stderr)
        workflow = json.loads(parsed.stdout)
        steps = workflow["jobs"]["validate"]["steps"]
        setup = next(step for step in steps if step.get("uses") == "actions/setup-python@v5")
        self.assertEqual(str(setup["with"]["python-version"]), "3.11")
        self.assertIn("PyYAML==6.0.3", text)
        verify = next(step for step in steps if "verify-port.sh" in step.get("run", ""))
        self.assertEqual(verify["run"], "bash scripts/verify-port.sh --skip-codex-install")
        self.assertEqual(verify["env"]["YAML_PYTHON"], "python3")
        self.assertEqual(
            verify["env"]["PLUGIN_CREATOR"],
            "${{ github.workspace }}/scripts/validators/validate_plugin.py",
        )
        self.assertEqual(
            verify["env"]["SKILL_CREATOR"],
            "${{ github.workspace }}/scripts/validators/quick_validate.py",
        )

    def test_command_execution_records_results_and_stops_on_first_failure(self):
        commands = [
            RUNNER.Command(
                "first",
                (sys.executable, "-c", "print('first-ok')"),
            ),
            RUNNER.Command(
                "second",
                (sys.executable, "-c", "import sys; print('second-failed'); sys.exit(7)"),
            ),
            RUNNER.Command(
                "must-not-run",
                (sys.executable, "-c", "raise SystemExit('unexpected')"),
            ),
        ]
        results = RUNNER.execute_commands(commands)
        self.assertEqual([item["name"] for item in results], ["first", "second"])
        self.assertEqual(results[0]["exit_code"], 0)
        self.assertEqual(results[1]["exit_code"], 7)
        self.assertNotIn("stdout", results[1])
        self.assertIn("stdout_sha256", results[1])
        self.assertEqual(results[1]["failure_detail"], "Command failed; inspect the console output.")

    def test_command_evidence_is_bounded_and_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret_path = pathlib.Path(tmp) / "credential-token"
            command = RUNNER.Command(
                "privacy",
                (
                    sys.executable,
                    "-c",
                    f"print({str(secret_path)!r}); print('SECRET_TOKEN=do-not-persist'); raise SystemExit(3)",
                ),
                cwd=pathlib.Path(tmp),
            )
            result = RUNNER.execute_commands(
                [command], replacements={tmp: "<TEMP>", sys.executable: "<PYTHON>"}
            )[0]
        serialized = json.dumps(result)
        self.assertNotIn(tmp, serialized)
        self.assertNotIn("do-not-persist", serialized)
        self.assertNotIn("stdout", result)
        self.assertNotIn("stderr", result)
        self.assertEqual(result["command"][0], "<PYTHON>")
        self.assertEqual(result["cwd"], "<TEMP>")

    def test_split_arguments_common_credentials_and_stream_display_are_redacted(self):
        secrets = {
            "split-argument-secret",
            "authorization-secret",
            "database-password",
            "github-pat-secret",
            "aws-access-secret",
            "credential-file-secret",
        }
        command = RUNNER.Command(
            "credential-sanitization",
            (
                sys.executable,
                "-c",
                "pass",
                "--token",
                "split-argument-secret",
                "--header",
                "Authorization: Bearer authorization-secret",
                "postgresql://user:database-password@db.example.invalid/name",
            ),
            env={
                "GITHUB_PAT": "github-pat-secret",
                "AWS_ACCESS_KEY_ID": "aws-access-secret",
                "GOOGLE_APPLICATION_CREDENTIALS": "credential-file-secret",
            },
        )
        console = io.StringIO()
        with contextlib.redirect_stdout(console), contextlib.redirect_stderr(console):
            result = RUNNER.execute_commands([command], stream=True)[0]

        serialized = json.dumps(result) + console.getvalue()
        for secret in secrets:
            self.assertNotIn(secret, serialized)
        self.assertEqual(result["command"][4], "<REDACTED>")
        self.assertEqual(result["env"]["GITHUB_PAT"], "<REDACTED>")
        self.assertEqual(result["env"]["AWS_ACCESS_KEY_ID"], "<REDACTED>")
        self.assertEqual(
            result["env"]["GOOGLE_APPLICATION_CREDENTIALS"], "<REDACTED>"
        )

    def test_normalized_command_evidence_is_checkout_location_independent(self):
        results = []
        for label in ("checkout-one", "checkout-two-with-longer-name"):
            with tempfile.TemporaryDirectory(prefix=label) as tmp:
                command = RUNNER.Command(
                    "location",
                    (sys.executable, "-c", "import os; print(os.getcwd())"),
                    cwd=pathlib.Path(tmp),
                )
                results.append(
                    RUNNER.execute_commands(
                        [command],
                        replacements={tmp: "<WRAPPER>", sys.executable: "<PYTHON>"},
                    )[0]
                )
        self.assertEqual(results[0], results[1])

    def test_command_matrix_binds_sanitized_explicit_environment_and_stdout_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = RUNNER.Command(
                "environment",
                (sys.executable, "-c", "print('selector-present')"),
                env={
                    "CODEX_HOME": str(pathlib.Path(tmp) / "codex-home"),
                    "SECRET_TOKEN": "must-not-persist",
                },
                required_stdout="selector-present",
            )
            result = RUNNER.execute_commands(
                [command], replacements={tmp: "<TEMP>", sys.executable: "<PYTHON>"}
            )[0]

        self.assertEqual(result["env"]["CODEX_HOME"], "<TEMP>/codex-home")
        self.assertEqual(result["env"]["SECRET_TOKEN"], "<REDACTED>")
        self.assertEqual(result["required_stdout"], "selector-present")
        original = RUNNER.matrix_digest([result])
        result["env"]["CODEX_HOME"] = "<HOME>/.codex"
        self.assertNotEqual(RUNNER.matrix_digest([result]), original)

    def test_core_plan_preserves_required_check_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = pathlib.Path(tmp) / "migration-map.before.json"
            snapshot.write_text("{}\n")
            commands = RUNNER.build_core_commands(
                wrapper=ROOT,
                python_bin=sys.executable,
                yaml_python=sys.executable,
                plugin_validator=pathlib.Path("/validator/plugin.py"),
                skill_validator=pathlib.Path("/validator/skill.py"),
                inventory_snapshot=snapshot,
            )
        self.assertEqual(
            [command.name for command in commands],
            [
                "wrapper-tests",
                "plugin-tests",
                "plugin-validator",
                "skill-validator",
                "shell-syntax",
                "python-compile",
                "json-validation",
                "inventory-regeneration",
                "inventory-stability",
                "port-scope",
            ],
        )
        self.assertEqual(commands[2].args[0], sys.executable)
        self.assertEqual(commands[3].args[0], sys.executable)
        git_root = pathlib.Path(
            subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"], cwd=ROOT, text=True
            ).strip()
        ).resolve()
        has_outer_marker = any(
            (ancestor / ".git").exists() or (ancestor / ".git").is_symlink()
            for ancestor in ROOT.resolve().parents
        )
        if git_root == ROOT.resolve() and not has_outer_marker:
            self.assertIn("--allow-standalone-without-base", commands[-1].args)
        else:
            self.assertNotIn("--allow-standalone-without-base", commands[-1].args)

    def test_scope_check_accepts_combined_three_port_release_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            wrapper, base_revision = self._scope_repository(repo)
            allowed_paths = (
                ".gitignore",
                "CLAUDE.md",
                "README.md",
                "claude/plugin.json",
                "codex/scripts/run-verification.py",
                "cursor/.cursor/hooks/gate-guard.sh",
                "docs/release-notes.md",
                "tests/test_cross_port.py",
            )
            for relative in allowed_paths:
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"changed: {relative}\n")
            subprocess.run(["git", "add", "--", *allowed_paths], cwd=repo, check=True)

            ok, message = RUNNER.scope_check(wrapper, base_revision)

        self.assertTrue(ok, message)

    def test_scope_check_fails_closed_when_base_revision_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            wrapper, _ = self._scope_repository(repo)
            release_note = repo / "docs" / "release.md"
            release_note.parent.mkdir()
            release_note.write_text("committed after the unavailable baseline\n")
            subprocess.run(
                ["git", "add", "--", "docs/release.md"], cwd=repo, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=NightFalcon Tests",
                    "-c",
                    "user.email=nightfalcon-tests@example.invalid",
                    "commit",
                    "-qm",
                    "release change",
                ],
                cwd=repo,
                check=True,
            )

            ok, message = RUNNER.scope_check(wrapper, "missing-baseline")
            opted_in_ok, opted_in_message = RUNNER.scope_check(
                wrapper,
                "missing-baseline",
                allow_standalone_without_base=True,
            )

        self.assertFalse(ok)
        self.assertIn("missing-baseline", message)
        self.assertIn("unavailable", message.lower())
        self.assertFalse(opted_in_ok, opted_in_message)

    def test_scope_check_ignores_local_short_ref_shadowing_canonical_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            source = self._canonical_history_source()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "fetch",
                    "-q",
                    "--no-tags",
                    str(source),
                    FULL_BASE_REVISION,
                ],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "checkout", "-q", "--detach", FULL_BASE_REVISION],
                cwd=repo,
                check=True,
            )
            wrapper = repo / "codex"
            wrapper.mkdir(exist_ok=True)
            (wrapper / "verification-scope.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "allowed_files": [".gitignore", "CLAUDE.md", "README.md"],
                        "allowed_directories": [
                            "claude",
                            "codex",
                            "cursor",
                            "docs",
                            "tests",
                        ],
                        "legacy_deletion_exceptions": [],
                        "legacy_rename_source_files": [],
                        "legacy_rename_source_directories": [],
                    }
                )
            )
            outside = repo / "OUTSIDE_SCOPE.md"
            outside.write_text("must remain visible to the canonical diff\n")
            subprocess.run(
                [
                    "git",
                    "add",
                    "--",
                    "codex/verification-scope.json",
                    outside.name,
                ],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=NightFalcon Tests",
                    "-c",
                    "user.email=nightfalcon-tests@example.invalid",
                    "commit",
                    "-qm",
                    "outside scope change",
                ],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "tag", "-f", "2da50fe", "HEAD"], cwd=repo, check=True
            )

            ok, message = RUNNER.scope_check(wrapper, RUNNER.BASE_REVISION)

        self.assertFalse(ok, message)
        self.assertIn(outside.name, message)

    def test_scope_check_rejects_blob_and_symlink_at_exact_directory_rule(self):
        for entry_kind, relative in (("blob", "docs"), ("symlink", "tests")):
            with (
                self.subTest(entry_kind=entry_kind),
                tempfile.TemporaryDirectory() as tmp,
            ):
                repo = pathlib.Path(tmp)
                wrapper, base_revision = self._scope_repository(repo)
                entry = repo / relative
                if entry_kind == "blob":
                    entry.write_text("not a directory\n")
                else:
                    entry.symlink_to("missing-symlink-target")
                subprocess.run(["git", "add", "--", relative], cwd=repo, check=True)

                ok, message = RUNNER.scope_check(wrapper, base_revision)

            self.assertFalse(ok)
            self.assertIn(relative, message)

    def test_scope_check_rejects_gitlink_at_exact_directory_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            wrapper, base_revision = self._scope_repository(repo)
            subprocess.run(
                [
                    "git",
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"160000,{base_revision},claude",
                ],
                cwd=repo,
                check=True,
            )

            ok, message = RUNNER.scope_check(wrapper, base_revision)

        self.assertFalse(ok)
        self.assertIn("claude", message)

    def test_legacy_directory_rule_authorizes_only_descendants(self):
        scope = RUNNER.VerificationScope(
            allowed_files=frozenset({"README.md"}),
            allowed_directories=("docs",),
            legacy_deletion_exceptions=frozenset(),
            legacy_rename_source_files=frozenset(),
            legacy_rename_source_directories=("legacy",),
        )

        self.assertFalse(scope.allows_legacy_rename_source("legacy"))
        self.assertTrue(scope.allows_legacy_rename_source("legacy/file.md"))

    def test_scope_check_authenticates_copied_standalone_distribution_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            wrapper = base / "distribution"
            runner = self._copy_scope_runner(wrapper)
            subprocess.run(["git", "init", "-q"], cwd=wrapper, check=True)
            subprocess.run(["git", "add", "-A"], cwd=wrapper, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=NightFalcon Tests",
                    "-c",
                    "user.email=nightfalcon-tests@example.invalid",
                    "commit",
                    "-qm",
                    "standalone baseline",
                ],
                cwd=wrapper,
                check=True,
            )
            wrapper_link = base / "distribution-link"
            wrapper_link.symlink_to(wrapper, target_is_directory=True)

            default_result = subprocess.run(
                [
                    sys.executable,
                    str(runner),
                    "--internal-scope-check",
                    str(wrapper),
                    "missing-baseline",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            opted_in_result = subprocess.run(
                [
                    sys.executable,
                    str(runner),
                    "--internal-scope-check",
                    str(wrapper_link),
                    FULL_BASE_REVISION,
                    "--allow-standalone-without-base",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(default_result.returncode, 1, default_result.stdout)
        self.assertEqual(opted_in_result.returncode, 0, opted_in_result.stderr)

    def test_scope_check_rejects_nested_git_directory_inside_outer_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            outer = pathlib.Path(tmp)
            wrapper = outer / "codex"
            runner = self._copy_scope_runner(wrapper)
            subprocess.run(["git", "init", "-q"], cwd=outer, check=True)
            subprocess.run(["git", "add", "-A"], cwd=outer, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=NightFalcon Tests",
                    "-c",
                    "user.email=nightfalcon-tests@example.invalid",
                    "commit",
                    "-qm",
                    "outer baseline",
                ],
                cwd=outer,
                check=True,
            )
            subprocess.run(["git", "init", "-q"], cwd=wrapper, check=True)
            subprocess.run(["git", "add", "-A"], cwd=wrapper, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=NightFalcon Tests",
                    "-c",
                    "user.email=nightfalcon-tests@example.invalid",
                    "commit",
                    "-qm",
                    "nested baseline",
                ],
                cwd=wrapper,
                check=True,
            )
            self._seed_full_base_and_shadow_short_ref(wrapper)

            result = subprocess.run(
                [
                    sys.executable,
                    str(runner),
                    "--internal-scope-check",
                    str(wrapper),
                    RUNNER.BASE_REVISION,
                    "--allow-standalone-without-base",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("enclosing", result.stderr.lower())

    def test_scope_check_rejects_nested_gitfile_inside_outer_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            outer = base / "outer"
            inner = base / "inner"
            wrapper = outer / "codex"
            outer.mkdir()
            inner.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=outer, check=True)
            subprocess.run(["git", "init", "-q"], cwd=inner, check=True)
            runner = self._copy_scope_runner(inner)
            subprocess.run(["git", "add", "-A"], cwd=inner, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=NightFalcon Tests",
                    "-c",
                    "user.email=nightfalcon-tests@example.invalid",
                    "commit",
                    "-qm",
                    "inner baseline",
                ],
                cwd=inner,
                check=True,
            )
            self._seed_full_base_and_shadow_short_ref(inner)
            subprocess.run(
                [
                    "git",
                    "worktree",
                    "add",
                    "-q",
                    "--detach",
                    str(wrapper),
                    "HEAD",
                ],
                cwd=inner,
                check=True,
            )
            runner = wrapper / "scripts" / runner.name

            result = subprocess.run(
                [
                    sys.executable,
                    str(runner),
                    "--internal-scope-check",
                    str(wrapper),
                    RUNNER.BASE_REVISION,
                    "--allow-standalone-without-base",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("enclosing", result.stderr.lower())

    def test_scope_check_rejects_nested_git_symlink_inside_outer_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            outer = base / "outer"
            wrapper = outer / "codex"
            git_directory = base / "inner.git"
            outer.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=outer, check=True)
            runner = self._copy_scope_runner(wrapper)
            subprocess.run(
                [
                    "git",
                    "init",
                    "-q",
                    f"--separate-git-dir={git_directory}",
                    str(wrapper),
                ],
                check=True,
            )
            (wrapper / ".git").unlink()
            (wrapper / ".git").symlink_to(git_directory, target_is_directory=True)
            subprocess.run(["git", "add", "-A"], cwd=wrapper, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=NightFalcon Tests",
                    "-c",
                    "user.email=nightfalcon-tests@example.invalid",
                    "commit",
                    "-qm",
                    "symlinked git baseline",
                ],
                cwd=wrapper,
                check=True,
            )
            self._seed_full_base_and_shadow_short_ref(wrapper)

            result = subprocess.run(
                [
                    sys.executable,
                    str(runner),
                    "--internal-scope-check",
                    str(wrapper),
                    RUNNER.BASE_REVISION,
                    "--allow-standalone-without-base",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("enclosing", result.stderr.lower())

    def test_scope_check_rejects_outer_repository_as_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            outer = pathlib.Path(tmp)
            distribution = outer / "codex"
            runner = self._copy_scope_runner(distribution)
            (outer / "verification-scope.json").write_text(
                (ROOT / "verification-scope.json").read_text()
            )
            subprocess.run(["git", "init", "-q"], cwd=outer, check=True)
            subprocess.run(["git", "add", "-A"], cwd=outer, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=NightFalcon Tests",
                    "-c",
                    "user.email=nightfalcon-tests@example.invalid",
                    "commit",
                    "-qm",
                    "canonical baseline",
                ],
                cwd=outer,
                check=True,
            )
            base_revision = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=outer, text=True
            ).strip()

            result = subprocess.run(
                [
                    sys.executable,
                    str(runner),
                    "--internal-scope-check",
                    str(outer),
                    base_revision,
                    "--allow-standalone-without-base",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("distribution root", result.stderr.lower())

    def test_scope_config_rejects_malformed_duplicate_and_overlapping_rules(self):
        valid = {
            "schema_version": 1,
            "allowed_files": ["README.md"],
            "allowed_directories": ["docs"],
            "legacy_deletion_exceptions": [],
            "legacy_rename_source_files": [],
            "legacy_rename_source_directories": [],
        }
        invalid_documents = {
            "boolean schema version": {**valid, "schema_version": True},
            "unknown property": {**valid, "unexpected": []},
            "empty file rules": {**valid, "allowed_files": []},
            "duplicate file": {
                **valid,
                "allowed_files": ["README.md", "README.md"],
            },
            "duplicate directory": {
                **valid,
                "allowed_directories": ["docs", "docs"],
            },
            "traversal": {**valid, "allowed_directories": ["docs/../private"]},
            "absolute path": {**valid, "allowed_files": ["/README.md"]},
            "backslash path": {**valid, "allowed_directories": ["docs\\archive"]},
            "non-normalized path": {
                **valid,
                "allowed_directories": ["docs//archive"],
            },
            "file covered by directory": {
                **valid,
                "allowed_files": ["docs/index.md"],
            },
            "nested directories": {
                **valid,
                "allowed_directories": ["docs", "docs/archive"],
            },
            "duplicate legacy deletion": {
                **valid,
                "legacy_deletion_exceptions": [
                    "legacy/obsolete.md",
                    "legacy/obsolete.md",
                ],
            },
            "legacy deletion inside allowed directory": {
                **valid,
                "legacy_deletion_exceptions": ["docs/obsolete.md"],
            },
            "duplicate legacy rename source": {
                **valid,
                "legacy_rename_source_files": ["legacy.md", "legacy.md"],
            },
            "legacy rename source inside allowed directory": {
                **valid,
                "legacy_rename_source_directories": ["docs/archive"],
            },
            "overlapping legacy rename sources": {
                **valid,
                "legacy_rename_source_files": ["legacy/old.md"],
                "legacy_rename_source_directories": ["legacy"],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "verification-scope.json"
            for label, document in invalid_documents.items():
                with self.subTest(label=label):
                    path.write_text(json.dumps(document))
                    with self.assertRaises(RUNNER.VerificationError):
                        RUNNER.load_verification_scope(path)

    def test_scope_config_rejects_duplicate_json_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "verification-scope.json"
            path.write_text(
                '{"schema_version":1,"schema_version":1,'
                '"allowed_files":["README.md"],'
                '"allowed_directories":["docs"],'
                '"legacy_deletion_exceptions":[],'
                '"legacy_rename_source_files":[],'
                '"legacy_rename_source_directories":[]}'
            )

            with self.assertRaises(RUNNER.VerificationError):
                RUNNER.load_verification_scope(path)

    def test_repository_scope_declares_exact_combined_release_paths(self):
        scope = RUNNER.load_verification_scope(ROOT / "verification-scope.json")

        self.assertEqual(
            scope.allowed_files,
            frozenset({
                ".gitignore", ".npmignore", "README.md", "LICENSE", "NOTICE",
                "SECURITY.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md",
                "SUPPORT.md", "migration-manifest.json", "owasp-provenance.json",
                "source-migration-map.json", "Cargo.toml", "MANIFEST.in", "VERSION",
                "composer.json", "embedded.go", "embedded_test.go", "go.mod",
                "nightfalcon.gemspec", "package.json", "pom.xml", "pyproject.toml",
            }),
        )
        self.assertEqual(
            set(scope.allowed_directories),
            {
                "claude",
                "cmd",
                "codex",
                "cursor",
                ".github",
                "docs",
                "packaging",
                "scripts",
                "shared",
                "src",
                "tests",
            },
        )
        self.assertEqual(
            scope.legacy_deletion_exceptions,
            frozenset(),
        )
        self.assertEqual(
            scope.legacy_rename_source_files,
            frozenset(),
        )
        self.assertEqual(
            set(scope.legacy_rename_source_directories),
            set(),
        )

    def test_current_combined_release_is_within_configured_scope(self):
        git_root = pathlib.Path(
            subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"], cwd=ROOT, text=True
            ).strip()
        ).resolve()
        base_present = subprocess.run(
            ["git", "cat-file", "-e", f"{RUNNER.BASE_REVISION}^{{commit}}"],
            cwd=git_root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
        if git_root != ROOT.resolve():
            self.assertTrue(
                base_present,
                "public combined checkout must contain its canonical base commit",
            )
        ok, message = RUNNER.scope_check(
            ROOT,
            RUNNER.BASE_REVISION,
            allow_standalone_without_base=git_root == ROOT.resolve(),
        )

        self.assertEqual(RUNNER.BASE_REVISION, FULL_BASE_REVISION)
        if base_present:
            merge_base = subprocess.check_output(
                ["git", "merge-base", RUNNER.BASE_REVISION, "HEAD"],
                cwd=git_root,
                text=True,
            ).strip()
            self.assertEqual(merge_base, FULL_BASE_REVISION)
        else:
            self.assertEqual(git_root, ROOT.resolve())
        self.assertTrue(ok, message)

    def test_scope_check_rejects_unknown_top_level_and_sibling_prefix_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            wrapper, base_revision = self._scope_repository(repo)
            outside_paths = (
                "CONTRIBUTING.md",
                "claude-archive/unexpected.md",
            )
            for relative in outside_paths:
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("outside configured release scope\n")
            subprocess.run(["git", "add", "--", *outside_paths], cwd=repo, check=True)

            ok, message = RUNNER.scope_check(wrapper, base_revision)

        self.assertFalse(ok)
        for relative in outside_paths:
            self.assertIn(relative, message)

    def test_scope_check_allows_only_declared_deletion_only_legacy_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            declared = "legacy/obsolete.md"
            undeclared = "legacy/must-remain.md"
            wrapper, base_revision = self._scope_repository(
                repo,
                legacy_deletion_exceptions=(declared,),
                baseline_paths=(declared, undeclared),
            )
            (repo / declared).unlink()
            subprocess.run(["git", "add", "-u", "--", declared], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=NightFalcon Tests",
                    "-c",
                    "user.email=nightfalcon-tests@example.invalid",
                    "commit",
                    "-qm",
                    "remove declared legacy path",
                ],
                cwd=repo,
                check=True,
            )

            declared_ok, declared_message = RUNNER.scope_check(wrapper, base_revision)

            (repo / undeclared).unlink()
            subprocess.run(["git", "add", "-u", "--", undeclared], cwd=repo, check=True)
            undeclared_ok, undeclared_message = RUNNER.scope_check(
                wrapper, base_revision
            )

        self.assertTrue(declared_ok, declared_message)
        self.assertFalse(undeclared_ok)
        self.assertIn(undeclared, undeclared_message)

    def test_scope_check_rejects_readded_legacy_deletion_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            legacy = "legacy/obsolete.md"
            wrapper, base_revision = self._scope_repository(
                repo,
                legacy_deletion_exceptions=(legacy,),
                baseline_paths=(legacy,),
            )
            subprocess.run(["git", "rm", "-q", "--", legacy], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=NightFalcon Tests",
                    "-c",
                    "user.email=nightfalcon-tests@example.invalid",
                    "commit",
                    "-qm",
                    "remove legacy path",
                ],
                cwd=repo,
                check=True,
            )
            (repo / legacy).parent.mkdir(parents=True, exist_ok=True)
            (repo / legacy).write_text("reintroduced\n")
            subprocess.run(["git", "add", "--", legacy], cwd=repo, check=True)

            ok, message = RUNNER.scope_check(wrapper, base_revision)

        self.assertFalse(ok)
        self.assertIn(legacy, message)

    def test_scope_check_allows_only_declared_legacy_rename_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            declared = "legacy/declared.md"
            undeclared = "private/undeclared.md"
            wrapper, base_revision = self._scope_repository(
                repo,
                legacy_rename_source_directories=("legacy",),
                baseline_paths=(declared, undeclared),
            )
            (repo / "docs").mkdir()
            subprocess.run(
                ["git", "mv", "--", declared, "docs/declared.md"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=NightFalcon Tests",
                    "-c",
                    "user.email=nightfalcon-tests@example.invalid",
                    "commit",
                    "-qm",
                    "migrate declared legacy path",
                ],
                cwd=repo,
                check=True,
            )

            declared_ok, declared_message = RUNNER.scope_check(wrapper, base_revision)

            subprocess.run(
                ["git", "mv", "--", undeclared, "docs/undeclared.md"],
                cwd=repo,
                check=True,
            )
            undeclared_ok, undeclared_message = RUNNER.scope_check(
                wrapper, base_revision
            )

        self.assertTrue(declared_ok, declared_message)
        self.assertFalse(undeclared_ok)
        self.assertIn(undeclared, undeclared_message)

    def test_distribution_manifest_is_location_independent_sensitive_and_cache_clean(self):
        self.assertTrue(DISTRIBUTION_PATH.is_file())
        distribution = load_module("nightfalcon_distribution_manifest", DISTRIBUTION_PATH)
        manifests = []
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            for root in (pathlib.Path(first), pathlib.Path(second)):
                (root / "nested").mkdir()
                (root / "nested" / "payload.txt").write_text("stable\n")
                (root / "__pycache__").mkdir()
                (root / "__pycache__" / "ignored.pyc").write_bytes(b"cache")
                (root / ".DS_Store").write_bytes(b"metadata")
                manifests.append(distribution.build_manifest(root))

            self.assertEqual(manifests[0]["digest"], manifests[1]["digest"])
            self.assertEqual(manifests[0]["file_count"], 1)
            self.assertEqual(manifests[0]["entries"][0]["path"], "nested/payload.txt")

            pathlib.Path(second, "nested", "payload.txt").write_text("changed\n")
            changed = distribution.build_manifest(pathlib.Path(second))
            self.assertNotEqual(manifests[0]["digest"], changed["digest"])

    def test_distribution_manifest_rejects_absolute_symlink_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            target = root / "payload.txt"
            target.write_text("payload\n")
            (root / "absolute-link").symlink_to(target)

            with self.assertRaisesRegex(DISTRIBUTION.ManifestError, "absolute"):
                DISTRIBUTION.build_manifest(root)

    def test_canonical_source_manifest_covers_every_release_scope_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            wrapper = self._digest_repository(repo)
            baseline = RUNNER.source_manifest(wrapper)
            scoped_sources = (
                repo / "README.md",
                repo / "claude" / "claude-source.txt",
                repo / "codex" / "wrapper-source.txt",
                repo / "cursor" / "cursor-source.txt",
                repo / "docs" / "release.md",
                repo / "tests" / "release_test.py",
            )
            for source in scoped_sources:
                with self.subTest(source=source.relative_to(repo).as_posix()):
                    original = source.read_bytes()
                    source.write_bytes(original + b"changed\n")
                    changed = RUNNER.source_manifest(wrapper)
                    self.assertNotEqual(baseline["digest"], changed["digest"])
                    source.write_bytes(original)

            results = wrapper / "verification-results.json"
            report = wrapper / "docs" / "verification-report.md"
            report.parent.mkdir(exist_ok=True)
            results.write_text("generated evidence\n")
            report.write_text("generated report\n")
            evidence_only = RUNNER.source_manifest(wrapper)

        self.assertEqual(baseline["digest"], evidence_only["digest"])

    def test_canonical_source_manifest_fails_closed_on_missing_or_unsafe_scope_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            wrapper = self._digest_repository(repo)
            (repo / "README.md").unlink()
            with self.assertRaisesRegex(
                (RUNNER.VerificationError, RUNNER.ManifestError), "required"
            ):
                RUNNER.source_manifest(wrapper)

        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            wrapper = self._digest_repository(repo)
            outside = repo.parent / f"{repo.name}-outside-source"
            outside.write_text("outside\n")
            try:
                (repo / "claude" / "escape").symlink_to(outside)
                with self.assertRaisesRegex(RUNNER.ManifestError, "absolute|escape"):
                    RUNNER.source_manifest(wrapper)
            finally:
                outside.unlink(missing_ok=True)

    def test_authenticated_standalone_source_manifest_remains_wrapper_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrapper = pathlib.Path(tmp)
            runner = self._copy_scope_runner(wrapper)
            (wrapper / "payload.txt").write_text("standalone\n")
            subprocess.run(["git", "init", "-q"], cwd=wrapper, check=True)
            standalone_runner = load_module(
                f"nightfalcon_standalone_{time.time_ns()}", runner
            )

            actual = standalone_runner.source_manifest(wrapper)
            expected = DISTRIBUTION.build_manifest(
                wrapper,
                exclude_paths=(
                    "verification-results.json",
                    "docs/verification-report.md",
                ),
            )

        self.assertEqual(actual["digest"], expected["digest"])
        self.assertEqual(actual["file_count"], expected["file_count"])

    def test_python_compile_redirects_bytecode_outside_distribution(self):
        with tempfile.TemporaryDirectory() as wrapper_tmp, tempfile.TemporaryDirectory() as run_tmp:
            wrapper = pathlib.Path(wrapper_tmp)
            plugin = wrapper / "plugins" / "nightfalcon"
            (plugin / "scripts").mkdir(parents=True)
            (plugin / "hooks").mkdir()
            (plugin / "scripts" / "sample.py").write_text("VALUE = 1\n")
            (plugin / "hooks" / "hook.py").write_text("VALUE = 2\n")
            snapshot = pathlib.Path(run_tmp) / "migration-map.before.json"
            snapshot.write_text("{}\n")
            commands = RUNNER.build_core_commands(
                wrapper=wrapper,
                python_bin=sys.executable,
                yaml_python=sys.executable,
                plugin_validator=pathlib.Path("/validator/plugin.py"),
                skill_validator=pathlib.Path("/validator/skill.py"),
                inventory_snapshot=snapshot,
            )
            compile_command = next(
                command for command in commands if command.name == "python-compile"
            )

            self.assertIn("PYTHONPYCACHEPREFIX", compile_command.env)
            cache_prefix = pathlib.Path(compile_command.env["PYTHONPYCACHEPREFIX"])
            self.assertFalse(cache_prefix.is_relative_to(wrapper))
            result = RUNNER.execute_commands([compile_command])[0]

            self.assertTrue(result["passed"])
            self.assertEqual(list(plugin.rglob("__pycache__")), [])
            self.assertTrue(any(cache_prefix.rglob("*.pyc")))

    def test_runner_local_import_does_not_create_distribution_bytecode(self):
        with tempfile.TemporaryDirectory() as tmp:
            scripts = pathlib.Path(tmp) / "scripts"
            scripts.mkdir()
            shutil.copy2(RUNNER_PATH, scripts / RUNNER_PATH.name)
            shutil.copy2(DISTRIBUTION_PATH, scripts / DISTRIBUTION_PATH.name)
            environment = os.environ.copy()
            environment.pop("PYTHONDONTWRITEBYTECODE", None)
            environment.pop("PYTHONPYCACHEPREFIX", None)

            result = subprocess.run(
                [sys.executable, str(scripts / RUNNER_PATH.name), "--help"],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            caches = list(scripts.rglob("__pycache__")) + list(scripts.rglob("*.pyc"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(caches, [])

    def test_arbitrary_real_codex_home_cannot_be_relabelled_as_runner_temporary(self):
        self.assertTrue(
            hasattr(RUNNER, "validate_runner_temp_home"),
            "verification driver has no runner-owned temp-home validator",
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp).resolve()
            runner_root = base / "nightfalcon.fixture"
            runner_root.mkdir(mode=0o700)
            (runner_root / ".nightfalcon-verification-root").write_text(
                "nightfalcon-verify-port-v1\n"
            )
            real_home = base / "real-codex-home"
            real_home.mkdir()

            with self.assertRaisesRegex(RUNNER.VerificationError, "exact child"):
                RUNNER.validate_runner_temp_home(runner_root, real_home)

            temporary_home = runner_root / "codex-home"
            temporary_home.mkdir(mode=0o700)
            validated = RUNNER.validate_runner_temp_home(runner_root, temporary_home)

        self.assertEqual(validated.name, "codex-home")

    def test_compile_matrix_is_stable_across_independent_run_temp_directories(self):
        digests = []
        recorded_prefixes = []
        for label in ("first", "second"):
            with tempfile.TemporaryDirectory(prefix=f"wrapper-{label}-") as wrapper_tmp, tempfile.TemporaryDirectory(
                prefix=f"run-{label}-"
            ) as run_tmp:
                wrapper = pathlib.Path(wrapper_tmp)
                plugin = wrapper / "plugins" / "nightfalcon"
                (plugin / "scripts").mkdir(parents=True)
                (plugin / "hooks").mkdir()
                (plugin / "scripts" / "sample.py").write_text("VALUE = 1\n")
                (plugin / "hooks" / "hook.py").write_text("VALUE = 2\n")
                snapshot = pathlib.Path(run_tmp) / "migration-map.before.json"
                snapshot.write_text("{}\n")
                commands = RUNNER.build_core_commands(
                    wrapper=wrapper,
                    python_bin=sys.executable,
                    yaml_python=sys.executable,
                    plugin_validator=pathlib.Path("/validator/plugin.py"),
                    skill_validator=pathlib.Path("/validator/skill.py"),
                    inventory_snapshot=snapshot,
                )
                compile_command = next(
                    command for command in commands if command.name == "python-compile"
                )
                result = RUNNER.execute_commands(
                    [compile_command],
                    replacements={
                        str(snapshot): "<INVENTORY_SNAPSHOT>",
                        str(snapshot.parent): "<RUN_TEMP>",
                        str(wrapper): "<WRAPPER>",
                        sys.executable: "<PYTHON>",
                    },
                )[0]
                digests.append(RUNNER.matrix_digest([result]))
                recorded_prefixes.append(result["env"]["PYTHONPYCACHEPREFIX"])

        self.assertEqual(recorded_prefixes, ["<RUN_TEMP>/python-bytecode"] * 2)
        self.assertEqual(digests[0], digests[1])

    def test_codex_smoke_plan_uses_exact_isolated_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = pathlib.Path(tmp)
            commands = RUNNER.build_codex_smoke_commands(
                ROOT, codex_home, codex_bin="codex"
            )
        self.assertEqual(
            [command.args[1:] for command in commands],
            [
                ("plugin", "marketplace", "add", str(ROOT), "--json"),
                ("plugin", "add", "nightfalcon@nightfalcon-open", "--json"),
                ("plugin", "list", "--json"),
                ("plugin", "remove", "nightfalcon@nightfalcon-open", "--json"),
                ("plugin", "marketplace", "remove", "nightfalcon-open"),
            ],
        )
        self.assertTrue(
            all(command.env["CODEX_HOME"] == str(codex_home) for command in commands)
        )

    def test_report_is_deterministic_and_preserves_claims_boundary(self):
        payload = {
            "schema_version": 1,
            "known_limitations": [
                "NOT-LIVE-TESTED: Full live phase-0 through phase-8 execution."
            ],
            "runs": [
                {
                    "run_id": "fixed-run",
                    "timestamp_utc": "2026-07-14T12:00:00Z",
                    "status": "PASS",
                    "commands": [
                        {
                            "name": "wrapper-tests",
                            "command": ["python3", "-m", "unittest"],
                            "cwd": str(ROOT),
                            "exit_code": 0,
                            "passed": True,
                            "stdout_bytes": 3,
                            "stdout_sha256": "a" * 64,
                            "stderr_bytes": 0,
                            "stderr_sha256": "b" * 64,
                        }
                    ],
                }
            ],
        }
        first = WRITER.render_report(payload)
        second = WRITER.render_report(payload)
        self.assertEqual(first, second)
        self.assertIn("fixed-run", first)
        self.assertIn("NOT-LIVE-TESTED", first)
        self.assertIn("| wrapper-tests |", first)

    def test_build_run_record_binds_source_matrix_and_command_inventory(self):
        self.assertTrue(hasattr(RUNNER, "build_run_record"))
        source = {"digest": "a" * 64, "file_count": 42}
        commands = self._command_results(CORE_COMMAND_NAMES)
        run = RUNNER.build_run_record(
            run_id="bound-run",
            timestamp="2026-07-17T12:00:00Z",
            source_manifest=source,
            matrix_kind="core-only",
            command_results=commands,
        )
        self.assertEqual(run["source_digest"], "a" * 64)
        self.assertEqual(run["source_file_count"], 42)
        self.assertEqual(run["matrix_kind"], "core-only")
        self.assertEqual(run["matrix_digest"], self._matrix_digest(commands))
        self.assertEqual(run["command_names"], list(CORE_COMMAND_NAMES))
        self.assertEqual(run["status"], "PASS")

    def test_two_clean_run_gate_accepts_only_current_complete_bound_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrapper = self._digest_repository(pathlib.Path(tmp))
            results_path = wrapper / "verification-results.json"
            runs = [
                self._scope_bound_run(wrapper, "run-one"),
                self._scope_bound_run(wrapper, "run-two"),
            ]
            results_path.write_text(json.dumps({"schema_version": 2, "runs": runs}))

            self.assertEqual(RUNNER.check_two_clean_runs(results_path), 0)

    def test_two_clean_run_gate_rejects_sibling_edit_between_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            wrapper = self._digest_repository(repo)
            first = self._scope_bound_run(wrapper, "run-one")
            sibling = repo / "claude" / "claude-source.txt"
            sibling.write_text("changed between runs\n")
            second = self._scope_bound_run(wrapper, "run-two")
            self.assertNotEqual(first["source_digest"], second["source_digest"])
            results_path = wrapper / "verification-results.json"
            results_path.write_text(
                json.dumps({"schema_version": 2, "runs": [first, second]})
            )

            self.assertEqual(RUNNER.check_two_clean_runs(results_path), 2)

    def test_two_clean_run_gate_rejects_sibling_edit_after_runs_as_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            wrapper = self._digest_repository(repo)
            runs = [
                self._scope_bound_run(wrapper, "run-one"),
                self._scope_bound_run(wrapper, "run-two"),
            ]
            results_path = wrapper / "verification-results.json"
            results_path.write_text(json.dumps({"schema_version": 2, "runs": runs}))
            (repo / "docs" / "release.md").write_text("changed after runs\n")
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                result = RUNNER.check_two_clean_runs(results_path)

        self.assertEqual(result, 2)
        self.assertIn("stale", stderr.getvalue().lower())

    def test_two_clean_run_gate_rejects_tampered_mismatched_stale_or_incomplete_passes(self):
        cases = (
            "status-only",
            "source-mismatch",
            "matrix-mismatch",
            "inventory-mismatch",
            "missing-core",
            "failed-command",
            "tampered-command",
            "tampered-env",
            "unsafe-install-home",
            "legacy-temp-label",
            "stale-source",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                wrapper = self._digest_repository(pathlib.Path(tmp))
                payload_file = wrapper / "wrapper-source.txt"
                runs = [
                    self._scope_bound_run(wrapper, "run-one"),
                    self._scope_bound_run(wrapper, "run-two"),
                ]
                if case == "status-only":
                    runs = [
                        {"run_id": "run-one", "status": "PASS", "commands": []},
                        {"run_id": "run-two", "status": "PASS", "commands": []},
                    ]
                elif case == "source-mismatch":
                    runs[1]["source_digest"] = "b" * 64
                elif case == "matrix-mismatch":
                    runs[1]["matrix_digest"] = "c" * 64
                elif case == "inventory-mismatch":
                    runs[1]["command_names"] = list(reversed(CORE_COMMAND_NAMES))
                elif case == "missing-core":
                    names = CORE_COMMAND_NAMES[:-1]
                    runs[1] = self._scope_bound_run(wrapper, "run-two", names)
                    runs[0] = self._scope_bound_run(wrapper, "run-one", names)
                elif case == "failed-command":
                    runs[1]["commands"][0]["passed"] = False
                    runs[1]["commands"][0]["exit_code"] = 9
                elif case == "tampered-command":
                    runs[1]["commands"][0]["command"].append("--tampered")
                elif case == "tampered-env":
                    runs[1]["commands"][0]["env"]["CODEX_HOME"] = "<HOME>/.codex"
                elif case == "unsafe-install-home":
                    names = CORE_COMMAND_NAMES + INSTALL_COMMAND_NAMES
                    runs = [
                        self._scope_bound_run(wrapper, "run-one", names),
                        self._scope_bound_run(wrapper, "run-two", names),
                    ]
                    for run in runs:
                        for command in run["commands"]:
                            if command["name"] in INSTALL_COMMAND_NAMES:
                                command["env"]["CODEX_HOME"] = "<HOME>/.codex"
                        run["matrix_digest"] = self._matrix_digest(run["commands"])
                elif case == "legacy-temp-label":
                    names = CORE_COMMAND_NAMES + INSTALL_COMMAND_NAMES
                    runs = [
                        self._scope_bound_run(wrapper, "run-one", names),
                        self._scope_bound_run(wrapper, "run-two", names),
                    ]
                    for run in runs:
                        for command in run["commands"]:
                            if command["name"] in INSTALL_COMMAND_NAMES:
                                command["env"]["CODEX_HOME"] = "<TEMP_CODEX_HOME>"
                        run["matrix_digest"] = self._matrix_digest(run["commands"])
                elif case == "stale-source":
                    payload_file.write_text("changed after verification\n")

                results_path = wrapper / "verification-results.json"
                results_path.write_text(
                    json.dumps({"schema_version": 2, "runs": runs})
                )

                self.assertEqual(RUNNER.check_two_clean_runs(results_path), 2)

    def test_report_install_claims_follow_retained_command_matrix(self):
        core_run = {
            "run_id": "core",
            "timestamp_utc": "2026-07-17T12:00:00Z",
            "status": "PASS",
            "matrix_kind": "core-only",
            "commands": self._command_results(CORE_COMMAND_NAMES),
        }
        full_run = {
            **core_run,
            "run_id": "full",
            "matrix_kind": "core+isolated-install",
            "commands": self._command_results(
                CORE_COMMAND_NAMES + INSTALL_COMMAND_NAMES
            ),
        }
        partial_run = {
            **core_run,
            "run_id": "partial",
            "commands": self._command_results(
                CORE_COMMAND_NAMES + INSTALL_COMMAND_NAMES[:2]
            ),
        }
        unsafe_run = {
            **full_run,
            "run_id": "unsafe",
            "commands": self._command_results(
                CORE_COMMAND_NAMES + INSTALL_COMMAND_NAMES
            ),
        }
        for command in unsafe_run["commands"]:
            if command["name"] in INSTALL_COMMAND_NAMES:
                command["env"]["CODEX_HOME"] = "<HOME>/.codex"

        core_report = WRITER.render_report({"schema_version": 2, "runs": [core_run]})
        full_report = WRITER.render_report({"schema_version": 2, "runs": [full_run]})
        partial_report = WRITER.render_report(
            {"schema_version": 2, "runs": [partial_run]}
        )
        unsafe_report = WRITER.render_report(
            {"schema_version": 2, "runs": [unsafe_run]}
        )

        self.assertIn("Isolated install coverage: `NOT RUN`", core_report)
        self.assertNotIn("and isolated-install checks", core_report)
        self.assertIn("Isolated install coverage: `PASS`", full_report)
        self.assertIn("Isolated install coverage: `INCOMPLETE`", partial_report)
        self.assertIn("Isolated install coverage: `FAIL`", unsafe_report)

    def test_missing_migration_map_atomically_replaces_stale_pass_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrapper = self._digest_repository(pathlib.Path(tmp))
            (wrapper / "scripts").mkdir(parents=True)
            (wrapper / "docs").mkdir()
            shutil.copy2(WRITER_PATH, wrapper / "scripts" / WRITER_PATH.name)
            stale = {
                "schema_version": 1,
                "known_limitations": [],
                "runs": [{"run_id": "stale-pass", "status": "PASS", "commands": []}],
            }
            (wrapper / "verification-results.json").write_text(json.dumps(stale))
            (wrapper / "docs" / "verification-report.md").write_text("STALE PASS REPORT\n")
            result = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER_PATH),
                    "--wrapper",
                    str(wrapper),
                    "--skip-codex-install",
                ],
                env=self._validator_environment(),
                text=True,
                capture_output=True,
                check=False,
            )
            payload = json.loads((wrapper / "verification-results.json").read_text())
            report = (wrapper / "docs" / "verification-report.md").read_text()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([run["status"] for run in payload["runs"]], ["FAIL"])
        self.assertEqual(payload["runs"][-1]["commands"][0]["name"], "verification-setup")
        self.assertIn("Latest status: `FAIL`", report)
        self.assertNotIn("STALE PASS REPORT", report)

    def test_report_writer_failure_is_recorded_and_gets_fallback_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrapper = self._digest_repository(pathlib.Path(tmp))
            scripts = wrapper / "scripts"
            scripts.mkdir(parents=True)
            (wrapper / "docs").mkdir()
            (scripts / WRITER_PATH.name).write_text("raise SystemExit(9)\n")
            stale = {
                "schema_version": 1,
                "known_limitations": [],
                "runs": [{"run_id": "stale-pass", "status": "PASS", "commands": []}],
            }
            (wrapper / "verification-results.json").write_text(json.dumps(stale))
            (wrapper / "docs" / "verification-report.md").write_text("STALE PASS REPORT\n")
            isolated_home = pathlib.Path(tmp) / "home"
            isolated_home.mkdir()
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(isolated_home),
                    "CODEX_HOME": str(isolated_home / "codex"),
                    "PLUGIN_CREATOR": str(isolated_home / "missing-plugin"),
                    "SKILL_CREATOR": str(isolated_home / "missing-skill"),
                    "PYTHON_BIN": sys.executable,
                    "YAML_PYTHON": self._validator_environment()["YAML_PYTHON"],
                }
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER_PATH),
                    "--wrapper",
                    str(wrapper),
                    "--skip-codex-install",
                    "--append-run",
                ],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            payload = json.loads((wrapper / "verification-results.json").read_text())
            report = (wrapper / "docs" / "verification-report.md").read_text()
        self.assertNotEqual(result.returncode, 0)
        latest = payload["runs"][-1]
        self.assertEqual([run["status"] for run in payload["runs"]], ["PASS", "FAIL"])
        self.assertEqual(latest["status"], "FAIL")
        self.assertEqual(latest["commands"][-1]["name"], "report-generation")
        self.assertFalse(latest["commands"][-1]["passed"])
        self.assertIn("Report generation: `FAIL`", report)
        self.assertNotIn("STALE PASS REPORT", report)

    def test_generated_evidence_contains_no_machine_or_credential_paths(self):
        results = ROOT / "verification-results.json"
        report = ROOT / "docs" / "verification-report.md"
        if not results.is_file() or not report.is_file():
            self.skipTest("generated verification evidence is not distributed")
        artifacts = "\n".join(
            [
                results.read_text(),
                report.read_text(),
            ]
        )
        self.assertNotIn(str(pathlib.Path.home()), artifacts)
        self.assertNotIn("/opt/homebrew", artifacts)
        self.assertIsNone(re.search(r"/(?:private/)?(?:var/folders|tmp)/", artifacts))
        self.assertIsNone(
            re.search(
                r"(?i)(?:TOKEN|PASSWORD|SECRET|API_KEY)=[^<\s]", artifacts
            )
        )

    def test_wrapper_help_works_from_parent_and_standalone_layouts(self):
        for cwd in (SOURCE, ROOT):
            result = subprocess.run(
                ["bash", str(VERIFY_PATH), "--help"],
                cwd=cwd,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--skip-codex-install", result.stdout)
            self.assertIn("bound PASS", result.stdout)

    def test_standalone_wrapper_root_verifier_uses_only_provisioned_validators(self):
        git_root = pathlib.Path(
            subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=ROOT,
                text=True,
            ).strip()
        ).resolve()
        if git_root == ROOT.resolve():
            self.skipTest("already executing inside the standalone verifier fixture")

        yaml_python = RUNNER.find_yaml_python(["python3.11", sys.executable])
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            fixture = base / "codex"
            shutil.copytree(
                ROOT,
                fixture,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            subprocess.run(["git", "init", "-q"], cwd=fixture, check=True)
            subprocess.run(["git", "add", "."], cwd=fixture, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=NightFalcon Test",
                    "-c",
                    "user.email=nightfalcon@example.invalid",
                    "commit",
                    "-qm",
                    "standalone fixture",
                ],
                cwd=fixture,
                check=True,
            )
            isolated_home = base / "home"
            isolated_codex = base / "codex-home"
            isolated_home.mkdir()
            environment = {
                "PATH": os.environ["PATH"],
                "HOME": str(isolated_home),
                "CODEX_HOME": str(isolated_codex),
                "LANG": "C.UTF-8",
                "PYTHON_BIN": yaml_python,
                "YAML_PYTHON": yaml_python,
                "PLUGIN_CREATOR": str(
                    fixture / "scripts" / "validators" / "validate_plugin.py"
                ),
                "SKILL_CREATOR": str(
                    fixture / "scripts" / "validators" / "quick_validate.py"
                ),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            result = subprocess.run(
                [
                    "bash",
                    str(fixture / "scripts" / "verify-port.sh"),
                    "--skip-codex-install",
                ],
                cwd=fixture,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            evidence = json.loads((fixture / "verification-results.json").read_text())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(evidence["runs"][-1]["status"], "PASS")
        self.assertIn("Plugin validation passed", result.stdout)
        self.assertIn("Skill is valid!", result.stdout)
        self.assertNotIn(str(pathlib.Path.home()), json.dumps(evidence))

    def test_shell_uses_cleanup_trap_for_temporary_codex_home(self):
        text = VERIFY_PATH.read_text()
        self.assertIn("trap cleanup EXIT", text)
        self.assertIn("terminate_runner TERM 143", text)
        self.assertIn('wait "$RUNNER_PID"', text)
        self.assertIn("mktemp -d", text)
        self.assertIn("CODEX_HOME", text)

    def test_shell_creates_owned_codex_home_inside_private_runner_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp).resolve()
            wrapper = base / "codex"
            scripts = wrapper / "scripts"
            temp_parent = base / "tmp"
            scripts.mkdir(parents=True)
            temp_parent.mkdir()
            shutil.copy2(VERIFY_PATH, scripts / "verify-port.sh")
            fake_bin = base / "bin"
            fake_bin.mkdir()
            fake_ps = fake_bin / "ps"
            fake_ps.write_text("#!/usr/bin/env bash\nprintf '424242\\n'\n")
            fake_ps.chmod(0o755)
            state_path = base / "runner-state.json"
            (scripts / "run-verification.py").write_text(
                "import json, os, pathlib, stat, sys\n"
                "root = pathlib.Path(sys.argv[sys.argv.index('--runner-temp-root') + 1])\n"
                "home = pathlib.Path(sys.argv[sys.argv.index('--temp-codex-home') + 1])\n"
                "state = {\n"
                "  'root_is_dir': root.is_dir() and not root.is_symlink(),\n"
                "  'home_is_dir': home.is_dir() and not home.is_symlink(),\n"
                "  'exact_child': home == root / 'codex-home',\n"
                "  'marker': (root / '.nightfalcon-verification-root').read_text(),\n"
                "  'root_mode': stat.S_IMODE(root.stat().st_mode),\n"
                "  'root_owner': root.stat().st_uid == os.getuid(),\n"
                "  'home_owner': home.stat().st_uid == os.getuid(),\n"
                "}\n"
                "pathlib.Path(os.environ['RUNNER_STATE']).write_text(json.dumps(state))\n"
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "PYTHON_BIN": sys.executable,
                    "RUNNER_STATE": str(state_path),
                    "TMPDIR": str(temp_parent),
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                }
            )

            result = subprocess.run(
                ["bash", str(scripts / "verify-port.sh")],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = json.loads(state_path.read_text())
            leftovers = list(temp_parent.glob("nightfalcon.*"))

        self.assertTrue(state["root_is_dir"])
        self.assertTrue(state["home_is_dir"])
        self.assertTrue(state["exact_child"])
        self.assertEqual(state["marker"], "nightfalcon-verify-port-v1\n")
        self.assertEqual(state["root_mode"], 0o700)
        self.assertTrue(state["root_owner"])
        self.assertTrue(state["home_owner"])
        self.assertEqual(leftovers, [])

    def test_shell_process_group_launcher_resolves_relative_python_from_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrapper = pathlib.Path(tmp) / "codex"
            scripts = wrapper / "scripts"
            scripts.mkdir(parents=True)
            shutil.copy2(VERIFY_PATH, scripts / "verify-port.sh")
            (scripts / "run-verification.py").write_text("raise SystemExit(0)\n")
            environment = os.environ.copy()
            environment.pop("PYTHON_BIN", None)
            result = subprocess.run(
                ["bash", str(scripts / "verify-port.sh"), "--skip-codex-install"],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_shell_terminates_runner_and_cleans_temp_home_on_term(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            wrapper = base / "codex"
            scripts = wrapper / "scripts"
            temp_root = base / "tmp"
            scripts.mkdir(parents=True)
            temp_root.mkdir()
            shutil.copy2(VERIFY_PATH, scripts / "verify-port.sh")
            pid_file = base / "runner.pid"
            (scripts / "run-verification.py").write_text(
                "import os, pathlib, time\n"
                "pathlib.Path(os.environ['RUNNER_PID_FILE']).write_text(str(os.getpid()))\n"
                "time.sleep(60)\n"
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "PYTHON_BIN": sys.executable,
                    "RUNNER_PID_FILE": str(pid_file),
                    "TMPDIR": str(temp_root),
                }
            )
            process = subprocess.Popen(
                ["bash", str(scripts / "verify-port.sh")],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            deadline = time.monotonic() + 5
            while not pid_file.is_file() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pid_file.is_file(), "fake runner did not start")
            runner_pid = int(pid_file.read_text())
            started = time.monotonic()
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                self.fail("verification shell did not exit promptly after SIGTERM")
            elapsed = time.monotonic() - started
            with self.assertRaises(ProcessLookupError):
                os.kill(runner_pid, 0)
            leftovers = list(temp_root.glob("nightfalcon.*"))
        self.assertNotEqual(process.returncode, 0)
        self.assertLess(elapsed, 5)
        self.assertEqual(leftovers, [])

    def test_shell_terminates_runner_process_group_before_cleaning_temp_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            wrapper = base / "codex"
            scripts = wrapper / "scripts"
            temp_root = base / "tmp"
            scripts.mkdir(parents=True)
            temp_root.mkdir()
            shutil.copy2(VERIFY_PATH, scripts / "verify-port.sh")
            state_file = base / "descendants.json"
            (scripts / "run-verification.py").write_text(
                "import json, os, pathlib, subprocess, sys, time\n"
                "home = pathlib.Path(sys.argv[sys.argv.index('--temp-codex-home') + 1])\n"
                "code = (\"import pathlib, sys, time; time.sleep(0.8); \"\n"
                "        \"p=pathlib.Path(sys.argv[1]); p.mkdir(parents=True, exist_ok=True); \"\n"
                "        \"(p/'recreated').write_text('descendant survived')\")\n"
                "child = subprocess.Popen([sys.executable, '-c', code, str(home)])\n"
                "pathlib.Path(os.environ['DESCENDANT_STATE']).write_text(json.dumps({\n"
                "    'runner': os.getpid(), 'runner_pgid': os.getpgrp(),\n"
                "    'grandchild': child.pid, 'home': str(home)}))\n"
                "time.sleep(60)\n"
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "PYTHON_BIN": sys.executable,
                    "DESCENDANT_STATE": str(state_file),
                    "TMPDIR": str(temp_root),
                }
            )
            process = subprocess.Popen(
                ["bash", str(scripts / "verify-port.sh")],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            descendant = None
            try:
                deadline = time.monotonic() + 5
                while not state_file.is_file() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(state_file.is_file(), "fake runner did not spawn its grandchild")
                descendant = json.loads(state_file.read_text())
                temp_home = pathlib.Path(descendant["home"])
                self.assertEqual(descendant["runner_pgid"], descendant["runner"])
                self.assertNotEqual(descendant["runner_pgid"], os.getpgid(process.pid))
                self.assertEqual(os.getpgid(descendant["grandchild"]), descendant["runner_pgid"])
                process.terminate()
                process.communicate(timeout=5)
                time.sleep(1.1)
                for key in ("runner", "grandchild"):
                    with self.assertRaises(ProcessLookupError, msg=f"{key} survived SIGTERM"):
                        os.kill(descendant[key], 0)
                self.assertFalse(temp_home.exists(), "a descendant recreated the temporary home")
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
                if descendant is not None:
                    for key in ("runner", "grandchild"):
                        try:
                            os.kill(descendant[key], signal.SIGKILL)
                        except ProcessLookupError:
                            pass
        self.assertEqual(process.returncode, 143)


if __name__ == "__main__":
    unittest.main()
