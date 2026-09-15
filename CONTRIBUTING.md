# Contributing

Contributions should preserve behavior across Claude Code, Codex, and Cursor.
Open an issue or pull request describing user-visible intent, affected phase,
security invariant, and verification evidence.

Before submitting:

1. Keep organization-private names, hosts, identifiers, policies, and context
  out of code, fixtures, docs, history, screenshots, and generated artifacts.
2. Apply behavior changes to all affected client distributions.
3. Add regression tests before changing security gates or analysis contracts.
4. Preserve context isolation, fail-closed gates, blind debate, and evidence
  requirements. Unknown data must not become dismissal evidence.
5. Update OWASP provenance when mappings or editions change. Do not copy full
  OWASP publications into this repository.

Contributions are licensed under Apache-2.0, except contributions to
OWASP-derived files identified in `NOTICE`, which are licensed under CC BY-SA
4.0.