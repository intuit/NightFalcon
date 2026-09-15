# Multi-Tenant Severity Doctrine

This doctrine is vendor-neutral. It applies only when repository evidence shows
separate tenant, account, workspace, organization, or customer security
boundaries. Relationship-aware BOLA and object-authorization analysis still run
for every repository; this flag changes severity calibration, not discovery.

## Cross-tenant impact

A signed-in principal reading or mutating another tenant's protected data is a
high-impact authorization failure. Authentication does not authorize an object,
and a paid or self-service account is not a trusted boundary.

Examples include:

- BOLA/IDOR returning another tenant's financial records, personal data,
  documents, transactions, or credentials;
- a mutation whose caller-supplied tenant or object identifier is not bound to
  the authenticated principal;
- shared cache keys, missing tenant filters, delegated-access drift, bulk or
  nested-object selectors, and alternate identifiers that bypass ownership
  checks;
- cross-repository calls that exchange a user identity for a service identity
  without preserving principal-action-object authorization.

## Signup-equivalence test

Ask: *Can any person obtain the required account through a public or
self-service flow, then use it to access data owned by a different tenant?* If
yes, the required login remains network-reachable and normally maps to
`AV:N/PR:L`, not an internal or privileged-only boundary. Invitation-only,
sales-gated, network-restricted, local, or physical preconditions must be
supported by code or deployment evidence before lowering reachability.

## CVSS application

- Use `AV:N` for a self-service authenticated endpoint reachable over a network.
- Use `PR:L` for an ordinary authenticated account and `PR:N` only when no
  privileges are required.
- Cross-tenant disclosure of sensitive records normally supports `VC:H`;
  cross-tenant mutation normally supports `VI:H`.
- Set subsequent-system metrics only when impact crosses into a genuinely
  different system. A tenant boundary inside one vulnerable system is not, by
  itself, subsequent-system impact.
- Compute CVSS-B from demonstrated metrics and derive P0-P4 mechanically. This
  doctrine creates no severity floor and never overrides the calculated vector.

Phase 4 must challenge tenant-boundary evidence, signup equivalence, object
ownership, delegated relationships, selector variants, and claimed impact. A
finding may be dismissed only through normal evidence-backed debate.
