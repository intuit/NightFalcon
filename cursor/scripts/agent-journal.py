#!/usr/bin/env python3
"""Canonical, append-only NightFalcon agent conversation journal."""

from __future__ import annotations

import argparse
import base64
import copy
import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import tempfile
import time
import uuid
from typing import Any, Iterator


SCHEMA_VERSION = "1"
HEADER_SCHEMA = "NightFalcon Agent Journal v1"
EVENT_TYPES = frozenset({
    "run-initialized", "run-resumed", "user-intent-recorded", "phase-started",
    "agent-spawn-requested", "agent-started", "agent-completed", "agent-failed",
    "retry-scheduled", "decision-recorded", "validation-passed", "validation-failed",
    "phase-acceptance-prepared", "phase-accepted", "phase-acceptance-aborted",
    "artifact-bound", "recovery-started", "recovery-completed", "recovery-blocked",
    "run-finalized", "next-run-handoff",
})
SYSTEM_EVENT_TYPES = frozenset({
    "run-initialized", "run-resumed", "user-intent-recorded",
    "phase-acceptance-prepared",
    "phase-accepted", "phase-acceptance-aborted", "artifact-bound",
    "recovery-started", "recovery-completed", "recovery-blocked",
    "run-finalized", "next-run-handoff",
})
WORKER_EVENT_TYPES = EVENT_TYPES - SYSTEM_EVENT_TYPES
ALLOWED_EVENT_FIELDS = frozenset({
    "event_type", "session_id", "phase", "epoch_id", "agent_id",
    "parent_agent_id", "agent_role", "action_code", "status_code",
    "decision_code", "reason_code", "artifact_refs", "counts", "handoff",
    "checkpoint_uuid",
})
ALLOWED_REQUEST_FIELDS = ALLOWED_EVENT_FIELDS - {"handoff", "checkpoint_uuid"}
EVENT_REQUIRED_FIELDS = frozenset({
    "sequence", "event_id", "timestamp_utc", "session_id", "phase", "epoch_id",
    "event_type", "previous_event_sha256", "event_sha256",
})
EVENT_OPTIONAL_FIELDS = ALLOWED_EVENT_FIELDS - {"event_type", "session_id", "phase", "epoch_id"}
EVENT_FIELDS = EVENT_REQUIRED_FIELDS | EVENT_OPTIONAL_FIELDS
IDENTIFIER_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
ARTIFACT_PATH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,511}\Z")
FINDING_ID_RE = re.compile(r"F-[0-9]{3,}\Z")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
JOURNAL_NAME_RE = re.compile(r"agent-conversation-(\d{4}-\d{2}-\d{2})\.md\Z")
MAX_IDENTIFIER_BYTES = 128
MAX_ARTIFACT_REFS = 64
MAX_ACCEPTANCE_BINDINGS = 4096
MAX_COUNTS = 64
MAX_INTENT_REPOSITORIES = 64
HANDOFF_MAX_BYTES = 16_384
HANDOFF_MAX_REPOSITORIES = 64
HANDOFF_MAX_CHECKPOINTS = 64
HANDOFF_MAX_ARTIFACTS = 64
HANDOFF_HEADING = b"## Next-run handoff\n\n"
REVIEW_ORDER = tuple(f"phase-{number}" for number in range(9))
TRIAGE_ORDER = ("triage-ingest", "phase-4", "phase-5", "phase-6", "phase-7", "phase-8")
TRANSACTION_NAME = ".nightfalcon-journal-transaction.json"
LEGACY_TRANSACTION_VERSION = "1"
TRANSACTION_VERSION = "2"
CHECKPOINT_TRANSITION_VERSION_FIELD = "journal_transition_version"
INIT_TRANSACTION_NAME = ".nightfalcon-journal-init-transaction.json"
INIT_TRANSACTION_VERSION = "1"
RESUME_TRANSACTION_NAME = ".nightfalcon-journal-resume-transaction.json"
RESUME_TRANSACTION_VERSION = "1"
ACCEPTANCE_FAILPOINTS = frozenset({
    "after-intent", "after-prepared-append", "after-state-replace",
    "before-git-commit", "after-git-commit-before-ref",
})
INIT_FAILPOINTS = frozenset({
    "after-init-intent", "during-init-journal-write",
    "after-init-journal-fsync", "after-init-state-replace",
})
RESUME_FAILPOINTS = frozenset({
    "before-resume-transaction", "after-resume-intent", "during-resume-journal-write",
    "after-resume-journal-fsync", "after-resume-state-replace",
})

AGENT_ROLES = frozenset({
    "orchestrator", "phase-worker", "devils-advocate", "judge", "poc-generator",
    "poc-reviewer", "validator", "recovery-worker", "finalizer",
})
ACTION_CODES = frozenset({
    "initialize", "resume", "start-phase", "spawn-agent", "start-agent", "complete-agent",
    "fail-agent", "schedule-retry", "record-decision", "validate", "prepare-acceptance",
    "accept-phase", "abort-acceptance", "bind-artifact", "start-recovery", "complete-recovery",
    "block-recovery", "finalize-run", "create-handoff",
})
STATUS_CODES = frozenset({
    "requested", "started", "completed", "failed", "passed", "accepted", "aborted",
    "scheduled", "blocked", "recovered", "finalized", "pending", "interrupted", "skipped",
})
DECISION_CODES = frozenset({
    "accepted", "rejected", "deferred", "unresolved", "dismissed", "confirmed",
    "not-applicable", "blocked",
})
REASON_CODES = frozenset({
    "user-request", "phase-contract", "validation-failure", "evidence-gap",
    "adversarial-objection", "retry-policy", "resume-reconciliation", "system-finalizer",
    "worker-failure", "spawn-rejected", "acceptance-gate",
})
ARTIFACT_SECTION_CODES = frozenset({
    "checkpoint", "findings", "manifest", "report", "state", "validation",
})
JOURNAL_MODES = frozenset({"review", "triage"})
LIFECYCLE_EVENT_TYPES = frozenset({
    "agent-spawn-requested", "agent-started", "agent-completed", "agent-failed", "retry-scheduled",
})
ATTEMPT_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
SECRET_IDENTIFIER_RE = re.compile(
    r"(?:api[-_]?key|access[-_]?key|authorization|bearer|credential|"
    r"password|passwd|private[-_]?key|secret|token)",
    re.IGNORECASE,
)
COUNT_NAMES = frozenset({
    "repository-count", "review-mode", "triage-mode", "agents", "findings",
    "candidates", "artifacts", "repositories", "retries", "accepted",
    "abandoned", "rejected", "confirmed", "dismissed", "unresolved",
    "validated", "failed", "passed", "completed", "requested", "started",
    "scheduled", "interrupted", "blocked", "recovered", "P0", "P1", "P2",
    "P3", "P4", "recovery-started", "recovery-completed", "recovery-blocked",
}) | STATUS_CODES | DECISION_CODES | REASON_CODES
MAX_COUNT_VALUE = 1_000_000

BASE_EVENT_INPUT_FIELDS = frozenset({
    "event_type", "session_id", "phase", "epoch_id",
})

# Each vocabulary entry has one closed payload shape.  Optional values are
# explicitly enumerated; every code mapping that is not data-bearing is fixed.
EVENT_CONTRACTS: dict[str, dict[str, object]] = {
    "run-initialized": {
        "required": {"agent_role", "action_code", "status_code", "reason_code"},
        "fixed": {"agent_role": "orchestrator", "action_code": "initialize", "status_code": "started", "reason_code": "user-request"},
    },
    "run-resumed": {
        "required": {"agent_role", "action_code", "status_code", "reason_code"},
        "fixed": {"agent_role": "orchestrator", "action_code": "resume", "status_code": "recovered", "reason_code": "resume-reconciliation"},
    },
    "user-intent-recorded": {
        "required": {"agent_role", "action_code", "status_code", "decision_code", "reason_code", "counts"},
        "fixed": {"agent_role": "orchestrator", "action_code": "record-decision", "status_code": "completed", "decision_code": "accepted", "reason_code": "user-request"},
    },
    "phase-started": {
        "required": {"agent_role", "action_code", "status_code", "reason_code"},
        "fixed": {"agent_role": "orchestrator", "action_code": "start-phase", "status_code": "started", "reason_code": "phase-contract"},
    },
    "agent-spawn-requested": {
        "required": {"agent_id", "agent_role", "action_code", "status_code", "reason_code"},
        "optional": {"parent_agent_id"},
        "fixed": {"action_code": "spawn-agent", "status_code": "requested", "reason_code": "phase-contract"},
    },
    "agent-started": {
        "required": {"agent_id", "agent_role", "action_code", "status_code", "reason_code"},
        "optional": {"parent_agent_id"},
        "fixed": {"action_code": "start-agent", "status_code": "started", "reason_code": "phase-contract"},
    },
    "agent-completed": {
        "required": {"agent_id", "agent_role", "action_code", "status_code", "reason_code"},
        "optional": {"parent_agent_id", "counts"},
        "fixed": {"action_code": "complete-agent", "status_code": "completed", "reason_code": "phase-contract"},
    },
    "agent-failed": {
        "required": {"agent_id", "agent_role", "action_code", "status_code", "reason_code"},
        "optional": {"parent_agent_id", "counts"},
        "fixed": {"action_code": "fail-agent"},
        "pairs": {
            ("failed", "worker-failure"), ("failed", "spawn-rejected"),
            ("interrupted", "resume-reconciliation"),
        },
    },
    "retry-scheduled": {
        "required": {"agent_id", "agent_role", "action_code", "status_code", "reason_code"},
        "optional": {"parent_agent_id", "counts"},
        "fixed": {"action_code": "schedule-retry", "status_code": "scheduled", "reason_code": "retry-policy"},
    },
    "decision-recorded": {
        "required": {"agent_role", "action_code", "decision_code", "reason_code"},
        "optional": {"agent_id", "parent_agent_id", "artifact_refs", "counts"},
        "fixed": {"action_code": "record-decision"},
    },
    "validation-passed": {
        "required": {"agent_role", "action_code", "status_code", "reason_code"},
        "optional": {"agent_id", "parent_agent_id", "artifact_refs", "counts"},
        "fixed": {"agent_role": "validator", "action_code": "validate", "status_code": "passed", "reason_code": "phase-contract"},
    },
    "validation-failed": {
        "required": {"agent_role", "action_code", "status_code", "reason_code"},
        "optional": {"agent_id", "parent_agent_id", "artifact_refs", "counts"},
        "fixed": {"agent_role": "validator", "action_code": "validate", "status_code": "failed", "reason_code": "validation-failure"},
    },
    "phase-acceptance-prepared": {
        "required": {"agent_role", "action_code", "status_code", "reason_code", "artifact_refs", "checkpoint_uuid"},
        "fixed": {"agent_role": "orchestrator", "action_code": "prepare-acceptance", "status_code": "pending", "reason_code": "acceptance-gate"},
    },
    "phase-accepted": {
        "required": {"agent_role", "action_code", "status_code", "reason_code", "artifact_refs", "checkpoint_uuid"},
        "fixed": {"agent_role": "orchestrator", "action_code": "accept-phase", "status_code": "accepted", "reason_code": "acceptance-gate"},
    },
    "phase-acceptance-aborted": {
        "required": {"agent_role", "action_code", "status_code", "reason_code", "checkpoint_uuid"},
        "fixed": {"agent_role": "orchestrator", "action_code": "abort-acceptance", "status_code": "aborted", "reason_code": "resume-reconciliation"},
    },
    "artifact-bound": {
        "required": {"agent_role", "action_code", "status_code", "reason_code", "artifact_refs", "checkpoint_uuid"},
        "fixed": {"agent_role": "orchestrator", "action_code": "bind-artifact", "status_code": "accepted", "reason_code": "acceptance-gate"},
    },
    "recovery-started": {
        "required": {"agent_role", "action_code", "status_code", "reason_code"},
        "fixed": {"agent_role": "recovery-worker", "action_code": "start-recovery", "status_code": "started", "reason_code": "resume-reconciliation"},
    },
    "recovery-completed": {
        "required": {"agent_role", "action_code", "status_code", "reason_code"},
        "fixed": {"agent_role": "recovery-worker", "action_code": "complete-recovery", "status_code": "recovered", "reason_code": "resume-reconciliation"},
    },
    "recovery-blocked": {
        "required": {"agent_role", "action_code", "status_code", "reason_code"},
        "fixed": {"agent_role": "recovery-worker", "action_code": "block-recovery", "status_code": "blocked", "reason_code": "resume-reconciliation"},
    },
    "run-finalized": {
        "required": {"agent_role", "action_code", "status_code", "reason_code", "checkpoint_uuid"},
        "fixed": {"agent_role": "finalizer", "action_code": "finalize-run", "status_code": "finalized", "reason_code": "phase-contract"},
    },
    "next-run-handoff": {
        "required": {"agent_role", "action_code", "status_code", "reason_code", "checkpoint_uuid", "handoff"},
        "fixed": {"agent_role": "finalizer", "action_code": "create-handoff", "status_code": "completed", "reason_code": "phase-contract"},
    },
}


class JournalError(ValueError):
    """The journal or a journal request violates its closed contract."""


@dataclasses.dataclass(frozen=True)
class JournalDocument:
    path: pathlib.Path
    date: str
    session_id: str
    events: list[dict[str, Any]]
    header_length: int
    length: int
    event_end_offsets: list[int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "events": self.events,
            "session_id": self.session_id,
        }


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def event_digest(event: dict[str, object]) -> str:
    unsigned = {key: value for key, value in event.items() if key != "event_sha256"}
    return hashlib.sha256(canonical_json(unsigned)).hexdigest()


def journal_path(workspace: pathlib.Path, date: str) -> pathlib.Path:
    if not DATE_RE.fullmatch(date):
        raise JournalError("date must use YYYY-MM-DD")
    return workspace / "output" / f"agent-conversation-{date}.md"


def _header(date: str, session_id: str) -> bytes:
    return (
        "# NightFalcon Agent Conversation\n\n"
        f"Schema: {HEADER_SCHEMA}\n"
        f"Date: {date}\n"
        f"Workspace session: {session_id}\n"
        "Handling: Confidential structured run metadata; not a raw transcript.\n\n"
        "## Events\n\n"
    ).encode("utf-8")


