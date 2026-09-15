#!/usr/bin/env python3
"""
Build the phase-8 executive HTML report from structured JSON + a static template.

Usage:
    build.py --input <executive-summary.json> --output <executive-report.html> [--template <path>]

Why this exists
---------------
Earlier phase-8 implementations had the subagent write the entire HTML file
itself — template, embedded data, and JS handlers as one long stream of
tokens. That surface failed in production: a finding body containing a
literal `</script>` ended the report's outer `<script>` block early and
broke every click handler. (The plugin's own report rendered the same
XSS payload it was warning about.)

This script inverts the architecture:

- The phase-8 subagent produces ONLY structured JSON (see the `--input`
  schema below). No HTML, no script tags, no escaping decisions.
- This script merges that JSON into `template.html` and writes the final
  HTML. All escaping (HTML, JSON-in-script, markdown→HTML conversion)
  happens here, deterministically, in Python stdlib.

Result: the template's `<script>` boundaries are immutable. The bug class
is eliminated.

Input JSON schema (see also scripts/report/sample-input.json)
-------------------------------------------------------------
{
  "report_title": "Security Review — 2026-05-25",
  "report_subtitle": "20 repos · adversarial methodology · phase 1–8",
  "footer_text": "Generated 2026-05-25 · No repo-identifying data was sent to external search engines",
  "totals": {"P0": 5, "P1": 3, "P2": 54, "P3": 79, "P4": 264, "clean_repos": 2},
  "summary_markdown": "# What we did\\n\\n…",         # rendered as the Executive Summary tab body
  "validation_corrections": [                          # I.6 — callout block in summary tab
    {"finding_id": "F-014", "repo": "graphql-api-service",
     "note": "spring-web 5.3.32 already patches CVE-2024-22243; downgraded P1 → P3."}
  ],
  "repos": [
    {
      "slug": "pay-disbursement",
      "repo_url": "https://github.com/org/pay-disbursement",  # findings-json v2: from receipt
      "commit_sha": "<40-char HEAD sha>",                     # findings-json v2: from receipt
      "multitenant_scope": true,                                    # findings-json v2: from receipt
      "most_severe": "P0",                            # enum §1; or "Clean" for clean repos
      "summary_note": "Two P0 cross-tenant flaws plus 8 P1 …",   # short list-row note
      "counts": {"P0": 7, "P1": 8, "P2": 3, "P3": 0, "P4": 0},
      "findings": [
        {
          "finding_id": "F-001",
          "tier": "P0",                                                      # derived from cvss_score (enums.md §1/§18)
          "cvss_score": 9.3,                                                  # CVSS v4.0 Base, canonical severity
          "cvss_vector": "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N",
          "cwe": "CWE-798",                                                   # optional secondary tag (enums.md §19); may be null
          "cve": null,                                                        # optional; set for dep-CVE findings
          "exposure": "EXTERNAL",                                            # enum §17 (EXTERNAL|INTERNAL|INTERNAL-RESTRICTED)
          "reachability": "Any unauthenticated caller on the internet.",     # v2: lifted from severity_md
          "exploitability": "Trivial — a single crafted request.",           # v2
          "impact": "Full privileged-support session takeover.",                   # v2
          "patch_status": "No fix in this repo's code path.",                # v2
          "title": "Anyone on the internet can mint a privileged-support login cookie",
          "category": "Broken authentication (home-rolled cookie signing)"
          "location": "src/auth/CookieSigner.java:42–78",
          "what_happens_md": "The cookie signer derives its HMAC key …",     # markdown
          "attack_steps_md": "1. Fetch the public entity ID…",                # markdown ordered list
          "evidence_md": "```java\\n…```",                                    # markdown code fence
          "fix_md": "Rotate the HMAC key to a server-only secret …",          # markdown
          "severity_md": "- Reachability. Any unauthenticated caller…",       # markdown bullets
          "validation": "CURRENT",                                             # enum §3 (optional)
          "poc": {                                                              # required tagged union: generated or skipped
            "script_path": "output/proof_of_concept/pay-disbursement/F-001-anyone-can-mint.sh",
            "script_type": "curl-shell",                                       # enum §15
            "verdict": "VALID",                                                # enum §14
            # Self-contained PoC (schema v4): parameters are declared INLINE in
            # the script itself (prefilled where safe, blank `# FILL:` for
            # IDs/hosts/tokens). The script does NOT source a shared .env to run.
            "guide_path": "output/proof_of_concept/pay-disbursement/POC-GUIDE-2026-05-25.md",  # shared per-repo doc of every PoC's params
            "config_path": "output/proof_of_concept/pay-disbursement/poc-config.env",  # OPTIONAL reference catalog only — never sourced at runtime
            "placeholders": ["TARGET_HOST", "AUTH_TOKEN", "VICTIM_TENANT_ID"]    # the inline params this script declares
          }
        }
      ]
    }
  ]
}

Markdown subset rendered (in `*_md` fields and `summary_markdown`)
------------------------------------------------------------------
- ATX headings (#, ##, ###, ####)
- Paragraphs (blank-line separated)
- Bulleted lists (-, *)
- Numbered lists (1., 2.)
- Fenced code blocks (```lang…```)
- Inline code (`x`)
- Bold (**x**) and italics (*x*)
- Plain HTML pass-through is NOT supported — embedded HTML in markdown
  is treated as literal text (entity-encoded), which is the safe default.

Safety
------
- All user-supplied strings pass through `html.escape(..., quote=True)`
  before being embedded in the HTML output.
- The `REPO_DETAIL` data is serialized via `json.dumps(..., ensure_ascii=False)`
  and then `</...>` sequences are escaped to `<\\/...>` to defeat HTML-parser
  premature-termination of the outer `<script>` block. This is the standard
  JSON-in-script-tag escape from OWASP.
- The script REFUSES to embed any raw `<script>` or `<style>` content
  pulled from user data, even if it appears inside a markdown code fence.
  Those are rendered as escaped text only.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path


# ── Markdown rendering (small, controlled subset) ────────────────────────────

_CODE_FENCE_RE = re.compile(r"^```([\w.+-]*)\s*$")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*([^\*\n]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^\*\n]+)\*(?!\*)")
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
_OL_RE = re.compile(r"^(\d+)\.\s+(.*)$")
_UL_RE = re.compile(r"^[-*]\s+(.*)$")


def _render_inline(text: str) -> str:
    """Render inline markdown spans (bold, italics, inline code) on an
    already-escaped string. Operates on entity-encoded input and produces
    HTML-safe output; the markdown markers themselves do not need escaping.
    """
    literals: list[str] = []

    def protect_literal(match: re.Match[str]) -> str:
        literals.append(match.group(1))
        return f"\x00NF_LITERAL_{len(literals) - 1}\x00"

    out = re.sub(r"\\([\\`*])", protect_literal, text)
    out = _INLINE_CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = _BOLD_RE.sub(lambda m: f"<strong>{m.group(1)}</strong>", out)
    out = _ITALIC_RE.sub(lambda m: f"<em>{m.group(1)}</em>", out)
    for index, literal in enumerate(literals):
        out = out.replace(f"\x00NF_LITERAL_{index}\x00", literal)
    return out


def render_markdown(md: str) -> str:
    """Render the controlled markdown subset to HTML. Input is treated as
    untrusted; raw HTML embedded in the markdown is entity-encoded.
    """
    if not md:
        return ""

    # First pass: HTML-escape the entire input. Markdown markers (#, *, -,
    # backticks, **) are ASCII characters that html.escape leaves alone, so
    # subsequent regexes still match.
    escaped = html.escape(md, quote=True)
    lines = escaped.split("\n")

    out: list[str] = []
    in_code = False
    code_lang = ""
    in_ol = False
    in_ul = False
    para: list[str] = []

    def flush_para():
        nonlocal para
        if para:
            joined = "<br>".join(_render_inline(s) for s in para)
            out.append(f"<p>{joined}</p>")
            para = []

    def close_lists():
        nonlocal in_ol, in_ul
        if in_ol:
            out.append("</ol>")
            in_ol = False
        if in_ul:
            out.append("</ul>")
            in_ul = False

    for raw in lines:
        line = raw.rstrip()

        # Code fence start/end
        m = _CODE_FENCE_RE.match(line)
        if m:
            flush_para()
            close_lists()
            if not in_code:
                in_code = True
                code_lang = m.group(1) or ""
                lang_attr = f' class="lang-{html.escape(code_lang)}"' if code_lang else ""
                out.append(f"<pre><code{lang_attr}>")
            else:
                in_code = False
                code_lang = ""
                out.append("</code></pre>")
            continue

        if in_code:
            # Inside a code block — emit verbatim (already HTML-escaped above).
            out.append(line)
            continue

        # Blank line — paragraph break
        if not line:
            flush_para()
            close_lists()
            continue

        # Heading
        hm = _HEADING_RE.match(line)
        if hm:
            flush_para()
            close_lists()
            level = len(hm.group(1))
            text = _render_inline(hm.group(2))
            # h1 collides with the page hero; demote to h2 minimum.
            level = max(level, 2)
            out.append(f"<h{level}>{text}</h{level}>")
            continue

        # Ordered list item
        om = _OL_RE.match(line)
        if om:
            flush_para()
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if not in_ol:
                out.append("<ol>")
                in_ol = True
            out.append(f"<li>{_render_inline(om.group(2))}</li>")
            continue

        # Unordered list item
        um = _UL_RE.match(line)
        if um:
            flush_para()
            if in_ol:
                out.append("</ol>")
                in_ol = False
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{_render_inline(um.group(1))}</li>")
            continue

        # Regular text — accumulate into a paragraph (preserves <br> on
        # explicit linebreaks within a single block).
        if in_ol or in_ul:
            # Continuation of the previous list item.
            out[-1] = out[-1][:-5] + " " + _render_inline(line) + "</li>"
        else:
            para.append(line)

    # End-of-input flush.
    flush_para()
    close_lists()
    if in_code:
        out.append("</code></pre>")

    return "\n".join(out)


# ── Finding-card rendering ───────────────────────────────────────────────────

_VALID_TIERS = ("P0", "P1", "P2", "P3", "P4")


def _most_severe_class(totals: dict) -> str:
    """Return the CSS class for the most-severe tier with a non-zero
    count in `totals`, or 'clean' when every tier is zero. Drives the
    headline-callout border colour."""
    for t in _VALID_TIERS:
        if int(totals.get(t, 0) or 0) > 0:
            return t.lower()
    return "clean"


def _tier_class(tier: str) -> str:
    """Map a tier value to its CSS class (lowercased). Returns 'p4' for unknown."""
    t = (tier or "").strip()
    return t.lower() if t in _VALID_TIERS else "p4"


# Display-only severity words shown alongside the canonical P-tier in the
# report (P0-P4 stays canonical everywhere else — enums, gates, phases).
_SEVERITY_WORD = {
    "P0": "Critical",
    "P1": "High",
    "P2": "Medium",
    "P3": "Low",
    "P4": "Informational",
}

# Exposure enum (references/enums.md §17) → CSS class + display label for the
# per-finding badge.
_EXPOSURE_META = {
    "EXTERNAL": ("exposure-external", "External"),
    "INTERNAL": ("exposure-internal", "Internal"),
    "INTERNAL-RESTRICTED": ("exposure-internal-restricted", "Internal-restricted"),
}


def _tier_label(tier: str) -> str:
    """Return 'P0 (Critical)' style label for display. Unknown tier → as-is."""
    t = (tier or "").strip()
    word = _SEVERITY_WORD.get(t)
    return f"{t} ({word})" if word else t


_CVSS_VECTOR_RE = re.compile(r"^CVSS:4\.0/[A-Za-z:/]+$")


def _render_cvss(f: dict) -> str:
    """Render the CVSS v4.0 Base line for a finding: score + vector +
    FIRST.org calculator link, plus an optional CWE tag. Returns "" when no
    usable CVSS data is present (Non-Regression: absence never breaks the
    report). All values are escaped; the calculator link is only built from a
    well-formed CVSS:4.0 vector."""
    score = f.get("cvss_score")
    vector = str(f.get("cvss_vector") or "").strip()
    cwe = str(f.get("cwe") or "").strip()
    if score is None and not vector and not cwe:
        return ""
    parts: list[str] = []
    if score is not None and score != "":
        try:
            parts.append(f'<strong>CVSS v4.0 Base:</strong> {float(score):.1f}')
        except (TypeError, ValueError):
            parts.append(f'<strong>CVSS v4.0 Base:</strong> {html.escape(str(score))}')
    if vector:
        v = html.escape(vector)
        if _CVSS_VECTOR_RE.match(vector):
            # Calculator anchor uses the raw vector; URL-safe chars only (/ : .).
            link = "https://www.first.org/cvss/calculator/4.0#" + vector
            parts.append(f'<code>{v}</code> '
                         f'(<a href="{html.escape(link)}">calculator</a>)')
        else:
            parts.append(f'<code>{v}</code>')
    if cwe and cwe.lower() != "null":
        parts.append(f'<span class="cwe-tag">{html.escape(cwe)}</span>')
    cve = str(f.get("cve") or "").strip()
    if cve and cve.lower() != "null":
        parts.append(f'<span class="cve-tag">{html.escape(cve)}</span>')
    if not parts:
        return ""
    return '    <p class="cvss-line">' + " &middot; ".join(parts) + "</p>"


def _report_href(ws_rel_path: str) -> str:
    """Convert a workspace-relative artifact path to an href relative to the
    report, which is written at `<workspace>/output/executive-report-*.html`.

    - Paths under `output/` (e.g. `output/proof_of_concept/<slug>/F-1.sh`) are
      siblings of the report → strip the leading `output/`.
    - Any other workspace-relative path (legacy `proof_of_concept/...`) is one
      level up from `output/` → prefix `../`.
    Returns an already-escaped href.
    """
    p = str(ws_rel_path or "")
    if p.startswith("output/"):
        rel = p[len("output/"):]
    else:
        rel = "../" + p
    return html.escape(rel)


def render_finding(f: dict) -> str:
    """Render one finding dict to an `<article class="finding tierN">…` block.
    Every string field is markdown-rendered or HTML-escaped. No raw HTML
    from `f` reaches the output unescaped.

    The finding is collapsible: a `<details open>` wraps the body so each
    finding is expanded by default but can be collapsed by clicking its
    header. The clickable `<summary>` carries the tier pill and the title.
    """
    tier = (f.get("tier") or "P4").strip()
    tier_class = _tier_class(tier)
    finding_id = html.escape(str(f.get("finding_id") or ""))
    title = html.escape(str(f.get("title") or "(untitled)"))
    category = html.escape(str(f.get("category") or ""))
    location = html.escape(str(f.get("location") or ""))
    validation = html.escape(str(f.get("validation") or ""))
    exposure_raw = str(f.get("exposure") or "").strip().upper()
    exp_class, exp_label = _EXPOSURE_META.get(exposure_raw, ("", ""))

    body: list[str] = []
    if exp_label:
        body.append(
            f'    <p class="exposure-line"><strong>Exposure:</strong> '
            f'<span class="exposure-badge {exp_class}">{html.escape(exp_label)}</span></p>'
        )
    # CVSS v4.0 Base: canonical score + vector + calculator link (enums.md §18).
    cvss_line = _render_cvss(f)
    if cvss_line:
        body.append(cvss_line)
    if category:
        body.append(f'    <p class="category"><strong>What kind of issue this is:</strong> {category}</p>')
    if location:
        body.append(f'    <p class="location"><strong>Where in the code this lives:</strong> {location}</p>')
    if f.get("what_happens_md"):
        body.append("    <h4>What happens</h4>")
        body.append("    " + render_markdown(f["what_happens_md"]))
    if f.get("attack_steps_md"):
        body.append("    <h4>How the attack works, step by step</h4>")
        body.append("    " + render_markdown(f["attack_steps_md"]))
    if f.get("evidence_md"):
        body.append("    <h4>Evidence from the code</h4>")
        body.append("    " + render_markdown(f["evidence_md"]))
    if f.get("fix_md"):
        body.append("    <h4>How to fix it</h4>")
        body.append("    " + render_markdown(f["fix_md"]))
    if f.get("severity_md"):
        body.append("    <h4>Severity, spelled out</h4>")
        body.append("    " + render_markdown(f["severity_md"]))
    references = f.get("references") or []
    if references:
        body.append("    <h4>References</h4>")
        body.append("    <ul class=\"finding-references\">")
        body.extend(f"      <li>{html.escape(str(reference))}</li>" for reference in references)
        body.append("    </ul>")

    # Proof-of-concept tagged union. Phase-6 emits a script per CONFIRMED
    # P0/P1/P2 finding; phase-7 copies it into findings-<DATE>.json; phase-8
    # passes it through verbatim. Render as a relative <a href> so the link
    # resolves when the HTML report is opened from the workspace root.
    poc = f.get("poc")
    if isinstance(poc, dict) and poc.get("script_path"):
        sp = html.escape(str(poc.get("script_path")))
        st = html.escape(str(poc.get("script_type") or ""))
        pv_raw = str(poc.get("verdict") or "")
        pv = html.escape(pv_raw)
        verdict_class = {
            "VALID": "ok",
            "NEEDS-REVISION": "warn",
            "INVALID": "bad",
        }.get(pv_raw, "")
        body.append("    <h4>Proof-of-concept script</h4>")
        body.append(
            f'    <p class="poc-link"><a href="{_report_href(poc.get("script_path"))}"><code>{sp}</code></a> '
            f'(<span class="poc-type">{st}</span>) — '
            f'<span class="poc-verdict {verdict_class}">reviewer verdict: {pv}</span>. '
            "Run only against a non-production environment you control.</p>"
        )
        # Required-parameters line. Self-contained PoC (schema v4): params are
        # declared INLINE in the script (prefilled where safe, blank `# FILL:`
        # for IDs/hosts/tokens) — the script runs WITHOUT sourcing a shared
        # .env. This line reminds the dev which inline values to set and links
        # the shared per-repo guide (guide_path) that documents them. The
        # optional catalog (config_path) is reference-only, never sourced.
        placeholders = poc.get("placeholders") or []
        guide_path = poc.get("guide_path")
        if isinstance(placeholders, list) and placeholders:
            keys_html = ", ".join(
                f"<code>{html.escape(str(k))}</code>" for k in placeholders
            )
            guide_html = (
                f' See <a href="{_report_href(guide_path)}">'
                f"<code>{html.escape(str(guide_path))}</code></a> for details."
                if guide_path
                else ""
            )
            body.append(
                f'    <p class="poc-requires">This script is self-contained — set '
                f"these parameters inline at the top of the script (or via env "
                f"vars) before running: {keys_html}.{guide_html}</p>"
            )
    elif isinstance(poc, dict) and poc.get("skipped"):
        # Skipped-PoC explanation. When `poc` is a {skipped:true,…}
        # object, render a muted one-liner stating why no script was
        # generated for this finding (§16 enum + free-text note).
        reason = html.escape(str(poc.get("reason") or ""))
        note = html.escape(str(poc.get("note") or ""))
        body.append("    <h4>Proof-of-concept script</h4>")
        body.append(
            f'    <p class="poc-skip">Not generated — '
            f'<span class="poc-skip-reason">{reason}</span>. {note}</p>'
        )

    # organization context citations (optional). Phase-6 emits these arrays
    # only when the finding overlaps a detected control; absent or empty
    # → no section rendered.
    caps = f.get("controls_in_scope") or []
    if isinstance(caps, list) and caps:
        items = []
        for c in caps:
            if not isinstance(c, dict):
                continue
            name = html.escape(str(c.get("name") or "").strip())
            cap_id = c.get("control_id")
            cap_id_str = f" ({html.escape(str(cap_id))})" if cap_id else ""
            note_path = c.get("note_path")
            label = f"{name}{cap_id_str}"
            if note_path:
                label = f"{label} — <code>{html.escape(str(note_path))}</code>"
            if name:
                items.append(f"    <li>{label}</li>")
        if items:
            body.append("    <h4>organization controls in scope</h4>")
            body.append("    <ul class=\"organization-controls\">")
            body.extend(items)
            body.append("    </ul>")

    pols = f.get("applicable_policies") or []
    if isinstance(pols, list) and pols:
        items = []
        for p in pols:
            if not isinstance(p, dict):
                continue
            pid = html.escape(str(p.get("id") or "").strip())
            ptitle = html.escape(str(p.get("title") or "").strip())
            bind = html.escape(str(p.get("binding_statement") or "").strip())
            note_path = p.get("note_path")
            parts = []
            if pid:
                parts.append(f"<strong>{pid}</strong>")
            if ptitle:
                parts.append(ptitle)
            head = " — ".join(parts) if parts else ""
            pol_body = f": {bind}" if bind else ""
            tail = f" — <code>{html.escape(str(note_path))}</code>" if note_path else ""
            if head or pol_body:
                items.append(f"    <li>{head}{pol_body}{tail}</li>")
        if items:
            body.append("    <h4>Applicable organization policies (standards)</h4>")
            body.append("    <ul class=\"organization-policies\">")
            body.extend(items)
            body.append("    </ul>")

    if validation:
        body.append(f'    <p class="validation"><strong>Validation:</strong> {validation}</p>')

    fid_label = f"{finding_id}: " if finding_id else ""
    exp_pill = (
        f'      <span class="exposure-badge {exp_class}">{html.escape(exp_label)}</span>'
        if exp_label else ""
    )
    summary_lines = [
        "    <summary>",
        f'      <span class="pill {tier_class}">{html.escape(_tier_label(tier))}</span>',
    ]
    if exp_pill:
        summary_lines.append(exp_pill)
    summary_lines += [
        f'      <span class="finding-title">{fid_label}{title}</span>',
        "    </summary>",
    ]
    sections: list[str] = [
        f'<article class="finding {tier_class}" data-fid="{finding_id}">',
        "  <details>",
        *summary_lines,
        '    <div class="finding-body">',
        *body,
        "    </div>",
        "  </details>",
        "</article>",
    ]
    return "\n".join(sections)


def _render_repo_meta(repo: dict) -> str:
    """Render the repo provenance line (repo URL + commit SHA) at the top of a
    repo's modal, so the source and exact commit are visible for ticketing.
    Both come from the per-repo findings-json top-level (schema v2)."""
    url = str(repo.get("repo_url") or "").strip()
    sha = str(repo.get("commit_sha") or "").strip()
    parts: list[str] = []
    if url and url.lower() != "null":
        if url.startswith(("https://", "http://")):
            parts.append(f'<a href="{html.escape(url)}">{html.escape(url)}</a>')
        else:
            parts.append(html.escape(url))
    if sha and sha.lower() != "null":
        parts.append(f'commit <code>{html.escape(sha)}</code>')
    if not parts:
        return ""
    return '<p class="repo-meta">' + " &middot; ".join(parts) + "</p>"


def render_repo_detail(repo: dict) -> str:
    """Render a repo's full findings block as HTML (consumed by the modal
    overlay via innerHTML). Findings are collapsed by default; the modal
    header's existing Expand-all / Collapse-all buttons toggle them."""
    meta = _render_repo_meta(repo)
    findings = sorted(
        repo.get("findings") or [],
        key=lambda f: (_TIER_ORDER.get((f.get("tier") or "P4").strip(), 9),
                       str(f.get("finding_id") or "")),
    )
    body = "\n".join(render_finding(f) for f in findings)
    if not body:
        body = '<p class="empty">No findings recorded for this repository.</p>'
    dismissed = repo.get("dismissed_findings") or []
    sections = [body, "<h3>Out of Scope / Dismissed</h3>"]
    if dismissed:
        sections.append("<ul>")
        sections.extend(
            "<li><strong>" + html.escape(str(item.get("finding_id") or ""))
            + ": " + html.escape(str(item.get("title") or "")) + "</strong> — "
            + html.escape(str(item.get("reason") or "")) + "</li>"
            for item in dismissed
        )
        sections.append("</ul>")
    else:
        sections.append("<p>No dismissed findings.</p>")
    coverage = repo.get("poc_coverage") or {}
    sections.extend((
        "<h3>Proof-of-Concept Coverage</h3>",
        f"<p>Generated: {int(coverage.get('generated', 0))}; "
        f"skipped: {int(coverage.get('skipped', 0))}.</p>",
        "<h3>Methodology Notes</h3>",
        "<ul>",
    ))
    sections.extend(f"<li>{html.escape(str(note))}</li>" for note in repo.get("methodology_notes") or [])
    sections.extend((
        "</ul>",
        "<h3>Disclaimer</h3>",
        f"<p>{html.escape(str(repo.get('disclaimer') or ''))}</p>",
    ))
    detail = "\n".join(sections)
    return (meta + "\n" + detail) if meta else detail


