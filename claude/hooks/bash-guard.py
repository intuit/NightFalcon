#!/usr/bin/env python3
"""Claude PreToolUse guard for shell mutation of NightFalcon-owned artifacts."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

from workspace import resolve_workspace


MUTATORS = {
    "apply_patch", "chmod", "chown", "cp", "dd", "install", "ln", "mkdir",
    "mv", "patch", "rm", "rmdir", "rsync", "tee", "touch", "truncate",
}
EDITORS = {
    "ed", "emacs", "emacsclient", "ex", "nano", "nvim", "pico", "red",
    "vi", "view", "vim",
}
INTERPRETERS = {"node", "nodejs", "perl", "php", "python", "python3", "ruby"}
SHELLS = {"bash", "dash", "ksh", "sh", "zsh"}
READ_ONLY_PROGRAMS = {
    "[", "basename", "cat", "cut", "diff", "dirname", "find", "git", "grep",
    "head", "jq", "ls", "pwd", "readlink", "realpath", "rg", "sed", "sort",
    "stat", "tail", "test", "tr", "uniq", "wc",
}
SEPARATORS = {";", "&&", "||", "|", "&", "(", ")"}
REDIRECTIONS = {">", ">>", ">|", "<>"}
PROTECTED = re.compile(
    r"(?:^|/)(?:state\.json|findings(?:/|$)|output(?:/|$)|proof_of_concept(?:/|$)|"
    r"(?:dataflow|candidates|debate|findings)-[^/]+|poc-manifest-[^/]+|"
    r"receipt-[^/]+|pattern-tags-[^/]+|executive-(?:summary|report)-[^/]+|"
    r"session-manifest\.json)$"
)
PROTECTED_TEXT = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:state\.json|findings(?:/|\b)|output(?:/|\b)|proof_of_concept(?:/|\b))"
)
WRITE_CODE = re.compile(
    r"(?:write_text|write_bytes|File\.(?:write|open)|fs\.(?:write|append)|"
    r"open\s*\([^)]*['\"](?:a|w|x|r\+|a\+|w\+)|"
    r"(?:unlink|remove|rename|replace|truncate|rmtree|copy|move)\s*\(|"
    r"q\(>\)|['\"]>+['\"])",
    re.IGNORECASE,
)


def deny(reason: str) -> None:
    print(f"BLOCKED: {reason}", file=sys.stderr)
    raise SystemExit(2)


def path_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def protected_reference(value: str, cwd: Path, workspace: Path) -> bool:
    normalized = value.strip("'\"").replace("\\", "/")
    if not normalized or normalized.startswith("-"):
        return False
    if "$" in normalized or "`" in normalized:
        return True
    if PROTECTED.search(normalized) or PROTECTED_TEXT.search(normalized):
        return True
    if normalized in {".", "./", "*", "./*"} and path_within(cwd, workspace):
        return True
    if any(character in normalized for character in "*?["):
        prefix = normalized.split("*", 1)[0].split("?", 1)[0].split("[", 1)[0]
        return bool(prefix) and protected_reference(prefix, cwd, workspace)
    candidate = Path(normalized).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    candidate = Path(os.path.normpath(candidate))
    if candidate == workspace:
        return True
    if path_within(candidate, workspace):
        relative = candidate.relative_to(workspace).as_posix()
        return bool(PROTECTED.search(relative))
    return False


def segments(command: str) -> list[list[str]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;()<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens = list(lexer)
    result: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in SEPARATORS:
            if current:
                result.append(current)
                current = []
        else:
            current.append(token)
    if current:
        result.append(current)
    return result


def unwrap(segment: list[str]) -> tuple[str, list[str]]:
    assignment = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
    index = 0
    while index < len(segment) and assignment.match(segment[index]):
        index += 1
    while index < len(segment):
        program = Path(segment[index]).name
        if program == "env":
            index += 1
            while index < len(segment) and (assignment.match(segment[index]) or segment[index].startswith("-")):
                if segment[index] in {"-u", "--unset", "-C", "--chdir"}:
                    index += 1
                index += 1
            continue
        if program in {"command", "exec", "sudo", "xargs"}:
            index += 1
            while index < len(segment) and segment[index].startswith("-"):
                index += 1
            continue
        return program, segment[index + 1 :]
    return "", []


def script_operand(program: str, args: list[str]) -> tuple[str | None, list[str]]:
    inline_flags = {"-c", "-e", "-E", "-r", "--eval", "--run"}
    if any(flag in args for flag in inline_flags):
        return None, args
    index = 0
    while index < len(args) and args[index].startswith("-"):
        if args[index] == "--":
            index += 1
            break
        index += 1
    if index >= len(args):
        return None, []
    return args[index], args[index + 1 :]


def approved_entrypoint(program: str, args: list[str], plugin_root: Path) -> bool:
    if program not in SHELLS | {"python", "python3"}:
        return False
    script, remaining = script_operand(program, args)
    if script is None or any(token in REDIRECTIONS for token in remaining):
        return False
    candidate = Path(os.path.expandvars(os.path.expanduser(script)))
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = Path(os.path.realpath(candidate))
    allowed = {
        plugin_root / "scripts/agent-journal.py",
        plugin_root / "scripts/check-gate.sh",
        plugin_root / "scripts/complete-phase.sh",
        plugin_root / "scripts/init-review.sh",
        plugin_root / "scripts/validate-findings.py",
        plugin_root / "scripts/report/build-executive.py",
        plugin_root / "scripts/report/build-markdown.py",
        plugin_root / "scripts/report/build-sarif.py",
        plugin_root / "scripts/report/build.py",
    }
    return candidate in {Path(os.path.realpath(item)) for item in allowed}


def inline_code(program: str, args: list[str]) -> str | None:
    flags = {
        "node": {"-e", "--eval"}, "nodejs": {"-e", "--eval"},
        "perl": {"-e", "-E"}, "php": {"-r", "--run"},
        "python": {"-c"}, "python3": {"-c"}, "ruby": {"-e"},
    }.get(program, set())
    for index, token in enumerate(args[:-1]):
        if token in flags:
            return args[index + 1]
    return None


def inspect_segment(segment: list[str], cwd: Path, workspace: Path, plugin_root: Path, depth: int) -> None:
    program, args = unwrap(segment)
    if not program:
        return
    if approved_entrypoint(program, args, plugin_root):
        return
    references = [token for token in segment if protected_reference(token, cwd, workspace)]

    for index, token in enumerate(segment[:-1]):
        if token in REDIRECTIONS and protected_reference(segment[index + 1], cwd, workspace):
            deny(f"shell redirection targets NightFalcon-owned artifact: {segment[index + 1]}")

    if program in SHELLS and "-c" in args:
        try:
            nested = args[args.index("-c") + 1]
        except IndexError:
            deny("shell -c command is missing")
        inspect(nested, cwd, workspace, plugin_root, depth + 1)
        return

    if program in MUTATORS and references:
        deny(f"{program} cannot mutate NightFalcon-owned artifacts")
    if program in EDITORS and references:
        deny(f"{program} cannot edit NightFalcon-owned artifacts")
    if program in {"sed", "perl"} and references and any(
        token == "-i" or token.startswith("-i") or token.startswith("--in-place") for token in args
    ):
        deny(f"{program} in-place mutation of NightFalcon-owned artifacts")
    if program == "find" and references and any(token in {"-delete", "-exec", "-execdir"} for token in args):
        deny("find mutation of NightFalcon-owned artifacts")
    if program == "git" and path_within(cwd, workspace) and args and args[0] in {
        "checkout", "clean", "mv", "reset", "restore", "rm",
    }:
        deny(f"git {args[0]} cannot mutate NightFalcon-owned artifacts")

    if program in {"7z", "bsdtar", "jar", "tar", "unzip"} and path_within(cwd, workspace):
        extraction = any(
            token in {"-x", "--extract", "xf", "x", "-xf"}
            or token.startswith(("-x", "--extract"))
            for token in args
        ) or program in {"unzip", "7z"}
        if extraction:
            deny(f"{program} extraction inside active review workspace")

    if program == "eval" and references and any(
        re.search(rf"(?<![A-Za-z0-9_]){re.escape(mutator)}(?![A-Za-z0-9_])", " ".join(args))
        for mutator in MUTATORS
    ):
        deny("eval cannot hide mutation of NightFalcon-owned artifacts")

    if program in INTERPRETERS:
        code = inline_code(program, args)
        if code is not None and (protected_reference(code, cwd, workspace) or references):
            deny(f"inline {program} cannot access NightFalcon-owned targets")
        script, remaining = script_operand(program, args)
        if script is not None and any(protected_reference(token, cwd, workspace) for token in remaining):
            deny(f"unapproved {program} script receives NightFalcon-owned target")

    if references and program not in READ_ONLY_PROGRAMS:
        deny(f"unclassified command {program} cannot access NightFalcon-owned targets")


def inspect(command: str, cwd: Path, workspace: Path, plugin_root: Path, depth: int = 0) -> None:
    if depth > 8:
        deny("nested shell command exceeds inspection limit")
    if PROTECTED_TEXT.search(command):
        mutation_words = "|".join(re.escape(item) for item in sorted(MUTATORS))
        if re.search(rf"\bxargs\b[^\n;]*(?:{mutation_words})\b", command):
            deny("xargs cannot stream NightFalcon-owned targets into a mutator")
        if ("$(" in command or "`" in command) and re.search(rf"\b(?:{mutation_words})\b", command):
            deny("command substitution cannot hide mutation of NightFalcon-owned artifacts")
    try:
        parsed = segments(command)
    except ValueError as exc:
        deny(f"cannot parse active-run shell command: {exc}")
    for segment in parsed:
        inspect_segment(segment, cwd, workspace, plugin_root, depth)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        deny(f"invalid Claude hook payload: {exc}")
    workspace = resolve_workspace(payload)
    # Hook subprocesses do not inherit exports from earlier Bash tool calls.
    # Dedicated marker plus canonical state avoid activating in unrelated
    # projects that happen to own a generic state.json.
    if workspace is None:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = payload.get("tool_input", {}).get("command")
    if not isinstance(command, str) or not command.strip():
        deny("Bash hook payload has no command")
    cwd = Path(os.path.realpath(payload.get("cwd") or workspace))
    plugin_root_value = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    if not plugin_root_value:
        deny("CLAUDE_PLUGIN_ROOT is unavailable during active review")
    plugin_root = Path(os.path.realpath(plugin_root_value))
    inspect(command, cwd, workspace, plugin_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
