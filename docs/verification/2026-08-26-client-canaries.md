# Client and Safety Canary Verification — 2026-08-26

## Client discovery

| Client | Version | Isolated check | Result |
|---|---:|---|---|
| Claude Code | 2.1.231 | `claude plugin validate --strict ./claude`, then read-only `--plugin-dir ./claude` discovery | PASS — marketplace validated; namespaced `/nightfalcon:nightfalcon` discovered (plugin skills require namespace) |
| Codex CLI | 0.148.0 | local marketplace add and `nightfalcon@nightfalcon-open` install under a temporary `CODEX_HOME` | PASS — plugin installed, enabled, version-bound, and listed |
| Cursor Agent | 2026.08.11-e8db854 | read-only workspace discovery with `--workspace ./cursor --mode ask` | PASS — `/nightfalcon` and `.cursor/rules/nightfalcon.mdc` discovered |

Checks used temporary or read-only state. No existing client configuration was changed.

## Fail-closed safety canaries

`tests/test_client_canaries.py` executes every canary independently against Claude, Codex, and Cursor package scripts:

1. Phase 2 is blocked when static dependency inventory is absent.
2. Phase 3 is blocked when post-Phase-2 cross-repository topology is absent.
3. Structured data flow is rejected when relationship, business-logic, or outbound-edge fields are absent.
4. Organization-context graph construction rejects a symlink escaping the configured provider root and publishes no output.

Result: **12 client-specific negative checks PASS** (four canaries across three clients).

## Boundary

These checks prove current package discovery and safety-critical contract enforcement. A full nine-phase review against an external repository is target-dependent operational validation, not a package-release prerequisite: it requires operator-selected repositories, provider authentication, and an authorized review scope. This release neither embeds nor assumes those targets or credentials. Package evidence therefore does not claim live UI persistence, model-transition behavior, or vulnerability findings for an unspecified external target.