def _json_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise JournalError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json_object(path: pathlib.Path) -> dict[str, Any]:
    try:
        raw, _mode = _read_regular_bytes(path, f"JSON object at {path}")
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_json_object_pairs
        )
    except (UnicodeError, json.JSONDecodeError, JournalError) as exc:
        raise JournalError(f"invalid JSON object at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise JournalError(f"JSON object required at {path}")
    return value


def _identifier(value: Any, field: str) -> str:
    if (not isinstance(value, str) or len(value.encode("utf-8")) > MAX_IDENTIFIER_BYTES
            or not IDENTIFIER_RE.fullmatch(value)):
        raise JournalError(f"{field} must be an identifier")
    return value


def _attempt_id(value: Any, field: str) -> str:
    normalized = _identifier(value, field)
    if SECRET_IDENTIFIER_RE.search(normalized):
        raise JournalError(f"{field} contains a prohibited secret-like token")
    if not ATTEMPT_ID_RE.fullmatch(normalized):
        raise JournalError(f"{field} must be an opaque UUIDv4 attempt ID")
    return normalized


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise JournalError(f"{field} must be a lowercase SHA-256 digest")
    return value


def safe_relative_path(raw: str) -> str:
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > 512:
        raise JournalError("artifact path must be a bounded string")
    if not ARTIFACT_PATH_RE.fullmatch(raw) or "\\" in raw or any(character in raw for character in ("@", "?", "#")):
        raise JournalError("artifact path must not contain URL or platform path syntax")
    if re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", raw):
        raise JournalError("artifact path must not contain a URI scheme")
    path = pathlib.PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise JournalError("artifact path must be workspace-relative")
    if any(ord(character) < 32 for character in raw):
        raise JournalError("artifact path contains a control character")
    return path.as_posix()


_safe_relative_path = safe_relative_path


def _enum(value: Any, field: str, allowed: frozenset[str]) -> str:
    normalized = _identifier(value, field)
    if normalized not in allowed:
        raise JournalError(f"{field} is not an allowed code")
    return normalized


def _artifact_selector(value: Any) -> str:
    if not isinstance(value, str):
        raise JournalError("artifact selector must be a finding ID or section code")
    if FINDING_ID_RE.fullmatch(value) or value in ARTIFACT_SECTION_CODES:
        return value
    raise JournalError("artifact selector must be a finding ID or section code")


def _validate_artifact_refs(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > MAX_ARTIFACT_REFS:
        raise JournalError("artifact_refs must be a bounded list")
    refs: list[dict[str, str]] = []
    for ref in value:
        if (not isinstance(ref, dict)
                or set(ref) - {"path", "selector", "sha256", "checkpoint_uuid"}
                or "path" not in ref):
            raise JournalError("artifact reference has an invalid schema")
        normalized = {"path": safe_relative_path(ref["path"])}
        if "selector" in ref:
            normalized["selector"] = _artifact_selector(ref["selector"])
        if "sha256" in ref:
            normalized["sha256"] = _sha256(ref["sha256"], "artifact sha256")
        if "checkpoint_uuid" in ref:
            normalized["checkpoint_uuid"] = _identifier(
                ref["checkpoint_uuid"], "artifact checkpoint_uuid"
            )
        refs.append(normalized)
    return refs


def _validate_binding_inventory(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > MAX_ACCEPTANCE_BINDINGS:
        raise JournalError("acceptance binding inventory is unbounded")
    bindings: list[dict[str, str]] = []
    for offset in range(0, len(value), MAX_ARTIFACT_REFS):
        bindings.extend(
            _validate_artifact_refs(value[offset:offset + MAX_ARTIFACT_REFS])
        )
    if bindings != sorted(bindings, key=canonical_json):
        raise JournalError("acceptance binding inventory is not canonical")
    if len({canonical_json(binding) for binding in bindings}) != len(bindings):
        raise JournalError("acceptance binding inventory contains duplicates")
    return bindings


def _validate_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or len(value) > MAX_COUNTS:
        raise JournalError("counts must be a bounded object")
    normalized: dict[str, int] = {}
    for key, count in value.items():
        normalized_key = _identifier(key, "count name")
        if SECRET_IDENTIFIER_RE.search(normalized_key):
            raise JournalError("count name contains a prohibited secret-like token")
        if normalized_key not in COUNT_NAMES:
            raise JournalError("count name is not in the closed vocabulary")
        normalized[normalized_key] = count
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            or count > MAX_COUNT_VALUE
        ):
            raise JournalError("counts must contain bounded nonnegative integers")
    return normalized


def _validate_event_contract(
    value: dict[str, Any], *, public: bool
) -> None:
    event_type = value.get("event_type")
    contract = EVENT_CONTRACTS.get(event_type)
    if contract is None:
        raise JournalError("event_type is invalid")
    generated = {
        "sequence", "event_id", "timestamp_utc", "previous_event_sha256",
        "event_sha256",
    }
    observed = set(value) - generated
    required = BASE_EVENT_INPUT_FIELDS | set(contract.get("required", set()))
    optional = set(contract.get("optional", set()))
    if not required <= observed or observed - required - optional:
        raise JournalError(f"{event_type} event does not match its closed field schema")
    fixed = contract.get("fixed", {})
    if not isinstance(fixed, dict) or any(
        value.get(field) != expected for field, expected in fixed.items()
    ):
        raise JournalError(f"{event_type} event has a noncanonical code mapping")
    pairs = contract.get("pairs")
    if pairs is not None and (
        value.get("status_code"), value.get("reason_code")
    ) not in pairs:
        raise JournalError(f"{event_type} event has a noncanonical status/reason pair")
    if public and event_type == "agent-failed" and (
        value.get("status_code"), value.get("reason_code")
    ) not in {("failed", "worker-failure"), ("failed", "spawn-rejected")}:
        raise JournalError("workers cannot record system interruption events")
    for field in ("agent_id", "parent_agent_id"):
        if field in value:
            _attempt_id(value[field], field)


def _validate_user_intent_event(event: dict[str, Any]) -> None:
    fields = EVENT_REQUIRED_FIELDS | {
        "agent_role", "action_code", "status_code", "decision_code",
        "reason_code", "counts",
    }
    expected = {
        "agent_role": "orchestrator",
        "action_code": "record-decision",
        "status_code": "completed",
        "decision_code": "accepted",
        "reason_code": "user-request",
    }
    counts = _validate_counts(event.get("counts"))
    mode_counts = (counts.get("review-mode"), counts.get("triage-mode"))
    if (
        set(event) != fields
        or any(event.get(field) != value for field, value in expected.items())
        or set(counts) != {
            "repository-count", "review-mode", "triage-mode",
        }
        or not 1 <= counts["repository-count"] <= MAX_INTENT_REPOSITORIES
        or mode_counts not in {(1, 0), (0, 1)}
    ):
        raise JournalError("user-intent-recorded event is not canonical")


def _validate_request(request: dict[str, object], event_types: frozenset[str]) -> dict[str, object]:
    if not isinstance(request, dict) or set(request) - ALLOWED_REQUEST_FIELDS:
        raise JournalError("request contains unknown or invalid fields")
    if "event_type" not in request or request["event_type"] not in event_types:
        raise JournalError("event_type is invalid")
    normalized: dict[str, object] = {"event_type": request["event_type"]}
    for field in ("session_id", "phase", "epoch_id"):
        if field in request:
            normalized[field] = _identifier(request[field], field)
    for field in ("agent_id", "parent_agent_id"):
        if field in request:
            normalized[field] = _attempt_id(request[field], field)
    for field, allowed in (
        ("agent_role", AGENT_ROLES), ("action_code", ACTION_CODES), ("status_code", STATUS_CODES),
        ("decision_code", DECISION_CODES), ("reason_code", REASON_CODES),
    ):
        if field in request:
            normalized[field] = _enum(request[field], field, allowed)
    if "artifact_refs" in request:
        normalized["artifact_refs"] = _validate_artifact_refs(request["artifact_refs"])
        if event_types == WORKER_EVENT_TYPES and any(
            "sha256" in ref or "checkpoint_uuid" in ref
            for ref in normalized["artifact_refs"]
        ):
            raise JournalError(
                "worker artifact references must remain provisional"
            )
    if "counts" in request:
        normalized["counts"] = _validate_counts(request["counts"])
    if event_types == WORKER_EVENT_TYPES:
        _validate_event_contract(normalized, public=True)
        if normalized["event_type"] == "decision-recorded" and normalized.get(
            "decision_code"
        ) in {"unresolved", "deferred"}:
            refs = normalized.get("artifact_refs")
            if not isinstance(refs, list) or not any(
                FINDING_ID_RE.fullmatch(ref.get("selector", "")) for ref in refs
            ):
                raise JournalError(
                    "unresolved decisions require a scoped finding selector"
                )
    return normalized


def validate_request(request: dict[str, object]) -> dict[str, object]:
    return _validate_request(request, WORKER_EVENT_TYPES)


def _validate_event(event: Any) -> dict[str, Any]:
    if not isinstance(event, dict) or set(event) - EVENT_FIELDS or not EVENT_REQUIRED_FIELDS <= set(event):
        raise JournalError("event has an invalid field set")
    if isinstance(event["sequence"], bool) or not isinstance(event["sequence"], int) or event["sequence"] < 1:
        raise JournalError("event sequence must be positive")
    _identifier(event["event_id"], "event_id")
    if not isinstance(event["timestamp_utc"], str) or not TIMESTAMP_RE.fullmatch(event["timestamp_utc"]):
        raise JournalError("timestamp_utc must be UTC seconds")
    for field in ("session_id", "phase", "epoch_id"):
        _identifier(event[field], field)
    if "checkpoint_uuid" in event:
        _identifier(event["checkpoint_uuid"], "checkpoint_uuid")
    if event["event_type"] not in EVENT_TYPES:
        raise JournalError("event_type is invalid")
    if event["previous_event_sha256"] is not None:
        _sha256(event["previous_event_sha256"], "previous_event_sha256")
    _sha256(event["event_sha256"], "event_sha256")
    _validate_request({key: event[key] for key in event if key in ALLOWED_REQUEST_FIELDS}, EVENT_TYPES)
    if event["event_type"] == "next-run-handoff" and "handoff" not in event:
        raise JournalError("next-run-handoff requires a system handoff")
    if "handoff" in event and event["event_type"] != "next-run-handoff":
        raise JournalError("handoff is reserved for system next-run-handoff events")
    if "handoff" in event:
        _validate_handoff(event["handoff"])
    if "artifact_refs" in event:
        _validate_artifact_refs(event["artifact_refs"])
    if "counts" in event:
        _validate_counts(event["counts"])
    _validate_event_contract(event, public=False)
    if event["event_type"] == "user-intent-recorded":
        _validate_user_intent_event(event)
    if event_digest(event) != event["event_sha256"]:
        raise JournalError("event SHA-256 does not match canonical event")
    return event


def _checked_journal_bytes(path: pathlib.Path) -> bytes:
    try:
        info = path.lstat()
    except OSError as exc:
        raise JournalError(f"journal is unavailable: {exc}") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
    ):
        raise JournalError(
            "journal path must be a regular non-symlink, non-hardlinked file"
        )
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise JournalError("journal mode must be 0600")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise JournalError(f"journal cannot be read: {exc}") from exc


def _validate_lifecycle_history(events: list[dict[str, Any]]) -> None:
    attempts: dict[str, dict[str, Any]] = {}
    for event in events:
        if event["event_type"] not in LIFECYCLE_EVENT_TYPES:
            continue
        agent_id = _attempt_id(event.get("agent_id"), "agent_id")
        signature = (
            event["session_id"], event["phase"], event["epoch_id"],
            event.get("parent_agent_id"), event["agent_role"],
        )
        event_type = event["event_type"]
        if event_type == "agent-spawn-requested":
            if agent_id in attempts:
                raise JournalError("agent attempt IDs must be globally unique")
            attempts[agent_id] = {
                "signature": signature, "state": "requested", "retried": False,
            }
            continue
        attempt = attempts.get(agent_id)
        if attempt is None or attempt["signature"] != signature:
            raise JournalError("agent lifecycle does not match its requested attempt")
        if event_type == "agent-started":
            if attempt["state"] != "requested":
                raise JournalError("agent-started must follow one request")
            attempt["state"] = "started"
        elif event_type in {"agent-completed", "agent-failed"}:
            pre_start_failure = (
                event_type == "agent-failed"
                and attempt["state"] == "requested"
                and (
                    (
                        event.get("status_code") == "failed"
                        and event.get("reason_code") == "spawn-rejected"
                    )
                    or event.get("status_code") == "interrupted"
                )
            )
            if attempt["state"] != "started" and not pre_start_failure:
                raise JournalError("agent terminal event must follow one start")
            if (
                attempt["state"] == "started"
                and event.get("reason_code") == "spawn-rejected"
            ):
                raise JournalError("spawn-rejected is valid only before agent-started")
            attempt["state"] = (
                "failed" if event_type == "agent-failed" else "completed"
            )
        else:
            if attempt["state"] != "failed" or attempt["retried"]:
                raise JournalError("retry-scheduled must follow one failed terminal")
            attempt["retried"] = True


def _validate_lifecycle_append(
    document: JournalDocument, request: dict[str, object]
) -> None:
    if request["event_type"] not in LIFECYCLE_EVENT_TYPES:
        return
    candidate: dict[str, Any] = dict(request)
    # Only lifecycle ordering and identity fields are inspected here; generated
    # integrity metadata is intentionally irrelevant to the state machine.
    history = [
        {key: value for key, value in event.items() if key not in {
            "sequence", "event_id", "timestamp_utc", "previous_event_sha256",
            "event_sha256",
        }}
        for event in document.events
    ]
    history.append(candidate)
    _validate_lifecycle_history(history)


def parse_journal(path: pathlib.Path) -> JournalDocument:
    match = JOURNAL_NAME_RE.fullmatch(path.name)
    if not match:
        raise JournalError("journal filename is invalid")
    date = match.group(1)
    raw = _checked_journal_bytes(path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JournalError("journal is not UTF-8") from exc
    header_match = re.match(
        r"# NightFalcon Agent Conversation\n\nSchema: NightFalcon Agent Journal v1\nDate: (\d{4}-\d{2}-\d{2})\n"
        r"Workspace session: ([A-Za-z0-9._:-]{1,128})\n"
        r"Handling: Confidential structured run metadata; not a raw transcript\.\n\n## Events\n\n",
        text,
    )
    if header_match is None or header_match.group(1) != date:
        raise JournalError("journal header is invalid")
    session_id = header_match.group(2)
    header = _header(date, session_id)
    if not raw.startswith(header):
        raise JournalError("journal header is not canonical")
    cursor = len(header)
    events: list[dict[str, Any]] = []
    offsets: list[int] = []
    intent_epochs: set[str] = set()
    previous: str | None = None
    while cursor < len(raw):
        has_handoff_heading = raw.startswith(HANDOFF_HEADING, cursor)
        if has_handoff_heading:
            cursor += len(HANDOFF_HEADING)
        if not raw.startswith(b"```json\n", cursor):
            raise JournalError("journal contains trailing or noncanonical bytes")
        end = raw.find(b"\n```\n", cursor + 8)
        if end < 0:
            raise JournalError("journal event fence is incomplete")
        payload = raw[cursor + 8:end]
        if not payload or b"\n" in payload:
            raise JournalError("journal event JSON must occupy one canonical line")
        try:
            event = json.loads(payload.decode("ascii"), object_pairs_hook=_json_object_pairs)
        except (UnicodeDecodeError, json.JSONDecodeError, JournalError) as exc:
            raise JournalError(f"journal event JSON is invalid: {exc}") from exc
        if canonical_json(event) != payload:
            raise JournalError("journal event JSON is not canonical")
        event = _validate_event(event)
        if has_handoff_heading != (event["event_type"] == "next-run-handoff"):
            raise JournalError("next-run-handoff must use its fixed Markdown heading")
        expected_sequence = len(events) + 1
        if event["sequence"] != expected_sequence or event["previous_event_sha256"] != previous:
            raise JournalError("journal hash chain is not contiguous")
        if event["session_id"] != session_id:
            raise JournalError("journal event session differs from header")
        if event["event_type"] == "user-intent-recorded":
            if event["epoch_id"] in intent_epochs:
                raise JournalError(
                    "journal contains multiple user intent events for one epoch"
                )
            intent_epochs.add(event["epoch_id"])
        previous = event["event_sha256"]
        cursor = end + len(b"\n```\n")
        events.append(event)
        offsets.append(cursor)
    _validate_lifecycle_history(events)
    return JournalDocument(path, date, session_id, events, len(header), len(raw), offsets)


@contextlib.contextmanager
def workspace_lock(workspace: pathlib.Path) -> Iterator[None]:
    lock_path = workspace / ".nightfalcon-journal.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _journal_state(state: dict[str, Any]) -> dict[str, Any]:
    journal = state.get("agent_journal")
    if not isinstance(journal, dict) or journal.get("schema_version") != SCHEMA_VERSION:
        raise JournalError("state does not contain a v1 agent_journal")
    return journal


def _current_journal_path(workspace: pathlib.Path, state: dict[str, Any]) -> pathlib.Path:
    journal = _journal_state(state)
    date = state.get("date")
    if not isinstance(date, str) or not DATE_RE.fullmatch(date):
        raise JournalError("state date must use YYYY-MM-DD")
    expected = journal_path(workspace, date)
    if journal.get("filename") != expected.name:
        raise JournalError("state journal filename does not match state date")
    return expected


def _validate_accepted_prefix(document: JournalDocument, journal: dict[str, Any]) -> None:
    sequence = journal.get("accepted_sequence")
    length = journal.get("accepted_length")
    head = journal.get("accepted_head_sha256")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0 or sequence > len(document.events):
        raise JournalError("accepted sequence is invalid")
    if isinstance(length, bool) or not isinstance(length, int) or length < document.header_length or length > document.length:
        raise JournalError("accepted length is invalid")
    expected_length = document.header_length if sequence == 0 else document.event_end_offsets[sequence - 1]
    expected_head = None if sequence == 0 else document.events[sequence - 1]["event_sha256"]
    if length != expected_length or head != expected_head:
        raise JournalError("accepted journal prefix differs from state")


def _authoritative_checkpoint_keys(
    entries: Any, field: str
) -> set[tuple[str, str]]:
    if not isinstance(entries, list):
        raise JournalError(f"{field} phase acceptance checkpoints must be a list")
    keys: set[tuple[str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise JournalError(
                f"{field} phase acceptance checkpoint entry must be an object"
            )
        if entry.get("checkpoint_uuid") is None:
            continue
        key = (
            _identifier(entry.get("phase"), f"{field} checkpoint phase"),
            _identifier(
                entry.get("checkpoint_uuid"), f"{field} checkpoint_uuid"
            ),
        )
        if key in keys:
            raise JournalError(
                f"{field} phase acceptance checkpoint entries must be unique"
            )
        keys.add(key)
    return keys


def _canonical_phase_acceptance(
    events: list[dict[str, Any]], index: int
) -> tuple[str, str] | None:
    event = events[index]
    if event.get("event_type") != "phase-accepted" or index == 0:
        return None
    fields = EVENT_REQUIRED_FIELDS | {
        "agent_role", "action_code", "status_code", "reason_code",
        "artifact_refs", "checkpoint_uuid",
    }
    if set(event) != fields:
        return None
    key = (event["phase"], event["checkpoint_uuid"])
    if any(
        event.get(field) != value
        for field, value in {
            "agent_role": "orchestrator",
            "action_code": "accept-phase",
            "status_code": "accepted",
            "reason_code": "acceptance-gate",
        }.items()
    ):
        return None
    refs = _validate_artifact_refs(event["artifact_refs"])
    if any(
        ref.get("checkpoint_uuid") != event["checkpoint_uuid"]
        or "sha256" not in ref
        for ref in refs
    ):
        return None
    prepared = events[index - 1]
    if set(prepared) != fields or any(
        prepared.get(field) != value
        for field, value in {
            "event_type": "phase-acceptance-prepared",
            "agent_role": "orchestrator",
            "action_code": "prepare-acceptance",
            "status_code": "pending",
            "reason_code": "acceptance-gate",
        }.items()
    ):
        return None
    shared = {
        "session_id", "phase", "epoch_id", "timestamp_utc",
        "reason_code", "artifact_refs", "checkpoint_uuid",
    }
    if any(prepared.get(field) != event.get(field) for field in shared):
        return None
    return key


def _checkpoint_binding_refs(
    events: list[dict[str, Any]], accepted: dict[str, Any]
) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for event in events:
        if (
            event.get("phase") != accepted.get("phase")
            or event.get("epoch_id") != accepted.get("epoch_id")
            or event.get("checkpoint_uuid") != accepted.get("checkpoint_uuid")
        ):
            continue
        if event["event_type"] == "artifact-bound":
            refs.extend(_validate_artifact_refs(event.get("artifact_refs")))
    # A single-event inventory remains readable for v1 checkpoints created
    # before chunk support, while new writers use artifact-bound exclusively.
    refs.extend(_validate_artifact_refs(accepted.get("artifact_refs", [])))
    return _validate_binding_inventory(refs)


def _accepted_fact_selection(
    state: dict[str, Any],
    manifest: dict[str, Any],
    accepted_prefix: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Select facts from epochs proven accepted inside an immutable prefix."""
    state_keys = _authoritative_checkpoint_keys(
        state.get("git_checkpoints", []), "state"
    )
    manifest_keys = _authoritative_checkpoint_keys(
        manifest.get("checkpoints", []), "manifest"
    )
    if state_keys != manifest_keys:
        raise JournalError(
            "state and manifest phase acceptance checkpoints differ"
        )
    completed = set(_validated_phases(manifest))
    phase_status = state.get("phase_status", {})
    if not isinstance(phase_status, dict):
        raise JournalError("state phase_status must be an object")
    if any(
        phase not in completed or phase_status.get(phase) != "completed"
        for phase, _ in state_keys
    ):
        raise JournalError(
            "authoritative phase acceptance checkpoint is not completed"
        )
    candidates: dict[tuple[str, str], list[dict[str, Any]]] = {}
    transition_indices = {
        index for index, event in enumerate(accepted_prefix)
        if event["event_type"] in {
            "phase-acceptance-prepared", "phase-accepted"
        }
    }
    canonical_indices: set[int] = set()
    for index in range(len(accepted_prefix)):
        key = _canonical_phase_acceptance(accepted_prefix, index)
        if key is not None:
            candidates.setdefault(key, []).append(accepted_prefix[index])
            canonical_indices.update({index - 1, index})
    if transition_indices != canonical_indices:
        raise JournalError("accepted prefix has noncanonical phase acceptance")
    if any(len(events) != 1 for events in candidates.values()):
        raise JournalError("accepted prefix has ambiguous phase acceptance")
    if set(candidates) != state_keys:
        raise JournalError(
            "accepted prefix phase acceptance set differs from authoritative checkpoints"
        )
    acceptance_events = sorted(
        (events[0] for events in candidates.values()),
        key=lambda event: event["sequence"],
    )
    accepted_epochs = {event["epoch_id"] for event in acceptance_events}
    facts = [
        event for event in accepted_prefix
        if event["epoch_id"] in accepted_epochs
    ]
    return facts, acceptance_events


def _accepted_fact_events(
    state: dict[str, Any],
    manifest: dict[str, Any],
    accepted_prefix: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return _accepted_fact_selection(state, manifest, accepted_prefix)[0]


def _append_fsync(path: pathlib.Path, block: bytes) -> None:
    _checked_journal_bytes(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
    try:
        _write_all(descriptor, block)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _render_event(event: dict[str, object]) -> bytes:
    heading = HANDOFF_HEADING if event["event_type"] == "next-run-handoff" else b""
    return heading + b"```json\n" + canonical_json(event) + b"\n```\n"


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    written = 0
    while written < len(view):
        count = os.write(descriptor, view[written:])
        if count <= 0:
            raise OSError("short write while persisting journal")
        written += count


def _now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_event(request: dict[str, object], document: JournalDocument) -> dict[str, object]:
    event: dict[str, object] = dict(request)
    event["sequence"] = len(document.events) + 1
    event["event_id"] = str(uuid.uuid4())
    event["timestamp_utc"] = _now_utc()
    event["previous_event_sha256"] = document.events[-1]["event_sha256"] if document.events else None
    event["event_sha256"] = event_digest(event)
    return event


def record_event(workspace: pathlib.Path, request: dict[str, object]) -> dict[str, object]:
    with workspace_lock(workspace):
        _fence_pending_init_transaction(workspace)
        if _path_exists_no_follow(workspace / RESUME_TRANSACTION_NAME):
            raise JournalError("pending resume transaction requires reconciliation")
        if _load_transaction(workspace) is not None:
            raise JournalError("pending acceptance transaction fences journal recording")
        state = _load_json_object(workspace / "state.json")
        journal = _journal_state(state)
        required_tokens = ("session_id", "phase", "epoch_id")
        if any(token not in request for token in required_tokens):
            raise JournalError("record request must include session_id, phase, and epoch_id")
        if journal.get("epoch_phase") != state.get("current_phase"):
            raise JournalError("journal epoch phase differs from current state")
        if (request["session_id"] != journal.get("session_id")
                or request["phase"] != state.get("current_phase")
                or request["phase"] != journal.get("epoch_phase")
                or request["epoch_id"] != journal.get("epoch_id")):
            raise JournalError("stale epoch")
        normalized = validate_request(request)
        if journal.get("epoch_status") != "open":
            raise JournalError("journal epoch is not open")
        document = parse_journal(_current_journal_path(workspace, state))
        if document.session_id != journal.get("session_id"):
            raise JournalError("journal session differs from state")
        _validate_accepted_prefix(document, journal)
        _validate_lifecycle_append(document, normalized)
        event = _build_event(normalized, document)
        _append_fsync(document.path, _render_event(event))
        return {"event_id": event["event_id"], "sequence": event["sequence"], "head_sha256": event["event_sha256"]}


def _canonical_intent_request(
    state: dict[str, Any], journal: dict[str, Any]
) -> dict[str, object]:
    mode = _enum(state.get("mode"), "state mode", JOURNAL_MODES)
    raw_repo_slugs = state.get("repo_slugs")
    if (
        not isinstance(raw_repo_slugs, list)
        or not 1 <= len(raw_repo_slugs) <= MAX_INTENT_REPOSITORIES
    ):
        raise JournalError("state repo_slugs must be a bounded nonempty list")
    repo_slugs = [
        _identifier(repo_slug, "state repo_slug")
        for repo_slug in raw_repo_slugs
    ]
    if len(set(repo_slugs)) != len(repo_slugs):
        raise JournalError("state repo_slugs must be unique")
    if mode == "triage" and len(repo_slugs) != 1:
        raise JournalError("triage user intent requires exactly one repository")
    return _validate_request({
        "event_type": "user-intent-recorded",
        "session_id": journal["session_id"],
        "phase": journal["epoch_phase"],
        "epoch_id": journal["epoch_id"],
        "agent_role": "orchestrator",
        "action_code": "record-decision",
        "status_code": "completed",
        "decision_code": "accepted",
        "reason_code": "user-request",
        "counts": {
            "repository-count": len(repo_slugs),
            "review-mode": int(mode == "review"),
            "triage-mode": int(mode == "triage"),
        },
    }, EVENT_TYPES)


def record_intent(workspace: pathlib.Path) -> dict[str, object]:
    with workspace_lock(workspace):
        state, manifest, _accepted, document = _load_authoritative_context_locked(
            workspace
        )
        journal = _journal_state(state)
        current_phase = _identifier(
            state.get("current_phase"), "state current_phase"
        )
        if current_phase == "done":
            return _validate_completed_finalization(state, manifest, document)
        if journal.get("epoch_phase") != current_phase:
            raise JournalError("journal epoch phase differs from current state")
        if journal.get("epoch_status") != "open":
            raise JournalError("journal epoch is not open")

        expected = _canonical_intent_request(state, journal)
        existing = [
            event for event in document.events
            if event["event_type"] == "user-intent-recorded"
            and event["session_id"] == journal["session_id"]
            and event["epoch_id"] == journal["epoch_id"]
        ]
        if len(existing) > 1:
            raise JournalError("current epoch has multiple user intent events")
        if existing:
            event = existing[0]
            observed = {
                field: event[field]
                for field in ALLOWED_REQUEST_FIELDS
                if field in event
            }
            if observed != expected:
                raise JournalError(
                    "current epoch user intent conflicts with authoritative state"
                )
        else:
            event = _build_event(expected, document)
            _append_fsync(document.path, _render_event(event))
        return {
            "event_id": event["event_id"],
            "sequence": event["sequence"],
            "head_sha256": event["event_sha256"],
        }


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True).encode("utf-8") + b"\n"


def _write_bytes_atomic(path: pathlib.Path, value: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.chmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _write_json_atomic(path: pathlib.Path, value: dict[str, Any]) -> None:
    _write_bytes_atomic(path, _json_bytes(value))


def _path_exists_no_follow(path: pathlib.Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise JournalError(f"initialization participant cannot be inspected: {exc}") from exc
    return True


def _read_regular_bytes(
    path: pathlib.Path,
    field: str,
    expected_mode: int | None = None,
) -> tuple[bytes, int]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise JournalError(f"{field} is unavailable: {exc}") from exc
    mode = stat.S_IMODE(before.st_mode)
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        raise JournalError(
            f"{field} must be a regular non-symlink, non-hardlinked file"
        )
    if expected_mode is not None and mode != expected_mode:
        raise JournalError(f"{field} mode differs from initialization intent")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise JournalError(f"{field} cannot be opened safely: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or stat.S_IMODE(opened.st_mode) != mode
            or opened.st_nlink != 1
        ):
            raise JournalError(f"{field} changed while it was inspected")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), mode
    finally:
        os.close(descriptor)


def _require_real_directory(path: pathlib.Path, field: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise JournalError(f"{field} is unavailable: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise JournalError(f"{field} must be a non-symlink directory")


def _fsync_directory(path: pathlib.Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _decode_json_object_bytes(value: bytes, field: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value.decode("utf-8"), object_pairs_hook=_json_object_pairs)
    except (UnicodeError, json.JSONDecodeError, JournalError) as exc:
        raise JournalError(f"transaction {field} is not a JSON object: {exc}") from exc
    if not isinstance(decoded, dict):
        raise JournalError(f"transaction {field} must be a JSON object")
    return decoded


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _base64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode_base64(value: Any, field: str) -> bytes:
    if not isinstance(value, str):
        raise JournalError(f"transaction {field} must be base64 text")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeError, ValueError) as exc:
        raise JournalError(f"transaction {field} is not canonical base64") from exc
    if _base64(decoded) != value:
        raise JournalError(f"transaction {field} is not canonical base64")
    return decoded


def _init_failpoint() -> str | None:
    specific = os.environ.get("NIGHTFALCON_JOURNAL_INIT_FAILPOINT")
    shared = os.environ.get("NIGHTFALCON_JOURNAL_FAILPOINT")
    if specific is not None and shared is not None and specific != shared:
        raise JournalError("conflicting initialization failpoints")
    configured = specific if specific is not None else shared
    if configured is not None and configured not in INIT_FAILPOINTS:
        raise JournalError("unknown initialization failpoint")
    return configured


def _raise_init_failpoint(configured: str | None, name: str) -> None:
    if configured == name:
        raise JournalError(f"injected initialization failure: {name}")


def _initial_journal_events(
    value: bytes,
    date: str,
    session_id: str,
    phase: str,
    epoch_id: str,
    state: dict[str, Any],
    journal: dict[str, Any],
) -> list[dict[str, Any]]:
    header = _header(date, session_id)
    if not value.startswith(header):
        raise JournalError("initialization journal header is not canonical")
    suffix = value[len(header):]
    events, _head, _next_sequence = _decode_transaction_events(
        suffix, "initialization journal", session_id, None, 1
    )
    if not events:
        raise JournalError("initialization journal has no event")
    event = events[0]
    expected_fields = EVENT_REQUIRED_FIELDS | {
        "agent_role", "action_code", "status_code", "reason_code",
    }
    if set(event) != expected_fields:
        raise JournalError("initialization journal event has an invalid closed schema")
    expected = {
        "sequence": 1,
        "session_id": session_id,
        "phase": phase,
        "epoch_id": epoch_id,
        "event_type": "run-initialized",
        "agent_role": "orchestrator",
        "action_code": "initialize",
        "status_code": "started",
        "reason_code": "user-request",
        "previous_event_sha256": None,
    }
    if any(event.get(field) != expected_value for field, expected_value in expected.items()):
        raise JournalError("initialization journal event identity is inconsistent")
    try:
        expected_intent = _canonical_intent_request(state, journal)
    except JournalError:
        expected_intent = None
    expected_count = 2 if expected_intent is not None else 1
    if len(events) != expected_count:
        raise JournalError("initialization journal event count is inconsistent")
    if expected_intent is not None:
        intent = events[1]
        if _request_projection(intent) != expected_intent:
            raise JournalError("initialization journal user intent differs from state")
        if intent["timestamp_utc"] != event["timestamp_utc"]:
            raise JournalError("initialization journal event timestamps differ")
    return events


def _decode_init_transaction(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version", "operation", "date", "session_id", "epoch_id",
        "checkpoint_uuid", "journal_filename", "state_mode", "journal_mode",
        "state", "journal",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise JournalError("initialization transaction has an invalid closed schema")
    if value.get("schema_version") != INIT_TRANSACTION_VERSION:
        raise JournalError("initialization transaction version is invalid")
    if value.get("operation") != "initialize":
        raise JournalError("initialization transaction operation is invalid")
    date = value.get("date")
    if not isinstance(date, str) or not DATE_RE.fullmatch(date):
        raise JournalError("initialization transaction date is invalid")
    session_id = _identifier(
        value.get("session_id"), "initialization transaction session_id"
    )
    epoch_id = _identifier(
        value.get("epoch_id"), "initialization transaction epoch_id"
    )
    checkpoint_uuid = _identifier(
        value.get("checkpoint_uuid"),
        "initialization transaction checkpoint_uuid",
    )
    journal_filename = value.get("journal_filename")
    expected_filename = f"agent-conversation-{date}.md"
    if journal_filename != expected_filename:
        raise JournalError("initialization transaction journal path is invalid")
    state_mode = value.get("state_mode")
    journal_mode = value.get("journal_mode")
    for mode, field in (
        (state_mode, "state_mode"), (journal_mode, "journal_mode")
    ):
        if (
            isinstance(mode, bool)
            or not isinstance(mode, int)
            or mode < 0
            or mode > 0o7777
        ):
            raise JournalError(f"initialization transaction {field} is invalid")
    if journal_mode != 0o600:
        raise JournalError("initialization transaction journal mode must be 0600")

    state_record = value.get("state")
    state_fields = {
        "old_base64", "old_sha256", "new_base64", "new_sha256",
    }
    if not isinstance(state_record, dict) or set(state_record) != state_fields:
        raise JournalError("initialization transaction state has an invalid schema")
    old_state = _decode_base64(
        state_record["old_base64"], "initialization state.old_base64"
    )
    new_state = _decode_base64(
        state_record["new_base64"], "initialization state.new_base64"
    )
    if (
        _digest_bytes(old_state)
        != _sha256(
            state_record["old_sha256"], "initialization state.old_sha256"
        )
        or _digest_bytes(new_state)
        != _sha256(
            state_record["new_sha256"], "initialization state.new_sha256"
        )
    ):
        raise JournalError("initialization transaction state bytes differ from digest")
    old_state_object = _decode_json_object_bytes(old_state, "initialization state.old")
    new_state_object = _decode_json_object_bytes(new_state, "initialization state.new")
    if "agent_journal" in old_state_object:
        raise JournalError("initialization transaction old state is already initialized")
    if old_state_object.get("date") not in (None, date):
        raise JournalError("initialization transaction date differs from old state")
    phase = _identifier(
        old_state_object.get("current_phase"),
        "initialization transaction current_phase",
    )
    expected_journal_state = {
        "schema_version": SCHEMA_VERSION,
        "filename": journal_filename,
        "session_id": session_id,
        "epoch_id": epoch_id,
        "epoch_phase": phase,
        "epoch_status": "open",
        "accepted_sequence": 0,
        "accepted_length": len(_header(date, session_id)),
        "accepted_head_sha256": None,
        "checkpoint_uuid": checkpoint_uuid,
    }
    expected_new_state = copy.deepcopy(old_state_object)
    expected_new_state["date"] = date
    expected_new_state["agent_journal"] = expected_journal_state
    if new_state_object != expected_new_state or new_state != _json_bytes(new_state_object):
        raise JournalError("initialization transaction new state is not exact")

    journal_record = value.get("journal")
    if not isinstance(journal_record, dict) or set(journal_record) != {
        "full_base64", "full_sha256",
    }:
        raise JournalError("initialization transaction journal has an invalid schema")
    full_journal = _decode_base64(
        journal_record["full_base64"], "initialization journal.full_base64"
    )
    if _digest_bytes(full_journal) != _sha256(
        journal_record["full_sha256"], "initialization journal.full_sha256"
    ):
        raise JournalError("initialization transaction journal bytes differ from digest")
    events = _initial_journal_events(
        full_journal, date, session_id, phase, epoch_id,
        new_state_object, expected_journal_state,
    )
    return {
        "date": date,
        "session_id": session_id,
        "epoch_id": epoch_id,
        "checkpoint_uuid": checkpoint_uuid,
        "phase": phase,
        "journal_filename": journal_filename,
        "state_mode": state_mode,
        "journal_mode": journal_mode,
        "old_state": old_state,
        "new_state": new_state,
        "full_journal": full_journal,
        "initial_events": events,
    }


def _load_init_transaction(workspace: pathlib.Path) -> dict[str, Any] | None:
    path = workspace / INIT_TRANSACTION_NAME
    if not _path_exists_no_follow(path):
        return None
    raw, _ = _read_regular_bytes(
        path, "initialization transaction", expected_mode=0o600
    )
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_json_object_pairs
        )
    except (UnicodeError, json.JSONDecodeError, JournalError) as exc:
        raise JournalError(f"initialization transaction is invalid JSON: {exc}") from exc
    if not isinstance(value, dict) or _json_bytes(value) != raw:
        raise JournalError("initialization transaction JSON is not canonical")
    return _decode_init_transaction(value)


def _build_init_transaction(
    old_state: bytes,
    old_state_object: dict[str, Any],
    state_mode: int,
    date: str,
    path: pathlib.Path,
) -> dict[str, Any]:
    state = copy.deepcopy(old_state_object)
    state["date"] = date
    phase = _identifier(state.get("current_phase"), "state current_phase")
    session_id = str(uuid.uuid4())
    epoch_id = str(uuid.uuid4())
    checkpoint_uuid = str(uuid.uuid4())
    header = _header(date, session_id)
    state["agent_journal"] = {
        "schema_version": SCHEMA_VERSION,
        "filename": path.name,
        "session_id": session_id,
        "epoch_id": epoch_id,
        "epoch_phase": phase,
        "epoch_status": "open",
        "accepted_sequence": 0,
        "accepted_length": len(header),
        "accepted_head_sha256": None,
        "checkpoint_uuid": checkpoint_uuid,
    }
    request = _validate_request({
        "event_type": "run-initialized",
        "session_id": session_id,
        "phase": phase,
        "epoch_id": epoch_id,
        "agent_role": "orchestrator",
        "action_code": "initialize",
        "status_code": "started",
        "reason_code": "user-request",
    }, EVENT_TYPES)
    document = JournalDocument(
        path, date, session_id, [], len(header), len(header), []
    )
    timestamp = _now_utc()
    event = _build_event_at(request, document, timestamp)
    rendered = _render_event(event)
    full_journal = header + rendered
    staged = _document_after_event(document, event, rendered)
    try:
        intent_request = _canonical_intent_request(
            state, state["agent_journal"]
        )
    except JournalError:
        intent_request = None
    if intent_request is not None:
        intent = _build_event_at(intent_request, staged, timestamp)
        full_journal += _render_event(intent)
    new_state = _json_bytes(state)
    return {
        "schema_version": INIT_TRANSACTION_VERSION,
        "operation": "initialize",
        "date": date,
        "session_id": session_id,
        "epoch_id": epoch_id,
        "checkpoint_uuid": checkpoint_uuid,
        "journal_filename": path.name,
        "state_mode": state_mode,
        "journal_mode": 0o600,
        "state": {
            "old_base64": _base64(old_state),
            "old_sha256": _digest_bytes(old_state),
            "new_base64": _base64(new_state),
            "new_sha256": _digest_bytes(new_state),
        },
        "journal": {
            "full_base64": _base64(full_journal),
            "full_sha256": _digest_bytes(full_journal),
        },
    }


def _preflight_init_transaction(
    workspace: pathlib.Path,
    transaction: dict[str, Any],
    *,
    allow_missing_output: bool = False,
) -> dict[str, Any]:
    if (
        _path_exists_no_follow(workspace / TRANSACTION_NAME)
        or _path_exists_no_follow(workspace / RESUME_TRANSACTION_NAME)
    ):
        raise JournalError(
            "initialization transaction cannot coexist with another transaction"
        )
    output = workspace / "output"
    output_exists = _path_exists_no_follow(output)
    if output_exists:
        _require_real_directory(output, "initialization output directory")
    elif not allow_missing_output:
        raise JournalError("initialization output directory is unavailable")
    state_path = workspace / "state.json"
    state_bytes, _ = _read_regular_bytes(
        state_path,
        "initialization state",
        expected_mode=transaction["state_mode"],
    )
    if state_bytes == transaction["old_state"]:
        state_stage = "old"
    elif state_bytes == transaction["new_state"]:
        state_stage = "new"
    else:
        raise JournalError("initialization state differs from exact old/new bytes")

    manifest_path = output / "session-manifest.json"
    if output_exists and _path_exists_no_follow(manifest_path):
        try:
            manifest_bytes, _ = _read_regular_bytes(
                manifest_path, "initialization manifest"
            )
            manifest = _decode_json_object_bytes(
                manifest_bytes, "initialization manifest"
            )
            new_state = _decode_json_object_bytes(
                transaction["new_state"], "initialization state.new"
            )
            if manifest:
                if manifest.get("date") != new_state.get("date"):
                    raise JournalError("manifest date differs from state")
                _validated_phases(manifest)
            _accepted_fact_events(new_state, manifest, [])
        except JournalError as exc:
            raise JournalError(
                f"initialization manifest preflight failed: {exc}"
            ) from exc

    journal_file = output / transaction["journal_filename"]
    if not output_exists or not _path_exists_no_follow(journal_file):
        journal_bytes = b""
    else:
        journal_bytes, _ = _read_regular_bytes(
            journal_file,
            "initialization journal",
            expected_mode=transaction["journal_mode"],
        )
    if not transaction["full_journal"].startswith(journal_bytes):
        raise JournalError("initialization journal is not an exact prefix")
    return {
        "state_path": state_path,
        "state_stage": state_stage,
        "journal_path": journal_file,
        "journal_prefix_length": len(journal_bytes),
        "journal_missing": transaction["full_journal"][len(journal_bytes):],
    }


def _ensure_init_output_directory(workspace: pathlib.Path) -> None:
    output = workspace / "output"
    if not _path_exists_no_follow(output):
        try:
            output.mkdir()
        except OSError as exc:
            raise JournalError(
                f"initialization output directory cannot be created: {exc}"
            ) from exc
        _fsync_directory(workspace)
    _require_real_directory(output, "initialization output directory")


def _persist_init_journal_suffix(
    path: pathlib.Path,
    missing: bytes,
    prefix_length: int,
    mode: int,
    configured_failpoint: str | None,
) -> None:
    if not missing:
        return
    created = prefix_length == 0 and not _path_exists_no_follow(path)
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    if created:
        flags |= os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, mode)
    except OSError as exc:
        raise JournalError(f"initialization journal cannot be opened: {exc}") from exc
    try:
        if created:
            os.fchmod(descriptor, mode)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != mode
            or info.st_size != prefix_length
            or info.st_nlink != 1
        ):
            raise JournalError("initialization journal changed before suffix write")
        persisted = missing
        if configured_failpoint == "during-init-journal-write":
            persisted = missing[:max(1, len(missing) // 2)]
        _write_all(descriptor, persisted)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if created:
        _fsync_directory(path.parent)
    if configured_failpoint == "during-init-journal-write":
        raise JournalError(
            "injected initialization failure: during-init-journal-write"
        )


def _validate_init_result(
    workspace: pathlib.Path, transaction: dict[str, Any]
) -> JournalDocument:
    state_bytes, _ = _read_regular_bytes(
        workspace / "state.json",
        "initialized state",
        expected_mode=transaction["state_mode"],
    )
    if state_bytes != transaction["new_state"]:
        raise JournalError("initialized state differs from initialization transaction")
    path = workspace / "output" / transaction["journal_filename"]
    journal_bytes, _ = _read_regular_bytes(
        path,
        "initialized journal",
        expected_mode=transaction["journal_mode"],
    )
    if journal_bytes != transaction["full_journal"]:
        raise JournalError("initialized journal differs from initialization transaction")
    state = _decode_json_object_bytes(transaction["new_state"], "initialization state.new")
    document = parse_journal(path)
    _validate_resume_context(workspace, state, document)
    if (
        document.events != transaction["initial_events"]
    ):
        raise JournalError("initialized journal events differ from initialization intent")
    return document


def _remove_init_transaction(workspace: pathlib.Path) -> None:
    try:
        (workspace / INIT_TRANSACTION_NAME).unlink()
    except OSError as exc:
        raise JournalError(
            f"initialization transaction could not be cleared: {exc}"
        ) from exc
    _fsync_directory(workspace)


def _apply_init_transaction_locked(
    workspace: pathlib.Path,
    transaction: dict[str, Any],
    configured_failpoint: str | None = None,
) -> None:
    persisted = _load_init_transaction(workspace)
    if persisted is None or persisted != transaction:
        raise JournalError("initialization transaction changed before recovery")
    preflight = _preflight_init_transaction(workspace, transaction)
    _raise_init_failpoint(configured_failpoint, "after-init-intent")
    _persist_init_journal_suffix(
        preflight["journal_path"],
        preflight["journal_missing"],
        preflight["journal_prefix_length"],
        transaction["journal_mode"],
        configured_failpoint,
    )
    _raise_init_failpoint(configured_failpoint, "after-init-journal-fsync")
    if preflight["state_stage"] == "old":
        _write_bytes_atomic(
            preflight["state_path"],
            transaction["new_state"],
            transaction["state_mode"],
        )
    _raise_init_failpoint(configured_failpoint, "after-init-state-replace")
    persisted = _load_init_transaction(workspace)
    if persisted is None or persisted != transaction:
        raise JournalError("initialization transaction changed before validation")
    _validate_init_result(workspace, transaction)
    _remove_init_transaction(workspace)


def _fence_pending_init_transaction(workspace: pathlib.Path) -> None:
    if _path_exists_no_follow(workspace / INIT_TRANSACTION_NAME):
        raise JournalError(
            "pending initialization transaction requires init recovery"
        )


def _resume_failpoint() -> str | None:
    configured = os.environ.get("NIGHTFALCON_JOURNAL_RESUME_FAILPOINT")
    if configured is not None and configured not in RESUME_FAILPOINTS:
        raise JournalError("unknown resume failpoint")
    return configured


def _raise_resume_failpoint(configured: str | None, name: str) -> None:
    if configured == name:
        raise JournalError(f"injected resume failure: {name}")


def _build_resume_transaction(
    workspace: pathlib.Path,
    old_state: dict[str, Any],
    new_state: dict[str, Any],
    document: JournalDocument,
    *,
    include_resume: bool,
    include_recovery: bool,
) -> dict[str, Any]:
    state_path = workspace / "state.json"
    old_state_bytes, state_mode = _read_regular_bytes(
        state_path, "resume state"
    )
    if _decode_json_object_bytes(old_state_bytes, "resume state.old") != old_state:
        raise JournalError("resume state changed before transaction creation")
    journal_path = document.path
    old_journal_bytes, journal_mode = _read_regular_bytes(
        journal_path, "resume journal"
    )
    if len(old_journal_bytes) != document.length:
        raise JournalError("resume journal changed before transaction creation")

    target_journal = _journal_state(new_state)
    timestamp = _now_utc()
    suffix = b""
    staged = document
    if include_recovery:
        old_journal = _journal_state(old_state)
        recovery_requests = (
            {
                "event_type": "recovery-started",
                "session_id": old_journal["session_id"],
                "phase": old_journal["epoch_phase"],
                "epoch_id": old_journal["epoch_id"],
                "agent_role": "recovery-worker",
                "action_code": "start-recovery",
                "status_code": "started",
                "reason_code": "resume-reconciliation",
            },
            {
                "event_type": "phase-acceptance-aborted",
                "session_id": old_journal["session_id"],
                "phase": old_journal["epoch_phase"],
                "epoch_id": old_journal["epoch_id"],
                "agent_role": "orchestrator",
                "action_code": "abort-acceptance",
                "status_code": "aborted",
                "reason_code": "resume-reconciliation",
                "checkpoint_uuid": old_journal["checkpoint_uuid"],
            },
            {
                "event_type": "recovery-completed",
                "session_id": old_journal["session_id"],
                "phase": old_journal["epoch_phase"],
                "epoch_id": old_journal["epoch_id"],
                "agent_role": "recovery-worker",
                "action_code": "complete-recovery",
                "status_code": "recovered",
                "reason_code": "resume-reconciliation",
            },
        )
        for request in recovery_requests:
            event = _build_event_at(request, staged, timestamp)
            rendered = _render_event(event)
            suffix += rendered
            staged = _document_after_event(staged, event, rendered)
    if include_resume:
        resumed = _build_event_at({
            "event_type": "run-resumed",
            "session_id": target_journal["session_id"],
            "phase": new_state["current_phase"],
            "epoch_id": target_journal["epoch_id"],
            "agent_role": "orchestrator",
            "action_code": "resume",
            "status_code": "recovered",
            "reason_code": "resume-reconciliation",
        }, staged, timestamp)
        rendered = _render_event(resumed)
        suffix += rendered
        staged = _document_after_event(staged, resumed, rendered)
    intent = _build_event_at(
        _canonical_intent_request(new_state, target_journal), staged, timestamp
    )
    suffix += _render_event(intent)
    new_state_bytes = _json_bytes(new_state)
    full_journal = old_journal_bytes + suffix
    return {
        "schema_version": RESUME_TRANSACTION_VERSION,
        "operation": "resume",
        "state_mode": state_mode,
        "journal_mode": journal_mode,
        "journal_filename": journal_path.name,
        "state": {
            "old_base64": _base64(old_state_bytes),
            "old_sha256": _digest_bytes(old_state_bytes),
            "new_base64": _base64(new_state_bytes),
            "new_sha256": _digest_bytes(new_state_bytes),
        },
        "journal": {
            "old_length": len(old_journal_bytes),
            "old_sha256": _digest_bytes(old_journal_bytes),
            "old_sequence": len(document.events),
            "old_head_sha256": (
                document.events[-1]["event_sha256"] if document.events else None
            ),
            "suffix_base64": _base64(suffix),
            "suffix_sha256": _digest_bytes(suffix),
            "new_length": len(full_journal),
            "new_sha256": _digest_bytes(full_journal),
        },
    }


def _decode_resume_transaction(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version", "operation", "state_mode", "journal_mode",
        "journal_filename", "state", "journal",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise JournalError("resume transaction has an invalid closed schema")
    if value.get("schema_version") != RESUME_TRANSACTION_VERSION:
        raise JournalError("resume transaction version is invalid")
    if value.get("operation") != "resume":
        raise JournalError("resume transaction operation is invalid")
    state_mode = value.get("state_mode")
    journal_mode = value.get("journal_mode")
    for mode, field in ((state_mode, "state_mode"), (journal_mode, "journal_mode")):
        if (
            isinstance(mode, bool)
            or not isinstance(mode, int)
            or mode < 0
            or mode > 0o7777
        ):
            raise JournalError(f"resume transaction {field} is invalid")
    if journal_mode != 0o600:
        raise JournalError("resume transaction journal mode must be 0600")

    state_record = value.get("state")
    state_fields = {"old_base64", "old_sha256", "new_base64", "new_sha256"}
    if not isinstance(state_record, dict) or set(state_record) != state_fields:
        raise JournalError("resume transaction state has an invalid schema")
    old_state = _decode_base64(state_record["old_base64"], "resume state.old_base64")
    new_state = _decode_base64(state_record["new_base64"], "resume state.new_base64")
    if (
        _digest_bytes(old_state) != _sha256(state_record["old_sha256"], "resume state.old_sha256")
        or _digest_bytes(new_state) != _sha256(state_record["new_sha256"], "resume state.new_sha256")
    ):
        raise JournalError("resume transaction state bytes differ from digest")
    old_state_object = _decode_json_object_bytes(old_state, "resume state.old")
    new_state_object = _decode_json_object_bytes(new_state, "resume state.new")
    if new_state != _json_bytes(new_state_object):
        raise JournalError("resume transaction new state is not canonical")
    old_journal_state = _journal_state(old_state_object)
    new_journal_state = _journal_state(new_state_object)
    if (
        old_state_object.get("current_phase") != new_state_object.get("current_phase")
        or old_journal_state["session_id"] != new_journal_state["session_id"]
        or old_journal_state["filename"] != new_journal_state["filename"]
    ):
        raise JournalError("resume transaction changes run identity")
    old_projection = copy.deepcopy(old_state_object)
    new_projection = copy.deepcopy(new_state_object)
    old_projection.pop("agent_journal", None)
    new_projection.pop("agent_journal", None)
    if old_projection != new_projection:
        raise JournalError("resume transaction changes non-journal state")
    unchanged_journal_fields = {
        "schema_version", "filename", "session_id", "accepted_sequence",
        "accepted_length", "accepted_head_sha256",
    }
    if any(
        old_journal_state.get(field) != new_journal_state.get(field)
        for field in unchanged_journal_fields
    ):
        raise JournalError("resume transaction changes accepted journal authority")
    rotated = old_state != new_state
    if rotated:
        if (
            new_journal_state["epoch_id"] == old_journal_state["epoch_id"]
            or new_journal_state["epoch_phase"] != new_state_object["current_phase"]
            or new_journal_state["epoch_status"] != "open"
            or new_journal_state["checkpoint_uuid"] == old_journal_state["checkpoint_uuid"]
        ):
            raise JournalError("resume transaction epoch rotation is invalid")
    elif new_journal_state != old_journal_state:
        raise JournalError("resume repair changes journal state")

    filename = value.get("journal_filename")
    if (
        not isinstance(filename, str)
        or not JOURNAL_NAME_RE.fullmatch(filename)
        or filename != new_journal_state["filename"]
    ):
        raise JournalError("resume transaction journal path is invalid")
    journal_record = value.get("journal")
    journal_fields = {
        "old_length", "old_sha256", "old_sequence", "old_head_sha256",
        "suffix_base64", "suffix_sha256", "new_length", "new_sha256",
    }
    if not isinstance(journal_record, dict) or set(journal_record) != journal_fields:
        raise JournalError("resume transaction journal has an invalid schema")
    old_length = journal_record.get("old_length")
    old_sequence = journal_record.get("old_sequence")
    new_length = journal_record.get("new_length")
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item < 0
        for item in (old_length, old_sequence, new_length)
    ):
        raise JournalError("resume transaction journal bounds are invalid")
    old_sha256 = _sha256(journal_record.get("old_sha256"), "resume journal.old_sha256")
    old_head = journal_record.get("old_head_sha256")
    if old_head is not None:
        old_head = _sha256(old_head, "resume journal.old_head_sha256")
    suffix = _decode_base64(journal_record.get("suffix_base64"), "resume journal.suffix_base64")
    if _digest_bytes(suffix) != _sha256(
        journal_record.get("suffix_sha256"), "resume journal.suffix_sha256"
    ):
        raise JournalError("resume transaction suffix differs from digest")
    if new_length != old_length + len(suffix):
        raise JournalError("resume transaction journal length is inconsistent")
    new_sha256 = _sha256(journal_record.get("new_sha256"), "resume journal.new_sha256")
    events, _head, _next_sequence = _decode_transaction_events(
        suffix,
        "resume suffix",
        new_journal_state["session_id"],
        old_head,
        old_sequence + 1,
    )
    event_types = [event["event_type"] for event in events]
    recovery_types = [
        "recovery-started",
        "phase-acceptance-aborted",
        "recovery-completed",
        "run-resumed",
        "user-intent-recorded",
    ]
    expected_types = (
        (["run-resumed", "user-intent-recorded"], recovery_types)
        if rotated
        else ((["run-resumed", "user-intent-recorded"],)
              if len(events) == 2
              else (["user-intent-recorded"],))
    )
    if event_types not in expected_types:
        raise JournalError("resume transaction suffix has invalid event types")
    recovery_count = 3 if event_types == recovery_types else 0
    if any(
        event["phase"] != old_journal_state["epoch_phase"]
        or event["epoch_id"] != old_journal_state["epoch_id"]
        for event in events[:recovery_count]
    ) or any(
        event["phase"] != new_journal_state["epoch_phase"]
        or event["epoch_id"] != new_journal_state["epoch_id"]
        for event in events[recovery_count:]
    ):
        raise JournalError("resume transaction suffix has stale epoch identity")
    if recovery_count and (
        events[0].get("agent_role") != "recovery-worker"
        or events[0].get("action_code") != "start-recovery"
        or events[0].get("status_code") != "started"
        or events[0].get("reason_code") != "resume-reconciliation"
        or events[1].get("agent_role") != "orchestrator"
        or events[1].get("action_code") != "abort-acceptance"
        or events[1].get("status_code") != "aborted"
        or events[1].get("reason_code") != "resume-reconciliation"
        or events[1].get("checkpoint_uuid") != old_journal_state["checkpoint_uuid"]
        or events[2].get("agent_role") != "recovery-worker"
        or events[2].get("action_code") != "complete-recovery"
        or events[2].get("status_code") != "recovered"
        or events[2].get("reason_code") != "resume-reconciliation"
    ):
        raise JournalError("resume transaction recovery sequence is invalid")
    resumed_index = recovery_count
    if len(events) - recovery_count == 2 and (
        events[resumed_index].get("agent_role") != "orchestrator"
        or events[resumed_index].get("action_code") != "resume"
        or events[resumed_index].get("status_code") != "recovered"
        or events[resumed_index].get("reason_code") != "resume-reconciliation"
    ):
        raise JournalError("resume transaction run-resumed event is invalid")
    if _request_projection(events[-1]) != _canonical_intent_request(
        new_state_object, new_journal_state
    ):
        raise JournalError("resume transaction user intent differs from new state")
    if len({event["timestamp_utc"] for event in events}) != 1:
        raise JournalError("resume transaction event timestamps differ")
    return {
        "state_mode": state_mode,
        "journal_mode": journal_mode,
        "journal_filename": filename,
        "old_state": old_state,
        "new_state": new_state,
        "old_journal_length": old_length,
        "old_journal_sha256": old_sha256,
        "suffix": suffix,
        "new_journal_length": new_length,
        "new_journal_sha256": new_sha256,
    }


def _load_resume_transaction(workspace: pathlib.Path) -> dict[str, Any] | None:
    path = workspace / RESUME_TRANSACTION_NAME
    if not _path_exists_no_follow(path):
        return None
    raw, _ = _read_regular_bytes(
        path, "resume transaction", expected_mode=0o600
    )
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_json_object_pairs)
    except (UnicodeError, json.JSONDecodeError, JournalError) as exc:
        raise JournalError(f"resume transaction is invalid JSON: {exc}") from exc
    if not isinstance(value, dict) or _json_bytes(value) != raw:
        raise JournalError("resume transaction JSON is not canonical")
    return _decode_resume_transaction(value)


def _preflight_resume_transaction(
    workspace: pathlib.Path, transaction: dict[str, Any]
) -> dict[str, Any]:
    if _path_exists_no_follow(workspace / TRANSACTION_NAME) or _path_exists_no_follow(
        workspace / INIT_TRANSACTION_NAME
    ):
        raise JournalError("resume transaction cannot coexist with another transaction")
    state_stage = _classify_exact_file(
        workspace / "state.json",
        transaction["old_state"],
        transaction["new_state"],
        "resume state",
    )
    journal_path = workspace / "output" / transaction["journal_filename"]
    raw, mode = _read_regular_bytes(
        journal_path, "resume journal", expected_mode=transaction["journal_mode"]
    )
    old_length = transaction["old_journal_length"]
    if (
        len(raw) < old_length
        or _digest_bytes(raw[:old_length]) != transaction["old_journal_sha256"]
    ):
        raise JournalError("resume journal differs from exact old prefix")
    observed_suffix = raw[old_length:]
    expected_suffix = transaction["suffix"]
    if _digest_bytes(raw[:old_length] + expected_suffix) != transaction[
        "new_journal_sha256"
    ]:
        raise JournalError("resume journal target digest is inconsistent")
    if (
        len(observed_suffix) > len(expected_suffix)
        or expected_suffix[:len(observed_suffix)] != observed_suffix
    ):
        raise JournalError("resume journal differs from exact transaction suffix")
    if mode != transaction["journal_mode"]:
        raise JournalError("resume journal mode changed")
    return {
        "state_stage": state_stage,
        "journal_path": journal_path,
        "journal_prefix_length": len(raw),
        "journal_missing": expected_suffix[len(observed_suffix):],
    }


def _persist_resume_journal_suffix(
    path: pathlib.Path,
    missing: bytes,
    prefix_length: int,
    mode: int,
    configured_failpoint: str | None,
) -> None:
    if not missing:
        return
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != mode
            or info.st_size != prefix_length
            or info.st_nlink != 1
        ):
            raise JournalError("resume journal changed before suffix write")
        persisted = missing
        if configured_failpoint == "during-resume-journal-write":
            persisted = missing[:max(1, len(missing) // 2)]
        _write_all(descriptor, persisted)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if configured_failpoint == "during-resume-journal-write":
        raise JournalError("injected resume failure: during-resume-journal-write")


def _remove_resume_transaction(workspace: pathlib.Path) -> None:
    try:
        (workspace / RESUME_TRANSACTION_NAME).unlink()
    except OSError as exc:
        raise JournalError(f"resume transaction could not be cleared: {exc}") from exc
    _fsync_directory(workspace)


def _apply_resume_transaction_locked(
    workspace: pathlib.Path,
    transaction: dict[str, Any],
    configured_failpoint: str | None = None,
) -> JournalDocument:
    persisted = _load_resume_transaction(workspace)
    if persisted is None or persisted != transaction:
        raise JournalError("resume transaction changed before recovery")
    preflight = _preflight_resume_transaction(workspace, transaction)
    _raise_resume_failpoint(configured_failpoint, "after-resume-intent")
    _persist_resume_journal_suffix(
        preflight["journal_path"],
        preflight["journal_missing"],
        preflight["journal_prefix_length"],
        transaction["journal_mode"],
        configured_failpoint,
    )
    _raise_resume_failpoint(configured_failpoint, "after-resume-journal-fsync")
    if preflight["state_stage"] == "old":
        _write_bytes_atomic(
            workspace / "state.json",
            transaction["new_state"],
            transaction["state_mode"],
        )
    _raise_resume_failpoint(configured_failpoint, "after-resume-state-replace")
    raw, _ = _read_regular_bytes(
        preflight["journal_path"],
        "resumed journal",
        expected_mode=transaction["journal_mode"],
    )
    if (
        len(raw) != transaction["new_journal_length"]
        or _digest_bytes(raw) != transaction["new_journal_sha256"]
    ):
        raise JournalError("resumed journal differs from resume transaction")
    state_bytes, _ = _read_regular_bytes(
        workspace / "state.json",
        "resumed state",
        expected_mode=transaction["state_mode"],
    )
    if state_bytes != transaction["new_state"]:
        raise JournalError("resumed state differs from resume transaction")
    state = _decode_json_object_bytes(transaction["new_state"], "resume state.new")
    document = parse_journal(preflight["journal_path"])
    _validate_resume_context(workspace, state, document)
    _validate_resume_epoch_intent(document, state, _journal_state(state))
    _remove_resume_transaction(workspace)
    return document


def _commit_resume_transaction_locked(
    workspace: pathlib.Path,
    old_state: dict[str, Any],
    new_state: dict[str, Any],
    document: JournalDocument,
    *,
    include_resume: bool,
    include_recovery: bool = False,
) -> JournalDocument:
    transaction_value = _build_resume_transaction(
        workspace, old_state, new_state, document,
        include_resume=include_resume,
        include_recovery=include_recovery,
    )
    transaction = _decode_resume_transaction(transaction_value)
    configured_failpoint = _resume_failpoint()
    _raise_resume_failpoint(configured_failpoint, "before-resume-transaction")
    _write_json_atomic(workspace / RESUME_TRANSACTION_NAME, transaction_value)
    return _apply_resume_transaction_locked(
        workspace, transaction, configured_failpoint
    )


def _append_event(path: pathlib.Path, document: JournalDocument, request: dict[str, object]) -> JournalDocument:
    event = _build_event(request, document)
    _validate_event(event)
    _append_fsync(path, _render_event(event))
    return parse_journal(path)


def _validate_resume_context(workspace: pathlib.Path, state: dict[str, Any], document: JournalDocument) -> dict[str, Any]:
    journal = _journal_state(state)
    if state.get("date") != document.date:
        raise JournalError("journal date differs from state")
    if document.session_id != journal.get("session_id"):
        raise JournalError("journal session differs from state")
    _identifier(state.get("current_phase"), "state current_phase")
    _identifier(journal.get("epoch_id"), "journal epoch_id")
    _identifier(journal.get("epoch_phase"), "journal epoch_phase")
    if state.get("current_phase") == "done":
        if journal.get("epoch_status") not in {"open", "closed"}:
            raise JournalError("completed journal epoch status is invalid")
    elif journal.get("epoch_status") != "open":
        raise JournalError("journal epoch is not open")
    _validate_accepted_prefix(document, journal)
    checkpoints = state.get("git_checkpoints", [])
    if not isinstance(checkpoints, list):
        raise JournalError("state git_checkpoints must be a list")
    manifest_path = workspace / "output" / "session-manifest.json"
    manifest = _load_json_object(manifest_path) if manifest_path.exists() else {}
    if manifest:
        if manifest.get("date") != state.get("date"):
            raise JournalError("manifest date differs from state")
        _validated_phases(manifest)
    _accepted_fact_events(
        state, manifest, document.events[:journal["accepted_sequence"]]
    )
    return journal


def _open_agent_attempts(
    document: JournalDocument, journal: dict[str, Any]
) -> list[dict[str, str]]:
    attempts: dict[str, dict[str, str]] = {}
    terminal: set[str] = set()
    for event in document.events:
        if event.get("epoch_id") != journal["epoch_id"] or event.get("phase") != journal["epoch_phase"]:
            continue
        agent_id = event.get("agent_id")
        if not isinstance(agent_id, str):
            continue
        if event["event_type"] in {"agent-spawn-requested", "agent-started"}:
            attempt = attempts.setdefault(agent_id, {"agent_id": agent_id})
            for field in ("agent_role", "parent_agent_id"):
                value = event.get(field)
                if isinstance(value, str):
                    attempt[field] = value
        elif event["event_type"] in {"agent-completed", "agent-failed"}:
            terminal.add(agent_id)
    return [attempts[agent_id] for agent_id in sorted(attempts) if agent_id not in terminal]


def initialize(workspace: pathlib.Path, date: str, port: str | None = None) -> dict[str, object]:
    if port is not None:
        _identifier(port, "port")
    configured_failpoint = _init_failpoint()
    state_path = workspace / "state.json"
    with workspace_lock(workspace):
        transaction_paths = (
            workspace / INIT_TRANSACTION_NAME,
            workspace / RESUME_TRANSACTION_NAME,
            workspace / TRANSACTION_NAME,
        )
        if sum(_path_exists_no_follow(path) for path in transaction_paths) > 1:
            raise JournalError(
                "init cannot continue while multiple journal transactions coexist"
            )
        pending_init = _load_init_transaction(workspace)
        if pending_init is not None:
            if pending_init["date"] != date:
                raise JournalError(
                    "initialization transaction date differs from requested date"
                )
            _preflight_init_transaction(
                workspace, pending_init, allow_missing_output=True
            )
            _ensure_init_output_directory(workspace)
            _apply_init_transaction_locked(
                workspace, pending_init, configured_failpoint
            )
            return {
                "filename": pending_init["journal_filename"],
                "session_id": pending_init["session_id"],
            }

        pending_resume = _load_resume_transaction(workspace)
        if pending_resume is not None:
            resumed_state = _decode_json_object_bytes(
                pending_resume["new_state"], "resume state.new"
            )
            if resumed_state.get("date") != date:
                raise JournalError(
                    "resume transaction date differs from requested date"
                )
            _preflight_resume_transaction(workspace, pending_resume)
            resumed_journal = _journal_state(resumed_state)
            return {
                "filename": pending_resume["journal_filename"],
                "session_id": resumed_journal["session_id"],
            }

        old_state, state_mode = _read_regular_bytes(
            state_path, "initialization state"
        )
        state = _decode_json_object_bytes(old_state, "initialization state.old")
        if state.get("date") not in (None, date):
            raise JournalError("date differs from authoritative state date")
        if not isinstance(state.get("current_phase"), str):
            raise JournalError("state current_phase must be present")
        if "agent_journal" in state and state.get("date") is None:
            raise JournalError("initialized state date cannot be null")
        output = workspace / "output"
        if _path_exists_no_follow(output):
            _require_real_directory(output, "initialization output directory")
        path = journal_path(workspace, date)
        if "agent_journal" not in state:
            if _path_exists_no_follow(workspace / TRANSACTION_NAME):
                raise JournalError(
                    "initialization cannot begin with an acceptance transaction"
                )
            if _path_exists_no_follow(path):
                raise JournalError(
                    "journal exists without initialization transaction or authoritative state extension"
                )
            transaction_value = _build_init_transaction(
                old_state, state, state_mode, date, path
            )
            transaction = _decode_init_transaction(transaction_value)
            _preflight_init_transaction(
                workspace, transaction, allow_missing_output=True
            )
            _write_json_atomic(
                workspace / INIT_TRANSACTION_NAME, transaction_value
            )
            persisted = _load_init_transaction(workspace)
            if persisted is None or persisted != transaction:
                raise JournalError(
                    "initialization transaction was not persisted exactly"
                )
            _ensure_init_output_directory(workspace)
            _apply_init_transaction_locked(
                workspace, persisted, configured_failpoint
            )
            return {
                "filename": transaction["journal_filename"],
                "session_id": transaction["session_id"],
            }
        else:
            _require_real_directory(output, "initialization output directory")
            journal = _journal_state(state)
            transaction = _load_transaction(workspace)
            if transaction is None:
                document = parse_journal(_current_journal_path(workspace, state))
                _validate_resume_context(workspace, state, document)
            else:
                if journal.get("session_id") != transaction["session_id"]:
                    raise JournalError("pending transaction session differs from workspace")
                _preflight_transaction(workspace, transaction)
        return {"filename": path.name, "session_id": state["agent_journal"]["session_id"]}


def _acceptance_barrier() -> None:
    raw = os.environ.get("NIGHTFALCON_JOURNAL_TEST_BARRIER")
    if raw is None:
        return
    barrier = pathlib.Path(raw)
    ready = pathlib.Path(raw + ".ready")
    ready.write_text("ready\n", encoding="utf-8")
    while not barrier.exists():
        time.sleep(0.01)


def _validate_phase_artifacts(
    workspace: pathlib.Path,
    phase: str,
    *,
    skip_candidates: bool = False,
    no_debate_needed: bool = False,
    no_validation_needed: bool = False,
    no_poc_needed: bool = False,
) -> None:
    command = [
        "bash",
        str(pathlib.Path(__file__).resolve().parent / "validate-phase.sh"),
        "--phase", phase,
        "--workspace", str(workspace),
    ]
    for enabled, flag in (
        (skip_candidates, "--skip-candidates"),
        (no_debate_needed, "--no-debate-needed"),
        (no_validation_needed, "--no-validation-needed"),
        (no_poc_needed, "--no-poc-needed"),
    ):
        if enabled:
            command.append(flag)
    result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise JournalError(
            f"phase artifact validation failed: {detail or 'unknown error'}"
        )


def _failpoint(name: str) -> None:
    configured = os.environ.get("NIGHTFALCON_JOURNAL_FAILPOINT")
    if configured is not None and configured not in ACCEPTANCE_FAILPOINTS:
        raise JournalError("unknown acceptance failpoint")
    if configured == name:
        raise JournalError(f"injected acceptance failure: {name}")


def _require_open_epoch(state: dict[str, Any], phase: str) -> dict[str, Any]:
    phase = _identifier(phase, "phase")
    journal = _journal_state(state)
    phase_status = state.get("phase_status")
    if isinstance(phase_status, dict) and phase_status.get(phase) == "completed":
        raise JournalError(f"phase {phase!r} is already completed and cannot be replayed")
    if (state.get("current_phase") != phase or journal.get("epoch_phase") != phase):
        raise JournalError("phase acceptance does not match the current epoch")
    if journal.get("epoch_status") != "open":
        raise JournalError("journal epoch is not open")
    _identifier(journal.get("epoch_id"), "journal epoch_id")
    _identifier(journal.get("checkpoint_uuid"), "journal checkpoint_uuid")
    return journal


def _validate_acceptance_readiness(
    state: dict[str, Any], document: JournalDocument
) -> None:
    journal = _journal_state(state)
    expected = _canonical_intent_request(state, journal)
    intents = [
        event for event in document.events
        if event["event_type"] == "user-intent-recorded"
        and event["session_id"] == journal["session_id"]
        and event["phase"] == journal["epoch_phase"]
        and event["epoch_id"] == journal["epoch_id"]
    ]
    if len(intents) != 1:
        raise JournalError(
            "phase acceptance requires one canonical current-epoch user intent"
        )
    observed = {
        field: intents[0][field]
        for field in ALLOWED_REQUEST_FIELDS
        if field in intents[0]
    }
    if observed != expected:
        raise JournalError(
            "phase acceptance current-epoch user intent is not canonical"
        )
    if _open_agent_attempts(document, journal):
        raise JournalError("phase acceptance requires an empty open-agent ledger")


def _phase_bindings(
    workspace: pathlib.Path,
    state: dict[str, Any],
    phase: str,
    checkpoint_uuid: str,
    journal_filename: str,
) -> list[dict[str, str]]:
    """Bind only canonical artifacts in the validated phase inventory."""
    bindings: list[dict[str, str]] = []
    _identifier(phase, "binding phase")
    date = state.get("date")
    if not isinstance(date, str) or not DATE_RE.fullmatch(date):
        raise JournalError("binding inventory requires the authoritative date")
    raw_slugs = state.get("repo_slugs")
    if not isinstance(raw_slugs, list):
        raise JournalError("binding inventory requires repository slugs")
    slugs = sorted({_identifier(slug, "binding repo_slug") for slug in raw_slugs})
    candidates: set[str] = {
        f"input/repos-{date}.txt",
        f"findings/cross-repository-topology-{date}.json",
        f"output/run-log-{date}.md",
        f"output/executive-report-{date}.json",
        f"output/executive-report-{date}.html",
        f"output/executive-summary-{date}.md",
        "output/proof_of_concept/run-all.sh",
    }
    names = (
        "candidates", "dataflow", "dependency-inventory",
        "organization-context", "owasp-context",
        "debate", "findings", "receipt", "pattern-tags",
    )
    extensions = {
        "candidates": ("md",),
        "dataflow": ("md", "json"),
        "dependency-inventory": ("json",),
        "organization-context": ("md",),
        "owasp-context": ("md",),
        "debate": ("md",),
        "findings": ("md", "json", "sarif"),
        "receipt": ("json",),
        "pattern-tags": ("json",),
    }
    for slug in slugs:
        for name in names:
            for extension in extensions[name]:
                candidates.add(f"findings/{slug}/{name}-{date}.{extension}")
        poc_directory = workspace / "output" / "proof_of_concept" / slug
        if poc_directory.exists():
            for path in poc_directory.rglob("*"):
                try:
                    info = path.lstat()
                except OSError as exc:
                    raise JournalError(
                        f"bound artifact is unavailable: {exc}"
                    ) from exc
                if stat.S_ISREG(info.st_mode):
                    candidates.add(path.relative_to(workspace).as_posix())
    candidates.discard("output/session-manifest.json")
    candidates.discard(f"output/{journal_filename}")
    for relative in sorted(candidates):
        path = workspace / relative
        if not path.exists():
            continue
        try:
            info = path.lstat()
            value = path.read_bytes()
        except OSError as exc:
            raise JournalError(f"bound artifact is unavailable: {exc}") from exc
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
        ):
            raise JournalError(
                "bound artifact must be a regular non-symlink, non-hardlinked file"
            )
        bindings.append({
            "path": safe_relative_path(relative),
            "sha256": _digest_bytes(value),
            "checkpoint_uuid": checkpoint_uuid,
        })
    return _validate_binding_inventory(
        sorted(bindings, key=canonical_json)
    )


def _promote_selector_bindings(
    workspace: pathlib.Path,
    state: dict[str, Any],
    document: JournalDocument,
    bindings: list[dict[str, str]],
) -> list[dict[str, str]]:
    journal = _journal_state(state)
    base_by_path = {
        binding["path"]: binding
        for binding in bindings
        if "selector" not in binding
    }
    slugs = {
        _identifier(slug, "selector repo_slug")
        for slug in state.get("repo_slugs", [])
    }
    promoted: dict[bytes, dict[str, str]] = {
        canonical_json(binding): binding for binding in bindings
    }
    for event in document.events:
        if (
            event.get("epoch_id") != journal["epoch_id"]
            or event.get("phase") != journal["epoch_phase"]
        ):
            continue
        for ref in _validate_artifact_refs(event.get("artifact_refs", [])):
            selector = ref.get("selector")
            if selector is None:
                continue
            path = ref["path"]
            parts = pathlib.PurePosixPath(path).parts
            if len(parts) < 3 or parts[0] != "findings" or parts[1] not in slugs:
                raise JournalError(
                    "finding selector reference lacks repository/path scope"
                )
            base = base_by_path.get(path)
            if base is None:
                raise JournalError(
                    "selector reference path is outside the validated phase inventory"
                )
            _select_artifact((workspace / path).read_bytes(), selector)
            binding = {**base, "selector": selector}
            promoted[canonical_json(binding)] = binding
    return _validate_binding_inventory(
        sorted(promoted.values(), key=canonical_json)
    )


def _build_event_at(
    request: dict[str, object], document: JournalDocument, timestamp: str
) -> dict[str, object]:
    if not isinstance(timestamp, str) or not TIMESTAMP_RE.fullmatch(timestamp):
        raise JournalError("timestamp must use UTC seconds")
    event: dict[str, object] = dict(request)
    event["sequence"] = len(document.events) + 1
    event["event_id"] = str(uuid.uuid4())
    event["timestamp_utc"] = timestamp
    event["previous_event_sha256"] = (
        document.events[-1]["event_sha256"] if document.events else None
    )
    event["event_sha256"] = event_digest(event)
    _validate_event(event)
    return event


def _document_after_event(
    document: JournalDocument, event: dict[str, object], block: bytes
) -> JournalDocument:
    return JournalDocument(
        document.path,
        document.date,
        document.session_id,
        document.events + [event],
        document.header_length,
        document.length + len(block),
        document.event_end_offsets + [document.length + len(block)],
    )


def _advance_phase(state: dict[str, Any], phase: str) -> str:
    mode = state.get("mode", "review")
    if mode not in JOURNAL_MODES:
        raise JournalError("state mode is invalid")
    order = TRIAGE_ORDER if mode == "triage" else REVIEW_ORDER
    if phase not in order:
        raise JournalError("phase is not valid for the run mode")
    index = order.index(phase)
    return order[index + 1] if index + 1 < len(order) else "done"


def _transition_documents(
    state: dict[str, Any],
    manifest: dict[str, Any],
    phase: str,
    timestamp: str,
    document: JournalDocument,
    bindings: list[dict[str, str]],
) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes, bytes]:
    journal = _require_open_epoch(state, phase)
    checkpoint_uuid = journal["checkpoint_uuid"]
    common: dict[str, object] = {
        "session_id": journal["session_id"],
        "phase": phase,
        "epoch_id": journal["epoch_id"],
        "agent_role": "orchestrator",
        "reason_code": "acceptance-gate",
        "checkpoint_uuid": checkpoint_uuid,
    }
    prepared_document = document
    prepared_prefix = b""
    for offset in range(0, len(bindings), MAX_ARTIFACT_REFS):
        chunk = bindings[offset:offset + MAX_ARTIFACT_REFS]
        bound_event = _build_event_at({
            **common,
            "event_type": "artifact-bound",
            "action_code": "bind-artifact",
            "status_code": "accepted",
            "artifact_refs": chunk,
        }, prepared_document, timestamp)
        bound_bytes = _render_event(bound_event)
        prepared_prefix += bound_bytes
        prepared_document = _document_after_event(
            prepared_document, bound_event, bound_bytes
        )
    prepared_event = _build_event_at({
        **common,
        "event_type": "phase-acceptance-prepared",
        "action_code": "prepare-acceptance",
        "status_code": "pending",
        "artifact_refs": [],
    }, prepared_document, timestamp)
    prepared_event_bytes = _render_event(prepared_event)
    prepared_bytes = prepared_prefix + prepared_event_bytes
    prepared_document = _document_after_event(
        prepared_document, prepared_event, prepared_event_bytes
    )
    accepted_event = _build_event_at({
        **common,
        "event_type": "phase-accepted",
        "action_code": "accept-phase",
        "status_code": "accepted",
        "artifact_refs": [],
    }, prepared_document, timestamp)
    accepted_bytes = _render_event(accepted_event)
    accepted_document = _document_after_event(
        prepared_document, accepted_event, accepted_bytes
    )

    new_state = copy.deepcopy(state)
    phase_status = new_state.setdefault("phase_status", {})
    history = new_state.setdefault("history", [])
    checkpoints = new_state.setdefault("git_checkpoints", [])
    if not isinstance(phase_status, dict):
        raise JournalError("state phase_status must be an object")
    if not isinstance(history, list):
        raise JournalError("state history must be a list")
    if not isinstance(checkpoints, list):
        raise JournalError("state git_checkpoints must be a list")
    next_phase = _advance_phase(new_state, phase)
    phase_status[phase] = "completed"
    history.append({"phase": phase, "completed_at": timestamp})
    checkpoints.append({
        "phase": phase,
        "checkpoint_uuid": checkpoint_uuid,
        "timestamp": timestamp,
        CHECKPOINT_TRANSITION_VERSION_FIELD: TRANSACTION_VERSION,
    })
    new_state["current_phase"] = next_phase
    new_journal = _journal_state(new_state)
    new_manifest = copy.deepcopy(manifest)
    phases = new_manifest.setdefault("phases_completed", [])
    manifest_checkpoints = new_manifest.setdefault("checkpoints", [])
    if not isinstance(phases, list):
        raise JournalError("manifest phases_completed must be a list")
    if not isinstance(manifest_checkpoints, list):
        raise JournalError("manifest checkpoints must be a list")
    if phase not in phases:
        phases.append(phase)
    manifest_checkpoints.append({
        "phase": phase,
        "checkpoint_uuid": checkpoint_uuid,
        "timestamp": timestamp,
        CHECKPOINT_TRANSITION_VERSION_FIELD: TRANSACTION_VERSION,
    })
    if next_phase == "done" and not new_manifest.get("ended_at"):
        new_manifest["ended_at"] = timestamp

    if next_phase == "done":
        new_journal["epoch_status"] = "closed"
    else:
        new_journal["epoch_id"] = str(uuid.uuid4())
        new_journal["epoch_phase"] = next_phase
        new_journal["epoch_status"] = "open"
        new_journal["checkpoint_uuid"] = str(uuid.uuid4())

    transition_bytes = accepted_bytes
    final_document = accepted_document
    if next_phase != "done":
        intent_event = _build_event_at(
            _canonical_intent_request(new_state, new_journal),
            final_document,
            timestamp,
        )
        intent_bytes = _render_event(intent_event)
        final_document = _document_after_event(
            final_document, intent_event, intent_bytes
        )
        transition_bytes += intent_bytes
    else:
        finalized_event = _build_event_at({
            "event_type": "run-finalized",
            "session_id": journal["session_id"],
            "phase": phase,
            "epoch_id": journal["epoch_id"],
            "agent_role": "finalizer",
            "action_code": "finalize-run",
            "status_code": "finalized",
            "reason_code": "phase-contract",
            "checkpoint_uuid": checkpoint_uuid,
        }, final_document, timestamp)
        finalized_bytes = _render_event(finalized_event)
        final_document = _document_after_event(
            final_document, finalized_event, finalized_bytes
        )
        handoff_event = _build_event_at({
            "event_type": "next-run-handoff",
            "session_id": journal["session_id"],
            "phase": phase,
            "epoch_id": journal["epoch_id"],
            "agent_role": "finalizer",
            "action_code": "create-handoff",
            "status_code": "completed",
            "reason_code": "phase-contract",
            "checkpoint_uuid": checkpoint_uuid,
            "handoff": _handoff_data(
                new_state, new_manifest, final_document.events
            ),
        }, final_document, timestamp)
        handoff_bytes = _render_event(handoff_event)
        final_document = _document_after_event(
            final_document, handoff_event, handoff_bytes
        )
        transition_bytes += finalized_bytes + handoff_bytes

    new_journal["accepted_sequence"] = len(final_document.events)
    new_journal["accepted_length"] = final_document.length
    new_journal["accepted_head_sha256"] = final_document.events[-1]["event_sha256"]

    recovery_bytes = b""
    if next_phase == "done":
        return (
            new_state, new_manifest, prepared_bytes, transition_bytes,
            recovery_bytes,
        )

    recovery_started = _build_event_at({
        "event_type": "recovery-started",
        "session_id": journal["session_id"],
        "phase": next_phase,
        "epoch_id": new_journal["epoch_id"],
        "agent_role": "recovery-worker",
        "action_code": "start-recovery",
        "status_code": "started",
        "reason_code": "resume-reconciliation",
    }, final_document, timestamp)
    recovery_started_bytes = _render_event(recovery_started)
    recovery_document = _document_after_event(
        final_document, recovery_started, recovery_started_bytes
    )
    recovery_completed = _build_event_at({
        "event_type": "recovery-completed",
        "session_id": journal["session_id"],
        "phase": next_phase,
        "epoch_id": new_journal["epoch_id"],
        "agent_role": "recovery-worker",
        "action_code": "complete-recovery",
        "status_code": "recovered",
        "reason_code": "resume-reconciliation",
    }, recovery_document, timestamp)
    recovery_bytes = recovery_started_bytes + _render_event(recovery_completed)
    return (
        new_state, new_manifest, prepared_bytes, transition_bytes,
        recovery_bytes,
    )


def _transition_record(old: bytes, new: bytes) -> dict[str, str]:
    return {
        "old_base64": _base64(old),
        "old_sha256": _digest_bytes(old),
        "new_base64": _base64(new),
        "new_sha256": _digest_bytes(new),
    }


def _event_record(value: bytes) -> dict[str, str]:
    return {"base64": _base64(value), "sha256": _digest_bytes(value)}


def _build_transaction(
    state: dict[str, Any],
    manifest: dict[str, Any],
    new_state: dict[str, Any],
    new_manifest: dict[str, Any],
    document: JournalDocument,
    phase: str,
    timestamp: str,
    bindings: list[dict[str, str]],
    prepared_bytes: bytes,
    accepted_bytes: bytes,
    recovery_bytes: bytes,
) -> dict[str, Any]:
    old_state_bytes = (document.path.parents[1] / "state.json").read_bytes()
    manifest_path = document.path.parent / "session-manifest.json"
    old_manifest_bytes = manifest_path.read_bytes()
    old_journal_bytes = _checked_journal_bytes(document.path)
    final_journal_bytes = old_journal_bytes + prepared_bytes + accepted_bytes
    recovery_journal_bytes = final_journal_bytes + recovery_bytes
    journal = _journal_state(state)
    final_head = _journal_state(new_state)["accepted_head_sha256"]
    return {
        "schema_version": TRANSACTION_VERSION,
        "session_id": journal["session_id"],
        "phase": phase,
        "timestamp": timestamp,
        "checkpoint_uuid": journal["checkpoint_uuid"],
        "bindings": bindings,
        "state": _transition_record(old_state_bytes, _json_bytes(new_state)),
        "manifest": _transition_record(old_manifest_bytes, _json_bytes(new_manifest)),
        "journal": {
            "old_length": len(old_journal_bytes),
            "old_head_sha256": document.events[-1]["event_sha256"] if document.events else None,
            "old_sha256": _digest_bytes(old_journal_bytes),
            "prepared": _event_record(prepared_bytes),
            "accepted": _event_record(accepted_bytes),
            "final_length": len(final_journal_bytes),
            "final_head_sha256": final_head,
            "final_sha256": _digest_bytes(final_journal_bytes),
            "recovery": _event_record(recovery_bytes),
            "recovery_final_length": len(recovery_journal_bytes),
            "recovery_final_sha256": _digest_bytes(recovery_journal_bytes),
        },
    }


def _decode_transition(value: Any, field: str) -> tuple[bytes, bytes]:
    required = {"old_base64", "old_sha256", "new_base64", "new_sha256"}
    if not isinstance(value, dict) or set(value) != required:
        raise JournalError(f"transaction {field} transition has an invalid schema")
    old = _decode_base64(value["old_base64"], f"{field}.old_base64")
    new = _decode_base64(value["new_base64"], f"{field}.new_base64")
    if (_digest_bytes(old) != _sha256(value["old_sha256"], f"{field}.old_sha256")
            or _digest_bytes(new) != _sha256(value["new_sha256"], f"{field}.new_sha256")):
        raise JournalError(f"transaction {field} bytes do not match their SHA-256")
    _decode_json_object_bytes(old, f"{field}.old")
    _decode_json_object_bytes(new, f"{field}.new")
    return old, new


def _decode_event_record(value: Any, field: str) -> bytes:
    if not isinstance(value, dict) or set(value) != {"base64", "sha256"}:
        raise JournalError(f"transaction {field} event has an invalid schema")
    decoded = _decode_base64(value["base64"], f"{field}.base64")
    if _digest_bytes(decoded) != _sha256(value["sha256"], f"{field}.sha256"):
        raise JournalError(f"transaction {field} bytes do not match their SHA-256")
    return decoded


def _checkpoint_transition_version(
    entries: Any, phase: str, checkpoint_uuid: str, field: str
) -> str | None:
    if not isinstance(entries, list):
        raise JournalError(f"{field} checkpoints must be a list")
    matches = [
        entry for entry in entries
        if isinstance(entry, dict)
        and entry.get("phase") == phase
        and entry.get("checkpoint_uuid") == checkpoint_uuid
    ]
    if len(matches) != 1:
        raise JournalError(f"{field} does not contain one matching checkpoint")
    version = matches[0].get(CHECKPOINT_TRANSITION_VERSION_FIELD)
    if version is None:
        return None
    if version != TRANSACTION_VERSION:
        raise JournalError(f"{field} checkpoint transition version is invalid")
    return version


def _decode_transaction_events(
    raw: bytes,
    field: str,
    session_id: str,
    previous_head: str | None,
    expected_sequence: int | None,
) -> tuple[list[dict[str, Any]], str | None, int | None]:
    cursor = 0
    events: list[dict[str, Any]] = []
    sequence = expected_sequence
    previous = previous_head
    while cursor < len(raw):
        has_handoff_heading = raw.startswith(HANDOFF_HEADING, cursor)
        if has_handoff_heading:
            cursor += len(HANDOFF_HEADING)
        if not raw.startswith(b"```json\n", cursor):
            raise JournalError(f"transaction {field} contains noncanonical bytes")
        end = raw.find(b"\n```\n", cursor + 8)
        if end < 0:
            raise JournalError(f"transaction {field} event fence is incomplete")
        payload = raw[cursor + 8:end]
        if not payload or b"\n" in payload:
            raise JournalError(f"transaction {field} event JSON is not one line")
        try:
            event = json.loads(
                payload.decode("ascii"), object_pairs_hook=_json_object_pairs
            )
        except (UnicodeDecodeError, json.JSONDecodeError, JournalError) as exc:
            raise JournalError(f"transaction {field} event is invalid: {exc}") from exc
        if canonical_json(event) != payload:
            raise JournalError(f"transaction {field} event is not canonical")
        event = _validate_event(event)
        if has_handoff_heading != (event["event_type"] == "next-run-handoff"):
            raise JournalError(f"transaction {field} handoff heading is invalid")
        if sequence is None:
            sequence = event["sequence"]
        if event["sequence"] != sequence or event["previous_event_sha256"] != previous:
            raise JournalError(f"transaction {field} hash chain is not contiguous")
        if event["session_id"] != session_id:
            raise JournalError(f"transaction {field} session is inconsistent")
        events.append(event)
        previous = event["event_sha256"]
        sequence += 1
        cursor = end + len(b"\n```\n")
    return events, previous, sequence


def _request_projection(event: dict[str, Any]) -> dict[str, object]:
    return {
        field: event[field]
        for field in ALLOWED_REQUEST_FIELDS
        if field in event
    }


def _validate_v2_transaction_semantics(
    *,
    session_id: str,
    phase: str,
    checkpoint_uuid: str,
    timestamp: str,
    bindings: list[dict[str, str]],
    old_state: dict[str, Any],
    new_state: dict[str, Any],
    new_manifest: dict[str, Any],
    old_head: str | None,
    prepared: bytes,
    accepted: bytes,
    recovery: bytes,
    final_head: str,
) -> None:
    old_journal = _journal_state(old_state)
    new_journal = _journal_state(new_state)
    state_version = _checkpoint_transition_version(
        new_state.get("git_checkpoints"), phase, checkpoint_uuid, "state"
    )
    manifest_version = _checkpoint_transition_version(
        new_manifest.get("checkpoints"), phase, checkpoint_uuid, "manifest"
    )
    if state_version != TRANSACTION_VERSION or manifest_version != TRANSACTION_VERSION:
        raise JournalError("v2 transaction checkpoint marker is missing")
    next_phase = _advance_phase(old_state, phase)
    if new_state.get("current_phase") != next_phase:
        raise JournalError("v2 transaction phase rotation is inconsistent")
    if next_phase == "done":
        if (
            new_journal.get("epoch_phase") != phase
            or new_journal.get("epoch_id") != old_journal.get("epoch_id")
            or new_journal.get("epoch_status") != "closed"
            or new_journal.get("checkpoint_uuid") != checkpoint_uuid
        ):
            raise JournalError("v2 terminal transaction epoch closure is inconsistent")
    elif (
        new_journal.get("epoch_phase") != next_phase
        or new_journal.get("epoch_id") == old_journal.get("epoch_id")
        or new_journal.get("epoch_status") != "open"
        or new_journal.get("checkpoint_uuid") == checkpoint_uuid
    ):
        raise JournalError("v2 transaction phase rotation is inconsistent")

    prepared_events, head, next_sequence = _decode_transaction_events(
        prepared, "journal.prepared", session_id, old_head, None
    )
    if (
        not prepared_events
        or prepared_events[0]["sequence"] <= old_journal["accepted_sequence"]
    ):
        raise JournalError("v2 transaction prepared sequence is invalid")
    accepted_events, accepted_head, recovery_sequence = _decode_transaction_events(
        accepted, "journal.accepted", session_id, head, next_sequence
    )
    if not accepted_events or accepted_head != final_head:
        raise JournalError("v2 transaction accepted suffix is invalid")
    combined = prepared_events + accepted_events
    accepted_index = len(prepared_events)
    if _canonical_phase_acceptance(combined, accepted_index) != (
        phase, checkpoint_uuid
    ):
        raise JournalError("v2 transaction phase acceptance is not canonical")
    if [event["event_type"] for event in prepared_events] != (
        ["artifact-bound"] * (len(prepared_events) - 1)
        + ["phase-acceptance-prepared"]
    ):
        raise JournalError("v2 prepared suffix has noncanonical event types")
    bound_refs = [
        ref
        for event in prepared_events[:-1]
        for ref in _validate_artifact_refs(event.get("artifact_refs"))
    ]
    if _validate_binding_inventory(bound_refs) != bindings:
        raise JournalError("v2 prepared suffix differs from transaction bindings")
    old_epoch = old_journal.get("epoch_id")
    if any(
        event.get("phase") != phase
        or event.get("epoch_id") != old_epoch
        or event.get("checkpoint_uuid") != checkpoint_uuid
        or event.get("timestamp_utc") != timestamp
        for event in prepared_events + accepted_events[:1]
    ):
        raise JournalError("v2 acceptance identity is inconsistent")

    if next_phase == "done":
        if [event["event_type"] for event in accepted_events] != [
            "phase-accepted", "run-finalized", "next-run-handoff"
        ]:
            raise JournalError("v2 terminal transaction suffix is invalid")
        terminal_expectations = (
            ("finalizer", "finalize-run", "finalized", "phase-contract"),
            ("finalizer", "create-handoff", "completed", "phase-contract"),
        )
        if any(
            event.get("agent_role") != role
            or event.get("action_code") != action
            or event.get("status_code") != status
            or event.get("reason_code") != reason
            or event.get("phase") != phase
            or event.get("epoch_id") != old_epoch
            or event.get("checkpoint_uuid") != checkpoint_uuid
            or event.get("timestamp_utc") != timestamp
            for event, (role, action, status, reason) in zip(
                accepted_events[1:], terminal_expectations, strict=True
            )
        ):
            raise JournalError("v2 terminal transaction identity is inconsistent")
        _validate_handoff(accepted_events[-1].get("handoff"))
        if recovery:
            raise JournalError("v2 terminal transaction must not append recovery events")
    else:
        if [event["event_type"] for event in accepted_events] != [
            "phase-accepted", "user-intent-recorded"
        ]:
            raise JournalError("v2 transition must seed one next-epoch intent")
        seed = accepted_events[1]
        if _request_projection(seed) != _canonical_intent_request(
            new_state, new_journal
        ) or seed.get("timestamp_utc") != timestamp:
            raise JournalError("v2 next-epoch intent differs from new state")
        recovery_events, _recovery_head, _ = _decode_transaction_events(
            recovery,
            "journal.recovery",
            session_id,
            accepted_head,
            recovery_sequence,
        )
        recovery_expectations = (
            ("recovery-started", "recovery-worker", "start-recovery", "started"),
            ("recovery-completed", "recovery-worker", "complete-recovery", "recovered"),
        )
        if len(recovery_events) != len(recovery_expectations) or any(
            event.get("event_type") != event_type
            or event.get("agent_role") != role
            or event.get("action_code") != action
            or event.get("status_code") != status
            or event.get("phase") != next_phase
            or event.get("epoch_id") != new_journal["epoch_id"]
            or event.get("reason_code") != "resume-reconciliation"
            or event.get("timestamp_utc") != timestamp
            for event, (event_type, role, action, status) in zip(
                recovery_events, recovery_expectations, strict=True
            )
        ):
            raise JournalError("v2 recovery suffix is inconsistent")
    if new_journal.get("accepted_sequence") != accepted_events[-1]["sequence"]:
        raise JournalError("v2 accepted sequence does not cover transition suffix")


def _decode_transaction(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version", "session_id", "phase", "timestamp", "checkpoint_uuid",
        "bindings", "state", "manifest", "journal",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise JournalError("acceptance transaction has an invalid closed schema")
    version = value.get("schema_version")
    if version not in {LEGACY_TRANSACTION_VERSION, TRANSACTION_VERSION}:
        raise JournalError("acceptance transaction version is invalid")
    session_id = _identifier(value.get("session_id"), "transaction session_id")
    phase = _identifier(value.get("phase"), "transaction phase")
    checkpoint_uuid = _identifier(
        value.get("checkpoint_uuid"), "transaction checkpoint_uuid"
    )
    timestamp = value.get("timestamp")
    if not isinstance(timestamp, str) or not TIMESTAMP_RE.fullmatch(timestamp):
        raise JournalError("transaction timestamp must use UTC seconds")
    bindings = _validate_binding_inventory(value.get("bindings"))
    for binding in bindings:
        if (binding.get("checkpoint_uuid") != checkpoint_uuid
                or "sha256" not in binding):
            raise JournalError("transaction binding is not checkpoint-bound")
    old_state, new_state = _decode_transition(value.get("state"), "state")
    old_manifest, new_manifest = _decode_transition(value.get("manifest"), "manifest")
    journal = value.get("journal")
    journal_fields = {
        "old_length", "old_head_sha256", "old_sha256", "prepared", "accepted",
        "final_length", "final_head_sha256", "final_sha256", "recovery",
        "recovery_final_length", "recovery_final_sha256",
    }
    if not isinstance(journal, dict) or set(journal) != journal_fields:
        raise JournalError("transaction journal transition has an invalid schema")
    for name in ("old_length", "final_length", "recovery_final_length"):
        if (isinstance(journal[name], bool) or not isinstance(journal[name], int)
                or journal[name] < 0):
            raise JournalError(f"transaction journal {name} is invalid")
    if journal["old_head_sha256"] is not None:
        _sha256(journal["old_head_sha256"], "transaction old journal head")
    _sha256(journal["old_sha256"], "transaction old journal SHA-256")
    _sha256(journal["final_head_sha256"], "transaction final journal head")
    _sha256(journal["final_sha256"], "transaction final journal SHA-256")
    _sha256(
        journal["recovery_final_sha256"],
        "transaction recovery journal SHA-256",
    )
    prepared = _decode_event_record(journal["prepared"], "journal.prepared")
    accepted = _decode_event_record(journal["accepted"], "journal.accepted")
    recovery = _decode_event_record(journal["recovery"], "journal.recovery")
    if journal["final_length"] != journal["old_length"] + len(prepared) + len(accepted):
        raise JournalError("transaction journal lengths are inconsistent")
    if journal["recovery_final_length"] != journal["final_length"] + len(recovery):
        raise JournalError("transaction recovery journal lengths are inconsistent")
    old_state_object = _decode_json_object_bytes(old_state, "state.old")
    new_state_object = _decode_json_object_bytes(new_state, "state.new")
    new_manifest_object = _decode_json_object_bytes(new_manifest, "manifest.new")
    old_journal = _journal_state(old_state_object)
    new_journal = _journal_state(new_state_object)
    if (old_journal.get("session_id") != session_id
            or old_journal.get("checkpoint_uuid") != checkpoint_uuid
            or old_state_object.get("current_phase") != phase):
        raise JournalError("transaction old state identity is inconsistent")
    if (new_journal.get("session_id") != session_id
            or new_journal.get("accepted_length") != journal["final_length"]
            or new_journal.get("accepted_head_sha256") != journal["final_head_sha256"]):
        raise JournalError("transaction new state journal metadata is inconsistent")
    if not any(
        isinstance(entry, dict)
        and entry.get("phase") == phase
        and entry.get("checkpoint_uuid") == checkpoint_uuid
        for entry in new_state_object.get("git_checkpoints", [])
    ):
        raise JournalError("transaction new state lacks its checkpoint UUID")
    if not any(
        isinstance(entry, dict)
        and entry.get("phase") == phase
        and entry.get("checkpoint_uuid") == checkpoint_uuid
        for entry in new_manifest_object.get("checkpoints", [])
    ):
        raise JournalError("transaction new manifest lacks its checkpoint UUID")
    if version == TRANSACTION_VERSION:
        _validate_v2_transaction_semantics(
            session_id=session_id,
            phase=phase,
            checkpoint_uuid=checkpoint_uuid,
            timestamp=timestamp,
            bindings=bindings,
            old_state=old_state_object,
            new_state=new_state_object,
            new_manifest=new_manifest_object,
            old_head=journal["old_head_sha256"],
            prepared=prepared,
            accepted=accepted,
            recovery=recovery,
            final_head=journal["final_head_sha256"],
        )
    return {
        "transaction_version": version,
        "session_id": session_id,
        "phase": phase,
        "timestamp": timestamp,
        "checkpoint_uuid": checkpoint_uuid,
        "bindings": bindings,
        "old_state": old_state,
        "new_state": new_state,
        "old_manifest": old_manifest,
        "new_manifest": new_manifest,
        "old_journal_length": journal["old_length"],
        "old_journal_head": journal["old_head_sha256"],
        "old_journal_sha256": journal["old_sha256"],
        "prepared_bytes": prepared,
        "accepted_bytes": accepted,
        "recovery_bytes": recovery,
        "final_journal_length": journal["final_length"],
        "final_journal_head": journal["final_head_sha256"],
        "final_journal_sha256": journal["final_sha256"],
        "recovery_journal_length": journal["recovery_final_length"],
        "recovery_journal_sha256": journal["recovery_final_sha256"],
    }


def _load_transaction(workspace: pathlib.Path) -> dict[str, Any] | None:
    path = workspace / TRANSACTION_NAME
    if not path.exists():
        return None
    return _decode_transaction(_load_json_object(path))


def _validate_bound_artifacts(workspace: pathlib.Path, transaction: dict[str, Any]) -> None:
    for binding in transaction["bindings"]:
        path = workspace / binding["path"]
        try:
            info = path.lstat()
            value = path.read_bytes()
        except OSError as exc:
            raise JournalError(f"bound artifact changed or disappeared: {exc}") from exc
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or _digest_bytes(value) != binding["sha256"]):
            raise JournalError(f"bound artifact changed: {binding['path']}")


def _classify_journal(
    workspace: pathlib.Path, transaction: dict[str, Any]
) -> tuple[pathlib.Path, bytes, bytes, bool]:
    old_state = _decode_json_object_bytes(transaction["old_state"], "state.old")
    path = _current_journal_path(workspace, old_state)
    raw = _checked_journal_bytes(path)
    old_length = transaction["old_journal_length"]
    transition = transaction["prepared_bytes"] + transaction["accepted_bytes"]
    recovery = transaction["recovery_bytes"]
    exact_suffix = transition + recovery
    if (len(raw) < old_length
            or _digest_bytes(raw[:old_length]) != transaction["old_journal_sha256"]):
        raise JournalError("journal differs from the exact acceptance transaction")
    suffix = raw[old_length:]
    if len(suffix) > len(exact_suffix) or exact_suffix[:len(suffix)] != suffix:
        raise JournalError("journal differs from the exact acceptance transaction")
    transition_length = min(len(suffix), len(transition))
    return (
        path,
        transition[transition_length:],
        recovery[max(0, len(suffix) - len(transition)):],
        len(suffix) > len(transition),
    )


def _classify_exact_file(
    path: pathlib.Path, old: bytes, new: bytes, field: str
) -> str:
    try:
        current, _mode = _read_regular_bytes(path, f"transaction {field}")
    except JournalError as exc:
        raise JournalError(f"transaction {field} is unavailable: {exc}") from exc
    if current == old:
        return "old"
    if current == new:
        return "new"
    raise JournalError(f"transaction {field} was mutated")


def _preflight_transaction(
    workspace: pathlib.Path, transaction: dict[str, Any]
) -> dict[str, Any]:
    _validate_bound_artifacts(workspace, transaction)
    (
        journal_path,
        journal_missing,
        recovery_missing,
        recovery_started,
    ) = _classify_journal(workspace, transaction)
    state_stage = _classify_exact_file(
        workspace / "state.json",
        transaction["old_state"], transaction["new_state"], "state",
    )
    manifest_stage = _classify_exact_file(
        workspace / "output" / "session-manifest.json",
        transaction["old_manifest"], transaction["new_manifest"], "manifest",
    )
    return {
        "journal_path": journal_path,
        "journal_missing": journal_missing,
        "recovery_missing": recovery_missing,
        "recovery_started": recovery_started,
        "state_stage": state_stage,
        "manifest_stage": manifest_stage,
    }


def _repair_journal(
    path: pathlib.Path, missing: bytes, transaction: dict[str, Any]
) -> JournalDocument:
    if missing:
        _append_fsync(path, missing)
    document = parse_journal(path)
    if (document.length != transaction["final_journal_length"]
            or not document.events
            or document.events[-1]["event_sha256"] != transaction["final_journal_head"]):
        raise JournalError("repaired journal does not match acceptance transaction")
    return document


def _repair_recovery_journal(
    path: pathlib.Path, missing: bytes, transaction: dict[str, Any]
) -> JournalDocument:
    if missing:
        _append_fsync(path, missing)
    document = parse_journal(path)
    raw = _checked_journal_bytes(path)
    recovery_types = [event["event_type"] for event in document.events[-2:]]
    if not transaction["recovery_bytes"]:
        recovery_types = []
    if (
        document.length != transaction["recovery_journal_length"]
        or _digest_bytes(raw) != transaction["recovery_journal_sha256"]
        or recovery_types not in ([], ["recovery-started", "recovery-completed"])
    ):
        raise JournalError("repaired journal does not match recovery transaction")
    return document


def _git(workspace: pathlib.Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ("git", "-C", str(workspace), *arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise JournalError(f"git is unavailable: {exc}") from exc


def _git_required(workspace: pathlib.Path, *arguments: str) -> bytes:
    result = _git(workspace, *arguments)
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise JournalError(f"git checkpoint operation failed: {detail or 'unknown error'}")
    return result.stdout


def _checkpoint_ref(session_id: str, phase: str) -> str:
    return (
        "refs/nightfalcon/checkpoints/"
        f"{_identifier(session_id, 'checkpoint session_id')}/"
        f"{_identifier(phase, 'checkpoint phase')}"
    )


def _commit_has_checkpoint_trailer(message: bytes, checkpoint_uuid: str) -> bool:
    try:
        text = message.decode("utf-8")
    except UnicodeDecodeError:
        return False
    trailer = f"NightFalcon-Checkpoint-ID: {checkpoint_uuid}"
    return trailer in text.splitlines()


def _checkpoint_commits(workspace: pathlib.Path, checkpoint_uuid: str) -> list[str]:
    checkpoint_uuid = _identifier(checkpoint_uuid, "checkpoint_uuid")
    revisions = _git_required(workspace, "rev-list", "--all").decode("ascii").splitlines()
    matches: list[str] = []
    for revision in revisions:
        if not re.fullmatch(r"[0-9a-f]{40,64}", revision):
            raise JournalError("git returned an invalid commit identity")
        message = _git_required(workspace, "show", "-s", "--format=%B", revision)
        if _commit_has_checkpoint_trailer(message, checkpoint_uuid):
            matches.append(revision)
    return matches


def _git_blob(workspace: pathlib.Path, commit: str, path: str) -> bytes:
    path = safe_relative_path(path)
    result = _git(workspace, "show", f"{commit}:{path}")
    if result.returncode:
        raise JournalError(f"checkpoint tree does not contain {path}")
    return result.stdout


def _validate_checkpoint_commit(
    workspace: pathlib.Path, transaction: dict[str, Any], commit: str
) -> None:
    for binding in transaction["bindings"]:
        value = _git_blob(workspace, commit, binding["path"])
        if _digest_bytes(value) != binding["sha256"]:
            raise JournalError(f"checkpoint bound artifact hash differs: {binding['path']}")
    old_state = _decode_json_object_bytes(transaction["old_state"], "state.old")
    journal_path_name = _current_journal_path(workspace, old_state).relative_to(workspace).as_posix()
    expected = {
        "state.json": transaction["new_state"],
        "output/session-manifest.json": transaction["new_manifest"],
        journal_path_name: (
            _checked_journal_bytes(_current_journal_path(workspace, old_state))[
                :transaction["old_journal_length"]
            ]
            + transaction["prepared_bytes"]
            + transaction["accepted_bytes"]
        ),
    }
    for path, exact in expected.items():
        if _git_blob(workspace, commit, path) != exact:
            raise JournalError(f"checkpoint tracked metadata differs: {path}")


def _checkpoint_paths(workspace: pathlib.Path) -> list[str]:
    paths = {"state.json"}
    for directory_name in ("findings", "output", "input"):
        directory = workspace / directory_name
        if directory.exists():
            for path in directory.rglob("*"):
                with contextlib.suppress(OSError):
                    if stat.S_ISREG(path.lstat().st_mode):
                        paths.add(path.relative_to(workspace).as_posix())
        tracked = _git_required(workspace, "ls-files", "--", directory_name)
        for path in tracked.decode("utf-8").splitlines():
            paths.add(safe_relative_path(path))
    return sorted(paths)


def _create_checkpoint_commit(workspace: pathlib.Path, transaction: dict[str, Any]) -> str:
    if _git(workspace, "rev-parse", "--git-dir").returncode:
        raise JournalError("phase acceptance requires a Git workspace")
    paths = _checkpoint_paths(workspace)
    _git_required(workspace, "add", "-A", "--", *paths)
    message = (
        f"[security-review: {transaction['phase']}] complete\n\n"
        f"NightFalcon-Checkpoint-ID: {transaction['checkpoint_uuid']}"
    )
    _git_required(
        workspace, "commit", "-q", "--allow-empty", "--only", "-m", message,
        "--", *paths,
    )
    commit = _git_required(workspace, "rev-parse", "HEAD").decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise JournalError("git returned an invalid checkpoint commit")
    return commit


def _find_or_create_checkpoint(workspace: pathlib.Path, transaction: dict[str, Any]) -> str:
    matches = _checkpoint_commits(workspace, transaction["checkpoint_uuid"])
    if len(matches) > 1:
        raise JournalError("ambiguous checkpoint commits share the checkpoint UUID")
    if matches:
        commit = matches[0]
    else:
        commit = _create_checkpoint_commit(workspace, transaction)
        matches = _checkpoint_commits(workspace, transaction["checkpoint_uuid"])
        if len(matches) != 1 or matches[0] != commit:
            raise JournalError("checkpoint trailer was not preserved canonically")
    _validate_checkpoint_commit(workspace, transaction, commit)
    return commit


def _repair_checkpoint_ref(
    workspace: pathlib.Path, transaction: dict[str, Any], commit: str
) -> None:
    ref = _checkpoint_ref(transaction["session_id"], transaction["phase"])
    existing = _git(workspace, "rev-parse", "--verify", "--quiet", ref)
    if existing.returncode == 0:
        current = existing.stdout.decode("ascii").strip()
        if current != commit:
            raise JournalError("checkpoint ref points to a different commit")
        return
    if existing.returncode not in (1, 128):
        raise JournalError("checkpoint ref could not be inspected")
    _git_required(workspace, "update-ref", ref, commit)


def _validate_transaction_result(
    workspace: pathlib.Path,
    transaction: dict[str, Any],
    document: JournalDocument,
    commit: str,
) -> None:
    state_path = workspace / "state.json"
    manifest_path = workspace / "output" / "session-manifest.json"
    if state_path.read_bytes() != transaction["new_state"]:
        raise JournalError("accepted state differs from transaction")
    if manifest_path.read_bytes() != transaction["new_manifest"]:
        raise JournalError("accepted manifest differs from transaction")
    state = _decode_json_object_bytes(transaction["new_state"], "state.new")
    _validate_accepted_prefix(document, _journal_state(state))
    _validate_checkpoint_commit(workspace, transaction, commit)
    ref = _checkpoint_ref(transaction["session_id"], transaction["phase"])
    resolved = _git_required(workspace, "rev-parse", "--verify", ref).decode("ascii").strip()
    if resolved != commit:
        raise JournalError("checkpoint ref did not converge")


def _remove_transaction(workspace: pathlib.Path) -> None:
    try:
        (workspace / TRANSACTION_NAME).unlink()
    except OSError as exc:
        raise JournalError(f"acceptance transaction could not be cleared: {exc}") from exc
    directory = os.open(workspace, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _recover_pending_transactions_locked(
    workspace: pathlib.Path,
) -> dict[str, object] | None:
    """Converge one exact durable transaction while the workspace lock is held."""
    transaction_paths = (
        workspace / INIT_TRANSACTION_NAME,
        workspace / RESUME_TRANSACTION_NAME,
        workspace / TRANSACTION_NAME,
    )
    if sum(_path_exists_no_follow(path) for path in transaction_paths) > 1:
        raise JournalError("multiple journal transactions cannot coexist")
    init_transaction = _load_init_transaction(workspace)
    if init_transaction is not None:
        _apply_init_transaction_locked(workspace, init_transaction)
        return {
            "filename": init_transaction["journal_filename"],
            "session_id": init_transaction["session_id"],
        }
    resume_transaction = _load_resume_transaction(workspace)
    if resume_transaction is not None:
        document = _apply_resume_transaction_locked(
            workspace, resume_transaction
        )
        state = _load_json_object(workspace / "state.json")
        journal = _journal_state(state)
        return {
            "epoch_id": journal["epoch_id"],
            "sequence": len(document.events),
        }
    transaction = _load_transaction(workspace)
    if transaction is None:
        return None
    preflight = _preflight_transaction(workspace, transaction)
    if preflight["recovery_started"]:
        if preflight["journal_missing"]:
            raise JournalError(
                "recovery journal cannot precede the acceptance transition"
            )
        document = parse_journal(preflight["journal_path"])
        accepted_state = _decode_json_object_bytes(
            transaction["new_state"], "state.new"
        )
        _validate_accepted_prefix(document, _journal_state(accepted_state))
        if not _checkpoint_commits(workspace, transaction["checkpoint_uuid"]):
            raise JournalError(
                "recovery journal exists before its checkpoint commit"
            )
    else:
        document = _repair_journal(
            preflight["journal_path"], preflight["journal_missing"], transaction
        )
    if preflight["state_stage"] == "old":
        _write_bytes_atomic(workspace / "state.json", transaction["new_state"])
    if preflight["manifest_stage"] == "old":
        _write_bytes_atomic(
            workspace / "output" / "session-manifest.json",
            transaction["new_manifest"],
        )
    commit = _find_or_create_checkpoint(workspace, transaction)
    _repair_checkpoint_ref(workspace, transaction, commit)
    document = _repair_recovery_journal(
        preflight["journal_path"], preflight["recovery_missing"], transaction
    )
    _validate_transaction_result(workspace, transaction, document, commit)
    _remove_transaction(workspace)
    return {
        "checkpoint_uuid": transaction["checkpoint_uuid"],
        "commit": commit,
    }


def accept_phase(
    workspace: pathlib.Path,
    phase: str,
    timestamp: str,
    *,
    skip_candidates: bool = False,
    no_debate_needed: bool = False,
    no_validation_needed: bool = False,
    no_poc_needed: bool = False,
) -> dict[str, object]:
    with workspace_lock(workspace):
        _recover_pending_transactions_locked(workspace)
        _acceptance_barrier()
        state_path = workspace / "state.json"
        manifest_path = workspace / "output" / "session-manifest.json"
        state, manifest, _accepted, document = _load_authoritative_context_locked(
            workspace, recover=False
        )
        journal = _require_open_epoch(state, phase)
        _validate_acceptance_readiness(state, document)
        _validate_phase_artifacts(
            workspace,
            phase,
            skip_candidates=skip_candidates,
            no_debate_needed=no_debate_needed,
            no_validation_needed=no_validation_needed,
            no_poc_needed=no_poc_needed,
        )
        bindings = _phase_bindings(
            workspace, state, phase, journal["checkpoint_uuid"],
            journal["filename"],
        )
        bindings = _promote_selector_bindings(
            workspace, state, document, bindings
        )
        (
            new_state,
            new_manifest,
            prepared_bytes,
            accepted_bytes,
            recovery_bytes,
        ) = _transition_documents(state, manifest, phase, timestamp, document, bindings)
        transaction_value = _build_transaction(
            state, manifest, new_state, new_manifest, document, phase, timestamp,
            bindings, prepared_bytes, accepted_bytes, recovery_bytes,
        )
        transaction = _decode_transaction(transaction_value)
        _write_json_atomic(workspace / TRANSACTION_NAME, transaction_value)
        _failpoint("after-intent")
        _append_fsync(document.path, prepared_bytes)
        _failpoint("after-prepared-append")
        _write_bytes_atomic(state_path, transaction["new_state"])
        _write_bytes_atomic(manifest_path, transaction["new_manifest"])
        _failpoint("after-state-replace")
        _append_fsync(document.path, accepted_bytes)
        _failpoint("before-git-commit")
        commit = _find_or_create_checkpoint(workspace, transaction)
        _failpoint("after-git-commit-before-ref")
        _repair_checkpoint_ref(workspace, transaction, commit)
        final_document = parse_journal(document.path)
        _validate_transaction_result(workspace, transaction, final_document, commit)
        _remove_transaction(workspace)
        return {"checkpoint_uuid": transaction["checkpoint_uuid"], "commit": commit}


def _validate_checkpoint_alignment(workspace: pathlib.Path, state: dict[str, Any]) -> None:
    checkpoints = state.get("git_checkpoints", [])
    if not isinstance(checkpoints, list):
        raise JournalError("state git_checkpoints must be a list")
    manifest_path = workspace / "output" / "session-manifest.json"
    manifest = _load_json_object(manifest_path) if manifest_path.exists() else {}
    manifest_checkpoints = manifest.get("checkpoints", [])
    if not isinstance(manifest_checkpoints, list):
        raise JournalError("manifest checkpoints must be a list")
    session_id = _journal_state(state)["session_id"]
    for entry in checkpoints:
        if not isinstance(entry, dict):
            raise JournalError("state checkpoint entry must be an object")
        checkpoint_uuid = entry.get("checkpoint_uuid")
        if checkpoint_uuid is None:
            continue
        checkpoint_uuid = _identifier(checkpoint_uuid, "state checkpoint_uuid")
        phase = _identifier(entry.get("phase"), "state checkpoint phase")
        state_version = _checkpoint_transition_version(
            checkpoints, phase, checkpoint_uuid, "state"
        )
        manifest_version = _checkpoint_transition_version(
            manifest_checkpoints, phase, checkpoint_uuid, "manifest"
        )
        if state_version != manifest_version:
            raise JournalError("manifest checkpoint differs from state")
        matches = _checkpoint_commits(workspace, checkpoint_uuid)
        if len(matches) != 1:
            raise JournalError("checkpoint UUID does not identify exactly one commit")
        ref = _checkpoint_ref(session_id, phase)
        resolved = _git_required(workspace, "rev-parse", "--verify", ref).decode("ascii").strip()
        if resolved != matches[0]:
            raise JournalError("checkpoint ref differs from checkpoint UUID commit")


def _validate_checkpoint_authority(
    workspace: pathlib.Path,
    state: dict[str, Any],
    manifest: dict[str, Any],
    document: JournalDocument,
) -> None:
    """Authenticate every accepted checkpoint through ref, trailer, tree, and blobs."""
    journal = _journal_state(state)
    accepted_prefix = document.events[:journal["accepted_sequence"]]
    _facts, acceptance_events = _accepted_fact_selection(
        state, manifest, accepted_prefix
    )
    live_journal = _checked_journal_bytes(document.path)
    expected_keys: set[tuple[str, str]] = set()
    for accepted in acceptance_events:
        phase = accepted["phase"]
        checkpoint_uuid = accepted["checkpoint_uuid"]
        expected_keys.add((phase, checkpoint_uuid))
        matches = _checkpoint_commits(workspace, checkpoint_uuid)
        if len(matches) != 1:
            raise JournalError(
                "checkpoint UUID does not identify exactly one trailer commit"
            )
        commit = matches[0]
        ref = _checkpoint_ref(journal["session_id"], phase)
        resolved = _git(workspace, "rev-parse", "--verify", "--quiet", ref)
        if resolved.returncode:
            raise JournalError("checkpoint authoritative ref is missing")
        ref_commit = resolved.stdout.decode("ascii").strip()
        if ref_commit != commit:
            raise JournalError("checkpoint ref differs from unique trailer commit")

        committed_state_bytes = _git_blob(workspace, commit, "state.json")
        committed_manifest_bytes = _git_blob(
            workspace, commit, "output/session-manifest.json"
        )
        committed_state = _decode_json_object_bytes(
            committed_state_bytes, "checkpoint state"
        )
        committed_manifest = _decode_json_object_bytes(
            committed_manifest_bytes, "checkpoint manifest"
        )
        if (
            committed_state_bytes != _json_bytes(committed_state)
            or committed_manifest_bytes != _json_bytes(committed_manifest)
        ):
            raise JournalError("checkpoint committed metadata is not canonical")
        committed_journal = _journal_state(committed_state)
        if (
            committed_state.get("date") != state.get("date")
            or committed_manifest.get("date") != state.get("date")
            or committed_journal.get("session_id") != journal.get("session_id")
            or committed_journal.get("filename") != journal.get("filename")
        ):
            raise JournalError("checkpoint committed metadata identity differs")
        if (
            _authoritative_checkpoint_keys(
                committed_state.get("git_checkpoints", []),
                "checkpoint state",
            )
            != expected_keys
            or _authoritative_checkpoint_keys(
                committed_manifest.get("checkpoints", []),
                "checkpoint manifest",
            )
            != expected_keys
        ):
            raise JournalError("checkpoint committed checkpoint set differs")
        phase_status = committed_state.get("phase_status", {})
        if (
            not isinstance(phase_status, dict)
            or phase_status.get(phase) != "completed"
            or phase not in _validated_phases(committed_manifest)
        ):
            raise JournalError("checkpoint committed phase metadata differs")
        state_version = _checkpoint_transition_version(
            committed_state.get("git_checkpoints"),
            phase,
            checkpoint_uuid,
            "checkpoint state",
        )
        manifest_version = _checkpoint_transition_version(
            committed_manifest.get("checkpoints"),
            phase,
            checkpoint_uuid,
            "checkpoint manifest",
        )
        if state_version != manifest_version:
            raise JournalError("checkpoint transition version differs")
        expected_sequence = accepted["sequence"] + (
            2 if phase == "phase-8"
            else 1 if state_version == TRANSACTION_VERSION
            else 0
        )
        if (
            committed_journal.get("accepted_sequence") != expected_sequence
            or expected_sequence > len(document.events)
        ):
            raise JournalError("checkpoint committed journal sequence differs")
        if state_version == TRANSACTION_VERSION and phase != "phase-8":
            seed = document.events[accepted["sequence"]]
            if _request_projection(seed) != _canonical_intent_request(
                committed_state, committed_journal
            ):
                raise JournalError(
                    "checkpoint next-epoch intent differs from committed state"
                )
        expected_length = document.event_end_offsets[expected_sequence - 1]
        expected_head = document.events[expected_sequence - 1]["event_sha256"]
        if (
            committed_journal.get("accepted_length") != expected_length
            or committed_journal.get("accepted_head_sha256") != expected_head
        ):
            raise JournalError("checkpoint committed journal metadata differs")
        journal_relative = f"output/{journal['filename']}"
        committed_journal_bytes = _git_blob(
            workspace, commit, journal_relative
        )
        if (
            committed_journal_bytes != live_journal[:expected_length]
            or len(committed_journal_bytes) != expected_length
        ):
            raise JournalError("checkpoint committed journal blob differs")
        for binding in _checkpoint_binding_refs(accepted_prefix, accepted):
            value = _git_blob(workspace, commit, binding["path"])
            if _digest_bytes(value) != binding.get("sha256"):
                raise JournalError(
                    f"checkpoint bound artifact blob differs: {binding['path']}"
                )


def _validate_completed_finalization(
    state: dict[str, Any], manifest: dict[str, Any], document: JournalDocument
) -> dict[str, object]:
    journal = _journal_state(state)
    events = document.events
    accepted_sequence = journal.get("accepted_sequence")
    if (
        isinstance(accepted_sequence, bool)
        or not isinstance(accepted_sequence, int)
        or accepted_sequence < 3
        or accepted_sequence > len(events)
    ):
        raise JournalError("completed run does not have one fully accepted final suffix")
    accepted_events = events[:accepted_sequence]
    trailing_events = events[accepted_sequence:]
    if trailing_events:
        phase8_state_version = _checkpoint_transition_version(
            state.get("git_checkpoints"),
            "phase-8",
            accepted_events[-1].get("checkpoint_uuid"),
            "state",
        )
        phase8_manifest_version = _checkpoint_transition_version(
            manifest.get("checkpoints"),
            "phase-8",
            accepted_events[-1].get("checkpoint_uuid"),
            "manifest",
        )
        expected_recovery = (
            ("recovery-started", "recovery-worker", "start-recovery", "started"),
            ("recovery-completed", "recovery-worker", "complete-recovery", "recovered"),
        )
        if (
            phase8_state_version is not None
            or phase8_manifest_version is not None
            or len(trailing_events) != 2
            or any(
                event.get("event_type") != event_type
                or event.get("agent_role") != agent_role
                or event.get("action_code") != action_code
                or event.get("status_code") != status_code
                or event.get("reason_code") != "resume-reconciliation"
                or event.get("session_id") != journal.get("session_id")
                or event.get("phase") != "done"
                or event.get("epoch_id") != journal.get("epoch_id")
                for event, (
                    event_type, agent_role, action_code, status_code
                ) in zip(trailing_events, expected_recovery, strict=True)
            )
            or trailing_events[0].get("timestamp_utc")
            != trailing_events[1].get("timestamp_utc")
        ):
            raise JournalError(
                "completed run has a noncanonical legacy recovery suffix"
            )
    finalized_indices = [
        index for index, event in enumerate(accepted_events)
        if event.get("event_type") == "run-finalized"
    ]
    handoff_indices = [
        index for index, event in enumerate(accepted_events)
        if event.get("event_type") == "next-run-handoff"
    ]
    if (
        finalized_indices != [len(accepted_events) - 2]
        or handoff_indices != [len(accepted_events) - 1]
    ):
        raise JournalError("completed run must contain one adjacent terminal pair at the tail")
    finalized, handoff = accepted_events[-2:]
    finalized_fields = EVENT_REQUIRED_FIELDS | {
        "agent_role", "action_code", "status_code", "reason_code", "checkpoint_uuid",
    }
    if set(finalized) != finalized_fields or set(handoff) != finalized_fields | {"handoff"}:
        raise JournalError("completed run terminal pair is not canonical")
    expected_finalized = {
        "event_type": "run-finalized",
        "agent_role": "finalizer",
        "action_code": "finalize-run",
        "status_code": "finalized",
        "reason_code": "phase-contract",
        "phase": "phase-8",
    }
    expected_handoff = {
        "event_type": "next-run-handoff",
        "agent_role": "finalizer",
        "action_code": "create-handoff",
        "status_code": "completed",
        "reason_code": "phase-contract",
        "phase": "phase-8",
    }
    if any(finalized.get(field) != value for field, value in expected_finalized.items()):
        raise JournalError("completed run finalized event is not canonical")
    if any(handoff.get(field) != value for field, value in expected_handoff.items()):
        raise JournalError("completed run handoff event is not canonical")
    checkpoint_uuid = _identifier(
        finalized.get("checkpoint_uuid"), "finalized checkpoint_uuid"
    )
    for field in ("session_id", "epoch_id", "timestamp_utc", "checkpoint_uuid"):
        if finalized.get(field) != handoff.get(field):
            raise JournalError("completed run terminal pair is inconsistent")
    accepted_event = accepted_events[-3]
    if (
        accepted_event.get("event_type") != "phase-accepted"
        or accepted_event.get("phase") != "phase-8"
        or accepted_event.get("checkpoint_uuid") != checkpoint_uuid
    ):
        raise JournalError("completed run terminal pair does not follow phase-8 acceptance")
    terminal_transition_version = _checkpoint_transition_version(
        state.get("git_checkpoints"), "phase-8", checkpoint_uuid, "state"
    )
    if terminal_transition_version == TRANSACTION_VERSION and (
        journal.get("epoch_status") != "closed"
        or journal.get("epoch_phase") != "phase-8"
        or journal.get("epoch_id") != finalized.get("epoch_id")
    ):
        raise JournalError("completed v2 run does not close its final phase epoch")
    phases = _validated_phases(manifest)
    if not phases or phases[-1] != "phase-8":
        raise JournalError("completed run manifest does not end with phase-8")
    phase_status = state.get("phase_status", {})
    if not isinstance(phase_status, dict) or phase_status.get("phase-8") != "completed":
        raise JournalError("completed run state has not accepted phase-8")
    phase_checkpoints = [
        entry for entry in state.get("git_checkpoints", [])
        if isinstance(entry, dict) and entry.get("phase") == "phase-8"
    ]
    manifest_phase_checkpoints = [
        entry for entry in manifest.get("checkpoints", [])
        if isinstance(entry, dict) and entry.get("phase") == "phase-8"
    ]
    if (
        len(phase_checkpoints) != 1
        or len(manifest_phase_checkpoints) != 1
        or phase_checkpoints[0].get("checkpoint_uuid") != checkpoint_uuid
        or manifest_phase_checkpoints[0].get("checkpoint_uuid") != checkpoint_uuid
    ):
        raise JournalError("completed run checkpoint differs from terminal pair")
    handoff_data = _validate_handoff(handoff.get("handoff"))
    expected_data = _handoff_data(state, manifest, accepted_events[:-1])
    if handoff_data != expected_data:
        raise JournalError("completed run handoff differs from authoritative projection")
    return {
        "checkpoint_uuid": checkpoint_uuid,
        "sequence": len(events),
        "status": "run complete",
    }


def _validate_resume_epoch_intent(
    document: JournalDocument,
    state: dict[str, Any],
    journal: dict[str, Any],
) -> None:
    expected = _canonical_intent_request(state, journal)
    intents = [
        event for event in document.events
        if event["event_type"] == "user-intent-recorded"
        and event["session_id"] == journal["session_id"]
        and event["phase"] == journal["epoch_phase"]
        and event["epoch_id"] == journal["epoch_id"]
    ]
    if len(intents) != 1 or _request_projection(intents[0]) != expected:
        raise JournalError("resume epoch does not have one canonical user intent")


def _reconcile_resume_locked(workspace: pathlib.Path) -> dict[str, object]:
    state_path = workspace / "state.json"
    state, manifest, _accepted, document = _load_authoritative_context_locked(
        workspace, recover=False
    )
    journal = _journal_state(state)
    if state.get("current_phase") == "done":
        return _validate_completed_finalization(state, manifest, document)

    current_events = [
        event for event in document.events[journal["accepted_sequence"]:]
        if event.get("epoch_id") == journal["epoch_id"]
        and event.get("phase") == journal["epoch_phase"]
    ]
    epoch_matches_state = journal.get("epoch_phase") == state.get("current_phase")
    current_types = [event["event_type"] for event in current_events]
    if epoch_matches_state and current_types == ["run-initialized"]:
        return {
            "epoch_id": journal["epoch_id"],
            "sequence": len(document.events),
        }
    if epoch_matches_state and current_types == [
        "run-initialized", "user-intent-recorded"
    ]:
        _validate_resume_epoch_intent(document, state, journal)
        return {
            "epoch_id": journal["epoch_id"],
            "sequence": len(document.events),
        }
    if epoch_matches_state and current_types == ["run-resumed"]:
        document = _commit_resume_transaction_locked(
            workspace, state, state, document, include_resume=False
        )
        _validate_resume_epoch_intent(document, state, journal)
        return {
            "epoch_id": journal["epoch_id"],
            "sequence": len(document.events),
        }
    if epoch_matches_state and current_types in (
        ["run-resumed", "user-intent-recorded"],
        ["recovery-started", "recovery-completed"],
    ):
        _validate_resume_epoch_intent(document, state, journal)
        return {
            "epoch_id": journal["epoch_id"],
            "sequence": len(document.events),
        }

    # A crash after the authoritative state rotation but before run-resumed is
    # repaired in place.  The completed recovery tail proves that the old epoch
    # was already closed, so rotating again would lose its identity.
    if (
        not current_events
        and len(document.events) >= 2
        and [event["event_type"] for event in document.events[-2:]]
        == ["phase-acceptance-aborted", "recovery-completed"]
        and document.events[-1]["session_id"] == journal["session_id"]
    ):
        document = _commit_resume_transaction_locked(
            workspace, state, state, document, include_resume=True
        )
        return {
            "epoch_id": journal["epoch_id"],
            "sequence": len(document.events),
        }

    open_attempts = _open_agent_attempts(document, journal)
    if open_attempts:
        raise JournalError(
            "open agent attempts require exact terminal events before reconciliation"
        )

    old_state = copy.deepcopy(state)
    new_state = copy.deepcopy(state)
    new_journal = _journal_state(new_state)
    new_epoch = str(uuid.uuid4())
    new_journal["epoch_id"] = new_epoch
    new_journal["epoch_phase"] = new_state["current_phase"]
    new_journal["epoch_status"] = "open"
    new_journal["checkpoint_uuid"] = str(uuid.uuid4())
    document = _commit_resume_transaction_locked(
        workspace, old_state, new_state, document,
        include_resume=True,
        include_recovery=True,
    )
    return {"epoch_id": new_epoch, "sequence": len(document.events)}


def _record_recovery_blocked_locked(workspace: pathlib.Path) -> None:
    """Append one safe failure marker without trusting checkpoint authority."""
    try:
        state = _load_json_object(workspace / "state.json")
        journal = _journal_state(state)
        if state.get("current_phase") == "done" or journal.get("epoch_status") != "open":
            return
        document = parse_journal(_current_journal_path(workspace, state))
        _validate_accepted_prefix(document, journal)
        current = [
            event for event in document.events[journal["accepted_sequence"]:]
            if event.get("epoch_id") == journal["epoch_id"]
            and event.get("phase") == journal["epoch_phase"]
        ]
        if any(event["event_type"] == "recovery-blocked" for event in current):
            return
        _append_event(document.path, document, {
            "event_type": "recovery-blocked",
            "session_id": journal["session_id"],
            "phase": journal["epoch_phase"],
            "epoch_id": journal["epoch_id"],
            "agent_role": "recovery-worker",
            "action_code": "block-recovery",
            "status_code": "blocked",
            "reason_code": "resume-reconciliation",
        })
    except (JournalError, OSError):
        # If the hash chain, authoritative prefix, or file identity is unsafe,
        # preserving the original evidence takes precedence over telemetry.
        return


def reconcile(workspace: pathlib.Path) -> dict[str, object]:
    """Recover a pending acceptance, or rotate a fresh resume epoch."""
    with workspace_lock(workspace):
        try:
            recovered = _recover_pending_transactions_locked(workspace)
            if recovered is not None:
                return recovered
            return _reconcile_resume_locked(workspace)
        except JournalError:
            if (
                _load_transaction(workspace) is None
                and _load_init_transaction(workspace) is None
                and _load_resume_transaction(workspace) is None
            ):
                _record_recovery_blocked_locked(workspace)
            raise


def _select_artifact(value: bytes, selector: str | None) -> bytes:
    if selector is None:
        return value
    selector = _artifact_selector(selector)
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JournalError("artifact selector requires UTF-8 content") from exc
    lines = text.splitlines(keepends=True)
    if FINDING_ID_RE.fullmatch(selector):
        heading = re.compile(rf"^##\s+.*\b{re.escape(selector)}\b")
    else:
        heading = re.compile(rf"^#+\s+.*\b{re.escape(selector)}\b", re.IGNORECASE)
    start = next((index for index, line in enumerate(lines) if heading.search(line)), None)
    if start is None:
        raise JournalError("artifact selector was not found in checkpoint content")
    level = len(lines[start]) - len(lines[start].lstrip("#"))
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = re.match(r"^(#+)\s+", lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    return "".join(lines[start:end]).encode("utf-8")


def resolve_artifact(
    workspace: pathlib.Path,
    checkpoint_uuid: str,
    path: str,
    sha256: str,
    selector: str | None = None,
) -> bytes:
    checkpoint_uuid = _identifier(checkpoint_uuid, "checkpoint_uuid")
    path = safe_relative_path(path)
    sha256 = _sha256(sha256, "artifact sha256")
    if selector is not None:
        selector = _artifact_selector(selector)
    with workspace_lock(workspace):
        state, manifest, _accepted, document = _load_authoritative_context_locked(
            workspace
        )
        journal = _journal_state(state)
        accepted_prefix = document.events[:journal["accepted_sequence"]]
        _facts, acceptance_events = _accepted_fact_selection(
            state, manifest, accepted_prefix
        )
        requested_binding: dict[str, str] = {
            "path": path,
            "checkpoint_uuid": checkpoint_uuid,
            "sha256": sha256,
        }
        if selector is not None:
            requested_binding["selector"] = selector
        accepted_bindings = [
            binding
            for event in acceptance_events
            for binding in _checkpoint_binding_refs(accepted_prefix, event)
        ]
        if requested_binding not in accepted_bindings:
            raise JournalError(
                "artifact selector/path/digest does not match an accepted binding"
            )
        checkpoint_entries = [
            entry for entry in state.get("git_checkpoints", [])
            if isinstance(entry, dict)
            and entry.get("checkpoint_uuid") == checkpoint_uuid
        ]
        if len(checkpoint_entries) != 1:
            raise JournalError(
                "checkpoint UUID does not identify one tracked checkpoint"
            )
        phase = _identifier(
            checkpoint_entries[0].get("phase"), "checkpoint phase"
        )
        ref = _checkpoint_ref(journal["session_id"], phase)
        resolved_ref = _git(workspace, "rev-parse", "--verify", "--quiet", ref)
        if resolved_ref.returncode:
            raise JournalError("authoritative checkpoint ref is missing")
        ref_commit = resolved_ref.stdout.decode("ascii").strip()
        matches = _checkpoint_commits(workspace, checkpoint_uuid)
        if len(matches) != 1 or matches[0] != ref_commit:
            raise JournalError(
                "checkpoint UUID does not identify exactly one commit"
            )
        commit = ref_commit
        message = _git_required(workspace, "show", "-s", "--format=%B", commit)
        if not _commit_has_checkpoint_trailer(message, checkpoint_uuid):
            raise JournalError("checkpoint commit trailer is invalid")
        value = _git_blob(workspace, commit, path)
        if _digest_bytes(value) != sha256:
            raise JournalError("checkpoint artifact SHA-256 differs from binding")
        return _select_artifact(value, selector)


def _load_authoritative_context_locked(
    workspace: pathlib.Path,
    *,
    recover: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], JournalDocument]:
    if recover:
        _recover_pending_transactions_locked(workspace)
    state = _load_json_object(workspace / "state.json")
    journal = _journal_state(state)
    document = parse_journal(_current_journal_path(workspace, state))
    _validate_resume_context(workspace, state, document)
    manifest_path = workspace / "output" / "session-manifest.json"
    manifest = _load_json_object(manifest_path) if manifest_path.exists() else {}
    _validate_checkpoint_authority(workspace, state, manifest, document)
    sequence = journal["accepted_sequence"]
    accepted_prefix = document.events[:sequence]
    if state.get("current_phase") == "done":
        _validate_completed_finalization(state, manifest, document)
        accepted_prefix = accepted_prefix[:-1]
    accepted = _accepted_fact_events(state, manifest, accepted_prefix)
    return state, manifest, accepted, document


def load_authoritative_context(workspace: pathlib.Path) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], JournalDocument]:
    with workspace_lock(workspace):
        return _load_authoritative_context_locked(workspace)


def validate_authoritative(workspace: pathlib.Path) -> JournalDocument:
    with workspace_lock(workspace):
        _state, _manifest, _accepted, document = (
            _load_authoritative_context_locked(workspace)
        )
        return document


def _validated_phases(manifest: dict[str, Any]) -> list[str]:
    phases = manifest.get("phases_completed", [])
    if not isinstance(phases, list):
        raise JournalError("manifest phases_completed must be a list")
    return [_identifier(phase, "completed phase") for phase in phases]


def summarize_agent_counts(events: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for event in events:
        if event["event_type"] not in LIFECYCLE_EVENT_TYPES:
            continue
        role = event.get("agent_role", "unattributed")
        outcome = event.get("status_code", event["event_type"])
        by_role = summary.setdefault(role, {})
        by_role[outcome] = by_role.get(outcome, 0) + 1
    return {role: dict(sorted(outcomes.items())) for role, outcomes in sorted(summary.items())}


def summarize_codes(events: list[dict[str, Any]], field: str) -> dict[str, int]:
    summary: dict[str, int] = {}
    for event in events:
        value = event.get(field)
        if isinstance(value, str):
            summary[value] = summary.get(value, 0) + 1
    return dict(sorted(summary.items()))


def accepted_unresolved_ids(events: list[dict[str, Any]]) -> list[dict[str, str]]:
    promoted: dict[tuple[str, str, str], dict[str, str]] = {}
    for event in events:
        if event.get("event_type") != "artifact-bound":
            continue
        for ref in _validate_artifact_refs(event.get("artifact_refs", [])):
            selector = ref.get("selector")
            if (
                isinstance(selector, str)
                and FINDING_ID_RE.fullmatch(selector)
                and "checkpoint_uuid" in ref
                and "sha256" in ref
            ):
                promoted[(event["epoch_id"], ref["path"], selector)] = ref
    unresolved: dict[bytes, dict[str, str]] = {}
    for event in events:
        if event.get("decision_code") not in {"unresolved", "deferred"}:
            continue
        for ref in _validate_artifact_refs(event.get("artifact_refs", [])):
            selector = ref.get("selector")
            if isinstance(selector, str) and FINDING_ID_RE.fullmatch(selector):
                binding = promoted.get((event["epoch_id"], ref["path"], selector))
                if binding is None:
                    raise JournalError(
                        "accepted unresolved selector lacks its promoted binding"
                    )
                parts = pathlib.PurePosixPath(binding["path"]).parts
                if len(parts) < 3 or parts[0] != "findings":
                    raise JournalError(
                        "accepted unresolved selector lacks repository scope"
                    )
                scoped = {
                    "repo_slug": parts[1],
                    "path": binding["path"],
                    "selector": selector,
                    "checkpoint_uuid": binding["checkpoint_uuid"],
                    "sha256": binding["sha256"],
                }
                unresolved[canonical_json(scoped)] = scoped
    return sorted(
        unresolved.values(),
        key=lambda item: (
            item["repo_slug"], item["selector"], item["checkpoint_uuid"],
            item["path"], item["sha256"],
        ),
    )


def _project_lifecycle_event(event: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "event_id": event["event_id"],
        "event_type": event["event_type"],
    }
    for field in (
        "agent_id", "parent_agent_id", "agent_role", "action_code",
        "status_code", "reason_code", "counts",
    ):
        if field in event:
            entry[field] = event[field]
    return entry


def current_epoch_lifecycle(document: JournalDocument, state: dict[str, Any]) -> list[dict[str, Any]]:
    journal = _journal_state(state)
    accepted_sequence = journal["accepted_sequence"]
    lifecycle: list[dict[str, Any]] = []
    for event in document.events[accepted_sequence:]:
        if event["epoch_id"] != journal["epoch_id"] or event["event_type"] not in LIFECYCLE_EVENT_TYPES:
            continue
        lifecycle.append(_project_lifecycle_event(event))
    return lifecycle


def interrupted_attempts(
    document: JournalDocument, state: dict[str, Any]
) -> list[dict[str, Any]]:
    """Project abandoned epoch lifecycle without promoting it to accepted facts."""
    aborted_epochs = {
        event["epoch_id"]
        for event in document.events
        if event["event_type"] == "phase-acceptance-aborted"
    }
    attempts: list[dict[str, Any]] = []
    for epoch_id in sorted(aborted_epochs):
        epoch_events = [
            event for event in document.events if event["epoch_id"] == epoch_id
        ]
        if not epoch_events:
            continue
        projected: list[dict[str, Any]] = []
        for event in epoch_events:
            if event["event_type"] not in LIFECYCLE_EVENT_TYPES:
                continue
            projected.append(_project_lifecycle_event(event))
        attempts.append({
            "epoch_id": epoch_id,
            "phase": epoch_events[0]["phase"],
            "events": projected,
        })
    return attempts


def resume_action(state: dict[str, Any]) -> str:
    current_phase = _identifier(state.get("current_phase"), "state current_phase")
    if state.get("run_status") == "complete" or current_phase in {"done", "complete", "completed"}:
        return "run complete"
    return "resume current phase"


def bounded_markdown_projection(projection: dict[str, Any], maximum_bytes: int) -> str:
    if maximum_bytes < 1:
        raise JournalError("handoff maximum must be positive")
    bounded = copy.deepcopy(projection)

    def render() -> str:
        return (
            HANDOFF_HEADING.decode("ascii")
            + "```json\n"
            + canonical_json(bounded).decode("ascii")
            + "\n```\n"
        )

    def mark_trimmed() -> None:
        bounded["truncated"] = True

    while len(render().encode("utf-8")) > maximum_bytes:
        interrupted = bounded.get("interrupted_attempt")
        if isinstance(interrupted, dict):
            current = interrupted.get("current_epoch_events")
            if isinstance(current, list) and current:
                current.pop(0)
                mark_trimmed()
                continue
            attempts = interrupted.get("attempts")
            if isinstance(attempts, list) and attempts:
                first = attempts[0]
                events = first.get("events") if isinstance(first, dict) else None
                if isinstance(events, list) and len(events) > 1:
                    events.pop(0)
                else:
                    attempts.pop(0)
                mark_trimmed()
                continue
        trimmed = False
        for field in ("unresolved_ids", "accepted_artifacts", "repositories"):
            entries = bounded.get(field)
            if isinstance(entries, list) and entries:
                removed = entries.pop()
                if field == "accepted_artifacts" and isinstance(removed, dict):
                    unresolved = bounded.get("unresolved_ids")
                    if isinstance(unresolved, list):
                        bounded["unresolved_ids"] = [
                            item for item in unresolved
                            if not isinstance(item, dict)
                            or any(
                                item.get(key) != removed.get(key)
                                for key in (
                                    "path", "selector", "checkpoint_uuid",
                                    "sha256",
                                )
                            )
                        ]
                mark_trimmed()
                trimmed = True
                break
        if trimmed:
            continue
        phases = bounded.get("phases_completed")
        if isinstance(phases, list) and len(phases) > 1:
            removed_phase = phases.pop(0)
            history = bounded.get("phase_history")
            if isinstance(history, list) and removed_phase in history:
                history.remove(removed_phase)
            phase_status = bounded.get("phase_status")
            if isinstance(phase_status, dict):
                phase_status.pop(removed_phase, None)
            checkpoints = bounded.get("checkpoints")
            removed_checkpoints: set[str] = set()
            if isinstance(checkpoints, list):
                retained = []
                for checkpoint in checkpoints:
                    if (
                        isinstance(checkpoint, dict)
                        and checkpoint.get("phase") == removed_phase
                    ):
                        identifier = checkpoint.get("checkpoint_uuid")
                        if isinstance(identifier, str):
                            removed_checkpoints.add(identifier)
                    else:
                        retained.append(checkpoint)
                bounded["checkpoints"] = retained
            artifacts = bounded.get("accepted_artifacts")
            if isinstance(artifacts, list):
                bounded["accepted_artifacts"] = [
                    artifact for artifact in artifacts
                    if not isinstance(artifact, dict)
                    or artifact.get("checkpoint_uuid") not in removed_checkpoints
                ]
            unresolved = bounded.get("unresolved_ids")
            if isinstance(unresolved, list):
                bounded["unresolved_ids"] = [
                    item for item in unresolved
                    if not isinstance(item, dict)
                    or item.get("checkpoint_uuid") not in removed_checkpoints
                ]
            mark_trimmed()
            continue
        raise JournalError("handoff projection exceeds maximum size")
    _validate_handoff(bounded, maximum_bytes=maximum_bytes)
    return render()


def _validate_projected_lifecycle_event(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise JournalError("provisional lifecycle event must be an object")
    event_type = value.get("event_type")
    if event_type not in LIFECYCLE_EVENT_TYPES:
        raise JournalError("provisional lifecycle event type is invalid")
    contract = EVENT_CONTRACTS[event_type]
    required = {"event_id", "event_type"} | set(contract["required"])
    optional = set(contract.get("optional", set()))
    if not required <= set(value) or set(value) - required - optional:
        raise JournalError("provisional lifecycle event has an invalid closed schema")
    _identifier(value.get("event_id"), "provisional event_id")
    for field in ("agent_id", "parent_agent_id"):
        if field in value:
            _attempt_id(value[field], f"provisional {field}")
    _enum(value.get("agent_role"), "provisional agent_role", AGENT_ROLES)
    _enum(value.get("action_code"), "provisional action_code", ACTION_CODES)
    _enum(value.get("status_code"), "provisional status_code", STATUS_CODES)
    _enum(value.get("reason_code"), "provisional reason_code", REASON_CODES)
    if "counts" in value:
        _validate_counts(value["counts"])
    fixed = contract.get("fixed", {})
    if any(value.get(field) != expected for field, expected in fixed.items()):
        raise JournalError("provisional lifecycle event mapping is invalid")
    pairs = contract.get("pairs")
    if pairs is not None and (
        value.get("status_code"), value.get("reason_code")
    ) not in pairs:
        raise JournalError("provisional lifecycle event status/reason is invalid")
    return value


def _validate_interrupted_attempt(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "attempts", "current_epoch_events",
    }:
        raise JournalError("handoff interrupted_attempt has an invalid schema")
    attempts = value.get("attempts")
    current = value.get("current_epoch_events")
    if (
        not isinstance(attempts, list)
        or len(attempts) > 64
        or not isinstance(current, list)
        or len(current) > 64
    ):
        raise JournalError("handoff provisional lifecycle is unbounded")
    seen_epochs: set[str] = set()
    for attempt in attempts:
        if not isinstance(attempt, dict) or set(attempt) != {
            "epoch_id", "phase", "events",
        }:
            raise JournalError("handoff interrupted epoch has an invalid schema")
        epoch_id = _identifier(attempt["epoch_id"], "interrupted epoch_id")
        _identifier(attempt["phase"], "interrupted phase")
        events = attempt["events"]
        if (
            epoch_id in seen_epochs
            or not isinstance(events, list)
            or len(events) > 64
        ):
            raise JournalError("handoff interrupted epochs must be unique and bounded")
        seen_epochs.add(epoch_id)
        for event in events:
            _validate_projected_lifecycle_event(event)
    for event in current:
        _validate_projected_lifecycle_event(event)
    return value


def _validate_handoff(
    value: Any, *, maximum_bytes: int = HANDOFF_MAX_BYTES
) -> dict[str, Any]:
    required = {
        "schema", "session_id", "date", "mode", "current_phase",
        "phases_completed", "agent_counts", "decision_counts",
        "unresolved_ids", "resume_action", "repositories", "phase_status",
        "phase_history", "checkpoints", "reason_counts", "retry_count",
        "recovery_counts", "failure_classes", "accepted_artifacts",
        "interrupted_attempt",
    }
    if (
        not isinstance(value, dict)
        or not required <= set(value)
        or set(value) - required - {"truncated"}
    ):
        raise JournalError("handoff has an invalid closed schema")
    if value.get("schema") != HEADER_SCHEMA:
        raise JournalError("handoff schema is invalid")
    _identifier(value.get("session_id"), "handoff session_id")
    if not isinstance(value.get("date"), str) or not DATE_RE.fullmatch(value["date"]):
        raise JournalError("handoff date is invalid")
    if value.get("mode") not in JOURNAL_MODES:
        raise JournalError("handoff mode is invalid")
    _identifier(value.get("current_phase"), "handoff current_phase")
    phases = value.get("phases_completed")
    if (
        not isinstance(phases, list)
        or len(phases) > len(REVIEW_ORDER)
        or len(phases) != len(set(phases))
    ):
        raise JournalError("handoff phases_completed is invalid")
    for phase in phases:
        _identifier(phase, "handoff completed phase")
    repositories = value.get("repositories")
    if (
        not isinstance(repositories, list)
        or len(repositories) > HANDOFF_MAX_REPOSITORIES
    ):
        raise JournalError("handoff repositories is invalid")
    repository_slugs: list[str] = []
    for repository in repositories:
        if not isinstance(repository, dict) or set(repository) != {"repo_slug"}:
            raise JournalError("handoff repository has an invalid schema")
        repository_slugs.append(_identifier(repository["repo_slug"], "handoff repo_slug"))
    if repository_slugs != sorted(set(repository_slugs)):
        raise JournalError("handoff repositories must be sorted and unique")
    phase_status = value.get("phase_status")
    if not isinstance(phase_status, dict) or len(phase_status) > len(REVIEW_ORDER):
        raise JournalError("handoff phase_status is invalid")
    for phase, status in phase_status.items():
        _identifier(phase, "handoff phase status phase")
        if status != "completed":
            raise JournalError("handoff phase status must be accepted")
    phase_history = value.get("phase_history")
    if (
        not isinstance(phase_history, list)
        or len(phase_history) > len(REVIEW_ORDER)
        or len(phase_history) != len(set(phase_history))
    ):
        raise JournalError("handoff phase_history is invalid")
    for phase in phase_history:
        _identifier(phase, "handoff phase history entry")
    if phase_history != phases or set(phase_status) - set(phase_history):
        raise JournalError("handoff accepted phase projections disagree")
    checkpoints = value.get("checkpoints")
    if not isinstance(checkpoints, list) or len(checkpoints) > HANDOFF_MAX_CHECKPOINTS:
        raise JournalError("handoff checkpoints is invalid")
    checkpoint_ids: list[str] = []
    for checkpoint in checkpoints:
        if not isinstance(checkpoint, dict) or set(checkpoint) != {"phase", "checkpoint_uuid"}:
            raise JournalError("handoff checkpoint has an invalid schema")
        _identifier(checkpoint["phase"], "handoff checkpoint phase")
        checkpoint_ids.append(_identifier(
            checkpoint["checkpoint_uuid"], "handoff checkpoint_uuid"
        ))
    if len(checkpoint_ids) != len(set(checkpoint_ids)):
        raise JournalError("handoff checkpoints must be unique")
    if any(checkpoint["phase"] not in phase_history for checkpoint in checkpoints):
        raise JournalError("handoff checkpoint phase is not accepted")
    counts = value.get("agent_counts")
    if not isinstance(counts, dict) or len(counts) > len(AGENT_ROLES) + 1:
        raise JournalError("handoff agent_counts is invalid")
    for role, outcomes in counts.items():
        _identifier(role, "handoff agent role")
        if not isinstance(outcomes, dict) or len(outcomes) > len(STATUS_CODES) + 1:
            raise JournalError("handoff agent outcome counts are invalid")
        _validate_counts(outcomes)
    decisions = value.get("decision_counts")
    if not isinstance(decisions, dict) or len(decisions) > len(DECISION_CODES):
        raise JournalError("handoff decision_counts is invalid")
    for decision in decisions:
        _enum(decision, "handoff decision code", DECISION_CODES)
    _validate_counts(decisions)
    reasons = value.get("reason_counts")
    if not isinstance(reasons, dict) or len(reasons) > len(REASON_CODES):
        raise JournalError("handoff reason_counts is invalid")
    for reason in reasons:
        _enum(reason, "handoff reason code", REASON_CODES)
    _validate_counts(reasons)
    retry_count = value.get("retry_count")
    if isinstance(retry_count, bool) or not isinstance(retry_count, int) or retry_count < 0:
        raise JournalError("handoff retry_count is invalid")
    recovery_counts = value.get("recovery_counts")
    recovery_types = frozenset({"recovery-started", "recovery-completed", "recovery-blocked"})
    if not isinstance(recovery_counts, dict) or set(recovery_counts) - recovery_types:
        raise JournalError("handoff recovery_counts is invalid")
    _validate_counts(recovery_counts)
    failure_classes = value.get("failure_classes")
    if not isinstance(failure_classes, dict) or len(failure_classes) > len(REASON_CODES):
        raise JournalError("handoff failure_classes is invalid")
    for reason in failure_classes:
        _enum(reason, "handoff failure class", REASON_CODES)
    _validate_counts(failure_classes)
    artifacts = value.get("accepted_artifacts")
    if not isinstance(artifacts, list) or len(artifacts) > HANDOFF_MAX_ARTIFACTS:
        raise JournalError("handoff accepted_artifacts is invalid")
    normalized_artifacts = _validate_artifact_refs(artifacts)
    if any("checkpoint_uuid" not in ref or "sha256" not in ref for ref in normalized_artifacts):
        raise JournalError("handoff accepted artifact lacks checkpoint binding")
    if artifacts != sorted(artifacts, key=lambda ref: canonical_json(ref)):
        raise JournalError("handoff accepted_artifacts is not canonical")
    if len({canonical_json(ref) for ref in artifacts}) != len(artifacts):
        raise JournalError("handoff accepted_artifacts must be unique")
    if any(ref["checkpoint_uuid"] not in checkpoint_ids for ref in normalized_artifacts):
        raise JournalError("handoff accepted artifact checkpoint is not accepted")
    unresolved = value.get("unresolved_ids")
    if not isinstance(unresolved, list):
        raise JournalError("handoff unresolved_ids is invalid")
    normalized_unresolved: list[dict[str, str]] = []
    for item in unresolved:
        if not isinstance(item, dict) or set(item) != {
            "repo_slug", "path", "selector", "checkpoint_uuid", "sha256",
        }:
            raise JournalError("handoff unresolved reference has an invalid schema")
        normalized = {
            "repo_slug": _identifier(item["repo_slug"], "unresolved repo_slug"),
            "path": safe_relative_path(item["path"]),
            "selector": _artifact_selector(item["selector"]),
            "checkpoint_uuid": _identifier(
                item["checkpoint_uuid"], "unresolved checkpoint_uuid"
            ),
            "sha256": _sha256(item["sha256"], "unresolved sha256"),
        }
        parts = pathlib.PurePosixPath(normalized["path"]).parts
        if (
            not FINDING_ID_RE.fullmatch(normalized["selector"])
            or len(parts) < 3
            or parts[0] != "findings"
            or parts[1] != normalized["repo_slug"]
            or normalized["checkpoint_uuid"] not in checkpoint_ids
        ):
            raise JournalError("handoff unresolved reference scope is invalid")
        accepted_binding = {
            key: normalized[key]
            for key in ("path", "selector", "checkpoint_uuid", "sha256")
        }
        if accepted_binding not in normalized_artifacts:
            raise JournalError(
                "handoff unresolved reference lacks its accepted selector binding"
            )
        normalized_unresolved.append(normalized)
    if (
        unresolved != sorted(
            unresolved,
            key=lambda item: (
                item["repo_slug"], item["selector"], item["checkpoint_uuid"],
                item["path"], item["sha256"],
            ),
        )
        or len({canonical_json(item) for item in unresolved}) != len(unresolved)
    ):
        raise JournalError("handoff unresolved references are not canonical")
    _validate_interrupted_attempt(value.get("interrupted_attempt"))
    if "truncated" in value and value["truncated"] is not True:
        raise JournalError("handoff truncated marker is invalid")
    if value.get("resume_action") not in {"resume current phase", "run complete"}:
        raise JournalError("handoff resume action is invalid")
    rendered_length = len((
        HANDOFF_HEADING
        + b"```json\n"
        + canonical_json(value)
        + b"\n```\n"
    ))
    if rendered_length > maximum_bytes:
        raise JournalError("handoff exceeds maximum size")
    return value


def _handoff_data(
    state: dict[str, Any],
    manifest: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    journal = _journal_state(state)
    accepted, acceptance_events = _accepted_fact_selection(
        state, manifest, events
    )
    mode = state.get("mode", "review")
    if mode not in JOURNAL_MODES:
        raise JournalError("state mode is invalid")
    repo_slugs = state.get("repo_slugs", [])
    if not isinstance(repo_slugs, list):
        raise JournalError("state repo_slugs must be a list")
    normalized_repo_slugs = [
        _identifier(repo_slug, "state repo_slug") for repo_slug in repo_slugs
    ]
    repositories = [
        {"repo_slug": repo_slug} for repo_slug in sorted(set(normalized_repo_slugs))
    ]
    phases_completed = _validated_phases(manifest)
    raw_phase_status = state.get("phase_status", {})
    if not isinstance(raw_phase_status, dict):
        raise JournalError("state phase_status must be an object")
    phase_status = {
        phase: "completed" for phase in phases_completed
        if raw_phase_status.get(phase) == "completed"
    }
    raw_checkpoints = manifest.get("checkpoints", [])
    if not isinstance(raw_checkpoints, list):
        raise JournalError("manifest checkpoints must be a list")
    checkpoints: list[dict[str, str]] = []
    for checkpoint in raw_checkpoints:
        if not isinstance(checkpoint, dict):
            raise JournalError("manifest checkpoint must be an object")
        checkpoints.append({
            "phase": _identifier(checkpoint.get("phase"), "manifest checkpoint phase"),
            "checkpoint_uuid": _identifier(
                checkpoint.get("checkpoint_uuid"), "manifest checkpoint_uuid"
            ),
        })
    artifact_bindings: dict[bytes, dict[str, str]] = {}
    for event in acceptance_events:
        for ref in _checkpoint_binding_refs(events, event):
            if "checkpoint_uuid" in ref and "sha256" in ref:
                artifact_bindings[canonical_json(ref)] = ref
    accepted_artifacts = [artifact_bindings[key] for key in sorted(artifact_bindings)]
    unresolved_refs = accepted_unresolved_ids(accepted)
    recovery_counts = {
        event_type: sum(1 for event in accepted if event.get("event_type") == event_type)
        for event_type in ("recovery-started", "recovery-completed", "recovery-blocked")
    }
    failure_events = {
        "agent-failed", "validation-failed", "recovery-blocked",
    }
    failure_classes = summarize_codes(
        [event for event in accepted if event.get("event_type") in failure_events],
        "reason_code",
    )
    truncated = False
    if len(repositories) > HANDOFF_MAX_REPOSITORIES:
        repositories = repositories[:HANDOFF_MAX_REPOSITORIES]
        truncated = True
    if len(checkpoints) > HANDOFF_MAX_CHECKPOINTS:
        checkpoints = checkpoints[:HANDOFF_MAX_CHECKPOINTS]
        truncated = True
    if len(accepted_artifacts) > HANDOFF_MAX_ARTIFACTS:
        unresolved_binding_keys = {
            canonical_json({
                key: item[key]
                for key in ("path", "selector", "checkpoint_uuid", "sha256")
            })
            for item in unresolved_refs
        }
        required_bindings = [
            binding for binding in accepted_artifacts
            if canonical_json(binding) in unresolved_binding_keys
        ]
        if len(required_bindings) > HANDOFF_MAX_ARTIFACTS:
            required_bindings = required_bindings[:HANDOFF_MAX_ARTIFACTS]
            retained = {canonical_json(binding) for binding in required_bindings}
            unresolved_refs = [
                item for item in unresolved_refs
                if canonical_json({
                    key: item[key]
                    for key in ("path", "selector", "checkpoint_uuid", "sha256")
                }) in retained
            ]
        remainder = [
            binding for binding in accepted_artifacts
            if binding not in required_bindings
        ]
        accepted_artifacts = sorted(
            required_bindings
            + remainder[:HANDOFF_MAX_ARTIFACTS - len(required_bindings)],
            key=canonical_json,
        )
        truncated = True
    projection_document = JournalDocument(
        pathlib.Path(),
        str(state.get("date", "")),
        str(journal.get("session_id", "")),
        events,
        0,
        0,
        [],
    )
    attempts = interrupted_attempts(projection_document, state)
    current_events = current_epoch_lifecycle(projection_document, state)
    if len(attempts) > 64:
        attempts = attempts[-64:]
        truncated = True
    for attempt in attempts:
        if len(attempt["events"]) > 64:
            attempt["events"] = attempt["events"][-64:]
            truncated = True
    if len(current_events) > 64:
        current_events = current_events[-64:]
        truncated = True
    projection: dict[str, Any] = {
        "schema": HEADER_SCHEMA,
        "session_id": _identifier(journal.get("session_id"), "state session_id"),
        "date": state.get("date"),
        "mode": mode,
        "current_phase": _identifier(state.get("current_phase"), "state current_phase"),
        "phases_completed": phases_completed,
        "repositories": repositories,
        "phase_status": phase_status,
        "phase_history": phases_completed.copy(),
        "checkpoints": checkpoints,
        "agent_counts": summarize_agent_counts(accepted),
        # User intent also carries decision_code=accepted as part of its closed
        # event contract.  The handoff's decision summary is intentionally the
        # review-decision projection, not a count of administrative metadata.
        "decision_counts": summarize_codes(
            [event for event in accepted if event["event_type"] == "decision-recorded"],
            "decision_code",
        ),
        "reason_counts": summarize_codes(accepted, "reason_code"),
        "retry_count": sum(1 for event in accepted if event.get("event_type") == "retry-scheduled"),
        "recovery_counts": recovery_counts,
        "failure_classes": failure_classes,
        "accepted_artifacts": accepted_artifacts,
        "unresolved_ids": unresolved_refs,
        "resume_action": resume_action(state),
        "interrupted_attempt": {
            "attempts": attempts,
            "current_epoch_events": current_events,
        },
    }
    if truncated:
        projection["truncated"] = True
    rendered = bounded_markdown_projection(projection, HANDOFF_MAX_BYTES)
    return json.loads(
        rendered.split("```json\n", 1)[1].split("\n```", 1)[0],
        object_pairs_hook=_json_object_pairs,
    )


def render_handoff(workspace: pathlib.Path) -> str:
    state, manifest, _accepted, document = load_authoritative_context(workspace)
    projection = _handoff_data(state, manifest, document.events)
    return (
        HANDOFF_HEADING.decode("ascii")
        + "```json\n"
        + canonical_json(projection).decode("ascii")
        + "\n```\n"
    )


def _request_from_args(args: argparse.Namespace) -> dict[str, object]:
    request: dict[str, object] = {"event_type": args.event_type}
    for field in ("session_id", "phase", "epoch_id", "agent_id", "parent_agent_id", "agent_role",
                  "action_code", "status_code", "decision_code", "reason_code"):
        value = getattr(args, field)
        if value is not None:
            request[field] = value
    for field in ("artifact_refs", "counts"):
        value = getattr(args, field)
        if value is not None:
            try:
                request[field] = json.loads(value, object_pairs_hook=_json_object_pairs)
            except (json.JSONDecodeError, JournalError) as exc:
                raise JournalError(f"{field} is not valid JSON: {exc}") from exc
    return request


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--workspace", required=True)
    init.add_argument("--date", required=True)
    init.add_argument("--port")
    record = commands.add_parser("record")
    record.add_argument("--workspace", required=True)
    record.add_argument("--event-type", required=True)
    for field in ("session_id", "phase", "epoch_id", "agent_id", "parent_agent_id", "agent_role",
                  "action_code", "status_code", "decision_code", "reason_code", "artifact_refs", "counts"):
        record.add_argument("--" + field.replace("_", "-"), dest=field)
    intent = commands.add_parser("record-intent")
    intent.add_argument("--workspace", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--workspace", required=True)
    reconcile_parser = commands.add_parser("reconcile")
    reconcile_parser.add_argument("--workspace", required=True)
    accept = commands.add_parser("accept-phase")
    accept.add_argument("--workspace", required=True)
    accept.add_argument("--phase", required=True)
    accept.add_argument("--timestamp", required=True)
    accept.add_argument("--skip-candidates", action="store_true")
    accept.add_argument("--no-debate-needed", action="store_true")
    accept.add_argument("--no-validation-needed", action="store_true")
    accept.add_argument("--no-poc-needed", action="store_true")
    resolve = commands.add_parser("resolve-artifact")
    resolve.add_argument("--workspace", required=True)
    resolve.add_argument("--checkpoint-uuid", required=True)
    resolve.add_argument("--path", required=True)
    resolve.add_argument("--sha256", required=True)
    resolve.add_argument("--selector")
    context = commands.add_parser("context")
    context.add_argument("--workspace", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    workspace = pathlib.Path(args.workspace).resolve()
    try:
        if args.command == "init":
            result: object = initialize(workspace, args.date, args.port)
        elif args.command == "record":
            result = record_event(workspace, _request_from_args(args))
        elif args.command == "record-intent":
            result = record_intent(workspace)
        elif args.command == "validate":
            result = validate_authoritative(workspace).as_dict()
        elif args.command == "reconcile":
            result = reconcile(workspace)
        elif args.command == "accept-phase":
            result = accept_phase(
                workspace,
                args.phase,
                args.timestamp,
                skip_candidates=args.skip_candidates,
                no_debate_needed=args.no_debate_needed,
                no_validation_needed=args.no_validation_needed,
                no_poc_needed=args.no_poc_needed,
            )
        elif args.command == "resolve-artifact":
            os.sys.stdout.buffer.write(resolve_artifact(
                workspace, args.checkpoint_uuid, args.path, args.sha256, args.selector
            ))
            return 0
        else:
            print(render_handoff(workspace), end="")
            return 0
    except JournalError as exc:
        print(f"ERROR: {exc}", file=os.sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
