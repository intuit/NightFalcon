#!/usr/bin/env bash
# validate-phase.sh — Validate phase output without mutating journal or state.
#
# Usage:
#   bash complete-phase.sh --phase <phase> --workspace <path> [--skip-candidates]
#   bash complete-phase.sh --phase <phase> --workspace <path> --check-slug <slug>
#
# Options:
#   --phase <name>        Phase name to complete (required)
#   --workspace <path>    Workspace root (required)
#   --check-slug <slug>   Validate one current-phase repo without changing state
#   --skip-candidates     For phase-3: mark complete even with no candidates (zero findings)
#   --no-debate-needed    For phase-4: mark complete when no P0/CRIT/HIGH needed debate
#   --no-validation-needed  For phase-5: mark complete when no eligible candidates
#   --no-poc-needed       For phase-6: mark complete when no eligible P0/P1/P2 candidates (none exist, or all were DISMISSED in debate)
#
# Exit codes:
#   0 — Success
#   1 — Usage error
#   2 — BLOCKED (output missing or invalid)

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

PHASE=""
WORKSPACE=""
SKIP_CANDIDATES=false
NO_DEBATE_NEEDED=false
NO_VALIDATION_NEEDED=false
NO_POC_NEEDED=false
CHECK_SLUG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --phase)                PHASE="$2"; shift 2 ;;
    --workspace)            WORKSPACE="$2"; shift 2 ;;
    --check-slug)           CHECK_SLUG="$2"; shift 2 ;;
    --skip-candidates)      SKIP_CANDIDATES=true; shift ;;
    --no-debate-needed)     NO_DEBATE_NEEDED=true; shift ;;
    --no-validation-needed) NO_VALIDATION_NEEDED=true; shift ;;
    --no-poc-needed)        NO_POC_NEEDED=true; shift ;;
    *) echo "ERROR: Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$PHASE" || -z "$WORKSPACE" ]]; then
  echo "ERROR: --phase and --workspace are required" >&2
  exit 1
fi

STATE_FILE="$WORKSPACE/state.json"

if [[ ! -f "$STATE_FILE" ]]; then
  echo "BLOCKED: state.json not found. Run init-review.sh first." >&2
  exit 2
fi

blocked() {
  echo "BLOCKED: $1" >&2
  exit 2
}

# Validate lifecycle state before looking at artifacts from this or an older
# run. Completion is valid only at the current pointer and cannot be replayed.
if ! STATE_META="$(python3 - "$STATE_FILE" "$PHASE" <<'PYEOF'
import json
import sys

state_file, requested = sys.argv[1:]
review_order = [f"phase-{index}" for index in range(9)]
triage_order = ["triage-ingest", "phase-4", "phase-5", "phase-6", "phase-7", "phase-8"]
all_phases = set(review_order) | set(triage_order)

try:
    with open(state_file) as handle:
        state = json.load(handle)
except Exception as exc:
    print(f"state.json is not valid JSON: {exc}")
    raise SystemExit(1)
if not isinstance(state, dict):
    print("state.json must contain an object")
    raise SystemExit(1)

mode = state.get("mode", "review") or "review"
if mode not in {"review", "triage"}:
    print(f"state.json.mode {mode!r} is invalid; expected review or triage")
    raise SystemExit(1)
order = triage_order if mode == "triage" else review_order

if requested not in all_phases:
    print(f"requested phase {requested!r} is unknown")
    raise SystemExit(1)
if requested not in order:
    print(f"requested phase {requested!r} is invalid for mode {mode!r}")
    raise SystemExit(1)

current = state.get("current_phase")
if current == "done":
    print("state.json.current_phase is done; completed phases cannot be replayed")
    raise SystemExit(1)
if current not in order:
    print(f"state.json.current_phase {current!r} is invalid for mode {mode!r}")
    raise SystemExit(1)

phase_status = state.get("phase_status", {})
if not isinstance(phase_status, dict):
    print("state.json.phase_status must contain an object")
    raise SystemExit(1)
if phase_status.get(requested) == "completed":
    print(f"phase {requested!r} is already completed and cannot be replayed")
    raise SystemExit(1)
if requested != current:
    print(
        f"requested phase {requested!r} does not match "
        f"state.json.current_phase {current!r}"
    )
    raise SystemExit(1)

date = state.get("date")
slugs = state.get("repo_slugs", [])
if not isinstance(date, str) or not date:
    print("state.json.date must be a non-empty string")
    raise SystemExit(1)
if not isinstance(slugs, list) or not all(isinstance(slug, str) and slug for slug in slugs):
    print("state.json.repo_slugs must be a list of non-empty strings")
    raise SystemExit(1)

print(date)
print(" ".join(slugs))
PYEOF
)"; then
  blocked "$STATE_META"
fi

DATE="$(printf '%s\n' "$STATE_META" | sed -n '1p')"
SLUGS="$(printf '%s\n' "$STATE_META" | sed -n '2p')"
MODE="$(python3 -c "import json; s=json.load(open('$STATE_FILE')); print(s.get('mode') or 'review')" 2>/dev/null)"

[[ "$SKIP_CANDIDATES" == "true" && "$PHASE" != "phase-3" ]] \
  && blocked "--skip-candidates is supported only for phase-3."
[[ "$NO_DEBATE_NEEDED" == "true" && "$PHASE" != "phase-4" ]] \
  && blocked "--no-debate-needed is supported only for phase-4."
[[ "$NO_VALIDATION_NEEDED" == "true" && "$PHASE" != "phase-5" ]] \
  && blocked "--no-validation-needed is supported only for phase-5."
[[ "$NO_POC_NEEDED" == "true" && "$PHASE" != "phase-6" ]] \
  && blocked "--no-poc-needed is supported only for phase-6."

if [[ -n "$CHECK_SLUG" ]]; then
  case "$PHASE" in
    triage-ingest|phase-1|phase-2|phase-3|phase-4|phase-5|phase-6|phase-7) ;;
    *) blocked "--check-slug is valid only for per-repo phases." ;;
  esac
  if [[ "$SKIP_CANDIDATES" == "true" || "$NO_DEBATE_NEEDED" == "true" || \
        "$NO_VALIDATION_NEEDED" == "true" || "$NO_POC_NEEDED" == "true" ]]; then
    blocked "--check-slug is read-only and cannot be combined with phase shortcut flags."
  fi
  if ! printf '%s\n' "$SLUGS" | tr ' ' '\n' | grep -Fxq -- "$CHECK_SLUG"; then
    blocked "--check-slug '$CHECK_SLUG' is not present in state.json.repo_slugs."
  fi
  SLUGS="$CHECK_SLUG"
fi

# Parse candidate records once into canonical tab-separated fields used by
# phase shortcut proofs: finding_id, effective tier, final disposition. The
# disposition may be omitted only by phase-4, which runs before debate assigns
# it. Every later shortcut fails closed on malformed or incomplete records.
candidate_records() {
  local candidate_file="$1" require_disposition="$2"
  python3 - "$candidate_file" "$require_disposition" <<'PYEOF'
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
require_disposition = sys.argv[2] == "true"
if not path.is_file() or path.stat().st_size == 0:
    print(f"{path} missing or empty", file=sys.stderr)
    raise SystemExit(1)

text = path.read_text()
matches = list(re.finditer(r"(?m)^## CANDIDATE-(F-[0-9]+)\b", text))
if not matches:
    if "NO_CANDIDATES" in text:
        raise SystemExit(0)
    print("missing candidate records or NO_CANDIDATES marker", file=sys.stderr)
    raise SystemExit(1)
if "NO_CANDIDATES" in text:
    print("NO_CANDIDATES cannot coexist with candidate records", file=sys.stderr)
    raise SystemExit(1)

field_prefix = r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?"
for index, match in enumerate(matches):
    finding_id = match.group(1)
    end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
    record = text[match.end():end]
    final_tier = re.findall(
        field_prefix + r"Final Tier(?: \(post-debate\))?:(?:\*\*)?\s*(P[0-4])\b",
        record,
    )
    initial_tier = re.findall(
        field_prefix + r"Tier:(?:\*\*)?\s*(P[0-4])\b",
        record,
    )
    dispositions = re.findall(
        field_prefix
        + r"Final Disposition:(?:\*\*)?\s*"
        + r"(CONFIRMED-MODIFIED|CONFIRMED|DISMISSED|NEEDS-REVIEW)\b",
        record,
    )
    tiers = final_tier or initial_tier
    if len(tiers) != 1:
        print(f"{finding_id} must contain exactly one effective Tier", file=sys.stderr)
        raise SystemExit(1)
    if len(dispositions) > 1 or (require_disposition and len(dispositions) != 1):
        print(f"{finding_id} must contain exactly one Final Disposition", file=sys.stderr)
        raise SystemExit(1)
    disposition = dispositions[0] if dispositions else ""
    print(f"{finding_id}\t{tiers[0]}\t{disposition}")
PYEOF
}

