# Enum Registry — single source of truth

This file is the canonical reference for every closed-enum field that
downstream phases parse. Every phase prompt cites this file rather than
re-defining enums inline; gate scripts validate against these exact
spellings.

**Rule:** if a field appears in this registry, downstream consumers
parse it as one of the listed values *exactly* (case-sensitive,
punctuation-sensitive). Free-string variants are gate violations.

Where a value contains a space-equivalent separator, **hyphens are
canonical**: `ACTIVELY-EXPLOITED`, not `ACTIVELY EXPLOITED`. This makes
the values shell-grep-friendly and avoids subtle whitespace-collapse
bugs in markdown parsing.

---

## 1. Severity tier (Phase 3 onward)

```
P0 | P1 | P2 | P3 | P4
```

P0 is the most severe, P4 is the least. Tier labels appear in section
headings, summary tables, and the receipt's `tier_initial` /
`tier_final` fields. Legacy word-only severity values are invalid; this plugin
uses canonical P-tiers only.

**The tier is derived mechanically from the CVSS v4.0 Base score**
(`references/cvss-policy.md`): P0 = 9.0–10.0, P1 = 7.0–8.9,
P2 = 4.0–6.9, P3 = 0.1–3.9, P4 = 0.0. The enum values are unchanged;
only their derivation is CVSS-based (the legacy D1–D4 rubric is retired).
The report may show the severity word alongside the tier
(`P0 (Critical)` … `P4 (Informational)`) — display only.

## 2. Disposition (Phase 4 → onward)

```
CONFIRMED | CONFIRMED-MODIFIED | DISMISSED | NEEDS-REVIEW
```

- `CONFIRMED` — finding stands as scored after the debate.
- `CONFIRMED-MODIFIED` — finding stands, but the debate refined the
  attack path, scoring, or scope. The candidate record must reflect
  the refinement; tier_initial vs tier_final in the receipt makes the
  change visible. Use this instead of free-form `Confirmed (downgraded)`
  / `Confirmed (reframed)` notes.
- `DISMISSED` — finding rejected (false positive, not reachable, fully
  mitigated, or hallucination-pattern with affirmative counter-evidence).
- `NEEDS-REVIEW` — ambiguous; flagged for human decision. The default
  for P0/P1 staleness-terminated debates.

Gate validation: `complete-phase.sh phase-4` requires the debate
transcript to contain at least one `Final Disposition:` line whose value
is in this set.

## 3. Validation status (Phase 5)

```
CURRENT | ACTIVELY-EXPLOITED | PATCHED | UNVERIFIED | NOT-RUN
```

- `CURRENT` — issue class confirmed active and unpatched for the version
  in use.
- `ACTIVELY-EXPLOITED` — on CISA KEV or with confirmed PoC/exploit in
  the wild. Escalates confidence on P0/P1 findings.
- `PATCHED` — issue class was patched in a version the repo already
  uses; finding is downgraded to INFO / P4.
- `UNVERIFIED` — could not find authoritative confirmation in the
  available sources; finding stands at its current tier with the
  uncertainty noted.
- `NOT-RUN` — validation was not attempted for this finding (e.g.,
  P3/P4 candidates, or `--no-validation-needed`).

Gate validation: `complete-phase.sh phase-5` requires the candidates
file's `Validation:` lines to use only these values. Use the hyphen
form (`ACTIVELY-EXPLOITED`, `NOT-RUN`), not the space form.

## 4. Finding type (Candidate Record + DA brief + receipt)

```
flow-based | config | dep-CVE | secret | novel-pattern
```

Routes the candidate to its burden-of-proof bucket in phase-4 and
selects which rows of the hallucination-pattern challenge menu apply.
Novel-pattern findings are explicitly exempt from the taxonomy.

## 5. Suspicion level (Phase 2 dataflow)

```
HIGH | MEDIUM | LOW
```

Per-flow rating that phase-3 uses to decide which flows must produce at
least one candidate. Phase-3 raises ≥1 candidate from every HIGH or
MEDIUM flow.

## 6. DA `challenge_type` (typed envelope, Phase 4)

```
reachability | sanitization | upstream-validation | scope-exclusion
| dependency-loaded | waf-gateway | hallucination-pattern
| burden-of-proof | other
```

Tags the round's challenge so downstream tools can index debates by
challenge class. `other` is a deliberate catch-all for novel
challenges; reaching for `other` more than once or twice in a single
debate is a smell — prefer one of the named types.

