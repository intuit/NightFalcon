# Pattern Validation Table

Static CWE → OWASP category → canonical citation URL mapping for
phase-5 validation of pattern-class findings.

**Why this exists.** Dependency-CVE findings benefit from current advisory lookup, while stable pattern classes benefit from deterministic canonical references. Repeated web searches for stable CWE/OWASP definitions add cost without improving repository-specific evidence.

**The fix.** Pattern-class validation is a *lookup-table* problem, not a
*search* problem. The CWE catalog is stable on multi-year timescales —
SQLi is CWE-89 regardless of year — so a static table with quarterly
refresh provides the same evidence as the web search without the cost.

**Refresh cadence.** Quarterly. The maintainer updates this file by:

1. Re-fetching the latest OWASP Top 10 listing from `owasp.org/Top10/`
   and CWE Top 25 from `cwe.mitre.org/top25/`.
2. Updating the `Last refreshed` line below.
3. Committing with `[security-scanner] refresh pattern-validation-table
   YYYY-MM-DD`.
4. The refresh is a non-blocking maintenance task — if the table is
   more than 6 months stale, phase-5 still uses it (drift on these
   categories is slow), but a stale-warning is recorded in the run log.

**Last refreshed:** 2026-05-25

---

## How phase-5 uses this

When a finding's `finding_type` is one of `flow-based`, `config`,
`secret`, or `novel-pattern` AND no specific dependency version is
named in the candidate record, look up the finding's category here.
Emit `Validation: CURRENT` with the matching citation URL. **Do not
run a web search.**

When a finding's `finding_type` is `dep-CVE` (or any other type with a
specific `X@Y.Z` version in the candidate record), do the full
phase-5 dep-CVE path: NVD / GHSA / CISA KEV / vendor advisory fetch.
The static table does not cover dep-CVE findings.

The Non-Regression Principle still applies: a category not in this
table does not fail the finding. Use the closest match plus the
generic OWASP Top 10 URL (`https://owasp.org/Top10/`) and note the
table-miss in the run log so the next refresh can add it.

---

## Table

The `cwe_id` is the canonical CWE identifier from cwe.mitre.org. `OWASP
current` records the closest category from latest stable edition for that
framework: Web 2025 or LLM Applications 2026. The
`citation_url` is the canonical reference phase-5 emits in the
`Validation source` field.