finalize_no_debate_candidates() {
  local candidate_file="$1"
  python3 - "$candidate_file" <<'PYEOF'
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
text = path.read_text()
matches = list(re.finditer(r"(?m)^## CANDIDATE-(F-[0-9]+)\b", text))
if not matches:
    raise SystemExit(0)

parts = [text[:matches[0].start()]]
for index, match in enumerate(matches):
    end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
    record = text[match.start():end].rstrip()
    final_tier = re.search(r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?Final Tier(?: \(post-debate\))?:(?:\*\*)?\s*(P[0-4])\b", record)
    initial_tier = re.search(r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?Tier:(?:\*\*)?\s*(P[0-4])\b", record)
    tier = (final_tier or initial_tier).group(1)
    additions = []
    field_prefix = r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?"
    for final_label, initial_label in (
        ("Final CVSS-B Score", "CVSS-B Score"),
        ("Final CVSS Vector", "CVSS Vector"),
        ("Final CVSS Rationale", "CVSS Rationale"),
        ("Final CVSS Calculator", "CVSS Calculator"),
    ):
        if re.search(field_prefix + re.escape(final_label) + r":", record):
            continue
        initial = re.search(
            field_prefix + re.escape(initial_label) + r":(?:\*\*)?\s*(.*?)\s*$",
            record,
        )
        if initial is None or not initial.group(1).rstrip("*").strip():
            print(f"{match.group(1)} missing {initial_label}", file=sys.stderr)
            raise SystemExit(1)
        additions.append(f"{final_label}: {initial.group(1).rstrip('*').strip()}")
    if final_tier is None:
        additions.append(f"Final Tier: {tier}")
    if not re.search(r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?Final Disposition:", record):
        additions.append("Final Disposition: NEEDS-REVIEW")
    if additions:
        record += "\n" + "\n".join(additions)
    parts.append(record + "\n")
path.write_text("".join(parts))
PYEOF
}

# ── Verify expected output files exist per phase ─────────────────────────────

case "$PHASE" in

  phase-0)
    # run-log must exist
    if [[ ! -s "$WORKSPACE/output/run-log-${DATE}.md" ]]; then
      blocked "output/run-log-${DATE}.md missing or empty — phase-0 must write the run log."
    fi
    # At least one slug must have been cloned
    if [[ -z "$SLUGS" ]]; then
      blocked "No repo_slugs in state.json — phase-0 must register cloned repos before completing."
    fi
    for slug in $SLUGS; do
      [[ -d "$WORKSPACE/sourcecode/$slug" ]] || blocked "sourcecode/$slug missing — every registered repository must clone before phase-0 completes."
      git -C "$WORKSPACE/sourcecode/$slug" rev-parse --is-inside-work-tree >/dev/null 2>&1 || blocked "sourcecode/$slug is not a valid Git checkout."
    done
    ;;

  triage-ingest)
    # Triage entry: must have produced a phase-3-shaped candidates file per slug
    # from the external backlog, so the debate phase (phase-4) has input.
    if [[ -z "$SLUGS" ]]; then
      blocked "No repo_slugs in state.json — triage-ingest must register the backlog's repo(s) before completing."
    fi
    for slug in $SLUGS; do
      f="$WORKSPACE/findings/$slug/candidates-${DATE}.md"
      [[ -s "$f" ]] || blocked "findings/$slug/candidates-${DATE}.md missing or empty — triage-ingest must convert the backlog into candidates."
    done
    ;;

  phase-1)
    for slug in $SLUGS; do
      f="$WORKSPACE/findings/$slug/dataflow-${DATE}.md"
      [[ -s "$f" ]] || blocked "findings/$slug/dataflow-${DATE}.md missing or empty — phase-1 must write the scope filter."
      grep -q "## External Attack Surface" "$f" || blocked "findings/$slug/dataflow-${DATE}.md missing '## External Attack Surface' — phase-1 incomplete."
      di="$WORKSPACE/findings/$slug/dependency-inventory-${DATE}.json"
      [[ -s "$di" ]] || blocked "findings/$slug/dependency-inventory-${DATE}.json missing or empty — phase-1 must write static dependency inventory."
      python3 - "$di" <<'PYEOF' || blocked "dependency inventory invalid — require schema_version, dependency_inventory list, package_manager_execution=false, network_access=false, and relative manifest paths."
import json, pathlib, sys
d = json.load(open(sys.argv[1]))
assert str(d.get("schema_version")) == "1"
assert isinstance(d.get("dependency_inventory"), list)
assert isinstance(d.get("manifest_coverage"), list)
assert d.get("manifests_detected") == len(d["manifest_coverage"])
assert d.get("package_manager_execution") is False
assert d.get("network_access") is False
for entry in d["manifest_coverage"]:
    assert isinstance(entry, dict)
    assert entry.get("status") == "parsed"
    path = pathlib.PurePosixPath(entry.get("manifest", ""))
    assert entry.get("manifest") and not path.is_absolute() and ".." not in path.parts
for row in d["dependency_inventory"]:
    assert isinstance(row, dict)
    manifest = row.get("manifest")
    if manifest is not None:
        path = pathlib.PurePosixPath(manifest)
        assert not path.is_absolute() and ".." not in path.parts
    assert row.get("reachability") in {"unknown", "reachable", "not-reachable"}
