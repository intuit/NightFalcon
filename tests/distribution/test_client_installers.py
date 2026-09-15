from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from nightfalcon.clients import ClaudeAdapter, CodexAdapter, CursorAdapter
from nightfalcon.clients.base import ClientInstallError


class ClientInstallerTests(unittest.TestCase):
    def _fixture(self, base: Path, client: str) -> Path:
        source = base / "payload" / client
        (source / "nested").mkdir(parents=True)
        (source / "plugin.json").write_text(json.dumps({"name": "nightfalcon"}))
        (source / "nested/runtime.py").write_text("print('nightfalcon')\n")
        return source

    def test_detection_uses_client_executable_lookup_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"
            calls: list[str] = []

            def locate(name: str) -> str | None:
                calls.append(name)
                return f"/tools/{name}" if name != "cursor" else None

            adapters = [
                ClaudeAdapter(root=root, which=locate),
                CodexAdapter(root=root, which=locate),
                CursorAdapter(root=root, which=locate),
            ]
            detections = [adapter.detect() for adapter in adapters]

            self.assertEqual(calls, ["claude", "codex", "cursor"])
            self.assertEqual([item.available for item in detections], [True, True, False])
            self.assertFalse(root.exists())

    def test_each_adapter_installs_verifies_and_uninstalls_receipt_owned_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "data"
            root.mkdir()
            for adapter_type, client in [
                (ClaudeAdapter, "claude"),
                (CodexAdapter, "codex"),
                (CursorAdapter, "cursor"),
            ]:
                with self.subTest(client=client):
                    source = self._fixture(base / client, client)
                    destination = root / "versions" / "3.0.0" / client
                    adapter = adapter_type(root=root, which=lambda _: None)
                    plan = adapter.plan(source, destination)

                    self.assertEqual(plan.client, client)
                    self.assertEqual(plan.conflicts, ())
                    receipt = adapter.install(plan)

                    self.assertEqual(receipt.client, client)
                    self.assertTrue((destination / "nested/runtime.py").is_file())
                    self.assertEqual(adapter.verify(receipt), [])
                    (destination / "nested/runtime.py").write_text("tampered")
                    self.assertTrue(any("mismatch" in item for item in adapter.verify(receipt)))
                    (destination / "nested/runtime.py").write_text("print('nightfalcon')\n")
                    removed = adapter.uninstall(receipt)
                    self.assertEqual(len(removed), 2)
                    self.assertFalse(destination.exists())

    def test_conflict_refuses_without_force_and_force_requires_owned_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "data"
            root.mkdir()
            source = self._fixture(base, "claude")
            destination = root / "versions/3.0.0/claude"
            destination.mkdir(parents=True)
            (destination / "user-file").write_text("mine")
            adapter = ClaudeAdapter(root=root, which=lambda _: None)
            plan = adapter.plan(source, destination)

            self.assertTrue(plan.conflicts)
            with self.assertRaises(ClientInstallError):
                adapter.install(plan)
            with self.assertRaises(ClientInstallError):
                adapter.install(plan, force=True)
            self.assertEqual((destination / "user-file").read_text(), "mine")

    def test_force_replaces_only_verified_receipt_owned_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "data"
            root.mkdir()
            source = self._fixture(base, "codex")
            destination = root / "versions/3.0.0/codex"
            adapter = CodexAdapter(root=root, which=lambda _: None)
            receipt = adapter.install(adapter.plan(source, destination))
            (source / "nested/runtime.py").write_text("print('updated')\n")

            updated = adapter.install(adapter.plan(source, destination), force=True)

            self.assertNotEqual(updated.manifest_sha256, receipt.manifest_sha256)
            self.assertEqual((destination / "nested/runtime.py").read_text(), "print('updated')\n")
            self.assertEqual(adapter.verify(updated), [])

    def test_failed_receipt_write_restores_previous_active_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "data"
            root.mkdir()
            source = self._fixture(base, "codex")
            destination = root / "versions/3.0.0/codex"
            adapter = CodexAdapter(root=root, which=lambda _: None)
            original = adapter.install(adapter.plan(source, destination))
            original_bytes = (destination / "nested/runtime.py").read_bytes()
            (source / "nested/runtime.py").write_text("print('updated')\n")

            with mock.patch(
                "nightfalcon.clients.common.write_receipt",
                side_effect=OSError("disk full"),
            ):
                with self.assertRaises(ClientInstallError):
                    adapter.install(adapter.plan(source, destination), force=True)

            self.assertEqual((destination / "nested/runtime.py").read_bytes(), original_bytes)
            self.assertEqual(adapter.verify(original), [])

    def test_client_instructions_are_exact_and_cursor_requires_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "data"
            root.mkdir()
            claude_source = self._fixture(base / "claude", "claude")
            codex_source = self._fixture(base / "codex", "codex")
            cursor_source = self._fixture(base / "cursor", "cursor")

            claude = ClaudeAdapter(root=root, which=lambda _: None).plan(
                claude_source, root / "versions/3.0.0/claude"
            )
            codex = CodexAdapter(root=root, which=lambda _: None).plan(
                codex_source, root / "versions/3.0.0/codex"
            )

            self.assertTrue(any("nightfalcon-local" in item for item in claude.instructions))
            self.assertTrue(any("nightfalcon-open" in item for item in codex.instructions))
            with self.assertRaises(ClientInstallError):
                CursorAdapter(root=root, which=lambda _: None).plan(cursor_source, Path("."))

    def test_codex_marketplace_manifest_uses_open_source_identity(self) -> None:
        root = Path(__file__).resolve().parents[2]
        marketplace = json.loads((root / "codex/.agents/plugins/marketplace.json").read_text())
        self.assertEqual(marketplace["name"], "nightfalcon-open")
        self.assertEqual(marketplace["interface"]["displayName"], "NightFalcon Open Source")


if __name__ == "__main__":
    unittest.main()
