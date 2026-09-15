import base64
import json
import hashlib
import os
import pathlib
import stat
import subprocess
import tempfile
import unittest
import uuid
from contextlib import contextmanager

from tests import journal_reporting_fixture as reporting


ROOT = pathlib.Path(__file__).resolve().parents[1]
DATE = "2026-08-26"
PORTS = {
    "claude": ROOT / "claude",
    "codex": ROOT / "codex" / "plugins" / "nightfalcon",
    "cursor": ROOT / "cursor",
}


def seed_workspace(base: str, phase: str = "phase-0") -> pathlib.Path:
    workspace = pathlib.Path(base) / "workspace"
    (workspace / "output").mkdir(parents=True)
    (workspace / "state.json").write_text(
        json.dumps({"date": DATE, "current_phase": phase, "mode": "review", "port_extension": {"keep": True}})
    )
    (workspace / "output" / "session-manifest.json").write_text(
        json.dumps({
            "date": DATE,
            "phases_completed": [],
            "manifest_extension": {"keep": True},
        })
    )
    return workspace


def run_journal(port: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/usr/local/bin/python3", str(port / "scripts" / "agent-journal.py"), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def initialize_workspace(base: str, port: pathlib.Path = PORTS["codex"]) -> tuple[pathlib.Path, dict[str, object]]:
    workspace = seed_workspace(base)
    result = run_journal(port, "init", "--workspace", str(workspace), "--date", DATE, "--port", "codex")
    if result.returncode:
        raise AssertionError(result.stderr)
    return workspace, json.loads((workspace / "state.json").read_text())


def configure_intent_state(
    workspace: pathlib.Path, mode: str, repo_slugs: list[str]
) -> dict[str, object]:
    state_path = workspace / "state.json"
    state = json.loads(state_path.read_text())
    state["mode"] = mode
    state["repo_slugs"] = repo_slugs
    state_path.write_text(json.dumps(state, indent=2) + "\n")
    return state


def parsed_journal(port: pathlib.Path, workspace: pathlib.Path) -> dict[str, object]:
    result = run_journal(port, "validate", "--workspace", str(workspace))
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def canonical_intent_source(
    state: dict[str, object], counts: dict[str, int]
) -> dict[str, object]:
    journal = state["agent_journal"]
    return {
        "timestamp_utc": "2026-08-26T12:00:00Z",
        "session_id": journal["session_id"],
        "phase": state["current_phase"],
        "epoch_id": journal["epoch_id"],
        "event_type": "user-intent-recorded",
        "agent_role": "orchestrator",
        "action_code": "record-decision",
        "status_code": "completed",
        "decision_code": "accepted",
        "reason_code": "user-request",
        "counts": counts,
    }


def record_args(workspace: pathlib.Path, state: dict[str, object], event_type: str = "phase-started") -> list[str]:
    journal = state["agent_journal"]
    arguments = [
        "record", "--workspace", str(workspace), "--event-type", event_type,
        "--session-id", journal["session_id"], "--phase", state["current_phase"],
        "--epoch-id", journal["epoch_id"],
    ]
    if event_type == "phase-started":
        arguments.extend([
            "--agent-role", "orchestrator",
            "--action-code", "start-phase",
            "--status-code", "started",
            "--reason-code", "phase-contract",
        ])
    return arguments


@contextmanager
def initialized_workspace(port: pathlib.Path):
    with tempfile.TemporaryDirectory() as tmp:
        yield initialize_workspace(tmp, port)


def record_json(port: pathlib.Path, workspace: pathlib.Path, request: dict[str, object]) -> subprocess.CompletedProcess[str]:
    """Call the public record_event boundary with a structured request."""
    program = """
import importlib.util
import json
import pathlib
import sys

spec = importlib.util.spec_from_file_location("agent_journal", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
try:
    module.record_event(pathlib.Path(sys.argv[2]), json.load(sys.stdin))
except module.JournalError as error:
    print(f"ERROR: {error}", file=sys.stderr)
    raise SystemExit(2)
"""
    return subprocess.run(
        ["/usr/local/bin/python3", "-c", program, str(port / "scripts" / "agent-journal.py"), str(workspace)],
        input=json.dumps(request), text=True, capture_output=True, check=False,
    )


def validate_handoff_data(port: pathlib.Path, value: dict[str, object]) -> subprocess.CompletedProcess[str]:
    program = """
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location("agent_journal", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
try:
    module._validate_handoff(json.load(sys.stdin))
except module.JournalError as error:
    print(f"ERROR: {error}", file=sys.stderr)
    raise SystemExit(2)
"""
    return subprocess.run(
        ["/usr/local/bin/python3", "-c", program, str(port / "scripts" / "agent-journal.py")],
        input=json.dumps(value), text=True, capture_output=True, check=False,
    )


def bound_handoff_data(
    port: pathlib.Path, value: dict[str, object], maximum: int,
) -> subprocess.CompletedProcess[str]:
    program = """
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location("agent_journal", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
try:
    rendered = module.bounded_markdown_projection(json.load(sys.stdin), int(sys.argv[2]))
    projection = json.loads(rendered.split("```json\\n", 1)[1].split("\\n```", 1)[0])
    module._validate_handoff(projection, maximum_bytes=int(sys.argv[2]))
except module.JournalError as error:
    print(f"ERROR: {error}", file=sys.stderr)
    raise SystemExit(2)
print(rendered, end="")
"""
    return subprocess.run(
        [
            "/usr/local/bin/python3", "-c", program,
            str(port / "scripts" / "agent-journal.py"), str(maximum),
        ],
        input=json.dumps(value), text=True, capture_output=True, check=False,
    )


def rewrite_accepted_events(
    workspace: pathlib.Path, events: list[dict[str, object]]
) -> None:
    state_path = workspace / "state.json"
    state = json.loads(state_path.read_text())
    journal = state["agent_journal"]
    header = (
        "# NightFalcon Agent Conversation\n\n"
        "Schema: NightFalcon Agent Journal v1\n"
        f"Date: {state['date']}\n"
        f"Workspace session: {journal['session_id']}\n"
        "Handling: Confidential structured run metadata; not a raw transcript.\n\n"
        "## Events\n\n"
    ).encode()
    blocks = []
    previous = None
    for sequence, source in enumerate(events, 1):
        event = json.loads(json.dumps(source))
        event["sequence"] = sequence
        event["event_id"] = str(uuid.uuid4())
        event["previous_event_sha256"] = previous
        event.pop("event_sha256", None)
        unsigned = json.dumps(
            event, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
        event["event_sha256"] = hashlib.sha256(unsigned).hexdigest()
        previous = event["event_sha256"]
        heading = b"## Next-run handoff\n\n" if event["event_type"] == "next-run-handoff" else b""
        blocks.append(
            heading + b"```json\n" + json.dumps(
                event, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode() + b"\n```\n"
        )
    payload = header + b"".join(blocks)
    journal_path = workspace / "output" / journal["filename"]
    journal_path.write_bytes(payload)
    journal["accepted_sequence"] = len(events)
    journal["accepted_length"] = len(payload)
    journal["accepted_head_sha256"] = previous
    state_path.write_text(json.dumps(state) + "\n")


def seed_typed_lifecycle(workspace: pathlib.Path, state: dict[str, object], count: int) -> None:
    journal = state["agent_journal"]
    program = """
import importlib.util
import json
import pathlib
import sys

spec = importlib.util.spec_from_file_location("agent_journal", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
workspace = pathlib.Path(sys.argv[2])
request = json.load(sys.stdin)
for index in range(request["count"] // 3):
    attempt_id = f"{index:08x}-0000-4000-8000-{index:012x}"
    common = {
        "session_id": request["session_id"],
        "phase": request["phase"],
        "epoch_id": request["epoch_id"],
        "agent_id": attempt_id,
        "agent_role": "phase-worker",
        "reason_code": "phase-contract",
    }
    for event_type, action_code, status_code in (
        ("agent-spawn-requested", "spawn-agent", "requested"),
        ("agent-started", "start-agent", "started"),
        ("agent-completed", "complete-agent", "completed"),
    ):
        event = {
            **common,
            "event_type": event_type,
            "action_code": action_code,
            "status_code": status_code,
        }
        if event_type == "agent-completed":
            event["counts"] = {"agents": 1}
        module.record_event(workspace, event)
"""
    result = subprocess.run(
        ["/usr/local/bin/python3", "-c", program,
         str(PORTS["codex"] / "scripts" / "agent-journal.py"), str(workspace)],
        input=json.dumps({
            "count": count,
            "session_id": journal["session_id"],
            "phase": state["current_phase"],
            "epoch_id": journal["epoch_id"],
        }),
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)


@contextmanager
def seeded_phase8_workspace(port: pathlib.Path = PORTS["codex"]):
    with tempfile.TemporaryDirectory() as tmp:
        workspace = pathlib.Path(tmp) / "workspace"
        reporting.write_phase8_workspace(port, workspace)
        (workspace / "output" / "session-manifest.json").write_text(json.dumps({
            "date": reporting.DATE,
            "phases_completed": [f"phase-{number}" for number in range(8)],
            "checkpoints": [],
        }) + "\n")
        initialized = run_journal(
            port,
            "init",
            "--workspace",
            str(workspace),
            "--date",
            reporting.DATE,
            "--port",
            "codex",
        )
        if initialized.returncode:
            raise AssertionError(initialized.stderr)
        for command in (
            ("git", "init", "-q"),
            ("git", "config", "user.name", "NightFalcon Test"),
            ("git", "config", "user.email", "nightfalcon@example.invalid"),
            ("git", "add", "."),
            ("git", "commit", "-qm", "phase 8 fixture"),
        ):
            result = subprocess.run(
                command, cwd=workspace, text=True, capture_output=True, check=False
            )
            if result.returncode:
                raise AssertionError(result.stderr)
        yield workspace


class JournalContractTests(unittest.TestCase):
    def test_all_ports_share_v2_and_legacy_transaction_engine(self) -> None:
        engines = {
            name: (port / "scripts" / "agent-journal.py").read_bytes()
            for name, port in PORTS.items()
        }
        self.assertEqual(engines["claude"], engines["cursor"])
        self.assertEqual(engines["codex"], engines["claude"])
        self.assertIn(b'TRANSACTION_VERSION = "2"', engines["codex"])
        self.assertIn(b'LEGACY_TRANSACTION_VERSION = "1"', engines["claude"])

    def test_all_ports_keep_identical_required_report_tree(self) -> None:
        date = reporting.DATE
        expected = {
            f"findings/demo/findings-{date}.json",
            f"findings/demo/findings-{date}.md",
            f"findings/demo/findings-{date}.sarif",
            f"output/agent-conversation-{date}.md",
            f"output/executive-report-{date}.html",
            f"output/executive-report-{date}.json",
            f"output/executive-report-{date}.sarif",
            f"output/executive-summary-{date}.md",
            "output/session-manifest.json",
        }
        trees = {}
        for name, port in PORTS.items():
            with self.subTest(port=name), tempfile.TemporaryDirectory() as tmp:
                workspace = pathlib.Path(tmp) / "workspace"
                reporting.write_phase8_workspace(port, workspace)
                findings = workspace / "findings" / "demo" / f"findings-{date}.json"
                rendered = (
                    (
                        "build-markdown.py",
                        workspace / "findings" / "demo" / f"findings-{date}.md",
                    ),
                    (
                        "build-sarif.py",
                        workspace / "findings" / "demo" / f"findings-{date}.sarif",
                    ),
                )
                for script, output in rendered:
                    result = subprocess.run(
                        [
                            "/usr/local/bin/python3",
                            str(port / "scripts" / "report" / script),
                            "--input",
                            str(findings),
                            "--output",
                            str(output),
                        ],
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

                manifest = workspace / "output" / "session-manifest.json"
                manifest.write_text(json.dumps({
                    "schema_version": "1",
                    "date": date,
                    "phases_completed": [f"phase-{number}" for number in range(8)],
                    "checkpoints": [],
                }) + "\n")
                initialized = run_journal(
                    port,
                    "init",
                    "--workspace",
                    str(workspace),
                    "--date",
                    date,
                    "--port",
                    name,
                )
                self.assertEqual(initialized.returncode, 0, initialized.stderr)

                tree = {
                    path.relative_to(workspace).as_posix()
                    for path in workspace.rglob("*")
                    if path.is_file()
                    and (
                        path.relative_to(workspace).as_posix().startswith(
                            "findings/demo/findings-"
                        )
                        or path.relative_to(workspace).as_posix().startswith(
                            "output/executive-"
                        )
                        or path.relative_to(workspace).as_posix().startswith(
                            "output/agent-conversation-"
                        )
                        or path.relative_to(workspace).as_posix()
                        == "output/session-manifest.json"
                    )
                }
                self.assertEqual(tree, expected)
                trees[name] = tree
        self.assertEqual(len({frozenset(tree) for tree in trees.values()}), 1)

    def test_record_intent_derives_closed_review_and_triage_events_without_sensitive_inputs(self) -> None:
        cases = (
            (
                "review",
                ["sensitive-repo-canary", "second-repo"],
                {
                    "repository-count": 2,
                    "review-mode": 1,
                    "triage-mode": 0,
                },
            ),
            (
                "triage",
                ["sensitive-repo-canary"],
                {
                    "repository-count": 1,
                    "review-mode": 0,
                    "triage-mode": 1,
                },
            ),
        )
        static_fields = {
            "event_type": "user-intent-recorded",
            "agent_role": "orchestrator",
            "action_code": "record-decision",
            "status_code": "completed",
            "decision_code": "accepted",
            "reason_code": "user-request",
        }
        envelope_fields = {
            "sequence", "event_id", "timestamp_utc", "session_id", "phase",
            "epoch_id", "previous_event_sha256", "event_sha256",
        }
        for port_name, port in PORTS.items():
            for mode, repo_slugs, expected_counts in cases:
                with self.subTest(port=port_name, mode=mode), tempfile.TemporaryDirectory() as tmp:
                    workspace, _ = initialize_workspace(tmp, port)
                    configure_intent_state(workspace, mode, repo_slugs)
                    raw_url = (
                        "https://user:raw-url-canary@example.invalid/org/repo"
                        "?token=credential-canary\n"
                    )
                    (workspace / "input").mkdir(exist_ok=True)
                    (workspace / "input" / "operator-input.txt").write_text(raw_url)

                    recorded = run_journal(
                        port, "record-intent", "--workspace", str(workspace)
                    )
                    self.assertEqual(recorded.returncode, 0, recorded.stderr)
                    events = parsed_journal(port, workspace)["events"]
                    intents = [
                        event for event in events
                        if event["event_type"] == "user-intent-recorded"
                    ]
                    self.assertEqual(len(intents), 1)
                    intent = intents[0]
                    self.assertEqual(
                        {field: intent[field] for field in static_fields},
                        static_fields,
                    )
                    self.assertEqual(intent["counts"], expected_counts)
                    self.assertEqual(
                        set(intent), envelope_fields | set(static_fields) | {"counts"}
                    )
                    journal_bytes = (
                        workspace / "output" / f"agent-conversation-{DATE}.md"
                    ).read_bytes()
                    for canary in (
                        b"sensitive-repo-canary",
                        b"raw-url-canary",
                        b"credential-canary",
                        str(workspace).encode(),
                        b"https://",
                    ):
                        self.assertNotIn(canary, journal_bytes)

    def test_generic_record_cannot_forge_user_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, state = initialize_workspace(tmp)
            state = configure_intent_state(workspace, "review", ["demo"])
            journal = state["agent_journal"]
            forged = record_json(PORTS["codex"], workspace, {
                "event_type": "user-intent-recorded",
                "session_id": journal["session_id"],
                "phase": state["current_phase"],
                "epoch_id": journal["epoch_id"],
                "agent_role": "orchestrator",
                "action_code": "record-decision",
                "status_code": "completed",
                "decision_code": "accepted",
                "reason_code": "user-request",
                "counts": {
                    "repository-count": 1,
                    "review-mode": 1,
                    "triage-mode": 0,
                },
            })
            self.assertEqual(forged.returncode, 2, forged.stderr)
            self.assertIn("event_type is invalid", forged.stderr)

    def test_record_intent_repeated_call_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, _ = initialize_workspace(tmp)
            configure_intent_state(workspace, "review", ["demo", "second"])
            path = workspace / "output" / f"agent-conversation-{DATE}.md"

            first = run_journal(
                PORTS["codex"], "record-intent", "--workspace", str(workspace)
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            first_bytes = path.read_bytes()
            second = run_journal(
                PORTS["codex"], "record-intent", "--workspace", str(workspace)
            )

            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(second.stdout, first.stdout)
            self.assertEqual(path.read_bytes(), first_bytes)
            intents = [
                event for event in parsed_journal(PORTS["codex"], workspace)["events"]
                if event["event_type"] == "user-intent-recorded"
            ]
            self.assertEqual(len(intents), 1)

    def test_record_intent_is_a_safe_noop_after_run_finalization(self) -> None:
        with seeded_phase8_workspace() as workspace:
            completed = reporting.complete(PORTS["codex"], workspace, "phase-8")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            state_path = workspace / "state.json"
            journal_path = (
                workspace / "output"
                / f"agent-conversation-{reporting.DATE}.md"
            )
            state_before = state_path.read_bytes()
            journal_before = journal_path.read_bytes()

            result = run_journal(
                PORTS["codex"], "record-intent", "--workspace", str(workspace)
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "run complete")
            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertEqual(journal_path.read_bytes(), journal_before)

    def test_record_intent_rejects_conflicting_malformed_or_duplicate_events(self) -> None:
        cases = (
            (
                "conflicting",
                [
                    {
                        "repository-count": 1,
                        "review-mode": 1,
                        "triage-mode": 0,
                    }
                ],
                None,
                "conflicts with authoritative state",
            ),
            (
                "malformed",
                [
                    {
                        "repository-count": 2,
                        "review-mode": 1,
                        "triage-mode": 0,
                    }
                ],
                "forged-agent",
                "opaque UUIDv4 attempt ID",
            ),
            (
                "duplicate",
                [
                    {
                        "repository-count": 2,
                        "review-mode": 1,
                        "triage-mode": 0,
                    },
                    {
                        "repository-count": 2,
                        "review-mode": 1,
                        "triage-mode": 0,
                    },
                ],
                None,
                "multiple user intent events",
            ),
        )
        for case, count_values, extra_agent_id, expected_error in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                workspace, _ = initialize_workspace(tmp)
                state = configure_intent_state(
                    workspace, "review", ["demo", "second"]
                )
                original = parsed_journal(PORTS["codex"], workspace)["events"]
                forged = []
                for counts in count_values:
                    event = canonical_intent_source(state, counts)
                    if extra_agent_id is not None:
                        event["agent_id"] = extra_agent_id
                    forged.append(event)
                rewrite_accepted_events(workspace, original + forged)

                result = run_journal(
                    PORTS["codex"], "record-intent", "--workspace", str(workspace)
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(expected_error, result.stderr)

    def test_record_intent_rejects_invalid_or_unbounded_authoritative_scope(self) -> None:
        cases = (
            ("missing-repositories", "review", [], "bounded nonempty list"),
            (
                "too-many-repositories",
                "review",
                [f"repo-{index}" for index in range(65)],
                "bounded nonempty list",
            ),
            ("duplicate-repositories", "review", ["demo", "demo"], "must be unique"),
            ("multi-repo-triage", "triage", ["demo", "second"], "exactly one repository"),
            ("unknown-mode", "audit", ["demo"], "state mode is not an allowed code"),
        )
        for case, mode, repo_slugs, expected_error in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                workspace, _ = initialize_workspace(tmp)
                configure_intent_state(workspace, mode, repo_slugs)
                result = run_journal(
                    PORTS["codex"], "record-intent", "--workspace", str(workspace)
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(expected_error, result.stderr)

    def test_worker_record_rejects_internal_transition_and_finalization_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, state = initialize_workspace(tmp)
            journal = state["agent_journal"]
            for event_type in (
                "run-initialized", "run-resumed", "user-intent-recorded",
                "phase-acceptance-prepared", "phase-accepted",
                "phase-acceptance-aborted", "artifact-bound",
                "recovery-started", "recovery-completed", "recovery-blocked",
                "run-finalized", "next-run-handoff",
            ):
                with self.subTest(event_type=event_type):
                    worker_result = record_json(PORTS["codex"], workspace, {
                        "event_type": event_type,
                        "session_id": journal["session_id"],
                        "phase": state["current_phase"],
                        "epoch_id": journal["epoch_id"],
                    })
                    self.assertEqual(worker_result.returncode, 2, worker_result.stderr)

    def test_phase8_worker_cannot_inject_forged_zero_digest_acceptance_binding(self) -> None:
        with seeded_phase8_workspace() as workspace:
            state = json.loads((workspace / "state.json").read_text())
            journal = state["agent_journal"]
            artifact = next((workspace / "findings" / "demo").glob("*.md"))
            forged = record_json(PORTS["codex"], workspace, {
                "event_type": "phase-accepted",
                "session_id": journal["session_id"],
                "phase": state["current_phase"],
                "epoch_id": journal["epoch_id"],
                "agent_role": "orchestrator",
                "action_code": "accept-phase",
                "status_code": "accepted",
                "reason_code": "acceptance-gate",
                "artifact_refs": [{
                    "path": artifact.relative_to(workspace).as_posix(),
                    "sha256": "0" * 64,
                    "checkpoint_uuid": journal["checkpoint_uuid"],
                }],
            })
            completed = reporting.complete(PORTS["codex"], workspace, "phase-8")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            validated = run_journal(
                PORTS["codex"], "validate", "--workspace", str(workspace)
            )
            self.assertEqual(validated.returncode, 0, validated.stderr)
            handoff = json.loads(validated.stdout)["events"][-1]["handoff"]
            contains_zero_digest = any(
                binding["sha256"] == "0" * 64
                for binding in handoff["accepted_artifacts"]
            )

        self.assertEqual(forged.returncode, 2, forged.stderr)
        self.assertFalse(contains_zero_digest)

    def test_parsed_journal_rejects_worker_forged_handoff_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, state = initialize_workspace(tmp)

            recorded = run_journal(PORTS["codex"], *record_args(workspace, state))
            self.assertEqual(recorded.returncode, 0, recorded.stderr)
            path = workspace / "output" / f"agent-conversation-{DATE}.md"
            raw = path.read_bytes()
            header, payload = raw.split(b"```json\n", 1)
            event = json.loads(payload.split(b"\n```\n", 1)[0])
            event["event_type"] = "next-run-handoff"
            unsigned = {key: value for key, value in event.items() if key != "event_sha256"}
            event["event_sha256"] = __import__("hashlib").sha256(
                json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
            ).hexdigest()
            path.write_bytes(header + b"```json\n" + json.dumps(
                event, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode() + b"\n```\n")
            parsed_result = run_journal(PORTS["codex"], "validate", "--workspace", str(workspace))
            self.assertEqual(parsed_result.returncode, 2, parsed_result.stderr)

    def test_context_accepts_authoritative_triage_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = seed_workspace(tmp)
            state = json.loads((workspace / "state.json").read_text())
            state["mode"] = "triage"
            (workspace / "state.json").write_text(json.dumps(state))
            initialized = run_journal(PORTS["codex"], "init", "--workspace", str(workspace), "--date", DATE)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            context = run_journal(PORTS["codex"], "context", "--workspace", str(workspace))
            self.assertEqual(context.returncode, 0, context.stderr)
            projection = json.loads(context.stdout.split("```json\n", 1)[1].split("\n```", 1)[0])
            self.assertEqual(projection["mode"], "triage")

    def test_context_marks_done_phase_as_run_complete(self) -> None:
        with seeded_phase8_workspace() as workspace:
            completed = reporting.complete(PORTS["codex"], workspace, "phase-8")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            context = run_journal(PORTS["codex"], "context", "--workspace", str(workspace))
            self.assertEqual(context.returncode, 0, context.stderr)
            projection = json.loads(context.stdout.split("```json\n", 1)[1].split("\n```", 1)[0])
            self.assertEqual(projection["resume_action"], "run complete")
            self.assertTrue(context.stdout.startswith("## Next-run handoff\n"))

    def test_phase8_acceptance_requires_and_finalizes_journal(self) -> None:
        with seeded_phase8_workspace() as workspace:
            result = reporting.complete(PORTS["codex"], workspace, "phase-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            validated = run_journal(
                PORTS["codex"], "validate", "--workspace", str(workspace)
            )
            self.assertEqual(validated.returncode, 0, validated.stderr)
            journal = json.loads(validated.stdout)
            self.assertEqual(journal["events"][-2]["event_type"], "run-finalized")
            self.assertEqual(journal["events"][-1]["event_type"], "next-run-handoff")
            state = json.loads((workspace / "state.json").read_text())
            journal_state = state["agent_journal"]
            self.assertEqual(journal_state["epoch_status"], "closed")
            self.assertEqual(journal_state["epoch_phase"], "phase-8")
            self.assertEqual(
                journal_state["epoch_id"], journal["events"][-2]["epoch_id"]
            )
            handoff = journal["events"][-1]["handoff"]
            self.assertEqual(handoff["current_phase"], "done")
            self.assertEqual(handoff["resume_action"], "run complete")
            self.assertIn("phase-8", handoff["phases_completed"])
            self.assertLessEqual(len(json.dumps(handoff).encode()), 16_384)
            required = {
                "repositories", "phase_status", "phase_history", "checkpoints",
                "reason_counts", "retry_count", "recovery_counts",
                "failure_classes", "accepted_artifacts",
            }
            self.assertTrue(required <= set(handoff))
            self.assertEqual(handoff["repositories"], [{"repo_slug": "demo"}])
            self.assertEqual(handoff["phase_status"]["phase-8"], "completed")
            self.assertEqual(handoff["phase_history"][-1], "phase-8")
            self.assertEqual(handoff["checkpoints"][-1]["phase"], "phase-8")
            self.assertTrue(handoff["checkpoints"][-1]["checkpoint_uuid"])
            self.assertGreater(len(handoff["accepted_artifacts"]), 0)
            for artifact in handoff["accepted_artifacts"]:
                self.assertTrue(artifact["checkpoint_uuid"])
                self.assertEqual(len(artifact["sha256"]), 64)
            path = workspace / "output" / f"agent-conversation-{reporting.DATE}.md"
            self.assertIn("## Next-run handoff\n", path.read_text())

    def test_final_handoff_rejects_missing_or_malformed_extended_schema(self) -> None:
        with seeded_phase8_workspace() as workspace:
            completed = reporting.complete(PORTS["codex"], workspace, "phase-8")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            validated = run_journal(
                PORTS["codex"], "validate", "--workspace", str(workspace)
            )
            handoff = json.loads(validated.stdout)["events"][-1]["handoff"]
            mutations = {}
            for field in (
                "repositories", "phase_status", "phase_history", "checkpoints",
                "reason_counts", "retry_count", "recovery_counts",
                "failure_classes", "accepted_artifacts",
            ):
                mutations[f"missing-{field}"] = {
                    key: value for key, value in handoff.items() if key != field
                }
            malformed_binding = json.loads(json.dumps(handoff))
            malformed_binding["accepted_artifacts"] = [{
                "path": "output/report.json",
                "checkpoint_uuid": "not-bound",
                "sha256": "0" * 63,
            }]
            mutations["malformed-binding"] = malformed_binding
            detached_binding = json.loads(json.dumps(handoff))
            detached_binding["accepted_artifacts"][0]["checkpoint_uuid"] = "detached-checkpoint"
            detached_binding["accepted_artifacts"].sort(
                key=lambda ref: json.dumps(ref, sort_keys=True, separators=(",", ":"))
            )
            mutations["detached-binding"] = detached_binding
            divergent_history = json.loads(json.dumps(handoff))
            divergent_history["phase_history"] = divergent_history["phase_history"][:-1]
            mutations["divergent-history"] = divergent_history
            oversized = json.loads(json.dumps(handoff))
            oversized["repositories"] = [
                {"repo_slug": f"repo-{index}"} for index in range(65)
            ]
            mutations["oversized-repositories"] = oversized
            for case, candidate in mutations.items():
                with self.subTest(case=case):
                    result = validate_handoff_data(PORTS["codex"], candidate)
                    self.assertEqual(result.returncode, 2, result.stderr)

    def test_completed_run_reconcile_is_idempotent_and_preserves_final_pair(self) -> None:
        with seeded_phase8_workspace() as workspace:
            completed = reporting.complete(PORTS["codex"], workspace, "phase-8")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            journal_path = (
                workspace / "output" / f"agent-conversation-{reporting.DATE}.md"
            )
            state_path = workspace / "state.json"
            before_journal = journal_path.read_bytes()
            before_state = state_path.read_bytes()
            for attempt in range(2):
                with self.subTest(attempt=attempt):
                    reconciled = run_journal(
                        PORTS["codex"], "reconcile", "--workspace", str(workspace)
                    )
                    self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
                    self.assertEqual(journal_path.read_bytes(), before_journal)
                    self.assertEqual(state_path.read_bytes(), before_state)
            validated = run_journal(
                PORTS["codex"], "validate", "--workspace", str(workspace)
            )
            events = json.loads(validated.stdout)["events"]
            self.assertEqual(
                [event["event_type"] for event in events[-2:]],
                ["run-finalized", "next-run-handoff"],
            )
            self.assertEqual(
                sum(event["event_type"] == "next-run-handoff" for event in events), 1
            )
            self.assertFalse(any(event["event_type"] == "run-resumed" for event in events))

    def test_codex_decodes_legacy_v1_terminal_acceptance_transaction(self) -> None:
        with seeded_phase8_workspace(port=PORTS["claude"]) as workspace:
            environment = os.environ.copy()
            environment["NIGHTFALCON_JOURNAL_FAILPOINT"] = "after-intent"
            interrupted = subprocess.run(
                (
                    "bash",
                    str(PORTS["claude"] / "scripts" / "complete-phase.sh"),
                    "--phase",
                    "phase-8",
                    "--workspace",
                    str(workspace),
                ),
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            transaction = json.loads(
                (workspace / ".nightfalcon-journal-transaction.json").read_text()
            )
            legacy_state = json.loads(base64.b64decode(
                transaction["state"]["new_base64"]
            ))
            legacy_journal = legacy_state["agent_journal"]
            legacy_journal["epoch_id"] = "11111111-1111-4111-8111-111111111111"
            legacy_journal["epoch_phase"] = "done"
            legacy_journal["epoch_status"] = "open"
            legacy_journal["checkpoint_uuid"] = "22222222-2222-4222-8222-222222222222"
            legacy_state["git_checkpoints"][-1].pop(
                "journal_transition_version", None
            )
            legacy_state_bytes = (
                json.dumps(legacy_state, sort_keys=True, indent=2) + "\n"
            ).encode()
            transaction["state"]["new_base64"] = base64.b64encode(
                legacy_state_bytes
            ).decode()
            transaction["state"]["new_sha256"] = hashlib.sha256(
                legacy_state_bytes
            ).hexdigest()
            legacy_manifest = json.loads(base64.b64decode(
                transaction["manifest"]["new_base64"]
            ))
            legacy_manifest["checkpoints"][-1].pop(
                "journal_transition_version", None
            )
            legacy_manifest_bytes = (
                json.dumps(legacy_manifest, sort_keys=True, indent=2) + "\n"
            ).encode()
            transaction["manifest"]["new_base64"] = base64.b64encode(
                legacy_manifest_bytes
            ).decode()
            transaction["manifest"]["new_sha256"] = hashlib.sha256(
                legacy_manifest_bytes
            ).hexdigest()
            transaction["schema_version"] = "1"
            (workspace / ".nightfalcon-journal-transaction.json").write_text(
                json.dumps(transaction, sort_keys=True, indent=2) + "\n"
            )
            validated = run_journal(
                PORTS["codex"], "validate", "--workspace", str(workspace)
            )
            state = json.loads((workspace / "state.json").read_text())

        self.assertEqual(interrupted.returncode, 2, interrupted.stdout)
        self.assertEqual(transaction["schema_version"], "1")
        self.assertEqual(validated.returncode, 0, validated.stderr)
        events = json.loads(validated.stdout)["events"]
        self.assertEqual(state["current_phase"], "done")
        self.assertEqual(
            [event["event_type"] for event in events[-2:]],
            ["run-finalized", "next-run-handoff"],
        )
        self.assertEqual(state["agent_journal"]["epoch_status"], "open")
        self.assertEqual(state["agent_journal"]["epoch_phase"], "done")

    def test_completed_run_reconcile_rejects_ambiguous_final_checkpoint(self) -> None:
        with seeded_phase8_workspace() as workspace:
            completed = reporting.complete(PORTS["codex"], workspace, "phase-8")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            manifest_path = workspace / "output" / "session-manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["checkpoints"].append({
                "phase": "phase-8",
                "checkpoint_uuid": "ambiguous-final-checkpoint",
                "timestamp": "2026-08-26T12:30:00Z",
            })
            manifest_path.write_text(json.dumps(manifest) + "\n")
            reconciled = run_journal(
                PORTS["codex"], "reconcile", "--workspace", str(workspace)
            )
            self.assertEqual(reconciled.returncode, 2, reconciled.stderr)

    def test_final_handoff_must_equal_authoritative_deterministic_projection(self) -> None:
        mutations = ("missing-phase8", "reversed-history", "forged-bindings", "fabricated-artifact")
        for mutation in mutations:
            with self.subTest(mutation=mutation), seeded_phase8_workspace() as workspace:
                completed = reporting.complete(PORTS["codex"], workspace, "phase-8")
                self.assertEqual(completed.returncode, 0, completed.stderr)
                validated = run_journal(
                    PORTS["codex"], "validate", "--workspace", str(workspace)
                )
                events = json.loads(validated.stdout)["events"]
                handoff = events[-1]["handoff"]
                if mutation == "missing-phase8":
                    handoff["phases_completed"].remove("phase-8")
                    handoff["phase_history"].remove("phase-8")
                    handoff["phase_status"].pop("phase-8")
                    handoff["checkpoints"] = []
                    handoff["accepted_artifacts"] = []
                elif mutation == "reversed-history":
                    handoff["phases_completed"].reverse()
                    handoff["phase_history"].reverse()
                elif mutation == "forged-bindings":
                    handoff["checkpoints"][0]["checkpoint_uuid"] = "forged-checkpoint"
                    for artifact in handoff["accepted_artifacts"]:
                        artifact["checkpoint_uuid"] = "forged-checkpoint"
                    handoff["accepted_artifacts"].sort(
                        key=lambda ref: json.dumps(ref, sort_keys=True, separators=(",", ":"))
                    )
                else:
                    handoff["accepted_artifacts"].append({
                        "path": "output/fabricated.json",
                        "checkpoint_uuid": handoff["checkpoints"][0]["checkpoint_uuid"],
                        "sha256": "a" * 64,
                    })
                    handoff["accepted_artifacts"].sort(
                        key=lambda ref: json.dumps(ref, sort_keys=True, separators=(",", ":"))
                    )
                rewrite_accepted_events(workspace, events)
                for command in ("validate", "reconcile"):
                    with self.subTest(command=command):
                        result = run_journal(
                            PORTS["codex"], command, "--workspace", str(workspace)
                        )
                        self.assertEqual(result.returncode, 2, result.stderr)

    def test_completed_run_rejects_any_earlier_or_duplicate_terminal_event(self) -> None:
        mutations = ("duplicate-pair", "earlier-finalized", "earlier-handoff")
        for mutation in mutations:
            with self.subTest(mutation=mutation), seeded_phase8_workspace() as workspace:
                completed = reporting.complete(PORTS["codex"], workspace, "phase-8")
                self.assertEqual(completed.returncode, 0, completed.stderr)
                validated = run_journal(
                    PORTS["codex"], "validate", "--workspace", str(workspace)
                )
                events = json.loads(validated.stdout)["events"]
                finalized = json.loads(json.dumps(events[-2]))
                handoff = json.loads(json.dumps(events[-1]))
                if mutation == "duplicate-pair":
                    events[-2:-2] = [finalized, handoff]
                elif mutation == "earlier-finalized":
                    events.insert(-2, finalized)
                else:
                    events.insert(-2, handoff)
                rewrite_accepted_events(workspace, events)
                for command in ("validate", "reconcile"):
                    with self.subTest(command=command):
                        result = run_journal(
                            PORTS["codex"], command, "--workspace", str(workspace)
                        )
                        self.assertEqual(result.returncode, 2, result.stderr)

    def test_phase8_completion_reports_missing_or_invalid_journal_inventory(self) -> None:
        cases = ("missing", "invalid")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                workspace = pathlib.Path(tmp) / "workspace"
                reporting.write_phase8_workspace(PORTS["codex"], workspace)
                journal_path = (
                    workspace / "output" /
                    f"agent-conversation-{reporting.DATE}.md"
                )
                if case == "missing":
                    journal_path.unlink()
                else:
                    journal_path.write_bytes(journal_path.read_bytes() + b"tampered")
                result = reporting.complete(PORTS["codex"], workspace, "phase-8")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                if case == "missing":
                    self.assertIn("journal is unavailable", result.stderr.lower())
                else:
                    self.assertIn("journal contains trailing", result.stderr.lower())

    def test_record_rejects_free_text_secret_and_unknown_fields(self) -> None:
        bad_requests = (
            {"event_type": "decision-recorded", "note": "api_key=canary-unknown-123"},
            {"event_type": "agent-failed", "stderr": "credential value"},
            {"event_type": "decision-recorded", "artifact_refs": [{"path": "../../secret"}]},
            {"event_type": "agent-started", "agent_role": "unbounded-worker-role"},
        )
        with initialized_workspace(PORTS["codex"]) as (workspace, state):
            journal = state["agent_journal"]
            for request in bad_requests:
                with self.subTest(request=request):
                    result = record_json(PORTS["codex"], workspace, {
                        **request,
                        "session_id": journal["session_id"],
                        "phase": state["current_phase"],
                        "epoch_id": journal["epoch_id"],
                    })
                    self.assertEqual(result.returncode, 2, result.stderr)

    def test_public_events_enforce_exact_fields_and_code_mappings(self) -> None:
        bad_variants = (
            {},
            {
                "agent_role": "orchestrator", "action_code": "resume",
                "status_code": "started", "reason_code": "phase-contract",
            },
            {
                "agent_role": "orchestrator", "action_code": "start-phase",
                "status_code": "started", "reason_code": "phase-contract",
                "counts": {"agents": 1},
            },
        )
        for index, variant in enumerate(bad_variants):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as tmp:
                workspace, state = initialize_workspace(tmp)
                journal = state["agent_journal"]
                result = record_json(PORTS["codex"], workspace, {
                    "event_type": "phase-started",
                    "session_id": journal["session_id"],
                    "phase": state["current_phase"],
                    "epoch_id": journal["epoch_id"],
                    **variant,
                })
                self.assertEqual(result.returncode, 2, result.stderr)

        with tempfile.TemporaryDirectory() as tmp:
            workspace, state = initialize_workspace(tmp)
            journal = state["agent_journal"]
            valid = record_json(PORTS["codex"], workspace, {
                "event_type": "phase-started",
                "session_id": journal["session_id"],
                "phase": state["current_phase"],
                "epoch_id": journal["epoch_id"],
                "agent_role": "orchestrator",
                "action_code": "start-phase",
                "status_code": "started",
                "reason_code": "phase-contract",
            })
            self.assertEqual(valid.returncode, 0, valid.stderr)

    def test_agent_lifecycle_is_a_unique_requested_started_terminal_fsm(self) -> None:
        attempt_id = "44444444-4444-4444-8444-444444444444"

        def lifecycle_request(
            state: dict[str, object], event_type: str,
            action: str, status: str, reason: str = "phase-contract",
        ) -> dict[str, object]:
            journal = state["agent_journal"]
            return {
                "event_type": event_type,
                "session_id": journal["session_id"],
                "phase": state["current_phase"],
                "epoch_id": journal["epoch_id"],
                "agent_id": attempt_id,
                "agent_role": "phase-worker",
                "action_code": action,
                "status_code": status,
                "reason_code": reason,
            }

        with tempfile.TemporaryDirectory() as tmp:
            workspace, state = initialize_workspace(tmp)
            started_first = record_json(
                PORTS["codex"], workspace,
                lifecycle_request(state, "agent-started", "start-agent", "started"),
            )
            self.assertEqual(started_first.returncode, 2, started_first.stderr)

        with tempfile.TemporaryDirectory() as tmp:
            workspace, state = initialize_workspace(tmp)
            for event_type, action, status in (
                ("agent-spawn-requested", "spawn-agent", "requested"),
                ("agent-started", "start-agent", "started"),
                ("agent-completed", "complete-agent", "completed"),
            ):
                result = record_json(
                    PORTS["codex"], workspace,
                    lifecycle_request(state, event_type, action, status),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            duplicate_terminal = record_json(
                PORTS["codex"], workspace,
                lifecycle_request(
                    state, "agent-failed", "fail-agent", "failed", "worker-failure"
                ),
            )
            reused_attempt = record_json(
                PORTS["codex"], workspace,
                lifecycle_request(
                    state, "agent-spawn-requested", "spawn-agent", "requested"
                ),
            )
            self.assertEqual(duplicate_terminal.returncode, 2, duplicate_terminal.stderr)
            self.assertEqual(reused_attempt.returncode, 2, reused_attempt.stderr)

    def test_pre_start_spawn_rejection_can_be_failed_and_retried_in_all_ports(self) -> None:
        for port_name, port in PORTS.items():
            with self.subTest(port=port_name), tempfile.TemporaryDirectory() as tmp:
                workspace, state = initialize_workspace(tmp, port)
                journal = state["agent_journal"]
                common = {
                    "session_id": journal["session_id"],
                    "phase": state["current_phase"],
                    "epoch_id": journal["epoch_id"],
                    "agent_id": "55555555-5555-4555-8555-555555555555",
                    "agent_role": "phase-worker",
                }
                events = (
                    {
                        "event_type": "agent-spawn-requested",
                        "action_code": "spawn-agent",
                        "status_code": "requested",
                        "reason_code": "phase-contract",
                    },
                    {
                        "event_type": "agent-failed",
                        "action_code": "fail-agent",
                        "status_code": "failed",
                        "reason_code": "spawn-rejected",
                    },
                    {
                        "event_type": "retry-scheduled",
                        "action_code": "schedule-retry",
                        "status_code": "scheduled",
                        "reason_code": "retry-policy",
                    },
                )
                for event in events:
                    result = record_json(port, workspace, {**common, **event})
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_started_attempt_rejects_spawn_rejected_reason(self) -> None:
        for port_name, port in PORTS.items():
            with self.subTest(port=port_name), tempfile.TemporaryDirectory() as tmp:
                workspace, state = initialize_workspace(tmp, port)
                journal = state["agent_journal"]
                common = {
                    "session_id": journal["session_id"],
                    "phase": state["current_phase"],
                    "epoch_id": journal["epoch_id"],
                    "agent_id": "77777777-7777-4777-8777-777777777777",
                    "agent_role": "phase-worker",
                }
                for event in (
                    {
                        "event_type": "agent-spawn-requested",
                        "action_code": "spawn-agent",
                        "status_code": "requested",
                        "reason_code": "phase-contract",
                    },
                    {
                        "event_type": "agent-started",
                        "action_code": "start-agent",
                        "status_code": "started",
                        "reason_code": "phase-contract",
                    },
                ):
                    result = record_json(port, workspace, {**common, **event})
                    self.assertEqual(result.returncode, 0, result.stderr)
                invalid = record_json(port, workspace, {
                    **common,
                    "event_type": "agent-failed",
                    "action_code": "fail-agent",
                    "status_code": "failed",
                    "reason_code": "spawn-rejected",
                })
                self.assertEqual(invalid.returncode, 2, invalid.stderr)

    def test_attempt_identifiers_and_count_names_reject_secret_canaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, state = initialize_workspace(tmp)
            journal = state["agent_journal"]
            secret_attempt = record_json(PORTS["codex"], workspace, {
                "event_type": "agent-spawn-requested",
                "session_id": journal["session_id"],
                "phase": state["current_phase"],
                "epoch_id": journal["epoch_id"],
                "agent_id": "api-token-canary-AKIAIOSFODNN7EXAMPLE",
                "agent_role": "phase-worker",
                "action_code": "spawn-agent",
                "status_code": "requested",
                "reason_code": "phase-contract",
            })
            self.assertEqual(secret_attempt.returncode, 2, secret_attempt.stderr)

        with tempfile.TemporaryDirectory() as tmp:
            workspace, state = initialize_workspace(tmp)
            journal = state["agent_journal"]
            secret_count = record_json(PORTS["codex"], workspace, {
                "event_type": "decision-recorded",
                "session_id": journal["session_id"],
                "phase": state["current_phase"],
                "epoch_id": journal["epoch_id"],
                "agent_role": "orchestrator",
                "action_code": "record-decision",
                "decision_code": "deferred",
                "reason_code": "evidence-gap",
                "counts": {"credential-token-canary": 1},
            })
            self.assertEqual(secret_count.returncode, 2, secret_count.stderr)

    def test_context_is_deterministic_bounded_and_contains_no_event_prose(self) -> None:
        with initialized_workspace(PORTS["codex"]) as (workspace, state):
            seed_typed_lifecycle(workspace, state, count=300)
            first = run_journal(PORTS["codex"], "context", "--workspace", str(workspace))
            second = run_journal(PORTS["codex"], "context", "--workspace", str(workspace))
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(first.stdout, second.stdout)
            self.assertLessEqual(len(first.stdout.encode()), 16384)
            self.assertNotIn("canary", first.stdout.lower())
            projection = json.loads(first.stdout.split("```json\n", 1)[1].split("\n```", 1)[0])
            self.assertIn("interrupted_attempt", projection)
            self.assertEqual(
                validate_handoff_data(PORTS["codex"], projection).returncode, 0
            )

    def test_final_context_schema_covers_empty_and_populated_provisional_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, state = initialize_workspace(tmp)
            state = configure_intent_state(workspace, "review", ["demo"])
            empty = run_journal(
                PORTS["codex"], "context", "--workspace", str(workspace)
            )
            self.assertEqual(empty.returncode, 0, empty.stderr)
            empty_projection = json.loads(
                empty.stdout.split("```json\n", 1)[1].split("\n```", 1)[0]
            )
            self.assertEqual(
                validate_handoff_data(PORTS["codex"], empty_projection).returncode,
                0,
            )

            journal = state["agent_journal"]
            common = {
                "session_id": journal["session_id"],
                "phase": state["current_phase"],
                "epoch_id": journal["epoch_id"],
                "agent_id": "88888888-8888-4888-8888-888888888888",
                "agent_role": "phase-worker",
                "reason_code": "phase-contract",
            }
            for event_type, action, status in (
                ("agent-spawn-requested", "spawn-agent", "requested"),
                ("agent-started", "start-agent", "started"),
            ):
                recorded = record_json(PORTS["codex"], workspace, {
                    **common,
                    "event_type": event_type,
                    "action_code": action,
                    "status_code": status,
                })
                self.assertEqual(recorded.returncode, 0, recorded.stderr)
            terminal = record_json(PORTS["codex"], workspace, {
                **common,
                "event_type": "agent-failed",
                "action_code": "fail-agent",
                "status_code": "failed",
                "reason_code": "worker-failure",
            })
            self.assertEqual(terminal.returncode, 0, terminal.stderr)
            reconciled = run_journal(
                PORTS["codex"], "reconcile", "--workspace", str(workspace),
            )
            self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
            populated = run_journal(
                PORTS["codex"], "context", "--workspace", str(workspace)
            )
            self.assertEqual(populated.returncode, 0, populated.stderr)
            populated_projection = json.loads(
                populated.stdout.split("```json\n", 1)[1].split("\n```", 1)[0]
            )

        self.assertTrue(populated_projection["interrupted_attempt"]["attempts"])
        validated = validate_handoff_data(PORTS["codex"], populated_projection)
        self.assertEqual(validated.returncode, 0, validated.stderr)

    def test_final_context_enforces_16383_16384_16385_markdown_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, _state = initialize_workspace(tmp)
            rendered = run_journal(
                PORTS["codex"], "context", "--workspace", str(workspace)
            )
            self.assertEqual(rendered.returncode, 0, rendered.stderr)
            projection = json.loads(
                rendered.stdout.split("```json\n", 1)[1].split("\n```", 1)[0]
            )
        template = {
            "epoch_id": "99999999-9999-4999-8999-999999999999",
            "phase": "phase-0",
            "events": [{
                "event_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "event_type": "agent-failed",
                "agent_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "agent_role": "phase-worker",
                "action_code": "fail-agent",
                "status_code": "interrupted",
                "reason_code": "resume-reconciliation",
                "counts": {"agents": 1},
            }],
        }
        projection["interrupted_attempt"]["attempts"] = [
            {
                **json.loads(json.dumps(template)),
                "epoch_id": f"{index:08x}-9999-4999-8999-{index:012x}",
            }
            for index in range(64)
        ]
        raw = (
            "## Next-run handoff\n\n```json\n"
            + json.dumps(projection, sort_keys=True, separators=(",", ":"))
            + "\n```\n"
        )
        self.assertGreater(len(raw.encode()), 16_385)
        for boundary in (16_383, 16_384, 16_385):
            with self.subTest(boundary=boundary):
                bounded = bound_handoff_data(PORTS["codex"], projection, boundary)
                self.assertEqual(bounded.returncode, 0, bounded.stderr)
                self.assertLessEqual(len(bounded.stdout.encode()), boundary)

    def test_init_creates_canonical_private_journal_and_state_extension(self) -> None:
        for name, port in PORTS.items():
            with self.subTest(port=name), tempfile.TemporaryDirectory() as tmp:
                workspace = seed_workspace(tmp)
                result = run_journal(
                    port, "init", "--workspace", str(workspace), "--date", DATE, "--port", name
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                path = workspace / "output" / f"agent-conversation-{DATE}.md"
                self.assertIn("Schema: NightFalcon Agent Journal v1", path.read_text())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                state = json.loads((workspace / "state.json").read_text())
                self.assertEqual(state["agent_journal"]["schema_version"], "1")
                self.assertEqual(state["port_extension"], {"keep": True})

    def test_record_builds_contiguous_verified_hash_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, state = initialize_workspace(tmp)
            phase_started = run_journal(
                PORTS["codex"], *record_args(workspace, state, "phase-started")
            )
            self.assertEqual(phase_started.returncode, 0, phase_started.stderr)
            journal = state["agent_journal"]
            common = [
                "--session-id", journal["session_id"],
                "--phase", state["current_phase"],
                "--epoch-id", journal["epoch_id"],
                "--agent-id", "55555555-5555-4555-8555-555555555555",
                "--agent-role", "phase-worker",
                "--reason-code", "phase-contract",
            ]
            for event_type, action, status in (
                ("agent-spawn-requested", "spawn-agent", "requested"),
                ("agent-started", "start-agent", "started"),
                ("agent-completed", "complete-agent", "completed"),
            ):
                result = run_journal(
                    PORTS["codex"], "record", "--workspace", str(workspace),
                    "--event-type", event_type, *common,
                    "--action-code", action, "--status-code", status,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            result = run_journal(PORTS["codex"], "validate", "--workspace", str(workspace))
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(result.stdout)
            self.assertEqual(
                [event["sequence"] for event in document["events"]],
                list(range(1, len(document["events"]) + 1)),
            )
            for previous, current in zip(document["events"], document["events"][1:]):
                self.assertEqual(current["previous_event_sha256"], previous["event_sha256"])

    def test_record_requires_explicit_current_epoch_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, state = initialize_workspace(tmp)
            missing = run_journal(PORTS["codex"], "record", "--workspace", str(workspace), "--event-type", "phase-started")
            self.assertEqual(missing.returncode, 2, missing.stderr)
            stale = run_journal(
                PORTS["codex"],
                *record_args(workspace, state),
                "--phase", "phase-1",
            )
            self.assertEqual(stale.returncode, 2, stale.stderr)

    def test_record_rejects_sensitive_url_like_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, state = initialize_workspace(tmp)
            for path in ("https://user:secret@example.test/output", "output/report?token=secret", "output\\secret"):
                with self.subTest(path=path):
                    result = run_journal(
                        PORTS["codex"],
                        *record_args(workspace, state),
                        "--artifact-refs", json.dumps([{"path": path}]),
                    )
                    self.assertEqual(result.returncode, 2, result.stderr)

    def test_validate_rejects_tampered_event_grammar_and_integrity(self) -> None:
        mutations = ("unknown", "duplicate", "broken_hash", "noncontiguous", "trailing", "wrong_date")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                workspace, state = initialize_workspace(tmp)
                recorded = run_journal(PORTS["codex"], *record_args(workspace, state))
                self.assertEqual(recorded.returncode, 0, recorded.stderr)
                path = workspace / "output" / f"agent-conversation-{DATE}.md"
                raw = path.read_bytes()
                if mutation == "unknown":
                    raw = raw.replace(
                        b"```json\n{", b"```json\n{\"unknown\":\"value\",", 1
                    )
                elif mutation == "duplicate":
                    raw = raw.replace(b'"event_type":"phase-started"', b'"event_type":"phase-started","event_type":"phase-started"')
                elif mutation == "broken_hash":
                    raw = raw.replace(json.loads(recorded.stdout)["head_sha256"].encode(), b"0" * 64)
                elif mutation == "noncontiguous":
                    event = json.loads(raw.split(b"```json\n", 1)[1].split(b"\n```\n", 1)[0])
                    event["sequence"] = 2
                    unsigned = {key: value for key, value in event.items() if key != "event_sha256"}
                    event["event_sha256"] = __import__("hashlib").sha256(
                        json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
                    ).hexdigest()
                    raw = raw.split(b"```json\n", 1)[0] + b"```json\n" + json.dumps(
                        event, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                    ).encode() + b"\n```\n"
                elif mutation == "trailing":
                    raw += b"unexpected"
                else:
                    raw = raw.replace(f"Date: {DATE}".encode(), b"Date: 2026-08-27")
                path.write_bytes(raw)
                result = run_journal(PORTS["codex"], "validate", "--workspace", str(workspace))
                self.assertEqual(result.returncode, 2, result.stderr)

    def test_validate_rejects_symlink_and_wrong_mode(self) -> None:
        for mutation in ("symlink", "mode"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                workspace, _ = initialize_workspace(tmp)
                path = workspace / "output" / f"agent-conversation-{DATE}.md"
                if mutation == "symlink":
                    target = workspace / "output" / "journal-target.md"
                    target.write_bytes(path.read_bytes())
                    target.chmod(0o600)
                    path.unlink()
                    path.symlink_to(target)
                else:
                    path.chmod(0o644)
                result = run_journal(PORTS["codex"], "validate", "--workspace", str(workspace))
                self.assertEqual(result.returncode, 2, result.stderr)