PYEOF
      # organization context file is mandatory and independent of tenancy. It must be either the structured
      # form (## Controls Detected / ## Applicable standards / ## Platform Services Detected)
      # or the explicit "No organization controls detected." sentinel. The file is optional
      # when multitenant-scope=NO (phase-1 skips this step); the gate only validates content
      # provider form or explicit sentinel. See phases/phase-1.md "Optional organization context provider".
      ic="$WORKSPACE/findings/$slug/organization-context-${DATE}.md"
      [[ -s "$ic" ]] || blocked "findings/$slug/organization-context-${DATE}.md missing or empty — phase-1 must write provider matches or the 'No organization controls detected.' sentinel."
      if ! grep -qE "^## (Controls Detected|Applicable standards|Platform Services Detected)" "$ic" \
           && ! grep -qF "No organization controls detected." "$ic"; then
        blocked "findings/$slug/organization-context-${DATE}.md missing both required section headers and the sentinel — phase-1 must write at least '## Controls Detected' or 'No organization controls detected.' See phases/phase-1.md 'Optional organization context provider'."
      fi
      # OWASP context file — mandatory. Phase-1 must write the structured
      # form (System Kinds Detected / Frameworks In Scope / Applicable Items)
      # OR the explicit sentinel. Unlike organization-context, this runs for every
      # repo because cross_domain ASVS is always in scope.
      oc="$WORKSPACE/findings/$slug/owasp-context-${DATE}.md"
      [[ -s "$oc" ]] || blocked "findings/$slug/owasp-context-${DATE}.md missing or empty — phase-1 must write the OWASP framework dispatch + applicable items. See phases/phase-1.md 'OWASP framework detection + applicable-item extraction'."
      if ! grep -qE "^## (System Kinds Detected|Frameworks In Scope|Applicable Items)" "$oc" \
           && ! grep -qF "No OWASP frameworks in scope." "$oc"; then
        blocked "findings/$slug/owasp-context-${DATE}.md missing both required section headers and the sentinel — phase-1 must write at least '## System Kinds Detected' or 'No OWASP frameworks in scope.'"
      fi
    done
    ;;

  phase-2)
    for slug in $SLUGS; do
      f="$WORKSPACE/findings/$slug/dataflow-${DATE}.md"
      [[ -s "$f" ]] || blocked "findings/$slug/dataflow-${DATE}.md missing or empty — phase-2 must append data flow map."
      grep -q "## Data Flow Map" "$f" || blocked "findings/$slug/dataflow-${DATE}.md missing '## Data Flow Map' — phase-2 incomplete."
      # Structured projection (hybrid prose+enum JSON) — required alongside the markdown
      j="$WORKSPACE/findings/$slug/dataflow-${DATE}.json"
      [[ -s "$j" ]] || blocked "findings/$slug/dataflow-${DATE}.json missing or empty — phase-2 must write the structured projection alongside the markdown. See phases/phase-2.md Output Artifact 2."
      dataflow_check="$(python3 "$SCRIPT_DIR/validate-findings.py" dataflow \
        --input "$j" --slug "$slug" --date "$DATE" 2>&1)" \
        || blocked "findings/$slug/dataflow-${DATE}.json invalid provenance: $dataflow_check"
      # Validate JSON parses and has the expected top-level shape.
      json_check="$(python3 -c "
import json, sys
try:
    d = json.load(open('$j'))
except Exception as e:
    print(f'PARSE_ERROR: {e}'); sys.exit(0)
missing = [k for k in ('schema_version', 'repo_slug', 'flows') if k not in d]
if missing:
    print(f'MISSING_FIELDS: {missing}'); sys.exit(0)
if not isinstance(d['flows'], list):
    print('FLOWS_NOT_LIST'); sys.exit(0)
print('OK')
" 2>&1)"
      if [[ "$json_check" != "OK" ]]; then
        blocked "findings/$slug/dataflow-${DATE}.json invalid: $json_check. Required top-level fields: schema_version, repo_slug, flows (list). See phases/phase-2.md Output Artifact 2."
      fi
    done
    if [[ -z "$CHECK_SLUG" ]]; then
      topology="$WORKSPACE/findings/cross-repository-topology-${DATE}.json"
      topology_args=(--input-dir "$WORKSPACE/findings" --output "$topology" --date "$DATE")
      for slug in $SLUGS; do topology_args+=(--slug "$slug"); done
      python3 "$SCRIPT_DIR/reconcile-topology.py" "${topology_args[@]}" \
        || blocked "cross_repository_topology reconciliation failed after all Phase 2 outputs."
      python3 - "$topology" <<'PYEOF' || blocked "cross_repository_topology output is malformed."
import json, sys
d = json.load(open(sys.argv[1]))
t = d.get("cross_repository_topology")
assert d.get("schema_version") == "2"
assert isinstance(t, dict) and t.get("phase_order") == "after-all-phase-2-before-phase-3"
assert isinstance(t.get("repositories"), list) and isinstance(t.get("edges"), list)
assert all(edge.get("classification") in {"matched", "external", "unresolved"} for edge in t["edges"])
PYEOF
    fi
    ;;

  phase-3)
    if [[ "$SKIP_CANDIDATES" == "true" ]]; then
      # Zero findings across all repos — must have written NO_CANDIDATES marker
      for slug in $SLUGS; do
        f="$WORKSPACE/findings/$slug/candidates-${DATE}.md"
        [[ -s "$f" ]] || blocked "findings/$slug/candidates-${DATE}.md missing — even with zero findings, phase-3 must write the file with NO_CANDIDATES marker."
        grep -q "NO_CANDIDATES" "$f" || blocked "findings/$slug/candidates-${DATE}.md missing NO_CANDIDATES marker — use --skip-candidates only when there are truly zero findings."
        ! grep -q "## CANDIDATE-" "$f" || blocked "findings/$slug/candidates-${DATE}.md is ambiguous — NO_CANDIDATES cannot coexist with candidate sections."
      done
    else
      # Mixed case: some repos have candidates, others have NO_CANDIDATES — both are valid
      for slug in $SLUGS; do
        f="$WORKSPACE/findings/$slug/candidates-${DATE}.md"
        [[ -s "$f" ]] || blocked "findings/$slug/candidates-${DATE}.md missing or empty — phase-3 must write scored candidates or NO_CANDIDATES marker."
        grep -q "## CANDIDATE-" "$f" || grep -q "NO_CANDIDATES" "$f" || blocked "findings/$slug/candidates-${DATE}.md has no CANDIDATE sections or NO_CANDIDATES marker."
        if grep -q "## CANDIDATE-" "$f" && grep -q "NO_CANDIDATES" "$f"; then
          blocked "findings/$slug/candidates-${DATE}.md is ambiguous — NO_CANDIDATES cannot coexist with candidate sections."
        fi
        if grep -q "## CANDIDATE-" "$f"; then
          marker_check="$(python3 - "$f" <<'PYEOF'
import pathlib
import re
import sys

text = pathlib.Path(sys.argv[1]).read_text()
records = re.split(r"(?m)^## CANDIDATE-", text)[1:]
marker_line_re = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?Debate required:(?:\*\*)?\s*(.*?)\s*$"
)
value_re = re.compile(r"^(YES|NO)(?:\s+\((P0/P1/P2|P3/P4)\))?(?:\*\*)?$")
documented_suffix = {"YES": "P0/P1/P2", "NO": "P3/P4"}
for index, record in enumerate(records, 1):
    marker_values = marker_line_re.findall(record)
    if len(marker_values) != 1:
        print(f"candidate {index} must contain exactly one Debate required marker")
        break
    parsed = value_re.fullmatch(marker_values[0])
    if parsed is None:
        print(
            f"candidate {index} has invalid Debate required value; use YES, NO, "
            "YES (P0/P1/P2), or NO (P3/P4)"
        )
        break
    value, suffix = parsed.groups()
    if suffix is not None and suffix != documented_suffix[value]:
        print(f"candidate {index} has mismatched Debate required suffix for {value}")
        break
else:
    print("OK")
PYEOF
          )"
          [[ "$marker_check" == "OK" ]] || blocked "findings/$slug/candidates-${DATE}.md invalid: $marker_check"
          cvss_check="$(python3 "$SCRIPT_DIR/validate-findings.py" candidates \
            --input "$f" --dataflow "$WORKSPACE/findings/$slug/dataflow-${DATE}.json" \
            --slug "$slug" --date "$DATE" 2>&1)" \
            || blocked "findings/$slug/candidates-${DATE}.md invalid CVSS/dataflow provenance: $cvss_check"
        fi
      done
    fi
    ;;

  phase-4)
    DISPO_RE='Final Disposition:[*]*[[:space:]]*(CONFIRMED-MODIFIED|CONFIRMED|DISMISSED|NEEDS-REVIEW)[*]*[[:space:]]*$'
    if [[ "$NO_DEBATE_NEEDED" == "true" ]]; then
      for slug in $SLUGS; do
        candidates="$WORKSPACE/findings/$slug/candidates-${DATE}.md"
        [[ -s "$candidates" ]] || blocked "findings/$slug/candidates-${DATE}.md missing or empty — phase-4 input required."
        records="$(candidate_records "$candidates" false 2>&1)" \
          || blocked "findings/$slug/candidates-${DATE}.md invalid: $records"
        while IFS=$'\t' read -r finding_id tier disposition; do
          [[ -z "$finding_id" ]] && continue
          case "$tier" in
            P0|P1|P2) blocked "--no-debate-needed is invalid: $slug $finding_id ($tier) requires debate." ;;
          esac
        done <<< "$records"
        finalize_no_debate_candidates "$candidates" \
          || blocked "findings/$slug/candidates-${DATE}.md could not be finalized for the no-debate path."
        f="$WORKSPACE/findings/$slug/debate-${DATE}.md"
        if [[ ! -s "$f" ]]; then
          cat > "$f" <<EOF
