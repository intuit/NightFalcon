#!/usr/bin/env bash
# check-gate.sh — Phase gate enforcement for nightfalcon.
#
# Modes:
#   --next-phase <phase>     Validate preconditions before starting a phase (orchestrator)
#   --enforce-output         Verify current phase wrote its output file (Stop hook)
#   --check-path <path>      Verify a write path is allowed for the current phase (PreToolUse hook)
#   --count-agent-spawn      Count one subagent spawn against the per-session agent
#                            budget; BLOCK when the budget is exhausted (PreToolUse hook
#                            on the Agent/Task tool)
#   --agent-budget-status    Print "AGENT_BUDGET: used=<n> max=<n> remaining=<n>" for
#                            the current session (orchestrator, informational)
#
# Usage:
#   bash check-gate.sh --next-phase <phase> --workspace <path>
#   bash check-gate.sh --enforce-output [--session <id>] --workspace <path>
#   bash check-gate.sh --check-path <path> --workspace <path>
#   bash check-gate.sh --count-agent-spawn --session <id> --workspace <path>
#   bash check-gate.sh --agent-budget-status [--session <id>] --workspace <path>
#
# Agent budget: NIGHTFALCON_MAX_AGENTS (default 150) caps the TOTAL subagent
# spawns per session — phase workers, phase-4 DAs, phase-6 PoC reviewers, all
# of them. The cap sits below the harness's own session subagent limit
# (typically 200) so the run stops under harness control (clean checkpoint +
# resume instructions) instead of dying mid-phase at the session ceiling.
# Counters are per-session files under <workspace>/.agent-spawns/ — a fresh
# session starts with a fresh budget; workspace artifacts persist across
# sessions so the run resumes from state.json.current_phase.
#
# Exit codes:
#   0 — Gate passed
#   1 — Usage error
#   2 — Gate BLOCKED

set -euo pipefail

MODE=""
NEXT_PHASE=""
CHECK_PATH=""
WORKSPACE=""
SESSION_ID=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --next-phase)         MODE="next-phase"; NEXT_PHASE="$2"; shift 2 ;;
    --enforce-output)     MODE="enforce-output"; shift ;;
    --check-path)         MODE="check-path"; CHECK_PATH="$2"; shift 2 ;;
    --count-agent-spawn)  MODE="count-agent-spawn"; shift ;;
    --agent-budget-status) MODE="agent-budget-status"; shift ;;
    --session)            SESSION_ID="${2:-}"; shift 2 ;;
    --workspace)          WORKSPACE="$2"; shift 2 ;;
    *) echo "ERROR: Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$MODE" ]]; then
  echo "ERROR: Must specify --next-phase <phase>, --enforce-output, --check-path <path>, --count-agent-spawn, or --agent-budget-status" >&2
  exit 1
fi

if [[ -z "$WORKSPACE" ]]; then
  echo "ERROR: --workspace is required" >&2
  exit 1
fi

STATE_FILE="$WORKSPACE/state.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOURNAL_TOOL="$SCRIPT_DIR/agent-journal.py"

# ── MODE: next-phase ──────────────────────────────────────────────────────────
[[ "$MODE" != "next-phase" && "$MODE" != "enforce-output" && "$MODE" != "check-path" \
   && "$MODE" != "count-agent-spawn" && "$MODE" != "agent-budget-status" ]] && {
  echo "ERROR: Unknown mode: $MODE" >&2; exit 1
}

# state.json required for all modes
if [[ ! -f "$STATE_FILE" && "$MODE" == "next-phase" ]]; then
  echo "BLOCKED: state.json not found at $STATE_FILE. Run init-review.sh first." >&2
  exit 2
fi

if [[ -f "$STATE_FILE" && ( "$MODE" == "next-phase" || "$MODE" == "enforce-output" ) ]]; then
  if ! python3 "$JOURNAL_TOOL" validate --workspace "$WORKSPACE" >/dev/null; then
    echo "BLOCKED: agent conversation journal validation failed; repair it before continuing." >&2
    exit 2
  fi
fi

