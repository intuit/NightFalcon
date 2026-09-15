# AGENTS.md — NightFalcon (Cursor port)

This directory is the **Cursor port** of the NightFalcon adversarial
security-review pipeline. It is a Cursor skill + gate scripts + hooks, not an
application. Sibling ports: `../claude/` (Claude Code) and `../codex/` (Codex).
**Do not sync the ports with a blanket copy** —
a pipeline change is applied to each port separately and intentionally.

## Working on this port

- The orchestrator is `.cursor/skills/nightfalcon/SKILL.md`. It runs phases as
  isolated subagents and enforces sequence via `scripts/check-gate.sh` (before)
  and `scripts/complete-phase.sh` (after). Exit 2 = BLOCKED; never bypass.
- **Editing phase behavior** → `phases/phase-<N>.md` + matching section in
  `references/context-scope.md` + matching `case` arm in
  `scripts/complete-phase.sh` (what it must write) + `scripts/check-gate.sh`
  (preconditions + allowed write paths).
- **Enums** (`references/enums.md`) are authoritative and gate-validated:
  tiers `P0..P4`; dispositions `CONFIRMED | CONFIRMED-MODIFIED | DISMISSED |
  NEEDS-REVIEW`; validation `CURRENT | ACTIVELY-EXPLOITED | PATCHED |
  UNVERIFIED | NOT-RUN`; PoC verdict `VALID | NEEDS-REVISION | INVALID`;
  exposure `EXTERNAL | INTERNAL | INTERNAL-RESTRICTED` (§17, not gate-validated).
- **Stable finding IDs** `F-NNN` assigned in phase-3, never renumbered.
- **Self-contained PoCs** (schema v4): params inline per script, one per-repo
  `POC-GUIDE-<DATE>.md`, `poc-config.env` reference-catalog only. PoC tree at
  `output/proof_of_concept/<slug>/`. PoCs are P0/P1/P2 only.
- **Model**: never selected/switched/downgraded by the plugin; omit the `model`
  argument on every spawn so Cursor uses the user's selected model.

## Cursor-specific

- Hooks live in `.cursor/hooks.json` + `.cursor/hooks/*.sh`, use JSON
  stdin/stdout (not exit-2 blocking), and activate only when workspace
  `.nightfalcon-review` plus canonical `state.json` exist. `afterFileEdit` is advisory (Cursor cannot block
  a file edit); `beforeShellExecution` is the real guard; `stop` nudges via
  `followup_message`. See `references/hook-setup.md`.
- After editing a hook script: `bash -n .cursor/hooks/<name>.sh` and keep it
  `chmod +x`.
- After editing gate scripts / report builder:
  `bash -n scripts/check-gate.sh && bash -n scripts/complete-phase.sh &&
  python3 -m py_compile scripts/report/build.py`.

## Commonly used commands

```bash
# Run end-to-end (from the workspace dir, inside Cursor):
/nightfalcon https://github.com/org/repo-a

# Gate scripts (workspace = dir containing state.json):
bash scripts/init-review.sh --workspace "$PWD" --date "$(date +%Y-%m-%d)"
bash scripts/check-gate.sh --next-phase phase-2 --workspace "$PWD"
bash scripts/complete-phase.sh --phase phase-2 --workspace "$PWD"

# Build the HTML report (phase-8 deterministic step):
python3 scripts/report/build.py --input <exec-summary.json> --output <report.html>
```
