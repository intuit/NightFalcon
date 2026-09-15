#!/usr/bin/env bash
# gate-guard.sh — Cursor beforeShellExecution hook for NightFalcon.
#
# Cursor hook contract: JSON on stdin, JSON on stdout.
#   Input:  {"command": "...", "cwd": "...", "hook_event_name": "...", ...}
#   Output: {"permission":"allow"} | {"permission":"deny","user_message":...,"agent_message":...}
#   Exit 0 = success (stdout governs). Other exits fail-open (allow).
#
# This hook fires on EVERY shell command (no matcher) — it must, because the
# commands it needs to DENY (state.json tampering, artifact deletion) do not
# mention the gate scripts, so a name-scoped matcher would never see them. The
# gate-script invocations (check-gate.sh / complete-phase.sh / init-review.sh)
# ARE the enforcement mechanism and are fast-ALLOWED. What we DENY is tampering
# that would let the orchestrator skip a gate: mutating phase_status /
# current_phase in state.json from the shell, or deleting a gate output so a
# later gate's file check passes vacuously. Everything else is allowed, and the
# whole hook is a no-op unless an initialized NightFalcon workspace is the
# current directory, an ancestor, or a client-declared workspace root.
#
# This is defense in depth, not same-UID process sandboxing. Cursor cannot
# block a file *edit* (afterFileEdit is observational), and another local
# process can race or bypass a hook. The gate scripts remain load-bearing.
#
# Activation: dedicated marker plus canonical state.json in resolved workspace.

set -uo pipefail

allow() { printf '{"permission":"allow"}\n'; exit 0; }
deny() {
  # $1 = user_message, $2 = agent_message
  python3 - "$1" "$2" <<'PY' 2>/dev/null || printf '{"permission":"deny","user_message":"blocked by NightFalcon gate-guard"}\n'
import json, sys
print(json.dumps({"permission": "deny",
                  "user_message": sys.argv[1],
                  "agent_message": sys.argv[2]}))
PY
  exit 0
}

payload="$(cat 2>/dev/null || true)"
hook_dir="$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P)"

cmd="$(printf '%s' "$payload" | python3 -c 'import json,sys;
try: print(json.load(sys.stdin).get("command",""))
except Exception: print("")' 2>/dev/null)"
[ -z "$cmd" ] && allow
hook_cwd="$(printf '%s' "$payload" | python3 -c 'import json,sys;
try: print(json.load(sys.stdin).get("cwd", ""))
except Exception: print("")' 2>/dev/null)"
workspace="$(printf '%s' "$payload" | python3 "$hook_dir/workspace.py" 2>/dev/null)"
[ -z "$workspace" ] && allow
guard_root="$(CDPATH= cd -- "$(dirname -- "$0")/../.." 2>/dev/null && pwd -P)"

# Tokenize shell syntax before classifying mutation primitives. This avoids the
# old regex's narrow state.json-only redirect coverage while keeping read-only
# inspection and the exact gate/report/PoC invocations usable.
mutation="$(python3 - "$cmd" "$hook_cwd" "$guard_root" <<'PY' 2>/dev/null || true
import os
import re
import shlex
import sys


PROTECTED_NAMES = re.compile(
    r"(?:^|/)(?:state\.json|(?:dataflow|candidates|debate|findings)-[^/]+|"
    r"poc-manifest-[^/]+|receipt-[^/]+|pattern-tags-[^/]+|"
    r"executive-(?:summary|report)-[^/]+|session-manifest\.json)$"
)
SHELLS = {"bash", "dash", "ksh", "sh", "zsh"}
INTERPRETERS = {"node", "nodejs", "perl", "php", "python", "python3", "ruby"}
EDITORS = {
    "ed", "red", "ex", "vi", "view", "vim", "vimdiff", "gvim", "mvim",
    "nvim", "nano", "pico", "emacs", "emacsclient",
}
SEPARATORS = {";", "&&", "||", "|", "&", "(", ")"}
COMMAND_SUBSTITUTION_MARKER = "__NIGHTFALCON_COMMAND_SUBSTITUTION_"
BACKTICK = chr(96)
COMMAND_CWD = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else None
GUARD_ROOT = os.path.realpath(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] else None


def dynamic_command_substitution(value):
    return COMMAND_SUBSTITUTION_MARKER in value