# Phase 4 Debate — $slug

NO_DEBATE_CANDIDATES
Final Disposition: NEEDS-REVIEW
EOF
        fi
        grep -q "NO_DEBATE_CANDIDATES" "$f" || blocked "findings/$slug/debate-${DATE}.md missing NO_DEBATE_CANDIDATES no-work marker."
        grep -qE "$DISPO_RE" "$f" || blocked "findings/$slug/debate-${DATE}.md missing canonical Final Disposition for no-work artifact."
      done
    else
      # Canonical disposition enum (references/enums.md §2)
      # CONFIRMED | CONFIRMED-MODIFIED | DISMISSED | NEEDS-REVIEW
      # `.*\b` tolerates markdown styling between "Final Disposition" and the verb
      # (e.g., `**Final Disposition:** CONFIRMED`).
      for slug in $SLUGS; do
        candidates="$WORKSPACE/findings/$slug/candidates-${DATE}.md"
        [[ -s "$candidates" ]] || blocked "findings/$slug/candidates-${DATE}.md missing or empty — phase-4 input required."
        f="$WORKSPACE/findings/$slug/debate-${DATE}.md"
        [[ -s "$f" ]] || blocked "findings/$slug/debate-${DATE}.md missing or empty — phase-4 must write debate transcript."
        grep -q "Final Disposition" "$f" || blocked "findings/$slug/debate-${DATE}.md missing 'Final Disposition' — phase-4 incomplete."
        # Validate every Final Disposition line uses the canonical enum.
        # Find any Final Disposition line whose value is NOT in the enum set:
        bad="$(grep -E 'Final Disposition:' "$f" | grep -vE "$DISPO_RE" || true)"
        if [[ -n "$bad" ]]; then
          blocked "findings/$slug/debate-${DATE}.md has Final Disposition line(s) with non-canonical verb. Allowed: CONFIRMED | CONFIRMED-MODIFIED | DISMISSED | NEEDS-REVIEW (see references/enums.md §2). Offending line(s): $(printf '%s' "$bad" | head -3 | tr '\n' '|')"
        fi
      done
    fi
    for slug in $SLUGS; do
      candidates="$WORKSPACE/findings/$slug/candidates-${DATE}.md"
      candidate_args=(--input "$candidates" --post-debate)
      if [[ "$MODE" == "review" ]]; then
        candidate_args+=(--dataflow "$WORKSPACE/findings/$slug/dataflow-${DATE}.json" --slug "$slug" --date "$DATE")
      fi
      post_debate_check="$(python3 "$SCRIPT_DIR/validate-findings.py" candidates \
        "${candidate_args[@]}" 2>&1)" \
        || blocked "findings/$slug/candidates-${DATE}.md invalid post-debate CVSS/provenance: $post_debate_check"
    done
    ;;

  phase-5)
    if [[ "$NO_VALIDATION_NEEDED" == "true" ]]; then
      for slug in $SLUGS; do
        f="$WORKSPACE/findings/$slug/candidates-${DATE}.md"
        records="$(candidate_records "$f" true 2>&1)" \
          || blocked "findings/$slug/candidates-${DATE}.md invalid: $records"
        while IFS=$'\t' read -r finding_id tier disposition; do
          [[ -z "$finding_id" ]] && continue
          if [[ "$tier" =~ ^P[0-2]$ && "$disposition" =~ ^(CONFIRMED|CONFIRMED-MODIFIED|NEEDS-REVIEW)$ ]]; then
            blocked "--no-validation-needed is invalid: $slug $finding_id ($tier, $disposition) is eligible for online validation."
          fi
        done <<< "$records"
      done
    else
      for slug in $SLUGS; do
        f="$WORKSPACE/findings/$slug/candidates-${DATE}.md"
        validation_args=(--input "$f" --post-debate --require-validation)
        if [[ "$MODE" == "review" ]]; then
          validation_args+=(
            --dataflow "$WORKSPACE/findings/$slug/dataflow-${DATE}.json"
            --slug "$slug" --date "$DATE"
          )
        fi
        validation_check="$(python3 "$SCRIPT_DIR/validate-findings.py" candidates \
          "${validation_args[@]}" 2>&1)" \
          || blocked "findings/$slug/candidates-${DATE}.md failed record-level validation result checks: $validation_check"
      done
    fi
    ;;

  phase-6)
    # PoC manifest enum (references/enums.md §14)
    # VALID | NEEDS-REVISION | INVALID
    if [[ "$NO_POC_NEEDED" == "true" ]]; then
      # Auto-stub the empty manifest if missing — matches the phase-4/5
      # skip-flag pattern (no prior subagent work required) while still
      # giving phase-7's gate the file it expects. Idempotent: leaves an
      # emit a deterministic complete skip ledger for downstream provenance.
      for slug in $SLUGS; do
        candidates="$WORKSPACE/findings/$slug/candidates-${DATE}.md"
        records="$(candidate_records "$candidates" true 2>&1)" \
          || blocked "findings/$slug/candidates-${DATE}.md invalid: $records"
        while IFS=$'\t' read -r finding_id tier disposition; do
          [[ -z "$finding_id" ]] && continue
          if [[ "$tier" =~ ^P[0-2]$ && "$disposition" =~ ^(CONFIRMED|CONFIRMED-MODIFIED|NEEDS-REVIEW)$ ]]; then
            blocked "--no-poc-needed is invalid: $slug $finding_id ($tier, $disposition) is eligible for PoC generation."
          fi
        done <<< "$records"
        m="$WORKSPACE/output/proof_of_concept/$slug/poc-manifest-${DATE}.json"
        mkdir -p "$WORKSPACE/output/proof_of_concept/$slug"
        python3 - "$m" "$slug" "$DATE" "$records" <<'PYEOF'
import json
import pathlib
import sys

manifest_path, slug, date, raw_records = sys.argv[1:]
skipped = []
for line in raw_records.splitlines():
    if not line:
        continue
    finding_id, tier, disposition = line.split("\t")
    if tier in {"P3", "P4"}:
        reason = "not-eligible-tier"
        note = f"PoC generation is scoped to P0/P1/P2; this finding is {tier}."
    else:
        reason = "not-eligible-disposition"
        note = (
            f"Disposition is {disposition}; PoC requires CONFIRMED or "
            "CONFIRMED-MODIFIED."
        )
    skipped.append(
        {
            "finding_id": finding_id,
            "tier": tier,
            "disposition": disposition,
            "reason": reason,
            "note": note,
        }
    )
if not skipped:
    skipped.append({"reason": "no-candidates", "note": "--no-poc-needed"})
manifest = {
    "schema_version": "4",
    "repo_slug": slug,
    "date": date,
    "config_path": None,
    "guide_path": None,
    "pocs": [],
    "skipped": skipped,
}
pathlib.Path(manifest_path).write_text(json.dumps(manifest, separators=(",", ":")) + "\n")
PYEOF
        python3 -c "
import json, sys
d = json.load(open('$m'))
for k in ('schema_version','repo_slug','pocs','skipped'):
  if k not in d: sys.exit('missing field: '+k)
