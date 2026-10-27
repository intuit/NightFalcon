from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class NativeLauncherTests(unittest.TestCase):
    def test_registry_manifests_use_exact_public_names_and_version(self) -> None:
        npm = json.loads((ROOT / "package.json").read_text())
        go_module = (ROOT / "go.mod").read_text()

        self.assertEqual((npm["name"], npm["version"]), ("nightfalcon", "1.0.0"))
        self.assertEqual(npm["bin"], {"nightfalcon": "packaging/npm/bin/nightfalcon.js"})
        self.assertIn("module github.com/intuit/nightfalcon\n", go_module)

    def test_script_launchers_forward_to_canonical_python_cli(self) -> None:
        commands = [
            ("node", ["node", "packaging/npm/bin/nightfalcon.js", "version"]),
        ]
        for runtime, command in commands:
            if not shutil.which(runtime):
                continue
            with self.subTest(runtime=runtime):
                result = subprocess.run(
                    command, cwd=ROOT, text=True, capture_output=True, check=False
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "nightfalcon 1.0.0\n")

    def test_npm_package_contains_runtime_and_no_generated_output(self) -> None:
        if not shutil.which("npm"):
            self.skipTest("npm is not installed")
        result = subprocess.run(
            ["npm", "pack", "--dry-run", "--json", "--ignore-scripts"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        files = {item["path"] for item in payload[0]["files"]}
        self.assertIn("src/nightfalcon/cli.py", files)
        self.assertIn("claude/.claude-plugin/plugin.json", files)
        self.assertIn("codex/plugins/nightfalcon/.codex-plugin/plugin.json", files)
        self.assertIn("cursor/VERSION", files)
        self.assertTrue(all("output/" not in path and "__pycache__" not in path for path in files))

    def test_compiled_launcher_sources_embed_runtime_and_avoid_network_bootstrap(self) -> None:
        sources = [
            ROOT / "embedded.go",
            ROOT / "cmd/nightfalcon/main.go",
            ROOT / "packaging/npm/bin/nightfalcon.js",
        ]
        combined = "\n".join(path.read_text() for path in sources)
        self.assertIn("src/nightfalcon", combined)
        self.assertIn("python", combined.casefold())
        for forbidden in ("curl ", "wget ", "requests.get", "http.get", "https.get"):
            self.assertNotIn(forbidden, combined.casefold())

    def test_compiled_launchers_preserve_canonical_executable_payload_modes(self) -> None:
        manifest = ROOT / "packaging/runtime-executables.txt"
        declared = {
            line for line in manifest.read_text().splitlines() if line
        }
        tracked = subprocess.run(
            ["git", "ls-files", "--stage", "--", "claude", "codex", "cursor"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        expected = {
            line.split(maxsplit=3)[3]
            for line in tracked.stdout.splitlines()
            if line.startswith("100755 ")
        }
        self.assertEqual(declared, expected)
        self.assertIn("cursor/.cursor/hooks/gate-guard.sh", declared)

        sources = {
            "go": (ROOT / "embedded.go").read_text()
            + (ROOT / "cmd/nightfalcon/main.go").read_text(),
        }
        for runtime, source in sources.items():
            with self.subTest(runtime=runtime):
                self.assertIn("runtime-executables.txt", source)
                self.assertRegex(source, r"(?i)(chmod|permissions|unixfilemode|isexecutable)")


if __name__ == "__main__":
    unittest.main()
