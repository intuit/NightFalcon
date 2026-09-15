from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from nightfalcon.platforms import data_root


ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / "scripts/release/build-zipapp.py"
VERIFIER = ROOT / "scripts/release/verify-release.py"


class ReleaseBuildTests(unittest.TestCase):
    def _build(self, output: Path) -> Path:
        result = subprocess.run(
            [sys.executable, str(BUILDER), "--repo", str(ROOT), "--output-dir", str(output)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return output / "nightfalcon-3.0.0.pyz"

    def test_two_builds_are_byte_identical_with_normalized_zip_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = self._build(base / "first")
            second = self._build(base / "second")

            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                entries = archive.infolist()
                self.assertEqual([entry.filename for entry in entries], sorted(entry.filename for entry in entries))
                self.assertTrue(all(entry.date_time == (1980, 1, 1, 0, 0, 0) for entry in entries))
                self.assertTrue(all((entry.external_attr >> 16) & 0o777 in {0o644, 0o755} for entry in entries))
                self.assertIn("nightfalcon/_payload-manifest.json", archive.namelist())
                self.assertIn("nightfalcon/_payloads/claude/.claude-plugin/plugin.json", archive.namelist())

    def test_release_verifier_accepts_build_and_rejects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "release"
            artifact = self._build(output)

            valid = subprocess.run(
                [sys.executable, str(VERIFIER), "--release-dir", str(output)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)
            manifest_path = output / "release-manifest.json"
            original_manifest = manifest_path.read_text()
            manifest = json.loads(original_manifest)
            manifest["payload_manifest_sha256"] = "0" * 64
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
            )
            false_identity = subprocess.run(
                [sys.executable, str(VERIFIER), "--release-dir", str(output)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(false_identity.returncode, 0)
            self.assertIn("payload manifest digest mismatch", false_identity.stderr)
            manifest_path.write_text(original_manifest)
            unexpected = output / "unexpected.bin"
            unexpected.write_bytes(b"not declared")
            extra = subprocess.run(
                [sys.executable, str(VERIFIER), "--release-dir", str(output)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(extra.returncode, 0)
            self.assertIn("inventory mismatch", extra.stderr)
            unexpected.unlink()
            artifact.write_bytes(artifact.read_bytes() + b"tampered")
            invalid = subprocess.run(
                [sys.executable, str(VERIFIER), "--release-dir", str(output)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("checksum mismatch", invalid.stderr)

    def test_zipapp_runs_and_installs_embedded_payload_offline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            artifact = self._build(base / "release")
            home = base / "home"
            home.mkdir()
            env = {
                "HOME": str(home),
                "LOCALAPPDATA": str(home / "AppData/Local"),
                "PATH": os.environ.get("PATH", ""),
            }

            version = subprocess.run(
                [sys.executable, str(artifact), "version"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            installed = subprocess.run(
                [sys.executable, str(artifact), "install", "claude", "--yes"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertEqual(version.stdout, "nightfalcon 3.0.0\n")
            self.assertEqual(installed.returncode, 0, installed.stderr)
            destination = data_root(env, sys.platform) / "versions/3.0.0/claude"
            self.assertTrue((destination / ".claude-plugin/plugin.json").is_file())

    def test_release_contains_checksum_manifest_and_spdx_sbom(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "release"
            artifact = self._build(output)
            checksum = (output / "nightfalcon-3.0.0.pyz.sha256").read_text().strip()
            expected = hashlib.sha256(artifact.read_bytes()).hexdigest()
            release = json.loads((output / "release-manifest.json").read_text())
            sbom = json.loads((output / "nightfalcon-3.0.0.spdx.json").read_text())

            self.assertEqual(checksum, f"{expected}  {artifact.name}")
            artifacts = {item["name"]: item for item in release["artifacts"]}
            self.assertEqual(artifacts[artifact.name]["sha256"], expected)
            self.assertEqual(
                set(artifacts),
                {path.name for path in output.iterdir() if path.name != "release-manifest.json"},
            )
            self.assertEqual(release["schema_version"], 2)
            self.assertEqual(release["version"], "3.0.0")
            self.assertEqual(sbom["spdxVersion"], "SPDX-2.3")
            self.assertEqual(sbom["packages"][0]["versionInfo"], "3.0.0")
            self.assertGreater(len(sbom["files"]), 100)


if __name__ == "__main__":
    unittest.main()
