#!/usr/bin/env bash
# init-review.sh — Initialize workspace and state.json for a security review run.
#
# Usage:
#   bash init-review.sh --workspace <path> --date <YYYY-MM-DD> [--model-pin-file <path>] [--slug <repo-slug>]...
#
# Model policy: recorded identity is advisory provenance only. NightFalcon
# omits model and reasoning overrides, so every new subagent uses the user's
# current Cursor selection; already-running agents keep their launch model.
#
# Exit codes:
#   0 — Success
#   1 — Usage error

set -euo pipefail

WORKSPACE=""
DATE=""
MODEL_PIN_FILE=""
MODE="review"
SLUGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace) WORKSPACE="$2"; shift 2 ;;
    --date)      DATE="$2"; shift 2 ;;
    --model-pin-file) MODEL_PIN_FILE="$2"; shift 2 ;;
    --model) echo "ERROR: --model is not accepted; model provenance comes from the optional SessionStart --model-pin-file" >&2; exit 1 ;;
    --mode)      MODE="$2"; shift 2 ;;
    --slug)      SLUGS+=("$2"); shift 2 ;;
    *) echo "ERROR: Unknown argument: $1" >&2; exit 1 ;;
  esac
done

# Triage mode must have at least one repo slug to file candidates under, since
# it skips phase-0 (which is where review mode registers slugs). Require it
# here so the run fails fast at init rather than dead-ending at triage-ingest.
if [[ "$MODE" == "triage" && "${#SLUGS[@]}" -ne 1 ]]; then
  echo "ERROR: triage mode requires exactly one --slug <repo-slug>." >&2
  exit 1
fi

if [[ "${#SLUGS[@]}" -gt 0 ]] && ! python3 - "${SLUGS[@]}" <<'PY'
import re, sys
slugs = sys.argv[1:]
if len(slugs) != len(set(slugs)):
    raise SystemExit("ERROR: --slug values must be unique")
for slug in slugs:
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?", slug):
        raise SystemExit(f"ERROR: invalid repository slug: {slug}")
PY
then
  exit 1
fi

# Run mode: "review" (default, full discovery pipeline) or "triage" (ingest an
# external findings backlog and re-adjudicate from phase-4). Persisted in
# state.json so the gate scripts enforce the correct phase order in code.
case "$MODE" in
  review|triage) ;;
  *) echo "ERROR: --mode must be 'review' or 'triage' (got '$MODE')" >&2; exit 1 ;;
esac

if [[ -z "$WORKSPACE" || -z "$DATE" ]]; then
  echo "ERROR: --workspace and --date are required" >&2
  exit 1
fi

STATE_FILE="$WORKSPACE/state.json"
if [[ -f "$STATE_FILE" ]]; then
  if ! RESUME_DATE="$(python3 - "$STATE_FILE" <<'PY'
import json, re, sys
try:
    value = json.load(open(sys.argv[1])).get("date")
except (OSError, ValueError, AttributeError) as exc:
    raise SystemExit(f"ERROR: cannot read authoritative resume date: {exc}")
if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
    raise SystemExit("ERROR: state.json.date is missing or invalid")
print(value)
PY
)"; then
    exit 1
  fi
  DATE="$RESUME_DATE"
  if [[ "${#SLUGS[@]}" -gt 0 ]] && ! python3 - "$STATE_FILE" "${SLUGS[@]}" <<'PY'
import json, sys
try:
    recorded = json.load(open(sys.argv[1])).get("repo_slugs")
except (OSError, ValueError, AttributeError) as exc:
    raise SystemExit(f"ERROR: cannot read authoritative resume scope: {exc}")
supplied = sys.argv[2:]
if not isinstance(recorded, list) or not all(isinstance(item, str) for item in recorded):
    raise SystemExit("ERROR: state.json.repo_slugs is missing or invalid")
if len(recorded) != len(set(recorded)) or set(recorded) != set(supplied):
    raise SystemExit("ERROR: supplied repository scope differs from authoritative state repo_slugs")
