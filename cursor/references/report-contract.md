# NightFalcon Report Artifact Contract

Claude, Codex, and Cursor ports emit the same final artifact layout and JSON
semantics. `<DATE>` is the run date from `state.json`; `<slug>` is each exact
entry in `state.json.repo_slugs`.

## Required per-repository artifacts

```text
findings/<slug>/
├── findings-<DATE>.md
├── findings-<DATE>.json
├── findings-<DATE>.sarif
├── pattern-tags-<DATE>.json
└── receipt-<DATE>.json
```

`findings-<DATE>.json` schema version is exactly `"2"`. Its `repo_slug`,
`repo_url`, `commit_sha`, `multitenant_scope`, and `date` identify reviewed source.
In review mode, `multitenant_scope` is a JSON boolean normalized from phase-0's
`multitenant-scope=YES | NO` run-log token; JSON strings `"YES"` and `"NO"` are
invalid. In source-less triage only, use the string `"n/a (triage)"`.
The phase-7 gate independently re-derives the review-mode boolean from the
matching run-log entry and blocks receipt/findings values that disagree.
Provenance values must match corresponding schema-v1 receipt exactly. Every
finding contains canonical CVSS v4.0 Base score, complete Base vector,
mechanically derived P-tier, `cwe` and `cve` keys, exposure, reachability,
exploitability, impact, patch status, report prose, validation status, and PoC
metadata. Every finding also contains a non-empty `references` string array.
Top-level `dismissed_findings`, `poc_coverage`, `methodology_notes`, and
`disclaimer` fields preserve the required Out of Scope / Dismissed, Proof-of-Concept
Coverage, Methodology Notes, and Disclaimer report sections. These fields are
canonical schema, not arbitrary extensions. `cwe` or `cve` may be JSON `null`
when not applicable; CVSS may not.

`dismissed_findings` is the sorted, unique, exact projection of all post-debate
candidates whose final disposition is `DISMISSED`. Each entry's `finding_id`,
`title`, and `reason` are copied verbatim from that candidate's ID,
`Dismissed Finding Title`, and `Dismissal Reason`; fabricated, omitted,
duplicated, or rewritten entries are invalid.

Optional `controls_in_scope` objects contain exactly `name`,
`control_id`, and `note_path`; the ID is `CTRL-NNNN` or JSON `null`, and the
path is repository-relative under `references/organization_context/`. Optional
`applicable_policies` objects contain exactly `id`, `title`,
`binding_statement`, and `note_path`; the ID is `STD-NNNN`, both text fields
are non-empty, and the path has the same context catalog prefix. Duplicate
control name/ID pairs and duplicate policy IDs are invalid. Markdown and
HTML render these same validated fields canonically.

Validated schema-v2 findings JSON is the sole report source for the repository.
`findings-<DATE>.md` is deterministically built with
`scripts/report/build-markdown.py`; per-repository SARIF is deterministically
built with `scripts/report/build-sarif.py`. Neither projection is an independent
source of truth. The phase-7 gate rebuilds and byte-compares both.

## Required aggregate artifacts

```text
output/
├── executive-summary-<DATE>.md
├── executive-report-<DATE>.json
├── executive-report-<DATE>.html
└── executive-report-<DATE>.sarif
```

`scripts/report/build-executive.py` deterministically builds both executive
JSON and executive-summary Markdown from the validated per-repository findings
JSON inputs. Executive JSON has fixed wrapper fields and contains exactly one
repo entry for every state slug, copying each repository's URL, commit SHA,
scope, findings, dismissed findings, PoC coverage, methodology notes, and
disclaimer. Findings are ordered by canonical severity (`P0` through `P4`),
then finding ID. Builder also derives detailed executive sections: aggregate
and per-repository counts, review scope, headline risk, P0/P1 action context,
PoC verdict coverage, repeated OWASP tags and exact risk categories, repository
narrative, validation and dismissal outcomes, methodology, and disclaimer.
When final schema-v2 inputs omit validation-correction history, review outcomes
state that limitation and never infer severity-change rationale. Every section
is projected only from validated schema-v2 records; agents may not author or
override it. HTML is deterministically rebuilt from executive JSON. Aggregate
SARIF is deterministically rebuilt from every per-repository findings JSON.
Gates rebuild and byte-compare all projections and reject missing, stale,
duplicated, substituted, unknown, or fabricated content.

## Canonical severity

CVSS v4.0 Base is canonical. Gates recompute score from vector and then derive
tier: P0=9.0-10.0, P1=7.0-8.9, P2=4.0-6.9, P3=0.1-3.9, P4=0.0. No manual tier
override or severity floor exists. HTML links every vector to FIRST calculator.

Canonical machine labels are always `P0`, `P1`, `P2`, `P3`, and `P4`. Reports
may accompany them with these exact **display-only aliases**: `P0 (Critical)`,
`P1 (High)`, `P2 (Medium)`, `P3 (Low)`, and `P4 (Informational)`. Aliases never
replace canonical labels in JSON, SARIF properties, gates, manifests, receipts,
or orchestration decisions. Human-readable HTML and Markdown render
reachability, exploitability, impact, and patch status once, inside
**Severity, spelled out**; structured JSON retains their separate fields.