# ── Findings-tab rendering (flat per-repo list: pill + id + short title) ─────

_TIER_ORDER = {t: i for i, t in enumerate(_VALID_TIERS)}


def render_findings_tab(repos: list) -> str:
    """Render the "Findings" tab body: every finding listed as a flat
    (non-collapsible) row of pill + finding-id + short title, grouped under
    its repo heading and ordered by tier (P0 first). Each row carries
    `data-slug` / `data-fid` so a click opens that finding in the repo modal.
    Reuses the same escaping as every other renderer; no new input field."""
    groups: list[str] = []
    for repo in repos:
        slug = html.escape(str(repo.get("slug") or "(unknown)"))
        findings = repo.get("findings") or []
        if not findings:
            continue
        ordered = sorted(
            findings,
            key=lambda f: (_TIER_ORDER.get((f.get("tier") or "P4").strip(), 9),
                           str(f.get("finding_id") or "")),
        )
        rows: list[str] = []
        for f in ordered:
            tier = (f.get("tier") or "P4").strip()
            tier_class = _tier_class(tier)
            fid = html.escape(str(f.get("finding_id") or ""))
            title = html.escape(str(f.get("title") or "(untitled)"))
            search_blob = html.escape((str(f.get("title") or "") + " " + str(f.get("finding_id") or "")).lower())
            poc = f.get("poc") or {}
            has_poc = isinstance(poc, dict) and bool(poc.get("script_path"))
            poc_dot = (
                '<span class="poc-dot" title="Executable PoC available"></span>'
                if has_poc else ""
            )
            rows.append(
                f'<div class="finding-row" data-slug="{slug}" data-fid="{fid}" '
                f'data-tier="{tier_class}" data-search="{search_blob}">'
                f'<span class="pill {tier_class}">{html.escape(_tier_label(tier))}</span>'
                f'<span class="frow-id">{fid}</span>'
                f'<span class="frow-title">{title}{poc_dot}</span>'
                "</div>"
            )
        counts = repo.get("counts") or {}
        total = sum(int(counts.get(t, 0) or 0) for t in _VALID_TIERS) or len(findings)
        groups.append(
            '<section class="findings-group">'
            f'<h2 class="findings-group-title">{slug} '
            f'<span class="findings-group-count">{total} findings</span></h2>'
            f'<div class="findings-list">{"".join(rows)}</div>'
            "</section>"
        )
    return "\n".join(groups) if groups else '<p class="empty">No findings.</p>'


