#!/usr/bin/env bash
# state-utils.sh — Build a normalized derived summary across all per-repo
# artifacts in the workspace. Resolves residual schema drift that the
# enum-pin commits cannot catch (parenthetical modifiers like
# "Confirmed (downgraded from P0)", multiple tier-downgrade forms,
# validation-status spacing variants, etc.).
#
# Why:
#   Even with enum-pinned phase-4 dispositions and phase-5 validations,
#   subagents emit parenthetical modifiers and surrounding prose that
#   the simple gate regex tolerates but downstream consumers
#   (phase-7, phase-8) re-parse fragilely. Doing the parse ONCE,
#   structurally, and emitting a derived-summary JSON lets phases 6/7
#   read JSON instead of grepping markdown.
#
# Usage:
#   bash state-utils.sh --workspace <path>
#
# Output:
#   Writes <workspace>/state-derived-summary.json with shape:
#   {
#     "schema_version": "1",
#     "date": "<YYYY-MM-DD>",
#     "generated_at": "<ISO-8601>",
#     "repos": {
#       "<slug>": {
#         "findings": [
#           {
#             "finding_id": "F-001",
#             "tier_initial": "P0",          // from phase-3 candidates
#             "tier_final": "P1",            // from phase-4 post-debate (may equal tier_initial)
#             "downgraded_from": "P0",       // present only if tier_initial != tier_final
#             "final_disposition": "CONFIRMED-MODIFIED",   // normalized to enum registry §2
#             "validation_status": "CURRENT",              // normalized to enum registry §3, or null
#             "raw_disposition": "Confirmed (downgraded)", // verbatim, for audit
#             "raw_tier_line":   "Final Tier: P1 (downgraded from P0)"  // verbatim, for audit
#           }
#         ],
#         "counts": {
#           "by_disposition": {"CONFIRMED": N, "CONFIRMED-MODIFIED": N, "DISMISSED": N, "NEEDS-REVIEW": N},
#           "by_tier_final":  {"P0": N, "P1": N, "P2": N, "P3": N, "P4": N},
#           "tier_changes":   N           // count where tier_initial != tier_final
#         }
#       }
#     },
#     "aggregate": {
#       "by_tier_final":  {...},
#       "by_disposition": {...},
#       "tier_change_findings": [
#         {"slug": "...", "finding_id": "F-XXX", "from": "P0", "to": "P1"}
#       ]
#     }
#   }
#
# Exit codes:
#   0 — success
#   1 — usage error
#   2 — workspace state invalid (e.g., state.json missing)

set -euo pipefail

WORKSPACE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace) WORKSPACE="$2"; shift 2 ;;
    *) echo "ERROR: unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$WORKSPACE" ]]; then
  echo "Usage: state-utils.sh --workspace <path>" >&2
  exit 1
fi

if [[ ! -f "$WORKSPACE/state.json" ]]; then
  echo "ERROR: $WORKSPACE/state.json not found." >&2
  exit 2
fi

# Delegate the per-repo scan + normalization to inline Python — bash
# regex is too limited for the parenthetical-modifier parsing.
python3 - "$WORKSPACE" <<'PYEOF'
import json, os, re, sys
from datetime import datetime, timezone
from pathlib import Path

workspace = Path(sys.argv[1])
state = json.loads((workspace / "state.json").read_text())
date = state.get("date") or ""
repo_slugs = state.get("repo_slugs") or []
findings_dir = workspace / "findings"

# Canonical enum values (matches references/enums.md §2 / §3)
DISPO_ENUM = {"CONFIRMED", "CONFIRMED-MODIFIED", "DISMISSED", "NEEDS-REVIEW"}
VAL_ENUM = {"CURRENT", "ACTIVELY-EXPLOITED", "PATCHED", "UNVERIFIED", "NOT-RUN"}
TIER_ENUM = {"P0", "P1", "P2", "P3", "P4"}

