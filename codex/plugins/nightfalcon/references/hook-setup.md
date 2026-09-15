# Hook Setup

NightFalcon registers Codex lifecycle hooks from `hooks/hooks.json`. Install
or enable the `nightfalcon` plugin, then review and trust the hooks in
Codex with `/hooks` before starting `$nightfalcon`. Codex supplies
`$PLUGIN_ROOT`; every hook command resolves
`$PLUGIN_ROOT/hooks/nightfalcon_hook.py`, and the adapter delegates phase and
path checks to `$PLUGIN_ROOT/scripts/check-gate.sh`.

Gate scripts remain authoritative. Hooks preserve lifecycle registration,
output enforcement, and allowed write paths. They never enforce a model
selection.

## Model policy

`model_policy` is `"user-selected"`. NightFalcon never selects or switches
models. Omit the `model` argument and every reasoning override so Codex uses
the model currently selected by the user. A user may change that selection at
any time. Already-running agents keep their launch model; later turns and new
agents use the new selection. Never reject, retry, or reroute work because the
observed model differs.

The SessionStart hook may report `NIGHTFALCON_MODEL_SELECTED` for advisory
provenance and creates a best-effort pin under `$PLUGIN_DATA/model-pins/`.
Missing or unusable pin metadata never blocks initialization or resume. Parent
selections are recorded best-effort outside the review workspace under
`$PLUGIN_DATA/model-selections/<workspace-hash>/<session-id>.json`. Audit write
failure never blocks a turn, agent, tool, or completed output.

## Activation boundary

SessionStart may create the advisory initialization pin. `init-review.sh`
records available provenance or explicit `unknown`/`unavailable` provenance,
persists `model_policy: "user-selected"` for newly initialized state, and writes
`.nightfalcon-review` in the fixed `$PWD`. Existing state is preserved
byte-for-byte. UserPromptSubmit, SubagentStart, SubagentStop, Stop, and
PreToolUse behavior resolves the nearest ancestor of hook JSON `cwd` (or the
hook process working directory) containing both `.nightfalcon-review` and
canonical `state.json`. This keeps enforcement active below
`sourcecode/<slug>`. Outside such a workspace the adapter exits successfully
without enforcing NightFalcon rules.

Do not point hooks at a different directory mid-task. The workspace is always
the task's `$PWD`, and the marker—not an environment-supplied workspace
override—controls activation.

## Codex JSON input

Codex sends one JSON object on stdin. The adapter uses:

- `hook_event_name` for `SessionStart`, `UserPromptSubmit`, `SubagentStart`,
  `SubagentStop`, `Stop`, or `PreToolUse`;
- `cwd` to locate the marker-bound workspace;
- `session_id` and `model` on SessionStart to create initial provenance and a
  best-effort external audit entry;
- `turn_id`, `session_id`, and `model` on UserPromptSubmit only for passive
  external selection history;
- `agent_id`, `agent_type`, `session_id`, and `cwd` on SubagentStart and
  SubagentStop for active-agent registry membership; supplied model metadata is
  never copied into child context;
- `tool_name` and `tool_input` on PreToolUse to extract every target path for
  `apply_patch`, `Edit`, or `Write`.

Model values are never compared with `state.json.model`. Existing legacy
model-control fields in a resumed `state.json` are ignored and left untouched.

## Hook behavior

### SessionStart — initial provenance

When valid metadata is available, writes `{session_id, model, source}`
atomically under `$PLUGIN_DATA/model-pins/` and returns
`NIGHTFALCON_MODEL_SELECTED model=<model> session_id=<id> pin_file=<path>`.
Initialization records one SessionStart `model_history` entry with no epoch.
When the pin is unavailable or invalid, initialization records an unknown model,
unavailable provenance, and empty history instead of blocking.

### UserPromptSubmit — passive selection audit

Appends a history entry only when the user-selected model changes. The audit
document records `model_policy: "user-selected"`, `current_model`, and passive
history. Ordinary prompts, model changes, missing model labels, and audit I/O
failures all continue without model enforcement.

### SubagentStart — lifecycle registration

Registers a valid `agent_id` under
`$PLUGIN_DATA/active-agents/<workspace-hash>/<session-id>.json`. A successful
start returns no additionalContext, so lifecycle metadata cannot expand a blind
child's authorized packet. A missing registry is initialized lazily under the
lifecycle lock; a malformed existing registry remains fail-closed. Registration
failure still blocks phase work because losing lifecycle accounting can exhaust
the thread budget; model identity never affects registration or acceptance.

### SubagentStop — lifecycle cleanup

Uses `cwd`, `session_id`, and `agent_id` to unregister the child that
SubagentStart recorded. Missing identity data or failed cleanup emits
`NIGHTFALCON_ACTIVE_AGENT_UNREGISTER_FAILED` and leaves conservative registry
state in place. This protects bounded thread recovery, not model selection.

### Stop — output enforcement

Before Codex completes the task, the adapter runs
`check-gate.sh --enforce-output --workspace "$PWD"`. Exit `2` blocks completion
when the current phase's required artifacts are absent or invalid.

### PreToolUse — write-path enforcement

Shell-like and other non-file tools return success. Only `apply_patch`, `Edit`,
and `Write` continue through path extraction and
`check-gate.sh --check-path <path> --workspace "$PWD"` for every target. Exit
`2` blocks writes outside the current phase's allowlist. The adapter never
checks a model before or after path enforcement.

## Exit code 2

An exit code of `2` is `BLOCKED`. Report the adapter or gate reason and do not
bypass it. For an output block, continue the current phase until its contract
is satisfied. For a path block, use only the phase's allowed write location;
do not retry the prohibited target.