# ── Agent budget (per-session subagent spawn cap) ────────────────────────────
# Total spawns per session across ALL phases. Default 150 — deliberately below
# the harness session subagent limit (typically 200) so the run always stops
# on the harness's terms, never the session ceiling's.
AGENT_BUDGET="${NIGHTFALCON_MAX_AGENTS:-150}"
[[ "$AGENT_BUDGET" =~ ^[0-9]+$ ]] || AGENT_BUDGET=150
SPAWN_DIR="$WORKSPACE/.agent-spawns"

agent_spawn_count() {
  # Usage: agent_spawn_count <count-file>; prints 0 if the file is absent.
  local f="$1"
  if [[ -n "$f" && -f "$f" ]]; then
    wc -l < "$f" | tr -d '[:space:]'
  else
    echo 0
  fi
}

# ── MODE: count-agent-spawn (PreToolUse hook on Agent/Task tool) ─────────────
if [[ "$MODE" == "count-agent-spawn" ]]; then
  # Only meter spawns while a run is active in this workspace.
  if [[ ! -f "$STATE_FILE" ]]; then exit 0; fi
  current_phase="$(python3 -c "
import json
try:
    s=json.load(open('$STATE_FILE'))
    print(s.get('current_phase',''))
except: print('')
" 2>/dev/null)"
  [[ -z "$current_phase" || "$current_phase" == "done" ]] && exit 0

  sid="${SESSION_ID:-unknown}"
  mkdir -p "$SPAWN_DIR"
  count_file="$SPAWN_DIR/$sid.count"
  used="$(agent_spawn_count "$count_file")"

  if (( used >= AGENT_BUDGET )); then
    touch "$SPAWN_DIR/$sid.exhausted"
    echo "BLOCKED: NightFalcon agent budget exhausted — $used of $AGENT_BUDGET subagent spawns used this session (cap NIGHTFALCON_MAX_AGENTS=$AGENT_BUDGET, set below the harness session subagent limit so the run pauses under its own control instead of dying at the session ceiling)." >&2
    echo "Do NOT retry this spawn and do NOT perform the subagent's work inline (DA/reviewer isolation still applies). Finish writing outputs for work already completed, log the pause in output/run-log-<DATE>.md, then follow SKILL.md 'Agent budget' — checkpoint and report resume instructions. A fresh session resumes from state.json.current_phase with a fresh budget." >&2
    exit 2
  fi

  # Parallel spawns may race between the count and the append; a small
  # overshoot (≤ batch width) is acceptable — the budget's headroom below the
  # session limit absorbs it.
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) phase=$current_phase" >> "$count_file"
  exit 0
fi

# ── MODE: agent-budget-status (orchestrator, informational) ──────────────────
if [[ "$MODE" == "agent-budget-status" ]]; then
  count_file=""
  if [[ -n "$SESSION_ID" && -f "$SPAWN_DIR/$SESSION_ID.count" ]]; then
    count_file="$SPAWN_DIR/$SESSION_ID.count"
  else
    # No session id available from Bash — the active session's counter is the
    # most recently modified one (one active session per workspace).
    count_file="$(ls -t "$SPAWN_DIR"/*.count 2>/dev/null | head -1 || true)"
  fi
  used="$(agent_spawn_count "$count_file")"
  remaining=$(( AGENT_BUDGET - used ))
  (( remaining < 0 )) && remaining=0
  echo "AGENT_BUDGET: used=$used max=$AGENT_BUDGET remaining=$remaining"
  exit 0
fi

# ── Helpers ───────────────────────────────────────────────────────────────────

# ── Allowed write paths per phase ────────────────────────────────────────────
# Returns allowed path prefixes for the current phase (relative to WORKSPACE).
allowed_write_paths() {
  local phase="$1"
  case "$phase" in
    phase-0) echo "sourcecode/ input/" ;;
    triage-ingest) echo "findings/ input/" ;;
    phase-1) echo "findings/" ;;
    phase-2) echo "findings/" ;;
    phase-3) echo "findings/" ;;
    phase-4) echo "findings/" ;;
    phase-5) echo "findings/" ;;
    phase-6) echo "output/proof_of_concept/" ;;
    phase-7) echo "findings/" ;;
    phase-8) echo "output/" ;;
    *)       echo "" ;;
  esac
}

