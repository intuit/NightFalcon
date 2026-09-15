# NightFalcon for Codex

This directory is a self-contained Codex marketplace for NightFalcon. The
installable plugin is normalized as `nightfalcon` and lives at
`plugins/nightfalcon/`. It preserves the nine-phase review pipeline,
deterministic gates, context isolation, blind debate, PoC review, and report
contracts while adapting the package and agent runtime to Codex.

Package, prompt, hook-adapter, preflight, initialization, and inventory
contracts have deterministic test evidence. Pre-release full-run evidence
reached `state.json.current_phase == "done"`; current public behavior is bound
by shipped tests and fresh client canaries, not historical task identifiers.

## Install

Add this wrapper as local marketplace `nightfalcon-open`, install the
normalized plugin, and confirm that Codex can see it:

```bash
codex plugin marketplace add /path/to/NightFalcon-OpenSource/codex
codex plugin add nightfalcon@nightfalcon-open
codex plugin list
```

These commands change the active Codex home. For an isolated smoke test, set a
temporary `CODEX_HOME` first; `scripts/verify-port.sh` performs that smoke test
without touching the real Codex home.

## Configure

Merge the shipped default into either the trusted project's
`.codex/config.toml` or the personal `$CODEX_HOME/config.toml`:

```toml
[agents]
max_depth = 5
max_threads = 8
```

`max_depth = 5` is the distribution default. Preflight accepts any effective
integer value greater than or equal to 5. A project configuration takes
precedence over personal configuration, and an invalid project value blocks
rather than silently falling back.

`max_threads = 8` is the recommended distribution setting. NightFalcon reads
the file-resolved configured value, keeps one run-wide ledger of every open
phase worker, keeps no more than that many agent threads open, and closes every
completed worker before refilling the queue or crossing a phase boundary. Codex
defaults to 6 when the key is omitted. Values below 3 are blocked because one
Phase-6 worker must be able to run two blind reviewers concurrently.

File preflight cannot see task-start profiles, `-c` overrides, or
`--ignore-user-config`; do not use those configuration paths for a NightFalcon
task. Runtime agent errors remain authoritative and trigger bounded recovery
or a hard block if the live cap cannot support the required blind roles.

Configuration is loaded at different boundaries. For project-local changes,
start a new task after changing the project config. Fully restart Codex after
changing `$CODEX_HOME/config.toml`, then start a new task. NightFalcon never
edits Codex configuration for you.

Preflight binds the review workspace to its current directory. From the
intended workspace it is invoked without a workspace option:

```bash
cd /path/to/review-workspace
python3 "$PLUGIN_ROOT/scripts/preflight.py"
```

The skill runs this check automatically. Exit `2` means `BLOCKED`; do not
initialize or spawn review agents. The public CLI intentionally has no
`--workspace` option.

NightFalcon never selects or switches models. Every phase worker and nested
reviewer omits model and reasoning overrides, so Codex uses the model currently
selected by the user. A user may change that selection at any time.
Already-running agents keep their launch model; later turns and new agents use
the new selection. NightFalcon never rejects, retries, or reroutes work because
an observed model differs. SessionStart provenance is best-effort: newly
initialized state records `model_policy: "user-selected"`, while an absent or
unusable pin records `model: "unknown"` and `model_provenance: "unavailable"`
without blocking. `NIGHTFALCON_MODEL_SELECTED` and external
`$PLUGIN_DATA/model-selections/` history are advisory only and are never added
to successful child context.

## Trust hooks

After installation and configuration, start a new task and run `/hooks`.
Review the NightFalcon `SessionStart`, `UserPromptSubmit`, `SubagentStart`,
`SubagentStop`, `Stop`, and `PreToolUse` command hooks,
then explicitly trust them before starting a review.

Hook fixtures exercise JSON handling, marker-based activation, allowed and
denied writes, multi-file patches, and Stop enforcement. Codex exposes no
portable script API for reading persisted trust. Gate scripts remain mandatory
and authoritative even when hooks are unavailable.

## Run

Open the directory that will be the review workspace and invoke the skill in
the Codex task. Select or change the Codex model whenever desired:

```text
$nightfalcon https://github.com/owner/repository
```