def command_substitution_end(command, start):
    """Return the end of a balanced $() expression, including nested ones."""
    frames = [None]
    index = start + 2
    while index < len(command):
        quote = frames[-1]
        char = command[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if quote == "'":
            if char == "'":
                frames[-1] = None
            index += 1
            continue
        if quote == '"':
            if char == '"':
                frames[-1] = None
                index += 1
                continue
            if command.startswith("$(", index):
                frames.append(None)
                index += 2
                continue
            index += 1
            continue
        if char in {"'", '"'}:
            frames[-1] = char
            index += 1
            continue
        if command.startswith("$(", index):
            frames.append(None)
            index += 2
            continue
        if char == ")":
            frames.pop()
            index += 1
            if not frames:
                return index
            continue
        index += 1
    return len(command)


def backtick_substitution_end(command, start):
    """Return the end of one live backtick substitution, if it is complete."""
    index = start + 1
    while index < len(command):
        if command[index] == "\\":
            index += 2
            continue
        if command[index] == BACKTICK:
            return index + 1
        index += 1
    return None


def live_backtick_substitutions(command):
    """Return bodies of unescaped backticks outside single-quoted literals."""
    bodies = []
    quote = None
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if char == "'" and quote is None:
            quote = "'"
            index += 1
            continue
        if char == '"' and quote in {None, '"'}:
            quote = None if quote == '"' else '"'
            index += 1
            continue
        if char == BACKTICK:
            end = backtick_substitution_end(command, index)
            if end is None:
                break
            bodies.append(command[index + 1 : end - 1])
            index = end
            continue
        index += 1
    return bodies


def mask_command_substitutions(command):
    """Keep dynamic provenance while hiding substitution syntax from shlex."""
    pieces = []
    quote = None
    index = 0
    marker_index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'":
            pieces.append(command[index : index + 2])
            index += 2
            continue
        if quote == "'":
            pieces.append(char)
            if char == "'":
                quote = None
            index += 1
            continue
        if char == "'" and quote is None:
            quote = char
            pieces.append(char)
            index += 1
            continue
        if char == '"' and quote in {None, '"'}:
            quote = None if quote == '"' else '"'
            pieces.append(char)
            index += 1
            continue
        if quote is None and char in {"\r", "\n"}:
            previous = next(
                (candidate for candidate in reversed(command[:index]) if not candidate.isspace()),
                "",
            )
            pieces.append(" " if previous in {"&", "|", ";", "("} else " ; ")
            if char == "\r" and command[index : index + 2] == "\r\n":
                index += 2
            else:
                index += 1
            continue
        if command.startswith("$(", index):
            end = command_substitution_end(command, index)
            pieces.append(f"{COMMAND_SUBSTITUTION_MARKER}{marker_index}__")
            marker_index += 1
            index = end
            continue
        if char == BACKTICK:
            end = backtick_substitution_end(command, index)
            if end is not None:
                pieces.append(f"{COMMAND_SUBSTITUTION_MARKER}{marker_index}__")
                marker_index += 1
                index = end
                continue
        pieces.append(char)
        index += 1
    return "".join(pieces)


def protected(value):
    value = value.strip().replace("\\", "/")
    if dynamic_command_substitution(value):
        return True
    while value.startswith("./"):
        value = value[2:]
    return (
        value == "state.json"
        or value.endswith("/state.json")
        or value in {"findings", "output", "proof_of_concept"}
        or value.startswith(("findings/", "output/", "proof_of_concept/"))
        or "/findings/" in value
        or "/output/" in value
        or "/proof_of_concept/" in value
        or bool(PROTECTED_NAMES.search(value))
    )


def contains_protected_reference(value):
    return protected(value) or bool(
        re.search(
            r"(?<![A-Za-z0-9_.-])(?:\.?/)?(?:state\.json|findings/|output/|proof_of_concept/)",
            value,
        )
    )


def tokenize(command):
    lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;()<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def command_segments(tokens):
    segment = []
    separator_before = None
    for token in tokens:
        if token in SEPARATORS:
            if segment:
                yield segment, separator_before
                segment = []
            separator_before = token
        else:
            segment.append(token)
    if segment:
        yield segment, separator_before


def inline_interpreter_write(program, tokens):
    flags = {
        "node": ("-e", "--eval"),
        "nodejs": ("-e", "--eval"),
        "perl": ("-e",),
        "php": ("-r",),
        "python": ("-c",),
        "python3": ("-c",),
        "ruby": ("-e",),
    }[program]
    code = None
    for flag in flags:
        try:
            code = tokens[tokens.index(flag) + 1]
            break
        except (ValueError, IndexError):
            continue
    if code is None:
        return False
    if not contains_protected_reference(code) and not any(protected(token) for token in tokens):
        return False
    write_syntax = re.compile(
        r"(?:write_text|write_bytes|File\.write|File\.open|fs\.(?:write|append)|"
        r"open\s*\([^)]*['\"](?:a|w|x|r\+|a\+|w\+)|"
        r"(?:unlink|remove|rename|replace|truncate|rmtree|copy|move)\s*\(|"
        r"q\(>\)|['\"]>+['\"])",
        re.IGNORECASE,
    )
    return bool(write_syntax.search(code))


def interpreter_script_invocation(program, args):
    """Return a script path and its arguments, excluding interpreter options."""
    inline_flags = {
        "node": {"-e", "--eval", "-p", "--print"},
        "nodejs": {"-e", "--eval", "-p", "--print"},
        "perl": {"-e", "-E"},
        "php": {"-r", "--run"},
        "python": {"-c", "-m"},
        "python3": {"-c", "-m"},
        "ruby": {"-e"},
    }
    if program in SHELLS:
        if "-c" in args:
            return None
        value_options = {"-O", "-o", "--init-file", "--rcfile"}
        explicit_script_options = set()
    else:
        if any(flag in args for flag in inline_flags[program]):
            return None
        value_options = {
            "node": {"-r", "--require", "--import", "--loader", "--conditions"},
            "nodejs": {"-r", "--require", "--import", "--loader", "--conditions"},
            "perl": {"-F", "-I", "-M", "-m", "-x"},
            "php": {"-c", "--php-ini", "-d", "--define", "-z", "--zend-extension"},
            "python": {"-W", "-X", "--check-hash-based-pycs"},
            "python3": {"-W", "-X", "--check-hash-based-pycs"},
            "ruby": {"-C", "--directory", "-E", "--encoding", "-F", "-I", "-K", "-r"},
        }[program]
        explicit_script_options = {"-f", "--file"} if program == "php" else set()

    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            index += 1
            break
        if token in explicit_script_options:
            if index + 1 >= len(args):
                return None
            return args[index + 1], args[index + 2 :]
        if program == "php" and token.startswith("--file="):
            return token.split("=", 1)[1], args[index + 1 :]
        if program == "php" and token.startswith("-f") and token != "-f":
            return token[2:], args[index + 1 :]
        if token in value_options:
            index += 2
            continue
        if token.startswith("--") and "=" in token:
            index += 1
            continue
        if token.startswith("-") and token != "-":
            index += 1
            continue
        break
    if index >= len(args) or args[index] == "-":
        return None
    return args[index], args[index + 1 :]


def approved_nightfalcon_entrypoint(program, script):
    """Recognize only the existing gate, report, and findings validator tools."""
    if not script or dynamic_command_substitution(script):
        return False
    expanded = os.path.expandvars(os.path.expanduser(script))
    if "$" in expanded:
        return False
    normalized = os.path.normpath(expanded)
    if normalized.startswith("../"):
        return False
    if os.path.isabs(normalized):
        if GUARD_ROOT is None:
            return False
        resolved = os.path.realpath(normalized)
        try:
            if os.path.commonpath([resolved, GUARD_ROOT]) != GUARD_ROOT:
                return False
            normalized = os.path.relpath(resolved, GUARD_ROOT)
        except (OSError, ValueError):
            return False
    normalized = normalized.replace("\\", "/")
    shell_entrypoints = {
        "scripts/check-gate.sh",
        "scripts/complete-phase.sh",
        "scripts/init-review.sh",
    }
    python_entrypoints = {
        "scripts/validate-findings.py",
        "scripts/report/build-executive.py",
        "scripts/report/build-markdown.py",
        "scripts/report/build-sarif.py",
        "scripts/report/build.py",
    }
    if program in SHELLS:
        return normalized in shell_entrypoints
    if program in {"python", "python3"}:
        return normalized in python_entrypoints or packaged_agent_journal(script)
    return False


def packaged_agent_journal(script):
    """Require the script operand to resolve to this packaged journal engine."""
    if not script or dynamic_command_substitution(script) or GUARD_ROOT is None:
        return False
    expanded = os.path.expandvars(os.path.expanduser(script))
    if "$" in expanded:
        return False
    if not os.path.isabs(expanded):
        if COMMAND_CWD is None:
            return False
        expanded = os.path.join(COMMAND_CWD, expanded)
    expected = os.path.realpath(os.path.join(GUARD_ROOT, "scripts", "agent-journal.py"))
    return os.path.realpath(os.path.abspath(expanded)) == expected


def script_argument_is_unsafe(value):
    if value == "--":
        return False
    candidate = value.split("=", 1)[1] if value.startswith("-") and "=" in value else value
    if candidate.startswith("-"):
        return dynamic_command_substitution(candidate) or "$" in candidate
    return contains_protected_reference(candidate) or git_pathspec_is_protected(candidate)


def editor_mutation_reason(program, args, *, streamed=False):
    informational = {"--help", "--version", "-h", "-v", "-V"}
    if len(args) == 1 and args[0] in informational:
        return None

    vi_family = {"ex", "vi", "view", "vim", "vimdiff", "gvim", "mvim", "nvim"}
    if program in vi_family:
        command_options = {
            "-c", "--cmd", "-S", "-s", "-e", "-E", "-es", "-Es", "-q", "-t",
            "--remote", "--remote-silent", "--remote-tab", "--remote-tab-silent",
            "--remote-send", "--remote-expr",
        }
        if any(
            token in command_options
            or token.startswith(("+", "-c", "--cmd=", "-S"))
            or bool(re.fullmatch(r"-[A-Za-z]*[cs][A-Za-z]*", token))
            for token in args
        ):
            return f"{program} command mode"
    elif program == "emacs":
        command_options = {
            "--batch", "-batch", "--eval", "-eval", "--execute", "-execute",
            "--funcall", "-funcall", "--load", "-load", "--script", "-script",
            "-f", "-l",
        }
        if any(
            token in command_options
            or token.startswith(
                ("--eval=", "--execute=", "--funcall=", "--load=", "--script=")
            )
            for token in args
        ):
            return "emacs command mode"
    elif program == "emacsclient":
        if any(
            token in {"--eval", "--execute", "-e"}
            or token.startswith(("--eval=", "--execute="))
            for token in args
        ):
            return "emacsclient command mode"

    if streamed:
        return f"{program} streamed command mode"

    value_options = {
        "ed": {"-p", "--prompt"},
        "red": {"-p", "--prompt"},
        "ex": {"-i", "-T", "-u", "-U", "-w", "-W", "--servername"},
        "vi": {"-i", "-T", "-u", "-U", "-w", "-W", "--servername"},
        "view": {"-i", "-T", "-u", "-U", "-w", "-W", "--servername"},
        "vim": {"-i", "-T", "-u", "-U", "-w", "-W", "--servername"},
        "vimdiff": {"-i", "-T", "-u", "-U", "-w", "-W", "--servername"},
        "gvim": {"-i", "-T", "-u", "-U", "-w", "-W", "--servername"},
        "mvim": {"-i", "-T", "-u", "-U", "-w", "-W", "--servername"},
        "nvim": {"-i", "-T", "-u", "-U", "-w", "-W", "--servername"},
        "nano": {
            "-Q", "-T", "-Y", "--backupdir", "--operatingdir", "--quotestr",
            "--rcfile", "--speller", "--syntax", "--tabsize", "--wordchars",
        },
        "pico": {"-Q", "-T", "-Y", "--operatingdir", "--quotestr", "--tabsize"},
        "emacs": {"--chdir", "--directory", "--display", "--name", "--title", "-L"},
        "emacsclient": {"--alternate-editor", "--server-file", "--socket-name", "-a", "-s"},
    }[program]
    target_options = (
        {"--file", "--find-file", "--visit", "-file", "-find-file", "-visit"}
        if program == "emacs"
        else set()
    )
    paths = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            paths.extend(args[index + 1 :])
            break
        if token in target_options:
            if index + 1 >= len(args):
                return f"{program} unresolved target"
            paths.append(args[index + 1])
            index += 2
            continue
        if token in value_options:
            index += 2
            continue
        if token.startswith("--") and "=" in token:
            name, value = token.split("=", 1)
            if name in target_options:
                paths.append(value)
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        paths.append(token)
        index += 1
    if any(script_argument_is_unsafe(path) for path in paths):
        return f"{program} target"
    return None


def env_operand_assignment(token):
    """Match env's NAME=VALUE operand grammar, not shell identifier grammar."""
    return "=" in token and bool(token.split("=", 1)[0])


def env_command_operands(tokens):
    """Return env's nested command tokens, conservatively expanding -S."""
    tokens = list(tokens)
    for _ in range(8):
        index = 0
        split_command = None
        options = True
        while index < len(tokens):
            token = tokens[index]
            if options and token == "--":
                options = False
                index += 1
                continue
            if options and token in {
                "-", "-i", "--ignore-environment", "-0", "--null", "-v",
            }:
                index += 1
                continue
            if options and token in {"-u", "--unset", "-C", "--chdir"}:
                if index + 1 >= len(tokens):
                    return [], False
                index += 2
                continue
            if options and token.startswith(("--unset=", "--chdir=")):
                index += 1
                continue
            if options and token in {"-S", "--split-string"}:
                if index + 1 >= len(tokens):
                    return [], False
                split_command = tokens[index + 1]
                index += 2
                break
            if options and token.startswith("--split-string="):
                split_command = token.split("=", 1)[1]
                index += 1
                break
            if options and token.startswith("-S") and token != "-S":
                split_command = token[2:]
                index += 1
                break
            if (
                options
                and token in {"--help", "--version"}
                and index + 1 == len(tokens)
            ):
                return [], True
            if options and token.startswith("-"):
                return [], False
            if env_operand_assignment(token):
                index += 1
                continue
            return tokens[index:], True
        if split_command is None:
            return [], True
        if (
            any(character in split_command for character in "\\$#")
            or any(character in split_command for character in "\0\n\r\f\v")
        ):
            return [], False
        try:
            tokens = shlex.split(split_command) + tokens[index:]
        except ValueError:
            return [], False
    return [], False


def actual_command(segment, assignments):
    """Return only the executable position, unwrapping env/sudo/command."""
    assignment = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
    index = 0
    while index < len(segment) and (segment[index] == "!" or assignment.match(segment[index])):
        index += 1

    while index < len(segment):
        executable, resolved = expand_static(segment[index], assignments)
        program = os.path.basename(executable)
        if not resolved:
            return executable, segment[index + 1 :], False
        if program == "env":
            remaining, env_resolved = env_command_operands(segment[index + 1 :])
            if not env_resolved:
                return executable, [], False
            segment = segment[:index] + remaining
            continue
        if program == "sudo":
            index += 1
            sudo_value_options = {
                "-C", "--close-from", "-D", "--chdir", "-g", "--group",
                "-h", "--host", "-p", "--prompt", "-R", "--chroot",
                "-T", "--command-timeout", "-u", "--user",
            }
            while index < len(segment):
                token = segment[index]
                if token == "--":
                    index += 1
                    break
                if assignment.match(token):
                    index += 1
                    continue
                if token in sudo_value_options:
                    index += 2
                    continue
                if token.startswith("-"):
                    index += 1
                    continue
                break
            continue
        if program == "command":
            index += 1
            while index < len(segment):
                token = segment[index]
                if token == "--":
                    index += 1
                    break
                if token == "-p":
                    index += 1
                    continue
                if token in {"-v", "-V"}:
                    return "command", [], True
                if token.startswith("-"):
                    return "command", [], True
                break
            continue
        if program == "builtin":
            index += 1
            while index < len(segment) and segment[index].startswith("-"):
                if segment[index] == "--":
                    index += 1
                    break
                index += 1
            continue
        if program == "exec":
            index += 1
            while index < len(segment):
                token = segment[index]
                if token == "--":
                    index += 1
                    break
                if token == "-a":
                    index += 2
                    continue
                if token in {"-c", "-l"}:
                    index += 1
                    continue
                break
            continue
        return executable, segment[index + 1 :], True
    return None, [], False


def target_directory(args):
    for index, arg in enumerate(args):
        if arg in {"-t", "--target-directory"} and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith("--target-directory="):
            return arg.split("=", 1)[1]
        if arg.startswith("-t") and arg != "-t":
            return arg[2:]
    return None


def install_directory_operands(args):
    value_options = {
        "-g", "--group", "-m", "--mode", "-o", "--owner",
        "-S", "--suffix", "-t", "--target-directory",
    }
    operands = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            operands.extend(args[index + 1 :])
            break
        if token in value_options:
            index += 2
            continue
        if token.startswith(
            ("--group=", "--mode=", "--owner=", "--suffix=", "--target-directory=")
        ):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        operands.append(token)
        index += 1
    return operands


def workspace_root():
    configured = os.environ.get("SECURITY_REVIEW_WORKSPACE")
    if configured:
        return os.path.realpath(os.path.abspath(os.path.expanduser(configured)))
    if COMMAND_CWD:
        return os.path.realpath(COMMAND_CWD)
    return os.path.realpath(os.getcwd())


def paths_overlap(first, second):
    try:
        common = os.path.commonpath([first, second])
    except (OSError, ValueError):
        return True
    return common in {first, second}


def mutation_destination_is_unsafe(value, *, existing_directory=False):
    """Require a local destination that resolves disjoint from the workspace."""
    if not value or dynamic_command_substitution(value) or protected(value):
        return True
    expanded = os.path.expandvars(os.path.expanduser(value))
    if "$" in expanded:
        return True
    if not os.path.isabs(expanded):
        if COMMAND_CWD is None:
            return True
        expanded = os.path.join(COMMAND_CWD, expanded)
    normalized = os.path.abspath(os.path.normpath(expanded))
    if existing_directory:
        if not os.path.isdir(normalized):
            return True
        resolved = os.path.realpath(normalized)
    elif os.path.exists(normalized):
        resolved = os.path.realpath(normalized)
    else:
        parent = os.path.dirname(normalized)
        if not os.path.isdir(parent):
            return True
        resolved = os.path.join(os.path.realpath(parent), os.path.basename(normalized))
    return paths_overlap(resolved, workspace_root()) or protected(resolved)


def tar_options(args):
    operations = set()
    archive = None
    directory = None
    to_stdout = False

    def parse_letters(letters, next_value):
        parsed_archive = None
        parsed_directory = None
        consumed_next = False
        parsed_operations = set()
        parsed_stdout = False
        for letter_index, flag in enumerate(letters):
            operation = {
                "x": "extract", "t": "list", "c": "create",
                "r": "create", "u": "create", "A": "create",
            }.get(flag)
            if operation:
                parsed_operations.add(operation)
            if flag == "O":
                parsed_stdout = True
            if flag in {"f", "C"}:
                suffix = letters[letter_index + 1 :]
                value = suffix or next_value
                consumed_next = not suffix and next_value is not None
                if flag == "f":
                    parsed_archive = value
                else:
                    parsed_directory = value
                break
        return (
            parsed_operations,
            parsed_archive,
            parsed_directory,
            parsed_stdout,
            consumed_next,
        )

    index = 0
    while index < len(args):
        token = args[index]
        if token in {"--extract", "--get"}:
            operations.add("extract")
        elif token == "--list":
            operations.add("list")
        elif token == "--create":
            operations.add("create")
        elif token in {"--append", "--update", "--concatenate", "--delete"}:
            operations.add("create")
        elif token in {"-C", "--directory"}:
            if index + 1 >= len(args):
                return operations, archive, None, to_stdout
            directory = args[index + 1]
            index += 1
        elif token.startswith("--directory="):
            directory = token.split("=", 1)[1]
        elif token in {"-f", "--file"}:
            if index + 1 >= len(args):
                return operations, None, directory, to_stdout
            archive = args[index + 1]
            index += 1
        elif token.startswith("--file="):
            archive = token.split("=", 1)[1]
        elif token in {"-O", "--to-stdout"}:
            to_stdout = True
        elif token.startswith("-") and not token.startswith("--"):
            letters = token[1:]
            parsed = parse_letters(
                letters, args[index + 1] if index + 1 < len(args) else None
            )
            operations.update(parsed[0])
            archive = parsed[1] if parsed[1] is not None else archive
            directory = parsed[2] if parsed[2] is not None else directory
            to_stdout = to_stdout or parsed[3]
            if parsed[4]:
                index += 1
        elif index == 0 and re.fullmatch(r"[A-Za-z]+", token):
            parsed = parse_letters(
                token, args[index + 1] if index + 1 < len(args) else None
            )
            operations.update(parsed[0])
            archive = parsed[1] if parsed[1] is not None else archive
            directory = parsed[2] if parsed[2] is not None else directory
            to_stdout = to_stdout or parsed[3]
            if parsed[4]:
                index += 1
        index += 1
    return operations, archive, directory, to_stdout


def cpio_options(args):
    modes = set()
    directory = None
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--extract":
            modes.add("extract")
        elif token == "--list":
            modes.add("list")
        elif token == "--pass-through":
            modes.add("pass")
        elif token in {"-D", "--directory"}:
            directory = args[index + 1] if index + 1 < len(args) else None
            index += 1
        elif token.startswith("--directory="):
            directory = token.split("=", 1)[1]
        elif token.startswith("-") and not token.startswith("--"):
            letters = token[1:]
            letter_index = 0
            while letter_index < len(letters):
                flag = letters[letter_index]
                if flag == "i":
                    modes.add("extract")
                elif flag == "t":
                    modes.add("list")
                elif flag == "p":
                    modes.add("pass")
                if flag in {"D", "E", "F", "H", "I", "O", "R"}:
                    suffix = letters[letter_index + 1 :]
                    if flag == "D":
                        if suffix:
                            directory = suffix
                        elif index + 1 < len(args):
                            directory = args[index + 1]
                            index += 1
                    break
                letter_index += 1
        index += 1
    return modes, directory


def unzip_options(args):
    modes = set()
    directory = None
    index = 0
    while index < len(args):
        token = args[index]
        if token in {"--help", "--version"}:
            modes.add("informational")
        elif token.startswith("-") and not token.startswith("--"):
            letters = token[1:]
            letter_index = 0
            while letter_index < len(letters):
                flag = letters[letter_index]
                if flag in {"l", "t", "v", "Z"}:
                    modes.add("informational")
                elif flag in {"p", "c"}:
                    modes.add("stdout")
                if flag == "d":
                    suffix = letters[letter_index + 1 :]
                    if suffix:
                        directory = suffix
                    elif index + 1 < len(args):
                        directory = args[index + 1]
                        index += 1
                    break
                letter_index += 1
        index += 1
    return modes, directory


def archive_mutation_reason(program, args):
    if program in {"tar", "bsdtar"}:
        operations, archive, directory, to_stdout = tar_options(args)
        if "extract" in operations:
            if to_stdout:
                return None
            destination = directory or COMMAND_CWD
            if mutation_destination_is_unsafe(destination, existing_directory=True):
                return f"{program} extraction destination"
            return None
        if "create" in operations:
            if archive == "-":
                return None
            if mutation_destination_is_unsafe(archive):
                return f"{program} archive destination"
        return None

    if program == "unzip":
        modes, directory = unzip_options(args)
        if "informational" in modes:
            return None
        if "stdout" in modes:
            return None
        destination = directory or COMMAND_CWD
        if mutation_destination_is_unsafe(destination, existing_directory=True):
            return "unzip extraction destination"
        return None

    if program == "cpio":
        modes, directory = cpio_options(args)
        if "extract" in modes and "list" not in modes:
            destination = directory or COMMAND_CWD
            if mutation_destination_is_unsafe(destination, existing_directory=True):
                return "cpio extraction destination"
        if "pass" in modes:
            destination = next((arg for arg in reversed(args) if not arg.startswith("-")), None)
            if mutation_destination_is_unsafe(destination, existing_directory=True):
                return "cpio pass-through destination"
        return None

    if program in {"7z", "7za", "7zr"}:
        command_index = next(
            (index for index, token in enumerate(args) if not token.startswith("-")),
            None,
        )
        if command_index is None:
            return None
        operation = args[command_index].lower()
        remaining = args[command_index + 1 :]
        if operation in {"l", "t", "i"}:
            return None
        if operation in {"x", "e"}:
            directory = None
            for index, token in enumerate(remaining):
                if token == "-o":
                    directory = remaining[index + 1] if index + 1 < len(remaining) else None
                elif token.startswith("-o") and token != "-o":
                    directory = token[2:]
            destination = directory or COMMAND_CWD
            if mutation_destination_is_unsafe(destination, existing_directory=True):
                return "7z extraction destination"
        if operation in {"a", "u", "d"}:
            archive = next((arg for arg in remaining if not arg.startswith("-")), None)
            if mutation_destination_is_unsafe(archive):
                return "7z archive destination"
        return None

    if program in {"rar", "unrar"}:
        command_index = next(
            (index for index, token in enumerate(args) if not token.startswith("-")),
            None,
        )
        if command_index is None:
            return None
        operation = args[command_index].lower()
        remaining = args[command_index + 1 :]
        if operation in {"l", "lb", "lt", "v", "t"}:
            return None
        if operation in {"x", "e"}:
            directory = next(
                (token[3:] for token in remaining if token.startswith("-op")),
                None,
            )
            if directory is None and remaining and remaining[-1].endswith(("/", "\\")):
                directory = remaining[-1]
            destination = directory or COMMAND_CWD
            if mutation_destination_is_unsafe(destination, existing_directory=True):
                return f"{program} extraction destination"
        if program == "rar" and operation in {"a", "u", "f", "d"}:
            archive = next((arg for arg in remaining if not arg.startswith("-")), None)
            if mutation_destination_is_unsafe(archive):
                return "rar archive destination"
        return None

    return None


def expand_static(value, assignments, maximum_depth=8):
    """Resolve bounded local/environment variable chains without executing shell."""
    variable = re.compile(
        r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))"
    )
    expanded = value
    for _ in range(maximum_depth):
        if dynamic_command_substitution(expanded) or chr(96) in expanded:
            return expanded, False
        unresolved = False

        def replace(match):
            nonlocal unresolved
            name = match.group(1) or match.group(2)
            if name in assignments:
                return assignments[name]
            if name in os.environ:
                return os.environ[name]
            unresolved = True
            return match.group(0)

        updated = variable.sub(replace, expanded)
        if unresolved:
            return updated, False
        if updated == expanded:
            return updated, variable.search(updated) is None
        expanded = updated
    return expanded, variable.search(expanded) is None


