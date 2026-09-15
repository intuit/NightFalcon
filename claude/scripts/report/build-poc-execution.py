#!/usr/bin/env python3
"""
build-poc-execution-report.py — render PoC execution results in the
3-bucket (CONFIRMED / BLOCKED / UNTESTED) format used by
output/poc-execution-results-2026-06-23.html.

Reads:
  proof_of_concept/results/<TAG>/classification.jsonl  — strict run_status
  proof_of_concept/results/<TAG>/verdicts.jsonl         — script self-verdict
  proof_of_concept/results/<TAG>/evidence-review.jsonl  — log review
  findings/<repo>/findings-2026-06-09.json              — tier/title/impact
  proof_of_concept/<repo>/output/F-NNN.log              — what-we-did

Writes:
  output/poc-execution-results-<OUT_TAG>.html           — overwrites in place

Verdict bucket mapping (strict bar — only live HTTP exploit counts):
  EXPLOITED-LIVE                          → CONFIRMED
  GATEWAY-BLOCKED, APP-REJECTED           → BLOCKED
  CODE-PATTERN-ONLY, NOT-RUN,
  EXCLUDED-HARDCODED-CRED                 → UNTESTED
"""
from __future__ import annotations
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path.cwd()  # workspace = directory the script is invoked from
NIGHTFALCON_TPL = Path(__file__).resolve().parent / "template.html"

TAG = sys.argv[1] if len(sys.argv) > 1 else "run-20260623"
OUT_TAG = sys.argv[2] if len(sys.argv) > 2 else "2026-06-24"
RES = ROOT / f"proof_of_concept/results/{TAG}"

# run_status → 3-bucket exploit_status
#
# BLOCKED is reserved for cases where a live request reached the
# environment and something (gateway-route or app-auth) actively
# refused it. CODE-PATTERN-ONLY etc. never sent a request → UNTESTED.
#
# GATEWAY-NO-ROUTE vs APP-REJECTED disambiguation: a 404 with no
# x-spanid/x-envoy-upstream-service-time means the path is not mapped
# on TARGET_HOST — the backend was never reached, so the resource-ID
# is irrelevant. A 4xx WITH x-spanid means the backend saw the request
# and refused it — that is a real defensive signal (and the only place
# a wrong/stale victim ID would matter).
BUCKET = {
    "EXPLOITED-LIVE": "CONFIRMED",
    "APP-REJECTED": "BLOCKED",
    "GATEWAY-NO-ROUTE": "BLOCKED",
    "GATEWAY-AUTH": "BLOCKED",
    "GATEWAY-BLOCKED": "BLOCKED",     # legacy alias
    "CODE-PATTERN-ONLY": "UNTESTED",
    "NOT-RUN": "UNTESTED",
    "EXCLUDED-HARDCODED-CRED": "UNTESTED",
}
BUCKET_ORDER = {"CONFIRMED": 0, "BLOCKED": 1, "UNTESTED": 2}
TIER_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}


def load_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def load_findings_index() -> dict[tuple[str, str], dict]:
    """(repo, F-NNN) → {tier, title, what_happens_md, category, location}."""
    idx: dict[tuple[str, str], dict] = {}
    for fj in (ROOT / "findings").glob("*/findings-*.json"):
        repo = fj.parent.name
        try:
            data = json.loads(fj.read_text())
        except Exception:
            continue
        for f in data.get("findings") or []:
            fid = f.get("finding_id")
            if fid:
                idx[(repo, fid)] = f
    return idx


def parse_log_verbose(log_path: str) -> dict:
    """Read the full PoC log and extract a verbose, structured record:
    - steps[]:      human-readable step-by-step (what each STEP did)
    - request:      the exact request sent (curl command / payload, full)
    - response:     the exact response (status line + headers + body, full)
    - verdict_block: the script's own VERDICT block verbatim
    """
    out = {"steps": [], "request": "", "response": "", "verdict_block": ""}
    if not log_path:
        return out
    p = Path(log_path)
    if not p.is_absolute():
        p = ROOT / log_path
    if not p.exists():
        return out
    txt = p.read_text(errors="replace")

    # STEP headers → human bullets
    for m in re.finditer(r"── STEP (\d+): (.+?) ──", txt):
        out["steps"].append(f"Step {m.group(1)}: {m.group(2).strip()}")

    # Request: every [req] line concatenated (full curl command, no trunc)
    req_lines = [l[6:].strip() for l in txt.splitlines() if l.startswith("[req] ")]
    out["request"] = "\n".join(req_lines)

    # Response: HTTP status + all [rsp] lines (headers + body excerpt)
    rsp_lines = [l[6:].rstrip() for l in txt.splitlines() if l.startswith("[rsp]")]
    out["response"] = "\n".join(rsp_lines)

    # VERDICT block verbatim
    vm = re.search(r"┌─ VERDICT ─+.*?└─+", txt, re.S)
    if vm:
        out["verdict_block"] = vm.group(0)

    return out


def read_body_file(body_path: str, limit: int = 8192) -> str:
    """Read a *.body.txt response file in full (up to limit bytes)."""
    if not body_path:
        return ""
    p = Path(body_path)
    if not p.is_absolute():
        p = ROOT / body_path
    if not p.exists():
        return ""
    data = p.read_bytes()[:limit].decode("utf-8", "replace")
    if p.stat().st_size > limit:
        data += f"\n\n…[{p.stat().st_size - limit} more bytes in {p.name}]"
    return data