# ── MODE: enforce-output (Stop hook) ─────────────────────────────────────────
if [[ "$MODE" == "enforce-output" ]]; then
  if [[ ! -f "$STATE_FILE" ]]; then exit 0; fi

  current_phase="$(python3 -c "
import json
try:
    s=json.load(open('$STATE_FILE'))
    print(s.get('current_phase',''))
except: print('')
" 2>/dev/null)"

  [[ -z "$current_phase" || "$current_phase" == "done" ]] && exit 0

  # Agent-budget exhaustion is the one sanctioned mid-queue stop: the session
  # cannot spawn any further subagents, so blocking the stop would trap the
  # orchestrator in a run it cannot advance. Sanctioned when either (a) a
  # spawn was actually blocked (.exhausted marker), or (b) the session's
  # counter shows remaining budget below the current phase's spawn threshold
  # (the proactive pause in SKILL.md "Agent budget" — verified here from the
  # shell-written counter, never on the model's say-so). Both are per-session
  # — a fresh session (fresh budget) is NOT exempted by an old session's state.
  if [[ -n "$SESSION_ID" ]]; then
    if [[ -f "$SPAWN_DIR/$SESSION_ID.exhausted" ]]; then
      exit 0
    fi
    used="$(agent_spawn_count "$SPAWN_DIR/$SESSION_ID.count")"
    threshold=2
    [[ "$current_phase" == "phase-4" || "$current_phase" == "phase-6" ]] && threshold=15
    if (( AGENT_BUDGET - used < threshold )); then
      exit 0
    fi
  fi

  DATE="$(python3 -c "import json; s=json.load(open('$STATE_FILE')); print(s.get('date',''))" 2>/dev/null)"
  SLUGS="$(python3 -c "
import json
s=json.load(open('$STATE_FILE'))
print(' '.join(s.get('repo_slugs',[])))
" 2>/dev/null)"

  # Check that the current phase has written at least one expected output file
  phase_done=true
  case "$current_phase" in
    phase-0)
      [[ -s "$WORKSPACE/output/run-log-${DATE}.md" ]] || phase_done=false ;;
    triage-ingest)
      for slug in $SLUGS; do
        [[ -s "$WORKSPACE/findings/$slug/candidates-${DATE}.md" ]] || phase_done=false
      done ;;
    phase-1)
      for slug in $SLUGS; do
        [[ -s "$WORKSPACE/findings/$slug/dataflow-${DATE}.md" ]] || phase_done=false
      done ;;
    phase-2)
      for slug in $SLUGS; do
        grep -q "## Data Flow Map" "$WORKSPACE/findings/$slug/dataflow-${DATE}.md" 2>/dev/null || phase_done=false
        # Structured projection JSON is also required
        [[ -s "$WORKSPACE/findings/$slug/dataflow-${DATE}.json" ]] || phase_done=false
      done ;;
    phase-3)
      for slug in $SLUGS; do
        [[ -s "$WORKSPACE/findings/$slug/candidates-${DATE}.md" ]] || phase_done=false
      done ;;
    phase-4)
      for slug in $SLUGS; do
        # Only required if candidates exist and need debate
        if grep -q "Debate required: YES" "$WORKSPACE/findings/$slug/candidates-${DATE}.md" 2>/dev/null; then
          [[ -s "$WORKSPACE/findings/$slug/debate-${DATE}.md" ]] || phase_done=false
        fi
      done ;;
    phase-5)
      for slug in $SLUGS; do
        if grep -q "Validation required: YES" "$WORKSPACE/findings/$slug/candidates-${DATE}.md" 2>/dev/null; then
          grep -q "Validation:" "$WORKSPACE/findings/$slug/candidates-${DATE}.md" 2>/dev/null || phase_done=false
        fi
      done ;;
    phase-6)
      for slug in $SLUGS; do
        [[ -s "$WORKSPACE/output/proof_of_concept/$slug/poc-manifest-${DATE}.json" ]] || phase_done=false
      done ;;
    phase-7)
      for slug in $SLUGS; do
        [[ -s "$WORKSPACE/findings/$slug/findings-${DATE}.md" ]] || phase_done=false
      done ;;
    phase-8)
      [[ -s "$WORKSPACE/output/executive-summary-${DATE}.md" ]] || phase_done=false
      [[ -s "$WORKSPACE/output/executive-report-${DATE}.html" ]] || phase_done=false ;;
  esac

  if [[ "$phase_done" == "false" ]]; then
    echo "BLOCKED: Phase '$current_phase' has not written its required output. Complete the phase before stopping." >&2
    exit 2
  fi
  exit 0