def xargs_command(args):
    value_options = {"-a", "--arg-file", "-d", "--delimiter", "-E", "--eof", "-I", "--replace", "-L", "--max-lines", "-n", "--max-args", "-P", "--max-procs", "-s", "--max-chars"}
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            return args[index + 1 :]
        if token in value_options:
            index += 2
            continue
        if token.startswith(("-I", "-L", "-n", "-P", "-s")) and len(token) > 2:
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return args[index:]
    return []


def find_roots(args):
    """Return find path operands before the first expression."""
    roots = []
    index = 0
    while index < len(args):
        token = args[index]
        if token in {"-H", "-L", "-P"}:
            index += 1
            continue
        if token == "-D":
            index += 2
            continue
        if token.startswith("-") or token in {"!", "("}:
            break
        roots.append(token)
        index += 1
    return roots or ["."]


def canonical_find_root(value):
    expanded = os.path.expandvars(value)
    if "$" in expanded:
        return None
    absolute = os.path.abspath(os.path.expanduser(expanded))
    if not os.path.exists(absolute):
        return None
    return os.path.realpath(absolute)


def broad_find_root(value):
    expanded = os.path.expandvars(value)
    if "$" in expanded:
        return True
    normalized = value.rstrip("/") or "/"
    if normalized in {".", "..", "/"}:
        return True
    try:
        resolved = canonical_find_root(value)
        if resolved is None:
            return True
        targets = [os.path.realpath(os.getcwd())]
        workspace = os.environ.get("SECURITY_REVIEW_WORKSPACE")
        if workspace:
            targets.append(os.path.realpath(os.path.abspath(workspace)))
        return any(os.path.commonpath([resolved, target]) == resolved for target in targets)
    except (OSError, ValueError):
        return True


