#!/usr/bin/env python3
"""Reinstall a local NightFalcon plugin and repair legacy cache aliases safely."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import fcntl
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from distribution_manifest import ManifestError, build_manifest


MARKETPLACE_FILE = pathlib.Path(".agents/plugins/marketplace.json")
PLUGIN_MANIFEST = pathlib.Path(".codex-plugin/plugin.json")
IGNORED_CACHE_ENTRIES = {".DS_Store", "__pycache__"}
TEMP_LINK_MARKER = ".nightfalcon-link-"


class RepairError(RuntimeError):
    """The reinstall could not be validated or completed safely."""


@dataclasses.dataclass(frozen=True)
class Context:
    wrapper: pathlib.Path
    source_root: pathlib.Path
    codex_home: pathlib.Path
    marketplace: str
    plugin_name: str
    version: str
    selector: str
    cache_root: pathlib.Path
    repair_root: pathlib.Path


@dataclasses.dataclass(frozen=True)
class VersionEntry:
    name: str
    path: pathlib.Path
    kind: str
    link_target: str | None = None


@dataclasses.dataclass
class Mutation:
    alias: pathlib.Path
    original_kind: str
    original_target: str | None = None
    backup: pathlib.Path | None = None
    changed: bool = False


def _read_object(path: pathlib.Path, label: str) -> dict[str, object]:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RepairError(f"cannot read {label}: {path}") from exc
    if not isinstance(document, dict):
        raise RepairError(f"{label} must be a JSON object: {path}")
    return document


def _component(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise RepairError(f"{label} must be a nonempty path component")
    if pathlib.PurePath(value).name != value or "/" in value or "\\" in value:
        raise RepairError(f"{label} must not contain a path separator")
    return value


def _lexical_absolute(path: pathlib.Path | str) -> pathlib.Path:
    return pathlib.Path(os.path.abspath(os.fspath(pathlib.Path(path).expanduser())))


def _reject_symlink_components(path: pathlib.Path, label: str) -> None:
    if not path.is_absolute():
        raise RepairError(f"{label} must be absolute")
    current = pathlib.Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            break
        except OSError as exc:
            raise RepairError(f"cannot inspect {label} component: {current}") from exc
        if stat.S_ISLNK(mode):
            raise RepairError(f"{label} contains a symlink component: {current}")
        if not stat.S_ISDIR(mode):
            raise RepairError(f"{label} contains a non-directory component: {current}")


def _validate_controlled_paths(context: Context) -> None:
    _reject_symlink_components(context.codex_home, "CODEX_HOME")
    _reject_symlink_components(context.cache_root, "plugin cache root")
    _reject_symlink_components(context.repair_root, "plugin repair root")
    for path, label in (
        (context.cache_root, "plugin cache root"),
        (context.repair_root, "plugin repair root"),
    ):
        try:
            path.relative_to(context.codex_home)
        except ValueError as exc:
            raise RepairError(f"{label} escapes CODEX_HOME") from exc


def _reject_symlink_file(path: pathlib.Path, label: str) -> None:
    _reject_symlink_components(path.parent, f"{label} parent")
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RepairError(f"cannot inspect {label}: {path}") from exc
    if stat.S_ISLNK(mode):
        raise RepairError(f"{label} is a symlink: {path}")
    if not stat.S_ISREG(mode):
        raise RepairError(f"{label} is not a regular file: {path}")


def _validate_operational_paths(context: Context, run_id: str | None = None) -> None:
    _validate_controlled_paths(context)
    audit_root = context.repair_root / "audit"
    backups_root = context.repair_root / "backups"
    _reject_symlink_components(audit_root, "repair audit directory")
    _reject_symlink_components(backups_root, "repair backup directory")
    _reject_symlink_file(context.repair_root / "repair.lock", "repair lock")
    if run_id is not None:
        _reject_symlink_components(
            backups_root / run_id, "repair run backup directory"
        )
        _reject_symlink_file(audit_root / f"{run_id}.json", "repair audit file")


def _validate_installed_root(context: Context, installed_root: pathlib.Path) -> None:
    try:
        mode = installed_root.lstat().st_mode
    except OSError as exc:
        raise RepairError("installed root is unavailable as a real cache child") from exc
    if (
        installed_root.parent != context.cache_root
        or stat.S_ISLNK(mode)
        or not stat.S_ISDIR(mode)
    ):
        raise RepairError("installed root must remain a real direct cache child")
    try:
        if installed_root.resolve(strict=True) != installed_root:
            raise RepairError("installed root must remain a real direct cache child")
    except OSError as exc:
        raise RepairError("installed root is unavailable as a real cache child") from exc


def resolve_context(
    wrapper: pathlib.Path | str,
    codex_home: pathlib.Path | str,
) -> Context:
    try:
        wrapper_path = pathlib.Path(wrapper).resolve(strict=True)
    except OSError as exc:
        raise RepairError(f"wrapper root is unavailable: {wrapper}") from exc
    if not wrapper_path.is_dir():
        raise RepairError(f"wrapper root is not a directory: {wrapper_path}")
    codex_home_path = _lexical_absolute(codex_home)

    marketplace_doc = _read_object(
        wrapper_path / MARKETPLACE_FILE, "marketplace manifest"
    )
    marketplace = _component(marketplace_doc.get("name"), "marketplace name")
    plugins = marketplace_doc.get("plugins")
    if not isinstance(plugins, list):
        raise RepairError("marketplace plugins must be a list")

    local_plugins: list[tuple[str, pathlib.Path]] = []
    for entry in plugins:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        source = entry.get("source")
        if (
            isinstance(name, str)
            and isinstance(source, dict)
            and source.get("source") == "local"
            and isinstance(source.get("path"), str)
        ):
            local_plugins.append((name, pathlib.Path(source["path"])))
    if len(local_plugins) != 1:
        raise RepairError("marketplace must identify exactly one local plugin")

    listed_name, relative_source = local_plugins[0]
    plugin_name = _component(listed_name, "plugin name")
    source_root = (wrapper_path / relative_source).resolve(strict=True)
    try:
        source_root.relative_to(wrapper_path)
    except ValueError as exc:
        raise RepairError("local plugin source escapes the wrapper root") from exc
    if not source_root.is_dir():
        raise RepairError(f"local plugin source is not a directory: {source_root}")

    plugin_doc = _read_object(source_root / PLUGIN_MANIFEST, "plugin manifest")
    manifest_name = _component(plugin_doc.get("name"), "manifest plugin name")
    if manifest_name != plugin_name:
        raise RepairError("marketplace and plugin manifest names differ")
    version = _component(plugin_doc.get("version"), "plugin version")

    cache_root = (
        codex_home_path / "plugins" / "cache" / marketplace / plugin_name
    )
    repair_root = (
        codex_home_path / "plugins" / "cache-repair" / marketplace / plugin_name
    )
    context = Context(
        wrapper=wrapper_path,
        source_root=source_root,
        codex_home=codex_home_path,
        marketplace=marketplace,
        plugin_name=plugin_name,
        version=version,
        selector=f"{plugin_name}@{marketplace}",
        cache_root=cache_root,
        repair_root=repair_root,
    )
    _validate_controlled_paths(context)
    return context


def _snapshot_versions(cache_root: pathlib.Path) -> list[VersionEntry]:
    if not cache_root.exists():
        return []
    if not cache_root.is_dir():
        raise RepairError(f"plugin cache root is not a directory: {cache_root}")
    entries: list[VersionEntry] = []
    for child in sorted(os.scandir(cache_root), key=lambda item: item.name.encode("utf-8")):
        if TEMP_LINK_MARKER in child.name:
            raise RepairError(
                f"stranded temporary compatibility link in plugin cache: {child.path}"
            )
        if child.name in IGNORED_CACHE_ENTRIES:
            continue
        path = pathlib.Path(child.path)
        if child.is_symlink():
            entries.append(
                VersionEntry(child.name, path, "symlink", os.readlink(child.path))
            )
        elif child.is_dir(follow_symlinks=False):
            entries.append(VersionEntry(child.name, path, "directory"))
        else:
            raise RepairError(f"unsupported entry in plugin version cache: {path}")
    return entries


def _identity_matches(candidate: pathlib.Path, context: Context) -> bool:
    try:
        document = _read_object(candidate / PLUGIN_MANIFEST, "installed plugin manifest")
    except RepairError:
        return False
    return (
        document.get("name") == context.plugin_name
        and document.get("version") == context.version
    )


def _discover_validated_install(
    context: Context,
    source_manifest: Mapping[str, object],
) -> tuple[pathlib.Path, dict[str, object]]:
    if not context.cache_root.is_dir():
        raise RepairError(
            "codex plugin add did not create the expected dynamic cache root: "
            f"{context.cache_root}"
        )
    identity_candidates: list[pathlib.Path] = []
    matching: list[tuple[pathlib.Path, dict[str, object]]] = []
    for child in sorted(
        os.scandir(context.cache_root), key=lambda item: item.name.encode("utf-8")
    ):
        if child.name in IGNORED_CACHE_ENTRIES or child.is_symlink():
            continue
        if not child.is_dir(follow_symlinks=False):
            continue
        candidate = pathlib.Path(child.path)
        if not _identity_matches(candidate, context):
            continue
        identity_candidates.append(candidate)
        try:
            installed_manifest = build_manifest(candidate)
        except ManifestError as exc:
            raise RepairError(f"installed distribution cannot be validated: {candidate}") from exc
        if (
            installed_manifest.get("digest") == source_manifest.get("digest")
            and installed_manifest.get("file_count") == source_manifest.get("file_count")
        ):
            matching.append((candidate, installed_manifest))
    if not matching:
        if identity_candidates:
            raise RepairError("installed distribution parity check failed")
        raise RepairError(
            "no real installed cache directory matches the current plugin name and version"
        )
    if len(matching) != 1:
        raise RepairError("multiple installed cache directories match source parity")
    candidate, installed_manifest = matching[0]
    return candidate.resolve(strict=True), installed_manifest


def _fsync_directory(path: pathlib.Path) -> None:
    """Best-effort durability barrier for a directory entry update."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        os.fsync(descriptor)
    except OSError:
        # Some filesystems/platforms do not support directory fsync. Atomic
        # rename semantics still apply, but power-loss durability is weaker.
        pass
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _mkdir_durable(path: pathlib.Path) -> None:
    missing: list[pathlib.Path] = []
    current = path
    while True:
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            missing.append(current)
            if current.parent == current:
                break
            current = current.parent
            continue
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise RepairError(f"directory creation encountered an unsafe path: {current}")
        break
    path.mkdir(parents=True, exist_ok=True)
    for created in reversed(missing):
        _fsync_directory(created.parent)


