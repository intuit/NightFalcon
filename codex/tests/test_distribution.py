import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "nightfalcon"


class DistributionTests(unittest.TestCase):
    def test_manifest_and_folder_name_match(self):
        manifest = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text())
        self.assertEqual(PLUGIN.name, "nightfalcon")
        self.assertEqual(manifest["name"], PLUGIN.name)
        self.assertRegex(manifest["version"], r"^3\.0\.0\+codex\.20260902\d{6}$")
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertNotIn("hooks", manifest)

    def test_manifest_attributes_plugin_to_contributors(self):
        manifest = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["author"], {
            "name": "NightFalcon contributors",
            "url": "https://github.com/intuit/nightfalcon",
        })
        self.assertEqual(
            manifest["interface"]["developerName"], "NightFalcon contributors"
        )

    def test_marketplace_points_to_plugin(self):
        market = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json").read_text())
        self.assertEqual(market["name"], "nightfalcon-open")
        entry = market["plugins"][0]
        self.assertEqual(entry["name"], "nightfalcon")
        self.assertEqual(entry["source"]["path"], "./plugins/nightfalcon")
        self.assertEqual(entry["policy"], {
            "installation": "AVAILABLE",
            "authentication": "ON_INSTALL",
        })
        self.assertEqual(entry["category"], "Developer Tools")

    def test_required_agent_depth_template(self):
        text = (ROOT / "config" / "codex-config.toml").read_text()
        self.assertIn("[agents]", text)
        self.assertIn("max_depth = 5", text)
        self.assertIn("max_threads = 8", text)


if __name__ == "__main__":
    unittest.main()
