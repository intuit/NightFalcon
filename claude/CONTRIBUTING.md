# Contributing to NightFalcon

Thanks for your interest in improving NightFalcon.

## What this repo is

NightFalcon is a Claude Code plugin — not an application. There is no build
step, no runtime server, and no unit-test suite. The "code" is mostly:

- **Subagent prompts** — `phases/*.md`
- **Shell gate scripts** — `scripts/*.sh`
- **Python tooling** — `scripts/report/`, `scripts/callgraph/`,
  `scripts/build-context-graph.py`

## Local checks before you open a PR

```bash
# Shell syntax
for f in scripts/*.sh; do bash -n "$f"; done

# Python compiles
python3 -m py_compile scripts/report/build.py \
  scripts/report/build-poc-execution.py \
  scripts/build-context-graph.py \
  scripts/callgraph/build-callgraph.py

# Graph builder runs (KB optional — OWASP graph must always regenerate)
python3 scripts/build-context-graph.py

# JSON metadata parses
python3 -c "import json; json.load(open('.claude-plugin/plugin.json'))"
python3 -c "import json; json.load(open('.claude-plugin/marketplace.json'))"
```

CI (`.github/workflows/plugin-check.yml`) runs the same checks.

## Editing the plugin

Plugin edits do **not** take effect until you bump `version` in both
`.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` and run
`/reload-plugins` — the plugin system caches by version. See `CLAUDE.md` for
the full architecture, conventions, and easy-to-violate invariants.

## Ground rules

- **Never bypass the gate scripts** — they are the enforcement mechanism.
- **Keep phases context-isolated** — pass each subagent only the files listed
  in `references/context-scope.md`.
- **Non-Regression Principle** — a missing taxonomy/context match never dismisses a
  finding; only a phase-4 debate can.
- **No proprietary or organization-specific data** in commits — keep private
  context only under an external `$NIGHTFALCON_CONTEXT_ROOT`, never inside the
  checkout. Package `references/organization_context/` remains empty public
  schema and fixtures.

## License

By contributing, you agree that your contributions are licensed under the
Apache License 2.0 (see `LICENSE`).
