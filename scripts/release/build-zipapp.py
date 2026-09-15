#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import zipfile


REPOSITORY = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(REPOSITORY / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from nightfalcon import __version__  # noqa: E402
from nightfalcon.payloads import PayloadManifest  # noqa: E402
from release_support import (  # noqa: E402
    canonical_json,
    finalize_release_directory,
    sha256_bytes,
    spdx_document,
)


FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def _tracked_python_files(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "src/nightfalcon"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 and (repo / ".git").exists():
        raise RuntimeError(result.stderr.decode("utf-8", "replace"))
    if result.returncode == 0:
        paths = sorted(item.decode() for item in result.stdout.split(b"\0") if item)
    else:
        package = repo / "src/nightfalcon"
        if package.is_symlink() or not package.is_dir():
            raise RuntimeError("canonical Python package is missing or unsafe")
        paths = []
        for candidate in sorted(package.rglob("*")):
            relative = candidate.relative_to(repo).as_posix()
            if candidate.is_symlink():
                raise RuntimeError(f"canonical Python package contains symlink: {relative}")
            if candidate.is_dir():
                continue
            if not candidate.is_file():
                raise RuntimeError(f"canonical Python package entry is not regular: {relative}")
            if "__pycache__" in candidate.parts or candidate.suffix in {".pyc", ".pyo"}:
                raise RuntimeError(f"canonical Python package contains generated file: {relative}")
            paths.append(relative)
    if not paths:
        raise RuntimeError("canonical Python package has no tracked files")
    return paths


def _mode(path: Path) -> int:
    return 0o755 if stat.S_IMODE(path.stat().st_mode) & 0o111 else 0o644


def _members(repo: Path) -> list[tuple[str, bytes, int]]:
    members: list[tuple[str, bytes, int]] = [
        ("__main__.py", b"from nightfalcon.cli import entrypoint\nraise SystemExit(entrypoint())\n", 0o644)
    ]
    for raw in _tracked_python_files(repo):
        source = repo / raw
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"unsafe canonical package file: {raw}")
        archive_name = PurePosixPath(raw).relative_to("src").as_posix()
        members.append((archive_name, source.read_bytes(), _mode(source)))

    manifest = PayloadManifest.build(repo)
    members.append(
        ("nightfalcon/_payload-manifest.json", manifest.to_json().encode(), 0o644)
    )
    for entry in manifest.files:
        source = repo.joinpath(*PurePosixPath(entry.path).parts)
        members.append(
            (
                f"nightfalcon/_payloads/{entry.path}",
                source.read_bytes(),
                int(entry.mode, 8),
            )
        )
    names = [item[0] for item in members]
    if len(names) != len(set(names)) or len({name.casefold() for name in names}) != len(names):
        raise RuntimeError("archive member names are ambiguous")
    return sorted(members)


def _write_zipapp(path: Path, members: list[tuple[str, bytes, int]]) -> None:
    path.write_bytes(b"#!/usr/bin/env python3\n")
    with zipfile.ZipFile(path, "a", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, content, mode in members:
            info = zipfile.ZipInfo(name, FIXED_TIME)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = mode << 16
            archive.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    path.chmod(0o755)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=REPOSITORY)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve(strict=True)
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    members = _members(repo)
    artifact = output / f"nightfalcon-{__version__}.pyz"
    _write_zipapp(artifact, members)
    artifact_bytes = artifact.read_bytes()
    digest = sha256_bytes(artifact_bytes)
    (output / f"{artifact.name}.sha256").write_text(
        f"{digest}  {artifact.name}\n", encoding="utf-8", newline="\n"
    )
    payload_manifest = PayloadManifest.build(repo)
    sbom = spdx_document(__version__, ((name, content) for name, content, _ in members))
    (output / f"nightfalcon-{__version__}.spdx.json").write_text(
        canonical_json(sbom), encoding="utf-8", newline="\n"
    )
    release = finalize_release_directory(
        output,
        version=__version__,
        payload_manifest_sha256=payload_manifest.sha256,
    )
    print(json.dumps(release, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
