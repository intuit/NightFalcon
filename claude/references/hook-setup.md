# Hook Setup

The plugin registers hooks automatically via `hooks/hooks.json` when enabled in Claude Code.
Hooks are a second enforcement layer — the orchestrator's gate scripts are the primary mechanism.

## What the hooks do

### Stop hook — output enforcement
Fires when Claude attempts to stop a session. Blocks stopping if the current phase
has not written its required output files to disk. This prevents Claude from declaring
a phase complete before the output actually exists.

### PreToolUse hook — write path guard
Fires before every Write or Edit tool call. Blocks writes to paths outside what the
current phase is permitted to write. Prevents a phase subagent from accidentally
writing to a later phase's output location or corrupting prior phase output.
Direct writes to `state.json` are always blocked; packaged lifecycle scripts own
state transitions.

### PreToolUse hook — Bash mutation guard
Fires before every Bash tool call. During an active review it blocks shell
redirection, mutation utilities, editors, nested shells, and inline interpreter
writes that target `state.json`, findings, output, or proof-of-concept artifacts.
Read-only inspection remains available. Packaged lifecycle, journal, validation,
and report commands remain allowed. This is defense in depth against tool-level
gate bypass, not a same-UID operating-system sandbox.

### PreToolUse hook — agent spawn budget
Fires before every Agent/Task tool call while a run is active. Counts the spawn
against a per-session budget (default **150**, override with the
`NIGHTFALCON_MAX_AGENTS` environment variable) covering ALL spawns — phase
workers, phase-4 Devil's Advocates, phase-6 PoC reviewers. Once the budget is
spent, further spawns are BLOCKED and the run pauses gracefully (checkpoint +
resume instructions) instead of dying at the harness's own session subagent
ceiling (typically 200). Counters live under `<workspace>/.agent-spawns/`,
keyed by session id — a fresh session gets a fresh budget and resumes from
`state.json.current_phase`. The Stop hook permits stopping (the one sanctioned
mid-queue stop) when a spawn was actually blocked, or when the session's
remaining budget falls below the current phase's spawn threshold (15 for the
fan-out phases 4 and 6, 2 for every other phase) — the proactive pause the
orchestrator performs before every spawn, verified from the shell-written
counter.

## Durable activation

Hook subprocesses resolve the nearest ancestor of hook payload `cwd` containing
both `.nightfalcon-review` and canonical `state.json`, bounded by
`CLAUDE_PROJECT_DIR` when Claude supplies it. Dedicated marker avoids false
activation in unrelated projects that own `state.json`; ancestor discovery
keeps hooks active after `cd sourcecode/<slug>`. These durable files work
across Claude's separate Bash tool processes. No workspace export is required.

`NIGHTFALCON_MAX_AGENTS` (optional, default `150`) sets the per-session
subagent spawn budget. Keep it comfortably below the harness session
subagent limit (`CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION`, typically 200)
so the graceful pause always fires before the hard ceiling:

```bash
export NIGHTFALCON_MAX_AGENTS=150
```

## Headless invocation

```bash
claude --bare \
  -p "$(cat ~/.claude/plugins/nightfalcon/skills/nightfalcon/SKILL.md)" \
  --plugin-dir ~/.claude/plugins/nightfalcon \
  --allowedTools "Read,Write,Edit,Bash,Glob,Grep,Agent,WebSearch" \
  --max-turns 200
```

## Hook behaviour on exit code 2

When a blocking hook exits with code 2 (BLOCKED):
- Claude receives the BLOCKED message and reason.
- Claude must stop the current action and report the block.
- Claude must not attempt to bypass or retry the blocked action.
- The block reason explains what is missing and what needs to happen first.