# ── Repo-row rendering ────────────────────────────────────────────────────────

def render_repo_row(repo: dict) -> str:
    slug = html.escape(str(repo.get("slug") or "(unknown)"))
    most_severe_raw = (repo.get("most_severe") or "P4").strip()
    most_severe_class = (
        most_severe_raw.lower() if most_severe_raw in (*_VALID_TIERS, "Clean") else "p4"
    )
    # Search-index attribute is lowercased slug + summary note for the filter.
    summary_note = str(repo.get("summary_note") or "")
    search_blob = (slug + " " + summary_note).lower()
    summary_note_html = html.escape(summary_note) if summary_note else "&nbsp;"

    counts = repo.get("counts") or {}
    count_chips: list[str] = []
    for tier in _VALID_TIERS:
        n = int(counts.get(tier, 0) or 0)
        cls = f"c on {tier.lower()}" if n > 0 else "c"
        count_chips.append(f'<span class="{cls}" title="{tier}">{tier} {n}</span>')

    pill_label = html.escape(_tier_label(most_severe_raw))
    return (
        f'<div class="repo-row" data-slug="{slug}" data-tier="{most_severe_class}" '
        f'data-search="{html.escape(search_blob)}">'
        f'<div><div class="name">{slug}</div><div class="note">{summary_note_html}</div></div>'
        f'<div><span class="pill {most_severe_class}">{pill_label}</span></div>'
        f'<div class="count-cluster">{"".join(count_chips)}</div>'
        "</div>"
    )


