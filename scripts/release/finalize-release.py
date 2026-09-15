#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from release_support import finalize_release_directory  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    args = parser.parse_args()
    root = args.release_dir.resolve(strict=True)
    manifest_path = root / "release-manifest.json"
    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        version = previous["version"]
        payload_digest = previous["payload_manifest_sha256"]
        if not isinstance(version, str) or not isinstance(payload_digest, str):
            raise ValueError("release identity fields must be strings")
        manifest = finalize_release_directory(
            root,
            version=version,
            payload_manifest_sha256=payload_digest,
        )
    except (OSError, KeyError, ValueError, json.JSONDecodeError, RuntimeError) as error:
        print(f"nightfalcon release finalization failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