# Regex: tolerant of markdown styling around the field names.
RE_FINDING_ID    = re.compile(r"^\s*-?\s*\*?\*?Finding ID:\*?\*?\s*`?(F-\d{3,})`?", re.MULTILINE)
RE_FINAL_DISPO   = re.compile(r"Final Disposition.*?\b(CONFIRMED-MODIFIED|CONFIRMED|DISMISSED|NEEDS-REVIEW|Confirmed(?:\s*\([^)]*\))?|Dismissed|Needs-?Review|UPHELD)\b", re.IGNORECASE)
RE_FINAL_TIER    = re.compile(r"Final Tier.*?\b(P[0-4])\b(?:.*?(?:downgraded from|from)\s+(P[0-4]))?", re.IGNORECASE)
RE_VALIDATION    = re.compile(r"Validation.*?\b(CURRENT|ACTIVELY-EXPLOITED|ACTIVELY EXPLOITED|PATCHED|UNVERIFIED|NOT-RUN|NOT_RUN)\b", re.IGNORECASE)
RE_TIER_LINE     = re.compile(r"^[^\n]*\bTier[^\n]*$", re.MULTILINE)
RE_CANDIDATE_BLOCK = re.compile(r"^##\s+CANDIDATE-\d+\b[^\n]*\n", re.MULTILINE)


def normalize_disposition(raw: str) -> str:
    """Map any disposition form to the canonical enum value."""
    if not raw:
        return "NEEDS-REVIEW"
    s = raw.strip().upper()
    # Strip parenthetical
    paren_idx = s.find("(")
    bare = s[:paren_idx].strip() if paren_idx > 0 else s
    has_modifier = paren_idx > 0
    # Direct enum hit
    if bare in DISPO_ENUM:
        return bare
    # Legacy variants
    if bare == "UPHELD":
        return "CONFIRMED"
    if bare in ("CONFIRMED", "CONFIRM"):
        return "CONFIRMED-MODIFIED" if has_modifier else "CONFIRMED"
    if bare in ("DISMISSED", "DISMISS"):
        return "DISMISSED"
    if bare in ("NEEDS-REVIEW", "NEEDSREVIEW", "NEEDS REVIEW"):
        return "NEEDS-REVIEW"
    return "NEEDS-REVIEW"


def normalize_validation(raw: str) -> str | None:
    if not raw:
        return None
    s = raw.strip().upper().replace(" ", "-").replace("_", "-")
    return s if s in VAL_ENUM else None


def parse_candidate_block(block: str) -> dict | None:
    """Extract finding_id + disposition + tier + validation from one candidate block."""
    fid_m = RE_FINDING_ID.search(block)
    if not fid_m:
        return None
    finding_id = fid_m.group(1)

    # Tier_initial: look for a line near the top with "Tier:" or "Initial tier"
    # The phase-3 candidate format has "Tier: P0 / P1 / ..." — extract the first match.
    tier_initial = None
    for m in re.finditer(r"\bTier[^\n]*?\b(P[0-4])\b", block):
        tier_initial = m.group(1)
        break

    # Disposition — capture the verb AND any trailing parenthetical so
    # "Confirmed (downgraded from P0)" → CONFIRMED-MODIFIED, not CONFIRMED.
    dispo_m = RE_FINAL_DISPO.search(block)
    raw_dispo = dispo_m.group(0) if dispo_m else ""
    raw_dispo_value = dispo_m.group(1) if dispo_m else ""
    # Check if the line containing the verb has a parenthetical right after it
    # (within the same line of `block`).
    has_paren_modifier = False
    if dispo_m:
        # Look at the same line as the match for a "(" right after the verb.
        line_start = block.rfind("\n", 0, dispo_m.start()) + 1
        line_end = block.find("\n", dispo_m.end())
        if line_end < 0:
            line_end = len(block)
        line = block[line_start:line_end]
        # Anything like "Confirmed (downgraded from P0)" / "Confirmed at P2"
        # qualifies as a modifier.
        after_verb = line[line.lower().find(raw_dispo_value.lower()) + len(raw_dispo_value):]
        if re.search(r"^\s*\(", after_verb) or re.search(r"^\s+(at\s+P[0-4]|downgraded|reframed)", after_verb, re.I):
            has_paren_modifier = True
    disposition = normalize_disposition(raw_dispo_value)
    if disposition == "CONFIRMED" and has_paren_modifier:
        disposition = "CONFIRMED-MODIFIED"

    # Final tier — phase-4 may have rewritten it
    tier_m = RE_FINAL_TIER.search(block)
    raw_tier_line = ""
    tier_final = tier_initial
    downgraded_from = None
    if tier_m:
        raw_tier_line = tier_m.group(0)
        tier_final = tier_m.group(1).upper() if tier_m.group(1) else tier_initial
        if tier_m.group(2):
            downgraded_from = tier_m.group(2).upper()
    if tier_final not in TIER_ENUM:
        tier_final = tier_initial

    # If disposition has a parenthetical modifier and we didn't find an
    # explicit Final Tier line that names the downgrade, infer:
    if (raw_dispo_value and "(" in raw_dispo and downgraded_from is None
            and tier_initial and tier_final and tier_initial != tier_final):
        downgraded_from = tier_initial

    # Validation
    val_m = RE_VALIDATION.search(block)
    validation_status = normalize_validation(val_m.group(1)) if val_m else None

    out = {
        "finding_id": finding_id,
        "tier_initial": tier_initial,
        "tier_final": tier_final,
        "final_disposition": disposition,
        "validation_status": validation_status,
        "raw_disposition": raw_dispo_value if raw_dispo_value else None,
        "raw_tier_line": raw_tier_line if raw_tier_line else None,
    }
    if downgraded_from:
        out["downgraded_from"] = downgraded_from
    return out