def _replace_path(source: pathlib.Path, destination: pathlib.Path) -> None:
    os.replace(source, destination)
    _fsync_directory(source.parent)
    if destination.parent != source.parent:
        _fsync_directory(destination.parent)


def _publish_direct_symlink(alias: pathlib.Path, target: pathlib.Path) -> None:
    temporary = alias.with_name(
        f".{alias.name}.nightfalcon-link-{os.getpid()}-{uuid.uuid4().hex}"
    )
    try:
        temporary.symlink_to(str(target), target_is_directory=True)
        _replace_path(temporary, alias)
    finally:
        if temporary.is_symlink():
            temporary.unlink()
            _fsync_directory(alias.parent)


def _direct_alias(alias: pathlib.Path, target: pathlib.Path) -> bool:
    return alias.is_symlink() and os.readlink(alias) == str(target)


def _remove_created_alias(alias: pathlib.Path, target: pathlib.Path) -> None:
    if alias.is_symlink() and os.readlink(alias) == str(target):
        alias.unlink()
        _fsync_directory(alias.parent)
    elif alias.exists() or alias.is_symlink():
        raise RepairError(f"refusing to replace concurrently changed alias: {alias}")


def _restore_mutation(mutation: Mutation, target: pathlib.Path) -> None:
    alias = mutation.alias
    if mutation.original_kind == "directory":
        backup = mutation.backup
        if backup is None:
            raise RepairError(f"legacy backup was not recorded: {alias}")
        if backup.is_dir() and not backup.is_symlink():
            if _direct_alias(alias, target):
                _remove_created_alias(alias, target)
            elif alias.exists() or alias.is_symlink():
                raise RepairError(
                    f"refusing to overwrite changed legacy directory during rollback: {alias}"
                )
            _replace_path(backup, alias)
        elif alias.is_dir() and not alias.is_symlink() and not backup.exists():
            # The move failed before changing the original directory.
            return
        else:
            raise RepairError(f"legacy backup is unavailable: {backup}")
    elif mutation.original_kind == "symlink":
        if mutation.original_target is None:
            raise RepairError("original symlink target was not recorded")
        if alias.is_symlink():
            current = os.readlink(alias)
            if current == mutation.original_target:
                return
            if current != str(target):
                raise RepairError(f"refusing to overwrite changed legacy link: {alias}")
        elif alias.exists():
            raise RepairError(f"refusing to restore a legacy link over a real path: {alias}")
        _publish_direct_symlink(alias, pathlib.Path(mutation.original_target))
    elif mutation.original_kind == "missing":
        if _direct_alias(alias, target):
            _remove_created_alias(alias, target)
        elif alias.exists() or alias.is_symlink():
            raise RepairError(f"refusing to remove concurrently created path: {alias}")
    else:
        raise RepairError(f"unsupported rollback type: {mutation.original_kind}")


