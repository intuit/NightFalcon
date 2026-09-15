#!/usr/bin/env python3
"""
Build a SARIF 2.1.0 log from one or more NightFalcon findings-<DATE>.json files.

Usage:
    build-sarif.py --input <findings.json> [--input <findings2.json> ...] \
                   --output <report.sarif>

Why this exists
---------------
SARIF (Static Analysis Results Interchange Format, OASIS 2.1.0) is the
industry-standard machine-readable format for static-analysis results. Emitting
it lets NightFalcon findings flow into GitHub code scanning, Azure DevOps,
Defender, and any SARIF-aware ticketing/automation without a custom parser.

This builder is deterministic — it maps the fields already present in the
findings-<DATE>.json (schema v2) into SARIF, in Python stdlib. The phase-7 and
phase-8 subagents never hand-write SARIF (that would be error-prone and drift
across ports); they call this script, exactly like the HTML report is built by
build.py.

Mapping (findings-json v2  ->  SARIF 2.1.0)
------------------------------------------
- One `runs[]` entry per input findings-json (per repo). tool.driver.name =
  "NightFalcon". Each run carries the repo_url as a versionControlProvenance
  entry and repo_slug/commit_sha in run.properties.
- Each finding -> one `results[]` object:
    ruleId          = cwe (CWE-NNN) if present, else cve, else "NIGHTFALCON.<finding_id>"
    level           = error (P0/P1) | warning (P2) | note (P3/P4), from tier
    message.text    = title + " — " + reachability/impact summary
    locations[]     = physicalLocation from the `location` string (best-effort
                      file + line parse)
    properties      = tier, cvss_score, cvss_vector, cwe, cve, exposure,
                      reachability, exploitability, impact, patch_status,
                      finding_id, security-severity (for GitHub code scanning)
- Each distinct ruleId -> one `tool.driver.rules[]` entry (deduped).

Security-severity: GitHub code scanning sorts by the numeric
`security-severity` property; we set it to the CVSS base score so the SARIF
ordering matches the P-tier.

ruleId stability note: for a finding with a CWE (or CVE) the ruleId is that
stable taxonomy id (CWE-639 is CWE-639 across re-scans), so SARIF-consumer
alert de-duplication works. For a finding with NEITHER — the
`NIGHTFALCON.<finding_id>` fallback — the ruleId is only per-run stable
(finding_id is assigned per run and may shift when the finding set changes).
Cross-run correlation of such unclassified findings should key on
`result.properties.finding_id` + `result.locations`, not ruleId. CVSS is
gate-required on every finding; CWE/CVE are optional, so the fallback path is
reachable — prefer setting a CWE whenever one fits to get a stable ruleId.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SARIF_VERSION = "2.1.0"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"

# tier -> SARIF result level
_LEVEL = {"P0": "error", "P1": "error", "P2": "warning", "P3": "note", "P4": "note"}

# Parse "path/to/File.java:42-78" or "path:42" or bare "path" out of a location
# string. Best-effort: SARIF needs a physicalLocation.artifactLocation.uri.
_LOC_RE = re.compile(r"^\s*([^\s:][^:]*?)(?::(\d+)(?:[-–]\d+)?)?\s*$")


def _first_location(location: str) -> dict | None:
    """Return a SARIF location dict from the finding's `location` string, or
    None when nothing file-like can be extracted. Only the first path is used;
    the full location string is preserved in result.properties.location."""
    if not location:
        return None
    # The location field may list several files; take the first token that
    # looks like path[:line].
    first = location.split(",")[0].split(";")[0].strip()
    m = _LOC_RE.match(first)
    if not m:
        return None
    uri = m.group(1).strip()
    if not uri:
        return None
    phys: dict = {"artifactLocation": {"uri": uri}}
    if m.group(2):
        phys["region"] = {"startLine": int(m.group(2))}
    return {"physicalLocation": phys}


def _rule_id(f: dict) -> str:
    cwe = str(f.get("cwe") or "").strip()
    if cwe and cwe.lower() != "null":
        return cwe
    cve = str(f.get("cve") or "").strip()
    if cve and cve.lower() != "null":
        return cve
    return "NIGHTFALCON." + str(f.get("finding_id") or "UNKNOWN")


def _security_severity(f: dict) -> str | None:
    sc = f.get("cvss_score")
    if sc is None or sc == "":
        return None
    try:
        return f"{float(sc):.1f}"
    except (TypeError, ValueError):
        return None


def _result(f: dict) -> dict:
    tier = str(f.get("tier") or "P4").strip()
    title = str(f.get("title") or "(untitled)")
    impact = str(f.get("impact") or "").strip()
    reach = str(f.get("reachability") or "").strip()
    msg = title
    if impact:
        msg += " — " + impact
    elif reach:
        msg += " — " + reach

    props: dict = {"finding_id": f.get("finding_id"), "tier": tier}
    for k in ("cvss_score", "cvss_vector", "cwe", "cve", "exposure",
              "reachability", "exploitability", "impact", "patch_status",
              "validation", "category", "location"):
        v = f.get(k)
        if v not in (None, ""):
            props[k] = v
    sev = _security_severity(f)
    if sev is not None:
        props["security-severity"] = sev

    result: dict = {
        "ruleId": _rule_id(f),
        "level": _LEVEL.get(tier, "note"),
        "message": {"text": msg},
        "properties": props,
    }
    loc = _first_location(str(f.get("location") or ""))
    if loc:
        result["locations"] = [loc]
    return result


def _rule(f: dict) -> dict:
    rid = _rule_id(f)
    rule: dict = {
        "id": rid,
        "name": rid.replace("-", "_"),
        "shortDescription": {"text": str(f.get("category") or rid)},
    }
    sev = _security_severity(f)
    props: dict = {}
    if sev is not None:
        props["security-severity"] = sev
    cwe = str(f.get("cwe") or "").strip()
    if cwe and cwe.lower() != "null" and cwe.upper().startswith("CWE-"):
        # SARIF taxonomy relationship hint for the CWE.
        props["cwe"] = cwe
    if props:
        rule["properties"] = props
    return rule


def _run_for(doc: dict) -> dict:
    findings = doc.get("findings") or []
    # Dedupe rules by id, first-wins.
    rules: dict[str, dict] = {}
    results: list[dict] = []
    for f in findings:
        r = _result(f)
        results.append(r)
        rid = r["ruleId"]
        if rid not in rules:
            rules[rid] = _rule(f)

    driver = {
        "name": "NightFalcon",
        "informationUri": "https://github.com/intuit/nightfalcon",
        "rules": list(rules.values()),
    }
    run: dict = {
        "tool": {"driver": driver},
        "results": results,
        "properties": {
            "repo_slug": doc.get("repo_slug"),
            "commit_sha": doc.get("commit_sha"),
            "multitenant_scope": doc.get("multitenant_scope"),
            "date": doc.get("date"),
        },
    }
    repo_url = str(doc.get("repo_url") or "").strip()
    if repo_url and repo_url.lower() != "null":
        vcp: dict = {"repositoryUri": repo_url}
        sha = str(doc.get("commit_sha") or "").strip()
        if sha and sha.lower() not in ("null", "n/a (triage)"):
            vcp["revisionId"] = sha
        run["versionControlProvenance"] = [vcp]
    return run


def build_sarif(docs: list[dict]) -> dict:
    return {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [_run_for(d) for d in docs],
    }


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--input", required=True, action="append", type=Path,
                   help="A findings-<DATE>.json (schema v2). Repeatable; one "
                        "SARIF run per input.")
    p.add_argument("--output", required=True, type=Path,
                   help="Path to write the .sarif log.")
    args = p.parse_args(argv)

    docs: list[dict] = []
    for inp in args.input:
        try:
            docs.append(json.loads(inp.read_text()))
        except FileNotFoundError:
            print(f"ERROR: input not found: {inp}", file=sys.stderr)
            return 2
        except json.JSONDecodeError as e:
            print(f"ERROR: {inp} failed to parse: {e}", file=sys.stderr)
            return 2

    sarif = build_sarif(docs)
    # Self-check: valid JSON, required SARIF keys, one run per input.
    assert sarif["version"] == SARIF_VERSION
    assert len(sarif["runs"]) == len(docs)
    for run in sarif["runs"]:
        assert run["tool"]["driver"]["name"] == "NightFalcon"
        assert isinstance(run["results"], list)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(sarif, indent=2, ensure_ascii=False) + "\n")
    n = sum(len(r["results"]) for r in sarif["runs"])
    print(f"wrote {args.output} ({len(sarif['runs'])} run(s), {n} result(s))",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
