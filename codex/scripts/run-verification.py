#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence


# Disable local-module bytecode before importing the distribution helper. The
# later compile command cannot prevent import-time cache pollution.
sys.dont_write_bytecode = True

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from distribution_manifest import ManifestError, build_manifest


BASE_REVISION = "86015f536797e2ed7168f1cd1c2a0dbdaf35bf83"
PLUGIN_ID = "nightfalcon@nightfalcon-open"
RESULTS_SCHEMA_VERSION = 2
SOURCE_EXCLUDES = ("verification-results.json", "docs/verification-report.md")
COMBINED_SOURCE_EXCLUDES = tuple(
    f"codex/{relative}" for relative in SOURCE_EXCLUDES
)
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
RUNNER_TEMP_MARKER = ".nightfalcon-verification-root"
RUNNER_TEMP_MARKER_CONTENT = "nightfalcon-verify-port-v1\n"
ISOLATED_CODEX_HOME_LABEL = "<VERIFY_TEMP_ROOT>/codex-home"
_CREDENTIAL_NAME = re.compile(
    r"(?:^|_)(?:TOKEN|PASSWORD|PASSWD|SECRET|API_KEY|ACCESS_KEY(?:_ID)?|"
    r"PRIVATE_KEY|PAT|AUTH|AUTHORIZATION|CREDENTIALS?|COOKIE)(?:_|$)",
    re.IGNORECASE,
)
KNOWN_LIMITATIONS = [
    "PASS: Current Claude Code, Codex CLI, and Cursor Agent package discovery is documented in docs/verification/2026-08-26-client-canaries.md.",
    "TARGET-DEPENDENT: Full phase-0 through phase-8 execution against an external repository is operational validation, not a package-release prerequisite; it requires an operator-selected target, provider authentication, and authorized scope.",
    "LIMITATION: Deterministic tests cover user-selected model policy and hook contracts; package evidence does not claim live UI persistence or live multi-phase model-transition behavior.",
    "PASS-WITH-DELTA: NightFalcon leaves spawn model and reasoning overrides unset, records initial and passive advisory provenance, and never rejects, retries, or reroutes work because an observed model differs.",
]


class VerificationError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class VerificationScope:
    allowed_files: frozenset[str]
    allowed_directories: tuple[str, ...]
    legacy_deletion_exceptions: frozenset[str]
    legacy_rename_source_files: frozenset[str]
    legacy_rename_source_directories: tuple[str, ...]

    def allows(self, path: str) -> bool:
        return path in self.allowed_files or any(
            path.startswith(directory + "/") for directory in self.allowed_directories
        )

    def allows_legacy_rename_source(self, path: str) -> bool:
        return path in self.legacy_rename_source_files or any(
            path.startswith(directory + "/")
            for directory in self.legacy_rename_source_directories
        )