def _rollback(mutations: Sequence[Mutation], target: pathlib.Path) -> list[str]:
    errors: list[str] = []
    for mutation in reversed(mutations):
        if not mutation.changed:
            continue
        try:
            _restore_mutation(mutation, target)
        except BaseException as exc:
            errors.append(f"{mutation.alias.name}: {type(exc).__name__}")
    return errors


def _mutation_document(mutation: Mutation) -> dict[str, object]:
    return {
        "version": mutation.alias.name,
        "original_kind": mutation.original_kind,
        "original_target": mutation.original_target,
        "backup_path": str(mutation.backup) if mutation.backup is not None else None,
    }


def _clean_journal_link_debris(alias: pathlib.Path) -> None:
    prefix = f".{alias.name}{TEMP_LINK_MARKER}"
    changed = False
    try:
        entries = sorted(os.scandir(alias.parent), key=lambda item: item.name.encode("utf-8"))
    except OSError as exc:
        raise RepairError(f"cannot inspect compatibility-link debris: {alias.parent}") from exc
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        debris = pathlib.Path(entry.path)
        if not entry.is_symlink():
            raise RepairError(f"journal-bound link debris is not a symlink: {debris}")
        debris.unlink()
        changed = True
    if changed:
        _fsync_directory(alias.parent)