# ── Summary tab rendering (markdown + optional validation-corrections callout) ──

def render_summary(report: dict) -> str:
    """Render the Executive-summary tab as a structured layout that
    mirrors the markdown executive-summary's section order:
      Aggregate Findings → Per-Repository Summary → What we did →
      The headline → PoC summary → Recurring patterns → Executive
      narrative → validation-corrections → folded Methodology +
      Disclaimer.
    Falls back to flat `summary_markdown` rendering when the
    structured `summary` object is absent (back-compat).
    """
    structured_summary = report.get("summary")
    required_summary_fields = {
        "what_we_did_md",
        "headline_md",
        "priority_findings_md",
        "narrative_md",
        "review_outcomes_md",
        "methodology_md",
        "disclaimer_md",
    }
    if (
        not isinstance(structured_summary, dict)
        or not required_summary_fields.issubset(structured_summary)
    ) and report.get("summary_markdown"):
        return render_markdown(report["summary_markdown"])
    s = structured_summary or {}
    totals = report.get("totals") or {}
    pieces: list[str] = []

    # 1. Aggregate Findings — totals across all repos.
    if totals:
        agg_rows = "".join(
            f'<tr><td>{html.escape(_tier_label(t))}</td><td class="ct {t.lower()}'
            f'{" nz" if int(totals.get(t, 0) or 0) > 0 else ""}">'
            f"{int(totals.get(t, 0) or 0)}</td></tr>"
            for t in _VALID_TIERS
        )
        pieces.append(
            '<section class="summary-table">'
            "<h2>Aggregate findings</h2>"
            '<table class="repo-table aggregate">'
            "<thead><tr><th>Tier</th><th>Total</th></tr></thead>"
            f"<tbody>{agg_rows}</tbody></table></section>"
        )

    # 2. Per-repo summary table — Repository | P0..P4 | Most severe.
    #    Each <tr> carries data-slug so a click opens that repo's modal.
    repos = report.get("repos") or []
    if repos:
        rows: list[str] = []
        for r in repos:
            slug = html.escape(str(r.get("slug") or ""))
            counts = r.get("counts") or {}
            cells: list[str] = [f'<td class="rt-slug">{slug}</td>']
            for t in _VALID_TIERS:
                n = int(counts.get(t, 0) or 0)
                nz = " nz" if n > 0 else ""
                cells.append(f'<td class="ct {t.lower()}{nz}">{n}</td>')
            ms_raw = str(r.get("most_severe") or "")
            ms_class = (
                ms_raw.lower() if ms_raw in (*_VALID_TIERS, "Clean") else "p4"
            )
            cells.append(
                f'<td class="rt-sev"><span class="pill {ms_class}">'
                f"{html.escape(_tier_label(ms_raw))}</span></td>"
            )
            rows.append(f'<tr data-slug="{slug}">{"".join(cells)}</tr>')
        head = (
            "<th>Repository</th>"
            + "".join(f"<th>{t}</th>" for t in _VALID_TIERS)
            + "<th>Most severe</th>"
        )
        pieces.append(
            '<section class="summary-table">'
            "<h2>Per-repository summary</h2>"
            '<table class="repo-table">'
            f"<thead><tr>{head}</tr></thead>"
            f'<tbody>{"".join(rows)}</tbody>'
            "</table></section>"
        )

    # 3. What we did — methodology paragraph (visible, not folded).
    if s.get("what_we_did_md"):
        pieces.append(
            '<section class="what-we-did"><h2>What we did</h2>'
            + render_markdown(s["what_we_did_md"])
            + "</section>"
        )

    # 4. The headline — the signature callout. Border colour matches
    #    the most-severe non-zero tier.
    if s.get("headline_md"):
        sev = _most_severe_class(totals)
        pieces.append(
            f'<section class="headline-callout {sev}">'
            '<div class="hc-eyebrow">The headline</div>'
            f'<div class="hc-body">{render_markdown(s["headline_md"])}</div>'
            "</section>"
        )

    # 5. Priority findings — concise decision context for P0/P1 findings,
    #    generated directly from validated records.
    if s.get("priority_findings_md"):
        pieces.append(
            '<section class="priority-findings">'
            "<h2>Priority findings requiring action</h2>"
            + render_markdown(s["priority_findings_md"])
            + "</section>"
        )

    # 6. PoC summary strip — total scripts by verdict + links to every repo's
    #    PoC guide. Self-contained PoCs (v4): params are inline in each script;
    #    the per-repo POC-GUIDE-<DATE>.md documents them. Prefer guide_paths[];
    #    fall back to config_paths[] (the optional reference catalog) for v3.
    poc = report.get("poc_summary") or {}
    if poc:
        guide_list = poc.get("guide_paths") or poc.get("config_paths") or []
        is_guides = bool(poc.get("guide_paths"))
        cfg_links = " · ".join(
            f'<a href="{_report_href(p)}">'
            f'<code>{html.escape(str(p).rsplit("/", 2)[-2] if "/" in str(p) else str(p))}</code></a>'
            for p in guide_list
        )
        tail = (
            (" — each PoC is self-contained (params inline); "
             f"see the per-repo guide: {cfg_links}")
            if is_guides
            else (f" — parameter catalog (reference only): {cfg_links}")
        ) if cfg_links else ""
        pieces.append(
            '<section class="poc-coverage"><h2>Proof-of-Concept Coverage</h2>'
            '<div class="poc-strip">'
            f'<span class="poc-strip-count">{int(poc["total"])} executable PoC '
            f'script{"s" if int(poc["total"]) != 1 else ""}</span> '
            f'(<span class="ok">{int(poc.get("valid", 0))} valid</span> · '
            f'<span class="warn">{int(poc.get("needs_revision", 0))} needs-revision</span> · '
            f'<span class="bad">{int(poc.get("invalid", 0))} invalid</span> · '
            f'<span class="neutral">{int(poc.get("unreviewed", 0))} unreviewed/other</span>)'
            + tail
            + "</div></section>"
        )

    # 7. Recurring patterns worth acting on — prose paragraphs, one per
    #    cluster: bolded title, affected-repo list, remediation text.
    patterns = report.get("recurring_patterns") or []
    if patterns:
        items: list[str] = []
        for p in patterns:
            title_txt = html.escape(str(p.get("title") or p.get("tag") or ""))
            repos_list = p.get("repos") or []
            repo_join = ", ".join(
                f"<code>{html.escape(str(r))}</code>" for r in repos_list
            )
            rem = render_markdown(p.get("remediation_md") or "")
            items.append(
                '<div class="pattern-item">'
                f'<p class="pattern-lead"><strong>{title_txt}.</strong> '
                f"Affects {len(repos_list)} "
                f'repo{"s" if len(repos_list) != 1 else ""} and '
                f'{int(p.get("finding_count", 0) or 0)} findings: {repo_join}.</p>'
                f"{rem}"
                "</div>"
            )
        pieces.append(
            '<section class="patterns">'
            "<h2>Recurring patterns worth acting on</h2>"
            + "".join(items)
            + "</section>"
        )

    # 8. Executive narrative.
    if s.get("narrative_md"):
        pieces.append(
            '<section class="narrative"><h2>Executive narrative</h2>'
            + render_markdown(s["narrative_md"])
            + "</section>"
        )

    # 9. Review outcomes — validation-state totals and dismissed candidates.
    if s.get("review_outcomes_md"):
        pieces.append(
            '<section class="review-outcomes"><h2>Review outcomes</h2>'
            + render_markdown(s["review_outcomes_md"])
            + "</section>"
        )

    # 10. Validation-corrections callout (unchanged behaviour).
    corrections = report.get("validation_corrections") or []
    if corrections:
        items_html: list[str] = []
        for c in corrections:
            fid = html.escape(str(c.get("finding_id") or ""))
            repo = html.escape(str(c.get("repo") or ""))
            note = html.escape(str(c.get("note") or ""))
            items_html.append(f"<li><code>{fid}</code> ({repo}) — {note}</li>")
        pieces.append(
            '<div class="callout">'
            "<h3>What phase-5 validation corrected</h3>"
            "<ul>" + "".join(items_html) + "</ul>"
            "</div>"
        )

    # 11. Methodology + disclaimer — folded into a <details>.
    meth: list[str] = []
    for k, label in (
        ("methodology_md", "Methodology"),
        ("disclaimer_md", "Disclaimer"),
    ):
        if s.get(k):
            meth.append(f"<h3>{html.escape(label)}</h3>{render_markdown(s[k])}")
    if meth:
        pieces.append(
            '<details class="methodology">'
            "<summary>Methodology, scope, and disclaimer</summary>"
            + "".join(meth)
            + "</details>"
        )

    return "\n".join(pieces)


