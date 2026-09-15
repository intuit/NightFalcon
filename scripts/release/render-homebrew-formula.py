#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import sys


REPOSITORY = Path(__file__).resolve().parents[2]
TEMPLATE = REPOSITORY / "packaging/homebrew/Formula/nightfalcon.rb.template"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    artifact = args.artifact.resolve(strict=True)
    match = re.fullmatch(r"nightfalcon-(\d+\.\d+\.\d+)\.pyz", artifact.name)
    if not match:
        print("artifact name must be nightfalcon-<semver>.pyz", file=sys.stderr)
        return 2
    version = match.group(1)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    template = TEMPLATE.read_text(encoding="utf-8")
    rendered = template.replace("@VERSION@", version).replace("@SHA256@", digest)
    if "@VERSION@" in rendered or "@SHA256@" in rendered:
        print("formula template contains unresolved tokens", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8", newline="\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
