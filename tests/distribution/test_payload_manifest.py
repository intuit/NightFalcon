from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from nightfalcon.payloads import PayloadError, PayloadManifest


ROOT = Path(__file__).resolve().parents[2]


class PayloadManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = PayloadManifest.build(ROOT)

    def test_manifest_is_sorted_stable_and_contains_only_runtime_roots(self) -> None:
        first = self.manifest.to_json()
        second = PayloadManifest.build(ROOT).to_json()

        self.assertEqual(first, second)
        paths = [entry.path for entry in self.manifest.files]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual({entry.client for entry in self.manifest.files}, {"claude", "codex", "cursor"})
        self.assertTrue(all(path.split("/", 1)[0] in {"claude", "codex", "cursor"} for path in paths))
        self.assertTrue(all("output" not in Path(path).parts for path in paths))
        self.assertTrue(all("__pycache__" not in Path(path).parts for path in paths))
        self.assertTrue(all(not path.endswith((".pyc", ".DS_Store")) for path in paths))
        self.assertRegex(self.manifest.sha256, r"^[0-9a-f]{64}$")

    def test_manifest_verification_detects_tampering_and_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            staged = Path(temporary)
            for entry in self.manifest.files:
                source = ROOT / entry.path
                destination = staged / entry.path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)

            self.assertEqual(self.manifest.verify(staged), [])
            target = staged / self.manifest.files[0].path
            target.write_bytes(target.read_bytes() + b"tampered")
            errors = self.manifest.verify(staged)
            self.assertTrue(any("sha256 mismatch" in error for error in errors), errors)
            target.unlink()
            errors = self.manifest.verify(staged)
            self.assertTrue(any("missing" in error for error in errors), errors)

    def test_manifest_rejects_tracked_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "claude").mkdir()
            (repo / "claude/target").write_text("target")
            (repo / "claude/link").symlink_to("target")
            subprocess.run(["git", "add", "claude"], cwd=repo, check=True)

            with self.assertRaises(PayloadError):
                PayloadManifest.build(repo)

    def test_source_archive_without_git_metadata_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            for client in ("claude", "codex", "cursor"):
                (source / client).mkdir()
                (source / client / "runtime.txt").write_text(client)

            manifest = PayloadManifest.build(source)

            self.assertEqual(
                [entry.path for entry in manifest.files],
                ["claude/runtime.txt", "codex/runtime.txt", "cursor/runtime.txt"],
            )

    def test_builder_cli_writes_exact_canonical_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "payload-manifest.json"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/release/build-payload-manifest.py",
                    "--repo",
                    str(ROOT),
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_text(), self.manifest.to_json())
            self.assertEqual(json.loads(result.stdout)["sha256"], self.manifest.sha256)


if __name__ == "__main__":
    unittest.main()