def git_subcommand(args):
    """Return a Git subcommand after global options and its remaining args."""
    value_options = {
        "-C", "-c", "--config-env", "--exec-path", "--git-dir",
        "--namespace", "--super-prefix", "--work-tree",
    }
    attached_options = (
        "-C", "-c", "--config-env=", "--exec-path=", "--git-dir=",
        "--namespace=", "--super-prefix=", "--work-tree=",
    )
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            index += 1
            break
        if token in {"--help", "--version"}:
            return None, []
        if token in value_options:
            if index + 1 >= len(args):
                return None, []
            index += 2
            continue
        if token.startswith(attached_options) and token not in {"-C", "-c"}:
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        break
    if index >= len(args):
        return None, []
    return args[index], args[index + 1 :]


def git_pathspec_is_protected(value):
    value = os.path.expandvars(value).replace("\\", "/")
    if "$" in value or protected(value):
        return True
    if value.startswith(":") or any(char in value for char in "*?["):
        return True
    normalized = os.path.normpath(os.path.expanduser(value))
    normalized_text = normalized.replace("\\", "/")
    if protected(normalized_text) or normalized_text.endswith(
        ("/findings", "/output", "/proof_of_concept")
    ):
        return True
    if normalized in {"", ".", "..", "/"} or normalized.startswith("../"):
        return True
    if not os.path.isabs(normalized):
        return False
    try:
        resolved = os.path.realpath(normalized)
        targets = [os.path.realpath(os.getcwd())]
        workspace = os.environ.get("SECURITY_REVIEW_WORKSPACE")
        if workspace:
            targets.append(os.path.realpath(os.path.abspath(workspace)))
        return any(
            os.path.commonpath([resolved, target]) == resolved
            for target in targets
        )
    except (OSError, ValueError):
        return True


