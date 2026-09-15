import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
REINSTALL_PATH = ROOT / "scripts" / "reinstall-local-plugin.py"
README_PATH = ROOT / "README.md"


def load_module():
    spec = importlib.util.spec_from_file_location(
        "nightfalcon_reinstall_local_plugin", REINSTALL_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakePluginAdd:
    def __init__(self, source, cache_root, version, *, mutate=None):
        self.source = source
        self.cache_root = cache_root
        self.version = version
        self.mutate = mutate
        self.calls = []
        self.environments = []

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        self.environments.append(dict(kwargs.get("env") or {}))
        installed = self.cache_root / self.version
        if not installed.exists():
            installed.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(self.source, installed)
            if self.mutate:
                self.mutate(installed)
        return subprocess.CompletedProcess(command, 0, stdout="installed\n", stderr="")


class FailedPluginAdd:
    def __call__(self, command, **kwargs):
        return subprocess.CompletedProcess(command, 9, stdout="", stderr="failed\n")


class ReinstallLocalPluginTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.temporary.name).resolve()
        self.wrapper = self.base / "checkout" / "NightFalcon-Codex"
        self.source = self.wrapper / "plugins" / "nightfalcon"
        self.codex_home = self.base / "codex-home"
        self.marketplace = "fixture-market"
        self.plugin_name = "nightfalcon"
        self.version = "2.0.0+codex.test"
        self.cache_root = (
            self.codex_home
            / "plugins"
            / "cache"
            / self.marketplace
            / self.plugin_name
        )
        (self.wrapper / ".agents" / "plugins").mkdir(parents=True)
        (self.source / ".codex-plugin").mkdir(parents=True)
        (self.source / "hooks").mkdir()
        (self.source / "scripts").mkdir()
        (self.wrapper / ".agents" / "plugins" / "marketplace.json").write_text(
            json.dumps(
                {
                    "name": self.marketplace,
                    "plugins": [
                        {
                            "name": self.plugin_name,
                            "source": {
                                "source": "local",
                                "path": "./plugins/nightfalcon",
                            },
                        }
                    ],
                }
            )
        )
        (self.source / ".codex-plugin" / "plugin.json").write_text(
            json.dumps({"name": self.plugin_name, "version": self.version})
        )
        (self.source / "hooks" / "hooks.json").write_text('{"hooks": {}}\n')
        (self.source / "hooks" / "nightfalcon_hook.py").write_text(
            "HOOK_BYTES = 'validated'\n"
        )
        executable = self.source / "scripts" / "gate.sh"
        executable.write_text("#!/usr/bin/env bash\nexit 0\n")
        executable.chmod(0o755)
        (self.source / "README.md").write_text("fixture plugin\n")

    def tearDown(self):
        self.temporary.cleanup()

    def reinstall(self, module, installer, *, run_id="test-run"):
        return module.repair_install(
            wrapper=self.wrapper,
            codex_home=self.codex_home,
            codex_bin="codex-fixture",
            run_command=installer,
            run_id=run_id,
        )

    def tree_snapshot(self, root):
        root = pathlib.Path(root)
        snapshot = []
        if not root.exists():
            return snapshot
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                snapshot.append((relative, "symlink", os.readlink(path)))
            elif path.is_file():
                snapshot.append((relative, "file", path.read_bytes()))
            elif path.is_dir():
                snapshot.append((relative, "directory", None))
        return snapshot

    def test_missing_cache_installs_with_dynamic_selector_and_records_audit(self):
        self.assertTrue(REINSTALL_PATH.is_file())
        module = load_module()
        installer = FakePluginAdd(self.source, self.cache_root, self.version)
        marketplace_path = self.wrapper / ".agents" / "plugins" / "marketplace.json"
        marketplace_before = marketplace_path.read_bytes()

        result = self.reinstall(module, installer)

        self.assertEqual(
            installer.calls,
            [["codex-fixture", "plugin", "add", f"{self.plugin_name}@{self.marketplace}"]],
        )
        self.assertEqual(installer.environments[0]["CODEX_HOME"], str(self.codex_home))
        self.assertEqual(pathlib.Path(result["installed_root"]), self.cache_root / self.version)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["aliases"], [])
        audit = json.loads(pathlib.Path(result["audit_path"]).read_text())
        self.assertEqual(audit["selector"], f"{self.plugin_name}@{self.marketplace}")
        self.assertEqual(audit["source_digest"], audit["installed_digest"])
        self.assertEqual(marketplace_path.read_bytes(), marketplace_before)

    def test_replaces_old_and_broken_symlinks_with_direct_validated_aliases(self):
        module = load_module()
        self.cache_root.mkdir(parents=True)
        outside = self.base / "old-install"
        (outside / "hooks").mkdir(parents=True)
        (outside / "hooks" / "nightfalcon_hook.py").write_text("old\n")
        old_alias = self.cache_root / "1.0.0"
        broken_alias = self.cache_root / "1.1.0"
        old_alias.symlink_to(outside, target_is_directory=True)
        broken_alias.symlink_to(self.base / "missing", target_is_directory=True)
        installer = FakePluginAdd(self.source, self.cache_root, self.version)

        result = self.reinstall(module, installer)
        installed = pathlib.Path(result["installed_root"])

        for alias in (old_alias, broken_alias):
            self.assertTrue(alias.is_symlink())
            self.assertEqual(pathlib.Path(os.readlink(alias)), installed)
            self.assertEqual(alias.resolve(strict=True), installed.resolve(strict=True))
            self.assertEqual(
                (alias / "hooks" / "nightfalcon_hook.py").read_bytes(),
                (installed / "hooks" / "nightfalcon_hook.py").read_bytes(),
            )

    def test_real_legacy_directory_is_moved_to_audited_backup(self):
        module = load_module()
        legacy = self.cache_root / "1.0.0"
        (legacy / "hooks").mkdir(parents=True)
        (legacy / "hooks" / "legacy.py").write_text("preserve me\n")
        installer = FakePluginAdd(self.source, self.cache_root, self.version)

        result = self.reinstall(module, installer)
        actions = [action for action in result["actions"] if action["alias"] == str(legacy)]

        self.assertTrue(legacy.is_symlink())
        self.assertEqual(len(actions), 1)
        backup = pathlib.Path(actions[0]["backup_path"])
        self.assertTrue(backup.is_dir())
        self.assertEqual((backup / "hooks" / "legacy.py").read_text(), "preserve me\n")
        self.assertTrue(backup.is_relative_to(self.codex_home))

    def test_repeated_execution_is_idempotent(self):
        module = load_module()
        legacy = self.cache_root / "1.0.0"
        (legacy / "hooks").mkdir(parents=True)
        (legacy / "hooks" / "legacy.py").write_text("preserve me\n")
        installer = FakePluginAdd(self.source, self.cache_root, self.version)

        first = self.reinstall(module, installer, run_id="first")
        backups_before = sorted(
            path.relative_to(self.codex_home).as_posix()
            for path in self.codex_home.rglob("legacy.py")
        )
        target_before = os.readlink(legacy)
        second = self.reinstall(module, installer, run_id="second")
        backups_after = sorted(
            path.relative_to(self.codex_home).as_posix()
            for path in self.codex_home.rglob("legacy.py")
        )

        self.assertEqual(first["status"], "PASS")
        self.assertEqual(second["status"], "PASS")
        self.assertEqual(backups_before, backups_after)
        self.assertEqual(os.readlink(legacy), target_before)
        self.assertTrue((self.cache_root / self.version).is_dir())
        self.assertFalse((self.cache_root / self.version).is_symlink())
        second_action = next(
            action for action in second["actions"] if action["alias"] == str(legacy)
        )
        self.assertEqual(second_action["action"], "unchanged")

    def test_bad_installed_parity_fails_before_alias_publication(self):
        module = load_module()
        self.cache_root.mkdir(parents=True)
        original_target = self.base / "original-target"
        alias = self.cache_root / "1.0.0"
        alias.symlink_to(original_target, target_is_directory=True)

        def corrupt(installed):
            (installed / "README.md").write_text("tampered install\n")

        installer = FakePluginAdd(
            self.source, self.cache_root, self.version, mutate=corrupt
        )

        with self.assertRaisesRegex(module.RepairError, "parity"):
            self.reinstall(module, installer)

        self.assertTrue(alias.is_symlink())
        self.assertEqual(os.readlink(alias), str(original_target))

    def test_mid_publication_failure_rolls_back_all_legacy_paths(self):
        module = load_module()
        real_legacy = self.cache_root / "1.0.0"
        (real_legacy / "hooks").mkdir(parents=True)
        (real_legacy / "hooks" / "legacy.py").write_text("preserve me\n")
        original_target = self.base / "old-target"
        linked_legacy = self.cache_root / "1.1.0"
        linked_legacy.symlink_to(original_target, target_is_directory=True)
        installer = FakePluginAdd(self.source, self.cache_root, self.version)
        original_publish = module._publish_direct_symlink
        calls = 0

        def fail_second(alias, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected publication failure")
            return original_publish(alias, target)

        with mock.patch.object(module, "_publish_direct_symlink", side_effect=fail_second):
            with self.assertRaisesRegex(module.RepairError, "rolled back"):
                self.reinstall(module, installer)

        self.assertTrue(real_legacy.is_dir())
        self.assertFalse(real_legacy.is_symlink())
        self.assertEqual(
            (real_legacy / "hooks" / "legacy.py").read_text(), "preserve me\n"
        )
        self.assertTrue(linked_legacy.is_symlink())
        self.assertEqual(os.readlink(linked_legacy), str(original_target))

    def test_successful_directory_move_then_baseexception_is_rolled_back(self):
        module = load_module()
        legacy = self.cache_root / "1.0.0"
        (legacy / "hooks").mkdir(parents=True)
        (legacy / "hooks" / "legacy.py").write_text("preserve me\n")
        installer = FakePluginAdd(self.source, self.cache_root, self.version)
        original_replace = module.os.replace
        injected = False

        def succeed_then_interrupt(source, destination):
            nonlocal injected
            result = original_replace(source, destination)
            if pathlib.Path(source) == legacy and not injected:
                injected = True
                raise KeyboardInterrupt("injected after directory move")
            return result

        with mock.patch.object(module.os, "replace", side_effect=succeed_then_interrupt):
            with self.assertRaisesRegex(module.RepairError, "rolled back"):
                self.reinstall(module, installer, run_id="move-interrupt")

        self.assertTrue(legacy.is_dir())
        self.assertFalse(legacy.is_symlink())
        self.assertEqual((legacy / "hooks" / "legacy.py").read_text(), "preserve me\n")
        audit = json.loads(
            (
                self.codex_home
                / "plugins"
                / "cache-repair"
                / self.marketplace
                / self.plugin_name
                / "audit"
                / "move-interrupt.json"
            ).read_text()
        )
        self.assertEqual(audit["status"], "FAIL")
        self.assertEqual(audit["rollback"], "PASS")

    def test_successful_symlink_swap_then_baseexception_restores_original_target(self):
        module = load_module()
        self.cache_root.mkdir(parents=True)
        original_target = self.base / "old-target"
        original_target.mkdir()
        legacy = self.cache_root / "1.0.0"
        legacy.symlink_to(original_target, target_is_directory=True)
        installer = FakePluginAdd(self.source, self.cache_root, self.version)
        original_replace = module.os.replace
        injected = False

        def succeed_then_interrupt(source, destination):
            nonlocal injected
            result = original_replace(source, destination)
            source_path = pathlib.Path(source)
            if (
                pathlib.Path(destination) == legacy
                and ".nightfalcon-link-" in source_path.name
                and not injected
            ):
                injected = True
                raise KeyboardInterrupt("injected after symlink swap")
            return result

        with mock.patch.object(module.os, "replace", side_effect=succeed_then_interrupt):
            with self.assertRaisesRegex(module.RepairError, "rolled back"):
                self.reinstall(module, installer, run_id="link-interrupt")

        self.assertTrue(legacy.is_symlink())
        self.assertEqual(os.readlink(legacy), str(original_target))

    def test_target_tamper_after_initial_validation_fails_and_rolls_back(self):
        module = load_module()
        self.cache_root.mkdir(parents=True)
        original_target = self.base / "old-target"
        original_target.mkdir()
        legacy = self.cache_root / "1.0.0"
        legacy.symlink_to(original_target, target_is_directory=True)
        installer = FakePluginAdd(self.source, self.cache_root, self.version)
        original_publish = module._publish_direct_symlink
        tampered = False

        def publish_then_tamper(alias, target):
            nonlocal tampered
            original_publish(alias, target)
            if alias == legacy and not tampered:
                (target / "hooks" / "nightfalcon_hook.py").write_text(
                    "HOOK_BYTES = 'tampered after validation'\n"
                )
                tampered = True

        with mock.patch.object(
            module, "_publish_direct_symlink", side_effect=publish_then_tamper
        ):
            with self.assertRaisesRegex(module.RepairError, "rolled back"):
                self.reinstall(module, installer, run_id="post-validation-tamper")

        self.assertTrue(legacy.is_symlink())
        self.assertEqual(os.readlink(legacy), str(original_target))
        installed = self.cache_root / self.version
        self.assertNotEqual(
            module.build_manifest(installed)["digest"],
            module.build_manifest(self.source)["digest"],
        )

    def test_final_install_must_remain_real_direct_cache_child_without_aliases(self):
        module = load_module()
        installer = FakePluginAdd(self.source, self.cache_root, self.version)
        original_discover = module._discover_validated_install
        external = self.base / "relocated-install"

        def replace_validated_root_with_symlink(context, source_manifest):
            installed, manifest = original_discover(context, source_manifest)
            os.replace(installed, external)
            installed.symlink_to(external, target_is_directory=True)
            return installed, manifest

        with mock.patch.object(
            module,
            "_discover_validated_install",
            side_effect=replace_validated_root_with_symlink,
        ):
            with self.assertRaisesRegex(module.RepairError, "installed.*real.*child"):
                self.reinstall(module, installer, run_id="installed-root-symlink")

        self.assertTrue((self.cache_root / self.version).is_symlink())
        self.assertTrue(external.is_dir())

    def test_final_audit_failure_rolls_back_published_aliases(self):
        module = load_module()
        legacy = self.cache_root / "1.0.0"
        (legacy / "hooks").mkdir(parents=True)
        (legacy / "hooks" / "legacy.py").write_text("preserve me\n")
        installer = FakePluginAdd(self.source, self.cache_root, self.version)
        original_atomic_json = module._atomic_json
        calls = 0

        def fail_final_audit(path, document):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt("injected final audit failure")
            return original_atomic_json(path, document)

        with mock.patch.object(module, "_atomic_json", side_effect=fail_final_audit):
            with self.assertRaisesRegex(module.RepairError, "rolled back"):
                self.reinstall(module, installer, run_id="audit-failure")

        self.assertTrue(legacy.is_dir())
        self.assertFalse(legacy.is_symlink())
        self.assertEqual(
            (legacy / "hooks" / "legacy.py").read_text(), "preserve me\n"
        )
        audit_path = (
            self.codex_home
            / "plugins"
            / "cache-repair"
            / self.marketplace
            / self.plugin_name
            / "audit"
            / "audit-failure.json"
        )
        audit = json.loads(audit_path.read_text())
        self.assertEqual(audit["status"], "FAIL")
        self.assertEqual(audit["rollback"], "PASS")

    def test_incomplete_rollback_stays_recoverable_on_next_run(self):
        module = load_module()
        self.cache_root.mkdir(parents=True)
        original_target = self.base / "old-target"
        original_target.mkdir()
        legacy = self.cache_root / "1.0.0"
        legacy.symlink_to(original_target, target_is_directory=True)
        installer = FakePluginAdd(self.source, self.cache_root, self.version)

        with (
            mock.patch.object(
                module, "_verify_aliases", side_effect=OSError("force rollback")
            ),
            mock.patch.object(module, "_rollback", return_value=["injected"]),
        ):
            with self.assertRaisesRegex(module.RepairError, "rollback incomplete"):
                self.reinstall(module, installer, run_id="incomplete-rollback")

        audit_path = (
            self.codex_home
            / "plugins"
            / "cache-repair"
            / self.marketplace
            / self.plugin_name
            / "audit"
            / "incomplete-rollback.json"
        )
        pending = json.loads(audit_path.read_text())
        self.assertEqual(pending["status"], "IN_PROGRESS")
        self.assertTrue(legacy.is_symlink())
        self.assertEqual(os.readlink(legacy), str(self.cache_root / self.version))

        with self.assertRaisesRegex(module.RepairError, "plugin add failed"):
            self.reinstall(module, FailedPluginAdd(), run_id="after-incomplete")

        self.assertTrue(legacy.is_symlink())
        self.assertEqual(os.readlink(legacy), str(original_target))
        recovered = json.loads(audit_path.read_text())
        self.assertEqual(recovered["status"], "RECOVERED")
        self.assertEqual(recovered["recovery"], "PASS")

    def test_next_run_recovers_interrupted_real_directory_move_from_journal(self):
        module = load_module()
        context = module.resolve_context(self.wrapper, self.codex_home)
        legacy = self.cache_root / "1.0.0"
        (legacy / "hooks").mkdir(parents=True)
        (legacy / "hooks" / "legacy.py").write_text("preserve me\n")
        backup = context.repair_root / "backups" / "interrupted" / legacy.name
        backup.parent.mkdir(parents=True)
        os.replace(legacy, backup)
        audit_path = context.repair_root / "audit" / "interrupted.json"
        audit_path.parent.mkdir(parents=True)
        audit_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "interrupted",
                    "selector": context.selector,
                    "cache_root": str(context.cache_root),
                    "installed_root": str(self.cache_root / self.version),
                    "status": "IN_PROGRESS",
                    "plan": [
                        {
                            "version": legacy.name,
                            "original_kind": "directory",
                            "original_target": None,
                            "backup_path": str(backup),
                        }
                    ],
                }
            )
            + "\n"
        )

        with self.assertRaisesRegex(module.RepairError, "plugin add failed"):
            self.reinstall(module, FailedPluginAdd(), run_id="after-interruption")

        self.assertTrue(legacy.is_dir())
        self.assertFalse(legacy.is_symlink())
        self.assertEqual(
            (legacy / "hooks" / "legacy.py").read_text(), "preserve me\n"
        )
        self.assertFalse(backup.exists())
        recovered = json.loads(audit_path.read_text())
        self.assertEqual(recovered["status"], "RECOVERED")
        self.assertEqual(recovered["recovery"], "PASS")

    def test_pending_old_run_backup_symlink_is_rejected_without_external_mutation(self):
        module = load_module()
        context = module.resolve_context(self.wrapper, self.codex_home)
        installed = self.cache_root / self.version
        installed.parent.mkdir(parents=True)
        shutil.copytree(self.source, installed)
        legacy = self.cache_root / "1.0.0"
        legacy.symlink_to(installed, target_is_directory=True)
        external = self.base / "external-old-run"
        (external / legacy.name / "hooks").mkdir(parents=True)
        (external / legacy.name / "hooks" / "legacy.py").write_text("untouched\n")
        before = self.tree_snapshot(external)
        backups_root = context.repair_root / "backups"
        backups_root.mkdir(parents=True)
        (backups_root / "interrupted").symlink_to(
            external, target_is_directory=True
        )
        audit_path = context.repair_root / "audit" / "interrupted.json"
        audit_path.parent.mkdir(parents=True)
        audit_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "interrupted",
                    "selector": context.selector,
                    "cache_root": str(context.cache_root),
                    "installed_root": str(installed),
                    "status": "IN_PROGRESS",
                    "plan": [
                        {
                            "version": legacy.name,
                            "original_kind": "directory",
                            "original_target": None,
                            "backup_path": str(
                                backups_root / "interrupted" / legacy.name
                            ),
                        }
                    ],
                }
            )
            + "\n"
        )

        with self.assertRaisesRegex(module.RepairError, "backup.*symlink"):
            self.reinstall(module, FailedPluginAdd(), run_id="after-old-symlink")

        self.assertEqual(self.tree_snapshot(external), before)
        self.assertTrue(legacy.is_symlink())
        self.assertEqual(os.readlink(legacy), str(installed))

    def test_pending_recovery_cleans_only_its_journal_bound_link_debris(self):
        module = load_module()
        context = module.resolve_context(self.wrapper, self.codex_home)
        installed = self.cache_root / self.version
        installed.parent.mkdir(parents=True)
        shutil.copytree(self.source, installed)
        original_target = self.base / "original-target"
        original_target.mkdir()
        legacy = self.cache_root / "1.0.0"
        legacy.symlink_to(installed, target_is_directory=True)
        debris = self.cache_root / ".1.0.0.nightfalcon-link-999-crash"
        debris.symlink_to(installed, target_is_directory=True)
        audit_path = context.repair_root / "audit" / "interrupted-link.json"
        audit_path.parent.mkdir(parents=True)
        audit_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "interrupted-link",
                    "selector": context.selector,
                    "cache_root": str(context.cache_root),
                    "installed_root": str(installed),
                    "status": "IN_PROGRESS",
                    "plan": [
                        {
                            "version": legacy.name,
                            "original_kind": "symlink",
                            "original_target": str(original_target),
                            "backup_path": None,
                        }
                    ],
                }
            )
            + "\n"
        )

        with self.assertRaisesRegex(module.RepairError, "plugin add failed"):
            self.reinstall(module, FailedPluginAdd(), run_id="after-link-crash")

        self.assertTrue(legacy.is_symlink())
        self.assertEqual(os.readlink(legacy), str(original_target))
        self.assertFalse(debris.exists() or debris.is_symlink())
        recovered = json.loads(audit_path.read_text())
        self.assertEqual(recovered["status"], "RECOVERED")

    def test_pending_recovery_barriers_already_restored_aliases_before_terminal_audit(self):
        module = load_module()
        context = module.resolve_context(self.wrapper, self.codex_home)
        self.cache_root.mkdir(parents=True)
        installed = self.cache_root / self.version
        original_target = self.base / "original-target"
        original_target.mkdir()
        original_link = self.cache_root / "1.0.0"
        original_link.symlink_to(original_target, target_is_directory=True)
        original_directory = self.cache_root / "1.1.0"
        (original_directory / "hooks").mkdir(parents=True)
        backup = (
            context.repair_root
            / "backups"
            / "already-restored"
            / original_directory.name
        )
        backup.parent.mkdir(parents=True)
        audit_path = context.repair_root / "audit" / "already-restored.json"
        audit_path.parent.mkdir(parents=True)
        audit_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "already-restored",
                    "selector": context.selector,
                    "cache_root": str(context.cache_root),
                    "installed_root": str(installed),
                    "status": "IN_PROGRESS",
                    "plan": [
                        {
                            "version": original_link.name,
                            "original_kind": "symlink",
                            "original_target": str(original_target),
                            "backup_path": None,
                        },
                        {
                            "version": original_directory.name,
                            "original_kind": "directory",
                            "original_target": None,
                            "backup_path": str(backup),
                        },
                    ],
                }
            )
            + "\n"
        )

        with mock.patch.object(module, "_fsync_directory") as fsync_directory:
            module._recover_pending_repairs(context)

        requested = [call.args[0] for call in fsync_directory.call_args_list]
        self.assertIn(self.cache_root, requested)
        self.assertIn(backup.parent, requested)
        recovered = json.loads(audit_path.read_text())
        self.assertEqual(recovered["status"], "RECOVERED")

    def test_failed_symlink_publication_barriers_removed_temporary_entry(self):
        module = load_module()
        self.cache_root.mkdir(parents=True)
        alias = self.cache_root / "1.0.0"
        target = self.base / "target"
        target.mkdir()

        with (
            mock.patch.object(
                module, "_replace_path", side_effect=OSError("replace failed")
            ),
            mock.patch.object(module, "_fsync_directory") as fsync_directory,
        ):
            with self.assertRaisesRegex(OSError, "replace failed"):
                module._publish_direct_symlink(alias, target)

        self.assertFalse(alias.exists() or alias.is_symlink())
        self.assertFalse(
            any(
                entry.name.startswith(f".{alias.name}.nightfalcon-link-")
                for entry in self.cache_root.iterdir()
            )
        )
        fsync_directory.assert_any_call(self.cache_root)

    def test_symlinked_codex_home_is_rejected_before_external_mutation(self):
        module = load_module()
        external = self.base / "external-codex-home"
        external.mkdir()
        (external / "sentinel.txt").write_text("untouched\n")
        before = self.tree_snapshot(external)
        self.codex_home.symlink_to(external, target_is_directory=True)
        installer = FakePluginAdd(self.source, self.cache_root, self.version)

        with self.assertRaisesRegex(module.RepairError, "symlink"):
            self.reinstall(module, installer, run_id="symlinked-home")

        self.assertEqual(installer.calls, [])
        self.assertEqual(self.tree_snapshot(external), before)

    def test_symlinked_cache_root_is_rejected_before_external_mutation(self):
        module = load_module()
        external = self.base / "external-cache"
        (external / "1.0.0" / "hooks").mkdir(parents=True)
        (external / "1.0.0" / "hooks" / "legacy.py").write_text("untouched\n")
        before = self.tree_snapshot(external)
        self.cache_root.parent.mkdir(parents=True)
        self.cache_root.symlink_to(external, target_is_directory=True)
        installer = FakePluginAdd(self.source, self.cache_root, self.version)

        with self.assertRaisesRegex(module.RepairError, "symlink"):
            self.reinstall(module, installer, run_id="symlinked-cache")

        self.assertEqual(installer.calls, [])
        self.assertEqual(self.tree_snapshot(external), before)

    def test_symlinked_repair_root_is_rejected_before_external_mutation(self):
        module = load_module()
        context = module.resolve_context(self.wrapper, self.codex_home)
        external = self.base / "external-repair"
        external.mkdir()
        (external / "sentinel.txt").write_text("untouched\n")
        before = self.tree_snapshot(external)
        context.repair_root.parent.mkdir(parents=True)
        context.repair_root.symlink_to(external, target_is_directory=True)
        installer = FakePluginAdd(self.source, self.cache_root, self.version)

        with self.assertRaisesRegex(module.RepairError, "symlink"):
            self.reinstall(module, installer, run_id="symlinked-repair")

        self.assertEqual(installer.calls, [])
        self.assertEqual(self.tree_snapshot(external), before)

    def test_symlinked_audit_parent_is_rejected_before_external_mutation(self):
        module = load_module()
        context = module.resolve_context(self.wrapper, self.codex_home)
        external = self.base / "external-audit"
        external.mkdir()
        (external / "sentinel.txt").write_text("untouched\n")
        before = self.tree_snapshot(external)
        context.repair_root.mkdir(parents=True)
        (context.repair_root / "audit").symlink_to(
            external, target_is_directory=True
        )
        installer = FakePluginAdd(self.source, self.cache_root, self.version)

        with self.assertRaisesRegex(module.RepairError, "audit.*symlink"):
            self.reinstall(module, installer, run_id="symlinked-audit")

        self.assertEqual(installer.calls, [])
        self.assertEqual(self.tree_snapshot(external), before)

    def test_symlinked_backup_parent_is_rejected_before_external_mutation(self):
        module = load_module()
        context = module.resolve_context(self.wrapper, self.codex_home)
        external = self.base / "external-backups"
        external.mkdir()
        (external / "sentinel.txt").write_text("untouched\n")
        before = self.tree_snapshot(external)
        context.repair_root.mkdir(parents=True)
        (context.repair_root / "backups").symlink_to(
            external, target_is_directory=True
        )
        legacy = self.cache_root / "1.0.0"
        (legacy / "hooks").mkdir(parents=True)
        (legacy / "hooks" / "legacy.py").write_text("preserve me\n")
        installer = FakePluginAdd(self.source, self.cache_root, self.version)

        with self.assertRaisesRegex(module.RepairError, "backup.*symlink"):
            self.reinstall(module, installer, run_id="symlinked-backups")

        self.assertEqual(installer.calls, [])
        self.assertEqual(self.tree_snapshot(external), before)
        self.assertTrue(legacy.is_dir())

    def test_symlinked_repair_lock_is_rejected_before_external_access(self):
        module = load_module()
        context = module.resolve_context(self.wrapper, self.codex_home)
        external = self.base / "external-lock"
        external.write_text("untouched\n")
        before = external.read_bytes()
        context.repair_root.mkdir(parents=True)
        (context.repair_root / "repair.lock").symlink_to(external)
        installer = FakePluginAdd(self.source, self.cache_root, self.version)

        with self.assertRaisesRegex(module.RepairError, "lock.*symlink"):
            self.reinstall(module, installer, run_id="symlinked-lock")

        self.assertEqual(installer.calls, [])
        self.assertEqual(external.read_bytes(), before)

    def test_stranded_temporary_link_is_never_published_as_compatibility_alias(self):
        module = load_module()
        self.cache_root.mkdir(parents=True)
        debris = self.cache_root / ".1.0.0.nightfalcon-link-123-deadbeef"
        debris.symlink_to(self.base / "missing", target_is_directory=True)
        installer = FakePluginAdd(self.source, self.cache_root, self.version)

        with self.assertRaisesRegex(module.RepairError, "temporary.*link"):
            self.reinstall(module, installer, run_id="temp-debris")

        self.assertEqual(installer.calls, [])
        self.assertTrue(debris.is_symlink())

    def test_atomic_publication_requests_directory_fsync(self):
        module = load_module()
        self.assertTrue(
            hasattr(module, "_fsync_directory"),
            "reinstall helper has no directory durability primitive",
        )
        legacy = self.cache_root / "1.0.0"
        (legacy / "hooks").mkdir(parents=True)
        (legacy / "hooks" / "legacy.py").write_text("preserve me\n")
        installer = FakePluginAdd(self.source, self.cache_root, self.version)

        with mock.patch.object(module, "_fsync_directory") as fsync_directory:
            result = self.reinstall(module, installer, run_id="fsync-publication")

        requested = [call.args[0] for call in fsync_directory.call_args_list]
        context = module.resolve_context(self.wrapper, self.codex_home)
        self.assertIn(self.cache_root, requested)
        self.assertIn(pathlib.Path(result["audit_path"]).parent, requested)
        self.assertIn(context.repair_root, requested)
        self.assertIn(context.repair_root / "backups", requested)
        self.assertIn(
            context.repair_root / "backups" / "fsync-publication", requested
        )

    def test_unsupported_directory_fsync_and_close_are_best_effort(self):
        module = load_module()
        with (
            mock.patch.object(module.os, "open", return_value=42),
            mock.patch.object(module.os, "fsync", side_effect=OSError("unsupported")),
            mock.patch.object(module.os, "close", side_effect=OSError("unsupported")),
        ):
            try:
                module._fsync_directory(self.base)
            except OSError as exc:
                self.fail(f"best-effort directory fsync leaked an error: {exc}")

    def test_readme_documents_best_effort_power_loss_boundary(self):
        text = README_PATH.read_text().lower()
        self.assertIn("best-effort fsync", text)
        self.assertIn("power loss", text)

    def test_helper_has_no_recursive_deletion_primitive(self):
        text = REINSTALL_PATH.read_text()
        self.assertNotIn("rmtree", text)
        self.assertNotIn("rm -rf", text)


if __name__ == "__main__":
    unittest.main()
