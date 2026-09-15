# NightFalcon for Cursor

The Cursor port of the NightFalcon adversarial security-review pipeline. It
runs a rigorous, gate-enforced, multi-phase security review of one or more
GitHub repositories, driven by a Cursor **skill** and enforced by shell **gate
scripts** and Cursor **hooks**.

This is a sibling of `claude/` (Claude Code plugin) and `codex/` (Codex
plugin). A pipeline behavior change is applied to each affected port.

## Install

1. Copy the contents of `cursor/` (`.cursor/`, `phases/`, `scripts/`, and
   `references/`) into your target repository. For an in-place checkout, open
   `cursor/` itself as the Cursor workspace; do not open the enclosing
   multi-client repository root. The skill resolves the port root from
   `.cursor/skills/nightfalcon/SKILL.md` (three directories up).
2. Make the hook scripts executable:
   ```bash
   chmod +x .cursor/hooks/*.sh scripts/*.sh
   ```
3. Open the repo in Cursor and trust the hooks when prompted (they are declared
   in `.cursor/hooks.json`).
4. Run the review:
   ```
   /nightfalcon https://github.com/org/repo-a https://github.com/org/repo-b
   ```
   or re-adjudicate an existing backlog:
   ```
   /nightfalcon --triage findings.json --repo ./path/to/source
   ```

The workspace is always the current directory (`$PWD`). Start Cursor from the
directory you want the review artifacts written into.

## How it works

The `/nightfalcon` skill (`.cursor/skills/nightfalcon/SKILL.md`) is the
**orchestrator** — it performs no analysis itself. For each phase it:

1. Runs `scripts/check-gate.sh --next-phase <phase>` (exit 2 = BLOCKED).
2. Spawns a fresh, context-isolated Cursor subagent that reads
   `phases/<phase>.md` and only the files listed in
   `references/context-scope.md`.
3. Runs `scripts/complete-phase.sh --phase <phase>` (exit 2 = BLOCKED).

Phase pipeline: **0 clone → 1 scope filter (external + internal surface) → 2
data-flow map → 3 score → 4 adversarial debate (blind Devil's Advocate, P0/P1/P2)
→ 5 online CVE validation → 6 PoC generation (self-contained, P0/P1/P2) → 7
per-repo findings report → 8 cross-repo HTML report**. Phases 1–7 run one
subagent per repo; phase-8 runs once.

`state.json` in the workspace is the source of truth (`current_phase`,
`repo_slugs`, `phase_status`, git checkpoints). Every phase boundary is a git
commit.

## Enforcement layers

1. **Gate scripts** (`scripts/check-gate.sh`, `scripts/complete-phase.sh`) —
   authoritative. Validate preconditions and outputs; advance `state.json` in
   the correct order; validate the manifest / enum spellings.
2. **Cursor hooks** (`.cursor/hooks.json`) — see `references/hook-setup.md`:
   - `beforeShellExecution` → `gate-guard.sh`: blocks shell attempts to bypass
     a gate (direct `state.json` writes, deleting artifacts). **Enforcing.**
   - `afterFileEdit` → `edit-log.sh`: logs out-of-scope edits. **Advisory
     only** — Cursor cannot block a file edit.
   - `stop` → `stop-enforce.sh`: nudges the agent to finish a phase whose
     output is missing (via `followup_message`, capped at 3).
3. **Skill discipline** — anti-stop-early rules, trust-boundary refusal of
   prompt injection, the Non-Regression Principle, and the model policy.

## Port differences vs Claude / Codex

Cursor's file-edit hook (`afterFileEdit`) is **observational** and cannot block
a write, so the per-phase write-path scope is advisory (logged) in this port
rather than hard-blocked. Cursor hooks signal decisions via JSON on stdout
(`permission: allow|deny` / `followup_message`) rather than exit code 2. The
gate scripts, phase prompts, enums, report builder, and self-contained-PoC
model are identical in intent to the other ports.

## Layout

```
cursor/
  .cursor/
    skills/nightfalcon/SKILL.md   orchestrator
    commands/nightfalcon.md       /nightfalcon entry
    rules/nightfalcon.mdc         project rule (discovery + contract)
    hooks.json                    hook registration
    hooks/                        gate-guard.sh, edit-log.sh, stop-enforce.sh
  phases/                         phase-0…8, phase-da, phase-poc-reviewer, triage-ingest
  scripts/                        check-gate.sh, complete-phase.sh, init-review.sh,
                                  state-utils.sh, report/build.py, report/template.html, …
  references/                     enums.md, context-scope.md, hook-setup.md, KB + OWASP graphs
  README.md  AGENTS.md
```

## No model selection

NightFalcon never selects, switches, or downgrades models. Every phase worker
and blind subagent runs on the model you currently have selected in Cursor.
Change it in Cursor at any time; new spawns pick up the new selection, running
agents keep theirs.