fi

# ── MODE: check-path (PreToolUse hook) ───────────────────────────────────────
if [[ "$MODE" == "check-path" ]]; then
  if [[ ! -f "$STATE_FILE" ]]; then exit 0; fi

  current_phase="$(python3 -c "
import json
try:
    s=json.load(open('$STATE_FILE'))
    print(s.get('current_phase',''))
except: print('')
" 2>/dev/null)"

  [[ -z "$current_phase" || "$current_phase" == "done" ]] && exit 0

  # Canonicalize the workspace and target, resolving existing symlink parents.
  # The prospective target itself need not exist yet.
  if ! normalized="$(python3 - "$WORKSPACE" "$CHECK_PATH" <<'PYEOF'
import pathlib
import sys

workspace_raw, target_raw = sys.argv[1:]
if not target_raw or any(character in target_raw for character in ("\x00", "\n", "\r")):
    print("write path is empty or contains an unsupported control character")
    raise SystemExit(1)

raw_path = pathlib.Path(target_raw)
if ".." in raw_path.parts:
    print(f"write path contains forbidden '..' traversal: {target_raw}")
    raise SystemExit(1)

try:
    workspace = pathlib.Path(workspace_raw).resolve(strict=True)
except (OSError, RuntimeError) as exc:
    print(f"workspace cannot be canonicalized: {exc}")
    raise SystemExit(1)
if not workspace.is_dir():
    print(f"workspace is not a directory: {workspace}")
    raise SystemExit(1)

target = raw_path if raw_path.is_absolute() else workspace / raw_path
try:
    target = target.resolve(strict=False)
    relative = target.relative_to(workspace)
except (OSError, RuntimeError, ValueError) as exc:
    print(f"write path escapes workspace: {target_raw} ({exc})")
    raise SystemExit(1)

print(relative.as_posix())
PYEOF
)"; then
    echo "BLOCKED: $normalized" >&2
    exit 2
  fi

  DATE="$(python3 - "$STATE_FILE" <<'PYEOF'
import json
import sys
try:
    with open(sys.argv[1]) as handle:
        state = json.load(handle)
    value = state.get("date", "")
    print(value if isinstance(value, str) else "")
except Exception:
    print("")
PYEOF
)"

  path_has_prefix() {
    local relative="$1" prefix="$2"
    local directory="${prefix%/}"
    [[ "$relative" == "$directory" || "$relative" == "$prefix"* ]]
  }

  if [[ -n "$DATE" && "$normalized" == "output/agent-conversation-${DATE}.md" ]]; then
    echo "BLOCKED: agent conversation journal is append-only; use scripts/agent-journal.py record." >&2
    exit 2
  fi
  if [[ "$normalized" == output/agent-conversation* || "$normalized" == output/agent_conversation* || "$normalized" == output/agentconversation* ]]; then
    echo "BLOCKED: agent conversation journal paths are reserved; use scripts/agent-journal.py record." >&2
    exit 2
  fi

  # State transitions are script-owned. Direct Write/Edit calls must not bypass
  # complete-phase.sh, init-review.sh, or agent-journal.py invariants.
  if [[ "$normalized" == "state.json" ]]; then
    echo "BLOCKED: state.json is script-owned; use packaged lifecycle commands." >&2
    exit 2
  fi

  # User-supplied input remains writable in every phase.
  if path_has_prefix "$normalized" "input/"; then
    exit 0
  fi

  # Every active phase may append only the canonical log for this run.
  if [[ -n "$DATE" && "$normalized" == "output/run-log-${DATE}.md" ]]; then
    exit 0
  fi

  allowed="$(allowed_write_paths "$current_phase")"
  for prefix in $allowed; do
    if path_has_prefix "$normalized" "$prefix"; then
      exit 0
    fi
  done

  echo "BLOCKED: Phase '$current_phase' is not permitted to write to: $normalized" >&2
  echo "Allowed paths for this phase: $allowed" >&2
  exit 2
