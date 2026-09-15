#!/usr/bin/env bash
# edit-log.sh — Cursor afterFileEdit hook for NightFalcon (ADVISORY ONLY).
#
# Cursor's afterFileEdit hook is OBSERVATIONAL: it fires AFTER an edit and
# cannot block it (unlike Claude Code's PreToolUse Write/Edit guard). So this
# hook cannot enforce the per-phase write-path scope. Instead it records any
# out-of-scope edit as a one-line advisory entry in the run log, so a human
# reviewing the run can see when the agent wrote outside the current phase's
# permitted paths. Real write enforcement is not possible here; the phase gate
# scripts (run through the shell, guarded by gate-guard.sh) remain the
# authoritative enforcement mechanism.
#
# Cursor hook contract: JSON on stdin, JSON on stdout. This hook always
# returns {} (no permission/blocking fields are valid for afterFileEdit).
#
# Activation: dedicated marker plus canonical state.json in resolved workspace.

set -uo pipefail

emit_ok() { printf '{}\n'; exit 0; }

payload="$(cat 2>/dev/null || true)"
hook_dir="$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P)"
ws="$(printf '%s' "$payload" | python3 "$hook_dir/workspace.py" 2>/dev/null)"
state="$ws/state.json"
[ -z "$ws" ] && emit_ok

read -r file_path_encoded current_phase date <<EOF
$(python3 - "$payload" "$state" <<'PY' 2>/dev/null
import base64, json, sys
try:
    p = json.loads(sys.argv[1] or "{}")
except Exception:
    p = {}
fp = p.get("file_path", "") or ""
try:
    s = json.load(open(sys.argv[2]))
except Exception:
    s = {}
encoded = base64.urlsafe_b64encode(fp.encode("utf-8")).decode("ascii")
print(encoded, s.get("current_phase", ""), s.get("date", ""))
PY
)
EOF
file_path="$(python3 - "$file_path_encoded" <<'PY' 2>/dev/null || true
import base64, sys
try:
    print(base64.urlsafe_b64decode(sys.argv[1].encode("ascii")).decode("utf-8"), end="")
except (UnicodeError, ValueError):
    pass
PY
)"

[ -z "${current_phase:-}" ] && emit_ok
[ -z "${file_path:-}" ] && emit_ok

# Direct edits to the canonical journal have already bypassed its append-only
# hash-chain writer. afterFileEdit cannot undo or block the write, so return an
# explicit stop follow-up that names the only supported repair/validation CLI.
is_journal="$(python3 - "$ws" "$file_path" "$date" <<'PY' 2>/dev/null || true
import pathlib
import re
import sys

workspace_raw, target_raw, date = sys.argv[1:]
if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
    raise SystemExit(0)
try:
    workspace = pathlib.Path(workspace_raw).resolve(strict=True)
    target = pathlib.Path(target_raw)
    target = target if target.is_absolute() else workspace / target
    expected = workspace / "output" / f"agent-conversation-{date}.md"
    if target.resolve(strict=False) == expected.resolve(strict=False):
        print("yes")
except (OSError, RuntimeError, ValueError):
    pass
PY
)"
if [ "$is_journal" = "yes" ]; then
  python3 - <<'PY' 2>/dev/null || printf '{"followup_message":"Stop: NightFalcon journal integrity violation detected."}\n'
import json
print(json.dumps({
    "followup_message": (
        "Stop: NightFalcon journal integrity violation detected after a direct edit. "
        "Do not edit the journal again. Use scripts/agent-journal.py record for "
        "append operations, then run scripts/agent-journal.py validate before continuing."
    )
}))
PY
  exit 0
fi

[ "${current_phase}" = "done" ] && emit_ok

# Allowed write-path prefixes per phase — MUST match allowed_write_paths() in
# scripts/check-gate.sh (PoC tree lives under output/proof_of_concept/).
allowed_for() {
  case "$1" in
    phase-0)       echo "output/run-log sourcecode/ input/" ;;
    triage-ingest) echo "findings/ input/" ;;
    phase-1|phase-2|phase-3|phase-4|phase-5|phase-7) echo "findings/" ;;
    phase-6)       echo "output/proof_of_concept/" ;;
    phase-8)       echo "output/" ;;
    *)             echo "" ;;
  esac
}

# Preserve lexical path for diagnostics, including parent segments supplied by
# Cursor. Use canonical path only for scope decisions.
display_workspace="${SECURITY_REVIEW_WORKSPACE:-$ws}"
display_norm="${file_path#$display_workspace/}"
display_norm="${display_norm#./}"

# Normalize real paths before making workspace-relative. macOS commonly exposes
# /var through /private/var; string-prefix stripping misclassifies those paths.
norm="$(python3 - "$ws" "$file_path" <<'PY' 2>/dev/null || printf '%s' "$file_path"
from pathlib import Path
import sys

workspace = Path(sys.argv[1]).resolve(strict=False)
target = Path(sys.argv[2])
if not target.is_absolute():
    target = workspace / target
target = target.resolve(strict=False)
try:
    print(target.relative_to(workspace))
except ValueError:
    print(target)
PY
)"

# state.json and input/ are always allowed (mirrors check-gate --check-path).
case "$norm" in
  state.json|input/*) emit_ok ;;
esac

allowed="$(allowed_for "$current_phase")"
for prefix in $allowed; do
  case "$norm" in
    "$prefix"*) emit_ok ;;
  esac
done

# Out of scope for this phase — record an advisory line. Never blocks.
mkdir -p "$ws/output" 2>/dev/null || true
ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)"
{
  echo ""
  echo "## Out-of-scope edits (advisory — Cursor afterFileEdit cannot block)"
  echo "- $ts phase=$current_phase wrote outside allowed paths ($allowed): \`$display_norm\`"
} >> "$ws/output/run-log-${date:-unknown}.md" 2>/dev/null || true

emit_ok
