import base64
import fcntl
import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from contextlib import contextmanager

from tests.test_agent_journal_contract import rewrite_accepted_events


ROOT = pathlib.Path(__file__).resolve().parents[1]
DATE = "2026-08-26"


def write_public_phase1_support(directory: pathlib.Path) -> None:
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
PORTS = {
    "claude": ROOT / "claude",
    "codex": ROOT / "codex" / "plugins" / "nightfalcon",
    "cursor": ROOT / "cursor",
}
ENGINE = PORTS["codex"] / "scripts" / "agent-journal.py"
FAILPOINTS = (
    "after-intent",
    "after-prepared-append",
    "after-state-replace",
    "before-git-commit",
    "after-git-commit-before-ref",
)
INIT_FAILPOINTS = (
    "after-init-intent",
    "during-init-journal-write",
    "after-init-journal-fsync",
    "after-init-state-replace",
)
INIT_INTENT_NAME = ".nightfalcon-journal-init-transaction.json"
RESUME_FAILPOINTS = (
    "before-resume-transaction",
    "after-resume-intent",
    "during-resume-journal-write",
    "after-resume-journal-fsync",
    "after-resume-state-replace",
)
RESUME_INTENT_NAME = ".nightfalcon-journal-resume-transaction.json"


def run(*args: str, cwd: pathlib.Path | None = None, env: dict[str, str] | None = None,
        text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=cwd, env=env, text=text, capture_output=True, check=False,
    )


def run_journal(port: pathlib.Path, *args: str, env: dict[str, str] | None = None,
                text: bool = True) -> subprocess.CompletedProcess:
    return run(
        "/usr/local/bin/python3", str(port / "scripts" / "agent-journal.py"),
        *args, env=env, text=text,
    )


def journal(*args: str, env: dict[str, str] | None = None,
            text: bool = True) -> subprocess.CompletedProcess:
    return run_journal(PORTS["codex"], *args, env=env, text=text)


def seed_init_workspace(
    base: str,
    *,
    port_extension: dict[str, bool] | None = None,
) -> pathlib.Path:
    workspace = pathlib.Path(base) / "workspace"
    (workspace / "output").mkdir(parents=True)
    state = {
        "date": DATE,
        "current_phase": "phase-0",
        "mode": "review",
        "phase_status": {},
        "history": [],
        "git_checkpoints": [],
        "repo_slugs": ["demo", "second"],
        "port_extension": (
            {"keep": True} if port_extension is None else port_extension
        ),
    }
    state_path = workspace / "state.json"
    state_path.write_text(json.dumps(state, indent=2) + "\n")
    state_path.chmod(0o640)
    (workspace / "output" / "session-manifest.json").write_text(
        json.dumps({
            "schema_version": "1",
            "date": DATE,
            "phases_completed": [],
            "manifest_extension": {"keep": True},
        }, indent=2) + "\n"
    )
    return workspace


def init_with_failpoint(
    workspace: pathlib.Path,
    failpoint: str,
    *,
    port: pathlib.Path = PORTS["codex"],
    port_name: str = "codex",
) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment["NIGHTFALCON_JOURNAL_INIT_FAILPOINT"] = failpoint
    return run_journal(
        port,
        "init",
        "--workspace",
        str(workspace),
        "--date",
        DATE,
        "--port",
        port_name,
        env=environment,
    )


def decode_init_intent(workspace: pathlib.Path) -> tuple[dict[str, object], bytes, bytes, bytes]:
    intent = json.loads((workspace / INIT_INTENT_NAME).read_text())
    old_state = base64.b64decode(intent["state"]["old_base64"], validate=True)
    new_state = base64.b64decode(intent["state"]["new_base64"], validate=True)
    full_journal = base64.b64decode(intent["journal"]["full_base64"], validate=True)
    return intent, old_state, new_state, full_journal


def filesystem_snapshot(*paths: pathlib.Path) -> dict[str, object]:
    snapshot: dict[str, object] = {}
    for path in paths:
        try:
            info = path.lstat()
        except FileNotFoundError:
            snapshot[str(path)] = None
            continue
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            snapshot[str(path)] = ("symlink", mode, os.readlink(path))
        elif stat.S_ISREG(info.st_mode):
            snapshot[str(path)] = ("file", mode, path.read_bytes())
        elif stat.S_ISDIR(info.st_mode):
            snapshot[str(path)] = ("directory", mode)
        else:
            snapshot[str(path)] = ("other", mode)
    return snapshot


def replace_checkpoint_blob(
    workspace: pathlib.Path, path: str, value: bytes, checkpoint_uuid: str
) -> str:
    """Replace one checkpoint blob in a unique sibling commit, leaving worktree bytes."""
    old_commit = run("git", "rev-parse", "HEAD", cwd=workspace).stdout.strip()
    parent = run("git", "rev-parse", "HEAD^", cwd=workspace).stdout.strip()
    head_ref = run("git", "symbolic-ref", "HEAD", cwd=workspace).stdout.strip()
    with tempfile.TemporaryDirectory() as temporary_index:
        environment = os.environ.copy()
        environment["GIT_INDEX_FILE"] = str(
            pathlib.Path(temporary_index) / "checkpoint.index"
        )
        read_tree = run("git", "read-tree", old_commit, cwd=workspace, env=environment)
        if read_tree.returncode:
            raise AssertionError(read_tree.stderr)
        blob = subprocess.run(
            ("git", "-C", str(workspace), "hash-object", "-w", "--stdin"),
            input=value,
            capture_output=True,
            check=False,
        )
        if blob.returncode:
            raise AssertionError(blob.stderr.decode())
        update = run(
            "git", "update-index", "--add", "--cacheinfo",
            f"100644,{blob.stdout.decode().strip()},{path}",
            cwd=workspace,
            env=environment,
        )
        if update.returncode:
            raise AssertionError(update.stderr)
        tree = run("git", "write-tree", cwd=workspace, env=environment)
        if tree.returncode:
            raise AssertionError(tree.stderr)
    message = (
        "tampered checkpoint\n\n"
        f"NightFalcon-Checkpoint-ID: {checkpoint_uuid}\n"
    )
    replacement = subprocess.run(
        ("git", "-C", str(workspace), "commit-tree", tree.stdout.strip(), "-p", parent),
        input=message,
        text=True,
        capture_output=True,
        check=False,
    )
    if replacement.returncode:
        raise AssertionError(replacement.stderr)
    replacement_commit = replacement.stdout.strip()
    moved = run(
        "git", "update-ref", head_ref, replacement_commit, old_commit,
        cwd=workspace,
    )
    if moved.returncode:
        raise AssertionError(moved.stderr)
    return replacement_commit


@contextmanager
def seeded_phase_workspace(
    port: pathlib.Path = PORTS["codex"],
    port_name: str = "codex",
    port_extension: dict[str, bool] | None = None,
    *,
    record_intent: bool = True,
):
    with tempfile.TemporaryDirectory() as temporary:
        workspace = pathlib.Path(temporary) / "workspace"
        (workspace / "findings" / "demo").mkdir(parents=True)
        (workspace / "sourcecode" / "demo").mkdir(parents=True)
        (workspace / "output").mkdir()
        (workspace / "input").mkdir()
        state = {
            "date": DATE,
            "current_phase": "phase-0",
            "mode": "review",
            "phase_status": {},
            "history": [],
            "git_checkpoints": [],
            "repo_slugs": ["demo"] if record_intent else [],
            "port_extension": (
                {"keep": True} if port_extension is None else port_extension
            ),
        }
        manifest = {
            "schema_version": "1",
            "date": DATE,
            "phases_completed": [],
            "ended_at": None,
            "manifest_extension": {"keep": True},
        }
        (workspace / "state.json").write_text(json.dumps(state, indent=2) + "\n")
        (workspace / "output" / "session-manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n"
        )
        artifact = workspace / "findings" / "demo" / f"candidates-{DATE}.md"
        artifact.write_text("# Candidates\n\n## CANDIDATE-1 — F-001\n\noriginal evidence\n")
        (workspace / "input" / f"repos-{DATE}.txt").write_text("demo\n")
        (workspace / "output" / f"run-log-{DATE}.md").write_text("# Run log\n")
        self_git = (
            ("git", "init", "-q"),
            ("git", "config", "user.name", "NightFalcon Test"),
            ("git", "config", "user.email", "nightfalcon@example.invalid"),
            ("git", "add", "."),
            ("git", "commit", "-qm", "initial workspace"),
        )
        for command in self_git:
            result = run(*command, cwd=workspace)
            if result.returncode:
                raise AssertionError(result.stderr)
        initialized = run_journal(
            port, "init", "--workspace", str(workspace), "--date", DATE,
            "--port", port_name,
        )
        if initialized.returncode:
            raise AssertionError(initialized.stderr)
        if not record_intent:
            current = json.loads((workspace / "state.json").read_text())
            current["repo_slugs"] = ["demo"]
            (workspace / "state.json").write_text(
                json.dumps(current, indent=2) + "\n"
            )
        if record_intent:
            intent = run_journal(port, "record-intent", "--workspace", str(workspace))
            if intent.returncode:
                raise AssertionError(intent.stderr)
        yield workspace, artifact


def accept_phase(
    workspace: pathlib.Path,
    failpoint: str | None = None,
    port: pathlib.Path = PORTS["codex"],
) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    if failpoint:
        environment["NIGHTFALCON_JOURNAL_FAILPOINT"] = failpoint
    return run_journal(
        port, "accept-phase",
        "--workspace", str(workspace),
        "--phase", "phase-0",
        "--timestamp", "2026-08-26T12:00:00Z",
        env=environment,
    )


@contextmanager
def direct_phase_workspace(phase: str):
    """Seed authoritative journal state without any phase output artifacts."""
    with tempfile.TemporaryDirectory() as temporary:
        workspace = pathlib.Path(temporary) / "workspace"
        (workspace / "output").mkdir(parents=True)
        (workspace / "sourcecode" / "demo").mkdir(parents=True)
        mode = "triage" if phase == "triage-ingest" else "review"
        state = {
            "date": DATE,
            "current_phase": phase,
            "mode": mode,
            "phase_status": {},
            "history": [],
            "git_checkpoints": [],
            "repo_slugs": ["demo"],
        }
        manifest = {
            "schema_version": "1",
            "date": DATE,
            "phases_completed": [],
            "checkpoints": [],
        }
        (workspace / "state.json").write_text(json.dumps(state, indent=2) + "\n")
        (workspace / "output" / "session-manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n"
        )
        initialized = journal(
            "init", "--workspace", str(workspace), "--date", DATE,
            "--port", "codex",
        )
        if initialized.returncode:
            raise AssertionError(initialized.stderr)
        intent = journal("record-intent", "--workspace", str(workspace))
        if intent.returncode:
            raise AssertionError(intent.stderr)
        for command in (
            ("git", "init", "-q"),
            ("git", "config", "user.name", "NightFalcon Test"),
            ("git", "config", "user.email", "nightfalcon@example.invalid"),
            ("git", "add", "."),
            ("git", "commit", "-qm", "initial phase fixture"),
        ):
            result = run(*command, cwd=workspace)
            if result.returncode:
                raise AssertionError(result.stderr)
        yield workspace


def reconcile(
    workspace: pathlib.Path,
    port: pathlib.Path = PORTS["codex"],
) -> subprocess.CompletedProcess:
    return run_journal(port, "reconcile", "--workspace", str(workspace))