fi

# ── Helpers (used by --next-phase mode) ──────────────────────────────────────

state_get() {
  local key="$1"
  python3 -c "
import json
try:
    with open('$STATE_FILE') as f:
        s = json.load(f)
    keys = '$key'.split('.')
    v = s
    for k in keys:
        v = v[k]
    print(str(v) if v is not None else '')
except (KeyError, TypeError, FileNotFoundError):
    print('')
" 2>/dev/null
}

phase_completed() {
  local phase="$1"
  python3 -c "
import json
try:
    with open('$STATE_FILE') as f:
        s = json.load(f)
    print(s.get('phase_status', {}).get('$phase', ''))
except: print('')
" 2>/dev/null | grep -q "^completed$"
}

findings_file_exists() {
  local slug="$1" filename="$2"
  local path="$WORKSPACE/findings/$slug/$filename"
  [[ -f "$path" ]] && [[ -s "$path" ]]
}

poc_file_exists() {
  local slug="$1" filename="$2"
  local path="$WORKSPACE/output/proof_of_concept/$slug/$filename"
  [[ -f "$path" ]] && [[ -s "$path" ]]
}

output_file_exists() {
  local filename="$1"
  local path="$WORKSPACE/output/$filename"
  [[ -f "$path" ]] && [[ -s "$path" ]]
}

blocked() {
  echo "BLOCKED: Phase '$NEXT_PHASE' cannot start:" >&2
  for reason in "$@"; do
    echo "  - $reason" >&2
  done
  exit 2
}

# ── Gate checks per phase ────────────────────────────────────────────────────

DATE="$(state_get date)"
SLUGS="$(python3 -c "
import json
try:
    with open('$STATE_FILE') as f:
        s = json.load(f)
    print(' '.join(s.get('repo_slugs', [])))
except: print('')
" 2>/dev/null)"

REASONS=()

# Run mode. "review" (default) = full discovery pipeline from cloned source.
# "triage" = ingest an external findings backlog, skip clone/scope/dataflow
# (phases 0-2) and phase-3 discovery, and enter the pipeline at phase-4
# (adversarial debate) on candidates produced by the triage-ingest step.
# Enforced HERE in code (not by the orchestrator LLM): in triage mode the
# gate does not require phase-0/1/2 outputs, and phase-4 requires the
# ingested candidates file instead of phase-3 completion.
MODE="$(state_get mode)"
[[ -z "$MODE" ]] && MODE="review"

# current_phase, not prerequisite file presence, is the state-machine source
# of truth. Bind every request to the valid order for the selected run mode.
CURRENT_PHASE="$(state_get current_phase)"
case "$MODE" in
  review) VALID_PHASES="phase-0 phase-1 phase-2 phase-3 phase-4 phase-5 phase-6 phase-7 phase-8" ;;
  triage) VALID_PHASES="triage-ingest phase-4 phase-5 phase-6 phase-7 phase-8" ;;
  *) blocked "state.json.mode '$MODE' is invalid; expected review or triage." ;;
esac

phase_in_mode() {
  local wanted="$1" candidate
  for candidate in $VALID_PHASES; do
    [[ "$candidate" == "$wanted" ]] && return 0
  done
  return 1
}