Multiple repository URLs are accepted. Existing findings can be triaged with
the skill's `--triage` form and optional `--repo` source checkout.

If Codex itself fails before reading the skill with
`failed to spawn code-mode host ... codex-code-mode-host: No such file or directory`,
repair or reinstall the Codex client so its bundled host is available. Do not
disable `code_mode_host`: current Codex fails closed and cannot read the skill
or execute its required preflight when that host is disabled. This error comes
from the Codex runtime, not from NightFalcon plugin discovery.

The workspace is always the invoking task's `$PWD`. NightFalcon does not
prompt for a workspace, read a workspace override from the environment, or
offer a portable model-selection argument. Gate exit `2`, a failed isolated
spawn, or insufficient nested-agent depth stops the run without bypassing
phase contracts or evaluating a blind reviewer inline.

No phase-boundary prompt or state update is needed for a model change.
Already-running agents remain on their launch model; later turns and newly
spawned agents use the new selection. Security gates, context isolation, blind
packets, and thread cleanup remain unchanged.

When a task is interrupted during a per-repo phase, start a new task in the
same workspace and invoke `$nightfalcon` again. The resume path validates each
slug with the phase completion contract, preserves valid existing artifacts,
and queues only missing or invalid repo outputs.

Pre-release full-run evidence completed phase 8 and reached
`state.json.current_phase == "done"`. Public releases require current static,
fixture, and live-client verification rather than relying on that old run.

## Update

Update the NightFalcon checkout, then complete the official cachebuster so the
plugin manifest has a new version. From `codex/`, use the
repository-owned reinstall helper:

```bash
python3 scripts/reinstall-local-plugin.py
codex plugin list
```

The helper resolves the local marketplace, plugin version, active
`CODEX_HOME`, and cache paths dynamically. It runs `codex plugin add` without
hand-editing marketplace or Codex configuration, validates the installed copy
against a deterministic source manifest, preserves remaining real legacy
cache directories under an audited backup root, and atomically publishes
direct compatibility symlinks for prior version paths. Broken and stale links
are repaired; repeated execution is safe. The helper prints its JSON audit
record path on success and leaves aliases unchanged if install parity cannot be
proved. Journal files and rename publication use best-effort fsync barriers;
atomic recovery remains available after process interruption, while absolute
durability across sudden power loss still depends on the host filesystem and
hardware honoring those barriers. Loss before the first journal barrier and an
uncoordinated external process replacing cache paths between validation and
mutation remain outside the helper's transactional guarantee.

`codex plugin marketplace upgrade` refreshes Git marketplace snapshots and is
not part of this local-marketplace update flow. Fully restart Codex after the
reinstall, then start a new task so configuration, skills, and hooks are loaded
from the refreshed plugin.

## Uninstall

Remove the installed plugin:

```bash
codex plugin remove nightfalcon@nightfalcon-open
```

Optionally remove the local marketplace registration as well:

```bash
codex plugin marketplace remove nightfalcon-open
```

Removing the plugin does not delete review workspaces or their findings.

## Verify

Run the focused documentation and current regression suites from the
NightFalcon repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest codex/tests/test_documentation.py -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s codex/tests -p 'test_*.py' -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s codex/plugins/nightfalcon/tests -p 'test_*.py' -v
```

The local verification command is:

```bash
bash codex/scripts/verify-port.sh --skip-codex-install
```

Run that command from the enclosing NightFalcon repository root.
GitHub Actions automation is currently removed; run validation locally.

The verifier writes bounded machine evidence to `verification-results.json`
and the readable evidence summary to `docs/verification-report.md`. Final
handoff requires two consecutive clean runs with the same source digest,
command matrix, and complete passing command inventory. Check those bound runs
without changing evidence by running:

```bash
bash codex/scripts/verify-port.sh --check-two-clean-runs
```

The plugin and skill validators need PyYAML in their interpreter. Self-contained
snapshots are shipped at `scripts/validators/validate_plugin.py` and
`scripts/validators/quick_validate.py`; verifier discovery uses them as the
final fallback, so a standalone checkout does not depend on `~/.codex`. Install PyYAML in the Python environment before running validation.
Their provenance and upstream snapshot hashes are recorded in each
vendored file.
