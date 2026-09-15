import json
import pathlib
import subprocess
import sys


def initialize_journal_fixture(
    workspace: pathlib.Path,
    date: str,
    engine: pathlib.Path,
) -> None:
    """Upgrade a lightweight workspace fixture through the production CLI."""
    output = workspace / "output"
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / "session-manifest.json"
    if not manifest.exists():
        manifest.write_text(
            json.dumps(
                {
                    "date": date,
                    "phases_completed": [],
                    "checkpoints": [],
                }
            )
            + "\n"
        )
    result = subprocess.run(
        [
            sys.executable,
            str(engine),
            "init",
            "--workspace",
            str(workspace),
            "--date",
            date,
            "--port",
            "fixture",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    state = json.loads((workspace / "state.json").read_text())
    if state.get("repo_slugs"):
        intent = subprocess.run(
            [
                sys.executable,
                str(engine),
                "record-intent",
                "--workspace",
                str(workspace),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if intent.returncode:
            raise AssertionError(intent.stderr)
    git_directory = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "--git-dir"],
        text=True,
        capture_output=True,
        check=False,
    )
    if git_directory.returncode:
        commands = (
            ("git", "-C", str(workspace), "init", "-q"),
            (
                "git", "-C", str(workspace), "config", "user.name",
                "NightFalcon Test",
            ),
            (
                "git", "-C", str(workspace), "config", "user.email",
                "nightfalcon@example.invalid",
            ),
            ("git", "-C", str(workspace), "add", "-A"),
            (
                "git", "-C", str(workspace), "commit", "-qm",
                "fixture baseline",
            ),
        )
        for command in commands:
            completed = subprocess.run(
                command,
                text=True,
                capture_output=True,
                check=False,
            )
            if completed.returncode:
                raise AssertionError(completed.stderr)
