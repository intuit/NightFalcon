#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import subprocess
import sys


REPOSITORY = Path(__file__).resolve().parents[2]
OUTPUT = REPOSITORY / "packaging/runtime-executables.txt"
ROOTS = ("claude", "codex", "cursor")


def build(repo: Path) -> str:
    result = subprocess.run(
        ["git", "ls-files", "--stage", "-z", "--", *ROOTS],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace"))
    paths: list[str] = []
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise RuntimeError("unexpected git executable inventory record")
        if fields[0] != b"100755":
            continue
        path = raw_path.decode("utf-8")
        normalized = PurePosixPath(path)
        if normalized.is_absolute() or ".." in normalized.parts or normalized.as_posix() != path:
            raise RuntimeError(f"unsafe executable payload path: {path!r}")
        paths.append(path)
    if len(paths) != len(set(paths)):
        raise RuntimeError("duplicate executable payload path")
    return "".join(f"{path}\n" for path in sorted(paths))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=REPOSITORY)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = build(args.repo.resolve(strict=True))
    if args.check:
        if not args.output.is_file() or args.output.read_text() != expected:
            print("runtime executable inventory is stale", file=sys.stderr)
            return 1
        print("runtime executable inventory is current")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(expected, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
