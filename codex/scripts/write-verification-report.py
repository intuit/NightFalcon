#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import shlex
from collections.abc import Mapping


INSTALL_COMMAND_NAMES = {
    "codex-marketplace-add",
    "codex-plugin-add",
    "codex-plugin-list",
    "codex-plugin-remove",
    "codex-marketplace-remove",
}
ISOLATED_CODEX_HOME_LABEL = "<VERIFY_TEMP_ROOT>/codex-home"


def _isolated_install_coverage(run: Mapping[str, object]) -> str:
    commands = run.get("commands")
    if not isinstance(commands, list):
        return "NOT RUN"
    install_commands = [
        command
        for command in commands
        if isinstance(command, Mapping) and command.get("name") in INSTALL_COMMAND_NAMES
    ]
    names = {command.get("name") for command in install_commands}
    if not names:
        return "NOT RUN"
    if names != INSTALL_COMMAND_NAMES:
        return "INCOMPLETE"
    if any(
        not isinstance(command.get("env"), Mapping)
        or command["env"].get("CODEX_HOME") != ISOLATED_CODEX_HOME_LABEL
        for command in install_commands
    ):
        return "FAIL"
    if all(
        command.get("passed") is True
        and isinstance(command.get("exit_code"), int)
        and not isinstance(command.get("exit_code"), bool)
        and command.get("exit_code") == 0
        for command in install_commands
    ):
        return "PASS"
    return "FAIL"


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def _command_text(command: object) -> str:
    if not isinstance(command, list):
        return ""
    return shlex.join(str(part) for part in command)


def render_report(payload: Mapping[str, object]) -> str:
    runs = payload.get("runs") or []
    if not isinstance(runs, list):
        raise ValueError("runs must be a list")
    latest = runs[-1] if runs else {}
    canonical = json.dumps(payload, indent=2) + "\n"
    results_hash = hashlib.sha256(canonical.encode()).hexdigest()
    lines = [
        "# NightFalcon Codex Verification Report",
        "",
        "This report records deterministic packaging, gate, report, and validator checks. Isolated-install coverage is reported only when its complete command matrix is retained. Known limitations also record separately sourced live evidence; the command table does not claim live phase-agent execution.",
        "",
        "## Summary",
        "",
        f"- Latest status: `{latest.get('status', 'NO-RUNS')}`",
        f"- Latest run ID: `{latest.get('run_id', 'n/a')}`",
        f"- Latest UTC timestamp: `{latest.get('timestamp_utc', 'n/a')}`",
        f"- Recorded runs: `{len(runs)}`",
        f"- Isolated install coverage: `{_isolated_install_coverage(latest)}`",
        f"- Results SHA-256: `{results_hash}`",
        "",
        "## Evidence",
        "",
    ]
    if not runs:
        lines.extend(["No verification runs recorded.", ""])
    for run in runs:
        lines.extend(
            [
                f"### Run `{run.get('run_id', 'unknown')}`",
                "",
                f"Status: `{run.get('status', 'unknown')}` · UTC: `{run.get('timestamp_utc', 'unknown')}`",
                "",
                f"Isolated install coverage: `{_isolated_install_coverage(run)}`",
                "",
                f"Source digest: `{run.get('source_digest', 'unavailable')}` · Matrix: `{run.get('matrix_kind', 'unavailable')}` / `{run.get('matrix_digest', 'unavailable')}`",
                "",
                "| Check | Command | Exit | Result |",
                "|---|---|---:|---|",
            ]
        )
        commands = run.get("commands") or []
        for command in commands:
            status = "PASS" if command.get("passed") else "FAIL"
            lines.append(
                "| {name} | `{command}` | {exit_code} | {status} |".format(
                    name=_cell(command.get("name", "")),
                    command=_cell(_command_text(command.get("command"))),
                    exit_code=command.get("exit_code", ""),
                    status=status,
                )
            )
        lines.append("")
        failures = [command for command in commands if not command.get("passed")]
        for failure in failures:
            lines.extend(
                [
                    f"#### Failure: `{failure.get('name', 'unknown')}`",
                    "",
                    "```text",
                    str(failure.get("failure_detail") or "No persisted output; inspect the console.").rstrip(),
                    "```",
                    "",
                ]
            )

    lines.extend(["## Known limitations", ""])
    limitations = payload.get("known_limitations") or []
    for limitation in limitations:
        lines.append(f"- {limitation}")
    if not limitations:
        lines.append("- None recorded.")
    lines.extend(
        [
            "",
            "## Reproduce",
            "",
            "From the enclosing NightFalcon repository:",
            "",
            "```bash",
            "bash codex/scripts/verify-port.sh",
            "```",
            "",
            "From a standalone `codex` package checkout:",
            "",
            "```bash",
            "bash scripts/verify-port.sh --skip-codex-install",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render NightFalcon verification JSON as Markdown.")
    parser.add_argument("--input", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = json.loads(args.input.read_text())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(render_report(payload))
        os.replace(temporary, args.output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Wrote verification report: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
