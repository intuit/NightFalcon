from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class PublicInstallDocumentationTests(unittest.TestCase):
    def test_readme_documents_client_installation_commands(self) -> None:
        readme = (ROOT / "README.md").read_text()
        for command in (
            "/plugin marketplace add ~/NightFalcon/claude",
            "/plugin install nightfalcon@nightfalcon-local",
            "codex plugin marketplace add ~/NightFalcon/codex",
            "codex plugin add nightfalcon@nightfalcon-open",
            "cp -R ~/NightFalcon/cursor/.cursor",
        ):
            self.assertIn(command, readme)
        for unsupported_command in (
            "pip install nightfalcon",
            "brew install nightfalcon",
            "npm install -g nightfalcon",
        ):
            self.assertNotIn(unsupported_command, readme)

    def test_readme_documents_usage_outputs_and_operational_guidance(self) -> None:
        """Require the public README to explain running and interpreting reviews."""
        readme = (ROOT / "README.md").read_text()
        for expected_text in (
            "mkdir ~/nightfalcon-review",
            "sourcecode/",
            "output/proof_of_concept/",
            "## Output",
            "True positives",
            "Not reachable in practice",
            "False positives",
            "executive-report-<DATE>.html",
            "Model refusals",
            "three or four times",
            "cross-validate findings",
        ):
            self.assertIn(expected_text, readme)

    def test_public_install_guides_have_no_legacy_repository_or_marketplace(self) -> None:
        paths = [
            ROOT / "README.md",
            ROOT / "claude/README.md",
            ROOT / "codex/README.md",
            ROOT / "cursor/README.md",
            ROOT / "docs/distribution.md",
        ]
        combined = "\n".join(path.read_text() for path in paths)
        self.assertNotIn("github.com/averma5/NightFalcon", combined)
        self.assertNotIn("nightfalcon@personal", combined)
        self.assertNotIn("marketplace remove personal", combined)
        self.assertIn("nightfalcon@nightfalcon-open", combined)

    def test_security_policy_covers_distribution_compromise(self) -> None:
        security = (ROOT / "SECURITY.md").read_text().casefold()
        for term in ("package", "checksum", "provenance", "compromise"):
            self.assertIn(term, security)

    def test_cursor_local_session_metadata_is_ignored(self) -> None:
        ignored = (ROOT / ".gitignore").read_text().splitlines()
        self.assertIn(".terragraph/", ignored)


if __name__ == "__main__":
    unittest.main()