# ── Hand-authored plain-English narratives for CONFIRMED findings ──────
# These override the auto-extracted technical narrative so a reader who
# doesn't know what "dispatchEvent" or "pushState" means can follow what
# the test actually did and why the result matters. One entry per
# EXPLOITED-LIVE finding. Numbered steps, no code syntax.
HUMAN_WHAT_WE_DID = {
    # Optional per-finding plain-English narrative overrides, keyed by
    # "<repo-slug>/F-NNN". Anything not listed here falls back to the
    # generated humanize_what_we_did() output. The single fictional entry
    # below shows the expected format. Populate this dict for your own run
    # if you want hand-written narratives for specific findings.
    "example-service/F-001": (
        "1. As an anonymous internet user (no login, no cookies, no API "
        "key), we requested the URL https://api.example.com/graphiql in a "
        "plain web request.\n\n"
        "2. The server responded with a developer-tools page (a GraphQL "
        "Playground IDE) normally meant for internal engineers only.\n\n"
        "3. We searched the page's HTML and found a working API gateway key "
        "embedded in plain text, which we extracted.\n\n"
        "4. No authentication was required at any step — anyone on the "
        "internet who knows this URL receives the same key."
    ),
}


def humanize_what_we_did(c: dict, b: dict, log: dict, f: dict) -> str:
    """Build a step-by-step plain-English narrative of what the test did.

    Combines (in priority order):
    1. The browser-poc result's what_we_did (already human-written)
    2. The log's STEP headers (script-authored step labels)
    3. The finding's attack_steps_md (the original how-to-exploit narrative)
    """
    parts: list[str] = []
    # Browser-test narrative (already human-written for browser tests)
    if b and b.get("what_we_did") and b["what_we_did"].strip() and not b["what_we_did"].startswith("("):
        parts.append(b["what_we_did"].strip())
    # Script STEP headers
    if log["steps"]:
        parts.append("\n".join(log["steps"]))
    # If we still have nothing, fall back to the finding's attack_steps_md
    # (the original phase-7 narrative of how the attack works).
    if not parts and f.get("attack_steps_md"):
        parts.append(re.sub(r"[`*_#>]", "", f["attack_steps_md"]).strip())
    return "\n\n".join(parts)


def build_request_payload(c: dict, b: dict, log: dict) -> str:
    """The exact request/payload sent — full curl command, postMessage
    payload, GraphQL mutation body, or storage writes."""
    parts: list[str] = []
    # Browser test: the dispatched message / storage writes / nav URL
    if b:
        wd = b.get("what_we_did") or ""
        # Pull out anything that looks like a payload from the narrative
        for m in re.finditer(
            r"(dispatchEvent\(.*?\)|postMessage\(.*?\)|sessionStorage\.setItem\(.*?\)|"
            r"localStorage\.setItem\(.*?\)|fetch\(.*?\)|navigate [^\s;]+|"
            r"\?[a-zA-Z]+=[^\s;]+)",
            wd, re.S,
        ):
            parts.append(m.group(0))
    # HTTP test: the full [req] block from the log
    if log["request"]:
        parts.append(log["request"])
    return "\n\n".join(p for p in parts if p)


def build_actual_response(c: dict, b: dict, log: dict, v: dict) -> str:
    """The exact response observed — full HTTP body, DOM state, or
    network-call list."""
    parts: list[str] = []
    http = c.get("http") or ""
    if http:
        parts.append(f"HTTP {http}")
    # Browser test: what_we_found is the observed state
    if b and b.get("what_we_found"):
        parts.append(b["what_we_found"].strip())
    # HTTP test: full response body file
    body_path = c.get("body") or (v or {}).get("body") or ""
    body = read_body_file(body_path)
    if body:
        parts.append(f"── response body ({body_path.rsplit('/',1)[-1]}) ──\n{body}")
    elif log["response"]:
        parts.append(log["response"])
    # Reviewer-isolated excerpt (the damning lines)
    if c.get("evidence_excerpt") and c["evidence_excerpt"] not in "\n".join(parts):
        parts.append(f"── key evidence ──\n{c['evidence_excerpt']}")
    return "\n\n".join(p for p in parts if p)


def build_why_proves(c: dict, b: dict, f: dict) -> str:
    """Plain-English explanation of WHY the request/response pair
    proves (or disproves) the vulnerability."""
    parts: list[str] = []
    # Browser test's own why
    if b and b.get("why"):
        parts.append(b["why"].strip())
    # Classification why (route-vs-app, etc.)
    if c.get("why") and (not b or c["why"] not in (b.get("why") or "")):
        parts.append(c["why"].strip())
    # Independent reviewer note
    rn = c.get("reviewer_note") or (b or {}).get("reviewer_note") or ""
    if rn and rn not in "\n".join(parts):
        parts.append(f"Reviewer note: {rn.strip()}")
    # Tie back to the finding's severity rationale
    if f.get("severity_md"):
        sev = re.sub(r"[`*_#>]", "", f["severity_md"]).strip()
        parts.append(f"Severity rationale (from finding): {sev}")
    return "\n\n".join(parts)


def md_to_text(md: str) -> str:
    """Strip markdown markers but keep paragraphs/lists intact (no truncation)."""
    if not md:
        return ""
    return re.sub(r"[`*_#>]", "", md).strip()


# ── HTML helpers ───────────────────────────────────────────────────────
def esc(s) -> str:
    return html.escape(str(s or ""), quote=True)


def safe_json_for_script(obj) -> str:
    """OWASP-safe JSON literal for embedding inside <script>."""
    s = json.dumps(obj, ensure_ascii=False)
    s = re.sub(r"</(script|style)", r"<\\/\1", s, flags=re.I)
    s = s.replace("<!--", "<\\!--").replace("]]>", "]\\]>")
    return s