def transaction_event_bytes(workspace: pathlib.Path, event: str) -> bytes:
    intent = json.loads(
        (workspace / ".nightfalcon-journal-transaction.json").read_text()
    )
    return base64.b64decode(intent["journal"][event]["base64"], validate=True)


def record_for_epoch(
    workspace: pathlib.Path,
    journal_state: dict[str, object],
    event_type: str,
    *extra: str,
) -> subprocess.CompletedProcess:
    return journal(
        "record", "--workspace", str(workspace), "--event-type", event_type,
        "--session-id", journal_state["session_id"],
        "--phase", journal_state["epoch_phase"],
        "--epoch-id", journal_state["epoch_id"],
        *extra,
    )


def record_completed_lifecycle(
    workspace: pathlib.Path,
    journal_state: dict[str, object],
    attempt_id: str,
    role: str,
    counts: dict[str, int] | None = None,
) -> list[subprocess.CompletedProcess]:
    results = []
    for event_type, action, status in (
        ("agent-spawn-requested", "spawn-agent", "requested"),
        ("agent-started", "start-agent", "started"),
        ("agent-completed", "complete-agent", "completed"),
    ):
        extra = [
            "--agent-id", attempt_id,
            "--agent-role", role,
            "--action-code", action,
            "--status-code", status,
            "--reason-code", "phase-contract",
        ]
        if event_type == "agent-completed" and counts is not None:
            extra.extend(["--counts", json.dumps(counts)])
        results.append(
            record_for_epoch(workspace, journal_state, event_type, *extra)
        )
    return results


def handoff_projection(workspace: pathlib.Path) -> dict[str, object]:
    rendered = journal("context", "--workspace", str(workspace))
    if rendered.returncode:
        raise AssertionError(rendered.stderr)
    prefix = "## Next-run handoff\n\n```json\n"
    if not rendered.stdout.startswith(prefix) or not rendered.stdout.endswith("\n```\n"):
        raise AssertionError("context did not render the canonical handoff envelope")
    return json.loads(rendered.stdout[len(prefix):-5])


