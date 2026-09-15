#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile


REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY / "src"))

from nightfalcon import __version__  # noqa: E402
from nightfalcon.payloads import PayloadManifest  # noqa: E402


def _tracked(repo: Path, pathspec: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", pathspec],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace"))
    return sorted(item.decode() for item in result.stdout.split(b"\0") if item)


def _copy_file(repo: Path, stage: Path, raw: str) -> None:
    relative = PurePosixPath(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"unsafe package source path: {raw}")
    source = repo.joinpath(*relative.parts)
    if source.is_symlink() or not source.is_file():
        raise RuntimeError(f"package source must be a regular file: {raw}")
    destination = stage.joinpath(*relative.parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination, follow_symlinks=False)


def _stage(repo: Path, stage: Path) -> None:
    for raw in ("LICENSE", "NOTICE", "README.md", "VERSION", "pyproject.toml", "MANIFEST.in"):
        _copy_file(repo, stage, raw)
    for raw in _tracked(repo, "src/nightfalcon"):
        _copy_file(repo, stage, raw)

    manifest = PayloadManifest.build(repo)
    package = stage / "src/nightfalcon"
    (package / "_payload-manifest.json").write_text(
        manifest.to_json(), encoding="utf-8", newline="\n"
    )
    for entry in manifest.files:
        source = repo.joinpath(*PurePosixPath(entry.path).parts)
        destination = package / "_payloads" / entry.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination, follow_symlinks=False)
        destination.chmod(int(entry.mode, 8))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=REPOSITORY)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve(strict=True)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="nightfalcon-python-package-") as temporary:
        stage = Path(temporary) / "source"
        stage.mkdir()
        _stage(repo, stage)
        environment = dict(os.environ)
        environment["SOURCE_DATE_EPOCH"] = "315532800"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--no-isolation",
                "--wheel",
                "--sdist",
                "--outdir",
                str(output),
                str(stage),
            ],
            env=environment,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return result.returncode

    wheel = output / f"nightfalcon-{__version__}-py3-none-any.whl"
    sdist = output / f"nightfalcon-{__version__}.tar.gz"
    if not wheel.is_file() or not sdist.is_file():
        print("nightfalcon package build did not produce expected files", file=sys.stderr)
        return 1
    print(wheel)
    print(sdist)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
