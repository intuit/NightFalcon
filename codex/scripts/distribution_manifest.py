#!/usr/bin/env python3
"""Deterministic, location-independent manifests for NightFalcon distributions."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import stat
from collections.abc import Iterable


FORMAT_VERSION = 1
IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
}
IGNORED_FILE_NAMES = {".DS_Store"}
IGNORED_FILE_SUFFIXES = {".pyc", ".pyo"}


class ManifestError(ValueError):
    """The requested tree cannot be represented safely and deterministically."""


def _canonical_json(document: object) -> bytes:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _normalized_excludes(paths: Iterable[str | pathlib.PurePath]) -> set[str]:
    normalized: set[str] = set()
    for value in paths:
        text = pathlib.PurePosixPath(str(value).replace(os.sep, "/")).as_posix()
        if text.startswith("/") or text == ".." or text.startswith("../"):
            raise ManifestError(f"excluded path escapes the distribution root: {value}")
        normalized.add(text.rstrip("/"))
    return normalized


def _ignored(relative: str, excludes: set[str]) -> bool:
    path = pathlib.PurePosixPath(relative)
    if any(part in IGNORED_DIRECTORY_NAMES for part in path.parts):
        return True
    if path.name in IGNORED_FILE_NAMES or path.suffix in IGNORED_FILE_SUFFIXES:
        return True
    return any(relative == item or relative.startswith(item + "/") for item in excludes)


def build_manifest(
    root: pathlib.Path | str,
    *,
    exclude_paths: Iterable[str | pathlib.PurePath] = (),
) -> dict[str, object]:
    """Return a canonical content/type/mode manifest without absolute paths."""
    root_path = pathlib.Path(root).resolve()
    if not root_path.is_dir():
        raise ManifestError(f"distribution root is not a directory: {root}")
    excludes = _normalized_excludes(exclude_paths)
    entries: list[dict[str, object]] = []

    def visit(directory: pathlib.Path, prefix: pathlib.PurePosixPath) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name.encode("utf-8"))
        except OSError as exc:
            raise ManifestError(f"cannot read distribution directory: {prefix}") from exc
        for child in children:
            relative_path = prefix / child.name
            relative = relative_path.as_posix()
            if _ignored(relative, excludes):
                continue
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise ManifestError(f"cannot stat distribution entry: {relative}") from exc
            mode = f"{stat.S_IMODE(metadata.st_mode):04o}"
            if child.is_symlink():
                target = os.readlink(child.path)
                if pathlib.PurePath(target).is_absolute():
                    raise ManifestError(
                        f"absolute distribution symlink target is not portable: {relative}"
                    )
                resolved = (directory / target).resolve(strict=False)
                try:
                    resolved.relative_to(root_path)
                except ValueError as exc:
                    raise ManifestError(
                        f"distribution symlink escapes root: {relative} -> {target}"
                    ) from exc
                entries.append(
                    {"path": relative, "type": "symlink", "mode": mode, "target": target}
                )
            elif child.is_dir(follow_symlinks=False):
                visit(pathlib.Path(child.path), relative_path)
            elif child.is_file(follow_symlinks=False):
                try:
                    content = pathlib.Path(child.path).read_bytes()
                except OSError as exc:
                    raise ManifestError(f"cannot read distribution file: {relative}") from exc
                entries.append(
                    {
                        "path": relative,
                        "type": "file",
                        "mode": mode,
                        "size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )
            else:
                raise ManifestError(f"unsupported distribution entry type: {relative}")

    visit(root_path, pathlib.PurePosixPath())
    subject = {"format_version": FORMAT_VERSION, "entries": entries}
    return {
        **subject,
        "file_count": len(entries),
        "total_bytes": sum(
            int(entry.get("size", len(str(entry.get("target", "")).encode("utf-8"))))
            for entry in entries
        ),
        "digest": hashlib.sha256(_canonical_json(subject)).hexdigest(),
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=pathlib.Path)
    parser.add_argument("--exclude", action="append", default=[])
    arguments = parser.parse_args()
    print(json.dumps(build_manifest(arguments.root, exclude_paths=arguments.exclude), indent=2))
