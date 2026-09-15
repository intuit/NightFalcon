#!/usr/bin/env bash
# complete-phase.sh — Delegate lock-owned validation and acceptance to the journal engine.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PHASE=""
WORKSPACE=""
CHECK_SLUG=""
SHORTCUT_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --phase) PHASE="$2"; shift 2 ;;
    --workspace) WORKSPACE="$2"; shift 2 ;;
    --check-slug) CHECK_SLUG="$2"; shift 2 ;;
    --skip-candidates|--no-debate-needed|--no-validation-needed|--no-poc-needed)
      SHORTCUT_ARGS+=("$1"); shift ;;
    *) echo "ERROR: Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$PHASE" || -z "$WORKSPACE" ]]; then
  echo "ERROR: --phase and --workspace are required" >&2
  exit 1
fi

if [[ -n "$CHECK_SLUG" ]]; then
  if [[ ${#SHORTCUT_ARGS[@]} -gt 0 ]]; then
    echo "BLOCKED: --check-slug is read-only and cannot be combined with phase shortcut flags" >&2
    exit 2
  fi
  exec bash "$SCRIPT_DIR/validate-phase.sh" \
    --phase "$PHASE" --workspace "$WORKSPACE" --check-slug "$CHECK_SLUG"
fi

TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
ACCEPT_ARGS=(accept-phase --workspace "$WORKSPACE" --phase "$PHASE" --timestamp "$TIMESTAMP")
if [[ ${#SHORTCUT_ARGS[@]} -gt 0 ]]; then
  ACCEPT_ARGS+=("${SHORTCUT_ARGS[@]}")
fi
python3 "$SCRIPT_DIR/agent-journal.py" "${ACCEPT_ARGS[@]}"
echo "Phase '$PHASE' complete."