## 7. Primary `response_type` (typed envelope, Phase 4)

```
rebuttal | concession | hold-position | reframe
```

- `rebuttal` — Primary disagrees and continues debate.
- `concession` — Primary agrees with DA, finding moves toward
  DISMISSED.
- `hold-position` — Primary stands without new evidence (used when
  invoking recall protection against an evidence-free DA challenge).
- `reframe` — Primary withdraws original `finding_type` claim and
  re-anchors as a different type. Allowed once per debate; second
  reframe counts as staleness.

## 8. DA `recommend_disposition` (typed envelope, Phase 4)

```
continue-debate | concede-to-primary | escalate-needs-review | dismiss
```

The DA's recommendation for what happens next. The orchestrator
synthesises this with Primary's `current_disposition` to determine the
actual round outcome.

## 9. Primary `current_disposition` (typed envelope, Phase 4)

```
continue-debate | concede | hold-final
```

- `continue-debate` — proceed to the next round.
- `concede` — Primary concedes; debate ends with DISMISSED.
- `hold-final` — Primary's final position; debate ends with CONFIRMED
  or NEEDS-REVIEW per the staleness rule's tier table.

## 10. Debate `termination_reason` (receipt, Phase 7)

```
natural | staleness | round-cap | early-concession
```

- `natural` — Primary held final after round N < MAX_DEBATE_ROUNDS with
  the DA accepting or remaining unable to advance.
- `staleness` — two consecutive rounds on the same side without a new
  evidence axis; per-tier disposition applied.
- `round-cap` — debate reached `MAX_DEBATE_ROUNDS` without natural
  termination.
- `early-concession` — debate ended at round 1 or 2 (either Primary
  conceded or DA conceded immediately).

## 11. Callgraph per-language outcome (Phase 1)

```
BUILT | EMPTY | UNSUPPORTED | FAILED | NOT-INSTALLED
```

- `BUILT` — graph produced with at least one definition or call.
- `EMPTY` — graph built but contained no captures (likely no source
  files of that language).
- `UNSUPPORTED` — no grammar installed for this language.
- `FAILED` — tree-sitter or the build script errored.
- `NOT-INSTALLED` — tree-sitter CLI not on the analysis host.

Phase-2 and phase-4 fall back to LLM-only reasoning for any outcome
other than `BUILT`. None of the non-`BUILT` outcomes gates a finding
(Non-Regression Principle).

## 12. Multitenant-scope flag (Phase 0 run log, receipt)

```
YES | NO
```

`YES` conservatively enables multitenant CVSS calibration when tenant, account, workspace, organization, or customer boundaries may exist. `NO` requires repository evidence that no such isolation boundary exists. Relationship-aware BOLA analysis always runs and organization-context-provider use is independent of this flag.

## 13. ControlGapStatus (Phase-3 candidate record `control_gap.status`)

```
CONTROL-USED | CONTROL-MISSING | NO-CONTROL-EXISTS
```

- `CONTROL-USED` — configured provider evidence shows a control on this exact path; remediate its misuse.
- `CONTROL-MISSING` — an explicit provider `coverage_probe` matches this path but the control fingerprint is absent. Use only checked-in provider guidance.
- `NO-CONTROL-EXISTS` — neither condition is proven; write a codebase-local fix without provider claims.

`expected_control` and `applicable_std` are nullable only for `NO-CONTROL-EXISTS`. Never call undeclared MCP tools or invent provider APIs. Hyphens are canonical.

## 14. PoC verdict (Phase 6 reviewer envelope + manifest)

```
VALID | NEEDS-REVISION | INVALID
```

- `VALID` — the PoC script faithfully exercises the finding's attack
  path: endpoint, method, field names, and payload match the code
  evidence; the safety header is present; placeholders are used for
  every host and credential; a developer running it (with placeholders
  substituted) would observe the vulnerability.
- `NEEDS-REVISION` — the script is plausible but has a correctable
  defect (wrong endpoint/method, mismatched field name, missing auth
  setup step, missing safety header, hardcoded hostname). Triggers
  exactly one regeneration round in phase-6.
- `INVALID` — the script does not exercise the claimed vulnerability
  or is structurally unrunnable (wrong vulnerability class, targets
  code not in the excerpt, or the approach cannot demonstrate the
  claim). Kept on disk for reference but flagged in the manifest.

Hyphen is canonical in `NEEDS-REVISION` (not `NEEDS_REVISION` or
`NEEDS REVISION`).

