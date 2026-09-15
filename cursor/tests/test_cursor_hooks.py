"""Tests for the Cursor-specific hook layer (.cursor/hooks/*.sh).

These have no analogue in the Claude/Codex ports: Cursor hooks use JSON on
stdin/stdout (not exit-2 blocking). We verify:
  - gate-guard.sh: allows legit gate calls, denies state.json tampering +
    artifact deletion, and is a no-op outside a marked NightFalcon workspace.
  - edit-log.sh: returns {} always; logs an advisory line only for out-of-scope
    edits (it can never block).
  - stop-enforce.sh: returns followup_message when the current phase output is
    missing, {} when present, and {} once the loop cap is hit.
Run: python3 -m pytest tests/  (or python3 -m unittest discover -s tests)
"""
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HOOKS = ROOT / ".cursor" / "hooks"
DATE = "2026-08-21"


def run_hook(script: str, payload: dict, *, active=True, workspace=None):
    env = dict(os.environ)
    env.pop("SECURITY_REVIEW_WORKSPACE", None)
    synthetic = None
    if active:
        env["NIGHTFALCON_CURSOR_ACTIVE"] = "1"
        if workspace is None:
            synthetic = tempfile.TemporaryDirectory()
            workspace = pathlib.Path(synthetic.name)
            (workspace / "state.json").write_text(json.dumps(
                {"date": DATE, "current_phase": "phase-1", "repo_slugs": ["demo"]}
            ))
            (workspace / ".nightfalcon-review").write_text("nightfalcon\n")
            (workspace / "findings" / "demo").mkdir(parents=True)
            (workspace / "output").mkdir()
    else:
        env.pop("NIGHTFALCON_CURSOR_ACTIVE", None)
    if workspace and active:
        env["SECURITY_REVIEW_WORKSPACE"] = str(workspace)
    p = subprocess.run(
        ["bash", str(HOOKS / script)],
        input=json.dumps(payload),
        text=True, capture_output=True, env=env, check=False,
    )
    out = (p.stdout or "").strip()
    if synthetic is not None:
        synthetic.cleanup()
    return json.loads(out) if out else {}


