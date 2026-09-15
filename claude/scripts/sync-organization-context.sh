#!/usr/bin/env bash
# Import a schema-v2 organization context provider. No implicit personal path,
# package mutation, or deletion. Source and destination must be explicit.

set -euo pipefail

SOURCE=""
DESTINATION="${NIGHTFALCON_CONTEXT_ROOT:-}"
DRY_RUN=false
DELETE=false
ALLOW_PACKAGE_DESTINATION=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE="${2:-}"; shift 2 ;;
    --destination) DESTINATION="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --delete) DELETE=true; shift ;;
    --allow-package-destination) ALLOW_PACKAGE_DESTINATION=true; shift ;;
    -h|--help)
      echo "Usage: $0 --source PATH [--destination PATH] [--dry-run] [--delete] [--allow-package-destination]"
      exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$SOURCE" && -d "$SOURCE" ]] || { echo "ERROR: --source must name a provider directory" >&2; exit 2; }
[[ -n "$DESTINATION" ]] || { echo "ERROR: --destination or NIGHTFALCON_CONTEXT_ROOT is required" >&2; exit 2; }
[[ -f "$SOURCE/_graph.json" ]] || { echo "ERROR: source _graph.json is required" >&2; exit 2; }

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PLUGIN_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
SOURCE="$(cd -- "$SOURCE" && pwd -P)"
mkdir -p "$DESTINATION"
DESTINATION="$(cd -- "$DESTINATION" && pwd -P)"
[[ "$SOURCE" != "$DESTINATION" ]] || { echo "ERROR: source and destination must differ" >&2; exit 2; }

PACKAGE_DESTINATION="$PLUGIN_ROOT/references/organization_context"
if [[ "$DESTINATION" == "$PACKAGE_DESTINATION" && "$ALLOW_PACKAGE_DESTINATION" != true ]]; then
  echo "ERROR: refusing to import private context into package; use external destination or explicit --allow-package-destination" >&2
  exit 2
fi

RSYNC_OPTIONS=(-a --itemize-changes
  --exclude='.git' --exclude='.git/**'
  --exclude='.claude' --exclude='.claude/**'
  --exclude='.codex' --exclude='.codex/**'
  --exclude='.cursor' --exclude='.cursor/**'
  --exclude='.obsidian' --exclude='.obsidian/**'
  --exclude='Prompts' --exclude='Prompts/**'
  --exclude='.DS_Store'
  --filter='protect /README.md'
  --filter='protect /schema.json')
[[ "$DRY_RUN" == true ]] && RSYNC_OPTIONS+=(--dry-run)
[[ "$DELETE" == true ]] && RSYNC_OPTIONS+=(--delete)

rsync "${RSYNC_OPTIONS[@]}" "$SOURCE/" "$DESTINATION/"

if [[ "$DRY_RUN" != true ]]; then
  VALIDATED="$(mktemp "${TMPDIR:-/tmp}/nightfalcon-context.XXXXXX.json")"
  trap 'rm -f "$VALIDATED"' EXIT
  python3 "$PLUGIN_ROOT/scripts/build-context-graph.py" \
    --root "$DESTINATION" --source "$DESTINATION/_graph.json" --output "$VALIDATED"
  echo "Context provider imported and validated: $DESTINATION"
fi
