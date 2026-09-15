#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile


REPOSITORY = Path(__file__).resolve().parents[2]


def tracked_files(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace"))
    return sorted(item.decode("utf-8") for item in result.stdout.split(b"\0") if item)


def stage_source(repo: Path, stage: Path) -> None:
    stage.mkdir(parents=True, exist_ok=False)
    for raw in tracked_files(repo):
        relative = PurePosixPath(raw)
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != raw:
            raise RuntimeError(f"unsafe tracked source path: {raw!r}")
        source = repo.joinpath(*relative.parts)
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"Debian source must be a regular file: {raw}")
        destination = stage.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=False)
    metadata = stage / "packaging/debian"
    if not metadata.is_dir():
        raise RuntimeError("packaging/debian metadata is missing")
    shutil.copytree(metadata, stage / "debian", copy_function=shutil.copy2)


def build(stage: Path, output: Path) -> None:
    executable = shutil.which("dpkg-buildpackage")
    if executable is None:
        raise RuntimeError("dpkg-buildpackage is required")
    result = subprocess.run(
        [executable, "--build=binary", "--no-sign"],
        cwd=stage,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"dpkg-buildpackage failed with status {result.returncode}")
    output.mkdir(parents=True, exist_ok=True)
    artifacts: list[Path] = []
    for pattern in ("nightfalcon_*.deb", "nightfalcon_*.buildinfo", "nightfalcon_*.changes"):
        for source in sorted(stage.parent.glob(pattern)):
            destination = output / source.name
            shutil.copy2(source, destination)
            artifacts.append(destination)
    if not any(path.suffix == ".deb" for path in artifacts):
        raise RuntimeError("Debian build did not produce a .deb package")
    for artifact in artifacts:
        print(artifact)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=REPOSITORY)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--stage-dir", type=Path)
    parser.add_argument("--stage-only", action="store_true")
    args = parser.parse_args()
    repo = args.repo.resolve(strict=True)
    if args.stage_only and args.stage_dir is None:
        parser.error("--stage-only requires --stage-dir")
    if not args.stage_only and args.output_dir is None:
        parser.error("--output-dir is required unless --stage-only is used")
    try:
        if args.stage_dir is not None:
            stage = args.stage_dir.resolve()
            stage_source(repo, stage)
            if not args.stage_only:
                build(stage, args.output_dir.resolve())
            else:
                print(stage)
            return 0
        with tempfile.TemporaryDirectory(prefix="nightfalcon-debian-") as temporary:
            stage = Path(temporary) / "nightfalcon-3.0.0"
            stage_source(repo, stage)
            build(stage, args.output_dir.resolve())
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"nightfalcon Debian build failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