Gate validation: `complete-phase.sh phase-6` requires every
`pocs[].verdict` in `poc-manifest-<DATE>.json` to be in this set.

## 15. PoC `script_type` (Phase 6 manifest)

```
curl-shell | python | browser-recipe | source-check | other
```

**Every PoC is runnable** — there are no inert data-file types. Maps to
file extension and execution model:

- `curl-shell` (`.sh`) — one or more `curl` invocations. Covers HTTP
  findings (SQLi, IDOR, SSRF, auth bypass, BOLA), GraphQL/single
  requests, and XXE/XML/JSON-body payloads embedded in the `curl` data
  argument.
- `python` (`.py`) — `requests`-based; multi-step/session flows or
  richer response assertions.
- `browser-recipe` (`.browser.json`) — a declarative recipe driven by
  the chrome-devtools MCP. Used **only** when a script cannot confirm
  the finding (e.g. XSS that executes solely in a rendered DOM, or a
  logged-in UI check). Escalation step 2.
- `source-check` (`.sh`) — verifies a source-resident condition against
  the cloned repo: dep-CVE version comparison, IaC misconfig inspection,
  or **leaked-secret presence** via `grep` (presence, not
  exploitability). Re-clones the repo via `ensure_source` when absent.
- `other` — runnable catch-all for novel formats; using it more than
  once per repo is a smell — prefer one of the named types.

Phase-6 selects per finding based on `finding_type`, the attack shape,
and the script-first-then-browser escalation order; phase-7 and phase-8
render the type label alongside the script link.

Related per-PoC field `confirm_path` (`script | browser | unconfirmed`)
records how the finding was confirmed and feeds the report's
browser-driven / unconfirmed lists.

Model fields in run state capture initial provenance only:

### Initial SessionStart provenance

```json
"model": "<initial advisory Cursor model slug>",
"model_policy": "user-selected",
"model_provenance": "available",
"model_history": [
  {
    "model": "<initial advisory Cursor model slug>",
    "session_id": "<session id>",
    "source": "SessionStart",
    "selected_at": "<ISO 8601>"
  }
]
```

If the optional SessionStart pin is absent or unusable, a newly initialized
run records `"model": "unknown"`, `"model_provenance": "unavailable"`, and
an empty `model_history`. Provenance availability never gates a run. Preserved
legacy state may omit these fields or retain obsolete model-control fields.

NightFalcon never selects or switches models. Omit the `model` argument and
every reasoning override so Cursor uses the model currently selected by the
user. A user may change that selection at any time. Already-running agents
keep their launch model; later turns and new agents use the new selection.
Never reject, retry, or reroute work because the observed model differs.

The SessionStart advisory marker is `NIGHTFALCON_MODEL_SELECTED`. Passive
selection history lives outside the workspace under
`$PLUGIN_DATA/model-selections/`. Existing legacy model-control fields are
ignored and left untouched.

For Phase-6 `pocs[].generated_by`, record the actual advisory Cursor model
reported to that worker, or `unknown` if unavailable. Never pass model
metadata to a blind DA or PoC reviewer.

## 16. PoC skip reason (Phase 6 manifest `skipped[].reason`)

```
not-eligible-tier | not-eligible-disposition | generation-refused
| generation-failed | no-candidates
```

Every finding in the candidates file appears in exactly one of
`pocs[]` or `skipped[]` in `poc-manifest-<DATE>.json`. `skipped[]`
records *why* a finding has no executable PoC, so phase-7/8 can
surface the reason instead of leaving the section blank.

- `not-eligible-tier` — the finding is P3 or P4. PoC generation is
  scoped to P0/P1/P2.
- `not-eligible-disposition` — the finding's disposition is
  `DISMISSED` or `NEEDS-REVIEW`. PoC generation requires
  `CONFIRMED` or `CONFIRMED-MODIFIED`.
- `generation-refused` — phase-6 attempted but the model declined to
  produce an executable script for this attack class. The
  orchestrator may retry the refused `F-NNN` set once in a fresh spawn that
  omits model and reasoning overrides. The retry uses the user's selection at
  launch and never compares its identity with attempt one. The `note`
  accumulates every attempt's verbatim refusal text
  (`"… Also refused on retry: <text>."`).
- `generation-failed` — write/tool error during generation. The skip
  `note` carries the error message.
- `no-candidates` — repo had `NO_CANDIDATES`; manifest-level only
  (no per-finding entries).

