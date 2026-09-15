# NightFalcon deep-analysis contracts

These contracts are mandatory discovery inputs. Unknown or absent fields increase uncertainty; they never dismiss evidence.

## `relationship_context` for BOLA and authorization

Every authorization candidate records:

- `principal`: identity, role, credential type, authentication source.
- `action`: operation and privilege level.
- `object`: type, identifier, and selector provenance (path/query/body/header/token/derived).
- `ownership_binding`: owner field, tenant field, delegation relation, and lookup path.
- `relationship`: `owner | same-tenant-nonowner | cross-tenant | delegated | admin | unknown`.
- `observed_decision` and `expected_decision`.
- `enforcement_points`, evidence, and unanswered questions.

Test at least owner, same-tenant non-owner, cross-tenant, delegated, admin, anonymous, and selector-substitution paths. BOLA is not limited to missing checks; include check/use disagreement, alternate selectors, bulk endpoints, nested objects, cache keys, indirect references, and cross-repository policy drift.

## `business_logic_invariants`

For each sensitive workflow record:

- invariant key and scope (principal, tenant, account, object, global);
- cardinality, quantity, value, time, ordering, and state-transition bounds;
- read/check/write sequence and transaction boundary;
- atomicity, isolation, lock/compare-and-swap behavior;
- idempotency key scope, binding, persistence, and replay lifetime;
- actors, concurrent interleavings, rollback/compensation, retries, and duplicate delivery.

Probe BLA1 action-limit overrun, BLA2 concurrent workflow order bypass, BLA3 object-state manipulation, BLA5 artifact lifetime, BLA6 missing transition validation, BLA7 quota violation, BLA9 broken access control, and shadow/legacy function paths. Canonical concurrency example: two actors redeem one coupon after both read `unused`; both succeed unless check-and-write is atomic.

## `cross_repository_topology`

Phase 1 emits provisional contracts only. After all Phase 2 outputs exist, run `scripts/reconcile-topology.py` before Phase 3. Every outbound edge becomes `matched`, `external`, or `unresolved`. Review auth propagation, tenant binding, object identifiers, schema validation, trust assumptions, error semantics, retries, idempotency, shared credentials, version drift, and dependency conflicts across both ends.

## `dependency_inventory`

Run `scripts/dependency-inventory.py` without package-manager execution or network access. Retain dependency group and selectors: optional, feature, target, platform, profile, workspace, development, peer, source, and version. `resolution_status` and `reachability` are separate. Unknown reachability cannot dismiss known-vulnerable, compromised, confused-name, mutable, unmaintained, outdated, untracked, license, maturity, or oversized-dependency risk.