class JournalRecoveryTests(unittest.TestCase):
    def assert_checkpoint_invariants(
        self,
        workspace: pathlib.Path,
        *,
        port: pathlib.Path = PORTS["codex"],
        expected_port_extension: dict[str, bool] | None = None,
    ) -> dict[str, object]:
        state = json.loads((workspace / "state.json").read_text())
        manifest = json.loads((workspace / "output" / "session-manifest.json").read_text())
        parsed = run_journal(port, "validate", "--workspace", str(workspace))
        self.assertEqual(parsed.returncode, 0, parsed.stderr)
        events = json.loads(parsed.stdout)["events"]
        accepted = [event for event in events if event["event_type"] == "phase-accepted"]
        prepared = [event for event in events if event["event_type"] == "phase-acceptance-prepared"]
        self.assertEqual(len(prepared), 1)
        self.assertEqual(len(accepted), 1)
        checkpoint_uuid = accepted[0]["checkpoint_uuid"]
        self.assertEqual(prepared[0]["checkpoint_uuid"], checkpoint_uuid)
        self.assertEqual(state["git_checkpoints"][-1]["checkpoint_uuid"], checkpoint_uuid)
        self.assertEqual(manifest["checkpoints"][-1]["checkpoint_uuid"], checkpoint_uuid)
        self.assertEqual(state["current_phase"], "phase-1")
        self.assertEqual(state["phase_status"]["phase-0"], "completed")
        self.assertEqual(
            state["port_extension"],
            {"keep": True}
            if expected_port_extension is None
            else expected_port_extension,
        )
        self.assertEqual(manifest["manifest_extension"], {"keep": True})
        checkpoint = state["git_checkpoints"][-1]
        expected_sequence = accepted[0]["sequence"]
        if checkpoint.get("journal_transition_version") == "2":
            expected_sequence += 1
            seed = events[accepted[0]["sequence"]]
            self.assertEqual(seed["event_type"], "user-intent-recorded")
            self.assertEqual(seed["phase"], "phase-1")
            self.assertEqual(
                seed["epoch_id"], state["agent_journal"]["epoch_id"]
            )
        self.assertEqual(
            state["agent_journal"]["accepted_sequence"], expected_sequence
        )
        provisional = events[expected_sequence:]
        if provisional:
            self.assertEqual(
                [event["event_type"] for event in provisional],
                ["recovery-started", "recovery-completed"],
            )
        self.assertEqual(state["agent_journal"]["epoch_phase"], "phase-1")
        self.assertNotEqual(state["agent_journal"]["checkpoint_uuid"], checkpoint_uuid)
        ref = f"refs/nightfalcon/checkpoints/{state['agent_journal']['session_id']}/phase-0"
        commit = run("git", "rev-parse", ref, cwd=workspace)
        self.assertEqual(commit.returncode, 0, commit.stderr)
        message = run("git", "show", "-s", "--format=%B", commit.stdout.strip(), cwd=workspace)
        self.assertIn(f"NightFalcon-Checkpoint-ID: {checkpoint_uuid}", message.stdout)
        tracked_payload = (
            (workspace / "state.json").read_text()
            + (workspace / "output" / "session-manifest.json").read_text()
            + (workspace / "output" / f"agent-conversation-{DATE}.md").read_text()
        )
        self.assertNotIn(commit.stdout.strip(), tracked_payload)
        self.assertFalse((workspace / ".nightfalcon-journal-transaction.json").exists())
        return {"accepted": accepted[0], "events": events}

    def test_direct_acceptance_runs_phase_contract_for_every_phase(self) -> None:
        """Removing a required phase artifact must make direct acceptance fail."""
        phases = ("triage-ingest",) + tuple(f"phase-{index}" for index in range(9))
        for phase in phases:
            with self.subTest(phase=phase), direct_phase_workspace(phase) as workspace:
                before_state = (workspace / "state.json").read_bytes()
                result = journal(
                    "accept-phase", "--workspace", str(workspace),
                    "--phase", phase, "--timestamp", "2026-08-26T12:00:00Z",
                )
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("phase artifact validation failed", result.stderr)
                self.assertEqual((workspace / "state.json").read_bytes(), before_state)
                self.assertFalse(
                    (workspace / ".nightfalcon-journal-transaction.json").exists()
                )

    def test_acceptance_revalidates_artifacts_after_wrapper_prevalidation(self) -> None:
        """A post-validation artifact mutation must not cross acceptance ownership."""
        with direct_phase_workspace("phase-0") as workspace:
            run_log = workspace / "output" / f"run-log-{DATE}.md"
            run_log.write_text("# Run log\n")
            barrier = workspace / "acceptance-barrier"
            environment = os.environ.copy()
            environment["NIGHTFALCON_JOURNAL_TEST_BARRIER"] = str(barrier)
            process = subprocess.Popen(
                [
                    "bash",
                    str(PORTS["codex"] / "scripts" / "complete-phase.sh"),
                    "--phase", "phase-0", "--workspace", str(workspace),
                ],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            ready = pathlib.Path(str(barrier) + ".ready")
            deadline = time.monotonic() + 10
            while (
                not ready.exists()
                and process.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            if not ready.exists():
                stdout, stderr = process.communicate(timeout=5)
                self.fail(
                    "acceptance did not reach the lock-owned barrier: "
                    f"{stdout}{stderr}"
                )
            run_log.unlink()
            barrier.write_text("release\n")
            stdout, stderr = process.communicate(timeout=20)

        self.assertEqual(process.returncode, 2, stdout + stderr)
        self.assertIn("phase artifact validation failed", stderr)

    def test_validate_and_context_reconcile_every_pending_acceptance_stage(self) -> None:
        """Read boundaries must converge an exact transaction before returning data."""
        for failpoint in FAILPOINTS:
            for command in ("validate", "context"):
                with (
                    self.subTest(failpoint=failpoint, command=command),
                    seeded_phase_workspace() as (workspace, _artifact),
                ):
                    interrupted = accept_phase(workspace, failpoint=failpoint)
                    self.assertEqual(interrupted.returncode, 2, interrupted.stderr)
                    result = journal(command, "--workspace", str(workspace))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(
                        (workspace / ".nightfalcon-journal-transaction.json").exists()
                    )
                    state = json.loads((workspace / "state.json").read_text())
                    self.assertEqual(state["current_phase"], "phase-1")

    def test_validate_and_context_wait_for_the_workspace_lock(self) -> None:
        """Unlocked readers can observe a split journal/state/checkpoint transition."""
        for command in ("validate", "context"):
            with self.subTest(command=command), direct_phase_workspace("phase-0") as workspace:
                lock_path = workspace / ".nightfalcon-journal.lock"
                with lock_path.open("a+") as lock:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                    process = subprocess.Popen(
                        [
                            "/usr/local/bin/python3",
                            str(ENGINE),
                            command,
                            "--workspace",
                            str(workspace),
                        ],
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    time.sleep(0.15)
                    self.assertIsNone(
                        process.poll(), f"{command} crossed the held workspace lock"
                    )
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                    stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stdout + stderr)

    def test_authoritative_readers_reject_retargeted_or_tampered_checkpoints(self) -> None:
        """Refs, trailers, committed metadata, and bound blobs are all authority."""
        mutations = ("missing-ref", "retargeted-ref", "metadata", "artifact")
        for mutation in mutations:
            for command in ("validate", "context"):
                with (
                    self.subTest(mutation=mutation, command=command),
                    seeded_phase_workspace() as (workspace, artifact),
                ):
                    accepted = accept_phase(workspace)
                    self.assertEqual(accepted.returncode, 0, accepted.stderr)
                    checkpoint_uuid = json.loads(accepted.stdout)["checkpoint_uuid"]
                    state = json.loads((workspace / "state.json").read_text())
                    ref = (
                        "refs/nightfalcon/checkpoints/"
                        f"{state['agent_journal']['session_id']}/phase-0"
                    )
                    if mutation == "missing-ref":
                        changed = run("git", "update-ref", "-d", ref, cwd=workspace)
                    elif mutation == "retargeted-ref":
                        parent = run("git", "rev-parse", "HEAD^", cwd=workspace).stdout.strip()
                        changed = run("git", "update-ref", ref, parent, cwd=workspace)
                    elif mutation == "metadata":
                        committed = json.loads((workspace / "state.json").read_text())
                        committed["agent_journal"]["accepted_head_sha256"] = "0" * 64
                        replacement = replace_checkpoint_blob(
                            workspace,
                            "state.json",
                            (json.dumps(committed, sort_keys=True, indent=2) + "\n").encode(),
                            checkpoint_uuid,
                        )
                        changed = run("git", "update-ref", ref, replacement, cwd=workspace)
                    else:
                        replacement = replace_checkpoint_blob(
                            workspace,
                            artifact.relative_to(workspace).as_posix(),
                            b"tampered checkpoint artifact\n",
                            checkpoint_uuid,
                        )
                        changed = run("git", "update-ref", ref, replacement, cwd=workspace)
                    self.assertEqual(changed.returncode, 0, changed.stderr)
                    result = journal(command, "--workspace", str(workspace))
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn("checkpoint", result.stderr.lower())

    def test_reconcile_seeds_one_canonical_current_epoch_intent(self) -> None:
        with seeded_phase_workspace() as (workspace, _artifact):
            epoch = json.loads((workspace / "state.json").read_text())["agent_journal"]
            started = record_for_epoch(
                workspace, epoch, "phase-started",
                "--agent-role", "orchestrator",
                "--action-code", "start-phase",
                "--status-code", "started",
                "--reason-code", "phase-contract",
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            resumed = reconcile(workspace)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            state = json.loads((workspace / "state.json").read_text())
            journal_state = state["agent_journal"]
            events = json.loads(
                journal("validate", "--workspace", str(workspace)).stdout
            )["events"]
            current = [
                event for event in events
                if event["session_id"] == journal_state["session_id"]
                and event["phase"] == journal_state["epoch_phase"]
                and event["epoch_id"] == journal_state["epoch_id"]
            ]
            first_state = (workspace / "state.json").read_bytes()
            journal_path = workspace / "output" / f"agent-conversation-{DATE}.md"
            first_journal = journal_path.read_bytes()
            second = reconcile(workspace)
            second_state = (workspace / "state.json").read_bytes()
            second_journal = journal_path.read_bytes()

        self.assertEqual(
            [event["event_type"] for event in current],
            ["run-resumed", "user-intent-recorded"],
        )
        self.assertEqual(
            sum(event["event_type"] == "user-intent-recorded" for event in current),
            1,
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second_state, first_state)
        self.assertEqual(second_journal, first_journal)

    def test_resume_transaction_recovers_every_torn_write_stage(self) -> None:
        for failpoint in RESUME_FAILPOINTS:
            with self.subTest(failpoint=failpoint), seeded_phase_workspace() as (
                workspace, _artifact
            ):
                epoch = json.loads(
                    (workspace / "state.json").read_text()
                )["agent_journal"]
                started = record_for_epoch(
                    workspace, epoch, "phase-started",
                    "--agent-role", "orchestrator",
                    "--action-code", "start-phase",
                    "--status-code", "started",
                    "--reason-code", "phase-contract",
                )
                self.assertEqual(started.returncode, 0, started.stderr)
                interrupted = journal(
                    "reconcile", "--workspace", str(workspace),
                    env=os.environ | {
                        "NIGHTFALCON_JOURNAL_RESUME_FAILPOINT": failpoint
                    },
                )
                transaction = workspace / RESUME_INTENT_NAME
                self.assertEqual(interrupted.returncode, 2, interrupted.stderr)
                self.assertIn("injected resume failure", interrupted.stderr)
                self.assertEqual(
                    transaction.is_file(), failpoint != "before-resume-transaction"
                )

                initialized = journal(
                    "init", "--workspace", str(workspace), "--date", DATE,
                    "--port", "codex",
                )
                self.assertEqual(initialized.returncode, 0, initialized.stderr)
                recovered = reconcile(workspace)
                self.assertEqual(recovered.returncode, 0, recovered.stderr)
                self.assertFalse(transaction.exists())
                state = json.loads((workspace / "state.json").read_text())
                journal_state = state["agent_journal"]
                validated = journal("validate", "--workspace", str(workspace))
                self.assertEqual(validated.returncode, 0, validated.stderr)
                current = [
                    event for event in json.loads(validated.stdout)["events"]
                    if event["session_id"] == journal_state["session_id"]
                    and event["phase"] == journal_state["epoch_phase"]
                    and event["epoch_id"] == journal_state["epoch_id"]
                ]
                self.assertEqual(
                    [event["event_type"] for event in current],
                    ["run-resumed", "user-intent-recorded"],
                )
                event_types = [
                    event["event_type"]
                    for event in json.loads(validated.stdout)["events"]
                ]
                for event_type in (
                    "recovery-started",
                    "phase-acceptance-aborted",
                    "recovery-completed",
                ):
                    self.assertEqual(event_types.count(event_type), 1)

    def test_active_run_reconcile_is_byte_exact_and_preserves_requested_started_attempt(self) -> None:
        """The same interrupted epoch must be reconciled once, not rotated repeatedly."""
        attempt_id = "11111111-1111-4111-8111-111111111111"
        with seeded_phase_workspace() as (workspace, _artifact):
            epoch = json.loads((workspace / "state.json").read_text())["agent_journal"]
            for event_type, action, status in (
                ("agent-spawn-requested", "spawn-agent", "requested"),
                ("agent-started", "start-agent", "started"),
            ):
                recorded = record_for_epoch(
                    workspace,
                    epoch,
                    event_type,
                    "--agent-id", attempt_id,
                    "--agent-role", "phase-worker",
                    "--action-code", action,
                    "--status-code", status,
                    "--reason-code", "phase-contract",
                )
                self.assertEqual(recorded.returncode, 0, recorded.stderr)
            terminal = record_for_epoch(
                workspace, epoch, "agent-failed",
                "--agent-id", attempt_id,
                "--agent-role", "phase-worker",
                "--action-code", "fail-agent",
                "--status-code", "failed",
                "--reason-code", "worker-failure",
            )
            self.assertEqual(terminal.returncode, 0, terminal.stderr)
            first = reconcile(workspace)
            self.assertEqual(first.returncode, 0, first.stderr)
            first_state = (workspace / "state.json").read_bytes()
            journal_path = workspace / "output" / f"agent-conversation-{DATE}.md"
            first_journal = journal_path.read_bytes()
            projection = handoff_projection(workspace)
            events = json.loads(journal("validate", "--workspace", str(workspace)).stdout)["events"]

            second = reconcile(workspace)
            self.assertEqual(second.returncode, 0, second.stderr)
            second_state = (workspace / "state.json").read_bytes()
            second_journal = journal_path.read_bytes()

        self.assertEqual(second_state, first_state)
        self.assertEqual(second_journal, first_journal)
        self.assertEqual(
            [event["event_type"] for event in events[-6:]],
            [
                "agent-failed",
                "recovery-started",
                "phase-acceptance-aborted",
                "recovery-completed",
                "run-resumed",
                "user-intent-recorded",
            ],
        )
        interrupted = projection["interrupted_attempt"]
        self.assertEqual(len(interrupted["attempts"]), 1)
        attempt = interrupted["attempts"][0]
        self.assertEqual(attempt["epoch_id"], epoch["epoch_id"])
        self.assertEqual(attempt["phase"], "phase-0")
        self.assertEqual(
            [event["event_type"] for event in attempt["events"] if event.get("agent_id") == attempt_id],
            ["agent-spawn-requested", "agent-started", "agent-failed"],
        )

    def test_open_attempt_reconcile_requires_exact_terminal_event(self) -> None:
        attempt_id = "33333333-3333-4333-8333-333333333333"
        with seeded_phase_workspace() as (workspace, _artifact):
            epoch = json.loads((workspace / "state.json").read_text())["agent_journal"]
            for event_type, action, status in (
                ("agent-spawn-requested", "spawn-agent", "requested"),
                ("agent-started", "start-agent", "started"),
            ):
                recorded = record_for_epoch(
                    workspace, epoch, event_type,
                    "--agent-id", attempt_id,
                    "--agent-role", "phase-worker",
                    "--action-code", action,
                    "--status-code", status,
                    "--reason-code", "phase-contract",
                )
                self.assertEqual(recorded.returncode, 0, recorded.stderr)

            refused = reconcile(workspace)
            refused_events = json.loads(
                journal("validate", "--workspace", str(workspace)).stdout
            )["events"]
            bypass = journal(
                "reconcile", "--workspace", str(workspace),
                "--runtime-cleanup-confirmed",
            )
            terminal = record_for_epoch(
                workspace, epoch, "agent-failed",
                "--agent-id", attempt_id,
                "--agent-role", "phase-worker",
                "--action-code", "fail-agent",
                "--status-code", "failed",
                "--reason-code", "worker-failure",
            )
            confirmed = reconcile(workspace)

        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn("exact terminal events", refused.stderr)
        self.assertFalse(any(
            event["event_type"] == "agent-failed"
            and event.get("agent_id") == attempt_id
            for event in refused_events
        ))
        self.assertEqual(bypass.returncode, 2, bypass.stderr)
        self.assertIn("unrecognized arguments", bypass.stderr)
        self.assertEqual(terminal.returncode, 0, terminal.stderr)
        self.assertEqual(confirmed.returncode, 0, confirmed.stderr)

    def test_requested_only_attempt_requires_exact_spawn_rejected_terminal_event(self) -> None:
        """Requested attempts stay open until their exact spawn rejection is recorded."""
        attempt_id = "22222222-2222-4222-8222-222222222222"
        with seeded_phase_workspace() as (workspace, _artifact):
            epoch = json.loads((workspace / "state.json").read_text())["agent_journal"]
            requested = record_for_epoch(
                workspace,
                epoch,
                "agent-spawn-requested",
                "--agent-id", attempt_id,
                "--agent-role", "phase-worker",
                "--action-code", "spawn-agent",
                "--status-code", "requested",
                "--reason-code", "phase-contract",
            )
            self.assertEqual(requested.returncode, 0, requested.stderr)
            refused = reconcile(workspace)
            terminal = record_for_epoch(
                workspace, epoch, "agent-failed",
                "--agent-id", attempt_id,
                "--agent-role", "phase-worker",
                "--action-code", "fail-agent",
                "--status-code", "failed",
                "--reason-code", "spawn-rejected",
            )
            self.assertEqual(terminal.returncode, 0, terminal.stderr)
            resumed = reconcile(workspace)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            events = json.loads(journal("validate", "--workspace", str(workspace)).stdout)["events"]

        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn("exact terminal events", refused.stderr)
        terminal = [
            event for event in events
            if event.get("agent_id") == attempt_id
            and event["event_type"] == "agent-failed"
        ]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["status_code"], "failed")
        self.assertEqual(terminal[0]["reason_code"], "spawn-rejected")
        self.assertEqual(
            [event["event_type"] for event in events].count("recovery-started"), 1
        )
        self.assertEqual(
            [event["event_type"] for event in events].count("recovery-completed"), 1
        )

    def test_transaction_recovery_events_and_second_reconcile_are_exact(self) -> None:
        """Transaction convergence records one recovery pair in the new epoch."""
        with seeded_phase_workspace() as (workspace, _artifact):
            interrupted = accept_phase(workspace, failpoint="after-intent")
            self.assertEqual(interrupted.returncode, 2, interrupted.stderr)
            first = reconcile(workspace)
            self.assertEqual(first.returncode, 0, first.stderr)
            journal_path = workspace / "output" / f"agent-conversation-{DATE}.md"
            first_state = (workspace / "state.json").read_bytes()
            first_journal = journal_path.read_bytes()
            events = json.loads(journal("validate", "--workspace", str(workspace)).stdout)["events"]
            second = reconcile(workspace)
            self.assertEqual(second.returncode, 0, second.stderr)
            second_state = (workspace / "state.json").read_bytes()
            second_journal = journal_path.read_bytes()

        self.assertEqual(second_state, first_state)
        self.assertEqual(second_journal, first_journal)
        self.assertEqual(
            [event["event_type"] for event in events].count("recovery-started"), 1
        )
        self.assertEqual(
            [event["event_type"] for event in events].count("recovery-completed"), 1
        )

    def test_safe_reconcile_failure_records_one_recovery_blocked_event(self) -> None:
        """A checkpoint authority failure is journaled once when append remains safe."""
        with seeded_phase_workspace() as (workspace, _artifact):
            accepted = accept_phase(workspace)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            state = json.loads((workspace / "state.json").read_text())
            ref = (
                "refs/nightfalcon/checkpoints/"
                f"{state['agent_journal']['session_id']}/phase-0"
            )
            commit = run("git", "rev-parse", ref, cwd=workspace).stdout.strip()
            removed = run("git", "update-ref", "-d", ref, cwd=workspace)
            self.assertEqual(removed.returncode, 0, removed.stderr)
            blocked = reconcile(workspace)
            self.assertEqual(blocked.returncode, 2, blocked.stderr)
            repaired = run("git", "update-ref", ref, commit, cwd=workspace)
            self.assertEqual(repaired.returncode, 0, repaired.stderr)
            events = json.loads(journal("validate", "--workspace", str(workspace)).stdout)["events"]

        self.assertEqual(
            [event["event_type"] for event in events].count("recovery-blocked"), 1
        )

    def test_acceptance_requires_current_intent_and_an_empty_agent_ledger(self) -> None:
        """Acceptance owns both intent and requested/started ledger closure."""
        with seeded_phase_workspace(record_intent=False) as (workspace, _artifact):
            missing_intent = accept_phase(workspace)
            self.assertEqual(missing_intent.returncode, 2, missing_intent.stderr)
            self.assertIn("intent", missing_intent.stderr.lower())

        attempt_id = "33333333-3333-4333-8333-333333333333"
        with seeded_phase_workspace() as (workspace, _artifact):
            epoch = json.loads((workspace / "state.json").read_text())["agent_journal"]
            requested = record_for_epoch(
                workspace, epoch, "agent-spawn-requested",
                "--agent-id", attempt_id,
                "--agent-role", "phase-worker",
                "--action-code", "spawn-agent",
                "--status-code", "requested",
                "--reason-code", "phase-contract",
            )
            self.assertEqual(requested.returncode, 0, requested.stderr)
            open_ledger = accept_phase(workspace)
            self.assertEqual(open_ledger.returncode, 2, open_ledger.stderr)
            self.assertIn("ledger", open_ledger.stderr.lower())

    def test_high_repository_inventory_is_chunk_bound_recoverable_and_resolvable(self) -> None:
        """>64 validated phase artifacts bind in deterministic system chunks."""
        with tempfile.TemporaryDirectory() as temporary:
            workspace = pathlib.Path(temporary) / "workspace"
            (workspace / "findings").mkdir(parents=True)
            (workspace / "output").mkdir()
            slugs = [f"repo-{index:02d}" for index in range(40)]
            state = {
                "date": DATE,
                "current_phase": "phase-1",
                "mode": "review",
                "phase_status": {},
                "history": [],
                "git_checkpoints": [],
                "repo_slugs": slugs,
            }
            manifest = {
                "schema_version": "1", "date": DATE,
                "phases_completed": [], "checkpoints": [],
            }
            (workspace / "state.json").write_text(json.dumps(state, indent=2) + "\n")
            (workspace / "output" / "session-manifest.json").write_text(
                json.dumps(manifest, indent=2) + "\n"
            )
            for slug in slugs:
                directory = workspace / "findings" / slug
                directory.mkdir()
                (directory / f"dataflow-{DATE}.md").write_text(
                    "# Scope\n\n## External Attack Surface\n\nvalidated\n"
                )
                (directory / f"owasp-context-{DATE}.md").write_text(
                    "No OWASP frameworks in scope.\n"
                )
                write_public_phase1_support(directory)
            unvalidated = workspace / "output" / "unvalidated-canary.txt"
            unvalidated.write_text("must not bind\n")
            initialized = journal(
                "init", "--workspace", str(workspace), "--date", DATE,
                "--port", "codex",
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            intent = journal("record-intent", "--workspace", str(workspace))
            self.assertEqual(intent.returncode, 0, intent.stderr)
            for command in (
                ("git", "init", "-q"),
                ("git", "config", "user.name", "NightFalcon Test"),
                ("git", "config", "user.email", "nightfalcon@example.invalid"),
                ("git", "add", "."),
                ("git", "commit", "-qm", "high repository fixture"),
            ):
                result = run(*command, cwd=workspace)
                self.assertEqual(result.returncode, 0, result.stderr)
            environment = os.environ.copy()
            environment["NIGHTFALCON_JOURNAL_FAILPOINT"] = "after-intent"
            interrupted = journal(
                "accept-phase", "--workspace", str(workspace),
                "--phase", "phase-1", "--timestamp", "2026-08-26T12:00:00Z",
                env=environment,
            )
            self.assertEqual(interrupted.returncode, 2, interrupted.stderr)
            recovered = reconcile(workspace)
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            parsed = json.loads(journal(
                "validate", "--workspace", str(workspace)
            ).stdout)

            chunks = [
                event for event in parsed["events"]
                if event["event_type"] == "artifact-bound"
            ]
            bindings = [ref for event in chunks for ref in event["artifact_refs"]]
            self.assertEqual(
                [len(event["artifact_refs"]) for event in chunks], [64, 64, 32]
            )
            self.assertEqual(len(bindings), 160)
            self.assertTrue(
                all(
                    any(
                        binding["path"]
                        == f"findings/{slug}/dependency-inventory-{DATE}.json"
                        for binding in bindings
                    )
                    for slug in slugs
                )
            )
            self.assertNotIn(
                unvalidated.relative_to(workspace).as_posix(),
                {binding["path"] for binding in bindings},
            )
            selected = bindings[-1]
            resolved = journal(
                "resolve-artifact", "--workspace", str(workspace),
                "--checkpoint-uuid", selected["checkpoint_uuid"],
                "--path", selected["path"], "--sha256", selected["sha256"],
                text=False,
            )
            self.assertEqual(resolved.returncode, 0, resolved.stderr)

    def test_phase2_acceptance_binds_cross_repository_topology(self) -> None:
        with direct_phase_workspace("phase-2") as workspace:
            directory = workspace / "findings" / "demo"
            directory.mkdir(parents=True)
            (directory / f"dataflow-{DATE}.md").write_text(
                "# Data flow\n\n## Data Flow Map\n\n- Flow-1\n"
            )
            (directory / f"dataflow-{DATE}.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "repo_slug": "demo",
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
            accepted = journal(
                "accept-phase",
                "--workspace",
                str(workspace),
                "--phase",
                "phase-2",
                "--timestamp",
                "2026-08-26T12:00:00Z",
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            parsed = json.loads(
                journal("validate", "--workspace", str(workspace)).stdout
            )
            bound_paths = {
                reference["path"]
                for event in parsed["events"]
                if event["event_type"] == "artifact-bound"
                for reference in event["artifact_refs"]
            }
            self.assertIn(
                f"findings/cross-repository-topology-{DATE}.json", bound_paths
            )

    def test_selector_bindings_and_unresolved_refs_are_repository_checkpoint_scoped(self) -> None:
        """Same finding IDs and later versions remain exact accepted references."""
        with tempfile.TemporaryDirectory() as temporary:
            workspace = pathlib.Path(temporary) / "workspace"
            (workspace / "findings").mkdir(parents=True)
            (workspace / "output").mkdir()
            (workspace / "input").mkdir()
            slugs = ["alpha", "beta"]
            state = {
                "date": DATE, "current_phase": "phase-0", "mode": "review",
                "phase_status": {}, "history": [], "git_checkpoints": [],
                "repo_slugs": slugs,
            }
            manifest = {
                "schema_version": "1", "date": DATE,
                "phases_completed": [], "checkpoints": [],
            }
            (workspace / "state.json").write_text(json.dumps(state, indent=2) + "\n")
            (workspace / "output" / "session-manifest.json").write_text(
                json.dumps(manifest, indent=2) + "\n"
            )
            (workspace / "output" / f"run-log-{DATE}.md").write_text("# Run log\n")
            (workspace / "input" / f"repos-{DATE}.txt").write_text("alpha\nbeta\n")
            paths = {}
            for slug in slugs:
                (workspace / "sourcecode" / slug).mkdir(parents=True)
                directory = workspace / "findings" / slug
                directory.mkdir()
                path = directory / f"candidates-{DATE}.md"
                path.write_text(
                    f"# Candidates\n\n## CANDIDATE-1 — F-001\n\n{slug} version one\n"
                )
                paths[slug] = path
            initialized = journal(
                "init", "--workspace", str(workspace), "--date", DATE,
                "--port", "codex",
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertEqual(
                journal("record-intent", "--workspace", str(workspace)).returncode,
                0,
            )
            for command in (
                ("git", "init", "-q"),
                ("git", "config", "user.name", "NightFalcon Test"),
                ("git", "config", "user.email", "nightfalcon@example.invalid"),
                ("git", "add", "."),
                ("git", "commit", "-qm", "selector fixture"),
            ):
                result = run(*command, cwd=workspace)
                self.assertEqual(result.returncode, 0, result.stderr)

            def record_unresolved(slug: str) -> subprocess.CompletedProcess:
                current = json.loads((workspace / "state.json").read_text())
                epoch = current["agent_journal"]
                return record_for_epoch(
                    workspace, epoch, "decision-recorded",
                    "--agent-role", "orchestrator",
                    "--action-code", "record-decision",
                    "--decision-code", "unresolved",
                    "--reason-code", "evidence-gap",
                    "--artifact-refs", json.dumps([{
                        "path": paths[slug].relative_to(workspace).as_posix(),
                        "selector": "F-001",
                    }]),
                )

            for slug in slugs:
                recorded = record_unresolved(slug)
                self.assertEqual(recorded.returncode, 0, recorded.stderr)
            phase0 = journal(
                "accept-phase", "--workspace", str(workspace),
                "--phase", "phase-0", "--timestamp", "2026-08-26T12:00:00Z",
            )
            self.assertEqual(phase0.returncode, 0, phase0.stderr)

            for slug in slugs:
                directory = workspace / "findings" / slug
                (directory / f"dataflow-{DATE}.md").write_text(
                    "# Scope\n\n## External Attack Surface\n\nvalidated\n"
                )
                (directory / f"owasp-context-{DATE}.md").write_text(
                    "No OWASP frameworks in scope.\n"
                )
                write_public_phase1_support(directory)
            paths["alpha"].write_text(
                "# Candidates\n\n## CANDIDATE-1 — F-001\n\nalpha version two\n"
            )
            self.assertEqual(
                journal("record-intent", "--workspace", str(workspace)).returncode,
                0,
            )
            recorded = record_unresolved("alpha")
            self.assertEqual(recorded.returncode, 0, recorded.stderr)
            phase1 = journal(
                "accept-phase", "--workspace", str(workspace),
                "--phase", "phase-1", "--timestamp", "2026-08-26T12:10:00Z",
            )
            self.assertEqual(phase1.returncode, 0, phase1.stderr)

            self.assertEqual(
                journal("record-intent", "--workspace", str(workspace)).returncode,
                0,
            )
            abandoned = record_unresolved("beta")
            self.assertEqual(abandoned.returncode, 0, abandoned.stderr)
            self.assertEqual(reconcile(workspace).returncode, 0)
            projection = handoff_projection(workspace)
            scoped = projection["unresolved_ids"]

            self.assertEqual(len(scoped), 3)
            self.assertEqual(
                [(entry["repo_slug"], entry["selector"]) for entry in scoped],
                [("alpha", "F-001"), ("alpha", "F-001"), ("beta", "F-001")],
            )
            self.assertEqual(len({entry["checkpoint_uuid"] for entry in scoped}), 2)
            self.assertTrue(all("sha256" in entry and "path" in entry for entry in scoped))
            paths["alpha"].write_text("later unaccepted replacement\n")
            for entry in scoped:
                resolved = journal(
                    "resolve-artifact", "--workspace", str(workspace),
                    "--checkpoint-uuid", entry["checkpoint_uuid"],
                    "--path", entry["path"], "--sha256", entry["sha256"],
                    "--selector", entry["selector"], text=False,
                )
                self.assertEqual(resolved.returncode, 0, resolved.stderr)
                self.assertIn(b"F-001", resolved.stdout)
            rejected = journal(
                "resolve-artifact", "--workspace", str(workspace),
                "--checkpoint-uuid", scoped[0]["checkpoint_uuid"],
                "--path", scoped[0]["path"], "--sha256", scoped[0]["sha256"],
                "--selector", "F-002", text=False,
            )
            self.assertEqual(rejected.returncode, 2, rejected.stderr)
            self.assertIn(b"accepted binding", rejected.stderr)

    def test_init_intent_persists_exact_self_verifying_participants_and_preserves_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = seed_init_workspace(temporary)
            state_path = workspace / "state.json"
            old_state = state_path.read_bytes()
            unpublished = workspace / f".{INIT_INTENT_NAME}.unpublished"
            unpublished.write_bytes(b"crash-before-intent-visibility")
            unpublished.chmod(0o600)

            interrupted = init_with_failpoint(workspace, "after-init-intent")
            self.assertEqual(interrupted.returncode, 2, interrupted.stderr)
            intent_path = workspace / INIT_INTENT_NAME
            journal_path = workspace / "output" / f"agent-conversation-{DATE}.md"
            self.assertTrue(intent_path.exists())
            self.assertFalse(journal_path.exists())
            self.assertEqual(state_path.read_bytes(), old_state)
            self.assertEqual(
                unpublished.read_bytes(), b"crash-before-intent-visibility"
            )
            self.assertEqual(stat.S_IMODE(intent_path.stat().st_mode), 0o600)

            intent, recorded_old, new_state, full_journal = decode_init_intent(
                workspace
            )
            self.assertEqual(set(intent), {
                "schema_version", "operation", "date", "session_id",
                "epoch_id", "checkpoint_uuid", "journal_filename",
                "state_mode", "journal_mode", "state", "journal",
            })
            self.assertEqual(intent["schema_version"], "1")
            self.assertEqual(intent["operation"], "initialize")
            self.assertEqual(intent["date"], DATE)
            self.assertEqual(intent["journal_filename"], journal_path.name)
            self.assertEqual(intent["state_mode"], 0o640)
            self.assertEqual(intent["journal_mode"], 0o600)
            self.assertEqual(recorded_old, old_state)
            self.assertEqual(
                hashlib.sha256(recorded_old).hexdigest(),
                intent["state"]["old_sha256"],
            )
            self.assertEqual(
                hashlib.sha256(new_state).hexdigest(),
                intent["state"]["new_sha256"],
            )
            self.assertEqual(
                hashlib.sha256(full_journal).hexdigest(),
                intent["journal"]["full_sha256"],
            )
            for field in ("session_id", "epoch_id", "checkpoint_uuid"):
                self.assertRegex(intent[field], r"^[A-Za-z0-9._:-]{1,128}$")

            recovered = journal(
                "init", "--workspace", str(workspace), "--date", DATE,
                "--port", "codex",
            )
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertEqual(state_path.read_bytes(), new_state)
            self.assertEqual(journal_path.read_bytes(), full_journal)
            self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o640)
            self.assertEqual(stat.S_IMODE(journal_path.stat().st_mode), 0o600)
            self.assertFalse(intent_path.exists())
            self.assertEqual(
                unpublished.read_bytes(), b"crash-before-intent-visibility"
            )

            state = json.loads(new_state)
            self.assertEqual(state["port_extension"], {"keep": True})
            self.assertEqual(state["repo_slugs"], ["demo", "second"])
            parsed = journal("validate", "--workspace", str(workspace))
            self.assertEqual(parsed.returncode, 0, parsed.stderr)
            events = json.loads(parsed.stdout)["events"]
            self.assertEqual(
                [event["event_type"] for event in events],
                ["run-initialized", "user-intent-recorded"],
            )

            before_repeat = (state_path.read_bytes(), journal_path.read_bytes())
            repeated = journal(
                "init", "--workspace", str(workspace), "--date", DATE,
                "--port", "cursor",
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(
                (state_path.read_bytes(), journal_path.read_bytes()), before_repeat
            )

    def test_init_finishes_preflight_before_creating_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = pathlib.Path(temporary) / "workspace"
            workspace.mkdir()
            (workspace / "state.json").write_text(json.dumps({
                "date": DATE,
                "current_phase": "phase-0",
                "mode": "review",
                "phase_status": {},
                "history": [],
                "git_checkpoints": [],
                "repo_slugs": ["demo"],
            }, indent=2) + "\n")
            # A conflicting acceptance intent is a preflight failure.  It must
            # be detected before initialization creates any participant path.
            (workspace / ".nightfalcon-journal-transaction.json").write_text(
                "{}\n"
            )

            initialized = journal(
                "init", "--workspace", str(workspace), "--date", DATE,
                "--port", "codex",
            )

            self.assertEqual(initialized.returncode, 2, initialized.stderr)
            self.assertIn("acceptance transaction", initialized.stderr)
            self.assertFalse((workspace / "output").exists())

    def test_initialized_legacy_null_date_is_rejected_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = seed_init_workspace(temporary)
            initialized = journal(
                "init", "--workspace", str(workspace), "--date", DATE,
                "--port", "codex",
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            state_path = workspace / "state.json"
            state = json.loads(state_path.read_text())
            state["date"] = None
            state_path.write_text(json.dumps(state, indent=2) + "\n")
            state_path.chmod(0o640)
            before = filesystem_snapshot(
                state_path,
                workspace / "output",
                workspace / INIT_INTENT_NAME,
            )

            repeated = journal(
                "init", "--workspace", str(workspace), "--date", DATE,
                "--port", "codex",
            )

            self.assertEqual(repeated.returncode, 2, repeated.stderr)
            self.assertIn("initialized state date cannot be null", repeated.stderr)
            self.assertEqual(
                filesystem_snapshot(
                    state_path,
                    workspace / "output",
                    workspace / INIT_INTENT_NAME,
                ),
                before,
            )

    def test_authoritative_files_reject_hardlinks(self) -> None:
        for participant in ("state", "manifest", "journal"):
            with self.subTest(participant=participant), seeded_phase_workspace() as (
                workspace, _artifact,
            ):
                paths = {
                    "state": workspace / "state.json",
                    "manifest": workspace / "output" / "session-manifest.json",
                    "journal": workspace / "output" / f"agent-conversation-{DATE}.md",
                }
                path = paths[participant]
                os.link(path, path.with_name(f"{path.name}.hardlink"))

                validated = journal("validate", "--workspace", str(workspace))

                self.assertEqual(validated.returncode, 2, validated.stderr)
                self.assertIn("hardlinked", validated.stderr)

    def test_transaction_decoder_rejects_noncanonical_base64(self) -> None:
        with seeded_phase_workspace() as (workspace, _artifact):
            failed = accept_phase(workspace, failpoint="after-intent")
            self.assertEqual(failed.returncode, 2, failed.stderr)
            intent_path = workspace / ".nightfalcon-journal-transaction.json"
            intent = json.loads(intent_path.read_text())
            candidates = [
                (intent["state"], "old_base64"),
                (intent["state"], "new_base64"),
                (intent["manifest"], "old_base64"),
                (intent["manifest"], "new_base64"),
                (intent["journal"]["prepared"], "base64"),
                (intent["journal"]["accepted"], "base64"),
                (intent["journal"]["recovery"], "base64"),
            ]
            container, key = next(
                (container, key)
                for container, key in candidates
                if container[key].endswith("=")
            )
            encoded = container[key]
            alphabet = (
                "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
            )
            padding = len(encoded) - len(encoded.rstrip("="))
            self.assertIn(padding, (1, 2))
            index = len(encoded) - padding - 1
            value = alphabet.index(encoded[index])
            # Flip one unused padding bit.  Python's strict decoder accepts the
            # spelling and yields the same bytes, but it is not canonical.
            replacement = alphabet[value + 1]
            noncanonical = encoded[:index] + replacement + encoded[index + 1:]
            self.assertEqual(
                base64.b64decode(noncanonical, validate=True),
                base64.b64decode(encoded, validate=True),
            )
            container[key] = noncanonical
            intent_path.write_text(
                json.dumps(intent, sort_keys=True, indent=2) + "\n"
            )
            intent_path.chmod(0o600)

            recovered = reconcile(workspace)

            self.assertEqual(recovered.returncode, 2, recovered.stderr)
            self.assertIn("canonical base64", recovered.stderr)

    def test_init_recovery_accepts_only_missing_or_exact_journal_prefix_with_old_or_new_state(self) -> None:
        cases = (
            ("missing-old-state", None, "old"),
            ("partial-header-old-state", 17, "old"),
            ("partial-event-old-state", "event", "old"),
            ("partial-event-new-state", "event", "new"),
            ("almost-full-new-state", -1, "new"),
            ("full-old-state", "full", "old"),
        )
        for case, prefix, state_stage in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                workspace = seed_init_workspace(temporary)
                interrupted = init_with_failpoint(
                    workspace, "after-init-intent"
                )
                self.assertEqual(interrupted.returncode, 2, interrupted.stderr)
                intent, old_state, new_state, full_journal = decode_init_intent(
                    workspace
                )
                state_path = workspace / "state.json"
                journal_path = (
                    workspace / "output" / intent["journal_filename"]
                )
                if state_stage == "new":
                    state_path.write_bytes(new_state)
                    state_path.chmod(intent["state_mode"])
                else:
                    self.assertEqual(state_path.read_bytes(), old_state)
                if prefix is not None:
                    if prefix == "event":
                        event_start = full_journal.index(b"```json\n")
                        prefix_length = event_start + 13
                    elif prefix == "full":
                        prefix_length = len(full_journal)
                    elif prefix == -1:
                        prefix_length = len(full_journal) - 1
                    else:
                        prefix_length = prefix
                    journal_path.write_bytes(full_journal[:prefix_length])
                    journal_path.chmod(intent["journal_mode"])

                recovered = journal(
                    "init", "--workspace", str(workspace), "--date", DATE,
                    "--port", "claude",
                )
                self.assertEqual(recovered.returncode, 0, recovered.stderr)
                self.assertEqual(state_path.read_bytes(), new_state)
                self.assertEqual(journal_path.read_bytes(), full_journal)
                self.assertFalse((workspace / INIT_INTENT_NAME).exists())

    def test_init_atomic_state_replace_preserves_special_permission_bits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = seed_init_workspace(temporary)
            state_path = workspace / "state.json"
            state_path.chmod(0o4750)

            initialized = journal(
                "init", "--workspace", str(workspace), "--date", DATE,
                "--port", "codex",
            )

            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o4750)

    def test_init_recovery_rejects_ambiguous_participants_before_mutation(self) -> None:
        cases = (
            "altered-state",
            "non-prefix-journal",
            "state-mode",
            "journal-mode",
            "state-symlink",
            "journal-symlink",
            "intent-mode",
            "intent-symlink",
            "intent-content",
            "invalid-manifest",
            "acceptance-intent-coexists",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                workspace = seed_init_workspace(temporary)
                interrupted = init_with_failpoint(
                    workspace, "after-init-intent"
                )
                self.assertEqual(interrupted.returncode, 2, interrupted.stderr)
                intent, _, _, full_journal = decode_init_intent(workspace)
                state_path = workspace / "state.json"
                journal_path = workspace / "output" / intent["journal_filename"]
                intent_path = workspace / INIT_INTENT_NAME
                extra_paths: list[pathlib.Path] = []

                if case == "altered-state":
                    state_path.write_bytes(state_path.read_bytes() + b" ")
                elif case == "non-prefix-journal":
                    journal_path.write_bytes(b"not-an-exact-prefix")
                    journal_path.chmod(0o600)
                elif case == "state-mode":
                    state_path.chmod(0o600)
                elif case == "journal-mode":
                    journal_path.write_bytes(full_journal[:17])
                    journal_path.chmod(0o640)
                elif case == "state-symlink":
                    target = workspace / "state-original.json"
                    state_path.rename(target)
                    state_path.symlink_to(target.name)
                    extra_paths.append(target)
                elif case == "journal-symlink":
                    target = workspace / "journal-target"
                    target.write_bytes(full_journal[:17])
                    journal_path.symlink_to(target)
                    extra_paths.append(target)
                elif case == "intent-mode":
                    intent_path.chmod(0o640)
                elif case == "intent-symlink":
                    target = workspace / "init-intent-original.json"
                    intent_path.rename(target)
                    intent_path.symlink_to(target.name)
                    extra_paths.append(target)
                elif case == "intent-content":
                    value = json.loads(intent_path.read_text())
                    value["session_id"] = "different-session"
                    intent_path.write_text(
                        json.dumps(value, sort_keys=True, indent=2) + "\n"
                    )
                    intent_path.chmod(0o600)
                elif case == "invalid-manifest":
                    manifest = (
                        workspace / "output" / "session-manifest.json"
                    )
                    value = json.loads(manifest.read_text())
                    value["date"] = "2026-08-25"
                    manifest.write_text(json.dumps(value, indent=2) + "\n")
                    extra_paths.append(manifest)
                else:
                    acceptance = workspace / ".nightfalcon-journal-transaction.json"
                    acceptance.write_text("{}\n")
                    acceptance.chmod(0o600)
                    extra_paths.append(acceptance)

                participants = [
                    state_path, journal_path, intent_path, *extra_paths,
                ]
                before = filesystem_snapshot(*participants)
                recovered = journal(
                    "init", "--workspace", str(workspace), "--date", DATE,
                    "--port", "codex",
                )
                self.assertEqual(recovered.returncode, 2, recovered.stderr)
                self.assertEqual(filesystem_snapshot(*participants), before)
                self.assertIn("init", recovered.stderr.lower())

    def test_init_rejects_preexisting_journal_without_intent_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = seed_init_workspace(temporary)
            state_path = workspace / "state.json"
            journal_path = workspace / "output" / f"agent-conversation-{DATE}.md"
            journal_path.write_bytes(b"preexisting-journal")
            journal_path.chmod(0o600)
            before = filesystem_snapshot(state_path, journal_path)

            initialized = journal(
                "init", "--workspace", str(workspace), "--date", DATE,
                "--port", "codex",
            )
            self.assertEqual(initialized.returncode, 2, initialized.stderr)
            self.assertEqual(filesystem_snapshot(state_path, journal_path), before)
            self.assertFalse((workspace / INIT_INTENT_NAME).exists())

    def test_pending_init_intent_is_reconciled_by_authoritative_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = seed_init_workspace(temporary)
            interrupted = init_with_failpoint(
                workspace, "after-init-state-replace"
            )
            self.assertEqual(interrupted.returncode, 2, interrupted.stderr)
            state = json.loads((workspace / "state.json").read_text())
            journal_path = (
                workspace / "output" / state["agent_journal"]["filename"]
            )
            recorded = journal(
                "record-intent", "--workspace", str(workspace)
            )
            self.assertEqual(recorded.returncode, 0, recorded.stderr)
            self.assertFalse((workspace / INIT_INTENT_NAME).exists())
            accepted = journal(
                "accept-phase", "--workspace", str(workspace),
                "--phase", "phase-0", "--timestamp", "2026-08-26T12:00:00Z",
            )
            self.assertEqual(accepted.returncode, 2, accepted.stderr)
            self.assertIn("phase artifact validation", accepted.stderr)

            recovered = journal(
                "init", "--workspace", str(workspace), "--date", DATE,
                "--port", "cursor",
            )
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            recorded = journal(
                "record-intent", "--workspace", str(workspace)
            )
            self.assertEqual(recorded.returncode, 0, recorded.stderr)

    def test_reconcile_converges_every_acceptance_failpoint_before_worker_launch(self) -> None:
        for failpoint in FAILPOINTS:
            with self.subTest(failpoint=failpoint), seeded_phase_workspace() as (workspace, _):
                failed = accept_phase(workspace, failpoint=failpoint)
                self.assertNotEqual(failed.returncode, 0)
                marker = workspace / "worker-launched"
                recovered = reconcile(workspace)
                self.assertEqual(recovered.returncode, 0, recovered.stderr)
                self.assertFalse(marker.exists())
                self.assert_checkpoint_invariants(workspace)
                second = reconcile(workspace)
                self.assertEqual(second.returncode, 0, second.stderr)

    def test_resume_fills_missing_user_intent_once(self) -> None:
        with seeded_phase_workspace() as (workspace, _):
            before = json.loads((workspace / "state.json").read_text())
            old_epoch = before["agent_journal"]["epoch_id"]
            started = record_for_epoch(
                workspace, before["agent_journal"], "phase-started",
                "--agent-role", "orchestrator",
                "--action-code", "start-phase",
                "--status-code", "started",
                "--reason-code", "phase-contract",
            )
            self.assertEqual(started.returncode, 0, started.stderr)

            resumed = reconcile(workspace)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            after = json.loads((workspace / "state.json").read_text())
            current_epoch = after["agent_journal"]["epoch_id"]
            self.assertNotEqual(current_epoch, old_epoch)

            first = journal(
                "record-intent", "--workspace", str(workspace)
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            path = workspace / "output" / f"agent-conversation-{DATE}.md"
            first_bytes = path.read_bytes()
            repeated = journal(
                "record-intent", "--workspace", str(workspace)
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(repeated.stdout, first.stdout)
            self.assertEqual(path.read_bytes(), first_bytes)

            validated = journal("validate", "--workspace", str(workspace))
            self.assertEqual(validated.returncode, 0, validated.stderr)
            intents = [
                event for event in json.loads(validated.stdout)["events"]
                if event["event_type"] == "user-intent-recorded"
            ]
            current_intents = [
                event for event in intents if event["epoch_id"] == current_epoch
            ]
            self.assertEqual(len(current_intents), 1)
            self.assertEqual(len(intents), 2)

    def test_phase_acceptance_selects_only_facts_from_accepted_resume_epoch(self) -> None:
        with seeded_phase_workspace() as (workspace, artifact):
            artifact.write_text(
                "# Candidates\n\n"
                "## CANDIDATE-1 — F-001\n\nabandoned evidence\n\n"
                "## CANDIDATE-2 — F-002\n\naccepted evidence\n"
            )
            epoch_a = json.loads((workspace / "state.json").read_text())["agent_journal"]
            abandoned_decision = record_for_epoch(
                workspace, epoch_a, "decision-recorded",
                "--agent-role", "phase-worker",
                "--action-code", "record-decision",
                "--decision-code", "deferred",
                "--reason-code", "evidence-gap",
                "--artifact-refs", json.dumps([{
                    "path": artifact.relative_to(workspace).as_posix(),
                    "selector": "F-001",
                }]),
            )
            self.assertEqual(abandoned_decision.returncode, 0, abandoned_decision.stderr)
            abandoned_lifecycle = record_completed_lifecycle(
                workspace, epoch_a,
                "aaaaaaaa-1111-4111-8111-111111111111",
                "phase-worker", {"abandoned": 7},
            )
            for result in abandoned_lifecycle:
                self.assertEqual(result.returncode, 0, result.stderr)

            resumed = reconcile(workspace)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            epoch_b = json.loads((workspace / "state.json").read_text())["agent_journal"]
            current_intent = journal(
                "record-intent", "--workspace", str(workspace)
            )
            self.assertEqual(current_intent.returncode, 0, current_intent.stderr)
            accepted_decision = record_for_epoch(
                workspace, epoch_b, "decision-recorded",
                "--agent-role", "validator",
                "--action-code", "record-decision",
                "--decision-code", "unresolved",
                "--reason-code", "phase-contract",
                "--artifact-refs", json.dumps([{
                    "path": artifact.relative_to(workspace).as_posix(),
                    "selector": "F-002",
                }]),
            )
            self.assertEqual(accepted_decision.returncode, 0, accepted_decision.stderr)
            accepted_lifecycle = record_completed_lifecycle(
                workspace, epoch_b,
                "bbbbbbbb-2222-4222-8222-222222222222",
                "validator", {"accepted": 2},
            )
            for result in accepted_lifecycle:
                self.assertEqual(result.returncode, 0, result.stderr)
            accepted = accept_phase(workspace)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)

            parsed = json.loads(journal(
                "validate", "--workspace", str(workspace)
            ).stdout)
            projection = handoff_projection(workspace)

        raw_epoch_ids = {event["epoch_id"] for event in parsed["events"]}
        self.assertIn(epoch_a["epoch_id"], raw_epoch_ids)
        self.assertIn(epoch_b["epoch_id"], raw_epoch_ids)
        self.assertEqual(projection["decision_counts"], {"unresolved": 1})
        self.assertEqual(
            [item["selector"] for item in projection["unresolved_ids"]],
            ["F-002"],
        )
        self.assertEqual(
            projection["agent_counts"],
            {"validator": {"completed": 1, "requested": 1, "started": 1}},
        )
        self.assertNotIn("F-001", json.dumps(projection))
        self.assertEqual(
            projection["interrupted_attempt"]["attempts"][0]["events"][-1]["counts"],
            {"abandoned": 7},
        )

    def test_accepted_facts_accumulate_across_multiple_phase_epochs(self) -> None:
        with seeded_phase_workspace() as (workspace, artifact):
            artifact.write_text(
                "# Candidates\n\n"
                "## CANDIDATE-1 — F-001\n\nphase zero\n\n"
                "## CANDIDATE-2 — F-002\n\nphase one\n"
            )
            phase_zero = json.loads(
                (workspace / "state.json").read_text()
            )["agent_journal"]
            first = record_for_epoch(
                workspace, phase_zero, "decision-recorded",
                "--agent-role", "orchestrator",
                "--action-code", "record-decision",
                "--decision-code", "deferred",
                "--reason-code", "evidence-gap",
                "--artifact-refs", json.dumps([{
                    "path": artifact.relative_to(workspace).as_posix(),
                    "selector": "F-001",
                }]),
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            accepted_zero = accept_phase(workspace)
            self.assertEqual(accepted_zero.returncode, 0, accepted_zero.stderr)

            phase_one_state = json.loads((workspace / "state.json").read_text())
            findings_directory = artifact.parent
            (findings_directory / f"dataflow-{DATE}.md").write_text(
                "# Scope\n\n## External Attack Surface\n\nvalidated\n"
            )
            (findings_directory / f"owasp-context-{DATE}.md").write_text(
                "No OWASP frameworks in scope.\n"
            )
            write_public_phase1_support(findings_directory)
            current_intent = journal(
                "record-intent", "--workspace", str(workspace)
            )
            self.assertEqual(current_intent.returncode, 0, current_intent.stderr)
            second = record_for_epoch(
                workspace, phase_one_state["agent_journal"], "decision-recorded",
                "--agent-role", "orchestrator",
                "--action-code", "record-decision",
                "--decision-code", "unresolved",
                "--reason-code", "evidence-gap",
                "--artifact-refs", json.dumps([{
                    "path": artifact.relative_to(workspace).as_posix(),
                    "selector": "F-002",
                }]),
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            accepted_one = journal(
                "accept-phase", "--workspace", str(workspace),
                "--phase", "phase-1",
                "--timestamp", "2026-08-26T13:00:00Z",
            )
            self.assertEqual(accepted_one.returncode, 0, accepted_one.stderr)

            projection = handoff_projection(workspace)

        self.assertEqual(
            projection["decision_counts"], {"deferred": 1, "unresolved": 1}
        )
        self.assertEqual(
            [item["selector"] for item in projection["unresolved_ids"]],
            ["F-001", "F-002"],
        )
        self.assertEqual(
            [checkpoint["phase"] for checkpoint in projection["checkpoints"]],
            ["phase-0", "phase-1"],
        )

    def test_worker_forged_phase_accepted_does_not_promote_abandoned_epoch(self) -> None:
        with seeded_phase_workspace() as (workspace, artifact):
            epoch_a = json.loads((workspace / "state.json").read_text())["agent_journal"]
            abandoned = record_for_epoch(
                workspace, epoch_a, "decision-recorded",
                "--agent-role", "orchestrator",
                "--action-code", "record-decision",
                "--decision-code", "deferred",
                "--reason-code", "evidence-gap",
                "--artifact-refs", json.dumps([{
                    "path": artifact.relative_to(workspace).as_posix(),
                    "selector": "F-001",
                }]),
            )
            forged = record_for_epoch(
                workspace, epoch_a, "phase-accepted",
                "--agent-role", "orchestrator",
                "--action-code", "accept-phase",
                "--status-code", "accepted",
                "--reason-code", "acceptance-gate",
            )
            self.assertEqual(abandoned.returncode, 0, abandoned.stderr)
            self.assertEqual(forged.returncode, 2, forged.stderr)
            resumed = reconcile(workspace)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            current_intent = journal(
                "record-intent", "--workspace", str(workspace)
            )
            self.assertEqual(current_intent.returncode, 0, current_intent.stderr)
            accepted = accept_phase(workspace)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)

            projection = handoff_projection(workspace)

        self.assertEqual(projection["decision_counts"], {})
        self.assertEqual(projection["unresolved_ids"], [])
        self.assertNotIn("F-001", json.dumps(projection))

    def test_duplicate_canonical_acceptance_candidates_fail_closed(self) -> None:
        with seeded_phase_workspace() as (workspace, _):
            accepted = accept_phase(workspace)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            parsed = json.loads(journal(
                "validate", "--workspace", str(workspace)
            ).stdout)
            events = parsed["events"]
            prepared_index = next(
                index for index, event in enumerate(events)
                if event["event_type"] == "phase-acceptance-prepared"
            )
            prepared, transition = events[prepared_index:prepared_index + 2]
            suffix = events[prepared_index + 2:]
            forged_prepared = json.loads(json.dumps(prepared))
            forged_transition = json.loads(json.dumps(transition))
            forged_prepared["epoch_id"] = "forged-old-epoch"
            forged_transition["epoch_id"] = "forged-old-epoch"
            rewrite_accepted_events(
                workspace,
                events[:prepared_index]
                + [forged_prepared, forged_transition, prepared, transition]
                + suffix,
            )

            validated = journal("validate", "--workspace", str(workspace))
            reconciled = reconcile(workspace)

        self.assertEqual(validated.returncode, 2)
        self.assertIn("ambiguous phase acceptance", validated.stderr)
        self.assertEqual(reconciled.returncode, 2)
        self.assertIn("ambiguous phase acceptance", reconciled.stderr)

    def test_acceptance_pair_set_must_exactly_match_authoritative_checkpoints(self) -> None:
        cases = (
            "missing-pair", "extra-pair", "noncanonical-pair",
            "state-extra-checkpoint", "manifest-extra-checkpoint",
        )
        for case in cases:
            with self.subTest(case=case), seeded_phase_workspace() as (workspace, _):
                accepted = accept_phase(workspace)
                self.assertEqual(accepted.returncode, 0, accepted.stderr)
                parsed = json.loads(journal(
                    "validate", "--workspace", str(workspace)
                ).stdout)
                events = parsed["events"]
                prepared_index = next(
                    index for index, event in enumerate(events)
                    if event["event_type"] == "phase-acceptance-prepared"
                )
                prepared, transition = events[prepared_index:prepared_index + 2]
                suffix = events[prepared_index + 2:]
                if case == "missing-pair":
                    rewrite_accepted_events(
                        workspace, events[:prepared_index] + suffix
                    )
                elif case == "extra-pair":
                    extra_prepared = json.loads(json.dumps(prepared))
                    extra_transition = json.loads(json.dumps(transition))
                    for event in (extra_prepared, extra_transition):
                        event["checkpoint_uuid"] = "extra-checkpoint"
                        for ref in event["artifact_refs"]:
                            ref["checkpoint_uuid"] = "extra-checkpoint"
                    rewrite_accepted_events(
                        workspace,
                        events[:prepared_index]
                        + [extra_prepared, extra_transition, prepared, transition]
                        + suffix,
                    )
                elif case == "noncanonical-pair":
                    transition["action_code"] = "record-decision"
                    rewrite_accepted_events(
                        workspace,
                        events[:prepared_index] + [prepared, transition] + suffix,
                    )
                else:
                    target = (
                        workspace / "state.json"
                        if case == "state-extra-checkpoint"
                        else workspace / "output" / "session-manifest.json"
                    )
                    value = json.loads(target.read_text())
                    key = "git_checkpoints" if target.name == "state.json" else "checkpoints"
                    value.setdefault(key, []).append({
                        "phase": "phase-0",
                        "checkpoint_uuid": f"{case}-uuid",
                        "timestamp": "2026-08-26T12:00:00Z",
                    })
                    target.write_text(json.dumps(value, indent=2) + "\n")

                results = {
                    "validate": journal("validate", "--workspace", str(workspace)),
                    "context": journal("context", "--workspace", str(workspace)),
                    "reconcile": reconcile(workspace),
                }

            for command, result in results.items():
                with self.subTest(case=case, command=command):
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertTrue(
                        "phase acceptance" in result.stderr
                        or "noncanonical code mapping" in result.stderr,
                        result.stderr,
                    )

    def test_pending_transaction_rejects_changed_bound_artifact(self) -> None:
        with seeded_phase_workspace() as (workspace, artifact):
            failed = accept_phase(workspace, failpoint="after-intent")
            self.assertNotEqual(failed.returncode, 0)
            artifact.write_text("changed while acceptance was pending\n")
            recovered = reconcile(workspace)
            self.assertEqual(recovered.returncode, 2)
            self.assertIn("bound artifact changed", recovered.stderr)
            self.assertTrue((workspace / ".nightfalcon-journal-transaction.json").exists())

    def test_reconcile_completes_torn_prepared_and_accepted_event_writes(self) -> None:
        cases = (
            ("prepared", "after-intent"),
            ("accepted", "after-prepared-append"),
        )
        for event, failpoint in cases:
            with self.subTest(event=event):
                with seeded_phase_workspace() as (prototype, _):
                    failed = accept_phase(prototype, failpoint=failpoint)
                    self.assertNotEqual(failed.returncode, 0)
                    exact = transaction_event_bytes(prototype, event)
                prefix_lengths = (1, len(exact) // 2, len(exact) - 1)
                for prefix_length in prefix_lengths:
                    with self.subTest(event=event, prefix_length=prefix_length), \
                            seeded_phase_workspace() as (workspace, _):
                        failed = accept_phase(workspace, failpoint=failpoint)
                        self.assertNotEqual(failed.returncode, 0)
                        torn = transaction_event_bytes(workspace, event)[:prefix_length]
                        journal_path = workspace / "output" / f"agent-conversation-{DATE}.md"
                        with journal_path.open("ab") as handle:
                            handle.write(torn)
                            handle.flush()
                            os.fsync(handle.fileno())
                        initialized = journal(
                            "init", "--workspace", str(workspace),
                            "--date", DATE, "--port", "codex",
                        )
                        self.assertEqual(initialized.returncode, 0, initialized.stderr)
                        recovered = reconcile(workspace)
                        self.assertEqual(recovered.returncode, 0, recovered.stderr)
                        self.assert_checkpoint_invariants(workspace)

    def test_pending_transaction_fences_old_epoch_record_after_early_failure(self) -> None:
        with seeded_phase_workspace() as (workspace, _):
            state = json.loads((workspace / "state.json").read_text())
            failed = accept_phase(workspace, failpoint="after-intent")
            self.assertNotEqual(failed.returncode, 0)
            journal_path = workspace / "output" / f"agent-conversation-{DATE}.md"
            before = journal_path.read_bytes()
            recorded = journal(
                "record", "--workspace", str(workspace),
                "--event-type", "phase-started",
                "--session-id", state["agent_journal"]["session_id"],
                "--phase", "phase-0",
                "--epoch-id", state["agent_journal"]["epoch_id"],
            )
            self.assertEqual(recorded.returncode, 2)
            self.assertIn("pending acceptance transaction", recorded.stderr)
            self.assertEqual(journal_path.read_bytes(), before)

    def test_reconcile_preflights_ambiguous_manifest_before_any_mutation(self) -> None:
        with seeded_phase_workspace() as (workspace, _):
            failed = accept_phase(workspace, failpoint="after-intent")
            self.assertNotEqual(failed.returncode, 0)
            manifest_path = workspace / "output" / "session-manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["ambiguous_external_change"] = True
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
            journal_path = workspace / "output" / f"agent-conversation-{DATE}.md"
            state_path = workspace / "state.json"
            before = {
                "journal": journal_path.read_bytes(),
                "state": state_path.read_bytes(),
                "manifest": manifest_path.read_bytes(),
            }
            recovered = reconcile(workspace)
            self.assertEqual(recovered.returncode, 2)
            self.assertIn("manifest", recovered.stderr)
            self.assertEqual(journal_path.read_bytes(), before["journal"])
            self.assertEqual(state_path.read_bytes(), before["state"])
            self.assertEqual(manifest_path.read_bytes(), before["manifest"])

    def test_checkpoint_commit_hook_cannot_strip_required_trailer(self) -> None:
        with seeded_phase_workspace() as (workspace, _):
            hook = workspace / ".git" / "hooks" / "commit-msg"
            hook.write_text(
                "#!/bin/sh\n"
                "grep -v '^NightFalcon-Checkpoint-ID:' \"$1\" > \"$1.tmp\"\n"
                "mv \"$1.tmp\" \"$1\"\n"
            )
            hook.chmod(0o755)
            accepted = accept_phase(workspace)
            self.assertEqual(accepted.returncode, 2)
            self.assertIn("checkpoint trailer", accepted.stderr)
            self.assertTrue((workspace / ".nightfalcon-journal-transaction.json").exists())
            state = json.loads((workspace / "state.json").read_text())
            ref = (
                "refs/nightfalcon/checkpoints/"
                f"{state['agent_journal']['session_id']}/phase-0"
            )
            missing_ref = run("git", "rev-parse", "--verify", "--quiet", ref, cwd=workspace)
            self.assertNotEqual(missing_ref.returncode, 0)
            hook.unlink()
            recovered = reconcile(workspace)
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assert_checkpoint_invariants(workspace)

    def test_transaction_intent_persists_exact_self_verifying_transition_bytes(self) -> None:
        with seeded_phase_workspace() as (workspace, _):
            failed = accept_phase(workspace, failpoint="after-intent")
            self.assertNotEqual(failed.returncode, 0)
            intent_path = workspace / ".nightfalcon-journal-transaction.json"
            self.assertTrue(intent_path.exists(), "acceptance did not persist its intent")
            intent = json.loads(intent_path.read_text())
            for transition in ("state", "manifest"):
                payload = intent[transition]
                for version in ("old", "new"):
                    exact = base64.b64decode(payload[f"{version}_base64"], validate=True)
                    self.assertEqual(
                        hashlib.sha256(exact).hexdigest(), payload[f"{version}_sha256"]
                    )
            for event in ("prepared", "accepted"):
                payload = intent["journal"][event]
                exact = base64.b64decode(payload["base64"], validate=True)
                self.assertEqual(hashlib.sha256(exact).hexdigest(), payload["sha256"])

    def test_late_writer_blocks_then_fails_stale_epoch(self) -> None:
        with seeded_phase_workspace() as (workspace, _):
            state = json.loads((workspace / "state.json").read_text())
            journal_state = state["agent_journal"]
            environment = os.environ.copy()
            barrier = workspace / "acceptance-barrier"
            environment["NIGHTFALCON_JOURNAL_TEST_BARRIER"] = str(barrier)
            acceptance = subprocess.Popen(
                [
                    "/usr/local/bin/python3", str(ENGINE), "accept-phase",
                    "--workspace", str(workspace), "--phase", "phase-0",
                    "--timestamp", "2026-08-26T12:00:00Z",
                ],
                env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            ready = pathlib.Path(str(barrier) + ".ready")
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "acceptance did not reach the lock-held barrier")
            writer = subprocess.Popen(
                [
                    "/usr/local/bin/python3", str(ENGINE), "record",
                    "--workspace", str(workspace), "--event-type", "phase-started",
                    "--session-id", journal_state["session_id"], "--phase", "phase-0",
                    "--epoch-id", journal_state["epoch_id"],
                ],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            time.sleep(0.1)
            self.assertIsNone(writer.poll(), "late writer did not block on workspace lock")
            barrier.write_text("continue\n")
            acceptance_stdout, acceptance_stderr = acceptance.communicate(timeout=10)
            self.assertEqual(acceptance.returncode, 0, acceptance_stderr or acceptance_stdout)
            _, writer_stderr = writer.communicate(timeout=10)
            self.assertEqual(writer.returncode, 2)
            self.assertIn("stale epoch", writer_stderr)

    def test_init_allows_pending_transaction_to_reconcile_before_worker_launch(self) -> None:
        with seeded_phase_workspace() as (workspace, _):
            failed = accept_phase(workspace, failpoint="after-state-replace")
            self.assertNotEqual(failed.returncode, 0)
            initialized = journal(
                "init", "--workspace", str(workspace), "--date", DATE, "--port", "codex"
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            recovered = reconcile(workspace)
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assert_checkpoint_invariants(workspace)

    def test_accepted_artifact_reference_resolves_original_after_later_edit(self) -> None:
        with seeded_phase_workspace() as (workspace, artifact):
            original = artifact.read_bytes()
            accepted = accept_phase(workspace)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            result = self.assert_checkpoint_invariants(workspace)
            bindings = [
                ref
                for event in result["events"]
                for ref in event.get("artifact_refs", [])
            ]
            binding = next(ref for ref in bindings if ref["path"] == artifact.relative_to(workspace).as_posix())
            artifact.write_text("later phase replacement\n")
            resolved = journal(
                "resolve-artifact", "--workspace", str(workspace),
                "--checkpoint-uuid", binding["checkpoint_uuid"],
                "--path", binding["path"], "--sha256", binding["sha256"],
                text=False,
            )
            self.assertEqual(resolved.returncode, 0, resolved.stderr)
            self.assertEqual(resolved.stdout, original)
            self.assertEqual(hashlib.sha256(resolved.stdout).hexdigest(), binding["sha256"])

    def test_typed_finding_selector_resolves_only_bound_checkpoint_section(self) -> None:
        with seeded_phase_workspace() as (workspace, artifact):
            artifact.write_text(
                "# Candidates\n\n"
                "## CANDIDATE-1 — F-001\n\nfirst evidence\n\n"
                "## CANDIDATE-2 — F-002\n\nsecond evidence\n"
            )
            epoch = json.loads((workspace / "state.json").read_text())["agent_journal"]
            decision = record_for_epoch(
                workspace, epoch, "decision-recorded",
                "--agent-role", "orchestrator",
                "--action-code", "record-decision",
                "--decision-code", "unresolved",
                "--reason-code", "evidence-gap",
                "--artifact-refs", json.dumps([{
                    "path": artifact.relative_to(workspace).as_posix(),
                    "selector": "F-001",
                }]),
            )
            self.assertEqual(decision.returncode, 0, decision.stderr)
            accepted = accept_phase(workspace)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            result = self.assert_checkpoint_invariants(workspace)
            binding = next(
                ref
                for event in result["events"]
                if event["event_type"] == "artifact-bound"
                for ref in event.get("artifact_refs", [])
                if ref["path"] == artifact.relative_to(workspace).as_posix()
                and ref.get("selector") == "F-001"
            )
            resolved = journal(
                "resolve-artifact", "--workspace", str(workspace),
                "--checkpoint-uuid", binding["checkpoint_uuid"],
                "--path", binding["path"], "--sha256", binding["sha256"],
                "--selector", "F-001", text=False,
            )
            self.assertEqual(resolved.returncode, 0, resolved.stderr)
            self.assertEqual(
                resolved.stdout,
                b"## CANDIDATE-1 \xe2\x80\x94 F-001\n\nfirst evidence\n\n",
            )

    def test_resolver_rejects_checkpoint_uuid_without_authoritative_ref(self) -> None:
        with seeded_phase_workspace() as (workspace, artifact):
            accepted = accept_phase(workspace)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            result = self.assert_checkpoint_invariants(workspace)
            binding = next(
                ref
                for event in result["events"]
                for ref in event.get("artifact_refs", [])
                if ref["path"] == artifact.relative_to(workspace).as_posix()
            )
            state = json.loads((workspace / "state.json").read_text())
            ref = (
                f"refs/nightfalcon/checkpoints/"
                f"{state['agent_journal']['session_id']}/phase-0"
            )
            deleted = run("git", "update-ref", "-d", ref, cwd=workspace)
            self.assertEqual(deleted.returncode, 0, deleted.stderr)
            resolved = journal(
                "resolve-artifact", "--workspace", str(workspace),
                "--checkpoint-uuid", binding["checkpoint_uuid"],
                "--path", binding["path"], "--sha256", binding["sha256"],
                text=False,
            )
            self.assertEqual(resolved.returncode, 2)
            self.assertIn(b"checkpoint authoritative ref", resolved.stderr)

    def test_three_ports_keep_byte_identical_transaction_engine(self) -> None:
        engines = [
            (port / "scripts" / "agent-journal.py").read_bytes()
            for port in PORTS.values()
        ]
        self.assertEqual(engines[0], engines[1])
        self.assertEqual(engines[0], engines[2])
        self.assertIn(b'TRANSACTION_VERSION = "2"', engines[0])


class CrossPortRecoveryTests(unittest.TestCase):
    assert_checkpoint_invariants = JournalRecoveryTests.assert_checkpoint_invariants

    def test_all_source_destination_ports_recover_every_init_failpoint_byte_identically(self) -> None:
        for source_name, source in PORTS.items():
            for failpoint in INIT_FAILPOINTS:
                with tempfile.TemporaryDirectory() as temporary:
                    prototype = seed_init_workspace(
                        temporary,
                        port_extension={source_name: True},
                    )
                    interrupted = init_with_failpoint(
                        prototype,
                        failpoint,
                        port=source,
                        port_name=source_name,
                    )
                    self.assertEqual(
                        interrupted.returncode, 2, interrupted.stderr
                    )
                    _, _, exact_state, exact_journal = decode_init_intent(
                        prototype
                    )
                    destination_results: set[tuple[bytes, bytes]] = set()
                    for destination_name, destination in PORTS.items():
                        with self.subTest(
                            source=source_name,
                            destination=destination_name,
                            failpoint=failpoint,
                        ):
                            workspace = (
                                pathlib.Path(temporary) /
                                f"recovered-{destination_name}"
                            )
                            shutil.copytree(
                                prototype, workspace, copy_function=shutil.copy2
                            )
                            recovered = run_journal(
                                destination,
                                "init",
                                "--workspace",
                                str(workspace),
                                "--date",
                                DATE,
                                "--port",
                                destination_name,
                            )
                            self.assertEqual(
                                recovered.returncode, 0, recovered.stderr
                            )
                            state_path = workspace / "state.json"
                            journal_path = (
                                workspace / "output" /
                                f"agent-conversation-{DATE}.md"
                            )
                            self.assertEqual(
                                state_path.read_bytes(), exact_state
                            )
                            self.assertEqual(
                                journal_path.read_bytes(), exact_journal
                            )
                            destination_results.add((
                                state_path.read_bytes(),
                                journal_path.read_bytes(),
                            ))
                            self.assertEqual(
                                json.loads(exact_state)["port_extension"],
                                {source_name: True},
                            )
                            self.assertFalse(
                                (workspace / INIT_INTENT_NAME).exists()
                            )

                            validated = run_journal(
                                destination,
                                "validate",
                                "--workspace",
                                str(workspace),
                            )
                            self.assertEqual(
                                validated.returncode, 0, validated.stderr
                            )
                            events = json.loads(validated.stdout)["events"]
                            self.assertEqual(
                                [event["event_type"] for event in events],
                                ["run-initialized", "user-intent-recorded"],
                            )
                    self.assertEqual(len(destination_results), 1)

    def test_all_source_destination_ports_reconcile_every_failpoint(self) -> None:
        for source_name, source in PORTS.items():
            for failpoint in FAILPOINTS:
                with seeded_phase_workspace(
                    port=source,
                    port_name=source_name,
                    port_extension={source_name: True},
                ) as (prototype, _):
                    interrupted = accept_phase(
                        prototype, failpoint=failpoint, port=source
                    )
                    self.assertNotEqual(interrupted.returncode, 0)
                    completed_bytes: set[tuple[bytes, bytes, bytes]] = set()
                    completed_semantics: set[bytes] = set()
                    for destination_name, destination in PORTS.items():
                        with self.subTest(
                            source=source_name,
                            destination=destination_name,
                            failpoint=failpoint,
                        ):
                            workspace = prototype.parent / (
                                f"recovered-{source_name}-{destination_name}-{failpoint}"
                            )
                            shutil.copytree(
                                prototype, workspace, copy_function=shutil.copy2
                            )
                            recovered = reconcile(workspace, port=destination)
                            self.assertEqual(
                                recovered.returncode, 0, recovered.stderr
                            )
                            result = self.assert_checkpoint_invariants(
                                workspace,
                                port=destination,
                                expected_port_extension={source_name: True},
                            )
                            event_ids = [
                                event["event_id"] for event in result["events"]
                            ]
                            self.assertEqual(len(event_ids), len(set(event_ids)))
                            completed_bytes.add((
                                (workspace / "state.json").read_bytes(),
                                (
                                    workspace / "output" /
                                    "session-manifest.json"
                                ).read_bytes(),
                                (
                                    workspace / "output" /
                                    f"agent-conversation-{DATE}.md"
                                ).read_bytes(),
                            ))
                            completed_semantics.add(
                                json.dumps(
                                    result["events"], sort_keys=True,
                                    separators=(",", ":"),
                                ).encode("ascii")
                            )
                    self.assertEqual(len(completed_bytes), 1)
                    self.assertEqual(len(completed_semantics), 1)


if __name__ == "__main__":
    unittest.main()
