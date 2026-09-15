from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from nightfalcon.filesystem import (
    InstallSafetyError,
    atomic_activate,
    safe_remove_receipt_paths,
    validate_destination,
)
from nightfalcon.platforms import data_root
from nightfalcon.receipts import InstallReceipt, ReceiptError, load_receipt, write_receipt


class PlatformRootTests(unittest.TestCase):
    def test_platform_roots_use_documented_environment(self) -> None:
        self.assertEqual(
            data_root({"HOME": "/Users/test"}, "darwin"),
            Path("/Users/test/Library/Application Support/NightFalcon"),
        )
        self.assertEqual(
            data_root({"HOME": "/home/test"}, "linux"),
            Path("/home/test/.local/share/nightfalcon"),
        )
        self.assertEqual(
            data_root(
                {"HOME": "/home/test", "XDG_DATA_HOME": "/data/test"}, "linux"
            ),
            Path("/data/test/nightfalcon"),
        )
        self.assertEqual(
            data_root({"LOCALAPPDATA": r"C:\Users\test\AppData\Local"}, "win32"),
            Path(r"C:\Users\test\AppData\Local") / "NightFalcon",
        )

    def test_platform_root_requires_absolute_owner_directory(self) -> None:
        with self.assertRaises(ValueError):
            data_root({"HOME": "relative"}, "linux")
        with self.assertRaises(ValueError):
            data_root({}, "win32")


class ReceiptTests(unittest.TestCase):
    def test_receipt_round_trip_is_canonical_and_strict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipt.json"
            receipt = InstallReceipt(
                version="3.0.0",
                client="codex",
                destination=str(Path(temporary) / "versions/3.0.0/codex"),
                manifest_sha256="a" * 64,
                paths=("plugin.json", "skills/nightfalcon/SKILL.md"),
                created_at="2026-09-02T00:00:00Z",
            )

            write_receipt(path, receipt)

            self.assertEqual(load_receipt(path), receipt)
            raw = path.read_bytes()
            self.assertTrue(raw.endswith(b"\n"))
            self.assertEqual(raw, receipt.to_json().encode())

            payload = json.loads(raw)
            payload["unexpected"] = True
            path.write_text(json.dumps(payload))
            with self.assertRaises(ReceiptError):
                load_receipt(path)

    def test_receipt_rejects_traversal_duplicate_paths_and_bad_digest(self) -> None:
        for paths, digest in [
            (("../escape",), "a" * 64),
            (("same", "same"), "a" * 64),
            (("safe",), "not-a-digest"),
        ]:
            with self.subTest(paths=paths, digest=digest):
                with self.assertRaises(ReceiptError):
                    InstallReceipt(
                        version="3.0.0",
                        client="claude",
                        destination="/tmp/nightfalcon/claude",
                        manifest_sha256=digest,
                        paths=paths,
                        created_at="2026-09-02T00:00:00Z",
                    )


class FilesystemSafetyTests(unittest.TestCase):
    def test_destination_must_be_strict_descendant_without_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "owned"
            root.mkdir()
            outside = base / "outside"
            outside.mkdir()

            self.assertEqual(
                validate_destination(root / "versions/3.0.0", root),
                (root / "versions/3.0.0").resolve(strict=False),
            )
            for destination in (root, outside, root / "../outside"):
                with self.subTest(destination=destination):
                    with self.assertRaises(InstallSafetyError):
                        validate_destination(destination, root)

            (root / "link").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(InstallSafetyError):
                validate_destination(root / "link/escape", root)

            (root / "real").mkdir()
            (root / "internal-link").symlink_to(root / "real", target_is_directory=True)
            with self.assertRaises(InstallSafetyError):
                validate_destination(root / "internal-link/file", root)

    def test_atomic_activate_preserves_current_destination_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staged = root / "staged"
            destination = root / "active"
            staged.mkdir()
            destination.mkdir()
            (staged / "value").write_text("new")
            (destination / "value").write_text("old")

            with self.assertRaises(InstallSafetyError):
                atomic_activate(staged, destination)

            self.assertEqual((destination / "value").read_text(), "old")
            self.assertEqual((staged / "value").read_text(), "new")

    def test_uninstall_removes_only_receipt_owned_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "owned"
            destination = root / "versions/3.0.0/claude"
            destination.mkdir(parents=True)
            owned = destination / "plugin.json"
            retained = destination / "user.txt"
            owned.write_text("owned")
            retained.write_text("user")
            receipt = InstallReceipt(
                version="3.0.0",
                client="claude",
                destination=str(destination),
                manifest_sha256="b" * 64,
                paths=("plugin.json",),
                created_at="2026-09-02T00:00:00Z",
            )

            removed = safe_remove_receipt_paths(receipt, root)

            self.assertEqual(removed, [owned.resolve(strict=False)])
            self.assertFalse(owned.exists())
            self.assertTrue(retained.exists())

            outside_receipt = InstallReceipt(
                version="3.0.0",
                client="claude",
                destination=str(Path(temporary) / "outside"),
                manifest_sha256="b" * 64,
                paths=("file",),
                created_at="2026-09-02T00:00:00Z",
            )
            with self.assertRaises(InstallSafetyError):
                safe_remove_receipt_paths(outside_receipt, root)


if __name__ == "__main__":
    unittest.main()
