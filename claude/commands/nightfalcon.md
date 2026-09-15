---
description: Run the multi-phase adversarial security review against one or more GitHub repos (with optional args).
---

# /nightfalcon:nightfalcon

You are being invoked via the `/nightfalcon:nightfalcon` namespaced plugin
slash command.
Load and execute the `nightfalcon` skill from this plugin
(file: `skills/nightfalcon/SKILL.md`).

Arguments passed to this command (zero or more, space-separated): `$ARGUMENTS`

## How to interpret arguments

- **If `$ARGUMENTS` contains one or more Git repository references** (any
  combination of GitHub HTTPS/SSH URLs, other HTTPS Git URLs, or
  `<org>/<repo>` GitHub shorthand), normalize them as documented by skill and
  treat them as list of repos to review. Proceed through skill Step 0, which
  blocks if existing state records a different repository scope; only a fresh
  run then enters phase-0 setup. Do not prompt again for the repo list.
- **If `$ARGUMENTS` is empty or contains no recognizable repo references**,
  first check canonical `state.json`. If it exists, resume only its recorded
  mode and repository scope without asking again. If it is absent, ask the user
  once for the list of repos before continuing. Accept any format above.
- **Model: user selected; `--model` is compatibility provenance only.** If
  `$ARGUMENTS` contains `--model <model-id>`, pass that value to
  `init-review.sh` so it is recorded in `state.json` as advisory provenance
  only. It never selects, pins, retries, or routes a phase subagent. Omit the
  model argument and every reasoning override on every worker, reviewer, and
  retry so each new spawn uses the model currently selected by the user.
  Already-running agents keep their launch model; later turns and new agents
  use the user's current selection.
- **If `$ARGUMENTS` contains `--triage <findings-file>`** (optionally with
  `--repo <path>`), run in **triage mode**: instead of discovering bugs from
  source, ingest the external findings backlog in `<findings-file>` and
  re-adjudicate it through the adversarial debate → validation → report
  pipeline (disproving/downgrading the junk). The skill initializes the run
  with `--mode triage`; the gate scripts then enforce, in code, that phases
  0–3 are skipped and the pipeline enters at phase-4. The model provenance
  rule above applies to triage runs too.
- **If `$ARGUMENTS` contains other flags or instructions** in addition to URLs
  (e.g., "focus on auth code", "skip dependency CVEs"), honor them as scoping
  hints but still run the full phase pipeline. The skill is the authority on
  phase ordering — flags cannot skip phases or bypass the gate scripts.

## Workspace setup

**The workspace is always the current working directory (`$PWD`).** No
exceptions. Do NOT ask the user for a workspace path, do NOT offer an
override prompt, and do NOT consult a workspace environment variable as an
input. The user invoked this
command from the directory they want the review to live in; respect
that, period.

Set the workspace, then let the skill's **Step 0** drive initialization —
it records optional advisory model provenance, resolves the mode and (for
triage) the slug, and calls `init-review.sh` with the right flags. Do not
hardcode the init call here; Step 0 in
`skills/nightfalcon/SKILL.md` is the authority.

```bash
WORKSPACE="$PWD"
# Step 0 then calls init-review.sh with the resolved values, e.g.:
#   bash "$CLAUDE_PLUGIN_ROOT/scripts/init-review.sh" \
#     --workspace "$WORKSPACE" --date "<state date on resume; today on fresh run>" \
#     --model "<advisory current model or empty>" \
#     --mode "<review|triage>"  # triage also: --slug "<slug>"
```

This scaffolds `input/`, `sourcecode/`, `findings/`, `output/`, and
`state.json` in the current directory. `--model`/`--mode`/`--slug` are
optional for a normal review (defaults: empty model provenance,
`review` mode); triage mode requires `--slug`. `init-review.sh` is idempotent
— if the workspace is already initialized (resuming a prior run), it
leaves existing files alone and continues.

Hooks resolve the nearest marked workspace ancestor from each hook payload's
`cwd` and activate only after both `.nightfalcon-review` and canonical
`state.json` exist. Do not rely on an `export` from an earlier Bash tool call:
Claude runs tool calls in separate shell processes.

**Any user message asking to use a different workspace** ("review this
into /tmp/foo", "use ~/audits as workspace") must be politely declined
in one sentence — the workspace is `$PWD`, and if they want a
different directory they should `cd` there before invoking the
command. This is non-negotiable because hook `cwd`, state, and gate paths must
identify the same workspace.

## Hand off to the orchestrator skill

After argument parsing and workspace setup are complete, follow the full
orchestrator protocol defined in `skills/nightfalcon/SKILL.md`:

1. Run `check-gate.sh --next-phase <phase>` before every phase.
2. Spawn the phase subagent with the system prompt at `phases/<phase>.md`,
   passing only the inputs listed in `references/context-scope.md` for that
   phase.
3. Run `complete-phase.sh --phase <phase>` after every phase.
4. Advance through phase-0 → phase-8 (or `done`) using the order in
   `state.json.current_phase`.

The Stop and PreToolUse hooks in `hooks/hooks.json` enforce phase output,
write-path discipline, and the per-session agent-spawn budget (default 150
spawns across all phases, `NIGHTFALCON_MAX_AGENTS` to override) mechanically —
you do not need to police them, but you also must not bypass them. When a
spawn is BLOCKED for budget exhaustion, follow SKILL.md "Agent budget":
checkpoint gracefully and report resume instructions instead of running into
the harness's session subagent ceiling.

## On resume

If `state.json` already exists, follow skill Step 0 on every invocation. It
re-runs idempotent `init-review.sh` with canonical `state.json.date`, reconciles
the journal, records current-epoch intent, then resumes from
`state.json.current_phase` via the gate-script pattern above.
