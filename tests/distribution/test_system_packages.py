from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class SystemPackageTests(unittest.TestCase):
    def test_homebrew_formula_renders_final_artifact_checksum_and_parses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            release = base / "release"
            build = subprocess.run(
                [
                    sys.executable,
                    "scripts/release/build-zipapp.py",
                    "--repo",
                    str(ROOT),
                    "--output-dir",
                    str(release),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(build.returncode, 0, build.stderr)
            artifact = release / "nightfalcon-3.0.0.pyz"
            formula = base / "nightfalcon.rb"
            render = subprocess.run(
                [
                    sys.executable,
                    "scripts/release/render-homebrew-formula.py",
                    "--artifact",
                    str(artifact),
                    "--output",
                    str(formula),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(render.returncode, 0, render.stderr)
            text = formula.read_text()
            self.assertIn(hashlib.sha256(artifact.read_bytes()).hexdigest(), text)
            self.assertIn('depends_on "python@3.11"', text)
            self.assertIn("nightfalcon-3.0.0.pyz", text)
            self.assertIn('system "#{bin}/nightfalcon", "version"', text)
            self.assertNotIn("@SHA256@", text)
            if shutil.which("ruby"):
                parsed = subprocess.run(
                    ["ruby", "-c", str(formula)], text=True, capture_output=True, check=False
                )
                self.assertEqual(parsed.returncode, 0, parsed.stderr)

    def test_debian_metadata_installs_offline_zipapp_with_exact_name(self) -> None:
        control = (ROOT / "packaging/debian/control").read_text()
        rules = (ROOT / "packaging/debian/rules").read_text()
        wrapper = (ROOT / "packaging/debian/nightfalcon").read_text()
        changelog = (ROOT / "packaging/debian/changelog").read_text()

        self.assertIn("Source: nightfalcon", control)
        self.assertIn("Package: nightfalcon", control)
        self.assertIn("Architecture: all", control)
        self.assertIn("Depends: python3 (>= 3.11)", control)
        self.assertIn("build-zipapp.py", rules)
        self.assertIn("/usr/lib/nightfalcon/nightfalcon.pyz", wrapper)
        self.assertIn("nightfalcon (3.0.0-1)", changelog)
        combined = "\n".join((control, rules, wrapper))
        self.assertNotIn("curl ", combined)
        self.assertNotIn("wget ", combined)

    def test_debian_builder_stages_canonical_source_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary) / "source"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/release/build-debian-package.py",
                    "--repo",
                    str(ROOT),
                    "--stage-dir",
                    str(stage),
                    "--stage-only",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((stage / "debian/control").is_file())
            self.assertTrue((stage / "debian/rules").is_file())
            self.assertTrue((stage / "scripts/release/build-zipapp.py").is_file())
            self.assertTrue((stage / "claude").is_dir())
            self.assertTrue((stage / "codex").is_dir())
            self.assertTrue((stage / "cursor").is_dir())
            release = Path(temporary) / "release"
            build = subprocess.run(
                [
                    sys.executable,
                    "scripts/release/build-zipapp.py",
                    "--repo",
                    str(stage),
                    "--output-dir",
                    str(release),
                ],
                cwd=stage,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(build.returncode, 0, build.stderr)
            self.assertTrue((release / "nightfalcon-3.0.0.pyz").is_file())


if __name__ == "__main__":
    unittest.main()
