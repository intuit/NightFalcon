# Live client invocation verification — 2026-08-27

Each final canary ran from an isolated, empty Git workspace in the client's
write-capable/default agent mode. No repository argument was supplied, so
success means the installed NightFalcon entry point resolved, loaded its
orchestrator, enforced client setup, and stopped at the safe repository-input
boundary without creating review state or output artifacts.

| Client | Version | Invocation | Result |
|---|---|---|---|
| Claude Code | 2.1.231 | `/nightfalcon:nightfalcon` via `claude --plugin-dir <claude-port> --permission-mode bypassPermissions -p ...` | PASS — asked for repository references; clean workspace remained unchanged |
| Codex CLI | 0.148.0 | `$nightfalcon` after isolated marketplace install, with `codex exec --sandbox workspace-write ...` | PASS — plugin, hooks, skill, `max_depth = 5`, and `max_threads = 8` resolved; asked for repository references; clean workspace remained unchanged |
| Cursor Agent | 2026.08.25-3e8eec8 | `/nightfalcon` via default agent mode with `cursor-agent --workspace <cursor-port> --force --print ...` | PASS — skill resolved and asked for review or triage input; clean workspace remained unchanged |

## Corrected launch defects

- Claude plugin skills are namespaced. Live invocation of bare `/nightfalcon`
  returned `Unknown command: /nightfalcon`; the public contract now uses
  `/nightfalcon:nightfalcon`.
- A Codex probe with `code_mode_host` disabled failed closed before skill
  preflight, proving that disabling the host is not a supported fallback.
  Normal Codex 0.148.0 found its bundled host and passed the final canary;
  documentation now requires repair/reinstall if host startup fails.
- Cursor must open the `cursor/` distribution root, not the enclosing
  multi-client repository root. Installation instructions now say so.

These are no-argument invocation canaries, not proof that a full live model
review of an arbitrary repository will produce identical findings across
clients. Deterministic gate, schema, hook, migration, and report behavior is
covered by repository regression suites.
