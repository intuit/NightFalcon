#!/usr/bin/env python3

import argparse
import dataclasses
import json
import os
import pathlib
import shutil
import tomllib


@dataclasses.dataclass(frozen=True)
class PreflightResult:
    ok: bool
    max_depth: int | None
    config_source: str | None
    max_threads: int | None
    max_threads_source: str | None
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


REQUIRED_TOOLS = ("bash", "codex", "git", "jq", "python3")
REQUIRED_DEPTH = 5
REQUIRED_THREADS = 3
DEFAULT_MAX_THREADS = 6


class ConfigError(ValueError):
    def __init__(self, path: pathlib.Path, message: str):
        super().__init__(message)
        self.path = path


def load_agent_integer(path: pathlib.Path, key: str) -> int | None:
    if not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(path, f"Invalid Codex config {path}: {error}") from error

    agents = data.get("agents")
    if agents is None:
        return None
    if not isinstance(agents, dict):
        raise ConfigError(path, f"Invalid agents configuration in {path}: expected a table")
    if key not in agents:
        return None

    value = agents[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(path, f"Invalid agents.{key} in {path}: expected an integer")
    return value


def load_depth(path: pathlib.Path) -> int | None:
    return load_agent_integer(path, "max_depth")


def load_threads(path: pathlib.Path) -> int | None:
    return load_agent_integer(path, "max_threads")


def effective_max_depth(
    workspace: pathlib.Path, codex_home: pathlib.Path
) -> tuple[int | None, str | None]:
    selected = []
    for path in (codex_home / "config.toml", workspace / ".codex" / "config.toml"):
        depth = load_depth(path)
        if depth is not None:
            selected.append((depth, path))
    return (selected[-1][0], str(selected[-1][1])) if selected else (None, None)


def effective_max_threads(
    workspace: pathlib.Path, codex_home: pathlib.Path
) -> tuple[int, str]:
    selected = []
    for path in (codex_home / "config.toml", workspace / ".codex" / "config.toml"):
        threads = load_threads(path)
        if threads is not None:
            selected.append((threads, path))
    if selected:
        return selected[-1][0], str(selected[-1][1])
    return DEFAULT_MAX_THREADS, "Codex default"


def run_preflight(
    workspace: pathlib.Path, codex_home: pathlib.Path, *, which=shutil.which
) -> PreflightResult:
    errors = []
    try:
        depth, source = effective_max_depth(workspace, codex_home)
    except ConfigError as error:
        depth, source = None, str(error.path)
        errors.append(str(error))

    try:
        threads, threads_source = effective_max_threads(workspace, codex_home)
    except ConfigError as error:
        threads, threads_source = None, str(error.path)
        if str(error) not in errors:
            errors.append(str(error))

    errors.extend(
        f"Missing required executable: {name}"
        for name in REQUIRED_TOOLS
        if which(name) is None
    )
    if depth is None or depth < REQUIRED_DEPTH:
        errors.append("Codex nested agents require:\n[agents]\nmax_depth = 5")
    if threads is None or threads < REQUIRED_THREADS:
        errors.append(
            "NightFalcon's phase-6 worker plus two blind reviewers require:\n"
            "[agents]\nmax_threads = 3"
        )
    warnings = (
        "Review and trust NightFalcon hooks with /hooks before relying on mechanical enforcement.",
        "Preflight reads config files only; task-start Codex profiles, -c overrides, "
        "and --ignore-user-config are not inspectable. Do not use them for "
        "NightFalcon; runtime agent errors are authoritative.",
    )
    return PreflightResult(
        not errors,
        depth,
        source,
        threads,
        threads_source,
        tuple(errors),
        warnings,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check NightFalcon Codex prerequisites.")
    parser.add_argument(
        "--codex-home",
        type=pathlib.Path,
        default=pathlib.Path(os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex")),
    )
    parser.add_argument("--json", action="store_true", help="Print structured JSON output.")
    return parser.parse_args(argv)


def print_text(result: PreflightResult) -> None:
    if result.config_source is None:
        print("Checked agents.max_depth: not configured")
    elif result.max_depth is None:
        print(f"Checked agents.max_depth: invalid ({result.config_source})")
    else:
        print(f"Checked agents.max_depth: {result.max_depth} ({result.config_source})")
    if result.max_threads is None:
        print(f"Checked agents.max_threads: invalid ({result.max_threads_source})")
    else:
        print(
            f"Checked agents.max_threads: {result.max_threads} "
            f"({result.max_threads_source})"
        )
    for error in result.errors:
        print(f"ERROR: {error}")
    for warning in result.warnings:
        print(f"WARNING: {warning}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_preflight(pathlib.Path.cwd(), args.codex_home)
    if args.json:
        print(json.dumps(dataclasses.asdict(result), sort_keys=True))
    else:
        print_text(result)
    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