if not isinstance(d['pocs'], list): sys.exit('pocs must be a list')
if not isinstance(d['skipped'], list): sys.exit('skipped must be a list')
" 2>/dev/null || blocked "output/proof_of_concept/$slug/poc-manifest-${DATE}.json invalid — required fields: schema_version, repo_slug, pocs (list)."
      done
    else
      # Track whether ANY repo produced PoCs (so we can require the
      # convenience batch runner once, after the loop). Each PoC is
      # self-contained — there is no cross-repo shared config to aggregate.
      any_pocs_tmp="$(mktemp)"
      trap 'rm -f "$any_pocs_tmp"' EXIT
      for slug in $SLUGS; do
        poc_dir="$WORKSPACE/output/proof_of_concept/$slug"
        m="$poc_dir/poc-manifest-${DATE}.json"
        [[ -s "$m" ]] || blocked "output/proof_of_concept/$slug/poc-manifest-${DATE}.json missing or empty — phase-6 must write the PoC manifest. See phases/phase-6.md."
        json_check="$(python3 -c "
import json, sys, os, re
WS = '$WORKSPACE'; DATE = '$DATE'; SLUG = '$slug'; ANYP = '$any_pocs_tmp'
try:
  d = json.load(open('$m'))
except Exception as e:
  print(f'PARSE_ERROR: {e}'); sys.exit(0)
for k in ('schema_version','repo_slug','pocs'):
  if k not in d: print(f'missing field: {k}'); sys.exit(0)
# Phase 6 emits only the self-contained schema-v4 contract.
if str(d['schema_version']) != '4':
  print(f'unexpected schema_version {d[\"schema_version\"]!r} (expected \"4\")'); sys.exit(0)
if not isinstance(d['pocs'], list):
  print('pocs must be a list'); sys.exit(0)
VERDICTS = ('VALID','NEEDS-REVISION','INVALID')
TYPES = ('curl-shell','python','browser-recipe','source-check','other')
CONFIRM = ('script','browser','unconfirmed')
SKIP_REASONS = ('not-eligible-tier','not-eligible-disposition','generation-refused','generation-failed','agent-budget-exhausted','no-candidates')
all_keys = set()
for i, p in enumerate(d['pocs']):
  fid = p.get('finding_id', f'<index {i}>')
  for k in ('finding_id','script_path','script_type','confirm_path','verdict','placeholders'):
    if k not in p:
      print(f'poc {fid}: missing field {k}'); sys.exit(0)
    if k != 'placeholders' and p[k] in (None, ''):
      print(f'poc {fid}: empty field {k}'); sys.exit(0)
  if p['verdict'] not in VERDICTS:
    print(f'poc {fid}: non-canonical verdict {p[\"verdict\"]!r} (allowed: VALID | NEEDS-REVISION | INVALID)'); sys.exit(0)
  if p['script_type'] not in TYPES:
    print(f'poc {fid}: non-canonical script_type {p[\"script_type\"]!r} (allowed: {\" | \".join(TYPES)})'); sys.exit(0)
  if p['confirm_path'] not in CONFIRM:
    print(f'poc {fid}: non-canonical confirm_path {p[\"confirm_path\"]!r} (allowed: {\" | \".join(CONFIRM)})'); sys.exit(0)
  if not isinstance(p['placeholders'], list):
    print(f'poc {fid}: placeholders must be a list'); sys.exit(0)
  if not str(p['script_path']).startswith('output/proof_of_concept/'):
    print(f'poc {fid}: script_path must be workspace-relative under output/proof_of_concept/, got {p[\"script_path\"]!r}'); sys.exit(0)
  sp = os.path.join('$WORKSPACE', p['script_path'])
  if not os.path.isfile(sp) or os.path.getsize(sp) == 0:
    print(f'poc {fid}: script_path does not exist or is empty: {p[\"script_path\"]}'); sys.exit(0)
  all_keys |= set(p['placeholders'])
# skipped[] — every entry must have finding_id, reason in §16 enum, note.
skipped = d.get('skipped', [])
if not isinstance(skipped, list):
  print('skipped must be a list'); sys.exit(0)
for i, s in enumerate(skipped):
  sid = s.get('finding_id', f'<skipped index {i}>')
  if 'reason' not in s or s['reason'] not in SKIP_REASONS:
    print(f'skipped {sid}: non-canonical reason {s.get(\"reason\")!r} (allowed: {\" | \".join(SKIP_REASONS)})'); sys.exit(0)
  if not isinstance(s.get('note',''), str):
    print(f'skipped {sid}: note must be a string'); sys.exit(0)
# pocs[] and skipped[] finding_ids must be disjoint.
poc_ids = {p['finding_id'] for p in d['pocs']}
skip_ids = {s['finding_id'] for s in skipped if s.get('finding_id')}
overlap = poc_ids & skip_ids
if overlap:
  print(f'finding_id(s) appear in both pocs[] and skipped[]: {sorted(overlap)}'); sys.exit(0)
# When there are PoCs, the per-repo documentation artifacts must exist.
# v4 (self-contained): scripts carry inline params and do NOT source a shared
# config; a per-repo guide (POC-GUIDE) + reference catalog (poc-config.env)
# document the params. We check the DOCUMENTATION exists and covers the keys —
# NOT that any file is a runtime dependency (it is not).
if d['pocs']:
  ver = str(d['schema_version'])
  if ver == '4':
    guide = d.get('guide_path')
    cfg = d.get('config_path')
    if guide != f'output/proof_of_concept/{SLUG}/POC-GUIDE-{DATE}.md':
      print(f'guide_path must be output/proof_of_concept/{SLUG}/POC-GUIDE-{DATE}.md when pocs[] non-empty, got {guide!r}'); sys.exit(0)
    if cfg != f'output/proof_of_concept/{SLUG}/poc-config.env':
      print(f'config_path must be the per-repo reference catalog output/proof_of_concept/{SLUG}/poc-config.env, got {cfg!r}'); sys.exit(0)
    gpath = os.path.join(WS, guide); cpath = os.path.join(WS, cfg)
    if not os.path.isfile(gpath) or os.path.getsize(gpath) == 0:
      print(f'guide missing or empty: {guide} (phase-6 Step 4a must write the per-repo PoC guide)'); sys.exit(0)
    if not os.path.isfile(cpath) or os.path.getsize(cpath) == 0:
      print(f'reference catalog missing or empty: {cfg} (phase-6 Step 4b must write the catalog)'); sys.exit(0)
    # The reference catalog should document every inline parameter used.
    cfg_keys = set(re.findall(r'^([A-Z][A-Z0-9_]*)=', open(cpath).read(), re.MULTILINE))
    missing = sorted(all_keys - cfg_keys)
    if missing:
      print(f'{cfg} does not document key(s) used by this repo\\'s PoCs: ' + ', '.join(missing)); sys.exit(0)
  # Mark that this repo produced PoCs (for the once-after-loop runner check).
  open(ANYP, 'a').write('1\\n')
print('OK')
" 2>&1)"
        if [[ "$json_check" != "OK" ]]; then
          blocked "output/proof_of_concept/$slug/poc-manifest-${DATE}.json invalid: $json_check. See references/enums.md §14/§15 and phases/phase-6.md."
        fi
      done

      # ── Convenience-runner check (run ONCE, after the per-slug loop) ──
      # When ANY repo produced PoCs, the batch runner should exist. It is
      # convenience-only (a single PoC runs standalone), so its absence is a
      # soft signal — but phase-6 is specified to write it, so require it.
      if [[ -s "$any_pocs_tmp" ]]; then
        root_invoker="$WORKSPACE/output/proof_of_concept/run-all.sh"
        [[ -s "$root_invoker" ]] || blocked "output/proof_of_concept/run-all.sh missing or empty — phase-6 must generate the convenience batch runner. See phases/phase-6.md Step 4c."
      fi
    fi
    for slug in $SLUGS; do
      candidates="$WORKSPACE/findings/$slug/candidates-${DATE}.md"
      manifest="$WORKSPACE/output/proof_of_concept/$slug/poc-manifest-${DATE}.json"
      manifest_args=(--input "$manifest" --candidates "$candidates" --slug "$slug" --date "$DATE")
      if [[ "$MODE" == "review" ]]; then
        manifest_args+=(--dataflow "$WORKSPACE/findings/$slug/dataflow-${DATE}.json")
      fi
      manifest_check="$(python3 "$SCRIPT_DIR/validate-findings.py" manifest \
        "${manifest_args[@]}" 2>&1)" \
        || blocked "output/proof_of_concept/$slug/poc-manifest-${DATE}.json failed exact candidate coverage validation: $manifest_check"
    done
    ;;

  phase-7)
    for slug in $SLUGS; do
      f="$WORKSPACE/findings/$slug/findings-${DATE}.md"
      [[ -s "$f" ]] || blocked "findings/$slug/findings-${DATE}.md missing or empty — rebuild it from validated findings JSON with scripts/report/build-markdown.py."
      # Receipt artifact (reproducibility) — required per phase-7 spec
      r="$WORKSPACE/findings/$slug/receipt-${DATE}.json"
      [[ -s "$r" ]] || blocked "findings/$slug/receipt-${DATE}.json missing or empty — phase-7 must emit the reproducibility receipt alongside the findings report."
      # Validate it parses as JSON
      python3 -c "import json; json.load(open('$r'))" 2>/dev/null || blocked "findings/$slug/receipt-${DATE}.json is not valid JSON — phase-7 receipt invalid."
      # Pattern-tags JSON (J.8) — required input for phase-8 clustering.
      pt="$WORKSPACE/findings/$slug/pattern-tags-${DATE}.json"
      [[ -s "$pt" ]] || blocked "findings/$slug/pattern-tags-${DATE}.json missing or empty — phase-7 must emit per-repo pattern tags (see references/pattern-tags.md). Empty 'tags: []' is allowed; missing file is not."
      python3 -c "