def load_verification_scope(path: pathlib.Path) -> VerificationScope:
    def reject_duplicate_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
        document: dict[str, object] = {}
        for key, value in pairs:
            if key in document:
                raise VerificationError(
                    f"verification scope contains duplicate JSON member: {key}"
                )
            document[key] = value
        return document

    try:
        document = json.loads(
            path.read_text(), object_pairs_hook=reject_duplicate_members
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"Cannot read verification scope: {path}") from exc
    expected_keys = {
        "schema_version",
        "allowed_files",
        "allowed_directories",
        "legacy_deletion_exceptions",
        "legacy_rename_source_files",
        "legacy_rename_source_directories",
    }
    if not isinstance(document, dict) or set(document) != expected_keys:
        raise VerificationError(
            "verification scope must contain only schema_version, allowed_files, "
            "allowed_directories, legacy_deletion_exceptions, "
            "legacy_rename_source_files, and legacy_rename_source_directories"
        )
    schema_version = document["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
    ):
        raise VerificationError("verification scope must use schema_version 1")
    allowed_files = document["allowed_files"]
    allowed_directories = document["allowed_directories"]
    legacy_deletion_exceptions = document["legacy_deletion_exceptions"]
    legacy_rename_source_files = document["legacy_rename_source_files"]
    legacy_rename_source_directories = document[
        "legacy_rename_source_directories"
    ]
    if (
        not isinstance(allowed_files, list)
        or not allowed_files
        or not all(isinstance(value, str) and value for value in allowed_files)
        or not isinstance(allowed_directories, list)
        or not allowed_directories
        or not all(isinstance(value, str) and value for value in allowed_directories)
        or not isinstance(legacy_deletion_exceptions, list)
        or not all(
            isinstance(value, str) and value
            for value in legacy_deletion_exceptions
        )
        or not isinstance(legacy_rename_source_files, list)
        or not all(
            isinstance(value, str) and value
            for value in legacy_rename_source_files
        )
        or not isinstance(legacy_rename_source_directories, list)
        or not all(
            isinstance(value, str) and value
            for value in legacy_rename_source_directories
        )
    ):
        raise VerificationError(
            "verification scope paths must be non-empty string arrays"
        )

    def validate_path(value: str) -> str:
        normalized = pathlib.PurePosixPath(value)
        if (
            "\\" in value
            or normalized.is_absolute()
            or normalized.as_posix() != value
            or any(part in {".", ".."} for part in normalized.parts)
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise VerificationError(
                f"verification scope path is not normalized repository-relative POSIX: {value!r}"
            )
        return value

    files = [validate_path(value) for value in allowed_files]
    directories = [validate_path(value) for value in allowed_directories]
    legacy_deletions = [validate_path(value) for value in legacy_deletion_exceptions]
    legacy_rename_files = [
        validate_path(value) for value in legacy_rename_source_files
    ]
    legacy_rename_directories = [
        validate_path(value) for value in legacy_rename_source_directories
    ]
    if (
        len(files) != len(set(files))
        or len(directories) != len(set(directories))
        or len(legacy_deletions) != len(set(legacy_deletions))
        or len(legacy_rename_files) != len(set(legacy_rename_files))
        or len(legacy_rename_directories) != len(set(legacy_rename_directories))
    ):
        raise VerificationError("verification scope contains duplicate paths")
    for file_path in files:
        if any(
            file_path == directory or file_path.startswith(directory + "/")
            for directory in directories
        ):
            raise VerificationError(
                f"verification scope file overlaps a directory rule: {file_path}"
            )
    for index, directory in enumerate(directories):
        for other in directories[index + 1 :]:
            if directory.startswith(other + "/") or other.startswith(directory + "/"):
                raise VerificationError(
                    f"verification scope contains overlapping directories: {directory}, {other}"
                )
    allowed_scope = VerificationScope(
        frozenset(files), tuple(directories), frozenset(), frozenset(), tuple()
    )
    for legacy_path in legacy_deletions:
        if allowed_scope.allows(legacy_path):
            raise VerificationError(
                f"legacy deletion exception overlaps allowed scope: {legacy_path}"
            )
    for source_path in legacy_rename_files:
        if allowed_scope.allows(source_path):
            raise VerificationError(
                f"legacy rename source overlaps allowed scope: {source_path}"
            )
        if any(
            source_path == directory or source_path.startswith(directory + "/")
            for directory in legacy_rename_directories
        ):
            raise VerificationError(
                f"legacy rename source file overlaps a directory rule: {source_path}"
            )
    for index, directory in enumerate(legacy_rename_directories):
        if allowed_scope.allows(directory):
            raise VerificationError(
                f"legacy rename source directory overlaps allowed scope: {directory}"
            )
        for other in legacy_rename_directories[index + 1 :]:
            if directory.startswith(other + "/") or other.startswith(directory + "/"):
                raise VerificationError(
                    "verification scope contains overlapping legacy rename directories: "
                    f"{directory}, {other}"
                )
    return VerificationScope(
        frozenset(files),
        tuple(directories),
        frozenset(legacy_deletions),
        frozenset(legacy_rename_files),
        tuple(legacy_rename_directories),
    )


def _is_credential_name(value: str) -> bool:
    canonical = re.sub(r"[^A-Za-z0-9]+", "_", value.strip("-")).strip("_")
    return bool(canonical and _CREDENTIAL_NAME.search(canonical))


def validate_runner_temp_home(
    runner_root: pathlib.Path, codex_home: pathlib.Path
) -> pathlib.Path:
    root = pathlib.Path(runner_root)
    home = pathlib.Path(codex_home)
    if not root.is_absolute() or not home.is_absolute():
        raise VerificationError("runner temporary paths must be absolute")
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise VerificationError("runner temporary root is unavailable") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        raise VerificationError("runner temporary root must be a real directory")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise VerificationError("runner temporary root cannot be resolved") from exc
    if resolved_root != root:
        raise VerificationError("runner temporary root contains a symlink component")
    if root_stat.st_uid != os.getuid() or stat.S_IMODE(root_stat.st_mode) & 0o077:
        raise VerificationError("runner temporary root is not privately owned")
    marker = root / RUNNER_TEMP_MARKER
    try:
        marker_stat = marker.lstat()
        marker_content = marker.read_text()
    except OSError as exc:
        raise VerificationError("runner temporary root has no ownership marker") from exc
    if (
        not stat.S_ISREG(marker_stat.st_mode)
        or stat.S_ISLNK(marker_stat.st_mode)
        or marker_stat.st_uid != os.getuid()
        or marker_content != RUNNER_TEMP_MARKER_CONTENT
    ):
        raise VerificationError("runner temporary ownership marker is invalid")
    expected_home = root / "codex-home"
    if home != expected_home:
        raise VerificationError("temporary CODEX_HOME must be the exact child of runner root")
    try:
        home_stat = home.lstat()
    except OSError as exc:
        raise VerificationError("temporary CODEX_HOME is unavailable") from exc
    if (
        not stat.S_ISDIR(home_stat.st_mode)
        or stat.S_ISLNK(home_stat.st_mode)
        or home_stat.st_uid != os.getuid()
        or stat.S_IMODE(home_stat.st_mode) & 0o077
    ):
        raise VerificationError("temporary CODEX_HOME must be a private real directory")
    if any(home.iterdir()):
        raise VerificationError("temporary CODEX_HOME must start empty")
    return home


@dataclasses.dataclass(frozen=True)
class Command:
    name: str
    args: tuple[str, ...]
    cwd: pathlib.Path | None = None
    env: Mapping[str, str] | None = None
    required_stdout: str | None = None


def _canonical_json(document: object) -> bytes:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _combined_source_manifest(
    repository: pathlib.Path,
    scope: VerificationScope,
) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    excluded = set(COMBINED_SOURCE_EXCLUDES)
    for relative in sorted(
        scope.allowed_files, key=lambda value: value.encode("utf-8")
    ):
        path = repository / relative
        if relative in excluded:
            continue
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ManifestError(
                f"required release-scope file is missing: {relative}"
            ) from exc
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise ManifestError(
                f"required release-scope file is not a regular file: {relative}"
            )
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ManifestError(
                f"cannot read release-scope file: {relative}"
            ) from exc
        entries.append(
            {
                "path": relative,
                "type": "file",
                "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )

    for relative in sorted(
        scope.allowed_directories, key=lambda value: value.encode("utf-8")
    ):
        directory = repository / relative
        if directory.is_symlink() or not directory.is_dir():
            raise ManifestError(
                f"required release-scope directory is missing or unsafe: {relative}"
            )
        prefix = relative + "/"
        directory_excludes = [
            path[len(prefix) :]
            for path in COMBINED_SOURCE_EXCLUDES
            if path.startswith(prefix)
        ]
        manifest = build_manifest(directory, exclude_paths=directory_excludes)
        for entry in manifest["entries"]:
            entries.append({**entry, "path": f"{relative}/{entry['path']}"})

    entries.sort(key=lambda entry: str(entry["path"]).encode("utf-8"))
    subject = {
        "format_version": 1,
        "scope_kind": "combined-release",
        "entries": entries,
    }
    return {
        **subject,
        "file_count": len(entries),
        "total_bytes": sum(
            int(entry.get("size", len(str(entry.get("target", "")).encode("utf-8"))))
            for entry in entries
        ),
        "digest": hashlib.sha256(_canonical_json(subject)).hexdigest(),
    }


def source_manifest(wrapper: pathlib.Path) -> dict[str, object]:
    wrapper = wrapper.resolve()
    repository = _git_toplevel(wrapper)
    if repository is None:
        raise VerificationError(
            "verification source root is not an authenticated Git worktree"
        )
    if _is_authenticated_standalone_root(wrapper, repository):
        return {
            **build_manifest(wrapper, exclude_paths=SOURCE_EXCLUDES),
            "scope_kind": "standalone-wrapper",
        }
    expected_wrapper = repository / "codex"
    if wrapper != expected_wrapper.resolve():
        raise VerificationError(
            "canonical verification wrapper is not repository/codex"
        )
    scope_path = wrapper / "verification-scope.json"
    if scope_path.is_symlink() or not scope_path.is_file():
        raise VerificationError("canonical verification scope is missing or unsafe")
    return _combined_source_manifest(
        repository,
        load_verification_scope(scope_path),
    )


def matrix_digest(command_results: Sequence[Mapping[str, object]]) -> str:
    commands: list[dict[str, object]] = []
    for command in command_results:
        name = command.get("name")
        arguments = command.get("command")
        cwd = command.get("cwd")
        environment = command.get("env")
        required_stdout = command.get("required_stdout")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(arguments, list)
            or not all(isinstance(value, str) for value in arguments)
            or (cwd is not None and not isinstance(cwd, str))
            or not isinstance(environment, dict)
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in environment.items()
            )
            or (required_stdout is not None and not isinstance(required_stdout, str))
        ):
            raise VerificationError("command evidence cannot form a deterministic matrix")
        commands.append(
            {
                "name": name,
                "command": arguments,
                "cwd": cwd,
                "env": environment,
                "required_stdout": required_stdout,
            }
        )
    subject = {"format_version": 2, "commands": commands}
    return hashlib.sha256(_canonical_json(subject)).hexdigest()


def build_run_record(
    *,
    run_id: str,
    timestamp: str,
    source_manifest: Mapping[str, object],
    matrix_kind: str,
    command_results: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    commands = [dict(command) for command in command_results]
    passed = bool(commands) and all(
        command.get("passed") is True
        and isinstance(command.get("exit_code"), int)
        and not isinstance(command.get("exit_code"), bool)
        and command.get("exit_code") == 0
        for command in commands
    )
    return {
        "run_id": run_id,
        "timestamp_utc": timestamp,
        "status": "PASS" if passed else "FAIL",
        "source_digest": source_manifest["digest"],
        "source_file_count": source_manifest["file_count"],
        "matrix_kind": matrix_kind,
        "matrix_digest": matrix_digest(commands),
        "command_names": [command["name"] for command in commands],
        "commands": commands,
    }


def _resolve_executable(candidate: str) -> str | None:
    path = pathlib.Path(candidate).expanduser()
    if path.parent != pathlib.Path(".") or path.is_absolute():
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which(candidate)


def find_yaml_python(candidates: Iterable[str]) -> str:
    attempted: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        resolved = _resolve_executable(candidate)
        if not resolved or resolved in seen:
            continue
        seen.add(resolved)
        attempted.append(resolved)
        result = subprocess.run(
            [resolved, "-c", "import yaml"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return resolved
    detail = ", ".join(attempted) if attempted else "no executable candidates"
    raise VerificationError(
        "No available Python interpreter can import PyYAML; external plugin/skill "
        f"validators cannot run. Tried: {detail}. Set YAML_PYTHON to a suitable interpreter."
    )


def yaml_python_candidates() -> list[str]:
    return [
        os.environ.get("YAML_PYTHON", ""),
        "python3",
        "python3.14",
        "python3.13",
        "python3.12",
        "python3.11",
        "python3.10",
        sys.executable,
    ]


def locate_validator(
    creator_name: str,
    script_name: str,
    env_name: str,
    *,
    wrapper: pathlib.Path | None = None,
) -> pathlib.Path:
    candidates: list[pathlib.Path] = []
    configured = os.environ.get(env_name)
    if configured:
        candidates.append(pathlib.Path(configured).expanduser())
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        candidates.append(
            pathlib.Path(codex_home).expanduser()
            / "skills"
            / ".system"
            / creator_name
        )
    candidates.append(pathlib.Path.home() / ".codex" / "skills" / ".system" / creator_name)
    if wrapper is not None:
        # Self-contained distribution fallback. Environment and installed
        # system-skill paths win when explicitly provisioned, while a clean
        # standalone checkout never depends on a user's personal Codex home.
        candidates.append(pathlib.Path(wrapper) / "scripts" / "validators" / script_name)

    searched: list[str] = []
    for candidate in candidates:
        script = candidate if candidate.name == script_name else candidate / "scripts" / script_name
        searched.append(str(script))
        if script.is_file():
            return script.resolve()
    raise VerificationError(
        f"Could not locate {script_name}. Searched: {', '.join(searched)}. "
        f"Set {env_name} to the {creator_name} directory or validator script."
    )


def execute_commands(
    commands: Sequence[Command],
    *,
    replacements: Mapping[str, str] | None = None,
    stream: bool = False,
) -> list[dict[str, object]]:
    replacements = replacements or {}
    results: list[dict[str, object]] = []

    def normalize(value: str) -> str:
        for raw, replacement in sorted(
            replacements.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if not raw:
                continue
            value = value.replace(raw, replacement)
        value = re.sub(
            r"\b([A-Za-z_][A-Za-z0-9_]*)=([^\s'\";]+)",
            lambda match: (
                f"{match.group(1)}=<REDACTED>"
                if _is_credential_name(match.group(1))
                else match.group(0)
            ),
            value,
        )
        value = re.sub(
            r"(?i)\b(Authorization|Proxy-Authorization|Cookie)\s*:\s*[^\r\n]+",
            r"\1: <REDACTED>",
            value,
        )
        value = re.sub(
            r"([A-Za-z][A-Za-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@",
            r"\1<REDACTED>@",
            value,
        )
        return value

    def normalized_arguments(arguments: Sequence[str]) -> list[str]:
        normalized: list[str] = []
        redact_next = False
        for argument in arguments:
            if redact_next:
                normalized.append("<REDACTED>")
                redact_next = False
                continue
            option, separator, _ = argument.partition("=")
            if argument.startswith("-") and _is_credential_name(option):
                normalized.append(
                    f"{normalize(option)}=<REDACTED>" if separator else normalize(option)
                )
                redact_next = not separator
                continue
            normalized.append(normalize(argument))
        return normalized

    def output_metadata(value: str) -> tuple[int, str]:
        normalized = normalize(value)
        encoded = normalized.encode()
        return len(encoded), hashlib.sha256(encoded).hexdigest()

    def normalized_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, value in sorted((environment or {}).items()):
            if _is_credential_name(key):
                normalized[key] = "<REDACTED>"
            else:
                normalized[key] = normalize(value)
        return normalized

    for command in commands:
        evidence_arguments = normalized_arguments(command.args)
        if stream:
            print(f"\n==> {command.name}: {shlex.join(evidence_arguments)}", flush=True)
        env = os.environ.copy()
        if command.env:
            env.update(command.env)
        completed = subprocess.run(
            command.args,
            cwd=command.cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        exit_code = completed.returncode
        stderr = completed.stderr
        if (
            exit_code == 0
            and command.required_stdout
            and command.required_stdout not in completed.stdout
        ):
            exit_code = 1
            stderr += (
                f"Expected output to contain {command.required_stdout!r}, but it did not.\n"
            )
        if stream:
            if completed.stdout:
                print(completed.stdout, end="")
            if stderr:
                print(stderr, end="", file=sys.stderr)
        stdout_bytes, stdout_sha256 = output_metadata(completed.stdout)
        stderr_bytes, stderr_sha256 = output_metadata(stderr)
        result = {
            "name": command.name,
            "command": evidence_arguments,
            "cwd": normalize(str(command.cwd)) if command.cwd else None,
            "env": normalized_environment(command.env),
            "required_stdout": (
                normalize(command.required_stdout) if command.required_stdout else None
            ),
            "exit_code": exit_code,
            "passed": exit_code == 0,
            "stdout_bytes": stdout_bytes,
            "stdout_sha256": stdout_sha256,
            "stderr_bytes": stderr_bytes,
            "stderr_sha256": stderr_sha256,
        }
        if exit_code != 0:
            result["failure_detail"] = "Command failed; inspect the console output."
        results.append(result)
        if exit_code != 0:
            break
    return results


def _git_toplevel(path: pathlib.Path) -> pathlib.Path | None:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return pathlib.Path(result.stdout.strip()).resolve()


def _has_enclosing_git_marker(wrapper: pathlib.Path) -> bool:
    for ancestor in wrapper.parents:
        marker = ancestor / ".git"
        if marker.exists() or marker.is_symlink():
            return True
    return False


def _is_authenticated_standalone_root(
    wrapper: pathlib.Path, repo: pathlib.Path
) -> bool:
    wrapper = wrapper.resolve()
    return (
        wrapper == SCRIPT_DIR.parent.resolve()
        and repo.resolve() == wrapper
        and not _has_enclosing_git_marker(wrapper)
    )


def build_core_commands(
    *,
    wrapper: pathlib.Path,
    python_bin: str,
    yaml_python: str,
    plugin_validator: pathlib.Path,
    skill_validator: pathlib.Path,
    inventory_snapshot: pathlib.Path,
) -> list[Command]:
    wrapper = wrapper.resolve()
    plugin = wrapper / "plugins" / "nightfalcon"
    runner = wrapper / "scripts" / "run-verification.py"
    shell_check = (
        'set -euo pipefail; while IFS= read -r -d "" file; do '
        'bash -n "$file"; done < <(find "$1" -type f -name "*.sh" -print0)'
    )
    json_check = (
        'set -euo pipefail; while IFS= read -r -d "" file; do '
        '"$2" -m json.tool "$file" >/dev/null; '
        'done < <(find "$1" -type f -name "*.json" -print0)'
    )
    scope_args = (
        python_bin,
        str(runner),
        "--internal-scope-check",
        str(wrapper),
        BASE_REVISION,
    )
    repo = _git_toplevel(wrapper)
    if repo is not None and _is_authenticated_standalone_root(wrapper, repo):
        scope_args += ("--allow-standalone-without-base",)
    return [
        Command(
            "wrapper-tests",
            (
                python_bin,
                "-m",
                "unittest",
                "discover",
                "-s",
                str(wrapper / "tests"),
                "-p",
                "test_*.py",
                "-v",
            ),
            cwd=wrapper,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
        ),
        Command(
            "plugin-tests",
            (
                python_bin,
                "-m",
                "unittest",
                "discover",
                "-s",
                str(plugin / "tests"),
                "-p",
                "test_*.py",
                "-v",
            ),
            cwd=wrapper,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
        ),
        Command(
            "plugin-validator",
            (yaml_python, str(plugin_validator), str(plugin)),
            cwd=wrapper,
        ),
        Command(
            "skill-validator",
            (yaml_python, str(skill_validator), str(plugin / "skills" / "nightfalcon")),
            cwd=wrapper,
        ),
        Command(
            "shell-syntax",
            ("bash", "-c", shell_check, "verify-shells", str(plugin / "scripts")),
            cwd=wrapper,
        ),
        Command(
            "python-compile",
            (
                python_bin,
                "-m",
                "compileall",
                "-q",
                str(plugin / "scripts"),
                str(plugin / "hooks"),
            ),
            cwd=wrapper,
            env={
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPYCACHEPREFIX": str(inventory_snapshot.parent / "python-bytecode"),
            },
        ),
        Command(
            "json-validation",
            (
                "bash",
                "-c",
                json_check,
                "verify-json",
                str(plugin),
                python_bin,
            ),
            cwd=wrapper,
        ),
        Command(
            "inventory-regeneration",
            (python_bin, str(wrapper / "scripts" / "build-port-inventory.py")),
            cwd=wrapper,
        ),
        Command(
            "inventory-stability",
            (
                python_bin,
                str(runner),
                "--internal-file-equals",
                str(inventory_snapshot),
                str(wrapper / "migration-map.json"),
            ),
            cwd=wrapper,
        ),
        Command(
            "port-scope",
            scope_args,
            cwd=wrapper,
        ),
    ]


def build_codex_smoke_commands(
    wrapper: pathlib.Path,
    codex_home: pathlib.Path,
    *,
    codex_bin: str,
) -> list[Command]:
    env = {"CODEX_HOME": str(codex_home)}
    return [
        Command(
            "codex-marketplace-add",
            (codex_bin, "plugin", "marketplace", "add", str(wrapper), "--json"),
            cwd=wrapper,
            env=env,
        ),
        Command(
            "codex-plugin-add",
            (codex_bin, "plugin", "add", PLUGIN_ID, "--json"),
            cwd=wrapper,
            env=env,
        ),
        Command(
            "codex-plugin-list",
            (codex_bin, "plugin", "list", "--json"),
            cwd=wrapper,
            env=env,
            required_stdout=PLUGIN_ID,
        ),
        Command(
            "codex-plugin-remove",
            (codex_bin, "plugin", "remove", PLUGIN_ID, "--json"),
            cwd=wrapper,
            env=env,
        ),
        Command(
            "codex-marketplace-remove",
            (codex_bin, "plugin", "marketplace", "remove", "nightfalcon-open"),
            cwd=wrapper,
            env=env,
        ),
    ]


def scope_check(
    wrapper: pathlib.Path,
    base_revision: str,
    *,
    allow_standalone_without_base: bool = False,
) -> tuple[bool, str]:
    wrapper = wrapper.resolve()
    scope = load_verification_scope(wrapper / "verification-scope.json")
    repo = _git_toplevel(wrapper)
    if repo is None:
        raise VerificationError(f"Wrapper {wrapper} is outside a Git worktree")
    if repo == wrapper and _has_enclosing_git_marker(wrapper):
        return (
            False,
            "Scope wrapper is a nested Git root inside an enclosing repository.",
        )
    try:
        wrapper.relative_to(repo)
    except ValueError as exc:
        raise VerificationError(f"Wrapper {wrapper} is outside Git root {repo}") from exc

    path_statuses: dict[str, set[str]] = {}
    rename_sources: dict[str, set[str]] = {}
    base_exists = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "rev-parse",
            "--verify",
            "--quiet",
            f"{base_revision}^{{commit}}",
        ],
        text=True,
        capture_output=True,
        check=False,
    ).returncode == 0
    standalone_without_base = (
        allow_standalone_without_base
        and _is_authenticated_standalone_root(wrapper, repo)
        and not base_exists
    )
    if not base_exists and not standalone_without_base:
        return (
            False,
            f"Configured base revision is unavailable: {base_revision}",
        )
    commands: list[list[str]] = []
    if base_exists:
        commands.append(
            [
                "git",
                "-C",
                str(repo),
                "diff",
                "--name-status",
                "--find-renames=50%",
                "-z",
                f"{base_revision}..HEAD",
            ]
        )
    commands.extend(
        [
            [
                "git",
                "-C",
                str(repo),
                "diff",
                "--name-status",
                "--find-renames=50%",
                "-z",
            ],
            [
                "git",
                "-C",
                str(repo),
                "diff",
                "--cached",
                "--name-status",
                "--find-renames=50%",
                "-z",
            ],
        ]
    )
    for command in commands:
        fields = subprocess.check_output(command).split(b"\0")
        if fields[-1] != b"":
            raise VerificationError("Git returned malformed NUL-delimited scope data")
        fields.pop()
        index = 0
        while index < len(fields):
            status = os.fsdecode(fields[index])
            index += 1
            path_count = 2 if status.startswith(("R", "C")) else 1
            if index + path_count > len(fields):
                raise VerificationError("Git returned incomplete scope status data")
            raw_paths = [
                os.fsdecode(value) for value in fields[index : index + path_count]
            ]
            index += path_count
            path = raw_paths[-1]
            path_statuses.setdefault(path, set()).add(status)
            if status.startswith("R"):
                rename_sources.setdefault(path, set()).add(raw_paths[0])

    if repo == wrapper:
        logical_paths = {
            path: f"codex/{path}" for path in path_statuses
        }
    else:
        logical_paths = {path: path for path in path_statuses}

    invalid_legacy_config: list[str] = []
    if base_exists and repo != wrapper:
        configured_legacy_paths = (
            set(scope.legacy_deletion_exceptions)
            | set(scope.legacy_rename_source_files)
            | set(scope.legacy_rename_source_directories)
        )
        for legacy_path in sorted(configured_legacy_paths):
            existed_at_base = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "cat-file",
                    "-e",
                    f"{base_revision}:{legacy_path}",
                ],
                text=True,
                capture_output=True,
                check=False,
            ).returncode == 0
            absent_at_head = subprocess.run(
                ["git", "-C", str(repo), "cat-file", "-e", f"HEAD:{legacy_path}"],
                text=True,
                capture_output=True,
                check=False,
            ).returncode != 0
            if not existed_at_base or not absent_at_head:
                invalid_legacy_config.append(legacy_path)
    if invalid_legacy_config:
        return (
            False,
            "Legacy deletion exceptions do not describe base-only paths:\n"
            + "\n".join(invalid_legacy_config),
        )

    outside: list[str] = []
    for path, logical_path in logical_paths.items():
        if scope.allows(logical_path):
            for source_path in rename_sources.get(path, set()):
                logical_source = (
                    f"codex/{source_path}"
                    if repo == wrapper
                    else source_path
                )
                if not scope.allows(
                    logical_source
                ) and not scope.allows_legacy_rename_source(logical_source):
                    outside.append(logical_source)
            continue
        if (
            repo != wrapper
            and logical_path in scope.legacy_deletion_exceptions
            and path_statuses[path] == {"D"}
        ):
            continue
        outside.append(logical_path)
    outside.sort()
    if outside:
        return (
            False,
            "Tracked repository diff escapes configured verification scope:\n"
            + "\n".join(outside),
        )
    base_note = (
        "standalone Git root (explicit missing-base opt-in)"
        if standalone_without_base
        else base_revision
    )
    return (
        True,
        f"Scope check passed for {len(path_statuses)} tracked path(s) against {base_note}.",
    )


def _timestamp() -> tuple[str, str]:
    now = dt.datetime.now(dt.timezone.utc)
    timestamp = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    run_id = now.strftime("%Y%m%dT%H%M%S.%fZ")
    return timestamp, run_id


def _setup_failure(name: str, wrapper: pathlib.Path) -> dict[str, object]:
    timestamp, run_id = _timestamp()
    return build_run_record(
        run_id=run_id,
        timestamp=timestamp,
        source_manifest=source_manifest(wrapper),
        matrix_kind="setup-failure",
        command_results=[
            {
                "name": name,
                "command": [],
                "cwd": None,
                "env": {},
                "required_stdout": None,
                "exit_code": 1,
                "passed": False,
                "stdout_bytes": 0,
                "stdout_sha256": hashlib.sha256(b"").hexdigest(),
                "stderr_bytes": 0,
                "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                "failure_detail": "Verification setup failed; inspect the console output.",
            }
        ],
    )


def _atomic_write_text(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _fallback_report(run: Mapping[str, object]) -> str:
    failures = [
        str(command.get("name", "unknown"))
        for command in run.get("commands", [])
        if isinstance(command, Mapping) and not command.get("passed")
    ]
    lines = [
        "# NightFalcon Codex Verification Report",
        "",
        "## Summary",
        "",
        "- Latest status: `FAIL`",
        f"- Latest run ID: `{run.get('run_id', 'unknown')}`",
        "- Report generation: `FAIL`",
        "",
        "## Failed checks",
        "",
    ]
    lines.extend(f"- `{name}`" for name in failures)
    lines.append("")
    return "\n".join(lines)


def _save_results(
    results_path: pathlib.Path,
    report_path: pathlib.Path,
    run: dict[str, object],
    *,
    append_run: bool,
    python_bin: str,
) -> int:
    payload: dict[str, object]
    if append_run and results_path.is_file():
        try:
            payload = json.loads(results_path.read_text())
        except (OSError, json.JSONDecodeError):
            payload = {
                "schema_version": RESULTS_SCHEMA_VERSION,
                "known_limitations": KNOWN_LIMITATIONS,
                "runs": [],
            }
    else:
        payload = {
            "schema_version": RESULTS_SCHEMA_VERSION,
            "known_limitations": KNOWN_LIMITATIONS,
            "runs": [],
        }
    payload["schema_version"] = RESULTS_SCHEMA_VERSION
    payload["known_limitations"] = KNOWN_LIMITATIONS
    runs = payload.setdefault("runs", [])
    if not isinstance(runs, list):
        runs = []
        payload["runs"] = runs
    runs.append(run)
    _atomic_write_text(results_path, json.dumps(payload, indent=2) + "\n")
    writer = results_path.parent / "scripts" / "write-verification-report.py"
    report = subprocess.run(
        [python_bin, str(writer), "--input", str(results_path), "--output", str(report_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    if report.stdout:
        print(report.stdout, end="")
    if report.stderr:
        print(report.stderr, end="", file=sys.stderr)
    if report.returncode == 0:
        return 0

    commands = run.setdefault("commands", [])
    if isinstance(commands, list):
        commands.append(
            {
                "name": "report-generation",
                "command": ["<PYTHON>", "<REPORT_WRITER>"],
                "cwd": "<WRAPPER>",
                "env": {},
                "required_stdout": None,
                "exit_code": report.returncode,
                "passed": False,
                "stdout_bytes": len(report.stdout.encode()),
                "stdout_sha256": hashlib.sha256(report.stdout.encode()).hexdigest(),
                "stderr_bytes": len(report.stderr.encode()),
                "stderr_sha256": hashlib.sha256(report.stderr.encode()).hexdigest(),
                "failure_detail": "Report generation failed; inspect the console output.",
            }
        )
        run["command_names"] = [
            command.get("name") for command in commands if isinstance(command, Mapping)
        ]
        run["matrix_digest"] = matrix_digest(
            [command for command in commands if isinstance(command, Mapping)]
        )
    run["status"] = "FAIL"
    _atomic_write_text(results_path, json.dumps(payload, indent=2) + "\n")
    _atomic_write_text(report_path, _fallback_report(run))
    return report.returncode or 1


def check_two_clean_runs(results_path: pathlib.Path) -> int:
    try:
        payload = json.loads(results_path.read_text())
        schema_version = payload["schema_version"]
        runs = payload["runs"]
        current_source = source_manifest(results_path.parent)
    except (
        OSError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ManifestError,
        VerificationError,
    ) as exc:
        print(f"BLOCKED: cannot read verification runs: {exc}", file=sys.stderr)
        return 2
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != RESULTS_SCHEMA_VERSION
        or not isinstance(runs, list)
        or len(runs) < 2
    ):
        print("BLOCKED: verification evidence schema or run count is invalid.", file=sys.stderr)
        return 2

    clean_runs = runs[-2:]
    required_core = set(CORE_COMMAND_NAMES)
    required_install = set(INSTALL_COMMAND_NAMES)
    for run in clean_runs:
        if not isinstance(run, dict) or run.get("status") != "PASS":
            print("BLOCKED: the last two verification runs are not both clean.", file=sys.stderr)
            return 2
        commands = run.get("commands")
        inventory = run.get("command_names")
        if not isinstance(commands, list) or not commands or not isinstance(inventory, list):
            print("BLOCKED: a PASS run has no complete command evidence.", file=sys.stderr)
            return 2
        names: list[str] = []
        for command in commands:
            if not isinstance(command, dict):
                print("BLOCKED: a PASS run contains malformed command evidence.", file=sys.stderr)
                return 2
            name = command.get("name")
            exit_code = command.get("exit_code")
            if (
                not isinstance(name, str)
                or not name
                or command.get("passed") is not True
                or isinstance(exit_code, bool)
                or not isinstance(exit_code, int)
                or exit_code != 0
            ):
                print("BLOCKED: every recorded command in a PASS run must pass.", file=sys.stderr)
                return 2
            names.append(name)
        if inventory != names or len(names) != len(set(names)):
            print("BLOCKED: command-name inventory does not match retained commands.", file=sys.stderr)
            return 2
        if not required_core.issubset(names):
            print("BLOCKED: a PASS run is missing required core checks.", file=sys.stderr)
            return 2
        install_present = required_install.intersection(names)
        expected_kind = "core+isolated-install" if install_present == required_install else "core-only"
        if install_present and install_present != required_install:
            print("BLOCKED: isolated-install command evidence is incomplete.", file=sys.stderr)
            return 2
        if install_present:
            install_commands = [
                command for command in commands if command.get("name") in required_install
            ]
            if any(
                not isinstance(command.get("env"), dict)
                or command["env"].get("CODEX_HOME") != ISOLATED_CODEX_HOME_LABEL
                for command in install_commands
            ):
                print(
                    "BLOCKED: isolated-install evidence is not bound to temporary CODEX_HOME.",
                    file=sys.stderr,
                )
                return 2
        if run.get("matrix_kind") != expected_kind:
            print("BLOCKED: recorded matrix kind does not match command evidence.", file=sys.stderr)
            return 2
        try:
            recomputed_matrix = matrix_digest(commands)
        except VerificationError as exc:
            print(f"BLOCKED: {exc}", file=sys.stderr)
            return 2
        if run.get("matrix_digest") != recomputed_matrix:
            print("BLOCKED: recorded matrix digest does not match command evidence.", file=sys.stderr)
            return 2
        if (
            run.get("source_digest") != current_source["digest"]
            or run.get("source_file_count") != current_source["file_count"]
        ):
            print("BLOCKED: verification source digest is stale or mismatched.", file=sys.stderr)
            return 2

    first, second = clean_runs
    for field in ("source_digest", "source_file_count", "matrix_digest", "matrix_kind", "command_names"):
        if first.get(field) != second.get(field):
            print(f"BLOCKED: the last two runs have different {field} values.", file=sys.stderr)
            return 2
    run_ids = [run.get("run_id") for run in clean_runs]
    if not all(isinstance(value, str) and value for value in run_ids) or run_ids[0] == run_ids[1]:
        print("BLOCKED: the last two verification run IDs are invalid.", file=sys.stderr)
        return 2

    print(f"Two clean bound runs: {run_ids[0]} and {run_ids[1]}")
    return 0


def run_matrix(args: argparse.Namespace) -> int:
    wrapper = args.wrapper.resolve()
    results_path = wrapper / "verification-results.json"
    report_path = wrapper / "docs" / "verification-report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    if args.check_two_clean_runs:
        return check_two_clean_runs(results_path)

    python_bin = _resolve_executable(os.environ.get("PYTHON_BIN", "python3"))
    if not python_bin:
        run = _setup_failure("python-selection", wrapper)
        _save_results(results_path, report_path, run, append_run=args.append_run, python_bin=sys.executable)
        return 1

    yaml_python = find_yaml_python(yaml_python_candidates())
    plugin_validator = locate_validator(
        "plugin-creator", "validate_plugin.py", "PLUGIN_CREATOR", wrapper=wrapper
    )
    skill_validator = locate_validator(
        "skill-creator", "quick_validate.py", "SKILL_CREATOR", wrapper=wrapper
    )

    migration_map = wrapper / "migration-map.json"
    with tempfile.TemporaryDirectory(prefix="nightfalcon-inventory-") as tmp:
        inventory_snapshot = pathlib.Path(tmp) / "migration-map.before.json"
        inventory_snapshot.write_bytes(migration_map.read_bytes())
        commands = build_core_commands(
            wrapper=wrapper,
            python_bin=python_bin,
            yaml_python=yaml_python,
            plugin_validator=plugin_validator,
            skill_validator=skill_validator,
            inventory_snapshot=inventory_snapshot,
        )
        replacements: dict[str, str] = {
            str(inventory_snapshot): "<INVENTORY_SNAPSHOT>",
            str(inventory_snapshot.parent): "<RUN_TEMP>",
            str(wrapper): "<WRAPPER>",
            str(wrapper.parent): "<SOURCE_REPO>",
            str(pathlib.Path.home()): "<HOME>",
            tempfile.gettempdir(): "<TEMP>",
            str(plugin_validator): "<PLUGIN_VALIDATOR>",
            str(skill_validator): "<SKILL_VALIDATOR>",
            python_bin: "<PYTHON>",
            yaml_python: "<YAML_PYTHON>",
        }
        if not args.skip_codex_install:
            if args.runner_temp_root is None or args.temp_codex_home is None:
                raise VerificationError(
                    "Runner-owned temporary paths were not supplied by verify-port.sh"
                )
            temp_codex_home = validate_runner_temp_home(
                args.runner_temp_root, args.temp_codex_home
            )
            codex_bin = shutil.which("codex")
            if not codex_bin:
                run = _setup_failure("codex-selection", wrapper)
                _save_results(
                    results_path,
                    report_path,
                    run,
                    append_run=args.append_run,
                    python_bin=python_bin,
                )
                return 1
            commands.extend(
                build_codex_smoke_commands(wrapper, temp_codex_home, codex_bin=codex_bin)
            )
            replacements[str(args.runner_temp_root)] = "<VERIFY_TEMP_ROOT>"
            replacements[codex_bin] = "<CODEX>"

        source_before = source_manifest(wrapper)
        command_results = execute_commands(
            commands,
            replacements=replacements,
            stream=True,
        )

    timestamp, run_id = _timestamp()
    source_after = source_manifest(wrapper)
    run = build_run_record(
        run_id=run_id,
        timestamp=timestamp,
        source_manifest=source_before,
        matrix_kind="core-only" if args.skip_codex_install else "core+isolated-install",
        command_results=command_results,
    )
    passed = (
        len(command_results) == len(commands)
        and run["status"] == "PASS"
        and source_before["digest"] == source_after["digest"]
        and source_before["file_count"] == source_after["file_count"]
    )
    if not passed:
        run["status"] = "FAIL"
    if source_before["digest"] != source_after["digest"]:
        run["source_digest_after"] = source_after["digest"]
        run["source_file_count_after"] = source_after["file_count"]
    report_exit = _save_results(
        results_path,
        report_path,
        run,
        append_run=args.append_run,
        python_bin=python_bin,
    )
    if report_exit != 0:
        return report_exit
    print(f"Verification run {run_id}: {run['status']}")
    return 0 if passed else 1


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the NightFalcon Codex verification matrix.")
    parser.add_argument("--wrapper", type=pathlib.Path, required=True)
    parser.add_argument("--runner-temp-root", type=pathlib.Path)
    parser.add_argument("--temp-codex-home", type=pathlib.Path)
    parser.add_argument("--skip-codex-install", action="store_true")
    parser.add_argument("--append-run", action="store_true")
    parser.add_argument("--check-two-clean-runs", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--internal-file-equals":
        before, after = map(pathlib.Path, argv[1:3])
        if before.read_bytes() != after.read_bytes():
            print("Inventory regeneration changed migration-map.json", file=sys.stderr)
            return 1
        print("Inventory regeneration left migration-map.json unchanged.")
        return 0
    if argv and argv[0] == "--internal-scope-check":
        allow_standalone_without_base = argv[3:] == [
            "--allow-standalone-without-base"
        ]
        if len(argv) not in (3, 4) or (
            len(argv) == 4 and not allow_standalone_without_base
        ):
            print("Invalid internal scope-check arguments.", file=sys.stderr)
            return 2
        wrapper = pathlib.Path(argv[1]).resolve()
        distribution_root = SCRIPT_DIR.parent.resolve()
        if wrapper != distribution_root:
            print(
                f"Scope wrapper does not match runner distribution root: {wrapper}",
                file=sys.stderr,
            )
            return 1
        ok, message = scope_check(
            wrapper,
            argv[2],
            allow_standalone_without_base=allow_standalone_without_base,
        )
        print(message, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    args = parse_args(argv)
    try:
        return run_matrix(args)
    except Exception as exc:
        wrapper = args.wrapper.resolve()
        results_path = wrapper / "verification-results.json"
        report_path = wrapper / "docs" / "verification-report.md"
        python_bin = _resolve_executable(os.environ.get("PYTHON_BIN", "python3")) or sys.executable
        run = _setup_failure("verification-setup", wrapper)
        try:
            _save_results(
                results_path,
                report_path,
                run,
                append_run=args.append_run,
                python_bin=python_bin,
            )
        except Exception:
            print("Verification evidence could not be written.", file=sys.stderr)
        print(f"Verification setup failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
