# Context Scope — What Each Phase Subagent Receives

The orchestrator assembles a minimal context packet before invoking each subagent.
Subagents receive **only** the files listed below — nothing more.

This keeps each subagent's context window lean and prevents earlier phase output
from polluting later phase analysis.

## Agent-journal boundary

Ordinary phase workers must not receive agent conversation journal content, the
journal path, `agent-journal.py context` output, parent conversation history,
or the full `state.json`. They receive no run log except for phase-7's
explicitly listed review-mode run-log input. The main orchestrator alone reads the
authoritative state and manifest. Every worker receives only this closed typed
`WORKER_STATE_ENVELOPE`: `session_id`, `current_phase`, and `epoch_id`, plus
the explicitly listed non-journal scalar inputs for its phase (for example
`DATE`, `REPO_SLUG`, `REPO_SLUGS`, and `RUN_MODE`). Journal requests are write-only typed `record` calls;
they contain only IDs, enum codes, and bounded counts — never raw transcript,
prompts, reasoning, source excerpts, PoCs, credentials, commands/output,
finding prose, or arbitrary free text. The exact phase packets below remain
authoritative.

`WORKER_STATE_ENVELOPE` is included in every packet; `state.json` is never a
worker input.

## Blind child role matrix

### Blind DA

Allowed: only the phase-4 DA prompt, current finding ID/type, one code/evidence
excerpt, one-sentence claim, current-round Primary statement, and round number.
Must not receive: journal path, journal content, context projection, full
state.json, run log, or parent conversation.

### Judge

Allowed: only the finding's stable `F-NNN` ID, reviewer labels `A` and `B`,
each reviewer `verdict`, `accuracy_ok`, `safety_ok`, and `completeness_ok`
fields, and the retry-round scalar. Do not pass reviewer reasoning or fixes.
Must not receive: journal path, journal content, context projection, full
state.json, run log, or parent conversation.

### Generator

Allowed: only the finding's stable `F-NNN` ID, final disposition enum,
one-sentence claim, candidate-embedded evidence excerpt, selected `script_type`,
and `placeholders[]`.
Must not receive: journal path, journal content, context projection, full
state.json, run log, or parent conversation.

### Reviewer

Allowed: only the phase-6 reviewer prompt, finding ID, claim, code excerpt,
script content, script type, placeholders, and reviewer label.
Must not receive: journal path, journal content, context projection, full
state.json, run log, or parent conversation.

---

## phase-0 — Clone
```
WORKER_STATE_ENVELOPE
input/repos-<date>.txt
```
No upstream analysis context. Entry phase.

**Writes:** `output/run-log-<date>.md`, `sourcecode/<slug>/`

**Does NOT receive:** any findings, dataflow, or candidates context.

---

## phase-1 — Scope Filter
```
state.json
WORKER_STATE_ENVELOPE
references/organization_context/_graph.json   ← PRIMARY KB lookup: fingerprint_index, controls[], platform_services[], standards[]
references/organization_context/  (whole tree, read-only) ← context fallback; open a specific note via entry.note_path
references/OWASP/_graph.json                  ← PRIMARY OWASP dispatcher: frameworks[], system_kind_index{}, fingerprint_index{}
references/OWASP/  (whole tree, read-only)    ← OWASP framework markdowns; phase-1 opens the ones the graph dispatches
references/analysis-contracts.md
scripts/dependency-inventory.py               ← static only; no package-manager execution or network
```
Reads source directly from `sourcecode/<slug>/`.
No prior analysis context needed — this is a fresh look at the code.

**Writes:**
- `findings/<slug>/dataflow-<date>.md` (`## External Attack Surface`
  section — header name retained for gate compatibility; now contains
  BOTH `### In-Scope Entry Points` (external) and `### Internal Surface`
  subsections, each entry point tagged with its exposure
  `EXTERNAL | INTERNAL | INTERNAL-RESTRICTED`, enums.md §17)
- `findings/<slug>/organization-context-<date>.md` (controls/platform-services/standards;
  sentinel acceptable if no Provider fingerprints match)
- `findings/<slug>/owasp-context-<date>.md` (system_kinds, frameworks in scope,
  per-framework applicable items with canonical citations, items explicitly
  skipped; sentinel `No OWASP frameworks in scope.` acceptable for the
  impossible-in-practice all-skip case)
- `findings/<slug>/dependency-inventory-<date>.json`

**Does NOT receive:** data flow map, candidates, debate, or findings context.

---

## phase-2 — Data Flow Mapping
```
WORKER_STATE_ENVELOPE
findings/<slug>/dataflow-<date>.md   ← External Attack Surface from phase-1
findings/<slug>/dependency-inventory-<date>.json
references/analysis-contracts.md
references/authorization-relationship.schema.json
references/business-logic-invariants.schema.json
```
Reads source directly from `sourcecode/<slug>/`.
Receives only the scope filter output — not candidates or findings.

