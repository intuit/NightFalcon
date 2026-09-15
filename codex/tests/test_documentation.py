import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parent if (ROOT.parent / "README.md").is_file() else ROOT


class DocumentationTests(unittest.TestCase):
    def test_wrapper_readme_has_portable_install_and_runtime_contract(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("/path/to/NightFalcon-OpenSource/codex", readme)
        self.assertNotIn("/" + "Users/", readme)
        self.assertIn("max_depth = 5", readme)
        self.assertIn("max_threads = 8", readme)
        self.assertIn("user-selected", readme)
        self.assertIn("state.json.current_phase == \"done\"", readme)

    def test_public_repository_docs_cover_security_license_and_support(self):
        required = ("README.md", "LICENSE", "NOTICE")
        if REPOSITORY != ROOT:
            required += ("SECURITY.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SUPPORT.md")
        for name in required:
            self.assertTrue((REPOSITORY / name).is_file(), name)
        notice = (REPOSITORY / "NOTICE").read_text()
        self.assertIn("Apache License 2.0", notice)
        self.assertIn("CC BY-SA", notice)

    def test_public_migration_inventory_is_bound_and_private_free(self):
        inventory = json.loads((ROOT / "migration-map.json").read_text())
        self.assertRegex(inventory["source_revision"], r"^[0-9a-f]{40}$")
        self.assertTrue(inventory["files"])
        corpus = json.dumps(inventory).lower()
        former_org = "in" + "tuit"
        self.assertNotIn(former_org, corpus)
        self.assertNotIn("/users/", corpus)

    def test_plugin_readme_states_fresh_live_validation_boundary(self):
        readme = (ROOT / "plugins/nightfalcon/README.md").read_text()
        self.assertIn("fresh live validation", readme.lower())
        self.assertIn('state.json.current_phase == "done"', readme)


if __name__ == "__main__":
    unittest.main()
