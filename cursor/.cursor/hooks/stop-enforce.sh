#!/usr/bin/env bash
# stop-enforce.sh — Cursor stop hook for NightFalcon.
#
# Cursor's stop hook fires when the agent finishes a turn. Unlike Claude Code's
# Stop hook (which blocks termination with exit 2), a Cursor stop hook keeps the
# agent working by returning {"followup_message": "..."} on stdout; returning {}
# lets the agent stop. So this hook does not "block" — it nudges the agent to
# finish the current phase when that phase has not yet written its required
# output, mirroring the intent of the Claude/Codex Stop enforcement.
#
# Cursor hook contract: JSON on stdin, JSON on stdout.
#   Input:  {"status":"completed|aborted|error","loop_count":N, ...}
#   Output: {"followup_message":"..."} to continue | {} to end.
#
# Activation: dedicated marker plus canonical state.json in resolved workspace.

set -uo pipefail

end() { printf '{}\n'; exit 0; }
followup() {
  python3 - "$1" <<'PY' 2>/dev/null || printf '{}\n'
import json, sys
print(json.dumps({"followup_message": sys.argv[1]}))
PY
  exit 0
}

payload="$(cat 2>/dev/null || true)"
hook_dir="$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P)"
ws="$(printf '%s' "$payload" | python3 "$hook_dir/workspace.py" 2>/dev/null)"
state="$ws/state.json"
[ -z "$ws" ] && end

# Never fight an aborted/error stop, and cap follow-ups so we cannot loop
# forever (Cursor also enforces loop_limit in hooks.json; this is a second
# belt). loop_count is provided by Cursor in the payload.
read -r status loop_count current_phase date slugs <<EOF
$(python3 - "$payload" "$state" <<'PY' 2>/dev/null
import json, sys
try:
    p = json.loads(sys.argv[1] or "{}")
except Exception:
    p = {}
try:
    s = json.load(open(sys.argv[2]))
except Exception:
    s = {}
print(p.get("status", "completed"),
      p.get("loop_count", 0),
      s.get("current_phase", ""),
      s.get("date", ""),
      ",".join(s.get("repo_slugs", [])) or "-")
PY
)
EOF

[ "${status:-completed}" != "completed" ] && end
[ -z "${current_phase:-}" ] && end
[ "${current_phase}" = "done" ] && end
# Stop nudging after 3 follow-ups to avoid an infinite loop.
case "${loop_count:-0}" in ''|*[!0-9]*) loop_count=0 ;; esac
[ "${loop_count}" -ge 3 ] && end

D="${date:-unknown}"
IFS=',' read -r -a SLUGS <<< "${slugs:--}"

# Has the current phase written its required output? (Mirrors the Claude/Codex
# Stop-hook checks; PoC tree is under output/proof_of_concept/.)
missing=""
check_slug() { # $1=relative path under workspace
  [ -s "$ws/$1" ] || missing="yes"
}
case "$current_phase" in
  phase-0)  [ -s "$ws/output/run-log-${D}.md" ] || missing="yes" ;;
  triage-ingest)
    for s in "${SLUGS[@]}"; do [ "$s" = "-" ] && continue; check_slug "findings/$s/candidates-${D}.md"; done ;;
  phase-1)  for s in "${SLUGS[@]}"; do [ "$s" = "-" ] && continue; check_slug "findings/$s/dataflow-${D}.md"; done ;;
  phase-2)  for s in "${SLUGS[@]}"; do [ "$s" = "-" ] && continue; check_slug "findings/$s/dataflow-${D}.json"; done ;;
  phase-3)  for s in "${SLUGS[@]}"; do [ "$s" = "-" ] && continue; check_slug "findings/$s/candidates-${D}.md"; done ;;
  phase-6)  for s in "${SLUGS[@]}"; do [ "$s" = "-" ] && continue; check_slug "output/proof_of_concept/$s/poc-manifest-${D}.json"; done ;;
  phase-7)  for s in "${SLUGS[@]}"; do [ "$s" = "-" ] && continue; check_slug "findings/$s/findings-${D}.md"; done ;;
  phase-8)
    [ -s "$ws/output/executive-summary-${D}.md" ] || missing="yes"
    [ -s "$ws/output/executive-report-${D}.html" ] || missing="yes" ;;
  *) : ;;  # phase-4/5 completion is verified by complete-phase.sh, not here
esac

if [ -n "$missing" ]; then
  followup "NightFalcon: phase '$current_phase' has not written its required output for every repo. Do not stop — finish the phase: run the phase worker for any repo still missing its artifact, then run scripts/complete-phase.sh --phase $current_phase --workspace \"$ws\". If a phase legitimately has no work, pass the documented skip flag (--skip-candidates / --no-debate-needed / --no-validation-needed / --no-poc-needed)."
fi

end
