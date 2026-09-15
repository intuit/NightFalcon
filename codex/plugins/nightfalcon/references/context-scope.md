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

`WORKER_STATE_ENVELOPE` is included in every phase-worker packet; `state.json`
is never a worker input. Its model fields remain initial provenance only at the
orchestrator boundary, not a runtime expectation.
NightFalcon never selects or switches models. Omit the `model` argument and
every reasoning override so Codex uses the model currently selected by the
user. A user may change that selection at any time. Already-running agents
keep their launch model; later turns and new agents use the new selection.
Never reject, retry, or reroute work because the observed model differs.

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

## triage-ingest — External Backlog Normalization
```
WORKER_STATE_ENVELOPE
BACKLOG_PATH: input/triage-backlog.<ext>
REPO_SLUGS: <single slug>
REPO_PATH: <optional source repo path; omitted in source-less triage>
```
This phase receives one external backlog and no prior NightFalcon analysis.
`REPO_PATH` is optional and is omitted in source-less triage. When source or a
backlog code excerpt is unavailable, preserve the claim with the exact
`EVIDENCE_UNAVAILABLE — external finding supplied without repository/code excerpt`
sentinel. Never invent source, framework context, locations, or quotations.

**Writes:**
- `findings/<slug>/candidates-<date>.md`
- exact `output/run-log-<date>.md` ingest note

**Does NOT receive:** phase-1 dataflow/framework context, debate transcripts,
validation, receipts, pattern tags, or findings reports.

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
  section — header name retained for gate compatibility; now contains BOTH
  `### In-Scope Entry Points` (external) and `### Internal Surface`
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
DATAFLOW_PATH: findings/<slug>/dataflow-<date>.md   ← External Attack Surface from phase-1
DATAFLOW_JSON_PATH: findings/<slug>/dataflow-<date>.json   ← new structured projection
DEPENDENCY_INVENTORY_PATH: findings/<slug>/dependency-inventory-<date>.json
references/analysis-contracts.md
references/authorization-relationship.schema.json
references/business-logic-invariants.schema.json
```
Reads source directly from `sourcecode/<slug>/`.
Receives only the scope filter output — not candidates or findings.

**Writes:**
- appends `## Data Flow Map` to `findings/<slug>/dataflow-<date>.md`
  and the structured `findings/<slug>/dataflow-<date>.json` (each flow carries
  an `exposure` field, enums.md §17, copied from the phase-1 entry-point tag);
  traces both external and internal flows
- `findings/<slug>/dataflow-<date>.json` (structured JSON projection of the
  same flows plus `relationship_context`, `business_logic_invariants`, and
  `outbound_edges`; required alongside the markdown)
- `findings/cross-repository-topology-<date>.json` after all repositories

**Does NOT receive:** candidates, debate, validation, or findings context.

---

## phase-3 — Issue Identification & Scoring
```
WORKER_STATE_ENVELOPE
DATAFLOW_PATH: findings/<slug>/dataflow-<date>.md         ← full dataflow (scope + flows from phases 1-2)
DATAFLOW_JSON_PATH: findings/<slug>/dataflow-<date>.json  ← structured flow projection from phase-2
DEPENDENCY_INVENTORY_PATH: findings/<slug>/dependency-inventory-<date>.json
TOPOLOGY_PATH: findings/cross-repository-topology-<date>.json
findings/<slug>/organization-context-<date>.md   ← controls/standards detected by phase-1 (or sentinel)
findings/<slug>/owasp-context-<date>.md    ← PRIMARY OWASP source for phase-3: frameworks dispatched, items applicable, canonical citations
references/organization_context/_graph.json  ← consulted on demand for full STD entries
references/organization_context/  (whole tree, read-only)  ← fallback only; raw STD bodies for P0/P1/P2 where the graph is too terse
references/OWASP/  (whole tree, read-only)  ← fallback only; raw OWASP framework markdowns when an item summary in owasp-context is insufficient
references/cvss-policy.md                   ← CVSS v4.0 Base scoring policy (canonical severity; P-tier derived from score)
```
Reads source directly from `sourcecode/<slug>/`.
Receives the complete dataflow file — both attack surface and flows.

**Writes:** `findings/<slug>/candidates-<date>.md` (each candidate carries a
**CVSS-B Score + Vector** (enums.md §18, the canonical severity; tier derived
from it), an optional **CWE** id (§19), and an **Exposure** field
`EXTERNAL | INTERNAL | INTERNAL-RESTRICTED` (§17) copied from its phase-2 flow;
internal findings are raised, not dropped).

**Does NOT receive:** debate, validation, or findings report context.

---

