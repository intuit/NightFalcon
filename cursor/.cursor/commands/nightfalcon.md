# /nightfalcon

Run the multi-phase adversarial security review against one or more GitHub
repos.

You are being invoked via the `/nightfalcon` command in Cursor. Load and
execute the `nightfalcon` skill in this repo
(`.cursor/skills/nightfalcon/SKILL.md`) and follow it exactly.

Any text after the command is the skill's invocation arguments (see the
skill's "Invocation arguments" section):

- **One or more GitHub repository references** (`https://github.com/...`,
  `https://git.example.org/...`, `git@github.com:...`, or `<org>/<repo>`) →
  treat as the repos to review and proceed to Step 0. Initialization blocks if
  existing state records a different repository scope; only a fresh run then
  enters phase-0. Do not prompt again for the list.
- **No recognizable repo reference** → if canonical `state.json` exists,
  resume its recorded mode and repository scope without asking again;
  otherwise ask once for the list.
- **`--triage <findings-file>`** → triage mode (re-adjudicate an existing
  findings backlog); optional `--repo <path>` grounds the imported claims.
- **Other text** (e.g. "focus on auth", "skip dependency CVEs") → scoping
  hints only; they never change phase order, eligibility, or gates.

## Model

NightFalcon does not select or switch models. Every phase worker and blind
subagent runs on the model the user currently has selected in Cursor — omit the
`model` argument and every reasoning override on every spawn. There is no
`--model` override in the Cursor port; if the user passes one, explain that they
can change the selected model in Cursor at any time instead. The model is never
downgraded on the plugin's initiative (see the skill's model policy for the
running-agent carve-out).

## Before you start

The skill's Step 0 initializes `$PWD`, writing `.nightfalcon-review` and
canonical `state.json`. Those durable files engage `.cursor/hooks`; hooks keep
resolving that workspace from nested `sourcecode/<slug>` directories. Decline
any workspace override and tell the user to start from the desired directory.