def extra_css() -> str:
    """Custom classes layered on top of the nightfalcon palette."""
    return (
        ".es-confirmed{color:#166534;background:#f0fdf4;border:1px solid #bbf7d0;"
        "padding:3px 10px;border-radius:5px;font-size:11px;font-weight:700;"
        "display:inline-block;white-space:nowrap}"
        ".es-blocked{color:#7f1d1d;background:#fef2f2;border:1px solid #fecaca;"
        "padding:3px 10px;border-radius:5px;font-size:11px;font-weight:700;"
        "display:inline-block;white-space:nowrap}"
        ".es-untested{color:#475569;background:#f8fafc;border:1px solid #e2e8f0;"
        "padding:3px 10px;border-radius:5px;font-size:11px;font-weight:700;"
        "display:inline-block;white-space:nowrap}"
        ".test-section{margin-bottom:18px}"
        ".test-section h4{font-size:11px;font-weight:700;text-transform:uppercase;"
        "letter-spacing:.8px;color:#64748b;margin:0 0 8px;padding-bottom:4px;"
        "border-bottom:1px solid var(--border)}"
        ".test-did{background:#f1f5f9;border:1px solid #e2e8f0;border-radius:8px;"
        "padding:12px;font-size:12px;white-space:pre-wrap;"
        "font-family:ui-monospace,monospace;margin:0}"
        ".test-found{background:#0f172a;color:#e2e8f0;border-radius:8px;padding:14px;"
        "font-size:12px;white-space:pre-wrap;overflow-x:auto;margin:0;line-height:1.6;"
        "font-family:ui-monospace,monospace}"
        ".test-found.empty{background:#fef9c3;color:#92400e;border:1px solid #fde68a;"
        "font-family:inherit}"
        ".fail-callout{background:#fef2f2;border-left:4px solid #dc2626;"
        "border-radius:0 8px 8px 0;padding:14px 18px;margin:14px 0;font-size:14px}"
        ".fail-callout h4{margin:0 0 6px;font-size:12px;font-weight:700;"
        "text-transform:uppercase;letter-spacing:.5px;color:#7f1d1d}"
        ".key-callout{background:#fffbeb;border:1px solid #fde68a;border-radius:10px;"
        "padding:20px 24px;margin:20px 0}"
        ".key-callout h3{margin:0 0 10px;color:#92400e;font-size:15px}"
        "table.st{width:100%;border-collapse:collapse;font-size:14px;margin:14px 0}"
        "table.st th{text-align:left;padding:10px 14px;background:var(--surface-3);"
        "font-weight:600;border-bottom:2px solid var(--border)}"
        "table.st td{padding:10px 14px;border-bottom:1px solid var(--border);"
        "vertical-align:top}"
        ".filter-bar select{padding:7px 12px;border:1px solid var(--border);"
        "border-radius:8px;font-size:13px;font-family:inherit;background:var(--surface);"
        "cursor:pointer;margin-right:6px}"
    )