def git_restore_pathspecs(args):
    paths = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            paths.extend(args[index + 1 :])
            break
        if token in {"-s", "--source"}:
            index += 2
            continue
        if token.startswith("--source="):
            index += 1
            continue
        if token in {"--pathspec-from-file", "--pathspec-file-nul"} or token.startswith(
            "--pathspec-from-file="
        ):
            return None
        if token.startswith("-"):
            index += 1
            continue
        paths.append(token)
        index += 1
    return paths


def git_clean_pathspecs(args):
    paths = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            paths.extend(args[index + 1 :])
            break
        if token in {"-e", "--exclude"}:
            index += 2
            continue
        if token.startswith("--exclude="):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        paths.append(token)
        index += 1
    return paths or ["."]


def git_mutation_reason(args):
    subcommand, subargs = git_subcommand(args)
    if subcommand is None:
        return None
    if subcommand == "restore":
        if subargs in (["--help"], ["-h"]):
            return None
        paths = git_restore_pathspecs(subargs)
        if paths is None:
            return "git restore unresolved pathspec"
        if any(git_pathspec_is_protected(path) for path in paths):
            return "git restore protected worktree"
        return None
    if subcommand == "checkout":
        if subargs in (["--help"], ["-h"]):
            return None
        if "--" in subargs:
            paths = subargs[subargs.index("--") + 1 :]
            if any(git_pathspec_is_protected(path) for path in paths):
                return "git checkout protected worktree"
            return None
        return "git checkout broad worktree mutation"
    if subcommand == "switch":
        if subargs in (["--help"], ["-h"]):
            return None
        return "git switch broad worktree mutation"
    if subcommand == "reset" and any(
        token in {"--hard", "--merge", "--keep"}
        or token.startswith(("--hard=", "--merge=", "--keep="))
        for token in subargs
    ):
        return "git reset worktree mutation"
    if subcommand == "clean":
        dry_run = "--dry-run" in subargs or any(
            token.startswith("-")
            and not token.startswith("--")
            and "n" in token[1:]
            for token in subargs
        )
        if dry_run:
            return None
        if any(
            git_pathspec_is_protected(path)
            for path in git_clean_pathspecs(subargs)
        ):
            return "git clean protected worktree"
        return None
    if subcommand == "apply":
        informational = any(
            token in {"--check", "--stat", "--numstat", "--summary"}
            for token in subargs
        )
        return None if informational and "--apply" not in subargs else "git apply mutation"
    if subcommand == "am":
        if len(subargs) == 1 and (
            subargs[0] == "--help"
            or subargs[0].startswith("--show-current-patch")
        ):
            return None
        return "git am worktree mutation"
    if subcommand == "checkout-index":
        return "git checkout-index worktree mutation"
    if subcommand == "read-tree" and any(
        token == "-u"
        or (
            token.startswith("-")
            and not token.startswith("--")
            and "u" in token[1:]
        )
        for token in subargs
    ):
        return "git read-tree worktree mutation"
    return None


