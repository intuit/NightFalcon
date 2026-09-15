# NightFalcon Open-Source Migration Plan

## Acceptance conditions

1. Claude, Codex, and Cursor packages preserve source behavior at `0c3183e02ec1f1500115233402cabdf7808ec4e6` except documented public adapters and expanded analysis.
2. Packaged output contains no private organization/product/domain/control identifiers.
3. Existing public v1 workspace fixtures load without mutation and emit canonical v2-equivalent output.
4. OWASP index, graph, prompts, and provenance use current official editions and correct stable/draft labels.
5. BOLA, business-logic concurrency, cross-repository topology, and conditional dependency fixtures are detected by all three ports.
6. Full deterministic suites pass twice from clean tree; current installed-client discovery and deterministic per-client negative canaries pass independently.
7. Typed journal engines, validators, completion gates, and recovery behavior
   stay byte-identical across all three clients, including high-repository
   artifact chunking and Phase-2 topology binding.

## TDD sequence

### Task 1: Freeze migration boundary and package policy

- Add source manifest generator and clean-room verifier tests.
- Assert exact source SHA, include hashes, exclusion counts/commitment, user OWASP semantic merge, and forbidden-term scan.
- Observe RED before migration tooling exists.

### Task 2: Migrate active three-port source

- Map `NightFalcon-Claude` to `claude`, Codex plugin to `codex`, and Cursor package to `cursor`.
- Exclude private knowledgebase, internal metadata, caches, plans, and source-repository generated evidence. Retain public two-run verifier evidence as release handoff proof.
- Preserve source executable bits and platform layout.
- Make migration manifest deterministic, content-bind every excluded category without publishing excluded paths, and hash complete public release scope except manifest self-reference and retained verifier evidence.
- Bind every Claude, Codex, and Cursor public destination to source SHA, classification, and content hash in one frozen three-port map.

### Task 3: Public context and v1 compatibility

- Add non-mutating v1 read adapter.
- Replace organization-specific scope fields with `multitenant_scope` and generic context provider.
- Decouple provider presence from tenancy doctrine.
- Fix external-root relative paths, containment, symlink rejection, and atomic graph output.
- Test configured/unconfigured provider crossed with tenancy true/false.

### Task 4: Current OWASP corpus

- Replace stale Web/API/LLM and incorrect index entries.
- Add Business Logic Abuse, OSS Top 10, and draft-labeled Agentic Skills.
- Add provenance manifest, notice boundary, graph nodes, fingerprints, and routing updates.
- Test exact category IDs/names, editions, URLs, status, license, and stable-count exclusion.

### Task 5: Deep logic and cross-repository analysis

- Add relationship-aware authorization/BOLA schema and fixtures.
- Add business invariant/interleaving schema and BLA1/BLA2 fixture.
- Build authoritative post-Phase-2 topology reconciler and unmatched-edge fixtures.
- Add static conditional dependency inventory with Cargo workspace coverage, fail-closed manifest coverage, and separate reachability receipt.
- Wire prompts, context scopes, gates, reports, JSON, HTML, and SARIF consistently.

### Task 6: Port parity and public documentation

- Mirror shared contract across Claude, Codex, Cursor while retaining platform-specific hooks.
- Add root README, installation/use docs, LICENSE, NOTICE, SECURITY, CONTRIBUTING, CODE_OF_CONDUCT, support policy, and CI.
- Document no-private-context boundary and optional generic context schema.
- Port append-only agent journal, epoch fencing, immutable checkpoint binding,
  crash reconciliation, final handoff projection, and blind-context exclusion.
- Add contract and every-failpoint recovery suites, including dependency
  inventory and cross-repository topology as authoritative bound artifacts.

### Task 7: Verification and live canaries

- Run syntax, unit, integration, graph reproducibility, packaging, forbidden-term, license, and parity suites.
- Run clean-room package verification twice.
- Pressure-test edited skill prompts with fresh agents per writing-skills guidance.
- Run installed Claude/Codex/Cursor package validation and discovery plus deterministic per-client negative canaries: phase skip, premature stop, forbidden write, fabricated dismissal, malformed provenance.
- Record package evidence separately from target-dependent external-repository execution; never infer live UI or model-transition behavior from fixtures.

### Task 8: Release handoff

- Confirm clean branch and summarize source SHA, migration manifest, exclusions, tests, canaries, licenses, and residual risks.
- Create a new root-history release branch from the verified tree so prior internal/private blobs are absent; preserve the development branch and local remote unchanged.
- Do not rewrite history, change remote, push, or publish without explicit user action.
