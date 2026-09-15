from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from nightfalcon.cli import main


class CliTests(unittest.TestCase):
    def _env(self, home: Path) -> dict[str, str]:
        return {"HOME": str(home), "PATH": "/usr/bin"}

    def test_detect_json_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            stdout = StringIO()

            result = main(
                ["detect", "--json"],
                env=self._env(home),
                platform="linux",
                which=lambda name: f"/tools/{name}" if name != "cursor" else None,
                stdout=stdout,
                stderr=StringIO(),
            )

            self.assertEqual(result, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(
                {item["client"]: item["available"] for item in payload["clients"]},
                {"claude": True, "codex": True, "cursor": False},
            )
            self.assertFalse((home / ".local/share/nightfalcon").exists())

    def test_no_argument_noninteractive_invocation_never_mutates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            stderr = StringIO()

            result = main(
                [],
                env=self._env(home),
                platform="linux",
                which=lambda _: "/tools/client",
                stdin_isatty=False,
                stdout=StringIO(),
                stderr=stderr,
            )

            self.assertEqual(result, 2)
            self.assertIn("non-interactive", stderr.getvalue())
            self.assertFalse((home / ".local/share/nightfalcon").exists())

    def test_declined_interactive_install_never_mutates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            prompts: list[str] = []

            result = main(
                [],
                env=self._env(home),
                platform="linux",
                which=lambda name: "/tools/claude" if name == "claude" else None,
                stdin_isatty=True,
                prompt=lambda message: prompts.append(message) or "n",
                stdout=StringIO(),
                stderr=StringIO(),
            )

            self.assertEqual(result, 0)
            self.assertEqual(len(prompts), 1)
            self.assertFalse((home / ".local/share/nightfalcon").exists())

    def test_explicit_install_and_doctor_use_managed_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            env = self._env(home)
            stdout = StringIO()

            installed = main(
                ["install", "claude", "--yes"],
                env=env,
                platform="linux",
                which=lambda _: None,
                stdout=stdout,
                stderr=StringIO(),
            )

            destination = home / ".local/share/nightfalcon/versions/3.0.0/claude"
            self.assertEqual(installed, 0)
            self.assertTrue((destination / ".claude-plugin/plugin.json").is_file())
            self.assertIn("/plugin install nightfalcon@nightfalcon-local", stdout.getvalue())

            doctor_output = StringIO()
            healthy = main(
                ["doctor", "--json"],
                env=env,
                platform="linux",
                which=lambda _: None,
                stdout=doctor_output,
                stderr=StringIO(),
            )
            report = json.loads(doctor_output.getvalue())
            self.assertEqual(healthy, 0)
            self.assertEqual(report["installations"][0]["client"], "claude")
            self.assertEqual(report["installations"][0]["status"], "ok")

            (destination / ".claude-plugin/plugin.json").write_text("tampered")
            unhealthy_output = StringIO()
            unhealthy = main(
                ["doctor", "--json"],
                env=env,
                platform="linux",
                which=lambda _: None,
                stdout=unhealthy_output,
                stderr=StringIO(),
            )
            self.assertEqual(unhealthy, 1)
            self.assertEqual(
                json.loads(unhealthy_output.getvalue())["installations"][0]["status"],
                "error",
            )

    def test_cli_uninstall_removes_verified_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            env = self._env(home)
            destination = home / ".local/share/nightfalcon/versions/3.0.0/codex"
            self.assertEqual(
                main(
                    ["install", "codex", "--yes"],
                    env=env,
                    platform="linux",
                    which=lambda _: None,
                    stdout=StringIO(),
                    stderr=StringIO(),
                ),
                0,
            )

            removed = main(
                ["uninstall", "codex", "--yes"],
                env=env,
                platform="linux",
                which=lambda _: None,
                stdout=StringIO(),
                stderr=StringIO(),
            )

            self.assertEqual(removed, 0)
            self.assertFalse(destination.exists())

    def test_install_requires_yes_when_not_interactive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            result = main(
                ["install", "codex"],
                env=self._env(home),
                platform="linux",
                which=lambda _: None,
                stdin_isatty=False,
                stdout=StringIO(),
                stderr=StringIO(),
            )

            self.assertEqual(result, 2)
            self.assertFalse((home / ".local/share/nightfalcon").exists())


if __name__ == "__main__":
    unittest.main()