def stream_command_mutates(command_tokens):
    if not command_tokens:
        return False
    command_tokens = list(command_tokens)
    if any(dynamic_command_substitution(token) for token in command_tokens):
        return True
    for _ in range(8):
        if not command_tokens or os.path.basename(command_tokens[0]) != "env":
            break
        command_tokens, env_resolved = env_command_operands(command_tokens[1:])
        if not env_resolved:
            return True
    else:
        return True
    if not command_tokens:
        return False
    program = os.path.basename(command_tokens[0])
    direct = {
        "rm", "unlink", "truncate", "touch", "chmod", "chown", "chgrp",
        "rmdir", "mkdir", "sponge", "sed", "tee", "cp", "install", "ln",
        "rsync", "ditto", "mv", "dd", "tar", "bsdtar", "unzip", "cpio",
        "7z", "7za", "7zr", "rar", "unrar",
    } | EDITORS
    if program in direct:
        return True
    if program in SHELLS and "-c" in command_tokens[1:]:
        try:
            code = command_tokens[command_tokens.index("-c") + 1]
        except IndexError:
            return False
        return bool(
            re.search(
                r"(?:^|[;&|]\s*)(?:rm|unlink|truncate|touch|chmod|chown|chgrp|"
                r"rmdir|mkdir|sponge|sed\s+-i|tee|cp|install|ln|rsync|ditto|"
                r"mv|dd|tar|bsdtar|unzip|cpio|7z|7za|7zr|rar|unrar)\b|>",
                code,
            )
        )
    return False