PY
  then
    exit 1
  fi
fi

# Model identity is best-effort initialization provenance, never a gate. Resume
# bypasses pin parsing entirely so every existing state.json stays byte-for-byte
# unchanged. A fresh run uses a valid SessionStart pin when available and
# otherwise records an explicit unknown/unavailable provenance result.
# Recorded value never selects, pins, retries, or routes a spawn.
MODEL_VALUE="unknown"
MODEL_SESSION_ID="unknown"
MODEL_SOURCE="unavailable"
MODEL_PROVENANCE="unavailable"
if [[ ! -f "$STATE_FILE" && -n "$MODEL_PIN_FILE" && -f "$MODEL_PIN_FILE" ]]; then
  PIN_VALUES="$(python3 -c 'import json,sys
d=json.load(open(sys.argv[1])); values=(d.get("model"),d.get("session_id"),d.get("source"))
if d.get("schema_version") != "1" or not isinstance(d.get("schema_version"),str): raise ValueError("unsupported pin schema")
if not all(isinstance(v,str) and v and not any(c in v for c in "\t\r\n") for v in values): raise ValueError("invalid pin values")
if values[2] != "SessionStart": raise ValueError("invalid pin source")
print("\t".join(values))' "$MODEL_PIN_FILE" 2>/dev/null)" || PIN_VALUES=""
  if [[ -n "$PIN_VALUES" ]]; then
    IFS=$'\t' read -r MODEL_VALUE MODEL_SESSION_ID MODEL_SOURCE <<< "$PIN_VALUES"
    MODEL_PROVENANCE="available"
  fi
fi

MODEL_JSON="$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$MODEL_VALUE")"
MODEL_SESSION_JSON="$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$MODEL_SESSION_ID")"
MODEL_SOURCE_JSON="$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$MODEL_SOURCE")"
MODEL_PROVENANCE_JSON="$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$MODEL_PROVENANCE")"
MODEL_SELECTED_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
MODEL_SELECTED_AT_JSON="$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$MODEL_SELECTED_AT")"
MODEL_HISTORY_JSON="[]"
if [[ "$MODEL_PROVENANCE" == "available" ]]; then
  MODEL_HISTORY_JSON="$(python3 -c 'import json,sys
print(json.dumps([{"model":sys.argv[1],"session_id":sys.argv[2],"source":sys.argv[3],"selected_at":sys.argv[4]}], indent=2))' "$MODEL_VALUE" "$MODEL_SESSION_ID" "$MODEL_SOURCE" "$MODEL_SELECTED_AT")"
fi

mkdir -p "$WORKSPACE/input"
mkdir -p "$WORKSPACE/sourcecode"
mkdir -p "$WORKSPACE/findings"
mkdir -p "$WORKSPACE/output"
mkdir -p "$WORKSPACE/output/proof_of_concept"

# Mark this workspace as an active NightFalcon (Cursor) review so the runtime
# hooks enforce phase gates only inside initialized review workspaces.
printf 'nightfalcon\n' > "$WORKSPACE/.nightfalcon-review"

# Record the NightFalcon (Cursor) port version in run metadata when the port
# root is supplied by the orchestrator. The Cursor port has no plugin manifest;
# it reads a plain VERSION file at the port root, defaulting to "unknown".
PLUGIN_VERSION="unknown"
if [[ -n "${PLUGIN_ROOT:-}" && -f "${PLUGIN_ROOT}/VERSION" ]]; then
  PLUGIN_VERSION="$(tr -d '[:space:]' < "${PLUGIN_ROOT}/VERSION" 2>/dev/null || echo unknown)"
  [[ -z "$PLUGIN_VERSION" ]] && PLUGIN_VERSION="unknown"
fi