def _pending_mutations(
    context: Context, audit_path: pathlib.Path, document: Mapping[str, object]
) -> tuple[pathlib.Path, list[Mutation]]:
    schema_version = document.get("schema_version")
    run_id = document.get("run_id")
    if (
        isinstance(schema_version, bool)
        or schema_version != 1
        or not isinstance(run_id, str)
        or _safe_run_id(run_id) != run_id
        or audit_path.name != f"{run_id}.json"
        or document.get("selector") != context.selector
        or document.get("cache_root") != str(context.cache_root)
    ):
        raise RepairError(f"invalid pending repair journal: {audit_path}")
    installed_value = document.get("installed_root")
    if not isinstance(installed_value, str):
        raise RepairError(f"pending repair journal has no installed root: {audit_path}")
    installed_root = pathlib.Path(installed_value)
    if installed_root.parent != context.cache_root:
        raise RepairError(f"pending installed root escapes the plugin cache: {audit_path}")

    plan = document.get("plan")
    if not isinstance(plan, list):
        raise RepairError(f"pending repair journal has no transaction plan: {audit_path}")
    mutations: list[Mutation] = []
    seen: set[str] = set()
    for item in plan:
        if not isinstance(item, dict):
            raise RepairError(f"pending repair plan is malformed: {audit_path}")
        version = _component(item.get("version"), "pending compatibility version")
        if version in seen or version == installed_root.name:
            raise RepairError(f"pending repair plan has duplicate/self alias: {audit_path}")
        seen.add(version)
        original_kind = item.get("original_kind")
        original_target = item.get("original_target")
        backup_value = item.get("backup_path")
        if original_kind not in {"directory", "symlink", "missing"}:
            raise RepairError(f"pending repair plan has invalid entry type: {audit_path}")
        if original_kind == "symlink" and not isinstance(original_target, str):
            raise RepairError(f"pending repair plan lost a symlink target: {audit_path}")
        if original_kind != "symlink" and original_target is not None:
            raise RepairError(f"pending repair plan has an unexpected link target: {audit_path}")
        backup: pathlib.Path | None = None
        if original_kind == "directory":
            expected_backup = context.repair_root / "backups" / run_id / version
            if backup_value != str(expected_backup):
                raise RepairError(f"pending repair backup path is invalid: {audit_path}")
            backup = expected_backup
        elif backup_value is not None:
            raise RepairError(f"pending repair plan has an unexpected backup: {audit_path}")
        mutations.append(
            Mutation(
                alias=context.cache_root / version,
                original_kind=original_kind,
                original_target=original_target,
                backup=backup,
                changed=True,
            )
        )
    return installed_root, mutations