# ── Defensive serializer for the REPO_DETAIL JSON literal ────────────────────

_SCRIPT_END_RE = re.compile(r"</(script|style)", flags=re.IGNORECASE)


def safe_json_for_script(obj) -> str:
    """Serialize `obj` to a JSON literal safe to embed inside a `<script>`
    block. Replaces `</script` / `</style` (case-insensitive) with
    `<\\/script` / `<\\/style` so the HTML parser does not terminate the
    surrounding `<script>` tag early. This is the OWASP-recommended escape.

    Additional defense: also escape `<!--` and `]]>` which are HTML-parser
    significant inside `<script>` content under the legacy CDATA rules.
    """
    encoded = json.dumps(obj, ensure_ascii=False)
    encoded = _SCRIPT_END_RE.sub(lambda m: "<\\/" + m.group(1), encoded)
    encoded = encoded.replace("<!--", "<\\!--").replace("]]>", "]\\]>")
    return encoded


# ── Template substitution ────────────────────────────────────────────────────

def build_html(report: dict, template: str) -> str:
    totals = report.get("totals") or {}
    repos = report.get("repos") or []

    repo_rows_html = "\n".join(render_repo_row(r) for r in repos)
    repo_detail_map = {r["slug"]: render_repo_detail(r) for r in repos if r.get("slug")}
    findings_tab_html = render_findings_tab(repos)

    substitutions = {
        "{{REPORT_TITLE}}":   html.escape(str(report.get("report_title")    or "Security Review")),
        "{{REPORT_SUBTITLE}}":html.escape(str(report.get("report_subtitle") or "")),
        "{{FOOTER_TEXT}}":    html.escape(str(report.get("footer_text")     or "")),
        "{{COUNT_P0}}":       html.escape(str(int(totals.get("P0", 0) or 0))),
        "{{COUNT_P1}}":       html.escape(str(int(totals.get("P1", 0) or 0))),
        "{{COUNT_P2}}":       html.escape(str(int(totals.get("P2", 0) or 0))),
        "{{COUNT_P3}}":       html.escape(str(int(totals.get("P3", 0) or 0))),
        "{{COUNT_P4}}":       html.escape(str(int(totals.get("P4", 0) or 0))),
        "{{COUNT_CLEAN}}":    html.escape(str(int(totals.get("clean_repos", 0) or 0))),
        "{{SUMMARY_HTML}}":   render_summary(report),
        "{{REPO_ROWS_HTML}}": repo_rows_html,
        "{{FINDINGS_TAB_HTML}}": findings_tab_html,
        "{{REPO_DETAIL_JSON}}": safe_json_for_script(repo_detail_map),
    }

    out = template
    for placeholder, value in substitutions.items():
        out = out.replace(placeholder, value)
    return out


