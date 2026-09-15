#!/usr/bin/env bash
set -euo pipefail

WRAPPER="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
TEMP_RUN_ROOT=""
TEMP_CODEX_HOME=""
RUNNER_PID=""
RUNNER_PGID=""

usage() {
  cat <<'EOF'
Usage: verify-port.sh [--skip-codex-install] [--append-run] [--check-two-clean-runs]

  --skip-codex-install   Skip the temporary CODEX_HOME install smoke.
  --append-run           Append this run to verification-results.json.
  --check-two-clean-runs Require two equal, complete, source-bound PASS runs.
EOF
}

skip_codex_install=false
check_only=false
for arg in "$@"; do
  case "$arg" in
    --help|-h) usage; exit 0 ;;
    --skip-codex-install) skip_codex_install=true ;;
    --check-two-clean-runs) check_only=true ;;
    --append-run) ;;
    *) echo "ERROR: Unknown argument: $arg" >&2; usage >&2; exit 1 ;;
  esac
done

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "ERROR: Python interpreter not found: $PYTHON_BIN" >&2
  exit 1
fi
export PYTHONDONTWRITEBYTECODE=1
SELF_PGID="$("$PYTHON_BIN" -B -c 'import os; print(os.getpgrp())')"

cleanup() {
  if [[ -n "$TEMP_RUN_ROOT" && -d "$TEMP_RUN_ROOT" ]]; then
    rm -rf "$TEMP_RUN_ROOT"
  fi
}

terminate_runner() {
  local signal_name="$1"
  local exit_status="$2"
  trap - INT TERM
  if [[ -n "$RUNNER_PID" ]]; then
    # The launcher calls setsid() before it execs the verification driver, so
    # RUNNER_PID is also the dedicated process-group ID. Signal both forms to
    # close the small launch race: before setsid only the PID exists; after it,
    # the negative PGID reaches the driver and every descendant. Never target
    # the wrapper's own group.
    if [[ -n "$RUNNER_PGID" && "$RUNNER_PGID" != "$SELF_PGID" ]]; then
      kill -s "$signal_name" -- "-$RUNNER_PGID" 2>/dev/null || true
    fi
    kill -s "$signal_name" "$RUNNER_PID" 2>/dev/null || true
    if [[ -n "$RUNNER_PGID" && "$RUNNER_PGID" != "$SELF_PGID" ]]; then
      kill -s "$signal_name" -- "-$RUNNER_PGID" 2>/dev/null || true
    fi
    wait "$RUNNER_PID" 2>/dev/null || true
    attempts=0
    while [[ -n "$RUNNER_PGID" ]] && kill -0 -- "-$RUNNER_PGID" 2>/dev/null; do
      attempts=$((attempts + 1))
      if [[ "$attempts" -eq 50 ]]; then
        kill -KILL -- "-$RUNNER_PGID" 2>/dev/null || true
      fi
      sleep 0.02
    done
  fi
  RUNNER_PID=""
  RUNNER_PGID=""
  cleanup
  exit "$exit_status"
}

trap cleanup EXIT
trap 'terminate_runner INT 130' INT
trap 'terminate_runner TERM 143' TERM

runner_args=(--wrapper "$WRAPPER")
if [[ "$skip_codex_install" == false && "$check_only" == false ]]; then
  TEMP_RUN_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/nightfalcon.XXXXXX")"
  TEMP_RUN_ROOT="$(cd "$TEMP_RUN_ROOT" && pwd -P)"
  chmod 700 "$TEMP_RUN_ROOT"
  printf 'nightfalcon-verify-port-v1\n' > "$TEMP_RUN_ROOT/.nightfalcon-verification-root"
  TEMP_CODEX_HOME="$TEMP_RUN_ROOT/codex-home"
  mkdir -m 700 "$TEMP_CODEX_HOME"
  runner_args+=(--runner-temp-root "$TEMP_RUN_ROOT" --temp-codex-home "$TEMP_CODEX_HOME")
fi

# Python provides the portable macOS/Linux setsid launcher; relying on the
# external `setsid` utility would break on a default macOS installation.
"$PYTHON_BIN" -B -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
  "$PYTHON_BIN" -B "$WRAPPER/scripts/run-verification.py" "${runner_args[@]}" "$@" &
RUNNER_PID=$!
RUNNER_PGID="$RUNNER_PID"
set +e
wait "$RUNNER_PID"
runner_status=$?
set -e
RUNNER_PID=""
RUNNER_PGID=""
exit "$runner_status"