import json, sys
d = json.load(open('$pt'))
for k in ('schema_version','repo_slug','tags'):
  if k not in d: sys.exit('missing field: '+k)
if not isinstance(d['tags'], list): sys.exit('tags must be a list')
" 2>/dev/null || blocked "findings/$slug/pattern-tags-${DATE}.json invalid — required fields: schema_version, repo_slug, tags (list). See phases/phase-7.md 'Pattern tags' section."
      # Findings-JSON projection — required input for phase-8 HTML build.
      # Phase-8 passes findings[] through verbatim into the HTML builder;
      # if this file is missing or malformed, phase-8 cannot produce
      # finding bodies in the report.
      fj="$WORKSPACE/findings/$slug/findings-${DATE}.json"
      [[ -s "$fj" ]] || blocked "findings/$slug/findings-${DATE}.json missing or empty — phase-7 must emit per-repo render-ready findings JSON for phase-8. See phases/phase-7.md 'Render-ready findings projection' section."
      python3 -c "
import json, re, sys
d = json.load(open('$fj'))
for k in ('schema_version','repo_slug','repo_url','commit_sha','findings'):
  if k not in d: sys.exit('missing top-level field: '+k)
if not isinstance(d['findings'], list): sys.exit('findings must be a list')
if not str(d.get('repo_url','')).strip():
  sys.exit('repo_url is empty (copy it from receipt-<DATE>.json)')
if not str(d.get('commit_sha','')).strip():
  sys.exit('commit_sha is empty (copy it from receipt-<DATE>.json)')
required_md = ('what_happens_md','attack_steps_md','evidence_md','fix_md','severity_md')
EXPOSURE = ('EXTERNAL','INTERNAL','INTERNAL-RESTRICTED')
VEC = re.compile(r'^CVSS:4\.0/')
def tier_for(score):
  if score >= 9.0: return 'P0'
  if score >= 7.0: return 'P1'
  if score >= 4.0: return 'P2'
  if score >= 0.1: return 'P3'
  return 'P4'
for i, f in enumerate(d['findings']):
  fid = f.get('finding_id', f'<index {i}>')
  for k in required_md:
    if not f.get(k):
      sys.exit(f'finding {fid}: missing or empty field: {k}')
  # CVSS is REQUIRED on every finding (canonical score; the tier derives from
  # it). Missing score or vector blocks — a finding without a score cannot be
  # tiered or ticketed.
  sc = f.get('cvss_score')
  if sc is None or sc == '':
    sys.exit(f'finding {fid}: cvss_score is required (every finding must carry a CVSS v4.0 Base score)')
  try:
    scf = float(sc)
  except (TypeError, ValueError):
    sys.exit(f'finding {fid}: cvss_score is not a number: {sc!r}')
  if not (0.0 <= scf <= 10.0):
    sys.exit(f'finding {fid}: cvss_score {scf} out of range 0.0-10.0')
  vec = str(f.get('cvss_vector','')).strip()
  if not vec:
    sys.exit(f'finding {fid}: cvss_vector is required (a CVSS:4.0/... Base vector)')
  if not VEC.match(vec):
    sys.exit(f'finding {fid}: cvss_vector must start with CVSS:4.0/ , got {vec!r}')
  t = str(f.get('tier','')).strip()
  if t and t != tier_for(scf):
    sys.exit(f'finding {fid}: tier {t} does not match cvss_score {scf} (band {tier_for(scf)}); the tier is derived from the score, enums.md §18')
  # exposure, when present, must be the canonical enum (not prose).
  ex = f.get('exposure')
  if ex is not None and str(ex).strip() and str(ex).strip() not in EXPOSURE:
    sys.exit(f'finding {fid}: exposure must be one of {EXPOSURE} (the enum, not a sentence), got {ex!r}')
" 2>/dev/null || blocked "findings/$slug/findings-${DATE}.json invalid — v2 requires top-level schema_version/repo_slug/repo_url/commit_sha/findings; every finding needs non-empty what_happens_md/attack_steps_md/evidence_md/fix_md/severity_md, a REQUIRED cvss_score (0.0-10.0) matching its tier band, a REQUIRED cvss_vector (CVSS:4.0/...), and exposure as the enum EXTERNAL|INTERNAL|INTERNAL-RESTRICTED when set. See phases/phase-7.md and references/cvss-policy.md."
      if [[ "$MODE" == "review" ]]; then
        expected_scope="$(python3 "$SCRIPT_DIR/normalize-multitenant-scope.py" \
          --run-log "$WORKSPACE/output/run-log-${DATE}.md" --slug "$slug" 2>&1)" \
          || blocked "phase-0 multitenant_scope provenance invalid for $slug: $expected_scope"
        findings_check="$(python3 "$SCRIPT_DIR/validate-findings.py" findings \
          --input "$fj" --receipt "$r" \
          --candidates "$WORKSPACE/findings/$slug/candidates-${DATE}.md" \
          --manifest "$WORKSPACE/output/proof_of_concept/$slug/poc-manifest-${DATE}.json" \
          --dataflow "$WORKSPACE/findings/$slug/dataflow-${DATE}.json" \
          --mode "$MODE" --slug "$slug" --date "$DATE" \
          --expected-multitenant-scope "$expected_scope" 2>&1)" \
          || blocked "findings/$slug/findings-${DATE}.json failed strict schema/CVSS/provenance validation: $findings_check"
      else
        findings_check="$(python3 "$SCRIPT_DIR/validate-findings.py" findings \
          --input "$fj" --receipt "$r" \
          --candidates "$WORKSPACE/findings/$slug/candidates-${DATE}.md" \
          --manifest "$WORKSPACE/output/proof_of_concept/$slug/poc-manifest-${DATE}.json" \
          --mode "$MODE" --slug "$slug" --date "$DATE" 2>&1)" \
          || blocked "findings/$slug/findings-${DATE}.json failed strict schema/CVSS/provenance validation: $findings_check"
      fi
      expected_md="$(mktemp "${TMPDIR:-/tmp}/nightfalcon-markdown.XXXXXX")"
      if ! python3 "$SCRIPT_DIR/report/build-markdown.py" --input "$fj" --output "$expected_md" >/dev/null 2>&1; then
        rm -f "$expected_md"
        blocked "findings/$slug/findings-${DATE}.md could not be rebuilt from validated findings JSON."
      fi
      if ! cmp -s "$expected_md" "$f"; then
        rm -f "$expected_md"
        blocked "findings/$slug/findings-${DATE}.md is stale or not derived exactly from findings-${DATE}.json; rebuild Markdown with scripts/report/build-markdown.py."
      fi
      rm -f "$expected_md"
      # SARIF projection — industry-standard machine-readable output built
      # mechanically from findings-<DATE>.json by scripts/report/build-sarif.py.
      # Must be valid SARIF 2.1.0 with exactly one run and one result per finding.
      sf="$WORKSPACE/findings/$slug/findings-${DATE}.sarif"
      [[ -s "$sf" ]] || blocked "findings/$slug/findings-${DATE}.sarif missing or empty — phase-7 must emit the SARIF 2.1.0 projection via scripts/report/build-sarif.py. See phases/phase-7.md 'SARIF projection' section."
      python3 -c "
