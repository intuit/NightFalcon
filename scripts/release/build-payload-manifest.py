#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY / "src"))

from nightfalcon.payloads import PayloadManifest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=REPOSITORY)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    manifest = PayloadManifest.build(args.repo)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(manifest.to_json(), encoding="utf-8", newline="\n")
    print(json.dumps({"files": len(manifest.files), "sha256": manifest.sha256}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