| Category | CWE ID | OWASP current | Citation URL |
|---|---|---|---|
| **Broken access control / IDOR / BOLA** | CWE-639, CWE-862, CWE-863 | A01:2025 | https://owasp.org/Top10/2025/ |
| **Cryptographic failure (weak algo, hardcoded key, IV reuse)** | CWE-327, CWE-326, CWE-329 | A04:2025 | https://owasp.org/Top10/2025/ |
| **SQL injection** | CWE-89 | A05:2025 | https://owasp.org/www-community/attacks/SQL_Injection |
| **NoSQL injection** | CWE-943 | A05:2025 | https://owasp.org/www-community/Injection_Theory |
| **OS command injection** | CWE-78 | A05:2025 | https://owasp.org/www-community/attacks/Command_Injection |
| **LDAP injection** | CWE-90 | A05:2025 | https://owasp.org/www-community/attacks/LDAP_Injection |
| **XPath injection** | CWE-643 | A05:2025 | https://owasp.org/www-community/attacks/XPATH_Injection |
| **Template injection (SSTI)** | CWE-1336 | A05:2025 | https://owasp.org/www-project-web-security-testing-guide/v42/4-Web_Application_Security_Testing/07-Input_Validation_Testing/18-Testing_for_Server-side_Template_Injection |
| **Cross-site scripting — reflected, stored, DOM-based** | CWE-79 | A05:2025 | https://owasp.org/www-community/attacks/xss/ |
| **Insecure design / business logic flaw** | CWE-840 | A06:2025 | https://owasp.org/Top10/2025/ |
| **Security misconfiguration (debug flag, default creds, permissive CORS)** | CWE-16, CWE-942 | A02:2025 | https://owasp.org/Top10/2025/ |
| **Vulnerable / outdated component (generic, no specific CVE)** | CWE-1104 | A03:2025 | https://owasp.org/Top10/2025/ |
| **Identification / authentication failure (weak token, session fixation)** | CWE-287, CWE-384 | A07:2025 | https://owasp.org/Top10/2025/ |
| **JWT signature not verified** | CWE-347 | A07:2025 | https://owasp.org/www-project-web-security-testing-guide/latest/4-Web_Application_Security_Testing/06-Session_Management_Testing/10-Testing_JSON_Web_Tokens |
| **SAML signature not validated** | CWE-347 | A07:2025 | https://www.owasp.org/index.php/SAML_Security_Cheat_Sheet |
| **Insecure deserialization** | CWE-502 | A08:2025 | https://owasp.org/www-community/vulnerabilities/Deserialization_of_untrusted_data |
| **Security logging / monitoring failure** | CWE-778, CWE-117 | A09:2025 | https://owasp.org/Top10/2025/ |
| **Log injection (newline / forgery into audit log)** | CWE-117 | A09:2025 | https://owasp.org/www-community/attacks/Log_Injection |
| **Server-side request forgery (SSRF)** | CWE-918 | A01:2025 | https://owasp.org/Top10/2025/A01_2025-Broken_Access_Control/ |
| **XML external entities (XXE)** | CWE-611 | A02:2025 | https://owasp.org/www-community/vulnerabilities/XML_External_Entity_(XXE)_Processing |
| **Path traversal** | CWE-22 | A01:2025 | https://owasp.org/www-community/attacks/Path_Traversal |
| **Open redirect** | CWE-601 | A01:2025 | https://owasp.org/www-community/attacks/Unvalidated_Redirects_and_Forwards_Cheat_Sheet |
| **CSRF** | CWE-352 | A01:2025 | https://owasp.org/www-community/attacks/csrf |
| **Race condition / TOCTOU** | CWE-362, CWE-367 | A06:2025 | https://owasp.org/www-community/vulnerabilities/Race_Conditions |
| **Hardcoded credentials** | CWE-798 | A07:2025 | https://owasp.org/www-community/vulnerabilities/Use_of_hard-coded_password |
| **Sensitive data in logs** | CWE-532 | A09:2025 | https://owasp.org/www-community/vulnerabilities/Insertion_of_Sensitive_Information_into_Log_File |
| **Mass assignment** | CWE-915 | A06:2025 | https://cheatsheetseries.owasp.org/cheatsheets/Mass_Assignment_Cheat_Sheet.html |
| **GraphQL introspection in production / batch DoS** | CWE-200, CWE-770 | A02:2025 | https://cheatsheetseries.owasp.org/cheatsheets/GraphQL_Cheat_Sheet.html |
| **OAuth / OIDC: redirect_uri substring match, missing PKCE, weak state** | CWE-1188, CWE-352 | A07:2025 | https://datatracker.ietf.org/doc/html/draft-ietf-oauth-security-topics |
| **Trust boundary violation (unauthenticated endpoint on internal allowlist)** | CWE-501 | A01:2025 | https://owasp.org/Top10/2025/ |
| **Prompt injection (direct or indirect)** | CWE-1039 | LLM01:2026 | https://genai.owasp.org/resource/owasp-genai-llm-top-10-2026/ |
| **Insecure LLM output handling (rendering as HTML / SQL / code)** | CWE-79, CWE-89 | LLM10:2026 | https://genai.owasp.org/resource/owasp-genai-llm-top-10-2026/ |
| **Excessive agency in LLM tool-use loops** | CWE-269 | LLM03:2026 | https://genai.owasp.org/resource/owasp-genai-llm-top-10-2026/ |
| **Hidden context exposure / training data leak** | CWE-200 | LLM08:2026 | https://genai.owasp.org/resource/owasp-genai-llm-top-10-2026/ |
| **IaC misconfiguration: public S3/GCS/Blob bucket, security group 0.0.0.0/0** | CWE-732 | A02:2025 | https://owasp.org/Top10/2025/ |
| **Kubernetes RBAC escalation (bind, escalate, impersonate verbs)** | CWE-269 | A02:2025 | https://kubernetes.io/docs/concepts/security/rbac-good-practices/ |
| **Container runs as root / privileged / hostPath** | CWE-250, CWE-732 | A02:2025 | https://kubernetes.io/docs/concepts/security/pod-security-standards/ |
| **AWS IAM: PassRole+CreateFunction, AttachUserPolicy, etc.** | CWE-269, CWE-732 | A01:2025 | https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies_examples.html |
| **Supply chain: typosquatting, dependency confusion, lockfile drift** | CWE-1357 | A03:2025 | https://owasp.org/www-project-software-component-verification-standard/ |

---

## Falling back when no row matches

If the finding's category does not match any row above, emit:

```
- Validation: CURRENT
- Validation source: https://owasp.org/Top10/
- Validation note: <plain English: the finding category did not match
  a row in references/pattern-validation-table.md. Recorded against
  the generic OWASP Top 10 reference. Maintainer should add a row for
  this category in the next quarterly refresh. The Non-Regression
  Principle applies — the finding is reported at full fidelity.>
```

Then write a single line to the run log: `pattern-validation-table
miss: <finding_type> / <category>`. The next quarterly refresh
addresses these.
