# Pattern Tags Allowlist

Closed allowlist of pattern tags that phase-7 emits per-repo and
phase-8 clusters across repos.

**Why this exists.** Cross-repo "Recurring patterns" synthesis is the
single highest-value output of the executive summary. Doing it from
scratch requires phase-8 to read every per-repo findings file — at
~5-15 KB each, across 20+ repos, that's 800K+ tokens, well past
phase-8's context limit. The user previously wrote this section by
hand.

**The fix.** Phase-7 emits a tiny `pattern-tags-<DATE>.json` per repo
(~200 bytes) tagging each repo's findings with a small array of
predefined pattern tags drawn from this allowlist. Phase-8 reads only
the tag JSONs (~4 KB total across 20 repos) and clusters them
programmatically — repos with the same tag form a recurring-pattern
cluster, which drives the "Recurring patterns" headings in the
executive summary. The detailed prose per cluster is written from the
top-N repos in each cluster on demand, not from all repos at once.

## Tag vocabulary

Tags use **kebab-case** and group by **root cause class**, not by tier
or finding-type. They are the smallest unit of cross-repo pattern
signal.

### Authorization / access-control patterns

- `bola-on-read` — IDOR / BOLA on a read endpoint returning another
  tenant's data (PII, financial, payee).
- `bola-on-write` — Missing per-object authorization on a mutation
  endpoint (creates / modifies / deletes another tenant's record).
- `last-mile-auth-bypass` — Endpoint on a `security.ignored` /
  `permitAll` / Spring Security bypass list in a production-tier profile.
- `path-traversal-authz` — Substring-match auth filters (CWE-1289)
  allowing path manipulation to reach another tenant's resources.
- `feature-flag-gated-authz` — `if (flag) authorize else continue`
  pattern — authorization gated by a runtime feature flag.
- `caller-supplied-tenant-id` — Body / query / header `tenantId` (or
  equivalent) trusted without binding to the authenticated caller.

### Authentication patterns

- `home-rolled-cookie` — Custom HMAC / signing scheme on an auth
  cookie or token; weak key derivation or no key rotation.
- `unsigned-saml` — SAML response accepted without signature /
  issuer / audience / timing validation.
- `jwt-skip-verify` — `JWT::decode(..., null, false)` / equivalent
  signature-verification disabled.
- `oauth-redirect-uri-substring` — `redirect_uri` validated by
  substring or `startsWith` instead of exact match.

### Injection patterns

- `sql-injection` — Caller-supplied data flowing into a SQL query
  without parameterization.
- `nosql-injection` — Caller-supplied data flowing into a NoSQL
  query (Mongo, Cassandra, etc.) without parameterization.
- `command-injection` — Caller-supplied data flowing into an OS
  command exec.
- `template-injection` — SSTI in Jinja, Thymeleaf, Freemarker, etc.
- `log-injection` — Unsanitized caller-supplied data written to an
  audit log (CRLF newline / forgery).
- `prompt-injection-direct` — User-supplied content concatenated
  into an LLM prompt without role separation or content fencing.
- `prompt-injection-indirect` — External data (web pages, docs,
  emails) flowing into LLM prompts without trust labelling.

### XSS / output-encoding patterns

- `xss-unsanitized-html-render` — `dangerouslySetInnerHTML` /
  `ReactHtmlParser` / `v-html` / `innerHTML` rendering a non-constant
  value without a sanitizer.
- `xss-reflected-query-param` — Server reflects a query / form
  parameter into HTML without encoding.
- `xss-mutation-via-dompurify-drift` — DOMPurify version drift
  vulnerable to one of the 2024-2026 mutation-XSS CVEs.
- `llm-output-rendered-as-code` — LLM output rendered into HTML /
  executed as code / used in SQL or shell commands without validation.

### Secret-exposure patterns

- `hardcoded-prod-api-key` — Production API key / signing key /
  OAuth secret committed to source-controlled artifacts (JS bundles,
  JARs, config files).
- `hardcoded-tls-private-key` — TLS private key bundled into the
  production artifact.
- `secret-in-error-message` — Stack trace / error response leaks
  internal secrets, keys, or paths.

### Crypto patterns

- `weak-crypto-algo` — MD5, SHA-1, DES, RC4 used for security-relevant
  hashing or encryption.
- `iv-reuse` — Fixed or reused IV in symmetric encryption.
- `predictable-token` — Tokens / nonces generated from a predictable
  source (timestamp, sequential counter, weak PRNG).

### Configuration / IaC patterns

- `public-cloud-bucket` — Public S3 / GCS / Blob bucket provisioned
  by IaC.
- `security-group-0000` — Security group with `0.0.0.0/0` ingress to
  a sensitive port.
- `k8s-privileged-pod` — Pod with `hostPID` / `hostNetwork` / `hostIPC`
  / `privileged: true` or `hostPath` mount.
- `container-root` — Final image stage runs as root user.
- `permissive-cors` — `Access-Control-Allow-Origin: *` combined with
  `Access-Control-Allow-Credentials: true`.
- `debug-flag-in-prod` — Debug / dev endpoint reachable in production.

### IAM / privilege-escalation patterns

- `aws-iam-passrole-escalation` — `iam:PassRole` to a high-privilege
  role combined with `lambda:CreateFunction` / `ec2:RunInstances`.
- `aws-iam-attach-policy-broad` — `iam:AttachUserPolicy` /
  `AttachRolePolicy` granted without resource scoping.
- `gcp-service-account-token-creator` — `iam.serviceAccountTokenCreator`
  on a high-privilege SA.
- `k8s-rbac-escalate-verb` — ClusterRole / Role with `bind`,
  `escalate`, or `impersonate` verbs.

### Open redirect / SSRF patterns

- `open-redirect-query-param` — Caller-controlled `redirectUrl` /
  `returnTo` / `referrer` flowing into `window.location.href` or HTTP
  redirect response.
- `ssrf-outbound-fetch` — Caller-controlled URL flowing into an
  outbound HTTP fetch without allowlist.

### Supply-chain patterns

- `dep-cve-eol-version` — Repo pinned to an EOL major / minor version
  of a dependency with known CVEs.
- `typosquatting-risk` — Package name with subtle typo against a
  well-known library.
- `dependency-confusion-risk` — Private package name that resolves
  to a public registry.
- `post-install-script-untrusted` — `prepare` / `postinstall` script
  in `package.json` for a dependency not on a vetted allowlist.

### Workflow / CI patterns

- `pwn-request-workflow` — `pull_request_target` + PR-controlled
  checkout (the "pwn request" pattern); any forked PR can exfiltrate
  secrets.
- `unpinned-base-image` — Docker `FROM image:latest` or unverified
  digest.

### Process / hygiene patterns

- `excluded-from-platform-security` — `pom.xml` / `package.json`
  excludes that remove `spring-boot-starter-security`,
  `platform-security`, or equivalent platform security dep.
- `webhook-no-hmac-verification` — Webhook endpoint parses and acts
  on JSON body without signature check.
- `graphql-no-depth-limit` — GraphQL schema with no depth or
  complexity limits allowing client-controlled fan-out.

---

## Tag emission rules (phase-7)

Phase-7 emits `findings/<slug>/pattern-tags-<DATE>.json` containing
ONLY tags from this allowlist. Format:

```json
{
  "schema_version": "1",
  "repo_slug": "<slug>",
  "date": "<DATE>",
  "tags": [
    {
      "tag": "<one of the kebab-case strings above>",
      "finding_ids": ["F-001", "F-014"]
    }
  ]
}
```

Rules:

1. **One tag per root cause class.** If a repo has 5 BOLA-on-read
   findings, emit `bola-on-read` once with all 5 finding IDs — not 5
   times.
2. **Closed allowlist.** Tags must match the entries above
   case-exactly. Unknown tags are rejected by the gate and become a
   `pattern-tags-allowlist miss` entry in the run log.
3. **Best-effort.** A finding may not match any tag; that's fine. The
   tag set is for the highest-leverage cross-repo signals.
4. **Stable across runs.** Once a finding is tagged, the tag persists
   across re-runs of the same repo (idempotent given same input).

## Tag consumption rules (phase-8)

Phase-8 reads every `findings/<slug>/pattern-tags-<DATE>.json` in the
workspace, builds a `tag → [(slug, finding_ids)]` reverse map, and
filters to tags with `repo_count >= 2` (clusters of one repo are not
recurring patterns). The resulting clusters drive the "Recurring
patterns" section of the executive summary. Cluster size determines
heading order (most repos first).

For each cluster, phase-8 emits one paragraph naming the pattern, the
affected repos, and the proposed cross-cutting remediation. Drawing
the prose requires reading at most 2-3 representative findings per
cluster — never all findings across all repos.

---

## Refresh cadence

This allowlist evolves as new pattern classes emerge. Cadence:
quarterly review + add-as-needed during reviews. Adding a tag is a
one-line addition under the appropriate section. Removing a tag is
rare — prefer marking it deprecated with a note rather than removing,
so historical run artifacts remain interpretable.

**Last refreshed:** 2026-05-25