import json, sys
s = json.load(open('$sf'))
if str(s.get('version')) != '2.1.0': sys.exit('version must be 2.1.0')
runs = s.get('runs')
if not isinstance(runs, list) or len(runs) != 1: sys.exit('expected exactly one run')
if runs[0].get('tool',{}).get('driver',{}).get('name') != 'NightFalcon': sys.exit('driver.name must be NightFalcon')
if not isinstance(runs[0].get('results'), list): sys.exit('results must be a list')
fj = json.load(open('$fj'))
nf = len(fj.get('findings') or [])
if len(runs[0]['results']) != nf: sys.exit(f'result count {len(runs[0][\"results\"])} != finding count {nf}')
" 2>/dev/null || blocked "findings/$slug/findings-${DATE}.sarif invalid — must be SARIF 2.1.0 with one run (driver NightFalcon) and one result per finding in findings-${DATE}.json. Rebuild it with scripts/report/build-sarif.py; do not hand-write it."
      expected_sf="$(mktemp "${TMPDIR:-/tmp}/nightfalcon-sarif.XXXXXX")"
      if ! python3 "$SCRIPT_DIR/report/build-sarif.py" --input "$fj" --output "$expected_sf" >/dev/null 2>&1; then
        rm -f "$expected_sf"
        blocked "findings/$slug/findings-${DATE}.sarif could not be rebuilt from findings JSON."
      fi
      if ! cmp -s "$expected_sf" "$sf"; then
        rm -f "$expected_sf"
        blocked "findings/$slug/findings-${DATE}.sarif is stale or not derived exactly from findings-${DATE}.json."
      fi
      rm -f "$expected_sf"
    done
    ;;

  phase-8)
    journal="$WORKSPACE/output/agent-conversation-${DATE}.md"
    [[ -s "$journal" ]] || blocked "output/agent-conversation-${DATE}.md journal missing or empty — phase-8 requires the canonical agent conversation journal."
    # Re-authenticate every Phase-7 source artifact before any deterministic
    # builder or comparison. This catches cross-phase tampering after Phase 7.
    for slug in $SLUGS; do
      findings_args=(
        --input "$WORKSPACE/findings/$slug/findings-${DATE}.json"
        --receipt "$WORKSPACE/findings/$slug/receipt-${DATE}.json"
        --candidates "$WORKSPACE/findings/$slug/candidates-${DATE}.md"
        --manifest "$WORKSPACE/output/proof_of_concept/$slug/poc-manifest-${DATE}.json"
        --mode "$MODE" --slug "$slug" --date "$DATE"
      )
      if [[ "$MODE" == "review" ]]; then
        expected_scope="$(python3 "$SCRIPT_DIR/normalize-multitenant-scope.py" \
          --run-log "$WORKSPACE/output/run-log-${DATE}.md" --slug "$slug" 2>&1)" \
          || blocked "phase-0 multitenant_scope provenance invalid for $slug: $expected_scope"
        findings_args+=(
          --dataflow "$WORKSPACE/findings/$slug/dataflow-${DATE}.json"
          --expected-multitenant-scope "$expected_scope"
        )
      fi
      findings_check="$(python3 "$SCRIPT_DIR/validate-findings.py" findings \
        "${findings_args[@]}" 2>&1)" \
        || blocked "findings/$slug/findings-${DATE}.json failed strict pre-report schema/CVSS/provenance validation: $findings_check"
    done
    ej="$WORKSPACE/output/executive-report-${DATE}.json"
    [[ -s "$ej" ]] || blocked "output/executive-report-${DATE}.json missing or empty — phase-8 must retain the structured JSON used to build HTML."
    expected_ej="$(mktemp "${TMPDIR:-/tmp}/nightfalcon-executive-json.XXXXXX")"
    expected_summary="$(mktemp "${TMPDIR:-/tmp}/nightfalcon-executive-summary.XXXXXX")"
    executive_args=(python3 "$SCRIPT_DIR/report/build-executive.py" --date "$DATE")
    for slug in $SLUGS; do
      executive_args+=(--input "$WORKSPACE/findings/$slug/findings-${DATE}.json")
    done
    if ! "${executive_args[@]}" --output "$expected_ej" --summary-output "$expected_summary" >/dev/null 2>&1; then
      rm -f "$expected_ej" "$expected_summary"
      blocked "output/executive-report-${DATE}.json could not be rebuilt from validated per-repo findings JSON."
    fi
    if ! cmp -s "$expected_ej" "$ej"; then
      rm -f "$expected_ej" "$expected_summary"
      blocked "output/executive-report-${DATE}.json is stale, fabricated, contains unknown fields, or is not the exact deterministic executive projection."
    fi
    rm -f "$expected_ej"
    summary="$WORKSPACE/output/executive-summary-${DATE}.md"
    if ! cmp -s "$expected_summary" "$summary"; then
      rm -f "$expected_summary"
      blocked "output/executive-summary-${DATE}.md is stale, fabricated, or is not the exact deterministic executive projection."
    fi
    rm -f "$expected_summary"
    report_check="$(python3 - "$ej" "$WORKSPACE" "$STATE_FILE" "$DATE" <<'PYEOF'
import json
import pathlib
import sys

report_path, workspace_arg, state_path, date = sys.argv[1:]
workspace = pathlib.Path(workspace_arg)
report = json.loads(pathlib.Path(report_path).read_text())
state = json.loads(pathlib.Path(state_path).read_text())
slugs = state.get("repo_slugs")
if not isinstance(report, dict):
    raise SystemExit("executive report JSON must be an object")
if not isinstance(slugs, list) or not slugs:
    raise SystemExit("state repo_slugs must be a non-empty list")
for key in ("report_title", "totals", "summary_markdown", "repos"):
    if key not in report:
        raise SystemExit(f"missing top-level field: {key}")
if not isinstance(report["repos"], list):
    raise SystemExit("repos must be a list")
repos = report["repos"]
repo_slugs = [repo.get("slug") for repo in repos if isinstance(repo, dict)]
if len(repo_slugs) != len(set(repo_slugs)):
    raise SystemExit("repos contains duplicate slug entries")
if set(repo_slugs) != set(slugs) or len(repo_slugs) != len(slugs):
    raise SystemExit(f"repos slugs {repo_slugs!r} do not exactly match state {slugs!r}")
