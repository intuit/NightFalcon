from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
import venv
import zipfile


ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / "scripts/release/build-python-package.py"


class PypiPackageTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("build"), "python build package is not installed")
    def test_wheel_and_sdist_include_payload_and_install_offline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            output = base / "dist"
            built = subprocess.run(
                [sys.executable, str(BUILDER), "--repo", str(ROOT), "--output-dir", str(output)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            wheel = output / "nightfalcon-3.0.0-py3-none-any.whl"
            sdist = output / "nightfalcon-3.0.0.tar.gz"
            self.assertTrue(wheel.is_file())
            self.assertTrue(sdist.is_file())

            with zipfile.ZipFile(wheel) as archive:
                names = archive.namelist()
                self.assertIn("nightfalcon/_payload-manifest.json", names)
                self.assertIn(
                    "nightfalcon/_payloads/codex/plugins/nightfalcon/.codex-plugin/plugin.json",
                    names,
                )
            with tarfile.open(sdist, "r:gz") as archive:
                names = archive.getnames()
                self.assertTrue(
                    any(name.endswith("src/nightfalcon/_payload-manifest.json") for name in names)
                )
                self.assertTrue(
                    any(name.endswith("src/nightfalcon/_payloads/cursor/VERSION") for name in names)
                )

            environment = base / "venv"
            venv.EnvBuilder(with_pip=True).create(environment)
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            command = environment / ("Scripts/nightfalcon.exe" if os.name == "nt" else "bin/nightfalcon")
            installed = subprocess.run(
                [str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            version = subprocess.run(
                [str(command), "version"], text=True, capture_output=True, check=False
            )
            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertEqual(version.stdout, "nightfalcon 3.0.0\n")

            home = base / "home"
            home.mkdir()
            env = dict(os.environ)
            env["HOME"] = str(home)
            env["LOCALAPPDATA"] = str(home / "AppData/Local")
            payload_install = subprocess.run(
                [str(command), "install", "codex", "--yes"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(payload_install.returncode, 0, payload_install.stderr)


if __name__ == "__main__":
    unittest.main()
