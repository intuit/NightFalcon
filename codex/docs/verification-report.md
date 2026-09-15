# NightFalcon Codex Verification Report

This report records deterministic packaging, gate, report, and validator checks. Isolated-install coverage is reported only when its complete command matrix is retained. Known limitations also record separately sourced live evidence; the command table does not claim live phase-agent execution.

## Summary

- Latest status: `PASS`
- Latest run ID: `20260827T193917.980259Z`
- Latest UTC timestamp: `2026-08-27T19:39:17Z`
- Recorded runs: `2`
- Isolated install coverage: `NOT RUN`
- Results SHA-256: `260e374c22577ffbcbebff63accf47f1eb0232ad5a5841ca17174c1c68005f66`

## Evidence

### Run `20260827T193417.252202Z`

Status: `PASS` · UTC: `2026-08-27T19:34:17Z`

Isolated install coverage: `NOT RUN`

Source digest: `f661256a7b7f8bfe6e095b9347fa012828294a0d09f511b3ffe0ddb23cca13b7` · Matrix: `core-only` / `0e80951adae29da6465d61600410b1a799118bc627c977cb466bbad37f155ebf`

| Check | Command | Exit | Result |
|---|---|---:|---|
| wrapper-tests | `'<PYTHON>' -m unittest discover -s '<WRAPPER>/tests' -p 'test_*.py' -v` | 0 | PASS |
| plugin-tests | `'<PYTHON>' -m unittest discover -s '<WRAPPER>/plugins/nightfalcon/tests' -p 'test_*.py' -v` | 0 | PASS |
| plugin-validator | `'<YAML_PYTHON>' '<PLUGIN_VALIDATOR>' '<WRAPPER>/plugins/nightfalcon'` | 0 | PASS |
| skill-validator | `'<YAML_PYTHON>' '<SKILL_VALIDATOR>' '<WRAPPER>/plugins/nightfalcon/skills/nightfalcon'` | 0 | PASS |
| shell-syntax | `bash -c 'set -euo pipefail; while IFS= read -r -d "" file; do bash -n "$file"; done < <(find "$1" -type f -name "*.sh" -print0)' verify-shells '<WRAPPER>/plugins/nightfalcon/scripts'` | 0 | PASS |
| python-compile | `'<PYTHON>' -m compileall -q '<WRAPPER>/plugins/nightfalcon/scripts' '<WRAPPER>/plugins/nightfalcon/hooks'` | 0 | PASS |
| json-validation | `bash -c 'set -euo pipefail; while IFS= read -r -d "" file; do "$2" -m json.tool "$file" >/dev/null; done < <(find "$1" -type f -name "*.json" -print0)' verify-json '<WRAPPER>/plugins/nightfalcon' '<PYTHON>'` | 0 | PASS |
| inventory-regeneration | `'<PYTHON>' '<WRAPPER>/scripts/build-port-inventory.py'` | 0 | PASS |
| inventory-stability | `'<PYTHON>' '<WRAPPER>/scripts/run-verification.py' --internal-file-equals '<INVENTORY_SNAPSHOT>' '<WRAPPER>/migration-map.json'` | 0 | PASS |
| port-scope | `'<PYTHON>' '<WRAPPER>/scripts/run-verification.py' --internal-scope-check '<WRAPPER>' 86015f536797e2ed7168f1cd1c2a0dbdaf35bf83` | 0 | PASS |

### Run `20260827T193917.980259Z`

Status: `PASS` · UTC: `2026-08-27T19:39:17Z`

Isolated install coverage: `NOT RUN`

Source digest: `f661256a7b7f8bfe6e095b9347fa012828294a0d09f511b3ffe0ddb23cca13b7` · Matrix: `core-only` / `0e80951adae29da6465d61600410b1a799118bc627c977cb466bbad37f155ebf`

| Check | Command | Exit | Result |
|---|---|---:|---|
| wrapper-tests | `'<PYTHON>' -m unittest discover -s '<WRAPPER>/tests' -p 'test_*.py' -v` | 0 | PASS |
| plugin-tests | `'<PYTHON>' -m unittest discover -s '<WRAPPER>/plugins/nightfalcon/tests' -p 'test_*.py' -v` | 0 | PASS |
| plugin-validator | `'<YAML_PYTHON>' '<PLUGIN_VALIDATOR>' '<WRAPPER>/plugins/nightfalcon'` | 0 | PASS |
| skill-validator | `'<YAML_PYTHON>' '<SKILL_VALIDATOR>' '<WRAPPER>/plugins/nightfalcon/skills/nightfalcon'` | 0 | PASS |
| shell-syntax | `bash -c 'set -euo pipefail; while IFS= read -r -d "" file; do bash -n "$file"; done < <(find "$1" -type f -name "*.sh" -print0)' verify-shells '<WRAPPER>/plugins/nightfalcon/scripts'` | 0 | PASS |
| python-compile | `'<PYTHON>' -m compileall -q '<WRAPPER>/plugins/nightfalcon/scripts' '<WRAPPER>/plugins/nightfalcon/hooks'` | 0 | PASS |
| json-validation | `bash -c 'set -euo pipefail; while IFS= read -r -d "" file; do "$2" -m json.tool "$file" >/dev/null; done < <(find "$1" -type f -name "*.json" -print0)' verify-json '<WRAPPER>/plugins/nightfalcon' '<PYTHON>'` | 0 | PASS |
| inventory-regeneration | `'<PYTHON>' '<WRAPPER>/scripts/build-port-inventory.py'` | 0 | PASS |
| inventory-stability | `'<PYTHON>' '<WRAPPER>/scripts/run-verification.py' --internal-file-equals '<INVENTORY_SNAPSHOT>' '<WRAPPER>/migration-map.json'` | 0 | PASS |
| port-scope | `'<PYTHON>' '<WRAPPER>/scripts/run-verification.py' --internal-scope-check '<WRAPPER>' 86015f536797e2ed7168f1cd1c2a0dbdaf35bf83` | 0 | PASS |

## Known limitations

- PASS: Current Claude Code, Codex CLI, and Cursor Agent package discovery is documented in docs/verification/2026-08-26-client-canaries.md.
- TARGET-DEPENDENT: Full phase-0 through phase-8 execution against an external repository is operational validation, not a package-release prerequisite; it requires an operator-selected target, provider authentication, and authorized scope.
- LIMITATION: Deterministic tests cover user-selected model policy and hook contracts; package evidence does not claim live UI persistence or live multi-phase model-transition behavior.
- PASS-WITH-DELTA: NightFalcon leaves spawn model and reasoning overrides unset, records initial and passive advisory provenance, and never rejects, retries, or reroutes work because an observed model differs.

## Reproduce

From the enclosing NightFalcon repository:

```bash
bash codex/scripts/verify-port.sh
```

From a standalone `codex` package checkout:

```bash
bash scripts/verify-port.sh --skip-codex-install
```