by_slug = {repo["slug"]: repo for repo in repos}
aggregate = {tier: 0 for tier in ("P0", "P1", "P2", "P3", "P4")}
clean = 0
for slug in slugs:
    source_path = workspace / "findings" / slug / f"findings-{date}.json"
    source = json.loads(source_path.read_text())
    repo = by_slug[slug]
    if repo.get("repo_url") != source.get("repo_url"):
        raise SystemExit(f"{slug}: repo_url does not match findings JSON")
    if repo.get("commit_sha") != source.get("commit_sha"):
        raise SystemExit(f"{slug}: commit_sha does not match findings JSON")
    if repo.get("multitenant_scope") != source.get("multitenant_scope"):
        raise SystemExit(f"{slug}: multitenant_scope does not match findings JSON")
    if repo.get("findings") != source.get("findings"):
        raise SystemExit(f"{slug}: findings array is not an exact projection")
    expected_counts = {tier: 0 for tier in aggregate}
    for finding in source.get("findings", []):
        tier = finding.get("tier")
        if tier not in expected_counts:
            raise SystemExit(f"{slug}: invalid finding tier {tier!r}")
        expected_counts[tier] += 1
    if repo.get("counts") != expected_counts:
        raise SystemExit(f"{slug}: counts do not match findings array")
    for tier, count in expected_counts.items():
        aggregate[tier] += count
    expected_most = next((tier for tier in aggregate if expected_counts[tier]), "Clean")
    if repo.get("most_severe") != expected_most:
        raise SystemExit(f"{slug}: most_severe does not match counts")
    if not source.get("findings"):
        clean += 1
totals = report.get("totals")
if not isinstance(totals, dict):
    raise SystemExit("totals must be an object")
for tier, count in aggregate.items():
    if totals.get(tier) != count:
        raise SystemExit(f"totals.{tier} does not match findings")
if totals.get("clean_repos") != clean:
    raise SystemExit("totals.clean_repos does not match findings")
print("OK")
PYEOF
)" || blocked "output/executive-report-${DATE}.json invalid or inconsistent with per-repo findings: $report_check"
    [[ "$report_check" == "OK" ]] || blocked "output/executive-report-${DATE}.json invalid: $report_check"
    [[ -s "$WORKSPACE/output/executive-summary-${DATE}.md" ]] || blocked "output/executive-summary-${DATE}.md missing or empty — phase-8 must write executive summary."
    h="$WORKSPACE/output/executive-report-${DATE}.html"
    [[ -s "$h" ]] || blocked "output/executive-report-${DATE}.html missing or empty — phase-8 must build the HTML report via scripts/report/build.py."

    # Structural validity (defeats the </script>-in-string class of bug).
    # The builder script already self-checks; this gate is a backstop in
    # case the builder is bypassed or the template is modified by hand.
    n_open="$(grep -oE '<script\b' "$h" | wc -l | tr -d ' ')"
    n_close="$(grep -oE '</script\s*>' "$h" | wc -l | tr -d ' ')"
    if [[ "$n_open" != "1" ]]; then
      blocked "output/executive-report-${DATE}.html has $n_open <script> opening tags (expected exactly 1). A second <script> usually means raw markup leaked from a finding body into the JSON literal. Re-run scripts/report/build.py from the structured JSON input."
    fi
    if [[ "$n_close" != "1" ]]; then
      blocked "output/executive-report-${DATE}.html has $n_close </script> closing tags (expected exactly 1). A second </script> terminates the outer <script> block early and breaks every click handler. Re-run scripts/report/build.py from the structured JSON input."
    fi

    # The JSON literal region (between `REPO_DETAIL = ` and the file's
    # closing </script>) must not contain any raw </script> — escaped
    # `<\/script` is fine, raw `</script>` is not.
    raw_close_in_json="$(python3 - <<PYEOF
import re, sys
with open("$h") as f:
    html_str = f.read()
m = re.search(r"REPO_DETAIL\s*=\s*", html_str)
if not m:
    # No REPO_DETAIL block — older builder; skip the JSON-region check.
    print("0"); sys.exit(0)
after = html_str[m.end():]
last_close = after.rfind("</script>")
if last_close < 0:
    print("0"); sys.exit(0)
json_region = after[:last_close]
n = len(re.findall(r"</script\s*>", json_region, re.IGNORECASE))
print(str(n))
PYEOF
)"
    if [[ "$raw_close_in_json" != "0" ]]; then
      blocked "output/executive-report-${DATE}.html has $raw_close_in_json raw '</script>' sequence(s) inside the REPO_DETAIL JSON literal — these terminate the outer <script> block and break every click handler. The builder's safe_json_for_script escape failed (or was bypassed). Re-run scripts/report/build.py and do not hand-edit the HTML."
    fi

    expected_h="$(mktemp "${TMPDIR:-/tmp}/nightfalcon-html.XXXXXX")"
    if ! python3 "$SCRIPT_DIR/report/build.py" --input "$ej" --output "$expected_h" >/dev/null 2>&1; then
      rm -f "$expected_h"
      blocked "output/executive-report-${DATE}.html could not be rebuilt from executive-report-${DATE}.json."
    fi
    if ! cmp -s "$expected_h" "$h"; then
      rm -f "$expected_h"
      blocked "output/executive-report-${DATE}.html is stale or not derived exactly from executive-report-${DATE}.json."
    fi
    rm -f "$expected_h"

    # Aggregate SARIF — one run per repo, built mechanically from every
    # per-repo findings-<DATE>.json by scripts/report/build-sarif.py.
    es="$WORKSPACE/output/executive-report-${DATE}.sarif"
    [[ -s "$es" ]] || blocked "output/executive-report-${DATE}.sarif missing or empty — phase-8 must emit the aggregate SARIF 2.1.0 log via scripts/report/build-sarif.py (one --input per repo findings-${DATE}.json). See phases/phase-8.md."
    N_SLUGS="$(printf '%s\n' "$SLUGS" | tr ' ' '\n' | grep -c .)"
    python3 -c "
import json, sys
s = json.load(open('$es'))
if str(s.get('version')) != '2.1.0': sys.exit('version must be 2.1.0')
runs = s.get('runs')
if not isinstance(runs, list) or not runs: sys.exit('runs must be a non-empty list')
for r in runs:
  if r.get('tool',{}).get('driver',{}).get('name') != 'NightFalcon': sys.exit('every run driver.name must be NightFalcon')
  if not isinstance(r.get('results'), list): sys.exit('every run results must be a list')
# One run per repo — a truncated aggregate (build-sarif called with a missing
# --input) would otherwise pass silently and under-report repos to automation.
if len(runs) != $N_SLUGS: sys.exit(f'expected one run per repo: {len(runs)} runs vs $N_SLUGS repo_slugs')
" 2>/dev/null || blocked "output/executive-report-${DATE}.sarif invalid — must be SARIF 2.1.0 with exactly one run per repo ($N_SLUGS repo_slugs), driver NightFalcon. Rebuild it with scripts/report/build-sarif.py passing one --input per repo findings-${DATE}.json; do not hand-write it."

    expected_es="$(mktemp "${TMPDIR:-/tmp}/nightfalcon-aggregate-sarif.XXXXXX")"
    sarif_args=(python3 "$SCRIPT_DIR/report/build-sarif.py")
    for slug in $SLUGS; do
      sarif_args+=(--input "$WORKSPACE/findings/$slug/findings-${DATE}.json")
    done
    if ! "${sarif_args[@]}" --output "$expected_es" >/dev/null 2>&1; then
      rm -f "$expected_es"
      blocked "output/executive-report-${DATE}.sarif could not be rebuilt from per-repo findings JSON."
    fi
    if ! cmp -s "$expected_es" "$es"; then
      rm -f "$expected_es"
      blocked "output/executive-report-${DATE}.sarif is stale, duplicated, or not derived exactly from all per-repo findings JSON files."
    fi
    rm -f "$expected_es"
    ;;

  *)
    echo "ERROR: Unknown phase: $PHASE" >&2
    exit 1
    ;;
esac

if [[ -n "$CHECK_SLUG" ]]; then
  echo "Phase '$PHASE' output valid for slug '$CHECK_SLUG'; state unchanged."
else
  echo "Phase '$PHASE' output valid; state unchanged."
fi
