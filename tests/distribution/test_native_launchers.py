from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]


class NativeLauncherTests(unittest.TestCase):
    def test_registry_manifests_use_exact_public_names_and_version(self) -> None:
        npm = json.loads((ROOT / "package.json").read_text())
        cargo = tomllib.loads((ROOT / "Cargo.toml").read_text())
        composer = json.loads((ROOT / "composer.json").read_text())
        go_module = (ROOT / "go.mod").read_text()
        gemspec = (ROOT / "nightfalcon.gemspec").read_text()
        pom = ET.parse(ROOT / "pom.xml").getroot()
        nuget = ET.parse(ROOT / "packaging/nuget/NightFalcon.csproj").getroot()

        self.assertEqual((npm["name"], npm["version"]), ("nightfalcon", "3.0.0"))
        self.assertEqual(npm["bin"], {"nightfalcon": "packaging/npm/bin/nightfalcon.js"})
        self.assertEqual((cargo["package"]["name"], cargo["package"]["version"]), ("nightfalcon", "3.0.0"))
        self.assertEqual(composer["name"], "intuit/nightfalcon")
        self.assertIn("module github.com/intuit/nightfalcon", go_module)
        self.assertIn('spec.name = "nightfalcon"', gemspec)
        namespace = {"m": "http://maven.apache.org/POM/4.0.0"}
        self.assertEqual(pom.findtext("m:groupId", namespaces=namespace), "com.intuit")
        self.assertEqual(pom.findtext("m:artifactId", namespaces=namespace), "nightfalcon")
        self.assertEqual(pom.findtext("m:version", namespaces=namespace), "3.0.0")
        properties = nuget.find("PropertyGroup")
        self.assertIsNotNone(properties)
        self.assertEqual(properties.findtext("PackageId"), "nightfalcon")
        self.assertEqual(properties.findtext("Version"), "3.0.0")

    def test_script_launchers_forward_to_canonical_python_cli(self) -> None:
        commands = [
            ("node", ["node", "packaging/npm/bin/nightfalcon.js", "version"]),
            ("ruby", ["ruby", "packaging/rubygems/bin/nightfalcon", "version"]),
            ("php", ["php", "packaging/packagist/bin/nightfalcon", "version"]),
        ]
        for runtime, command in commands:
            if not shutil.which(runtime):
                continue
            with self.subTest(runtime=runtime):
                result = subprocess.run(
                    command, cwd=ROOT, text=True, capture_output=True, check=False
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "nightfalcon 3.0.0\n")

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

    def test_ruby_gem_builds_from_tracked_runtime_files(self) -> None:
        if not shutil.which("gem"):
            self.skipTest("RubyGems is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "nightfalcon-3.0.0.gem"
            result = subprocess.run(
                ["gem", "build", "nightfalcon.gemspec", "--output", str(output)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.is_file())

    def test_compiled_launcher_sources_embed_runtime_and_avoid_network_bootstrap(self) -> None:
        sources = [
            ROOT / "packaging/cargo/build.rs",
            ROOT / "packaging/cargo/src/main.rs",
            ROOT / "embedded.go",
            ROOT / "cmd/nightfalcon/main.go",
            ROOT / "packaging/maven/src/main/java/com/intuit/nightfalcon/Launcher.java",
            ROOT / "packaging/nuget/Program.cs",
            ROOT / "packaging/npm/bin/nightfalcon.js",
            ROOT / "packaging/rubygems/bin/nightfalcon",
            ROOT / "packaging/packagist/bin/nightfalcon",
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
        rust_builder = (ROOT / "packaging/cargo/build.rs").read_text()
        self.assertIn('static FILES: &[(&str, &[u8], bool)]', rust_builder)
        rust_launcher = (ROOT / "packaging/cargo/src/main.rs").read_text()
        self.assertIn("AlreadyExists", rust_launcher)
        self.assertIn("create_private_temp_root", rust_launcher)
        self.assertNotIn("if root.exists()", rust_launcher)

        sources = {
            "go": (ROOT / "embedded.go").read_text()
            + (ROOT / "cmd/nightfalcon/main.go").read_text(),
            "rust": rust_builder + rust_launcher,
            "maven": (
                ROOT / "packaging/maven/src/main/java/com/intuit/nightfalcon/Launcher.java"
            ).read_text(),
            "nuget": (ROOT / "packaging/nuget/Program.cs").read_text(),
        }
        for runtime, source in sources.items():
            with self.subTest(runtime=runtime):
                self.assertIn("runtime-executables.txt", source)
                self.assertRegex(source, r"(?i)(chmod|permissions|unixfilemode|isexecutable)")


if __name__ == "__main__":
    unittest.main()
