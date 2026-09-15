# Hook Setup (Cursor)

Cursor distribution ships three hooks in `.cursor/hooks.json`. Cursor reads
`.cursor/hooks.json` from the project root; the hook scripts live in
`.cursor/hooks/` and must be executable (`chmod +x .cursor/hooks/*.sh`).

After copying the `.cursor/` directory into your repo, open Cursor and review /
trust the hooks (Cursor prompts on first run, or use the hooks panel). Then
invoke `/nightfalcon`.

The gate scripts (`scripts/check-gate.sh`, `scripts/complete-phase.sh`) remain
the **authoritative** enforcement. The hooks are a second layer. None of them
enforces a model selection.

## The three hooks

| Hook event | Script | Role |
|---|---|---|
| `beforeShellExecution` | `.cursor/hooks/gate-guard.sh` | **Enforcing.** Allows the gate-script invocations; DENIES shell attempts to bypass a gate (direct `state.json` writes, deleting review artifacts). Cursor can block shell commands, and the gate protocol is shell-driven, so this is the load-bearing guard — the Cursor analogue of Claude Code's PreToolUse Write/Edit guard. |
| `afterFileEdit` | `.cursor/hooks/edit-log.sh` | **Advisory only.** Cursor's file-edit hook is observational and cannot block a write. It appends a one-line note to `output/run-log-<DATE>.md` when the agent edits outside the current phase's allowed paths. |
| `stop` | `.cursor/hooks/stop-enforce.sh` | **Continuation.** When a turn ends with the current phase's required output missing, it returns a `followup_message` nudging the agent to finish the phase (capped at 3 follow-ups). Replaces the Claude/Codex exit-2 Stop block with Cursor's continuation model. |

## Cursor hook contract (why the scripts look the way they do)

Cursor hooks read a JSON object on **stdin** and write a JSON object on
**stdout**:

- `beforeShellExecution` → `{"permission":"allow"}` or
  `{"permission":"deny","user_message":"…","agent_message":"…"}`.
- `afterFileEdit` → `{}` (no permission/blocking fields are honored).
- `stop` → `{"followup_message":"…"}` to continue, or `{}` to end.

This differs from Claude Code / Codex, where hooks signal BLOCK via **exit code
2**. In Cursor the JSON on stdout governs; exit 0 is the normal path.

## Durable activation

All three hooks resolve the nearest ancestor of `cwd` containing
`.nightfalcon-review` and canonical `state.json`, bounded by `workspace_root`
or `workspace_roots` when Cursor supplies them. This keeps hooks active after
`cd sourcecode/<slug>` while preventing unrelated `state.json` files from
activating hooks. Durable files survive separate shell processes.
`SECURITY_REVIEW_WORKSPACE` remains an optional compatibility path hint, but
cannot activate hooks without both markers.

## What Cursor CANNOT enforce (intentional port limitation)

Cursor's `afterFileEdit` fires **after** the edit and cannot block it. So the
per-phase write-path scope is **advisory** in the Cursor port (logged, not
blocked), whereas the Claude and Codex ports block out-of-scope writes at the
harness level. The gate scripts still block the run from *advancing* past a
phase whose outputs are wrong or missing, and `gate-guard.sh` blocks shell
tampering — so sequencing and artifact integrity are still enforced; only the
in-phase write boundary is soft. Respect each phase's allowed write paths per
`references/context-scope.md`.

## Model policy

NightFalcon never selects or switches models. Omit the `model` argument and
every reasoning override so Cursor uses the model currently selected by the
user. A user may change that selection at any time. Already-running agents keep
their launch model; later turns and new agents use the new selection. A running
agent that hits repeated declines may have its model switched by the harness to
make progress, but every newly spawned subagent uses the user's current model.
Never reject, retry, or reroute work because the observed model differs.