def _recover_pending_repairs(context: Context) -> None:
    _validate_operational_paths(context)
    audit_root = context.repair_root / "audit"
    if not audit_root.exists():
        return
    if not audit_root.is_dir():
        raise RepairError(f"repair audit root is not a directory: {audit_root}")
    for audit_path in sorted(audit_root.glob("*.json"), key=lambda path: path.name):
        _reject_symlink_file(audit_path, "repair audit file")
        document = _read_object(audit_path, "repair audit")
        if document.get("status") != "IN_PROGRESS":
            continue
        installed_root, mutations = _pending_mutations(context, audit_path, document)
        _validate_operational_paths(context, document["run_id"])
        for mutation in reversed(mutations):
            _restore_mutation(mutation, installed_root)
            _clean_journal_link_debris(mutation.alias)
            # Recovery can observe an already-restored state after the prior
            # process changed the directory entry but died before its barrier.
            # Persist both sides of the original rename before closing the
            # journal, even when _restore_mutation itself was a no-op.
            _fsync_directory(mutation.alias.parent)
            if mutation.backup is not None:
                _fsync_directory(mutation.backup.parent)
        document["status"] = "RECOVERED"
        document["recovery"] = "PASS"
        _atomic_json(audit_path, document)


def _verify_aliases(
    aliases: Sequence[pathlib.Path],
    installed_root: pathlib.Path,
    trusted_hooks: Mapping[str, object],
) -> None:
    hooks_root = installed_root / "hooks"
    if not hooks_root.is_dir():
        raise RepairError("validated installation has no hooks directory")
    installed_hooks = build_manifest(hooks_root)
    if (
        installed_hooks.get("digest") != trusted_hooks.get("digest")
        or installed_hooks.get("file_count") != trusted_hooks.get("file_count")
    ):
        raise RepairError("installed hook bytes no longer match the trusted source")
    for alias in aliases:
        if not _direct_alias(alias, installed_root):
            raise RepairError(f"compatibility alias is not direct: {alias}")
        try:
            resolved = alias.resolve(strict=True)
        except OSError as exc:
            raise RepairError(f"compatibility alias is broken: {alias}") from exc
        if resolved != installed_root:
            raise RepairError(f"compatibility alias resolves to an unvalidated root: {alias}")
        alias_hooks = build_manifest(alias / "hooks")
        if (
            alias_hooks.get("digest") != trusted_hooks.get("digest")
            or alias_hooks.get("file_count") != trusted_hooks.get("file_count")
        ):
            raise RepairError(f"compatibility hook bytes differ: {alias}")


