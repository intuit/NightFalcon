import hashlib
import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "nightfalcon"
SOURCE_REVISION = "0c3183e02ec1f1500115233402cabdf7808ec4e6"


def inventory():
    return json.loads((ROOT / "migration-map.json").read_text())


def destination_files():
    return {
        path.relative_to(PLUGIN).as_posix()
        for path in PLUGIN.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


class InventoryTests(unittest.TestCase):
    def test_inventory_binds_completed_source_and_balances_exclusions(self):
        payload = inventory()
        self.assertEqual(payload["schema_version"], "2")
        self.assertEqual(payload["source_revision"], SOURCE_REVISION)
        self.assertEqual(
            payload["source_runtime_file_count"],
            payload["source_included_file_count"] + payload["source_excluded_file_count"],
        )
        self.assertGreater(payload["source_excluded_file_count"], 0)
        self.assertRegex(payload["source_excluded_sha256"], r"^[0-9a-f]{64}$")

    def test_every_public_destination_is_covered_and_hash_bound(self):
        payload = inventory()
        covered = {item["destination"] for item in payload["files"]}
        self.assertEqual(covered, destination_files())
        self.assertEqual(payload["destination_file_count"], len(covered))
        for item in payload["files"]:
            actual = hashlib.sha256((PLUGIN / item["destination"]).read_bytes()).hexdigest()
            self.assertEqual(actual, item["destination_sha256"], item["destination"])

    def test_source_mappings_are_not_self_generated_public_hashes(self):
        payload = inventory()
        adapted = [item for item in payload["files"] if item["classification"] == "ADAPTED"]
        unchanged = [item for item in payload["files"] if item["classification"] == "UNCHANGED"]
        additions = [item for item in payload["files"] if item["classification"] == "PUBLIC_ADDITION"]
        self.assertTrue(adapted)
        self.assertTrue(unchanged)
        self.assertTrue(additions)
        self.assertTrue(all(item["sha256"] != item["destination_sha256"] for item in adapted))
        self.assertTrue(all(item["sha256"] == item["destination_sha256"] for item in unchanged))
        self.assertTrue(all(item["source"] is None and item["sha256"] is None for item in additions))
        sources = [item["source"] for item in payload["files"] if item["source"] is not None]
        self.assertEqual(len(sources), len(set(sources)))

    def test_frozen_inventory_validation_is_stable(self):
        builder = ROOT / "scripts" / "build-port-inventory.py"
        before = (ROOT / "migration-map.json").read_bytes()
        first = subprocess.run(["python3", str(builder)], text=True, capture_output=True, check=False)
        second = subprocess.run(["python3", str(builder)], text=True, capture_output=True, check=False)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual((ROOT / "migration-map.json").read_bytes(), before)

    def test_standalone_builder_rejects_wrong_source_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            wrapper = base / "codex"
            scripts = wrapper / "scripts"
            plugin = wrapper / "plugins" / "nightfalcon"
            scripts.mkdir(parents=True)
            plugin.mkdir(parents=True)
            shutil.copy2(ROOT / "scripts" / "build-port-inventory.py", scripts / "build-port-inventory.py")
            (wrapper / "migration-map.json").write_text(json.dumps({
                "schema_version": "2",
                "source_revision": "a" * 40,
                "source_runtime_file_count": 0,
                "source_included_file_count": 0,
                "source_excluded_file_count": 0,
                "source_excluded_sha256": hashlib.sha256().hexdigest(),
                "destination_file_count": 0,
                "files": [],
            }))
            result = subprocess.run(
                ["python3", str(scripts / "build-port-inventory.py")],
                text=True, capture_output=True, check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("completed source revision", result.stderr)


if __name__ == "__main__":
    unittest.main()
