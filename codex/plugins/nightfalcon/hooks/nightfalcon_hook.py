#!/usr/bin/env python3
"""Codex lifecycle-hook adapter for NightFalcon's deterministic gate script."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys


PATCH_PATH = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$")
MOVE_PATH = re.compile(r"^\*\*\* Move to: (.+)$")
SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]+$")
MARKER = ".nightfalcon-review"


@dataclasses.dataclass(frozen=True)
class HookResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""


def _valid_session_id(value: object) -> bool:
    return isinstance(value, str) and bool(value) and SAFE_SESSION_ID.fullmatch(value) is not None


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_write_json(path: pathlib.Path, document: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(document, indent=2) + "\n")
        temporary.replace(path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _registry_paths(
    workspace: pathlib.Path, session_id: str, plugin_data: pathlib.Path
) -> tuple[pathlib.Path, pathlib.Path]:
    if not _valid_session_id(session_id):
        raise ValueError("invalid session_id")
    key = hashlib.sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()
    root = plugin_data / "active-agents" / key
    return root / f"{session_id}.json", root / f"{session_id}.lock"


@contextlib.contextmanager
def _locked_registry(
    workspace: pathlib.Path, session_id: str, plugin_data: pathlib.Path
):
    path, lock_path = _registry_paths(workspace, session_id, plugin_data)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield path


@contextlib.contextmanager
def _locked_state(workspace: pathlib.Path, plugin_data: pathlib.Path):
    key = hashlib.sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()
    lock_path = plugin_data / "state-locks" / f"{key}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def initialize_agent_registry(
    workspace: pathlib.Path, session_id: str, plugin_data: pathlib.Path
) -> None:
    with _locked_registry(workspace, session_id, plugin_data) as path:
        if not path.exists() and not path.is_symlink():
            _atomic_write_json(path, {"schema_version": 1, "active_agent_ids": []})
            return
        _validate_agent_registry(json.loads(path.read_text()))


def _validate_agent_registry(document: object) -> set[str]:
    if not isinstance(document, dict):
        raise ValueError("active-agent registry must contain an object")
    schema_version = document.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
    ):
        raise ValueError("active-agent registry has an unsupported schema_version")
    values = document["active_agent_ids"]
    if (
        not isinstance(values, list)
        or not all(isinstance(value, str) and value for value in values)
        or len(values) != len(set(values))
    ):
        raise ValueError("active_agent_ids must be a unique non-empty string list")
    return set(values)


def _mutate_agent_registry(
    workspace: pathlib.Path,
    session_id: str,
    plugin_data: pathlib.Path,
    *,
    add: str | None = None,
    remove: str | None = None,
) -> tuple[set[str], str | None]:
    if not _valid_session_id(session_id):
        return set(), "active-agent registry unavailable: invalid session_id"
    try:
        with _locked_registry(workspace, session_id, plugin_data) as path:
            if not path.exists() and not path.is_symlink() and add is not None:
                document = {"schema_version": 1, "active_agent_ids": []}
            else:
                document = json.loads(path.read_text())
            active = _validate_agent_registry(document)
            if remove is not None and remove not in active:
                return (
                    active,
                    f"active-agent registry cleanup failed: unknown agent_id {remove}",
                )
            if add is not None:
                active.add(add)
            if remove is not None:
                active.discard(remove)
            if add is not None or remove is not None:
                _atomic_write_json(
                    path, {"schema_version": 1, "active_agent_ids": sorted(active)}
                )
            return active, None
    except (OSError, UnicodeError, KeyError, TypeError, ValueError) as exc:
        return set(), f"active-agent registry unavailable: {exc}"


def active_agent_ids(
    workspace: pathlib.Path, session_id: str, plugin_data: pathlib.Path | None
) -> tuple[set[str], str | None]:
    if plugin_data is None:
        return set(), "active-agent registry unavailable: PLUGIN_DATA is not configured"
    return _mutate_agent_registry(workspace, session_id, plugin_data)


def extract_paths(tool_name: str, tool_input: dict[str, object]) -> list[str]:
    """Return all explicit file targets for Write/Edit/apply_patch; preserve order and deduplicate."""
    if not isinstance(tool_input, dict):
        return []

    paths: list[str] = []
    if tool_name in {"Write", "Edit"}:
        file_path = tool_input.get("file_path")
        if isinstance(file_path, str) and file_path:
            paths.append(file_path)
    elif tool_name == "apply_patch":
        patch = tool_input.get("command")
        if not isinstance(patch, str):
            patch = tool_input.get("patch")
        if isinstance(patch, str):
            for line in patch.splitlines():
                match = PATCH_PATH.match(line) or MOVE_PATH.match(line)
                if match:
                    paths.append(match.group(1))

    return list(dict.fromkeys(paths))


def _active_workspace(payload: dict[str, object]) -> pathlib.Path | None:
    def nearest(start: pathlib.Path) -> pathlib.Path | None:
        try:
            current = start.resolve(strict=False)
            if not current.is_dir():
                current = current.parent
            while True:
                if (
                    (current / MARKER).is_file()
                    and (current / "state.json").is_file()
                ):
                    return current
                if current.parent == current:
                    return None
                current = current.parent
        except (OSError, RuntimeError, TypeError, ValueError):
            return None

    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return nearest(pathlib.Path(cwd))

    try:
        return nearest(pathlib.Path.cwd())
    except (OSError, ValueError):
        return None


def _is_canonical_journal_target(workspace: pathlib.Path, raw_path: str) -> bool:
    """Recognize the current run journal after canonicalizing existing parents."""
    if not raw_path or any(character in raw_path for character in ("\x00", "\n", "\r")):
        return False
    try:
        state = json.loads((workspace / "state.json").read_text())
        if not isinstance(state, dict):
            return False
        date = state.get("date")
        if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            return False
        workspace = workspace.resolve(strict=True)
        target = pathlib.Path(raw_path)
        if ".." in target.parts:
            return False
        target = target if target.is_absolute() else workspace / target
        expected = workspace / "output" / f"agent-conversation-{date}.md"
        return target.resolve(strict=False) == expected.resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _blocked(message: str) -> HookResult:
    return HookResult(2, stderr=f"BLOCKED: {message}\n")


def _hook_context(event: str, message: str, *, warning: str | None = None) -> HookResult:
    output: dict[str, object] = {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": message,
        }
    }
    if warning:
        output["systemMessage"] = warning
    return HookResult(0, stdout=json.dumps(output))


def _lifecycle_failure(event: str, marker: str, detail: str) -> HookResult:
    message = (
        f"{marker} reason={detail}. Return exactly this failure marker to the parent; "
        "do not use tools, write files, or perform phase work."
    )
    return _hook_context(event, message, warning=message)


def _plugin_data_root(plugin_data: pathlib.Path | None) -> pathlib.Path | None:
    if plugin_data is not None:
        return pathlib.Path(plugin_data)
    value = os.environ.get("PLUGIN_DATA")
    return pathlib.Path(value) if value else None


def _model_audit_path(
    workspace: pathlib.Path, session_id: str, plugin_data: pathlib.Path
) -> pathlib.Path:
    if not _valid_session_id(session_id):
        raise ValueError("invalid session_id")
    key = hashlib.sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()
    return plugin_data / "model-selections" / key / f"{session_id}.json"


def _observe_parent_model(
    workspace: pathlib.Path,
    payload: dict[str, object],
    plugin_data: pathlib.Path | None,
    source: str,
) -> str | None:
    """Best-effort audit of a parent-selected model; never block hook execution."""
    model = payload.get("model")
    session_id = payload.get("session_id")
    turn_id = payload.get("turn_id")
    if (
        not isinstance(model, str)
        or not model
        or not _valid_session_id(session_id)
        or source not in {"SessionStart", "UserPromptSubmit"}
        or plugin_data is None
    ):
        return None
    try:
        with _locked_state(workspace, plugin_data):
            path = _model_audit_path(workspace, session_id, plugin_data)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                document = json.loads(path.read_text())
                schema_version = document.get("schema_version") if isinstance(document, dict) else None
                if (
                    not isinstance(document, dict)
                    or isinstance(schema_version, bool)
                    or not isinstance(schema_version, int)
                    or schema_version != 1
                    or document.get("model_policy") != "user-selected"
                ):
                    return None
            else:
                document = {}
            previous_model = document.get("current_model")
            if not isinstance(previous_model, str) or not previous_model:
                previous_model = None
            history: list[dict[str, object]] = []
            existing_history = document.get("history")
            if isinstance(existing_history, list):
                for entry in existing_history:
                    if not isinstance(entry, dict):
                        continue
                    entry_model = entry.get("model")
                    entry_source = entry.get("source")
                    selected_at = entry.get("selected_at")
                    if (
                        not isinstance(entry_model, str)
                        or not entry_model
                        or not _valid_session_id(entry.get("session_id"))
                        or entry_source not in {"SessionStart", "UserPromptSubmit"}
                        or not isinstance(selected_at, str)
                        or not selected_at
                    ):
                        continue
                    previous = entry.get("previous_model")
                    entry_turn = entry.get("turn_id")
                    if previous is not None and (
                        not isinstance(previous, str) or not previous
                    ):
                        continue
                    if entry_turn is not None and (
                        not isinstance(entry_turn, str) or not entry_turn
                    ):
                        continue
                    preserved = {
                        key: entry[key]
                        for key in (
                            "model",
                            "previous_model",
                            "session_id",
                            "turn_id",
                            "source",
                            "selected_at",
                        )
                        if key in entry
                    }
                    history.append(preserved)
            if model != previous_model:
                observed: dict[str, object] = {
                    "model": model,
                    "session_id": session_id,
                    "source": source,
                    "selected_at": _utc_now(),
                }
                if isinstance(previous_model, str) and previous_model:
                    observed["previous_model"] = previous_model
                if isinstance(turn_id, str) and turn_id:
                    observed["turn_id"] = turn_id
                history.append(observed)
            _atomic_write_json(
                path,
                {
                    "schema_version": 1,
                    "model_policy": "user-selected",
                    "current_model": model,
                    "history": history,
                },
            )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return f"NIGHTFALCON_MODEL_SELECTED model={model}"


def _write_session_model_pin(
    payload: dict[str, object], plugin_data: pathlib.Path | None
) -> pathlib.Path | None:
    model = payload.get("model")
    session_id = payload.get("session_id")
    root = _plugin_data_root(plugin_data)
    if (
        not isinstance(model, str)
        or not model
        or not isinstance(session_id, str)
        or not SAFE_SESSION_ID.fullmatch(session_id)
        or root is None
    ):
        return None

    directory = root / "model-pins"
    path = directory / f"{session_id}.json"
    temporary = directory / f".{session_id}.{os.getpid()}.tmp"
    document = {
        "schema_version": "1",
        "session_id": session_id,
        "model": model,
        "source": "SessionStart",
    }
    try:
        directory.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(document, indent=2) + "\n")
        temporary.replace(path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return path


def _gate_script(plugin_root: pathlib.Path | None) -> pathlib.Path | None:
    root: object = plugin_root
    if root is None:
        root = os.environ.get("PLUGIN_ROOT")
    if not isinstance(root, (str, os.PathLike)) or not root:
        return None

    try:
        gate = pathlib.Path(root) / "scripts" / "check-gate.sh"
    except (TypeError, ValueError):
        return None
    return gate if gate.is_file() else None


def _run_gate(
    gate: pathlib.Path,
    action: str,
    workspace: pathlib.Path,
    value: str | None = None,
) -> HookResult:
    command = ["bash", str(gate), action]
    if value is not None:
        command.append(value)
    command.extend(["--workspace", str(workspace)])

    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return _blocked(f"could not execute check-gate.sh: {exc}")
    return HookResult(completed.returncode, completed.stdout, completed.stderr)


def handle(
    payload: dict[str, object],
    *,
    plugin_root: pathlib.Path | None = None,
    plugin_data: pathlib.Path | None = None,
) -> HookResult:
    """Normalize Codex event, stay inert outside marked workspaces, and delegate active checks to check-gate.sh."""
    if not isinstance(payload, dict):
        workspace = _active_workspace({})
        if workspace is None:
            return HookResult(0)
        return _blocked("hook payload must be a JSON object")

    event = payload.get("hook_event_name")
    if event == "SessionStart":
        model = payload.get("model")
        workspace = _active_workspace(payload)
        root = _plugin_data_root(plugin_data)
        if workspace is not None:
            source = payload.get("source")
            session_id = payload.get("session_id")
            if (
                source in {"startup", "resume"}
                and _valid_session_id(session_id)
                and root is not None
            ):
                try:
                    initialize_agent_registry(workspace, session_id, root)
                except (OSError, UnicodeError, KeyError, TypeError, ValueError) as exc:
                    return _blocked(f"active-agent registry unavailable: {exc}")

        pin = _write_session_model_pin(payload, plugin_data)
        if workspace is not None:
            _observe_parent_model(workspace, payload, root, "SessionStart")
        session_id = payload.get("session_id")
        model_label = model if isinstance(model, str) and model else "unavailable"
        pin_label = str(pin) if pin is not None else "unavailable"
        return _hook_context(
            "SessionStart",
            f"NIGHTFALCON_MODEL_SELECTED model={model_label} session_id={session_id} "
            f"pin_file={pin_label}. Required configuration: "
            "[agents] max_depth = 5; recommended max_threads = 8.",
        )

    workspace = _active_workspace(payload)
    if workspace is None:
        return HookResult(0)

    if not isinstance(event, str) or not event:
        return _blocked("hook payload is missing hook_event_name")

    if event == "UserPromptSubmit":
        context = _observe_parent_model(
            workspace,
            payload,
            _plugin_data_root(plugin_data),
            "UserPromptSubmit",
        )
        if context is not None:
            return _hook_context("UserPromptSubmit", context)
        return HookResult(0)

    if event == "SubagentStart":
        agent_id = payload.get("agent_id")
        session_id = payload.get("session_id")
        root = _plugin_data_root(plugin_data)
        if not isinstance(agent_id, str) or not agent_id:
            return _lifecycle_failure(
                "SubagentStart",
                "NIGHTFALCON_CHILD_REGISTRATION_FAILED",
                "missing agent_id",
            )
        if not _valid_session_id(session_id):
            return _lifecycle_failure(
                "SubagentStart",
                "NIGHTFALCON_CHILD_REGISTRATION_FAILED",
                "invalid session_id",
            )
        if root is None:
            return _lifecycle_failure(
                "SubagentStart",
                "NIGHTFALCON_CHILD_REGISTRATION_FAILED",
                "PLUGIN_DATA is not configured",
            )
        try:
            with _locked_state(workspace, root):
                _, registry_error = _mutate_agent_registry(
                    workspace, session_id, root, add=agent_id
                )
                if registry_error:
                    return _lifecycle_failure(
                        "SubagentStart",
                        "NIGHTFALCON_CHILD_REGISTRATION_FAILED",
                        registry_error,
                    )
        except (OSError, ValueError) as exc:
            return _lifecycle_failure(
                "SubagentStart",
                "NIGHTFALCON_CHILD_REGISTRATION_FAILED",
                f"state lock unavailable: {exc}",
            )
        return HookResult(0)

    if event == "SubagentStop":
        agent_id = payload.get("agent_id")
        session_id = payload.get("session_id")
        root = _plugin_data_root(plugin_data)
        if not isinstance(agent_id, str) or not agent_id:
            return _lifecycle_failure(
                "SubagentStop",
                "NIGHTFALCON_ACTIVE_AGENT_UNREGISTER_FAILED",
                "missing agent_id",
            )
        if not _valid_session_id(session_id):
            return _lifecycle_failure(
                "SubagentStop",
                "NIGHTFALCON_ACTIVE_AGENT_UNREGISTER_FAILED",
                "invalid session_id",
            )
        if root is None:
            return _lifecycle_failure(
                "SubagentStop",
                "NIGHTFALCON_ACTIVE_AGENT_UNREGISTER_FAILED",
                "PLUGIN_DATA is not configured",
            )
        try:
            with _locked_state(workspace, root):
                _, registry_error = _mutate_agent_registry(
                    workspace, session_id, root, remove=agent_id
                )
                if registry_error:
                    return _lifecycle_failure(
                        "SubagentStop",
                        "NIGHTFALCON_ACTIVE_AGENT_UNREGISTER_FAILED",
                        registry_error,
                    )
        except (OSError, ValueError) as exc:
            return _lifecycle_failure(
                "SubagentStop",
                "NIGHTFALCON_ACTIVE_AGENT_UNREGISTER_FAILED",
                f"state lock unavailable: {exc}",
            )
        return _hook_context(
            "SubagentStop", f"NIGHTFALCON_ACTIVE_AGENT_UNREGISTERED agent_id={agent_id}."
        )

    if event not in {"Stop", "PreToolUse"}:
        return HookResult(0)

    if event == "PreToolUse":
        tool_name = payload.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            return _blocked("PreToolUse payload is missing tool_name")
        if tool_name not in {"apply_patch", "Edit", "Write"}:
            return HookResult(0)

        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            return _blocked(f"PreToolUse {tool_name} payload requires an object tool_input")

        paths = extract_paths(tool_name, tool_input)
        if not paths:
            return _blocked("could not determine target path from PreToolUse payload")

        gate = _gate_script(plugin_root)
        if gate is None:
            return _blocked("NightFalcon plugin root is missing scripts/check-gate.sh")

        for path in paths:
            if _is_canonical_journal_target(workspace, path):
                return _blocked(
                    "direct agent conversation journal edits bypass hash-chain integrity "
                    "validation; use scripts/agent-journal.py record, then run "
                    "scripts/agent-journal.py validate before continuing"
                )
            result = _run_gate(gate, "--check-path", workspace, path)
            if result.exit_code != 0:
                return result
        return HookResult(0)

    gate = _gate_script(plugin_root)
    if gate is None:
        return _blocked("NightFalcon plugin root is missing scripts/check-gate.sh")
    return _run_gate(gate, "--enforce-output", workspace)


def main() -> int:
    """Read one JSON object from stdin, emit result streams, and return HookResult.exit_code."""
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, UnicodeError) as exc:
        if _active_workspace({}) is None:
            result = HookResult(0)
        else:
            result = _blocked(f"malformed hook JSON input: {exc}")
    else:
        if not isinstance(payload, dict):
            if _active_workspace({}) is None:
                result = HookResult(0)
            else:
                result = _blocked("malformed hook JSON input: expected an object")
        else:
            result = handle(payload)

    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