def split_candidates(text: str) -> list[str]:
    """Split a candidates file into per-candidate blocks."""
    parts = []
    matches = list(RE_CANDIDATE_BLOCK.finditer(text))
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        parts.append(text[start:end])
    return parts


# Build the derived summary
result = {
    "schema_version": "1",
    "date": date,
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "repos": {},
    "aggregate": {
        "by_tier_final": {t: 0 for t in TIER_ENUM},
        "by_disposition": {d: 0 for d in DISPO_ENUM},
        "tier_change_findings": [],
    },
}

for slug in repo_slugs:
    cand_path = findings_dir / slug / f"candidates-{date}.md"
    if not cand_path.exists():
        # Some repos may have NO_CANDIDATES or simply no candidates file — skip silently.
        continue
    text = cand_path.read_text()
    if "NO_CANDIDATES" in text and not RE_CANDIDATE_BLOCK.search(text):
        result["repos"][slug] = {
            "findings": [],
            "counts": {
                "by_disposition": {d: 0 for d in DISPO_ENUM},
                "by_tier_final":  {t: 0 for t in TIER_ENUM},
                "tier_changes":   0,
            },
        }
        continue

    findings = []
    for block in split_candidates(text):
        f = parse_candidate_block(block)
        if f:
            findings.append(f)

    counts_dispo = {d: 0 for d in DISPO_ENUM}
    counts_tier = {t: 0 for t in TIER_ENUM}
    tier_changes = 0
    for f in findings:
        counts_dispo[f["final_disposition"]] = counts_dispo.get(f["final_disposition"], 0) + 1
        if f["tier_final"]:
            counts_tier[f["tier_final"]] = counts_tier.get(f["tier_final"], 0) + 1
        if f.get("tier_initial") and f.get("tier_final") and f["tier_initial"] != f["tier_final"]:
            tier_changes += 1
            result["aggregate"]["tier_change_findings"].append({
                "slug": slug,
                "finding_id": f["finding_id"],
                "from": f["tier_initial"],
                "to": f["tier_final"],
            })

    result["repos"][slug] = {
        "findings": findings,
        "counts": {
            "by_disposition": counts_dispo,
            "by_tier_final":  counts_tier,
            "tier_changes":   tier_changes,
        },
    }

    # Aggregate
    for d, n in counts_dispo.items():
        result["aggregate"]["by_disposition"][d] = result["aggregate"]["by_disposition"].get(d, 0) + n
    for t, n in counts_tier.items():
        result["aggregate"]["by_tier_final"][t] = result["aggregate"]["by_tier_final"].get(t, 0) + n

out_path = workspace / "state-derived-summary.json"
out_path.write_text(json.dumps(result, indent=2) + "\n")
print(f"wrote {out_path}", file=sys.stderr)
PYEOF