## phase-4 — Adversarial Debate (per candidate)
```
WORKER_STATE_ENVELOPE
RUN_MODE: review | triage
findings/<slug>/candidates-<date>.md   ← scored candidates from phase-3
<code excerpt for the specific candidate being debated>
MAX_AGENT_THREADS: <file-resolved configured agents.max_threads>   ← concurrency bound only
```
The DA subagent receives only the authorized blind packet defined below — no
phase files or parent context. See `phases/phase-da.md` for its constraints.

**Writes:** `findings/<slug>/debate-<date>.md`, updates
`candidates-<date>.md`, and the phase-4 parent appends sanitized blind-leaf
injection notices to the exact `output/run-log-<date>.md` when signaled.

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
findings/<slug>/dependency-inventory-<date>.json ← package/version provenance
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
POC_GUIDE_PATH: output/proof_of_concept/<slug>/POC-GUIDE-<date>.md   ← single per-repo guide (all PoCs' params)
POC_CONFIG_PATH: output/proof_of_concept/<slug>/poc-config.env       ← per-repo reference catalog (NOT sourced at runtime)
POC_INVOKER_PATH: output/proof_of_concept/run-all.sh                 ← convenience batch runner (PoCs run standalone without it)
POC_LIB_PATH: output/proof_of_concept/lib/poc-common.sh              ← clone helper for source-check only
SOURCE_DIR_ROOT: sourcecode/                            ← generated PoC runtime wiring only
REPO_MAP_PATH: input/repo-map-<date>.txt                ← generated PoC re-checkout wiring only
MAX_AGENT_THREADS: <file-resolved configured agents.max_threads>   ← concurrency bound only
```
There is no `SOURCE_PATH` in the Phase-6 packet. The worker reads source
excerpts ONLY from the candidate's "Evidence from the code" section and must
not directly open or search `sourcecode/<slug>/`. `SOURCE_DIR_ROOT` and
`REPO_MAP_PATH` are generated PoC runtime and re-checkout wiring only, not
Phase-6 analysis inputs.

**Writes:**
- per repo: self-contained PoC scripts
  `output/proof_of_concept/<slug>/F-<NNN>-*.<ext>` (parameters declared inline —
  they do NOT source a shared config), `output/proof_of_concept/<slug>/output/`,
  the single per-repo guide `output/proof_of_concept/<slug>/POC-GUIDE-<date>.md`,
  the per-repo reference catalog `output/proof_of_concept/<slug>/poc-config.env`
  (documentation only, never sourced at runtime), and
  `output/proof_of_concept/<slug>/poc-manifest-<date>.json` (schema v4)
- root, only when at least one runnable PoC exists: the convenience runner
  `output/proof_of_concept/run-all.sh` (and
  `output/proof_of_concept/lib/poc-common.sh` for `source-check` re-clone only)
- exact `output/run-log-<date>.md`: the phase-6 parent appends sanitized
  blind-reviewer injection notices only when a typed envelope signals one

Each PoC is self-contained: it declares every parameter inline (prefilled where
safe, blank `# FILL:` otherwise) and does NOT source a shared config. The
per-repo `poc-config.env` is a reference catalog only — never sourced at
runtime.

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
created by a fresh Codex nested-agent spawn, one fresh DA per round per
finding (never reused). The phase-4 worker waits for each required DA; a spawn
or wait failure is a runtime block, never permission to evaluate inline.

**Passed to the DA — exactly this, nothing more:**
```
<DA prompt (phases/phase-da.md) as system prompt>
<finding F-NNN ID + finding_type + applicable hallucination patterns>
<code excerpt: the specific file and line range only — the only code it sees>
<candidate claim: one sentence>
<primary's statement for THIS round only>
<current round number>
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

Every normal DA envelope contains the required boolean
`prompt_injection_detected`. The blind leaf has no date or logging context and
never writes a log. When the boolean is true, the parent worker uses its own
phase context to append one sanitized line to the canonical run log, without
copying or paraphrasing attacker text, then continues with the full challenge.
The single-line `BLIND_VIOLATION` isolation response remains separate from the
normal typed envelope.

---

## What the PoC Reviewer Subagent Receives (phase-6 internal)

The PoC reviewer is intentionally context-blind — same isolation discipline
as the DA. Each reviewer is created by its own fresh Codex nested-agent spawn,
the phase-6 worker waits for both, and neither reviewer is reused:

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
- The contents of `poc-config.env` (the reviewer checks that the script is
  self-contained — parameters declared inline, no shared-config source — not
  any reference-catalog values)

Every normal PoC-reviewer envelope contains the required boolean
`prompt_injection_detected`. The blind leaf receives no workspace, date, or
logging context and never writes a log. When the boolean is true, the parent
worker uses its own phase context to append one sanitized line to the canonical
run log, without copying or paraphrasing attacker text, then continues with the
full review. The single-line `BLIND_VIOLATION` isolation response remains
separate from the normal typed envelope.