def run_hook_without_activation_env(script: str, payload: dict, *, cwd):
    env = dict(os.environ)
    env.pop("NIGHTFALCON_CURSOR_ACTIVE", None)
    env.pop("SECURITY_REVIEW_WORKSPACE", None)
    p = subprocess.run(
        ["bash", str(HOOKS / script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        cwd=cwd,
        check=False,
    )
    out = (p.stdout or "").strip()
    return json.loads(out) if out else {}


def make_ws(base, current_phase, slugs=("demo",)):
    ws = base / "workspace"
    (ws / "findings" / "demo").mkdir(parents=True, exist_ok=True)
    (ws / "output").mkdir(parents=True, exist_ok=True)
    (ws / "state.json").write_text(json.dumps(
        {"date": DATE, "current_phase": current_phase, "repo_slugs": list(slugs)}
    ))
    (ws / ".nightfalcon-review").write_text("nightfalcon\n")
    return ws


class GateGuardTests(unittest.TestCase):
    def test_unrelated_state_json_without_marker_keeps_all_hooks_inert(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp)
            (workspace / "state.json").write_text(
                json.dumps({"date": DATE, "current_phase": "phase-1", "repo_slugs": ["demo"]})
            )
            (workspace / "output").mkdir()
            gate = run_hook_without_activation_env(
                "gate-guard.sh",
                {"command": 'sed -i "s/phase-1/done/" state.json', "cwd": str(workspace)},
                cwd=workspace,
            )
            edit = run_hook_without_activation_env(
                "edit-log.sh",
                {"file_path": str(workspace / "outside.txt"), "cwd": str(workspace)},
                cwd=workspace,
            )
            stop = run_hook_without_activation_env(
                "stop-enforce.sh",
                {"status": "completed", "loop_count": 0, "cwd": str(workspace)},
                cwd=workspace,
            )
            self.assertEqual(gate.get("permission"), "allow")
            self.assertEqual(edit, {})
            self.assertEqual(stop, {})
            self.assertFalse((workspace / f"output/run-log-{DATE}.md").exists())

    def test_activates_from_cwd_state_marker_without_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_ws(pathlib.Path(tmp), "phase-3")
            result = run_hook_without_activation_env(
                "gate-guard.sh",
                {"command": 'sed -i "s/phase-3/done/" state.json', "cwd": str(workspace)},
                cwd=workspace,
            )
            self.assertEqual(result.get("permission"), "deny")

    def test_all_hooks_resolve_marked_workspace_from_nested_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_ws(pathlib.Path(tmp), "phase-3")
            nested = workspace / "sourcecode" / "demo" / "nested"
            nested.mkdir(parents=True)
            gate = run_hook_without_activation_env(
                "gate-guard.sh",
                {"command": 'sed -i "s/phase-3/done/" ../../../state.json', "cwd": str(nested)},
                cwd=nested,
            )
            edit = run_hook_without_activation_env(
                "edit-log.sh",
                {"file_path": str(workspace / "outside.txt"), "cwd": str(nested)},
                cwd=nested,
            )
            stop = run_hook_without_activation_env(
                "stop-enforce.sh",
                {"status": "completed", "loop_count": 0, "cwd": str(nested)},
                cwd=nested,
            )
            self.assertEqual(gate.get("permission"), "deny")
            self.assertEqual(edit, {})
            self.assertIn("followup_message", stop)
            self.assertTrue((workspace / f"output/run-log-{DATE}.md").is_file())

    def test_allows_legit_gate_invocation(self):
        r = run_hook("gate-guard.sh",
                     {"command": "bash scripts/check-gate.sh --next-phase phase-2 --workspace /x"})
        self.assertEqual(r.get("permission"), "allow")

    def test_denies_state_json_shell_write(self):
        r = run_hook("gate-guard.sh",
                     {"command": 'sed -i "s/phase-1/phase-8/" ./state.json'})
        self.assertEqual(r.get("permission"), "deny")
        self.assertIn("state.json", r.get("user_message", ""))

    def test_denies_artifact_deletion(self):
        r = run_hook("gate-guard.sh",
                     {"command": "rm -rf findings/demo/candidates-2026-08-21.md"})
        self.assertEqual(r.get("permission"), "deny")

    def test_denies_named_mutation_primitives_against_protected_paths(self):
        commands = (
            "cp /tmp/replacement state.json",
            f"mv /tmp/replacement findings/demo/candidates-{DATE}.md",
            f"truncate -s 0 output/executive-summary-{DATE}.md",
            f"dd if=/tmp/replacement of=output/proof_of_concept/demo/poc-manifest-{DATE}.json",
            "rm -rf findings",
            "cp -t findings /tmp/replacement",
            "cp --target-directory=findings /tmp/replacement",
            'sed --in-place "s/phase-1/phase-8/" state.json',
        )
        for command in commands:
            with self.subTest(command=command):
                result = run_hook("gate-guard.sh", {"command": command})
                self.assertEqual(result.get("permission"), "deny")

    def test_denies_language_interpreter_writes_to_protected_paths(self):
        commands = (
            "python3 -c \"from pathlib import Path; Path('state.json').write_text('x')\"",
            f"perl -e 'open my $fh, q(>), q(findings/demo/candidates-{DATE}.md)'",
            f"ruby -e \"File.write('output/executive-summary-{DATE}.md', 'x')\"",
            "perl -pi -e 's/phase-1/phase-8/' state.json",
            "python3 -c \"import sys; open(sys.argv[1], 'w').write('x')\" state.json",
        )
        for command in commands:
            with self.subTest(command=command):
                result = run_hook("gate-guard.sh", {"command": command})
                self.assertEqual(result.get("permission"), "deny")

    def test_denies_script_interpreters_with_unsafe_target_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            workspace = make_ws(base, "phase-6")
            commands = (
                "python3 /tmp/overwrite.py state.json",
                f"node /tmp/overwrite.js output/executive-report-{DATE}.json",
                f"command python -B /tmp/overwrite.py findings/demo/candidates-{DATE}.md",
                "env NODE_NO_WARNINGS=1 node --trace-warnings /tmp/overwrite.js docs/../output/replaced.json",
                "ruby -w /tmp/overwrite.rb ./state.json",
                f"perl -w /tmp/overwrite.pl findings/demo/../demo/candidates-{DATE}.md",
                "php -d display_errors=1 /tmp/overwrite.php --target=state.json",
                "php --file=/tmp/overwrite.php state.json",
                "bash -eu /tmp/overwrite.sh output",
                "sh /tmp/overwrite.sh $UNRESOLVED_TARGET",
                "zsh --no-rcs /tmp/overwrite.sh .",
                "dash /tmp/overwrite.sh docs/../findings",
                "ksh /tmp/overwrite.sh proof_of_concept",
                f"python3 /tmp/overwrite.py {workspace}",
                f"node /tmp/overwrite.js {base}",
            )
            for command in commands:
                with self.subTest(command=command):
                    result = run_hook(
                        "gate-guard.sh",
                        {"command": command, "cwd": str(workspace)},
                        workspace=workspace,
                    )
                    self.assertEqual(result.get("permission"), "deny")

            inactive = run_hook(
                "gate-guard.sh",
                {"command": "python3 /tmp/overwrite.py state.json"},
                active=False,
                workspace=workspace,
            )
            self.assertEqual(inactive.get("permission"), "allow")

    def test_script_interpreter_guard_preserves_safe_and_approved_invocations(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            workspace = make_ws(base, "phase-6")
            disjoint = base / "disjoint"
            disjoint.mkdir()
            approved = (
                f"bash {ROOT / 'scripts/check-gate.sh'} --next-phase phase-7 --workspace {workspace}",
                f"bash {ROOT / 'scripts/complete-phase.sh'} --phase phase-6 --workspace {workspace}",
                f"python3 {ROOT / 'scripts/validate-findings.py'} findings/demo/findings-{DATE}.json --mode security",
                f"python3 {ROOT / 'scripts/report/build.py'} --input output/executive-report-{DATE}.json --output output/executive-report-{DATE}.html",
                f"python3 scripts/report/build.py --input findings/demo/findings-{DATE}.json --output output/report.html",
            )
            safe = (
                "python3 --version",
                "node --help",
                "command ruby -v",
                "env LC_ALL=C perl --version",
                "php -h",
                "bash --version",
                f"python3 -B /tmp/read.py {disjoint / 'input.json'}",
                f"node --trace-warnings /tmp/read.js {disjoint / 'input.json'}",
                f"ruby -w /tmp/read.rb {disjoint / 'input.json'}",
                f"perl -w /tmp/read.pl {disjoint / 'input.json'}",
                f"php -d display_errors=1 /tmp/read.php {disjoint / 'input.json'}",
                f"bash -eu /tmp/read.sh {disjoint / 'input.json'}",
                "python3 -m unittest discover -s tests -p 'test_*.py'",
                "python3 -m json.tool state.json",
            )
            for command in approved + safe:
                with self.subTest(command=command):
                    result = run_hook(
                        "gate-guard.sh",
                        {"command": command, "cwd": str(workspace)},
                        workspace=workspace,
                    )
                    self.assertEqual(result.get("permission"), "allow")

    def test_allows_only_packaged_agent_journal_cli_and_blocks_direct_mutations(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_ws(pathlib.Path(tmp), "phase-8")
            journal = workspace / "output" / f"agent-conversation-{DATE}.md"
            journal.write_text("fixture\n")
            packaged = ROOT / "scripts" / "agent-journal.py"
            allowed = run_hook(
                "gate-guard.sh",
                {
                    "command": (
                        f"python3 {packaged} validate --workspace {workspace}"
                    ),
                    "cwd": str(workspace),
                },
                workspace=workspace,
            )
            self.assertEqual(allowed.get("permission"), "allow")

            commands = (
                f"python3 /tmp/agent-journal.py validate --workspace {workspace}",
                f"python3 {packaged} validate --workspace {workspace} > {journal}",
                f"target={journal}; python3 {packaged} validate --workspace {workspace} > \"$target\"",
                f"truncate -s 0 {journal}",
                f"sed -i 's/x/y/' {journal}",
                f"perl -pi -e 's/x/y/' {journal}",
                f"printf x > {journal}",
                f"d={workspace / 'output'}; f=\"$d/agent-conversation-{DATE}.md\"; printf x > \"$f\"",
                f"f=\"$SECURITY_REVIEW_WORKSPACE/output/agent-conversation-{DATE}.md\"; printf x > \"$f\"",
                f"op=truncate; \"$op\" -s 0 {journal}",
                f"tool=/tmp/agent-journal.py; \"$tool\" validate --workspace {workspace}",
                f"printf x > \"$UNRESOLVED_JOURNAL_TARGET\"",
                f"\"$UNRESOLVED_MUTATOR\" {journal}",
                "target=\"$UNRESOLVED_MUTATION_TARGET\"; truncate -s 0 \"$target\"",
            )
            for command in commands:
                with self.subTest(command=command):
                    result = run_hook(
                        "gate-guard.sh",
                        {"command": command, "cwd": str(workspace)},
                        workspace=workspace,
                    )
                    self.assertEqual(result.get("permission"), "deny")

            safe_commands = (
                "d=/tmp; f=\"$d/nightfalcon-safe\"; printf x > \"$f\"",
                "op=cat; \"$op\" state.json",
                "f=\"$UNRESOLVED_READ_TARGET\"; cat \"$f\"",
            )
            for command in safe_commands:
                with self.subTest(command=command):
                    result = run_hook(
                        "gate-guard.sh",
                        {"command": command, "cwd": str(workspace)},
                        workspace=workspace,
                    )
                    self.assertEqual(result.get("permission"), "allow")

    def test_exec_eval_and_unresolved_streams_cannot_bypass_journal_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_ws(pathlib.Path(tmp), "phase-8")
            journal = workspace / "output" / f"agent-conversation-{DATE}.md"
            journal.write_text("fixture\n")
            packaged = ROOT / "scripts" / "agent-journal.py"
            denied = (
                f"exec truncate -s 0 {journal}",
                f"exec /tmp/agent-journal.py validate --workspace {workspace}",
                f"exec python3 /tmp/agent-journal.py validate --workspace {workspace}",
                f"builtin exec truncate -s 0 {journal}",
                f"builtin exec /tmp/agent-journal.py validate --workspace {workspace}",
                f"builtin exec python3 /tmp/agent-journal.py validate --workspace {workspace}",
                f"eval 'truncate -s 0 {journal}'",
                f"builtin eval 'truncate -s 0 {journal}'",
                f"operation='truncate -s 0 {journal}'; eval \"$operation\"",
                "eval \"$UNRESOLVED_EVAL_COMMAND\"",
                "printf '%s\\0' \"$UNRESOLVED_MUTATION_TARGET\" | xargs -0 truncate -s 0",
            )
            for command in denied:
                with self.subTest(command=command):
                    result = run_hook(
                        "gate-guard.sh",
                        {"command": command, "cwd": str(workspace)},
                        workspace=workspace,
                    )
                    self.assertEqual(result.get("permission"), "deny")

            allowed = (
                f"exec python3 {packaged} validate --workspace {workspace}",
                "exec cat state.json",
                "builtin exec cat state.json",
                "eval 'cat state.json'",
                "builtin eval 'cat state.json'",
                "printf '%s\\0' /tmp/nightfalcon-safe | xargs -0 cat",
            )
            for command in allowed:
                with self.subTest(command=command):
                    result = run_hook(
                        "gate-guard.sh",
                        {"command": command, "cwd": str(workspace)},
                        workspace=workspace,
                    )
                    self.assertEqual(result.get("permission"), "allow")

    def test_eval_terminator_and_env_wrapped_xargs_sink_are_classified(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_ws(pathlib.Path(tmp) / "review workspace", "phase-8")
            journal = workspace / "output" / f"agent-conversation-{DATE}.md"
            journal.write_text("fixture\n")
            packaged = ROOT / "scripts" / "agent-journal.py"
            denied = (
                f"eval -- 'truncate -s 0 \"{journal}\"'",
                f"builtin eval -- 'truncate -s 0 \"{journal}\"'",
                "printf '%s\\0' \"$UNRESOLVED_MUTATION_TARGET\" | xargs -0 env truncate -s 0",
            )
            for command in denied:
                with self.subTest(command=command):
                    result = run_hook(
                        "gate-guard.sh",
                        {"command": command, "cwd": str(workspace)},
                        workspace=workspace,
                    )
                    self.assertEqual(result.get("permission"), "deny")

            allowed = (
                "eval -- 'cat state.json'",
                "builtin eval -- 'cat state.json'",
                "printf '%s\\0' /tmp/nightfalcon-safe | xargs -0 env cat",
                f"eval -- 'python3 \"{packaged}\" validate --workspace \"{workspace}\"'",
            )
            for command in allowed:
                with self.subTest(command=command):
                    result = run_hook(
                        "gate-guard.sh",
                        {"command": command, "cwd": str(workspace)},
                        workspace=workspace,
                    )
                    self.assertEqual(result.get("permission"), "allow")

    def test_env_operand_grammar_cannot_hide_xargs_or_eval_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_ws(pathlib.Path(tmp) / "env review workspace", "phase-8")
            journal = workspace / "output" / f"agent-conversation-{DATE}.md"
            journal.write_text("fixture\n")
            packaged = ROOT / "scripts" / "agent-journal.py"
            denied = (
                "printf '%s\\0' \"$UNRESOLVED_MUTATION_TARGET\" | "
                "xargs -0 env -- FOO=bar truncate -s 0",
                "printf '%s\\0' \"$UNRESOLVED_MUTATION_TARGET\" | "
                "xargs -0 env -S 'FOO=bar truncate -s 0'",
                "printf '%s\\0' \"$UNRESOLVED_MUTATION_TARGET\" | "
                "xargs -0 env 'x-y=1' truncate -s 0",
                f"eval -- 'env -- FOO=bar truncate -s 0 \"{journal}\"'",
            )
            for command in denied:
                with self.subTest(command=command):
                    result = run_hook(
                        "gate-guard.sh",
                        {"command": command, "cwd": str(workspace)},
                        workspace=workspace,
                    )
                    self.assertEqual(result.get("permission"), "deny")

            allowed = (
                "printf '%s\\0' /tmp/nightfalcon-safe | "
                "xargs -0 env -- FOO=bar cat",
                "printf '%s\\0' /tmp/nightfalcon-safe | "
                "xargs -0 env -S 'FOO=bar cat'",
                "printf '%s\\0' /tmp/nightfalcon-safe | "
                "xargs -0 env 'x-y=1' cat",
                f"eval -- 'env -- FOO=bar python3 \"{packaged}\" validate "
                f"--workspace \"{workspace}\"'",
            )
            for command in allowed:
                with self.subTest(command=command):
                    result = run_hook(
                        "gate-guard.sh",
                        {"command": command, "cwd": str(workspace)},
                        workspace=workspace,
                    )
                    self.assertEqual(result.get("permission"), "allow")

    def test_env_split_escape_and_read_only_short_options_are_classified(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_ws(pathlib.Path(tmp) / "env escape workspace", "phase-8")
            journal = workspace / "output" / f"agent-conversation-{DATE}.md"
            journal.write_text("fixture\n")
            command = (
                f"printf '%s\\0' \"{journal}\" | "
                "xargs -0 env -S 'truncate\\_-s\\_0'"
            )
            with self.subTest(command=command):
                result = run_hook(
                    "gate-guard.sh",
                    {"command": command, "cwd": str(workspace)},
                    workspace=workspace,
                )
                self.assertEqual(result.get("permission"), "deny")

            for command in ("env - cat state.json", "env -v cat state.json"):
                with self.subTest(command=command):
                    result = run_hook(
                        "gate-guard.sh",
                        {"command": command, "cwd": str(workspace)},
                        workspace=workspace,
                    )
                    self.assertEqual(result.get("permission"), "allow")

    def test_generated_env_split_mutator_cannot_bypass_protected_xargs_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_ws(
                pathlib.Path(tmp) / "command substitution workspace", "phase-8"
            )
            journal = workspace / "output" / f"agent-conversation-{DATE}.md"
            original = b"journal\n"
            journal.write_bytes(original)
            command = (
                f"printf '%s\\0' \"{journal}\" | "
                "xargs -0 env -S \"$(printf 'truncate\\_-s\\_0')\""
            )

            executed = subprocess.run(
                ["bash", "-c", command], text=True, capture_output=True, check=False
            )
            self.assertEqual(executed.returncode, 0, executed.stderr)
            self.assertEqual(journal.read_bytes(), b"")
            journal.write_bytes(original)

            result = run_hook(
                "gate-guard.sh",
                {"command": command, "cwd": str(workspace)},
                workspace=workspace,
            )
            self.assertEqual(result.get("permission"), "deny")
            self.assertEqual(journal.read_bytes(), original)

    def test_live_backtick_env_split_mutator_cannot_bypass_protected_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            workspace = make_ws(base / "backtick substitution workspace", "phase-8")
            journal = workspace / "output" / f"agent-conversation-{DATE}.md"
            original = b"journal\n"
            journal.write_bytes(original)
            command = (
                f"printf '%s\\0' \"{journal}\" | "
                "xargs -0 env -S \"`printf 'truncate -s 0'`\""
            )

            executed = subprocess.run(
                ["bash", "-c", command], text=True, capture_output=True, check=False
            )
            self.assertEqual(executed.returncode, 0, executed.stderr)
            self.assertEqual(journal.read_bytes(), b"")
            journal.write_bytes(original)

            safe_target = base / "safe-target"
            safe_target.write_bytes(original)
            allowed = (
                "printf '%s\\n' '`truncate state.json`'",
                "printf '%s\\n' \"\\`truncate state.json\\`\"",
                'cat "`printf state.json`"',
                (
                    f"printf '%s\\0' \"{safe_target}\" | "
                    "xargs -0 env -S \"`printf 'truncate -s 0'`\""
                ),
            )
            for safe_command in allowed:
                with self.subTest(command=safe_command):
                    safe_result = run_hook(
                        "gate-guard.sh",
                        {"command": safe_command, "cwd": str(workspace)},
                        workspace=workspace,
                    )
                    self.assertEqual(safe_result.get("permission"), "allow")

            result = run_hook(
                "gate-guard.sh",
                {"command": command, "cwd": str(workspace)},
                workspace=workspace,
            )
            self.assertEqual(result.get("permission"), "deny")
            self.assertEqual(journal.read_bytes(), original)

    def test_denies_nested_shell_writes_to_protected_paths(self):
        commands = (
            f"bash -c 'printf x > findings/demo/candidates-{DATE}.md'",
            "sh -c 'truncate -s 0 state.json'",
        )
        for command in commands:
            with self.subTest(command=command):
                result = run_hook("gate-guard.sh", {"command": command})
                self.assertEqual(result.get("permission"), "deny")

    def test_denies_documented_indirect_shell_mutation_classes(self):
        commands = (
            "`rm state.json`",
            'p=state.json; rm "$p"',
            "find findings -type f -delete",
            "find output -type f -exec rm {} +",
            "find . -name state.json -exec rm -f {} +",
            "find . -path './output/*' -delete",
            "find . -type f -delete",
            "find . -type f -exec rm -f {} +",
            "find . -name receipt-2026-08-26.json -exec rm -f {} +",
            "find . -name candidates-2026-08-26.md -delete",
            "printf '%s\\n' state.json | xargs rm",
            "printf '%s\\n' state.json | xargs -I{} sh -c 'rm \"$1\"' _ {}",
        )
        for command in commands:
            with self.subTest(command=command):
                result = run_hook("gate-guard.sh", {"command": command})
                self.assertEqual(result.get("permission"), "deny")

    def test_allows_read_only_forms_of_indirect_shell_commands(self):
        commands = (
            "p=state.json; cat \"$p\"",
            "find findings -type f -print",
            "find output -type f -exec cat {} +",
            "find . -name state.json -print",
            "find . -name state.json -exec cat {} +",
            "find . -type f -print",
            "find . -type f -exec cat {} +",
            "printf '%s\\n' state.json | xargs cat",
            "printf '%s\\n' state.json | xargs -I{} sh -c 'cat \"$1\"' _ {}",
        )
        for command in commands:
            with self.subTest(command=command):
                result = run_hook("gate-guard.sh", {"command": command})
                self.assertEqual(result.get("permission"), "allow")

    def test_command_substitution_cannot_hide_mutation_targets(self):
        mutations = (
            'target=$(printf state.json); truncate "$target"',
            'target=$(printf state.json)\ntruncate "$target"',
            'truncate "$(printf state.json)"',
            "truncate $(printf state.json)",
            'p=$(printf findings/x); rm "$p"',
            'p=$(printf "$(printf state.json)"); rm "$p"',
            'truncate "$(printf \'%s\' "$(printf state.json)")"',
            'command truncate "$(printf state.json)"',
            'env LC_ALL=C truncate "$(printf state.json)"',
        )
        for command in mutations:
            with self.subTest(command=command):
                result = run_hook("gate-guard.sh", {"command": command})
                self.assertEqual(result.get("permission"), "deny")

        safe = (
            'value=$(printf state.json); printf \'%s\' "$value"',
            'cat "$(printf /tmp/safe)"',
            'command cat "$(printf state.json)"',
            "truncate -s 0 /tmp/nightfalcon-safe",
            'p=/tmp/nightfalcon-safe; rm "$p"',
            "command touch /tmp/nightfalcon-safe",
        )
        for command in safe:
            with self.subTest(command=command):
                result = run_hook("gate-guard.sh", {"command": command})
                self.assertEqual(result.get("permission"), "allow")

        inactive = run_hook(
            "gate-guard.sh",
            {"command": 'target=$(printf state.json); truncate "$target"'},
            active=False,
        )
        self.assertEqual(inactive.get("permission"), "allow")

    def test_archive_extraction_requires_disjoint_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            workspace = make_ws(base, "phase-6")
            disjoint = base / "disjoint"
            disjoint.mkdir()
            disjoint_x = disjoint / "x"
            disjoint_x.mkdir()
            missing = base / "missing"

            mutations = (
                "tar -xf archive.tar -C findings",
                "tar xf archive.tar --directory=output",
                "bsdtar --extract --file archive.tar -C findings",
                "command tar -xvf archive.tar -C output",
                "env LC_ALL=C tar --extract -f archive.tar -C findings",
                "tar -xf archive.tar",
                f"tar -xf archive.tar -C {missing}",
                "unzip archive.zip -d findings",
                "unzip archive.zip -doutput",
                "unzip archive.zip",
                "cpio -id < archive.cpio",
                "7z x archive.7z -ooutput",
                "unrar x archive.rar findings/",
                "tar -cf output/archive.tar source",
            )
            for command in mutations:
                with self.subTest(command=command):
                    result = run_hook(
                        "gate-guard.sh",
                        {"command": command, "cwd": str(workspace)},
                        workspace=workspace,
                    )
                    self.assertEqual(result.get("permission"), "deny")

            safe = (
                "tar -tf archive.tar",
                "tar --list --file archive.tar",
                "unzip -t archive.zip",
                "unzip -l archive.zip",
                "cpio -it < archive.cpio",
                "7z l archive.7z",
                "7z t archive.7z",
                "unrar t archive.rar",
                f"tar -cf {disjoint / 'archive.tar'} source",
                f"tar cf {disjoint / 'archive-two.tar'} source",
                f"tar -cf{disjoint_x / 'archive-attached.tar'} source",
                f"tar -xf archive.tar -C {disjoint}",
                f"unzip archive.zip -d {disjoint}",
                f"cpio -id -D {disjoint} < archive.cpio",
                f"cpio -iD{disjoint} < archive.cpio",
                f"7z x archive.7z -o{disjoint}",
                f"unrar x archive.rar {disjoint}/",
            )
            for command in safe:
                with self.subTest(command=command):
                    result = run_hook(
                        "gate-guard.sh",
                        {"command": command, "cwd": str(workspace)},
                        workspace=workspace,
                    )
                    self.assertEqual(result.get("permission"), "allow")

            default_disjoint = run_hook(
                "gate-guard.sh",
                {"command": "tar -xf archive.tar", "cwd": str(disjoint)},
                workspace=workspace,
            )
            self.assertEqual(default_disjoint.get("permission"), "allow")

            inactive = run_hook(
                "gate-guard.sh",
                {"command": "tar -xf archive.tar -C findings"},
                active=False,
                workspace=workspace,
            )
            self.assertEqual(inactive.get("permission"), "allow")

    def test_deployment_copy_requires_disjoint_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            workspace = make_ws(base, "phase-6")
            disjoint = base / "disjoint"
            disjoint.mkdir()

            mutations = (
                "rsync /tmp/source findings/",
                "rsync /tmp/source .",
                "install /tmp/source output/replacement",
                "install -t findings /tmp/source",
                f"install -d findings {disjoint / 'new-dir'}",
                "ditto /tmp/source findings/replacement",
                "command ditto /tmp/source output/replacement",
                "env COPYFILE_DISABLE=1 ditto /tmp/source .",
            )
            for command in mutations:
                with self.subTest(command=command):
                    result = run_hook(
                        "gate-guard.sh",
                        {"command": command, "cwd": str(workspace)},
                        workspace=workspace,
                    )
                    self.assertEqual(result.get("permission"), "deny")

            safe = (
                f"rsync /tmp/source {disjoint}/",
                f"install /tmp/source {disjoint / 'replacement'}",
                f"install -t {disjoint} /tmp/source",
                f"install -d {disjoint / 'new-dir'}",
                f"ditto /tmp/source {disjoint / 'replacement'}",
            )
            for command in safe:
                with self.subTest(command=command):
                    result = run_hook(
                        "gate-guard.sh",
                        {"command": command, "cwd": str(workspace)},
                        workspace=workspace,
                    )
                    self.assertEqual(result.get("permission"), "allow")

    def test_find_mutation_denies_workspace_and_ancestor_but_allows_disjoint_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            workspace = make_ws(base, "phase-6")
            disjoint = base / "disjoint"
            disjoint.mkdir()
            commands = {
                f"find {workspace} -type f -delete": "deny",
                f"find {base} -type f -exec rm -f {{}} +": "deny",
                f"find {disjoint} -type f -delete": "allow",
            }
            for command, permission in commands.items():
                with self.subTest(command=command):
                    result = run_hook(
                        "gate-guard.sh", {"command": command}, workspace=workspace
                    )
                    self.assertEqual(result.get("permission"), permission)

    def test_find_mutation_canonicalizes_symlink_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            workspace = make_ws(base, "phase-6")
            workspace_link = base / "workspace-link"
            workspace_link.symlink_to(workspace, target_is_directory=True)
            findings_link = base / "findings-link"
            findings_link.symlink_to(
                workspace / "findings", target_is_directory=True
            )
            disjoint = base / "disjoint"
            disjoint.mkdir()
            disjoint_link = base / "disjoint-link"
            disjoint_link.symlink_to(disjoint, target_is_directory=True)
            commands = {
                f"find -L {workspace_link} -type f -delete": "deny",
                f"find {workspace_link}/. -type f -exec rm -f {{}} +": "deny",
                f"find -L {findings_link} -type f -delete": "deny",
                f"find {base / 'missing'} -type f -delete": "deny",
                f"find -L {workspace_link} -type f -print": "allow",
                f"find -L {disjoint_link} -type f -delete": "allow",
            }
            for command, permission in commands.items():
                with self.subTest(command=command):
                    result = run_hook(
                        "gate-guard.sh", {"command": command}, workspace=workspace
                    )
                    self.assertEqual(result.get("permission"), permission)

    def test_classifies_only_command_positions_and_honors_wrappers(self):
        read_only = (
            f"grep rm findings/demo/candidates-{DATE}.md",
            f"printf '%s\\n' sudo findings/demo/candidates-{DATE}.md",
            f"command grep rm findings/demo/candidates-{DATE}.md",
        )
        for command in read_only:
            with self.subTest(command=command):
                result = run_hook("gate-guard.sh", {"command": command})
                self.assertEqual(result.get("permission"), "allow")

        wrapped_mutations = (
            "env NIGHTFALCON_TEST=1 cp /tmp/replacement state.json",
            "sudo -- mv /tmp/replacement findings/demo/candidates-2026-08-21.md",
            "command cp /tmp/replacement state.json",
            "command -- cp /tmp/replacement state.json",
            "command -p cp /tmp/replacement state.json",
        )
        for command in wrapped_mutations:
            with self.subTest(command=command):
                result = run_hook("gate-guard.sh", {"command": command})
                self.assertEqual(result.get("permission"), "deny")

    def test_denies_patch_mutations_and_allows_only_informational_queries(self):
        mutations = (
            "patch state.json /tmp/state.patch",
            "patch -i /tmp/state.patch state.json",
            "patch < /tmp/state.patch state.json",
            "patch state.json < /tmp/state.patch",
            "patch < /tmp/state.patch",
            "command patch state.json < /tmp/state.patch",
            "env LC_ALL=C patch -i /tmp/state.patch state.json",
        )
        for command in mutations:
            with self.subTest(command=command):
                result = run_hook("gate-guard.sh", {"command": command})
                self.assertEqual(result.get("permission"), "deny")

        informational = (
            "patch --version",
            "patch --help",
            "command patch -v",
            "env LC_ALL=C patch -h",
        )
        for command in informational:
            with self.subTest(command=command):
                result = run_hook("gate-guard.sh", {"command": command})
                self.assertEqual(result.get("permission"), "allow")

        inactive = run_hook(
            "gate-guard.sh",
            {"command": "patch state.json < /tmp/state.patch"},
            active=False,
        )
        self.assertEqual(inactive.get("permission"), "allow")

    def test_denies_editor_mutations_of_protected_or_unresolved_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            workspace = make_ws(base, "phase-6")
            mutations = (
                "printf '1c\\n{}\\n.\\nw\\n' | ed state.json",
                "command ed ./state.json",
                f"env LC_ALL=C ex -s docs/../findings/demo/candidates-{DATE}.md",
                f"vi output/executive-summary-{DATE}.md",
                f"vim -N findings/demo/findings-{DATE}.json",
                "nvim -- state.json",
                "view docs/../output",
                f"vimdiff /tmp/baseline output/executive-report-{DATE}.json",
                "nano output",
                "pico findings",
                "red proof_of_concept",
                "emacs -Q state.json",
                f"emacsclient --no-wait output/executive-report-{DATE}.json",
                "nano $UNRESOLVED_EDITOR_TARGET",
                f"vi {workspace}",
                f"emacsclient {base}",
            )
            for command in mutations:
                with self.subTest(command=command):
                    result = run_hook(
                        "gate-guard.sh",
                        {"command": command, "cwd": str(workspace)},
                        workspace=workspace,
                    )
                    self.assertEqual(result.get("permission"), "deny")

            inactive = run_hook(
                "gate-guard.sh",
                {"command": "printf 'w\\n' | ed state.json"},
                active=False,
                workspace=workspace,
            )
            self.assertEqual(inactive.get("permission"), "allow")

    def test_editor_guard_fails_closed_on_batch_commands_and_allows_safe_forms(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            workspace = make_ws(base, "phase-6")
            disjoint = base / "disjoint"
            disjoint.mkdir()
            ambiguous = (
                "vim -c 'edit state.json'",
                "nvim --cmd 'source /tmp/commands.vim'",
                "ex +':e state.json'",
                "emacs --batch --eval '(find-file \"state.json\")'",
                "emacsclient --eval '(find-file \"state.json\")'",
                "printf 'e state.json\\nw\\n' | ed",
                "cat /tmp/editor.commands | ex -s",
                "ed < /tmp/editor.commands",
                "ex -sc 'source /tmp/editor.commands'",
                "vim -t hidden-tag",
                "nvim --remote-send '<Esc>:source /tmp/editor.commands<CR>'",
            )
            for command in ambiguous:
                with self.subTest(command=command):
                    result = run_hook(
                        "gate-guard.sh",
                        {"command": command, "cwd": str(workspace)},
                        workspace=workspace,
                    )
                    self.assertEqual(result.get("permission"), "deny")

            safe = (
                "ed --help",
                "ex --version",
                "vi --help",
                "vim --version",
                "nvim -v",
                "nano --help",
                "emacs --version",
                "emacsclient --help",
                f"ed {disjoint / 'notes.txt'}",
                f"ex {disjoint / 'notes.txt'}",
                f"vim {disjoint / 'notes.txt'}",
                f"nvim {disjoint / 'notes.txt'}",
                f"nano {disjoint / 'notes.txt'}",
                f"emacs -Q {disjoint / 'notes.txt'}",
                f"emacsclient --no-wait {disjoint / 'notes.txt'}",
            )
            for command in safe:
                with self.subTest(command=command):
                    result = run_hook(
                        "gate-guard.sh",
                        {"command": command, "cwd": str(workspace)},
                        workspace=workspace,
                    )
                    self.assertEqual(result.get("permission"), "allow")

    def test_denies_git_worktree_rollbacks_and_preserves_proven_safe_forms(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            workspace = make_ws(base, "phase-6")
            mutations = (
                "git restore --source=HEAD~1 -- state.json",
                "git restore --source HEAD~1 .",
                "git restore --worktree -- findings",
                "git restore -- docs/../findings",
                f"git restore -- {workspace}",
                f"git restore -- {base}",
                "command git restore -- output",
                "env GIT_OPTIONAL_LOCKS=0 git restore -- state.json",
                "git -C /tmp restore -- state.json",
                "git checkout HEAD~1 -- state.json",
                "git checkout -- findings",
                "git checkout HEAD~1",
                "git switch --detach HEAD~1",
                "git reset --hard HEAD~1",
                "git reset --merge HEAD",
                "git clean -fd .",
                "git clean -fx findings",
                "git clean -fd docs/../output",
                f"git clean -fd {workspace}",
                "git apply /tmp/change.patch",
                "git am /tmp/change.patch",
                "git checkout-index -a -f",
                "git read-tree -mu HEAD",
            )
            for command in mutations:
                with self.subTest(command=command):
                    result = run_hook(
                        "gate-guard.sh", {"command": command}, workspace=workspace
                    )
                    self.assertEqual(result.get("permission"), "deny")

            safe = (
                "git status --short",
                "git diff -- state.json",
                "git show HEAD:state.json",
                "git log --oneline -- state.json",
                "git restore -- docs/readme.md",
                "git checkout HEAD -- docs/readme.md",
                "git clean -fd -- docs/generated",
                "git clean -nd .",
                "git apply --check /tmp/change.patch",
                "git apply --stat /tmp/change.patch",
                "git am --show-current-patch=diff",
            )
            for command in safe:
                with self.subTest(command=command):
                    result = run_hook(
                        "gate-guard.sh", {"command": command}, workspace=workspace
                    )
                    self.assertEqual(result.get("permission"), "allow")

            inactive = run_hook(
                "gate-guard.sh",
                {"command": "git restore --source=HEAD~1 -- state.json"},
                active=False,
                workspace=workspace,
            )
            self.assertEqual(inactive.get("permission"), "allow")

    def test_allows_ordinary_commands(self):
        for cmd in (
            "git status",
            f"python3 scripts/report/build.py --input findings/demo/findings-{DATE}.json --output output/report.html",
            "bash output/proof_of_concept/run-all.sh",
            "cat state.json",
            f"grep -n Tier findings/demo/candidates-{DATE}.md",
            "python3 -m json.tool state.json",
        ):
            r = run_hook("gate-guard.sh", {"command": cmd})
            self.assertEqual(r.get("permission"), "allow", cmd)

    def test_noop_when_inactive(self):
        r = run_hook("gate-guard.sh",
                     {"command": 'sed -i x ./state.json'}, active=False)
        self.assertEqual(r.get("permission"), "allow")


class EditLogTests(unittest.TestCase):
    def test_activates_from_workspace_marker_without_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(pathlib.Path(tmp), "phase-1")
            result = run_hook_without_activation_env(
                "edit-log.sh",
                {
                    "cwd": str(ws),
                    "file_path": str(ws / "output/proof_of_concept/demo/F-001.sh"),
                },
                cwd=ws,
            )
            self.assertEqual(result, {})
            self.assertIn(
                "Out-of-scope edits",
                (ws / f"output/run-log-{DATE}.md").read_text(),
            )

    def test_in_scope_edit_returns_empty_no_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(pathlib.Path(tmp), "phase-1")
            r = run_hook("edit-log.sh",
                         {"file_path": str(ws / "findings/demo/dataflow-2026-08-21.md")},
                         workspace=ws)
            self.assertEqual(r, {})
            self.assertFalse((ws / f"output/run-log-{DATE}.md").exists())

    def test_out_of_scope_edit_logs_advisory_never_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(pathlib.Path(tmp), "phase-1")
            r = run_hook("edit-log.sh",
                         {"file_path": str(ws / "output/proof_of_concept/demo/F-001.sh")},
                         workspace=ws)
            # afterFileEdit can never block -> always {}
            self.assertEqual(r, {})
            log = (ws / f"output/run-log-{DATE}.md").read_text()
            self.assertIn("Out-of-scope edits", log)
            self.assertIn("output/proof_of_concept/demo/F-001.sh", log)

    def test_phase6_poc_write_is_in_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(pathlib.Path(tmp), "phase-6")
            r = run_hook("edit-log.sh",
                         {"file_path": str(ws / "output/proof_of_concept/demo/F-001.sh")},
                         workspace=ws)
            self.assertEqual(r, {})
            self.assertFalse((ws / f"output/run-log-{DATE}.md").exists())

    def test_canonical_journal_edit_reports_integrity_violation_and_stop_followup(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(pathlib.Path(tmp), "phase-8")
            journal = ws / "output" / f"agent-conversation-{DATE}.md"
            journal.write_text("tampered\n")
            result = run_hook(
                "edit-log.sh",
                {"file_path": str(journal)},
                workspace=ws,
            )
            self.assertIn("followup_message", result)
            message = result["followup_message"]
            self.assertIn("integrity violation", message.lower())
            self.assertIn("scripts/agent-journal.py", message)
            self.assertIn("validate", message)
            self.assertIn("stop", message.lower())

    def test_canonical_journal_edit_normalizes_parent_segments_before_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(pathlib.Path(tmp), "phase-8")
            journal = ws / "output" / f"agent-conversation-{DATE}.md"
            journal.write_text("tampered\n")
            result = run_hook(
                "edit-log.sh",
                {"file_path": f"output/sub/../agent-conversation-{DATE}.md"},
                workspace=ws,
            )
            self.assertIn("followup_message", result)
            self.assertIn("integrity violation", result["followup_message"].lower())

            other_ws = make_ws(pathlib.Path(tmp) / "other", "phase-1")
            unrelated = run_hook(
                "edit-log.sh",
                {"file_path": "output/sub/../ordinary.md"},
                workspace=other_ws,
            )
            self.assertEqual(unrelated, {})
            advisory = (other_ws / f"output/run-log-{DATE}.md").read_text()
            self.assertIn("output/sub/../ordinary.md", advisory)

    def test_absolute_canonical_journal_edit_preserves_workspace_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_ws(pathlib.Path(tmp) / "review workspace", "phase-8")
            journal = workspace / "output" / f"agent-conversation-{DATE}.md"
            journal.write_text("tampered\n")
            result = run_hook(
                "edit-log.sh",
                {"file_path": str(journal)},
                workspace=workspace,
            )
            self.assertIn("followup_message", result)
            self.assertIn("integrity violation", result["followup_message"].lower())


class StopEnforceTests(unittest.TestCase):
    def test_activates_from_workspace_marker_without_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(pathlib.Path(tmp), "phase-1")
            result = run_hook_without_activation_env(
                "stop-enforce.sh",
                {"status": "completed", "loop_count": 0, "cwd": str(ws)},
                cwd=ws,
            )
            self.assertIn("followup_message", result)
            self.assertIn("phase-1", result["followup_message"])

    def test_followup_when_phase_output_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(pathlib.Path(tmp), "phase-1")
            r = run_hook("stop-enforce.sh",
                         {"status": "completed", "loop_count": 0}, workspace=ws)
            self.assertIn("followup_message", r)
            self.assertIn("phase-1", r["followup_message"])

    def test_ends_when_phase_output_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(pathlib.Path(tmp), "phase-1")
            (ws / "findings/demo/dataflow-2026-08-21.md").write_text("x")
            r = run_hook("stop-enforce.sh",
                         {"status": "completed", "loop_count": 0}, workspace=ws)
            self.assertEqual(r, {})

    def test_ends_at_loop_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(pathlib.Path(tmp), "phase-1")  # output missing
            r = run_hook("stop-enforce.sh",
                         {"status": "completed", "loop_count": 3}, workspace=ws)
            self.assertEqual(r, {})

    def test_ends_on_done_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(pathlib.Path(tmp), "done")
            r = run_hook("stop-enforce.sh",
                         {"status": "completed", "loop_count": 0}, workspace=ws)
            self.assertEqual(r, {})

    def test_ends_on_aborted_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(pathlib.Path(tmp), "phase-1")  # output missing
            r = run_hook("stop-enforce.sh",
                         {"status": "aborted", "loop_count": 0}, workspace=ws)
            self.assertEqual(r, {})


if __name__ == "__main__":
    unittest.main()
