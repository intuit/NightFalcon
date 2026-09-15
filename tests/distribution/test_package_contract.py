from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[2]
VERSION = "3.0.0"
REPOSITORY = "https://github.com/intuit/nightfalcon"


class PackageContractTests(unittest.TestCase):
    def test_python_distribution_metadata_is_canonical(self) -> None:
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
        project = metadata["project"]

        self.assertEqual(project["name"], "nightfalcon")
        self.assertEqual(project["version"], VERSION)
        self.assertEqual(project["requires-python"], ">=3.11")
        self.assertEqual(
            project["scripts"], {"nightfalcon": "nightfalcon.cli:entrypoint"}
        )
        self.assertEqual(project["urls"]["Repository"], REPOSITORY)
        self.assertEqual(project.get("dependencies", []), [])

    def test_all_distribution_versions_converge(self) -> None:
        self.assertEqual((ROOT / "VERSION").read_text().strip(), VERSION)
        claude_plugin = json.loads(
            (ROOT / "claude/.claude-plugin/plugin.json").read_text()
        )
        claude_marketplace = json.loads(
            (ROOT / "claude/.claude-plugin/marketplace.json").read_text()
        )
        codex_plugin = json.loads(
            (ROOT / "codex/plugins/nightfalcon/.codex-plugin/plugin.json").read_text()
        )

        self.assertEqual(claude_plugin["version"], VERSION)
        self.assertEqual(claude_marketplace["plugins"][0]["version"], VERSION)
        self.assertEqual(codex_plugin["version"].split("+", 1)[0], VERSION)
        self.assertEqual((ROOT / "cursor/VERSION").read_text().strip(), VERSION)
        self.assertEqual(claude_plugin["repository"], REPOSITORY)
        self.assertEqual(codex_plugin["repository"], REPOSITORY)

    def test_module_version_command_uses_distribution_version(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src")
        result = subprocess.run(
            [sys.executable, "-m", "nightfalcon", "version"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"nightfalcon {VERSION}\n")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
