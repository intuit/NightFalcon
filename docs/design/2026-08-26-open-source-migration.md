# NightFalcon Open-Source Migration Design

## Objective

Publish one behaviorally equivalent NightFalcon harness for Claude, Codex, and Cursor from source commit `0c3183e02ec1f1500115233402cabdf7808ec4e6`, while excluding organization-private material and replacing organization-specific policy coupling with public, optional interfaces.

## Release boundary

- Include active harness code, prompts, gates, schemas, tests, report builders, and public security references.
- Exclude private knowledgebase content, internal control identifiers, internal product/service names, organization domains, private CI, caches, local plans, and source-repository generated evidence. Retain public verifier evidence from two final clean runs; release hashing excludes only those self-generated evidence files.
- Record complete public release hashes plus content-sensitive commitments for every excluded category without publishing excluded filenames.
- Preserve existing public v1 workspaces through a non-mutating read adapter. Never rewrite user input in place.

## Public architecture

Three ports share one security contract and platform-specific launch/enforcement adapters:

1. Phase 0 records repositories and immutable review metadata.
2. A private workspace journal records typed user intent, agent lifecycle,
   accepted phase facts, checkpoint references, and final handoff provenance.
   It is append-only, hash chained, epoch fenced, crash recoverable, and never
   passed into blind worker context.
3. Phase 1 builds per-repository scope, provisional contracts, dependency inventory, and attack surface.
4. Phase 2 emits dataflow, trust-boundary, authorization, and outbound-edge evidence.
5. A deterministic topology reconciler runs after every Phase 2 result and before Phase 3. Every edge is `matched`, `external`, or `unresolved`.
6. Phases 3-8 retain source behavior: scoring, blind debate, validation, PoC review, findings, and bound multi-format reporting.

Optional organization context uses a versioned provider rooted inside package or at `NIGHTFALCON_CONTEXT_ROOT`. Provider discovery is independent from `multitenant_scope`. Tenancy doctrine always remains available because BOLA and isolation analysis are universal.

## Expanded security analysis

### Authorization and BOLA

Authorization evidence records principal, role/credential, action, object selector provenance, ownership/tenant binding, relationship (`owner`, `same-tenant-nonowner`, `cross-tenant`, `delegated`, `admin`, `unknown`), observed decision, expected decision, enforcement point, and evidence. Missing relationship data is unknown, never a dismissal.

### Business logic

Business invariants record key scope, cardinality/value bounds, read/check/write sequence, transaction/lock behavior, idempotency and replay lifetime, actors, and interleavings. Canonical fixture covers dual coupon redemption and maps to OWASP BLA1/BLA2.

### Cross-repository analysis

Authoritative topology consumes all Phase 2 outputs. It detects trust mismatches, authentication/authorization drift, unresolved internal-looking endpoints, shared-secret blast radius, schema/validation divergence, and dependency/version conflicts. Phase 1 contracts remain provisional only.

### Dependencies and libraries

Static inventory parses supported manifests and locks without package-manager execution or network access. It retains conditional, optional, target, profile, feature, workspace, and source selectors. Any detected manifest that cannot be parsed or is rejected blocks Phase 1 instead of silently reducing coverage. Resolution and reachability are separate receipts. Unknown reachability cannot dismiss a dependency risk.

## OWASP corpus

Pin official current editions as of 2026-08-26: Web 2025, API 2023, Mobile 2024, LLM 2026, Agentic Applications 2026, Kubernetes 2025, MCP 2025 beta, NHI 2025, Smart Contract 2026, Business Logic Abuse 2025, OSS Top 10, ASVS 5.0.0, plus latest stable CI/CD, Desktop, and Serverless editions. Agentic Skills is labeled public-review draft and excluded from stable-framework counts.

Use concise original adaptations, not copied project bodies. `NOTICE` and a machine-readable provenance manifest carry source URL, edition, status, license, adaptation note, and retrieval date. CC BY-SA adaptations remain clearly bounded and share-alike. Kubernetes Top Ten and Smart Contract Top 10 use CC BY-NC-SA 4.0, so their document bodies are not bundled or adapted; only identifier/title reference metadata and official links remain.

## Compatibility and fail-closed rules

- v1 `org_scope` is normalized in memory into public v2 semantics; source bytes remain unchanged.
- Mixed or malformed schema fails closed.
- External context roots must resolve beneath their configured root; symlink escapes fail closed.
- Missing provider means public-default analysis, not tenancy disablement.
- Missing OWASP mapping, call graph, relationship, reachability, or cross-repo resolution never dismisses a finding.
- Platform-specific hooks must reject phase skip, premature stop, protected-state write, fabricated dismissal, and malformed provenance.
- Phase acceptance revalidates artifacts, binds immutable checkpoints in
  deterministic chunks, requires empty agent ledger, and reconciles torn
  transactions before any resumed worker launch.

## Verification

Deterministic tests prove shared contract parity, schemas, adapters, topology,
OWASP provenance, journal transaction recovery across every failpoint,
immutable artifact resolution, licensing, packaging, forbidden-term absence,
migration-manifest content hashes, and every stable Claude, Codex, and Cursor
public destination hash plus its source classification. Two self-generated
verifier evidence files are bound by retained run records instead of circular
destination hashes. Current live-client canaries
prove package validation and discovery. Deterministic fail-closed canaries prove
runtime enforcement contracts. Target-dependent external-repository execution
remains operational validation and is not inferred from package evidence.