def _atomic_json(path: pathlib.Path, document: Mapping[str, object]) -> None:
    _mkdir_durable(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w") as handle:
            handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        _replace_path(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def _repair_lock(context: Context):
    _validate_operational_paths(context)
    _mkdir_durable(context.repair_root)
    _validate_operational_paths(context)
    lock_path = context.repair_root / "repair.lock"
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _default_run_id() -> str:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{timestamp}-{os.getpid()}"


def _safe_run_id(value: str) -> str:
    return _component(value, "repair run ID")


def repair_install(
    *,
    wrapper: pathlib.Path | str,
    codex_home: pathlib.Path | str,
    codex_bin: str,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    run_id: str | None = None,
) -> dict[str, object]:
    context = resolve_context(wrapper, codex_home)
    run_id = _safe_run_id(run_id or _default_run_id())
    _validate_operational_paths(context, run_id)
    audit_path = context.repair_root / "audit" / f"{run_id}.json"
    backup_root = context.repair_root / "backups" / run_id
    mutations: list[Mutation] = []
    actions: list[dict[str, object]] = []
    source_before = build_manifest(context.source_root)
    source_hooks_before = build_manifest(context.source_root / "hooks")
    audit: dict[str, object] = {
        "schema_version": 1,
        "run_id": run_id,
        "selector": context.selector,
        "source_root": str(context.source_root),
        "cache_root": str(context.cache_root),
        "source_digest": source_before["digest"],
        "status": "FAIL",
        "actions": actions,
    }

    with _repair_lock(context):
        _validate_operational_paths(context, run_id)
        _recover_pending_repairs(context)
        preinstall = _snapshot_versions(context.cache_root)
        audit["preinstall_versions"] = [entry.name for entry in preinstall]
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(context.codex_home)
        command = [codex_bin, "plugin", "add", context.selector]
        try:
            completed = run_command(
                command,
                cwd=context.wrapper,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            audit["failure"] = f"plugin add could not start: {type(exc).__name__}"
            _atomic_json(audit_path, audit)
            raise RepairError("codex plugin add could not be started") from exc
        if (
            isinstance(completed.returncode, bool)
            or not isinstance(completed.returncode, int)
            or completed.returncode != 0
        ):
            audit["failure"] = f"plugin add exit: {completed.returncode}"
            _atomic_json(audit_path, audit)
            raise RepairError(f"codex plugin add failed with exit {completed.returncode}")

        _validate_operational_paths(context, run_id)
        source_after = build_manifest(context.source_root)
        if source_after["digest"] != source_before["digest"]:
            audit["failure"] = "source distribution changed during plugin add"
            _atomic_json(audit_path, audit)
            raise RepairError("source distribution changed during plugin add")

        try:
            installed_root, installed_manifest = _discover_validated_install(
                context, source_before
            )
        except BaseException as exc:
            audit["failure"] = str(exc)
            _atomic_json(audit_path, audit)
            raise
        audit["installed_root"] = str(installed_root)
        audit["installed_digest"] = installed_manifest["digest"]
        _validate_installed_root(context, installed_root)

        aliases = [entry for entry in preinstall if entry.path != installed_root]
        alias_paths = [entry.path for entry in aliases]
        for entry in aliases:
            path = entry.path
            if path.is_symlink():
                continue
            if path.exists() and not path.is_dir():
                audit["failure"] = f"unsupported legacy cache entry: {entry.name}"
                _atomic_json(audit_path, audit)
                raise RepairError(f"unsupported legacy cache entry: {path}")

        publication_plan: list[tuple[Mutation, dict[str, object]]] = []
        for entry in aliases:
            alias = entry.path
            if _direct_alias(alias, installed_root):
                actions.append(
                    {
                        "alias": str(alias),
                        "action": "unchanged",
                        "target": str(installed_root),
                    }
                )
                continue
            if alias.is_symlink():
                mutation = Mutation(
                    alias=alias,
                    original_kind="symlink",
                    original_target=os.readlink(alias),
                )
            elif alias.is_dir():
                backup = backup_root / entry.name
                if backup.exists() or backup.is_symlink():
                    raise RepairError(f"backup path already exists: {backup}")
                mutation = Mutation(
                    alias=alias,
                    original_kind="directory",
                    backup=backup,
                )
            elif not alias.exists():
                mutation = Mutation(alias=alias, original_kind="missing")
            else:
                raise RepairError(f"unsupported legacy cache entry: {alias}")
            action: dict[str, object] = {
                "alias": str(alias),
                "action": "planned",
                "original_kind": mutation.original_kind,
                "target": str(installed_root),
            }
            if mutation.backup is not None:
                action["backup_path"] = str(mutation.backup)
            mutations.append(mutation)
            actions.append(action)
            publication_plan.append((mutation, action))

        audit["plan"] = [_mutation_document(mutation) for mutation in mutations]
        audit["status"] = "IN_PROGRESS"
        try:
            _validate_operational_paths(context, run_id)
            _atomic_json(audit_path, audit)
        except OSError as exc:
            raise RepairError("cannot create the repair audit before publication") from exc

        try:
            for mutation, action in publication_plan:
                _validate_operational_paths(context, run_id)
                _validate_installed_root(context, installed_root)
                alias = mutation.alias
                # The journal already describes the complete original state.
                # Mark this entry before the first mutator so a call that
                # succeeds and then raises is still restored.
                mutation.changed = True
                if mutation.original_kind == "directory":
                    _mkdir_durable(mutation.backup.parent)
                    _replace_path(alias, mutation.backup)
                _publish_direct_symlink(alias, installed_root)
                action["action"] = "published"

            _validate_operational_paths(context, run_id)
            _validate_installed_root(context, installed_root)
            source_final = build_manifest(context.source_root)
            installed_final = build_manifest(installed_root)
            if (
                source_final.get("digest") != source_before.get("digest")
                or source_final.get("file_count") != source_before.get("file_count")
            ):
                raise RepairError("source distribution changed during alias publication")
            if (
                installed_final.get("digest") != source_before.get("digest")
                or installed_final.get("file_count") != source_before.get("file_count")
            ):
                raise RepairError(
                    "installed distribution parity changed during alias publication"
                )
            _verify_aliases(alias_paths, installed_root, source_hooks_before)
            audit["status"] = "PASS"
            audit["installed_digest"] = installed_final["digest"]
            audit["aliases"] = [str(path) for path in alias_paths]
            _atomic_json(audit_path, audit)
        except BaseException as exc:
            rollback_errors = _rollback(mutations, installed_root)
            # A failed rollback remains recoverable on the next invocation;
            # never turn its durable IN_PROGRESS plan into a terminal record.
            audit["status"] = "IN_PROGRESS" if rollback_errors else "FAIL"
            audit["failure"] = f"{type(exc).__name__}: {exc}"
            audit["rollback"] = "FAIL" if rollback_errors else "PASS"
            if rollback_errors:
                audit["rollback_errors"] = rollback_errors
            try:
                _atomic_json(audit_path, audit)
            except OSError:
                # The pre-publication IN_PROGRESS record remains as durable
                # evidence when the filesystem cannot accept a final update.
                pass
            detail = "rollback incomplete" if rollback_errors else "changes rolled back"
            raise RepairError(f"compatibility alias publication failed; {detail}") from exc

        return {
            **audit,
            "audit_path": str(audit_path),
            "aliases": [str(path) for path in alias_paths],
        }


def _resolve_codex_home(explicit: pathlib.Path | None) -> pathlib.Path:
    if explicit is not None:
        return explicit
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return pathlib.Path(configured)
    home = os.environ.get("HOME")
    if not home:
        raise RepairError("HOME and CODEX_HOME are both unavailable")
    return pathlib.Path(home) / ".codex"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reinstall the local NightFalcon plugin and repair cache aliases."
    )
    parser.add_argument("--codex-home", type=pathlib.Path)
    parser.add_argument("--codex-bin", default="codex")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    wrapper = pathlib.Path(__file__).resolve().parents[1]
    try:
        codex_home = _resolve_codex_home(args.codex_home)
        codex_bin = shutil.which(args.codex_bin)
        if not codex_bin:
            raise RepairError(f"Codex executable not found: {args.codex_bin}")
        result = repair_install(
            wrapper=wrapper,
            codex_home=codex_home,
            codex_bin=codex_bin,
        )
    except (RepairError, ManifestError, OSError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    print(f"Reinstalled {result['selector']}; audit: {result['audit_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
