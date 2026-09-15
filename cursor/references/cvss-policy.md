# CVSS v4.0 Scoring Policy

CVSS v4.0 Base (`CVSS-B`) is the **canonical severity score** for every
NightFalcon finding. It replaces the legacy four-dimension (D1–D4) rubric.
Every candidate (phase-3) and every final finding (phase-7) publishes:

1. the numeric `CVSS-B` score (0.0–10.0, one decimal),
2. the full CVSS v4.0 vector string,
3. a concise per-metric rationale, and
4. a vector-anchored link to `https://www.first.org/cvss/calculator/4.0`.

Score only what the attack path demonstrates: attacker access, attack
requirements, privileges required, user interaction, and the impacts actually
shown. State material deployment assumptions explicitly. Do not invent
controls, mitigations, or downstream authority that the evidence does not show.

## The P-tier is derived mechanically from CVSS-B

The `P0..P4` tier is a pure function of the score — never assigned by feel:

| Tier | CVSS-B range |
|---|---|
| **P0** | 9.0 – 10.0 |
| **P1** | 7.0 – 8.9 |
| **P2** | 4.0 – 6.9 |
| **P3** | 0.1 – 3.9 |
| **P4** | 0.0 |

This mapping is authoritative. Phase-3 assigns the tier from the score,
phase-4 re-derives it if the debate changes any metric, and phase-7 renders
both the score and the tier. The tier enum values are unchanged
(`references/enums.md` §1); only their *derivation* is now CVSS-based.

## CVSS v4.0 Base metrics (what to set)

Set every Base metric; leave Threat / Environmental / Supplemental
unspecified (Base-only scoring, `CVSS-B`):

- **AV** Attack Vector: `N` network / `A` adjacent / `L` local / `P` physical
- **AC** Attack Complexity: `L` low / `H` high
- **AT** Attack Requirements: `N` none / `P` present
- **PR** Privileges Required: `N` none / `L` low / `H` high
- **UI** User Interaction: `N` none / `P` passive / `A` active
- **VC/VI/VA** Vulnerable-system Confidentiality / Integrity / Availability:
  `H` high / `L` low / `N` none
- **SC/SI/SA** Subsequent-system (downstream) C / I / A: `H` / `L` / `N`

A complete vector looks like:
`CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N`

Publish the vector with a `#`-anchored calculator link so a reviewer can
reproduce the score:
`https://www.first.org/cvss/calculator/4.0#CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N`

Calculate and verify Base scores with the bundled reference-compatible tool:

```bash
python3 scripts/cvss_v4.py '<full CVSS:4.0/... Base vector>'
```

Phase gates run the same algorithm and reject incomplete vectors, unknown or
duplicate metrics, non-Base metrics, score/vector mismatches, tier/score
mismatches, and calculator links that are not anchored to the exact vector.

## Multi-tenant / cross-tenant anchors (replaces the old P0 doctrine)

When `multitenant-scope=YES` (default; see phase-0) apply these metric anchors to
any cross-tenant finding. They make the CVSS score reflect blast radius
instead of the sophistication of the path:

- **AV:N** for any endpoint reachable by a self-provisioned self-service identity,
  even when it requires authentication. Signup-equivalence makes
  "authenticated" indistinct from "public" at the attack-vector metric.
- Set **PR:L** when exploitation requires any ordinary authenticated account;
  use **PR:N** only when no privileges are required before exploitation.
  Self-service signup informs business risk but does not change CVSS metric
  definitions.
- A **cross-tenant read** of another tenant's PII / financial data sets
  **VC:H** for the confidentiality impact on the vulnerable system. A
  **cross-tenant write / mutation** sets **VI:H**. Set **SC/SI/SA** only when
  exploitation causes impact in a genuinely different subsequent system;
  crossing a tenant boundary inside one vulnerable system does not by itself
  create subsequent-system impact.
- **No tier override or floor exists.** Compute the CVSS-B score from the
  evidence-backed metrics, then derive P0-P4 from the table above. If a
  cross-tenant finding computes below 9.0, it is not P0. Never change a tier
  without changing the vector and recalculating the score.

See `references/multitenant-p0-doctrine.md` for the underlying rationale; the
doctrine's *effect* is now expressed through these metric anchors.

## Secret calibration (do not over-score credential findings)

A credential-looking value in JavaScript, a browser bundle, a client
artifact, or a `source-check` grep hit is **not automatically P0** and does
not by itself prove account takeover or server-side authority. Before scoring
a secret finding at high `VC/VI` or SC/SI, establish that the secret is:

- **non-public** (not an intentionally publishable client id / publishable key),
- **active or likely active** (not rotated / revoked / obviously a test stub),
- **used as a security boundary** (authorizes a real operation), and
- tied to **specific authorized operations and data** you can name.

When those links are absent, **preserve the candidate** but mark the missing
evidence, score **only the demonstrated impact** (often `VC:L` presence /
information-exposure, not `VC:H` compromise), and **do not** assign P0 (whose
report display alias is Critical). The phase-4 Devil's Advocate challenges any secret finding
scored `VC:H`/`VI:H` without those four links established.

## Non-Regression Principle still applies

CVSS is a scoring method, not a candidate-discovery filter. Preserve plausible
candidates while evidence is gathered, but every candidate must have a valid,
best-supported Base vector before phase-3 completes. A finding is never dropped
for lacking a CWE or OWASP mapping. The only mechanism that removes a scored
candidate is a phase-4 debate concluding `DISMISSED`.

## CWE (secondary classification)

CVSS is the priority score. CWE is an optional secondary tag: when a finding
maps cleanly to a CWE (e.g. `CWE-89` SQL injection, `CWE-639`/`CWE-284`
IDOR/BOLA, `CWE-798` hard-coded credentials), record the `CWE-NNN` id so the
report can show it alongside the CVSS score. Omit it when no single CWE fits;
never force one. It never affects the tier.