# ── Self-check: the produced HTML must have exactly one script-tag pair ──────

def self_check(html_str: str) -> tuple[bool, list[str]]:
    """Verify the produced HTML is structurally sound. Returns (ok, errors)."""
    errors: list[str] = []

    n_open = len(re.findall(r"<script\b", html_str, flags=re.IGNORECASE))
    n_close = len(re.findall(r"</script\s*>", html_str, flags=re.IGNORECASE))
    if n_open != 1:
        errors.append(f"expected exactly one <script> opening tag, found {n_open}")
    if n_close != 1:
        errors.append(f"expected exactly one </script> closing tag, found {n_close}")

    # The single </script> must be at the end of the file (after the data
    # block). Check the data block doesn't contain a raw </script>.
    if "REPO_DETAIL = " in html_str:
        head, _, after = html_str.partition("REPO_DETAIL = ")
        # `after` runs from the JSON literal to end-of-file. The substring
        # between the JSON's start and the trailing </script> must contain
        # only `<\/script` (escaped), not raw `</script`. Scan up to the
        # last </script> in the file.
        last_close = after.rfind("</script>")
        if last_close < 0:
            errors.append("REPO_DETAIL block present but no closing </script> found")
        else:
            json_region = after[:last_close]
            if "</script>" in json_region.lower():
                errors.append(
                    "found raw '</script>' inside REPO_DETAIL JSON literal — "
                    "the safe_json_for_script escape failed"
                )

    return (not errors, errors)


# ── CLI entrypoint ───────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    here = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--input", required=True, type=Path,
                   help="Path to the structured JSON input (see schema in docstring).")
    p.add_argument("--output", required=True, type=Path,
                   help="Path to write the resulting HTML report.")
    p.add_argument("--template", type=Path, default=here / "template.html",
                   help="Path to the HTML template. Defaults to template.html "
                        "next to this script.")
    args = p.parse_args(argv)

    try:
        report = json.loads(args.input.read_text())
    except FileNotFoundError:
        print(f"ERROR: input file not found: {args.input}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(f"ERROR: input JSON failed to parse: {e}", file=sys.stderr)
        return 2

    try:
        template = args.template.read_text()
    except FileNotFoundError:
        print(f"ERROR: template not found: {args.template}", file=sys.stderr)
        return 2

    html_out = build_html(report, template)
    ok, errors = self_check(html_out)
    if not ok:
        for e in errors:
            print(f"ERROR: self-check failed: {e}", file=sys.stderr)
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html_out)
    print(f"wrote {args.output} ({len(html_out):,} bytes) — self-check OK", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
