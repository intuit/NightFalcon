# High-Value Attack-Pattern Checklists

Loaded at Phase 3 entry, but only the sections matching stacks present
in the Phase 2 inventory. Each section is a candidate trigger menu:
if a pattern below is present in in-scope code, raise a candidate,
score it, and debate it.

These encode attack knowledge that does not always emerge from generic
flow tracing. They are **additive aids**, not gating filters — the
Non-Regression Principle applies, and findings outside these checklists
are still reported at full fidelity.

Every candidate raised under one of these checklists must record the
matched stack and pattern in its Candidate Record under the
`Attack-Pattern Checklist Reference` field (see SKILL.md Phase 3
Candidate Record Format). The field is required when this file is
loaded; it confirms the checklists were read.

---

## AWS IAM (Terraform, CloudFormation, source-embedded policies)

- `iam:PassRole` to a high-privilege role combined with `lambda:CreateFunction`,
  `ec2:RunInstances`, or `glue:CreateDevEndpoint`
- `iam:CreatePolicyVersion` or `iam:SetDefaultPolicyVersion` on a managed policy
  attached to high-privilege principals
- `iam:AttachUserPolicy`, `iam:AttachRolePolicy`, `iam:AddUserToGroup` granted
  without resource scoping
- `iam:PutUserPolicy`, `iam:PutRolePolicy`, `iam:PutGroupPolicy` (inline policy injection)
- `sts:AssumeRole` trust policies with `Principal: "*"` or overly broad conditions
- Wildcards in `Action` or `Resource` on policies bound to externally invokable Lambdas

## GCP IAM

- `iam.serviceAccountUser` or `iam.serviceAccountTokenCreator` on a high-privilege SA
- `iam.serviceAccountKeys.create` plus any compute role
- Custom roles with `*.setIamPolicy` permissions

## Kubernetes RBAC

- ClusterRole or Role granting `bind`, `escalate`, or `impersonate` verbs
- Wildcard `verbs: ["*"]` or `resources: ["*"]` on RoleBindings/ClusterRoleBindings
- ServiceAccount tokens auto-mounted into pods running untrusted code
- Pods with `hostPID: true`, `hostNetwork: true`, `hostIPC: true`, or `privileged: true`
- `hostPath` volumes mounted into pods reachable from external traffic

## Container / Dockerfile

- `USER root` (missing or root) in final image stage
- `--privileged` or `--cap-add=SYS_ADMIN`/`SYS_PTRACE`/`NET_ADMIN` in compose / k8s
- Secrets in `ENV`, `ARG`, or copied into image layers
- Unpinned base images (`FROM image:latest`) or unverified digests
- `ADD` of remote URLs (prefer `COPY` plus explicit verification)

## OAuth / OIDC application code

- `redirect_uri` validated by substring or `startswith` instead of exact match
- PKCE not enforced for public clients
- `state` parameter missing, predictable, or not validated on callback
- Tokens stored in `localStorage` accessible to XSS
- Implicit flow used for new applications

## Supply chain

- Direct deps without lockfile entry (lockfile drift)
- Package names with subtle typos against well-known libraries (typosquatting)
- Private package names that resolve to public registries (dependency confusion)
- Post-install / `prepare` scripts in `package.json` for dependencies not on a vetted allowlist
- Git URLs in `requirements.txt` or `package.json` pointing to forks

## LLM application code

- User-supplied content concatenated directly into a prompt without role separation
  or content fencing
- LLM output rendered into HTML, executed as code, used in SQL, or used in shell
  commands without validation
- Tool-use loops where the LLM can both choose and execute privileged tools without
  policy gating
- System prompts that contain secrets, API keys, or internal endpoints
- Indirect prompt injection paths: external data sources (web pages, documents,
  emails) flow into prompts without sanitization or trust labelling

## Secret Exposure

The LLM checks these patterns by direct file reading (grep/Read tool).
No external secrets scanner is run; raise candidates from source inspection.

- AWS access key patterns (`AKIA[0-9A-Z]{16}`, `ASIA[0-9A-Z]{16}`)
- GCP service account JSON or `AIza[0-9A-Za-z-_]{35}`
- Private keys (`-----BEGIN .* PRIVATE KEY-----`)
- High-entropy strings in `.env`, `.envrc`, CI config, or any file extension that
  ships to production