**Writes:** appends `## Data Flow Map` to `findings/<slug>/dataflow-<date>.md`
and the structured `findings/<slug>/dataflow-<date>.json` (each flow carries
an `exposure` field, enums.md §17, copied from the phase-1 entry-point tag).
The JSON also carries `relationship_context`, `business_logic_invariants`, and
`outbound_edges`. After all per-repo outputs, orchestrator deterministically
writes `findings/cross-repository-topology-<date>.json`. Traces both external
and internal flows.

**Does NOT receive:** candidates, debate, validation, or findings context.

---

## phase-3 — Issue Identification & Scoring
```
WORKER_STATE_ENVELOPE
findings/<slug>/dataflow-<date>.md         ← full dataflow (scope + flows from phases 1-2)
findings/<slug>/dataflow-<date>.json       ← relationships, invariants, outbound edges
findings/<slug>/dependency-inventory-<date>.json
findings/cross-repository-topology-<date>.json
findings/<slug>/organization-context-<date>.md   ← controls/standards detected by phase-1 (or sentinel)
findings/<slug>/owasp-context-<date>.md    ← PRIMARY OWASP source for phase-3: frameworks dispatched, items applicable, canonical citations
references/organization_context/_graph.json  ← consulted on demand for full STD entries
references/organization_context/  (whole tree, read-only)  ← fallback only; raw STD bodies for P0/P1/P2 where the graph is too terse
references/OWASP/  (whole tree, read-only)  ← fallback only; raw OWASP framework markdowns when an item summary in owasp-context is insufficient
references/cvss-policy.md   ← CVSS v4.0 Base scoring policy (canonical severity; P-tier derived from score)
```
Reads source directly from `sourcecode/<slug>/`.
Receives the complete dataflow file — both attack surface and flows.

**Writes:** `findings/<slug>/candidates-<date>.md` (each candidate carries an
**Exposure** field `EXTERNAL | INTERNAL | INTERNAL-RESTRICTED`, enums.md §17,
copied from its phase-2 flow; internal findings are raised, not dropped — and
a CVSS-B **Score** + **Vector** (enums.md §18, canonical severity; tier derived
from it) plus an optional **CWE** id (enums.md §19)).

**Does NOT receive:** debate, validation, or findings report context.

---

## phase-4 — Adversarial Debate (per candidate)
```
WORKER_STATE_ENVELOPE
RUN_MODE: review | triage
findings/<slug>/candidates-<date>.md   ← scored candidates from phase-3
<code excerpt for the specific candidate being debated>
```
The DA subagent receives even less — only the code excerpt and the claim.
See `phases/phase-da.md` for DA context constraints.

**Writes:** `findings/<slug>/debate-<date>.md`, updates `candidates-<date>.md`

**Does NOT receive:** full dataflow, validation, or findings report context.
The orchestrator **must strip the `control_gap` field** from any candidate
record passed into phase-4 (Primary or DA). The DA stays context-blind
by design — knowing a candidate is `CONTROL-MISSING` would bias the
debate toward a "missing-control" framing instead of evaluating the
exploit on its merits.

---

## phase-5 — Online Validation
```
WORKER_STATE_ENVELOPE
findings/<slug>/candidates-<date>.md   ← with final dispositions from phase-4
findings/<slug>/debate-<date>.md       ← for tier confirmation
findings/<slug>/dependency-inventory-<date>.json ← package/version provenance for supply-chain validation
```
Does NOT receive source code — validation uses only generic terms, no code details.

**Writes:** updates `findings/<slug>/candidates-<date>.md` with Validation entries

**Does NOT receive:** dataflow, source code, or findings report context.

---

## phase-6 — Proof-of-Concept Generation
```
WORKER_STATE_ENVELOPE
findings/<slug>/candidates-<date>.md   ← dispositions + validations + attack steps + code evidence
findings/<slug>/debate-<date>.md       ← refined attack-path detail (when candidate alone insufficient)
```
Reads source excerpts ONLY from the candidate's "Evidence from the
code" section — does NOT re-open `sourcecode/<slug>/` beyond that.

**Writes:** self-contained PoC scripts
`output/proof_of_concept/<slug>/F-<NNN>-*.<ext>` (parameters declared inline —
they do NOT source a shared config), the single per-repo guide
`output/proof_of_concept/<slug>/POC-GUIDE-<date>.md`, the per-repo reference
catalog `output/proof_of_concept/<slug>/poc-config.env` (documentation only,
never sourced at runtime), the manifest
`output/proof_of_concept/<slug>/poc-manifest-<date>.json` (schema v4), and —
when any PoC was produced — the convenience runner
`output/proof_of_concept/run-all.sh` (and `lib/poc-common.sh` for `source-check`
re-clone only).

**Does NOT receive:** dataflow, organization-context, owasp-context, or
findings report context.

---

