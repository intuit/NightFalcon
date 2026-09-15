# OWASP Framework Index

Current official editions pinned 2026-08-26. Use category mappings as discovery prompts, never as dismissal gates. Missing mapping must not dismiss evidence. Optional organization-specific controls live in `[[Organization_Context]]`; cross-links never override code evidence.

| Framework | Edition/status | Primary review use |
|---|---|---|
| Web Top 10 | 2025 final | Web/application design and implementation |
| API Security Top 10 | 2023 final | Object/function authorization, business flows, API consumption |
| Mobile Top 10 | 2024 final | Mobile client, storage, transport, binary, privacy |
| LLM Applications Top 10 | 2026 final | Model-facing input/output, agency, context, vectors |
| Agentic Applications Top 10 | 2026 final | Tools, identity, memory, inter-agent trust, cascading failure |
| Kubernetes Top 10 | 2025 final | Workload, cluster, cloud, segmentation, components |
| MCP Top 10 | 2025 beta | Official-source reference only; noncommercial source body is not bundled or adapted |
| Non-Human Identities Top 10 | 2025 final | Service identities, secrets, lifecycle, isolation |
| Smart Contract Top 10 | 2026 final | On-chain authorization, economics, oracle, upgradeability |
| Business Logic Abuse Top 10 | 2025 final | Invariants, concurrency, state transitions, replay |
| Open Source Software Top 10 | current | Dependency provenance, maintenance, mutability, license |
| Agentic Skills Top 10 | v1 public-review draft | Skill packaging and execution; excluded from stable counts |
| ASVS | 5.0.0 final | Verification requirements |
| CI/CD Top 10 | 1.0 (2022), latest stable | Pipeline identity, execution, dependencies, artifacts |
| Desktop App Top 10 | 2021, latest stable | Desktop trust boundaries |
| Serverless Top 10 | 2017 interpretation, latest project edition | Event/config/identity risks |

## Web Top 10: 2025

A01 Broken Access Control; A02 Security Misconfiguration; A03 Software Supply Chain Failures; A04 Cryptographic Failures; A05 Injection; A06 Insecure Design; A07 Authentication Failures; A08 Software or Data Integrity Failures; A09 Security Logging and Alerting Failures; A10 Mishandling of Exceptional Conditions.

## API Security Top 10: 2023

API1 Broken Object Level Authorization; API2 Broken Authentication; API3 Broken Object Property Level Authorization; API4 Unrestricted Resource Consumption; API5 Broken Function Level Authorization; API6 Unrestricted Access to Sensitive Business Flows; API7 Server Side Request Forgery; API8 Security Misconfiguration; API9 Improper Inventory Management; API10 Unsafe Consumption of APIs.

## Mobile Top 10: 2024

M1 Improper Credential Usage; M2 Inadequate Supply Chain Security; M3 Insecure Authentication/Authorization; M4 Insufficient Input/Output Validation; M5 Insecure Communication; M6 Inadequate Privacy Controls; M7 Insufficient Binary Protections; M8 Security Misconfiguration; M9 Insecure Data Storage; M10 Insufficient Cryptography.

## LLM Applications Top 10: 2026

LLM01 Prompt Injection; LLM02 Sensitive Information Disclosure; LLM03 Excessive Agency; LLM04 Supply Chain; LLM05 Data and Model Poisoning; LLM06 Unbounded Consumption; LLM07 Misinformation; LLM08 Hidden Context Exposure; LLM09 Vector and Embedding Weaknesses; LLM10 Improper Output Handling.

## Agentic Applications Top 10: 2026

ASI01 Goal Hijack; ASI02 Tool Misuse and Exploitation; ASI03 Identity and Privilege Abuse; ASI04 Agentic Supply Chain Vulnerabilities; ASI05 Unexpected Code Execution; ASI06 Memory and Context Poisoning; ASI07 Insecure Inter-Agent Communication; ASI08 Cascading Failures; ASI09 Human-Agent Trust Exploitation; ASI10 Rogue Agents.

## Business Logic Abuse Top 10: 2025

BLA1 Action Limit Overrun; BLA2 Concurrent Workflow Order Bypass; BLA3 Object State Manipulations; BLA4 Malicious Logic Loop; BLA5 Artifact Lifetime Exploitation; BLA6 Missing Transition Validation; BLA7 Resource Quota Violation; BLA8 Internal State Disclosure; BLA9 Broken Access Control; BLA10 Shadow Function Abuse.

## Kubernetes Top 10: 2025 (external reference)

K01 Insecure Workload Configurations; K02 Overly Permissive Authorization Configurations; K03 Secrets Management Failures; K04 Lack of Cluster Level Policy Enforcement; K05 Missing Network Segmentation Controls; K06 Overly Exposed Kubernetes Components; K07 Misconfigured and Vulnerable Cluster Components; K08 Cluster to Cloud Lateral Movement; K09 Broken Authentication Mechanisms; K10 Inadequate Logging and Monitoring. Identifiers and titles are reference metadata only; NightFalcon does not bundle or adapt the project's noncommercial-licensed document body.

## Smart Contract Top 10: 2026 (external reference)

SC01 Access Control Vulnerabilities; SC02 Business Logic Vulnerabilities; SC03 Price Oracle Manipulation; SC04 Flash Loan-Facilitated Attacks; SC05 Lack of Input Validation; SC06 Unchecked External Calls; SC07 Arithmetic Errors; SC08 Reentrancy Attacks; SC09 Integer Overflow and Underflow; SC10 Proxy and Upgradeability Vulnerabilities. Identifiers and titles are reference metadata only; NightFalcon does not bundle or adapt the project's noncommercial-licensed document body.

## Open Source Software Top 10

OSS-RISK1 Known Vulnerabilities; OSS-RISK2 Compromised Legitimate Package; OSS-RISK3 Name Confusion Attacks; OSS-RISK4 Unmaintained Software; OSS-RISK5 Outdated Software; OSS-RISK6 Untracked Dependencies; OSS-RISK7 License and Regulatory Risk; OSS-RISK8 Immature Software; OSS-RISK9 Unapproved Change and Mutable Dependency; OSS-RISK10 Under/Over-Sized Dependency.