Hyphens are canonical. Gate validation: `complete-phase.sh phase-6`
requires every `skipped[].reason` to be in this set.

**PoC tier cutoff (never changes with this enum):** PoCs are generated
for `P0`, `P1`, and `P2` findings only. `P3` (Low) and `P4`
(Informational) never receive a PoC — they land in `skipped[]` with
`not-eligible-tier`. Every finding at every tier still appears in the
report; only the executable PoC is tier-gated.

## 17. Exposure (Phase 1 → onward)

```
EXTERNAL | INTERNAL | INTERNAL-RESTRICTED
```

Classifies the network reachability of the surface a finding lives on.
Assigned in phase-1 (per entry point), carried on every phase-2 flow,
copied onto the phase-3 candidate record, and rendered as a per-finding
badge in the phase-8 HTML report.

- `EXTERNAL` — reachable from the public internet or by authenticated
  external users (public REST/GraphQL/gRPC endpoints, external-facing
  SDKs, external file upload/download, externally-exposed WebSockets).
- `INTERNAL` — reachable only from inside the trust boundary:
  service-to-service / internal APIs, admin tooling with an internal
  interface, cron/batch workers with an internal trigger, internal-only
  microservices. Not reachable from the public internet.
- `INTERNAL-RESTRICTED` — reachable only when network-adjacent: over a
  VPN or on the internal network, and typically not from an arbitrary
  internal service either. Matches the D1 Attack-Vector score-2 row in
  `phases/phase-3.md` ("network-adjacent access (internal network /
  VPN)").

Hyphen is canonical in `INTERNAL-RESTRICTED` (not `INTERNAL RESTRICTED`
or `INTERNAL_RESTRICTED`).

**Not gate-validated.** Like most enums in this registry, Exposure is
enforced by prompt discipline + per-phase self-checks, not by a
`complete-phase.sh` regex. Phase-1/2/3 write it; phase-7 copies it into
`findings-<DATE>.json`; phase-8's builder renders it. A missing or
non-canonical value degrades the report badge but does not BLOCK a phase
(Non-Regression Principle — a classification gap never drops a finding).

## 18. CVSS v4.0 Base (Phase 3 → onward)

The canonical severity score. Full policy in `references/cvss-policy.md`.
Two fields travel with every candidate and finding:

- `cvss_score` — number `0.0`–`10.0`, one decimal. Drives the P-tier
  (§1) mechanically (P0 9.0–10.0, P1 7.0–8.9, P2 4.0–6.9, P3 0.1–3.9,
  P4 0.0).
- `cvss_vector` — a CVSS v4.0 Base vector string, must start with
  `CVSS:4.0/` and set every Base metric (AV, AC, AT, PR, UI, VC, VI, VA,
  SC, SI, SA). Example:
  `CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N`

The report shows the score, the vector, and a
`https://www.first.org/cvss/calculator/4.0#<vector>` link. `cvss_score`
and `cvss_vector` must be mutually consistent, and the tier must match the
score's band. Base-only: Threat / Environmental / Supplemental metrics are
left unspecified.

## 19. CWE id (optional secondary classification)

```
CWE-<digits>   (e.g. CWE-89, CWE-639, CWE-798)   |   null
```

Optional per-finding tag recorded when a finding maps cleanly to one CWE.
CVSS is the priority score; CWE is a secondary label rendered alongside it.
`null`/absent is valid and never affects the tier. Never force a CWE that
does not fit.

---

## How gate scripts use this file

`complete-phase.sh` validates a subset of enums where a violation
would corrupt downstream analytics:

| Phase | Field validated | Enum |
|---|---|---|
| phase-4 | `Final Disposition:` line in `debate-<DATE>.md` | §2 Disposition |
| phase-5 | `Validation:` line in `candidates-<DATE>.md` | §3 Validation status |
| phase-6 | `pocs[].verdict` in `poc-manifest-<DATE>.json` | §14 PoC verdict |
| phase-6 | `pocs[].script_type` in `poc-manifest-<DATE>.json` | §15 PoC script_type |
| phase-6 | `pocs[].confirm_path` in `poc-manifest-<DATE>.json` | §15 (`script\|browser\|unconfirmed`) |
| phase-6 | `skipped[].reason` in `poc-manifest-<DATE>.json` | §16 PoC skip reason |

Gate violations exit `2` with a message naming the violating value and
linking to this file. Other enums are enforced by prompt discipline +
post-write self-checks within each phase; the gate does not yet
validate them.