# Initialize the workspace as a git repo so complete-phase.sh's
# per-phase checkpoint commits actually persist. Without this,
# `git rev-parse --git-dir` fails and every phase boundary's commit
# silently no-ops — state.json.git_checkpoints stays empty for the
# whole run, and the eight phase-boundary recovery points that are
# documented in SKILL.md don't exist on disk.
#
# .gitignore excludes the cloned source trees under sourcecode/ —
# committing 20 repos × ~30 MB each would balloon the workspace
# repo to ~600 MB on the first phase-0 commit. Findings, output,
# and the receipt artifacts ARE committed (that's the audit trail).
if [[ ! -d "$WORKSPACE/.git" ]]; then
  (
    cd "$WORKSPACE"
    git init -q
    git config user.email "security-review@localhost"
    git config user.name "security-review"
    cat > .gitignore <<'GITIGNORE'
# Cloned source trees are large and not part of the audit trail.
# Phase-0 clones them; phase-8 cleanup deletes them. They never need
# to be in git.
sourcecode/
GITIGNORE
    git add .gitignore
    git commit -q --allow-empty -m "[security-review: phase-0 start]"
    echo "Initialized $WORKSPACE as a git repo (sourcecode/ excluded). Phase boundaries will be git-checkpointed."
  )
else
  echo "$WORKSPACE is already a git repo — leaving alone."
fi

# Only initialize if state doesn't already exist
if [[ ! -f "$STATE_FILE" ]]; then
  # Starting phase depends on mode: full review begins at clone (phase-0);
  # triage begins at the ingest step (clone/scope/dataflow/scoring are skipped).
  START_PHASE="phase-0"
  REPO_SLUGS_JSON="[]"
  if [[ "${#SLUGS[@]}" -gt 0 ]]; then
    REPO_SLUGS_JSON="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${SLUGS[@]}")"
  fi
  if [[ "$MODE" == "triage" ]]; then
    START_PHASE="triage-ingest"
    # Triage skips phase-0, so seed the slug now (validated non-empty above).
  fi
  cat > "$STATE_FILE" <<EOF
{
  "date": "$DATE",
  "model": $MODEL_JSON,
  "model_policy": "user-selected",
  "model_provenance": $MODEL_PROVENANCE_JSON,
  "model_history": $MODEL_HISTORY_JSON,
  "plugin_version": "$PLUGIN_VERSION",
  "mode": "$MODE",
  "current_phase": "$START_PHASE",
  "phase_status": {},
  "repo_slugs": $REPO_SLUGS_JSON,
  "git_checkpoints": [],
  "history": []
}
EOF
  echo "Initialized state.json at $STATE_FILE"
else
  echo "state.json already exists at $STATE_FILE — skipping init."
fi

# Session manifest — run-provenance metadata at output/session-manifest.json.
# Records when the run started, model identity, repos input file, and
# (filled in incrementally by complete-phase.sh) phases_completed +
# ended_at. Cheap and useful for "compare runs" or "regression suite"
# tooling later.
MANIFEST_FILE="$WORKSPACE/output/session-manifest.json"
mkdir -p "$WORKSPACE/output"
if [[ ! -f "$MANIFEST_FILE" ]]; then
  TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  cat > "$MANIFEST_FILE" <<EOF
{
  "schema_version": "1",
  "started_at": "$TIMESTAMP",
  "ended_at": null,
  "date": "$DATE",
  "plugin": {
    "name": "nightfalcon",
    "version": "$PLUGIN_VERSION"
  },
  "model": $MODEL_JSON,
  "model_policy": "user-selected",
  "model_provenance": $MODEL_PROVENANCE_JSON,
  "workspace": "$WORKSPACE",
  "repos_input": "input/repos-$DATE.txt",
  "phases_completed": []
}
EOF
  echo "Initialized session manifest at $MANIFEST_FILE"
else
  echo "session-manifest.json already exists at $MANIFEST_FILE — skipping init."
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT_NAME="cursor"
JOURNAL_TOOL="$SCRIPT_DIR/agent-journal.py"
python3 "$JOURNAL_TOOL" init --workspace "$WORKSPACE" --date "$DATE" --port "$PORT_NAME"
python3 "$JOURNAL_TOOL" reconcile --workspace "$WORKSPACE"
