#!/usr/bin/env bash
set -euo pipefail

WRAPPER="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
SOURCE="$(cd "$WRAPPER/.." && pwd -P)"
DEST="$WRAPPER/plugins/nightfalcon"

git -C "$SOURCE" ls-files -z phases references scripts |
while IFS= read -r -d '' relative; do
  mkdir -p "$DEST/$(dirname "$relative")"
  cp -p "$SOURCE/$relative" "$DEST/$relative"
done