## phase-7 — Findings Report
```
WORKER_STATE_ENVELOPE
output/run-log-<date>.md                    ← review mode only
findings/<slug>/candidates-<date>.md       ← validated post-debate candidates
findings/<slug>/debate-<date>.md           ← final debate provenance
findings/<slug>/dataflow-<date>.md         ← review mode only; omitted in source-less triage
output/proof_of_concept/<slug>/poc-manifest-<date>.json
findings/<slug>/organization-context-<date>.md   ← optional; omitted in source-less triage
findings/<slug>/owasp-context-<date>.md    ← optional; omitted in source-less triage
FINDINGS_JSON_PATH: findings/<slug>/findings-<date>.json
RECEIPT_PATH: findings/<slug>/receipt-<date>.json
PATTERN_TAGS_PATH: findings/<slug>/pattern-tags-<date>.json
```
Structured projection phase only. Does not re-read source code.

**Worker writes:** validated schema-v2 `findings-<date>.json`, validator-bound
schema-v1 `receipt-<date>.json`, and schema-v1 `pattern-tags-<date>.json`.
The findings JSON preserves complete report prose, CVSS/exposure, provenance,
and the exact manifest-derived PoC tagged union for every final finding.

**Orchestrator builds:** `findings-<date>.md` with
`scripts/report/build-markdown.py` and `findings-<date>.sarif` with
`scripts/report/build-sarif.py`, both solely from validated findings JSON.

**Does NOT receive:** source code directly. **Does NOT author:** Markdown, SARIF,
HTML, or fenced report text.

---

## phase-8 — Executive Summary & HTML Report
```
WORKER_STATE_ENVELOPE
FINDINGS_JSON_PATHS: findings/<slug>/findings-<date>.json      ← sole report content source, all slugs in state order
PLUGIN_ROOT: <verified absolute plugin root>
EXECUTIVE_REPORT_JSON_PATH: output/executive-report-<date>.json
EXECUTIVE_SUMMARY_PATH: output/executive-summary-<date>.md
EXECUTIVE_REPORT_HTML_PATH: output/executive-report-<date>.html
EXECUTIVE_REPORT_SARIF_PATH: output/executive-report-<date>.sarif
```
Cross-repository projection only. Validated schema-v2 findings JSON is the sole
source for repo provenance, counts, finding bodies, executive JSON, HTML, and
SARIF. Receipts remain validator-bound audit artifacts, not report input.

**Worker writes:** no report content.

**Orchestrator builds:** executive JSON and executive-summary Markdown from
validated findings JSON, HTML from executive JSON, and aggregate SARIF from
each findings JSON.

**Does NOT receive:** receipts, pattern tags, state-derived summaries,
per-repository findings Markdown, candidates, debate transcripts, dataflow
maps, or source code. **Does NOT author:** generated report artifacts.

---

## Context Budget Rule

Each phase subagent should receive files totalling no more than ~50K tokens.
If a file is too large to pass in full, the orchestrator passes a summary:

| File | Max size before summarising |
|------|-----------------------------|
| dataflow-<date>.md | 20K tokens |
| candidates-<date>.md | 30K tokens |
| debate-<date>.md | 20K tokens |
| poc-manifest-<date>.json | 10K tokens |
| findings-<date>.md | 40K tokens |

If a file exceeds its limit, the orchestrator reads it and passes a condensed
version: section headers + key data only, not full prose.

---

## What the DA Subagent Receives (phase-4 internal)

The DA is intentionally context-blind, and it is **never evaluated inline** in
the phase-4 conversation — it always runs as a fresh, isolated subagent
spawned via the Agent tool, one fresh DA per round per finding (never reused).

**Passed to the DA — exactly this, nothing more:**
```
<DA prompt (phases/phase-da.md) as system prompt>
<finding F-NNN ID + finding_type + applicable hallucination patterns>
<code excerpt: the specific file and line range only — the only code it sees>
<candidate claim: one sentence>
<primary's statement for THIS round only>
<current round number>
<WORKSPACE path>
```

**Never passed to the DA (each item below defeats blindness):**
- Agent conversation journal content, journal path, or `context` output
- Scoring breakdown, tier, or justification
- Prior debate rounds (this finding or any other)
- The Primary's internal reasoning or confidence
- Any other candidate or finding
- The dataflow map or any other prior phase output (candidates, debate
  transcript, run log)
- Validation results
- The orchestrator's or phase-4's conversation history

If the DA can see any "never passed" item, it returns `BLIND_VIOLATION` and is
re-spawned cleanly (see `phases/phase-da.md`).

---

## What the PoC Reviewer Subagent Receives (phase-6 internal)

The PoC reviewer is intentionally context-blind — same isolation
discipline as the DA:

```
<PoC script content — full file>
<code excerpt: the specific file and line range only>
<candidate claim: one sentence>
<script_type>
<placeholders[] — the inline parameters this self-contained PoC declares>
<F-NNN ID + reviewer label A|B>
```

**Never passed to either reviewer:**
- Agent conversation journal content, journal path, or `context` output
- Scoring breakdown, tier, or severity justification
- Debate transcript or prior phase output
- Other findings or other PoC scripts
- The other reviewer's verdict
- Provider / OWASP context
- The reference catalog (`poc-config.env`) contents (the reviewer checks
  that the script is self-contained — parameters declared inline, no
  shared-config source — not any catalog values)
