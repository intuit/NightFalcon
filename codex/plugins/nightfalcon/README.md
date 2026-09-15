# NightFalcon Codex plugin

`nightfalcon` is the installable plugin inside the `personal` marketplace
wrapper. Invoke its primary skill as `$nightfalcon <repo-url>` from the
directory that must become the review workspace.

## Runtime contract

- Workspace is exactly the task's `$PWD`; there is no workspace override.
- Preflight is run from that directory as `python3
  "$PLUGIN_ROOT/scripts/preflight.py"`, with no `--workspace` option.
- The shipped Codex defaults are `[agents] max_depth = 5` and
  `max_threads = 8`; effective depth values of 5 or greater and thread values
  of 3 or greater are accepted.
- Agent work runs in bounded batches with a run-wide open-agent ledger. Every
  completed phase worker, blind DA, and PoC reviewer thread is closed before
  its slot is reused or a phase boundary is crossed.
- Preflight file-resolves the configured thread ceiling; it cannot inspect
  task-start profiles, `-c` overrides, or `--ignore-user-config`. Runtime agent
  errors remain authoritative.
- Every phase worker, Devil's Advocate, and PoC reviewer is a fresh nested
  agent with only its phase-authorized context packet.
- NightFalcon never selects or switches models. Every phase worker and blind
  leaf omits model and reasoning overrides, so Codex uses the model currently
  selected by the user. Users may change that selection at any time;
  already-running agents keep their launch model, while later turns and new
  agents use the new selection. Model identity never causes rejection, retry,
  or rerouting.
- SessionStart provenance is best-effort. Newly initialized state stores
  `model_policy: "user-selected"`; a missing or unusable pin stores explicit
  `unknown`/`unavailable` provenance and never blocks. Passive history lives
  under `$PLUGIN_DATA/model-selections/`, and `NIGHTFALCON_MODEL_SELECTED` is
  advisory parent context only. Successful child registration adds no context.
  Legacy model-control fields are ignored and left byte-for-byte untouched.
- `check-gate.sh` runs before each phase and `complete-phase.sh` runs after it.
  Exit `2` is a hard block.
- Interrupted per-repo phases use `complete-phase.sh --check-slug <slug>` to
  validate existing artifacts without changing state and queue only unfinished
  repos.
- Hooks are an additional enforcement layer. Users must inspect and trust the
  SessionStart, UserPromptSubmit, SubagentStart, SubagentStop, Stop, and
  PreToolUse commands with `/hooks`; gate scripts remain authoritative.

## User-controlled model selection

Change the model directly in Codex whenever desired. No phase-boundary token,
state edit, or worker restart is required. Existing agents continue on their
launch model; subsequent turns and new agents use the new selection. The
run-wide thread ledger and phase gates continue to govern lifecycle and output
regardless of model.

## Contents

- `skills/nightfalcon/SKILL.md`: Codex entry point and orchestrator contract.
- `phases/`: phase-0 through phase-8 prompts plus blind reviewer prompts.
- `references/context-scope.md`: the only allowed context packets by phase.
- `references/enums.md`: canonical tiers, dispositions, and status enums.
- `hooks/`: Codex hook registration and the JSON adapter.
- `scripts/`: preflight, gates, state, callgraph, and report tooling.
- `tests/`: focused plugin behavior and prompt-contract tests.

## Installation and evidence

Use the wrapper-level lifecycle in
[`../../README.md`](../../README.md). The detailed source-to-port comparison is
[`../../docs/migration-comparison.md`](../../docs/migration-comparison.md).

Package and deterministic hooks have automated evidence. Pre-release full-run
evidence reached `state.json.current_phase == "done"`. Public releases still
require fresh live validation before claiming end-to-end hook behavior.