def mutation_reason(command, depth=0):
    if depth > 4:
        return "nested shell depth"
    for nested in live_backtick_substitutions(command):
        reason = mutation_reason(nested, depth + 1)
        if reason:
            return f"backtick substitution {reason}"
    try:
        tokens = tokenize(mask_command_substitutions(command))
    except ValueError:
        # An unparseable command naming both a protected path and a write
        # primitive is safer to deny than to interpret differently from shell.
        if contains_protected_reference(command) and re.search(r"(?:>|\b(?:rm|mv|cp|tee|truncate|dd)\b)", command):
            return "unparseable mutation command"
        return None

    for index, token in enumerate(tokens[:-1]):
        if ">" in token and protected(tokens[index + 1]):
            return "shell redirect"

    assignments = {}
    assignment = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
    protected_stream = contains_protected_reference(command)
    unresolved_stream = False
    for segment, separator_before in command_segments(tokens):
        if segment and all(assignment.match(token) for token in segment):
            for token in segment:
                match = assignment.match(token)
                assignments[match.group(1)] = match.group(2)
            continue
        segment_assignments = dict(assignments)
        for token in segment:
            match = assignment.match(token)
            if not match:
                break
            segment_assignments[match.group(1)] = match.group(2)
        executable, args, executable_resolved = actual_command(
            segment, segment_assignments
        )
        if executable is None:
            continue
        program = os.path.basename(executable)
        expanded_args = [expand_static(arg, segment_assignments) for arg in args]
        args = [value for value, _ in expanded_args]
        input_stream_unresolved = separator_before == "|" and unresolved_stream
        unresolved_stream = (
            any(not resolved for _, resolved in expanded_args)
            or input_stream_unresolved
        )
        for index, token in enumerate(args[:-1]):
            if ">" not in token:
                continue
            if not expanded_args[index + 1][1]:
                return "unresolved shell redirect"
            if protected(args[index + 1]):
                return "shell redirect"
        if not executable_resolved:
            return "unresolved executable"
        unresolved_arguments = any(not resolved for _, resolved in expanded_args)
        unresolved_mutators = {
            "rm", "unlink", "truncate", "touch", "chmod", "chown", "chgrp",
            "rmdir", "mkdir", "sponge", "tee", "cp", "install", "ln", "rsync",
            "ditto", "mv", "dd", "patch",
        } | EDITORS
        in_place = program in {"sed", "perl"} and any(
            arg == "-i" or arg.startswith("-i")
            or arg == "--in-place" or arg.startswith("--in-place=")
            for arg in args
        )
        if unresolved_arguments and (program in unresolved_mutators or in_place):
            return "unresolved mutation target"
        if program == "agent-journal.py" and not packaged_agent_journal(executable):
            return "unverified agent-journal.py executable"
        if program == "eval":
            eval_args = args[1:] if args[:1] == ["--"] else args
            eval_expansions = (
                expanded_args[1:] if args[:1] == ["--"] else expanded_args
            )
            if any(not resolved for _, resolved in eval_expansions):
                return "unresolved eval command"
            reason = mutation_reason(" ".join(eval_args), depth + 1)
            if reason:
                return f"eval {reason}"
            continue
        reason = archive_mutation_reason(program, args)
        if reason:
            return reason
        if program == "patch":
            if len(args) == 1 and args[0] in {"--help", "--version", "-h", "-v"}:
                continue
            return "patch mutation"
        if program == "git":
            reason = git_mutation_reason(args)
            if reason:
                return reason
            continue
        if program in EDITORS:
            reason = editor_mutation_reason(
                program,
                args,
                streamed=separator_before == "|" or "<" in segment,
            )
            if reason:
                return reason
            continue
        if program in SHELLS and "-c" in args:
            try:
                nested = args[args.index("-c") + 1]
            except IndexError:
                nested = ""
            reason = mutation_reason(nested, depth + 1)
            if reason:
                return f"nested shell {reason}"
        if program in INTERPRETERS and inline_interpreter_write(program, args):
            return f"{program} inline write"
        if program in INTERPRETERS or program in SHELLS:
            invocation = interpreter_script_invocation(program, args)
            if invocation is not None:
                script, script_args = invocation
                if (
                    os.path.basename(script) == "agent-journal.py"
                    and not packaged_agent_journal(script)
                ):
                    return f"{program} unverified agent-journal.py script"
                if not approved_nightfalcon_entrypoint(program, script) and any(
                    script_argument_is_unsafe(arg) for arg in script_args
                ):
                    return f"{program} script target"
        if program == "perl" and any(
            arg == "--in-place" or arg.startswith("--in-place=")
            or (arg.startswith("-") and not arg.startswith("--") and "i" in arg[1:])
            for arg in args
        ):
            if any(protected(arg) for arg in args):
                return "perl in-place"
        if program in {"rm", "unlink", "truncate", "touch", "chmod", "chown", "chgrp", "rmdir", "mkdir", "sponge"}:
            if any(protected(arg) for arg in args if not arg.startswith("-")):
                return program
        if program == "sed" and any(
            arg == "-i" or arg.startswith("-i")
            or arg == "--in-place" or arg.startswith("--in-place=")
            for arg in args
        ):
            if any(protected(arg) for arg in args):
                return "sed in-place"
        if program == "tee" and any(protected(arg) for arg in args if not arg.startswith("-")):
            return "tee"
        if program == "install" and any(
            token == "--directory"
            or (
                token.startswith("-")
                and not token.startswith("--")
                and "d" in token[1:]
            )
            for token in args
        ):
            if any(
                mutation_destination_is_unsafe(destination)
                for destination in install_directory_operands(args)
            ):
                return "install"
            continue
        if program in {"cp", "install", "ln", "rsync", "ditto"}:
            explicit_target = target_directory(args)
            operands = [arg for arg in args if not arg.startswith("-")]
            destination = explicit_target or (operands[-1] if operands else None)
            if destination and re.match(r"^[^/]+:", destination):
                continue
            if mutation_destination_is_unsafe(
                destination, existing_directory=explicit_target is not None
            ):
                return program
        if program == "mv" and any(protected(arg) for arg in args if not arg.startswith("-")):
            return "mv"
        if program == "dd" and any(
            arg.startswith("of=") and protected(arg.split("=", 1)[-1])
            for arg in args
        ):
            return "dd"
        if program == "find":
            roots = find_roots(args)
            resolved_roots = [canonical_find_root(root) for root in roots]
            protected_root = any(protected(root) for root in roots) or any(
                resolved is not None and protected(resolved)
                for resolved in resolved_roots
            )
            protected_selection = protected_root or any(
                broad_find_root(root) for root in roots
            )
            if protected_selection and "-delete" in args:
                return "find -delete"
            if protected_selection:
                for marker in ("-exec", "-execdir", "-ok", "-okdir"):
                    if marker not in args:
                        continue
                    start = args.index(marker) + 1
                    end = next(
                        (i for i in range(start, len(args)) if args[i] in {";", "+"}),
                        len(args),
                    )
                    nested = [
                        "findings/__matched__" if token == "{}" else token
                        for token in args[start:end]
                    ]
                    reason = mutation_reason(
                        " ".join(shlex.quote(token) for token in nested), depth + 1
                    )
                    if reason:
                        return f"find {marker} {reason}"
        if program == "xargs":
            nested = xargs_command(args)
            if stream_command_mutates(nested) and (
                protected_stream or input_stream_unresolved
            ):
                return "xargs nested mutation"
    return None


reason = mutation_reason(sys.argv[1])
if reason:
    print(reason)
PY
)"

if [[ -n "$mutation" ]]; then
  deny "NightFalcon: shell mutation of state.json or accepted review artifacts is blocked ($mutation)." \
       "Use the approved gate/report/PoC commands. Inspect artifacts read-only; advance phases only with scripts/complete-phase.sh."
fi

# Everything else (the legitimate gate-script invocations, git, clones, PoC
# runs) is allowed.
allow