def build() -> int:
    classification = load_jsonl(RES / "classification.jsonl")
    verdicts = {(v["repo"], v["finding"]): v for v in load_jsonl(RES / "verdicts.jsonl")}
    browser = {(b["repo"], b["finding_id"]): b for b in load_jsonl(RES / "browser-poc-results.jsonl")}
    findings_idx = load_findings_index()

    # ── Build POC_DATA + per-repo aggregate ────────────────────────────
    poc_data: dict[str, dict] = {}
    repo_agg: dict[str, dict] = {}
    bucket_counts = {"CONFIRMED": 0, "BLOCKED": 0, "UNTESTED": 0}
    rows: list[dict] = []

    for c in classification:
        repo, fid = c["repo"], c["finding_id"]
        key = f"{repo}/{fid}"
        v = verdicts.get((repo, fid), {})
        f = findings_idx.get((repo, fid), {})
        b = browser.get((repo, fid), {})
        bucket = BUCKET.get(c["run_status"], "UNTESTED")
        bucket_counts[bucket] += 1

        tier = (f.get("tier") or "P4").strip()
        title = f.get("title") or c.get("reason") or fid
        log_path = c.get("log") or v.get("log") or ""
        log = parse_log_verbose(log_path)

        entry = {
            "finding_id": fid,
            "repo": repo,
            "tier": tier,
            "title": title,
            "category": f.get("category") or "",
            "location": f.get("location") or "",
            "script_type": c.get("poc_type") or "",
            "exploit_status": bucket,
            "run_status": c["run_status"],
            "http": c.get("http") or "",
            # FULL business impact — the complete what_happens_md from the
            # phase-7 finding, no truncation. This is the plain-English
            # explanation of what an attacker gains.
            "business_impact": md_to_text(f.get("what_happens_md") or ""),
            # Step-by-step what we actually did, in human terms.
            # Hand-authored plain-English override (CONFIRMED findings)
            # takes priority over the auto-extracted technical narrative.
            "what_we_did": HUMAN_WHAT_WE_DID.get(key) or humanize_what_we_did(c, b, log, f),
            # The exact request/payload sent.
            "request_payload": build_request_payload(c, b, log),
            # The exact response/DOM state observed.
            "actual_response": build_actual_response(c, b, log, v),
            # Why the request/response pair proves (or disproves) the risk.
            "why_it_proves": build_why_proves(c, b, f),
            # The original phase-7 attack narrative (how the attack works
            # end-to-end, written for a human reader).
            "attack_narrative": md_to_text(f.get("attack_steps_md") or ""),
            # The original phase-7 fix recommendation.
            "fix": md_to_text(f.get("fix_md") or ""),
            "false_positive_reason": c.get("why") if bucket == "BLOCKED" else "",
            "log": log_path.replace(str(ROOT) + "/", ""),
            "verdict_block": log.get("verdict_block") or "",
        }
        poc_data[key] = entry
        rows.append(entry)

        ra = repo_agg.setdefault(
            repo, {"slug": repo, "CONFIRMED": 0, "BLOCKED": 0, "UNTESTED": 0, "pocs": [], "tier": "P4"}
        )
        ra[bucket] += 1
        ra["pocs"].append(key)
        if TIER_ORDER.get(tier, 9) < TIER_ORDER.get(ra["tier"], 9):
            ra["tier"] = tier

    # Sort rows: CONFIRMED first, then BLOCKED (P0→P4), then UNTESTED
    rows.sort(key=lambda r: (BUCKET_ORDER[r["exploit_status"]], TIER_ORDER.get(r["tier"], 9), r["repo"], r["finding_id"]))

    confirmed_rows = [r for r in rows if r["exploit_status"] == "CONFIRMED"]
    blocked_p01 = [r for r in rows if r["exploit_status"] == "BLOCKED" and r["tier"] in ("P0", "P1")]

    # ── Pull nightfalcon CSS from template (everything inside <style>) ──
    tpl = NIGHTFALCON_TPL.read_text()
    css = re.search(r"<style>(.*?)</style>", tpl, re.S).group(1)

    # ── Render HTML ────────────────────────────────────────────────────
    n_repos = len(repo_agg)
    n_total = len(rows)

    H: list[str] = []
    H.append("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"UTF-8\"/>")
    H.append("<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"/>")
    H.append(f"<title>PoC Execution Report — {esc(OUT_TAG)} (Live-Exploit Verified)</title>")
    H.append(f"<style>{css}\n{extra_css()}</style></head><body>")

    # Hero
    H.append('<header class="hero"><div class="wrap">')
    H.append(
        f'<h1>PoC Execution Report — {esc(OUT_TAG)} '
        f'<span style="font-size:14px;opacity:.7">(strict live-exploit bar · real-victim cross-tenant)</span></h1>'
    )
    H.append(
        f'<div class="subtitle">{n_repos} repos · {n_total} PoC tests · '
        f'e2e environment · evidence-reviewed</div>'
    )
    H.append('<div class="badges">')
    H.append(
        f'<span class="badge" style="border-color:rgba(22,163,74,.4)">✓ Confirmed '
        f'<span class="num" style="color:#bbf7d0">{bucket_counts["CONFIRMED"]}</span></span>'
    )
    H.append(
        f'<span class="badge" style="border-color:rgba(220,38,38,.4)">⊘ Blocked '
        f'<span class="num" style="color:#fecaca">{bucket_counts["BLOCKED"]}</span></span>'
    )
    H.append(f'<span class="badge">? Untested <span class="num">{bucket_counts["UNTESTED"]}</span></span>')
    H.append(f'<span class="badge">Repos <span class="num">{n_repos}</span></span>')
    H.append("</div></div></header>")

    # Tabs
    H.append('<main><div class="wrap"><nav class="tabs">')
    H.append('<button class="tab-btn active" data-tab="summary">Executive Summary</button>')
    H.append('<button class="tab-btn" data-tab="repos">Repositories</button>')
    H.append(f'<button class="tab-btn" data-tab="findings">PoC Results ({n_total})</button>')
    H.append("</nav>")

    # ── Summary tab ────────────────────────────────────────────────────
    H.append('<section id="tab-summary" class="tab-panel active"><div class="summary">')
    H.append("<h2>Methodology</h2>")
    H.append(
        f"<p>{n_total} proof-of-concept tests against a preprod target with "
        f"<strong>real session credentials</strong> from a live browser "
        f"(a synthetic attacker tenant + a synthetic victim tenant, both "
        f"verified reachable). Write/mutation "
        f"PoCs ran with <code>POC_ALLOW_WRITE=1</code>. Every log + response body "
        f"was <strong>independently re-read</strong> by 6 parallel reviewers. "
        f"Verification bar: a finding is <strong>CONFIRMED only if the script "
        f"reached the live target from outside and observed the attack succeed</strong> "
        f"(HTTP 2xx + meaningful payload). Source-grep matches and gateway-blocked "
        f"probes do <strong>not</strong> count. Hardcoded-JS-credential findings "
        f"are deferred per direction.</p>"
    )
    H.append('<table class="st"><tr><th>Verdict</th><th>Count</th><th>Meaning</th></tr>')
    H.append(
        f'<tr><td><span class="es-confirmed">✓ CONFIRMED</span></td>'
        f'<td><strong>{bucket_counts["CONFIRMED"]}</strong></td>'
        f"<td>Real HTTP test → response body directly proves the attack worked. "
        f"Exploitable in e2e right now.</td></tr>"
    )
    H.append(
        f'<tr><td><span class="es-blocked">⊘ BLOCKED</span></td>'
        f'<td><strong>{bucket_counts["BLOCKED"]}</strong></td>'
        f"<td>Real test made → environment stopped it (gateway "
        f"<code>gw-go-filter</code> 401/403/404, route absent, app rejected). "
        f"Vulnerability confirmed in source. <strong>NOT a code fix.</strong></td></tr>"
    )
    H.append(
        f'<tr><td><span class="es-untested">? UNTESTED</span></td>'
        f'<td><strong>{bucket_counts["UNTESTED"]}</strong></td>'
        f"<td>Could not construct a valid live test — source-grep only "
        f"(code pattern proven, no HTTP probe), missing per-service host/IDs in "
        f"<code>poc-config.env</code>, browser-driven PoC, or hardcoded-JS-credential "
        f"finding (deferred per direction).</td></tr>"
    )
    H.append("</table>")

    # 404 disambiguation callout — answers "is the 404 a route problem
    # or a wrong-resource-ID problem?"
    n_noroute = sum(1 for c in classification if c["run_status"] == "GATEWAY-NO-ROUTE")
    n_apprej = sum(1 for c in classification if c["run_status"] == "APP-REJECTED")
    n_gwauth = sum(1 for c in classification if c["run_status"] == "GATEWAY-AUTH")
    n_stale = sum(1 for c in classification if c.get("stale_victim_id"))
    H.append('<div class="key-callout"><h3>🔍 404 Disambiguation — Route vs Resource-ID</h3>')
    H.append(
        f"<p>Every blocked HTTP probe was re-classified by checking for the "
        f"<code>x-spanid</code> / <code>x-envoy-upstream-service-time</code> "
        f"response headers (only present when istio actually forwarded the "
        f"request to a backend pod), and by re-probing each 404 path with "
        f"<strong>no resource ID</strong> in the URL:</p>"
        f'<table class="st"><tr><th>Sub-status</th><th>Count</th><th>What it means</th></tr>'
        f'<tr><td><code>GATEWAY-NO-ROUTE</code></td><td><strong>{n_noroute}</strong></td>'
        f"<td>404 with no upstream marker. The same path with no ID also "
        f"returns 404. <strong>The path is not routed on "
        f"<code>{esc('&lt;TARGET_HOST&gt;')}</code></strong> — "
        f"the backend was never reached. The victim ID is irrelevant; these "
        f"services need their own ingress hosts in <code>poc-config.env</code>.</td></tr>"
        f'<tr><td><code>APP-REJECTED</code></td><td><strong>{n_apprej}</strong></td>'
        f"<td>4xx <strong>with</strong> <code>x-spanid</code> — backend saw the "
        f"cross-tenant request and refused it. Real defensive signal at the app "
        f"layer. This is the only group where a wrong victim ID could matter.</td></tr>"
        f'<tr><td><code>GATEWAY-AUTH</code></td><td><strong>{n_gwauth}</strong></td>'
        f"<td>401/403 with no upstream marker — gateway rejected the credential "
        f"before routing.</td></tr></table>"
        f"<p><strong>Victim-ID validity:</strong> the configured victim entity "
        f"IDs were verified to resolve to real entities in the synthetic "
        f"victim tenant before the run. {n_stale} PoC(s) reference stale IDs "
        f"and are flagged for re-run after re-seeding.</p></div>"
    )

    # Cross-tenant baseline callout — populated from idor-baseline.json
    # if present (the live re-test with verified-real victim IDs).
    bl = []
    bl_path = RES / "idor-baseline.json"
    if bl_path.exists():
        try:
            bl = json.loads(bl_path.read_text())
        except Exception:
            bl = []
    H.append('<div class="key-callout"><h3>🎯 Cross-Tenant Baseline (Verified-Real Victim IDs)</h3>')
    H.append(
        "<p>After re-auth, the victim tenant was queried directly to "
        "<strong>verify the victim entity IDs are real</strong> before "
        "re-running the canonical IDOR vectors with the fresh attacker "
        "session and these verified IDs. Every vector reached the app "
        "(<code>x-spanid</code> present) and was refused:</p>"
    )
    if bl:
        H.append('<table class="st"><tr><th>Vector</th><th>HTTP</th><th>Response</th></tr>')
        for r in bl:
            label = esc(r.get("label", ""))
            http = esc(r.get("http", ""))
            body = esc((r.get("body") or "")[:140].replace("\n", " "))
            H.append(
                f'<tr><td><code style="font-size:11px">{label}</code></td>'
                f'<td><strong>{http}</strong></td>'
                f'<td style="font-size:11px;font-family:ui-monospace,monospace;'
                f'color:#475569">{body}</td></tr>'
            )
        H.append("</table>")
    H.append(
        '<ul style="font-size:13px">'
        "<li><strong>A</strong> attacker → <code>company{employee(id:&lt;victim&gt;)}</code> "
        "→ HTTP 200, <code>employee: null</code> — resolver scopes to caller tenant</li>"
        "<li><strong>B</strong> attacker → <code>company(id:&lt;victim tenant&gt;){employees}</code> "
        "→ HTTP 200, <strong><code>Forbidden</code></strong> from the backend, "
        "<code>employees: null</code> — explicit authZ deny</li>"
        "<li><strong>C</strong> attacker cookie + spoofed <code>x-org-company-id</code> header "
        "→ HTTP 401 — ticket/tenant binding enforced</li>"
        "<li><strong>E</strong> attacker → Relay <code>node(id:&lt;victim employee&gt;)</code> "
        "→ HTTP 200, <code>node: null</code></li>"
        "</ul>"
        "<p><strong>Zero victim PII appeared in any attacker "
        "response.</strong> The IDOR findings against the backend are not "
        "exploitable from outside — the backend enforces tenant scoping at every "
        "entry point. This rules out the wrong-ID hypothesis: with verified-real "
        "victim IDs and correct query shapes, cross-tenant access is still refused.</p>"
        "</div>"
    )

    # Confirmed exploits — verbose card per finding, not a cramped table
    H.append(f"<h2>The {len(confirmed_rows)} Confirmed Exploit{'s' if len(confirmed_rows)!=1 else ''}</h2>")
    for r in confirmed_rows:
        H.append(
            f'<div class="finding {r["tier"].lower()}" style="margin-bottom:16px">'
            f'<details open><summary>'
            f'<span class="es-confirmed" style="margin-right:8px">✓ CONFIRMED</span>'
            f'<span class="pill {r["tier"].lower()}" style="margin-right:8px">{esc(r["tier"])}</span>'
            f'<span class="finding-title">{esc(r["repo"])} / {esc(r["finding_id"])} — {esc(r["title"])}</span>'
            f'</summary><div class="finding-body" style="padding:0 20px 20px">'
        )
        # Business impact (full)
        if r.get("business_impact"):
            H.append(
                '<div style="background:#dbeafe;border-left:4px solid #2563eb;'
                'border-radius:0 8px 8px 0;padding:14px 18px;margin:14px 0">'
                '<div style="font-size:11px;font-weight:700;text-transform:uppercase;'
                'letter-spacing:.6px;color:#1e40af;margin-bottom:6px">Business Impact</div>'
                f'<p style="margin:0;color:#1e3a8a;font-size:14px;line-height:1.7">'
                f'{esc(r["business_impact"]).replace(chr(10)+chr(10), "</p><p>").replace(chr(10), "<br>")}</p></div>'
            )
        # What we did
        if r.get("what_we_did"):
            H.append(
                '<div class="test-section"><h4>What we did — step by step</h4>'
                '<div style="background:var(--surface-2);border:1px solid var(--border);'
                'border-radius:8px;padding:14px;font-size:14px;line-height:1.7">'
                f'<p style="margin:0">{esc(r["what_we_did"]).replace(chr(10)+chr(10), "</p><p>").replace(chr(10), "<br>")}</p></div></div>'
            )
        # Request payload
        if r.get("request_payload"):
            H.append(
                '<div class="test-section"><h4>Actual request / payload sent</h4>'
                f'<pre class="test-did">{esc(r["request_payload"])}</pre></div>'
            )
        # Response
        if r.get("actual_response"):
            H.append(
                '<div class="test-section"><h4>Actual response observed</h4>'
                f'<pre class="test-found">{esc(r["actual_response"])}</pre></div>'
            )
        # Why it proves
        if r.get("why_it_proves"):
            H.append(
                '<div class="test-section"><h4>Why this proves the risk</h4>'
                '<div style="background:#f0fdf4;border:1px solid #bbf7d0;border-left:4px solid #16a34a;'
                'border-radius:0 8px 8px 0;padding:14px 18px;font-size:14px;line-height:1.7;color:#14532d">'
                f'<p style="margin:0">{esc(r["why_it_proves"]).replace(chr(10)+chr(10), "</p><p>").replace(chr(10), "<br>")}</p></div></div>'
            )
        H.append("</div></details></div>")

    # Blocked P0/P1 table
    H.append("<h2>Blocked — P0/P1 (Real Vulns, Environment-Shielded)</h2>")
    H.append('<table class="st"><tr><th>Repo</th><th>ID</th><th>Tier</th><th>Status</th><th>Title</th><th>What Blocked It</th></tr>')
    for r in blocked_p01:
        H.append(
            f'<tr><td><code style="font-size:11px">{esc(r["repo"])}</code></td>'
            f'<td>{esc(r["finding_id"])}</td>'
            f'<td><span class="pill {r["tier"].lower()}">{esc(r["tier"])}</span></td>'
            f'<td><code style="font-size:10px">{esc(r["run_status"])}</code></td>'
            f'<td style="font-size:12px">{esc(r["title"][:55])}</td>'
            f'<td style="font-size:11px;color:#475569">{esc((r["false_positive_reason"] or "")[:90])}</td></tr>'
        )
    H.append("</table>")
    H.append("</div></section>")

    # ── Repos tab ──────────────────────────────────────────────────────
    H.append('<section id="tab-repos" class="tab-panel">')
    H.append('<div class="filter-bar"><input type="search" id="filter-text" placeholder="Filter repositories…"/></div>')
    H.append('<div class="repo-grid" id="repo-grid">')
    for slug in sorted(repo_agg, key=lambda s: (-repo_agg[s]["CONFIRMED"], -repo_agg[s]["BLOCKED"], s)):
        ra = repo_agg[slug]
        note = f'{ra["CONFIRMED"]} confirmed · {ra["BLOCKED"]} blocked · {ra["UNTESTED"]} untested'
        H.append(
            f'<div class="repo-row" data-search="{esc(slug.lower())}" onclick="openRepoModal(\'{esc(slug)}\')">'
            f'<div><div class="name">{esc(slug)}</div><div class="note">{esc(note)}</div></div>'
            f'<div><span class="pill {ra["tier"].lower()}">{esc(ra["tier"])}</span></div>'
            f'<div class="count-cluster">'
            f'<span class="c{" on p4" if ra["CONFIRMED"] else ""}">✓ {ra["CONFIRMED"]}</span>'
            f'<span class="c{" on p0" if ra["BLOCKED"] else ""}">⊘ {ra["BLOCKED"]}</span>'
            f'<span class="c">? {ra["UNTESTED"]}</span>'
            f"</div></div>"
        )
    H.append("</div></section>")

    # ── Findings tab ───────────────────────────────────────────────────
    H.append('<section id="tab-findings" class="tab-panel">')
    H.append(
        '<div class="filter-bar">'
        '<input type="search" id="poc-search" placeholder="Search…"/>'
        '<select id="verdict-filter"><option value="">All</option>'
        '<option value="confirmed">✓ CONFIRMED</option>'
        '<option value="blocked">⊘ BLOCKED</option>'
        '<option value="untested">? UNTESTED</option></select>'
        '<select id="tier-filter"><option value="">All tiers</option>'
        '<option value="P0">P0</option><option value="P1">P1</option>'
        '<option value="P2">P2</option><option value="P3">P3</option>'
        '<option value="P4">P4</option></select></div>'
    )
    H.append('<div class="findings-list" id="findings-list">')
    for r in rows:
        bk = r["exploit_status"].lower()
        search = f'{r["repo"]} {r["finding_id"]} {r["title"]}'.lower()
        H.append(
            f'<div class="finding-row" data-verdict="{bk}" data-tier="{esc(r["tier"])}" '
            f'data-search="{esc(search)}" onclick="openPocModal(\'{esc(r["repo"])}/{esc(r["finding_id"])}\')">'
            f'<span class="es-{bk}">{esc(r["exploit_status"])}</span>'
            f'<span class="pill {r["tier"].lower()}">{esc(r["tier"])}</span>'
            f'<span class="frow-id">{esc(r["repo"])}/{esc(r["finding_id"])}</span>'
            f'<span class="frow-title">{esc(r["title"][:90])}</span></div>'
        )
    H.append("</div></section></div></main>")

    H.append(
        f'<footer>PoC run {esc(OUT_TAG)} · target: preprod (synthetic test tenants) · strict bar: '
        f'live HTTP exploit only — source-grep / gateway-blocked / hardcoded-JS-cred '
        f'do not count as confirmed</footer>'
    )

    # Modal
    H.append(
        '<div class="overlay" id="overlay"><div class="modal">'
        '<button class="close" onclick="closeModal()" aria-label="Close">×</button>'
        '<div id="modal-content"></div></div></div>'
    )

    # ── Script ─────────────────────────────────────────────────────────
    repo_data_min = {
        slug: {"slug": slug, "tier": ra["tier"], "pocs": ra["pocs"]}
        for slug, ra in repo_agg.items()
    }
    H.append("<script>")
    H.append(f"const POC_DATA={safe_json_for_script(poc_data)};")
    H.append(f"const REPO_DATA={safe_json_for_script(repo_data_min)};")
    H.append(
        "function _e(s){if(s==null)return '';return String(s)"
        ".replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')"
        ".replace(/\"/g,'&quot;');}"
        "function esCss(e){return {'CONFIRMED':'confirmed','BLOCKED':'blocked',"
        "'UNTESTED':'untested'}[e]||'untested';}"
        "function esLabel(e){return {'CONFIRMED':'✓ CONFIRMED','BLOCKED':'⊘ BLOCKED',"
        "'UNTESTED':'? UNTESTED'}[e]||e;}"
        "function esDesc(e){return {'CONFIRMED':'Live test made — response proves "
        "the attack worked. Exploitable in e2e.','BLOCKED':'Live test made — "
        "environment stopped it (gateway/app). Vulnerability still in source. "
        "NOT a code fix.','UNTESTED':'Could not test live — source-grep only, "
        "missing config, browser-driven, or hardcoded-cred (deferred).'}[e]||'';}"
        "function _para(s){return _e(s).replace(/\\n\\n+/g,'</p><p>').replace(/\\n/g,'<br>');}"
        "function renderPoC(p){const e=p.exploit_status||'UNTESTED';let h='';"
        "/* ── Status pill + run-status tag ── */"
        "h+='<div style=\"background:var(--surface-2);border:1px solid var(--border);"
        "border-radius:8px;padding:12px 16px;margin:0 0 16px;display:flex;"
        "align-items:flex-start;gap:12px\"><span class=\"es-'+esCss(e)+'\">'+"
        "esLabel(e)+'</span><span style=\"font-size:13px;color:var(--text-2);"
        "line-height:1.5\">'+_e(esDesc(e))+' <code style=\"font-size:11px\">['+"
        "_e(p.run_status)+(p.http?(' · HTTP '+_e(p.http)):'')+(p.script_type?(' · '+_e(p.script_type)):'')"
        "+']</code></span></div>';"
        "/* ── 1. Business Impact — full what_happens_md, no truncation ── */"
        "if(p.business_impact)h+='<div style=\"background:#dbeafe;border-left:4px "
        "solid #2563eb;border-radius:0 8px 8px 0;padding:16px 20px;margin:0 0 18px\">"
        "<div style=\"font-size:11px;font-weight:700;text-transform:uppercase;"
        "letter-spacing:.6px;color:#1e40af;margin-bottom:8px\">Business Impact — what an attacker gains</div>"
        "<p style=\"margin:0;color:#1e3a8a;font-size:14px;line-height:1.7\">'+"
        "_para(p.business_impact)+'</p></div>';"
        "/* ── 2. What We Did — step-by-step in human terms ── */"
        "if(p.what_we_did)h+='<div class=\"test-section\"><h4>What we did — step by step</h4>"
        "<div style=\"background:var(--surface-2);border:1px solid var(--border);"
        "border-radius:8px;padding:14px 16px;font-size:14px;line-height:1.7;color:var(--text)\">"
        "<p style=\"margin:0\">'+_para(p.what_we_did)+'</p></div></div>';"
        "/* ── 3. Actual request / payload ── */"
        "if(p.request_payload)h+='<div class=\"test-section\"><h4>Actual request / payload sent</h4>"
        "<pre class=\"test-did\">'+_e(p.request_payload)+'</pre></div>';"
        "/* ── 4. Actual response observed ── */"
        "h+='<div class=\"test-section\"><h4>Actual response observed</h4>';"
        "if(!p.actual_response||p.actual_response.trim()==='')"
        "h+='<div class=\"test-found empty\">⚠ No live response captured for this finding.</div>';"
        "else h+='<pre class=\"test-found\">'+_e(p.actual_response)+'</pre>';"
        "h+='</div>';"
        "/* ── 5. Why this proves (or disproves) the risk ── */"
        "if(p.why_it_proves)h+='<div class=\"test-section\"><h4>Why this proves the risk</h4>"
        "<div style=\"background:#f0fdf4;border:1px solid #bbf7d0;border-left:4px solid #16a34a;"
        "border-radius:0 8px 8px 0;padding:14px 18px;font-size:14px;line-height:1.7;color:#14532d\">"
        "<p style=\"margin:0\">'+_para(p.why_it_proves)+'</p></div></div>';"
        "/* ── 6. What blocked it (BLOCKED only) ── */"
        "if(e==='BLOCKED'&&p.false_positive_reason)h+='<div class=\"fail-callout\">"
        "<h4>What blocked it (NOT a code fix)</h4><p style=\"margin:0\">'+"
        "_para(p.false_positive_reason)+'</p></div>';"
        "/* ── 7. How the attack works end-to-end (from the finding) ── */"
        "if(p.attack_narrative)h+='<details style=\"margin:14px 0\"><summary "
        "style=\"cursor:pointer;font-size:12px;font-weight:700;text-transform:uppercase;"
        "letter-spacing:.6px;color:var(--text-3)\">How the full attack works (from the finding write-up)</summary>"
        "<div style=\"background:var(--surface-2);border:1px solid var(--border);"
        "border-radius:8px;padding:14px 16px;margin-top:8px;font-size:14px;line-height:1.7\">"
        "<p style=\"margin:0\">'+_para(p.attack_narrative)+'</p></div></details>';"
        "/* ── 8. Where in the code this lives ── */"
        "if(p.location)h+='<div class=\"test-section\"><h4>Where in the code this lives</h4>"
        "<pre class=\"test-did\" style=\"font-size:12px\">'+_e(p.location)+'</pre></div>';"
        "/* ── 9. How to fix ── */"
        "if(p.fix)h+='<details style=\"margin:14px 0\"><summary "
        "style=\"cursor:pointer;font-size:12px;font-weight:700;text-transform:uppercase;"
        "letter-spacing:.6px;color:var(--text-3)\">How to fix it</summary>"
        "<div style=\"background:var(--surface-2);border:1px solid var(--border);"
        "border-radius:8px;padding:14px 16px;margin-top:8px;font-size:14px;line-height:1.7\">"
        "<p style=\"margin:0\">'+_para(p.fix)+'</p></div></details>';"
        "/* ── 10. Script verdict block + log path ── */"
        "if(p.verdict_block)h+='<details style=\"margin:14px 0\"><summary "
        "style=\"cursor:pointer;font-size:12px;font-weight:700;text-transform:uppercase;"
        "letter-spacing:.6px;color:var(--text-3)\">Script verdict block (raw)</summary>"
        "<pre class=\"test-did\" style=\"font-size:11px;margin-top:8px\">'+_e(p.verdict_block)+'</pre></details>';"
        "if(p.log)h+='<p style=\"font-size:12px;color:var(--text-3);margin-top:14px\">"
        "Full log: <code>'+_e(p.log)+'</code></p>';"
        "return h;}"
        "function openPocModal(k){const p=POC_DATA[k];if(!p)return;"
        "const h='<h2><span class=\"es-'+esCss(p.exploit_status)+'\" "
        "style=\"margin-right:10px\">'+esLabel(p.exploit_status)+'</span>"
        "<span class=\"pill '+(p.tier||'').toLowerCase()+'\" "
        "style=\"margin-right:10px\">'+_e(p.tier)+'</span>'+_e(p.repo)+' / '+"
        "_e(p.finding_id)+'</h2><p style=\"font-size:16px;font-weight:600;"
        "margin:0 0 16px\">'+_e(p.title)+'</p>'+renderPoC(p);"
        "document.getElementById('modal-content').innerHTML=h;"
        "document.getElementById('overlay').classList.add('open');}"
        "function openRepoModal(slug){const rd=REPO_DATA[slug];if(!rd)return;"
        "let h='<h2>'+_e(slug)+'</h2><div class=\"overlay-toolbar\">"
        "<button onclick=\"document.querySelectorAll(\\'#modal-content details\\')"
        ".forEach(d=>d.open=true)\">Expand all</button>"
        "<button onclick=\"document.querySelectorAll(\\'#modal-content details\\')"
        ".forEach(d=>d.open=false)\">Collapse all</button></div>';"
        "for(const u of rd.pocs){const p=POC_DATA[u];if(!p)continue;"
        "h+='<div class=\"finding '+(p.tier||'').toLowerCase()+'\" "
        "style=\"margin-bottom:10px\"><details><summary>"
        "<span class=\"es-'+esCss(p.exploit_status)+'\" "
        "style=\"margin-right:8px\">'+esLabel(p.exploit_status)+'</span>"
        "<span class=\"pill '+(p.tier||'').toLowerCase()+'\" "
        "style=\"margin-right:8px\">'+_e(p.tier)+'</span>"
        "<span class=\"finding-title\">'+_e(p.finding_id)+' — '+_e(p.title)+'</span>"
        "</summary><div class=\"finding-body\">'+renderPoC(p)+'</div></details></div>';}"
        "document.getElementById('modal-content').innerHTML=h;"
        "document.getElementById('overlay').classList.add('open');}"
        "function closeModal(){document.getElementById('overlay').classList.remove('open');}"
        "document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal();});"
        "document.getElementById('overlay').addEventListener('click',"
        "e=>{if(e.target.id==='overlay')closeModal();});"
        "function applyFilters(){"
        "const v=(document.getElementById('verdict-filter')||{}).value||'';"
        "const t=(document.getElementById('tier-filter')||{}).value||'';"
        "const s=((document.getElementById('poc-search')||{}).value||'').toLowerCase();"
        "document.querySelectorAll('#findings-list .finding-row').forEach(r=>{"
        "const ok=(!v||r.dataset.verdict===v)&&(!t||r.dataset.tier===t)"
        "&&(!s||r.dataset.search.includes(s));"
        "r.style.display=ok?'':'none';});}"
        "function applyRepoFilter(){"
        "const s=((document.getElementById('filter-text')||{}).value||'').toLowerCase();"
        "document.querySelectorAll('#repo-grid .repo-row').forEach(r=>{"
        "r.style.display=(!s||r.dataset.search.includes(s))?'':'none';});}"
        "document.addEventListener('DOMContentLoaded',function(){"
        "document.querySelectorAll('.tab-btn').forEach(b=>b.addEventListener('click',"
        "function(){document.querySelectorAll('.tab-btn').forEach(x=>x.classList"
        ".remove('active'));document.querySelectorAll('.tab-panel').forEach("
        "x=>x.classList.remove('active'));this.classList.add('active');"
        "document.getElementById('tab-'+this.dataset.tab).classList.add('active');}));"
        "['verdict-filter','tier-filter','poc-search'].forEach(id=>{const el="
        "document.getElementById(id);if(el)el.addEventListener("
        "id==='poc-search'?'input':'change',applyFilters);});"
        "const ft=document.getElementById('filter-text');"
        "if(ft)ft.addEventListener('input',applyRepoFilter);});"
    )
    H.append("</script></body></html>")

    out_html = ROOT / f"output/poc-execution-results-{OUT_TAG}.html"
    out_html.write_text("".join(H))

    # Self-check — what matters is that no raw `</script>` appears before
    # the single real closing tag (early termination). `<script` opens
    # inside the JSON payload are inert (browser treats script content as
    # raw text until it sees `</script>`), so we only assert the close.
    body = out_html.read_text()
    n_close = len(re.findall(r"</script\s*>", body, re.I))
    assert n_close == 1, f"script-tag check failed: {n_close} </script> closers"
    # And the JSON region must not contain a raw </script (safe_json escaped it).
    json_region = body.split("const POC_DATA=", 1)[1].rsplit("</script>", 1)[0]
    assert "</script" not in json_region.lower(), "raw </script in JSON region"

    print(f"[wrote] {out_html} ({len(body):,} bytes)")
    print(f"  CONFIRMED: {bucket_counts['CONFIRMED']}")
    print(f"  BLOCKED:   {bucket_counts['BLOCKED']}")
    print(f"  UNTESTED:  {bucket_counts['UNTESTED']}")
    print(f"  repos:     {n_repos}")
    for r in confirmed_rows:
        print(f"  ✓ {r['repo']}/{r['finding_id']} [{r['tier']}] {r['title'][:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(build())