[[ "$CURRENT_PHASE" == "done" ]] && blocked "state.json.current_phase is done; no phase may be replayed."
phase_in_mode "$CURRENT_PHASE" || blocked "state.json.current_phase '$CURRENT_PHASE' is invalid for mode '$MODE'."
phase_in_mode "$NEXT_PHASE" || blocked "requested phase '$NEXT_PHASE' is invalid for mode '$MODE'."
[[ "$NEXT_PHASE" == "$CURRENT_PHASE" ]] || blocked "requested phase '$NEXT_PHASE' does not match state.json.current_phase '$CURRENT_PHASE'."
phase_completed "$NEXT_PHASE" && blocked "phase '$NEXT_PHASE' is already completed and cannot be replayed."

case "$NEXT_PHASE" in

  phase-0)
    # No preconditions — first phase
    ;;

  phase-1)
    if ! phase_completed "phase-0"; then
      REASONS+=("phase-0 (clone) must complete before scope filter. Check run-log-${DATE}.md for clone failures.")
    fi
    # Each slug must have a cloned directory
    for slug in $SLUGS; do
      if [[ ! -d "$WORKSPACE/sourcecode/$slug" ]]; then
        REASONS+=("sourcecode/$slug/ not found — clone may have failed for $slug.")
      fi
    done
    ;;

  phase-2)
    if ! phase_completed "phase-1"; then
      REASONS+=("phase-1 (scope filter) must complete before data flow mapping.")
    fi
    for slug in $SLUGS; do
      if ! findings_file_exists "$slug" "dataflow-${DATE}.md"; then
        REASONS+=("findings/$slug/dataflow-${DATE}.md missing or empty — phase-1 output required.")
      elif ! grep -q "## External Attack Surface" "$WORKSPACE/findings/$slug/dataflow-${DATE}.md" 2>/dev/null; then
        REASONS+=("findings/$slug/dataflow-${DATE}.md missing '## External Attack Surface' section — phase-1 incomplete.")
      fi
      [[ -s "$WORKSPACE/findings/$slug/dependency-inventory-${DATE}.json" ]] || \
        REASONS+=("findings/$slug/dependency-inventory-${DATE}.json missing — phase-1 static library inventory required.")
    done
    ;;

  phase-3)
    if ! phase_completed "phase-2"; then
      REASONS+=("phase-2 (data flow mapping) must complete before issue identification.")
    fi
    for slug in $SLUGS; do
      if ! findings_file_exists "$slug" "dataflow-${DATE}.md"; then
        REASONS+=("findings/$slug/dataflow-${DATE}.md missing — phase-2 output required.")
      elif ! grep -q "## Data Flow Map" "$WORKSPACE/findings/$slug/dataflow-${DATE}.md" 2>/dev/null; then
        REASONS+=("findings/$slug/dataflow-${DATE}.md missing '## Data Flow Map' section — phase-2 incomplete.")
      fi
    done
    topology="$WORKSPACE/findings/cross-repository-topology-${DATE}.json"
    if [[ ! -s "$topology" ]]; then
      REASONS+=("findings/cross-repository-topology-${DATE}.json missing — authoritative post-Phase-2 reconciliation required.")
    else
      topology_args=(--input-dir "$WORKSPACE/findings" --output "$topology" --date "$DATE" --validate)
      for slug in $SLUGS; do topology_args+=(--slug "$slug"); done
      python3 "$SCRIPT_DIR/reconcile-topology.py" "${topology_args[@]}" >/dev/null 2>&1 || \
        REASONS+=("findings/cross-repository-topology-${DATE}.json stale or mismatched to current date/repository set.")
    fi
    ;;

  phase-4)
    if [[ "$MODE" == "triage" ]]; then
      # Triage mode: phases 0-3 are skipped; debate runs on the ingested
      # backlog. Require the triage-ingest step to have produced candidates.
      if ! phase_completed "triage-ingest"; then
        REASONS+=("triage-ingest must complete before adversarial debate (triage mode).")
      fi
    else
      if ! phase_completed "phase-3"; then
        REASONS+=("phase-3 (scoring) must complete before adversarial debate.")
      fi
    fi
    for slug in $SLUGS; do
      if ! findings_file_exists "$slug" "candidates-${DATE}.md"; then
        REASONS+=("findings/$slug/candidates-${DATE}.md missing — $([[ "$MODE" == triage ]] && echo "triage-ingest" || echo "phase-3") output required.")
      fi
    done
    ;;

  triage-ingest)
    # Triage-only entry phase. Requires triage mode and an input findings file.
    if [[ "$MODE" != "triage" ]]; then
      REASONS+=("triage-ingest only runs in triage mode (state.json.mode == 'triage').")
    fi
    # Is there a non-empty backlog file at input/triage-backlog.<ext>? A glob
    # cannot be tested inside [[ -s ... ]] (it does not expand there), so
    # iterate the matches explicitly and look for at least one non-empty file.
    backlog_found=false
    for bf in "$WORKSPACE"/input/triage-backlog.*; do
      [[ -s "$bf" ]] && { backlog_found=true; break; }
    done
    if [[ "$backlog_found" != true ]]; then
      REASONS+=("no triage backlog input found — expected a non-empty input/triage-backlog.* (the findings file to triage).")
    fi
    ;;

  phase-5)
    if ! phase_completed "phase-4"; then
      REASONS+=("phase-4 (debate) must complete before online validation.")
    fi
    for slug in $SLUGS; do
      if ! findings_file_exists "$slug" "debate-${DATE}.md"; then
        REASONS+=("findings/$slug/debate-${DATE}.md missing — phase-4 output required.")
      elif ! grep -q "Final Disposition" "$WORKSPACE/findings/$slug/debate-${DATE}.md" 2>/dev/null; then
        REASONS+=("findings/$slug/debate-${DATE}.md missing 'Final Disposition' — phase-4 incomplete.")
      fi
    done
    ;;

  phase-6)
    # Phase-5 may be skipped if no P0/P1/P2 candidates — check phase-4 OR phase-5 complete
    if ! phase_completed "phase-4" && ! phase_completed "phase-5"; then
      REASONS+=("phase-4 (debate) or phase-5 (validation) must complete before PoC generation.")
    fi
    # NOTE: phase-6 does NOT require sourcecode/<slug>/ to be present. The
    # only source-presence gate is in phase-1 (above). source-check PoCs
    # re-clone the repo in-phase (and at run time, via run-all.sh +
    # input/repo-map-<DATE>.txt), so a cleaned-up sourcecode/ must not block
    # PoC generation. Do NOT add a sourcecode/ existence check here.
    for slug in $SLUGS; do
      if ! findings_file_exists "$slug" "candidates-${DATE}.md"; then
        REASONS+=("findings/$slug/candidates-${DATE}.md missing — required input for PoC generation.")
      fi
    done
    ;;

  phase-7)
    if ! phase_completed "phase-6"; then
      REASONS+=("phase-6 (PoC generation) must complete before findings report.")
    fi
    for slug in $SLUGS; do
      if ! findings_file_exists "$slug" "candidates-${DATE}.md"; then
        REASONS+=("findings/$slug/candidates-${DATE}.md missing — required input for findings report.")
      fi
      if ! poc_file_exists "$slug" "poc-manifest-${DATE}.json"; then
        REASONS+=("output/proof_of_concept/$slug/poc-manifest-${DATE}.json missing — phase-6 output required.")
      fi
    done
    ;;

  phase-8)
    if ! phase_completed "phase-7"; then
      REASONS+=("phase-7 (findings report) must complete before executive summary.")
    fi
    for slug in $SLUGS; do
      if ! findings_file_exists "$slug" "findings-${DATE}.md"; then
        REASONS+=("findings/$slug/findings-${DATE}.md missing — phase-7 output required.")
      elif ! grep -q "## Findings Summary" "$WORKSPACE/findings/$slug/findings-${DATE}.md" 2>/dev/null; then
        REASONS+=("findings/$slug/findings-${DATE}.md missing '## Findings Summary' table — phase-7 incomplete.")
      fi
    done
    ;;

  *)
    echo "ERROR: Unknown phase: $NEXT_PHASE" >&2
    exit 1
    ;;
esac

if [[ ${#REASONS[@]} -gt 0 ]]; then
  blocked "${REASONS[@]}"
fi

echo "Gate passed: '$NEXT_PHASE' may proceed."
exit 0
