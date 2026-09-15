#!/usr/bin/env python3
"""
Build a name-based call graph from tree-sitter query output.

Usage:
    build-callgraph.py <repo-root> <language> <output-dir>

Where:
    <repo-root>   Directory containing source files
    <language>    One of: java, javascript, python, go
    <output-dir>  Where to write callgraph.json and callgraph-summary.md

The script:
    1. Locates the matching query file under <skill>/assets/queries/<lang>-calls.scm
    2. Walks <repo-root> for files of that language (excluding obvious vendor/build dirs)
    3. Runs `tree-sitter query` on each file
    4. Aggregates captures into a JSON call graph: definitions keyed by name,
       call sites keyed by callee name
    5. Writes <output-dir>/callgraph-<lang>.json and <output-dir>/callgraph-<lang>-summary.md

Name-based resolution: when two functions share a name, both definitions are listed
under that name and all call sites map to that bucket. The LLM disambiguates at
review time using surrounding code context.

Fails fast: missing query file, missing tree-sitter CLI, or no source files of the
requested language all exit non-zero with a clear error.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

LANG_CONFIG = {
    "java":       {"ext": ".java", "captures": {"method.def": "def", "method.call": "call"}},
    "javascript": {"ext": ".js",   "captures": {"func.def": "def", "func.call": "call"}},
    "python":     {"ext": ".py",   "captures": {"func.def": "def", "func.call": "call"}},
    "go":         {"ext": ".go",   "captures": {"func.def": "def", "func.call": "call"}},
}

EXCLUDE_DIRS = {
    "node_modules", "vendor", "target", "build", "dist", "out",
    ".venv", "venv", "env", "__pycache__", ".tox", ".nox",
    ".next", ".nuxt", ".svelte-kit", ".cache", ".idea", ".vscode",
    ".git", "coverage", ".nyc_output", "generated", "__generated__",
    "bower_components", "jspm_packages", ".pnpm", ".yarn",
}

CAPTURE_RE = re.compile(r"capture: \d+ - ([\w.]+), start: \((\d+),")
TEXT_RE = re.compile(r"text: `([^`]+)`")


def find_source_files(root: Path, ext: str):
    """Yield source files under root, skipping known vendor/build dirs."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if fn.endswith(ext):
                yield Path(dirpath) / fn


def run_tree_sitter_query(query_path: Path, files: list[Path]) -> str:
    """Invoke tree-sitter on a batch of files. Returns raw stdout."""
    # tree-sitter accepts multiple files in one invocation; chunk to avoid argv limits.
    CHUNK = 200
    out_lines = []
    for i in range(0, len(files), CHUNK):
        batch = files[i:i + CHUNK]
        result = subprocess.run(
            ["tree-sitter", "query", str(query_path), *map(str, batch)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0 and not result.stdout:
            # Hard fail — empty output AND non-zero usually means CLI/grammar problem
            raise RuntimeError(
                f"tree-sitter query failed: {result.stderr.strip()}"
            )
        out_lines.append(result.stdout)
    return "".join(out_lines)


def parse_query_output(text: str, capture_map: dict[str, str]):
    """
    Parse tree-sitter's query stdout into (defs, calls) name → [(file, line)] mappings.

    Output format from `tree-sitter query`:
        <file>
          pattern: <N>
            capture: <N> - <capture-name>, start: (line, col), end: ..., text: `name`
    """
    defs = defaultdict(list)
    calls = defaultdict(list)
    current_file = None

    for line in text.splitlines():
        if not line:
            continue
        if not line.startswith(" "):
            current_file = line.strip()
            continue
        cap_m = CAPTURE_RE.search(line)
        name_m = TEXT_RE.search(line)
        if not (cap_m and name_m):
            continue
        capture_name = cap_m.group(1)
        kind = capture_map.get(capture_name)
        if kind is None:
            continue
        line_no = int(cap_m.group(2)) + 1   # tree-sitter is 0-indexed; humans want 1-indexed
        symbol = name_m.group(1)
        target = defs if kind == "def" else calls
        target[symbol].append({"file": current_file, "line": line_no})

    return defs, calls


def write_outputs(defs, calls, lang: str, out_dir: Path):
    """Write the JSON graph and a human-readable summary."""
    out_dir.mkdir(parents=True, exist_ok=True)

    graph_path = out_dir / f"callgraph-{lang}.json"
    graph_path.write_text(json.dumps({
        "language": lang,
        "definitions": dict(defs),
        "calls": dict(calls),
        "stats": {
            "distinct_def_names":  len(defs),
            "distinct_call_names": len(calls),
            "total_definitions":   sum(len(v) for v in defs.values()),
            "total_call_sites":    sum(len(v) for v in calls.values()),
        },
    }, indent=2))

    ranked = sorted(calls.items(), key=lambda kv: -len(kv[1]))
    summary_lines = [
        f"# Call Graph Summary — {lang}",
        "",
        f"- Distinct method/function names defined: **{len(defs)}**",
        f"- Distinct method/function names called : **{len(calls)}**",
        f"- Total definitions: **{sum(len(v) for v in defs.values())}**",
        f"- Total call sites : **{sum(len(v) for v in calls.values())}**",
        "",
        "## Top 20 most-called names",
        "",
        "| Calls | Local defs | Name |",
        "|---:|---:|---|",
    ]
    for name, sites in ranked[:20]:
        n_defs = len(defs.get(name, []))
        summary_lines.append(f"| {len(sites)} | {n_defs} | `{name}` |")
    summary_lines.append("")
    summary_lines.append(
        "*Names with 0 local defs are external calls — library or framework functions.*"
    )

    (out_dir / f"callgraph-{lang}-summary.md").write_text("\n".join(summary_lines))
    return graph_path


def main():
    if len(sys.argv) != 4:
        sys.exit(
            "Usage: build-callgraph.py <repo-root> <language> <output-dir>\n"
            f"Languages: {', '.join(LANG_CONFIG)}"
        )
    repo_root = Path(sys.argv[1]).resolve()
    language  = sys.argv[2]
    out_dir   = Path(sys.argv[3]).resolve()

    if language not in LANG_CONFIG:
        sys.exit(f"Unsupported language '{language}'. Choose: {', '.join(LANG_CONFIG)}")
    if not repo_root.is_dir():
        sys.exit(f"repo-root does not exist or is not a directory: {repo_root}")
    if shutil.which("tree-sitter") is None:
        sys.exit("tree-sitter CLI not on PATH. Install via: brew install tree-sitter-cli")

    cfg = LANG_CONFIG[language]
    skill_dir = Path(__file__).resolve().parent
    query_path = skill_dir / "queries" / f"{language}-calls.scm"
    if not query_path.is_file():
        sys.exit(f"Query file missing: {query_path}")

    files = list(find_source_files(repo_root, cfg["ext"]))
    if not files:
        sys.exit(f"No *{cfg['ext']} files found under {repo_root}")

    print(f"[{language}] indexing {len(files)} files under {repo_root}", file=sys.stderr)
    raw = run_tree_sitter_query(query_path, files)
    defs, calls = parse_query_output(raw, cfg["captures"])
    out_path = write_outputs(defs, calls, language, out_dir)
    print(f"[{language}] wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
