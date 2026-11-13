"""Isolated stdlib tests: never installs to the real home or executes payloads."""

import errno
import fcntl
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import plugin_install
import plugin_support as support

ROOT = Path(__file__).absolute().parent.parent


def snapshot(root):
    result = {}
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        result[str(path.relative_to(root))] = (
            stat.S_IMODE(mode), os.readlink(path) if stat.S_ISLNK(mode)
            else path.read_bytes() if stat.S_ISREG(mode) else None)
    return result


class PluginInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.source = self.base / "source"
        (self.source / "plugin").mkdir(parents=True)
        for name in support.FILES:
            (self.source / name).write_bytes((ROOT / name).read_bytes())
        self.destination = self.home / support.DESTINATION
        self.state = self.home / support.MANIFEST
        # No installer path may launch a command or background process.
        for name in ("subprocess.Popen", "os.system", "os.fork", "os.posix_spawn"):
            patcher = mock.patch(name, side_effect=AssertionError("process creation forbidden"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_operation(self, operation="install", dry_run=False):
        support.run(operation, self.home, self.source, dry_run)

    def update_source(self):
        with (self.source / "plugin/Panel.qml").open("ab") as stream:
            stream.write(b"\n// test generation two\n")

    def install_update(self):
        self.run_operation()
        self.first = snapshot(self.destination)
        self.update_source()
        self.run_operation()

    def assert_refuses_unchanged(self, operation="install"):
        before = snapshot(self.base)
        with self.assertRaises((support.InstallError, OSError)):
            self.run_operation(operation)
        self.assertEqual(before, snapshot(self.base))

    def test_symlink_parent_spelling_is_checked_before_normalization(self):
        other = self.base / "other"
        (other / "child").mkdir(parents=True)
        (self.base / "alias").symlink_to(other / "child", target_is_directory=True)
        for field in ("home", "source"):
            with self.subTest(field=field):
                home = self.base / "alias/../home" if field == "home" else self.home
                source = self.base / "alias/../source" if field == "source" else self.source
                before = snapshot(self.base)
                with self.assertRaises(support.InstallError):
                    support.run("install", home, source, dry_run=True)
                self.assertEqual(before, snapshot(self.base))

    def test_exact_payload_modes_and_external_metadata(self):
        (self.source / "assets").mkdir()
        (self.source / "assets/secret").write_text("never copy")
        (self.source / "plugin/__pycache__").mkdir()
        (self.source / "plugin/__pycache__/ignored").write_text("cache")
        self.run_operation()
        files = {str(p.relative_to(self.destination)) for p in self.destination.rglob("*") if p.is_file()}
        self.assertEqual(files, set(support.FILES))
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        for name in support.FILES:
            path = self.destination / name
            self.assertEqual(path.read_bytes(), (self.source / name).read_bytes())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755 if name.endswith(".py") else 0o644)
        self.run_operation("verify")
        saved = json.loads(self.state.read_text())
        self.assertEqual(saved["current"], support.source_generation(self.source))
        self.assertIsNone(saved["previous"])

    def test_dry_run_first_install_writes_nothing(self):
        before = snapshot(self.base)
        self.run_operation(dry_run=True)
        self.assertEqual(before, snapshot(self.base))

    def test_verify_and_all_dry_runs_readonly(self):
        self.install_update()
        before = snapshot(self.base)
        self.run_operation("verify")
        self.update_source()
        before = snapshot(self.base)
        for operation in ("install", "rollback", "uninstall"):
            self.run_operation(operation, dry_run=True)
            self.assertEqual(before, snapshot(self.base))

    def test_identical_reinstall_keeps_previous(self):
        self.install_update()
        before = snapshot(self.home)
        self.run_operation()
        self.assertEqual(before, snapshot(self.home))
        self.run_operation("rollback")
        self.assertEqual(self.first, snapshot(self.destination))
        self.assertIsNone(json.loads(self.state.read_text())["previous"])

    def test_first_install_rollback_refuses_without_changes(self):
        self.run_operation()
        before = snapshot(self.base)
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                with self.assertRaisesRegex(support.InstallError, "No previous plugin generation"):
                    self.run_operation("rollback", dry_run=dry_run)
                self.assertEqual(before, snapshot(self.base))
        self.run_operation("verify")
        self.run_operation("uninstall")
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.state.exists())

    def test_repeated_rollback_refuses_without_uninstalling(self):
        self.install_update()
        self.run_operation("rollback")
        self.assertEqual(self.first, snapshot(self.destination))
        self.assertIsNone(json.loads(self.state.read_text())["previous"])
        before = snapshot(self.base)
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                with self.assertRaisesRegex(support.InstallError, "No previous plugin generation"):
                    self.run_operation("rollback", dry_run=dry_run)
                self.assertEqual(before, snapshot(self.base))
        self.run_operation("verify")

    def test_uninstall_preserves_configs_collections_and_other_plugin(self):
        unrelated = [".config/omarchy/bar.json", ".config/swiftwall/config.toml",
                     "Pictures/wallpapers/collection.jpg", ".config/omarchy/plugins/other/manifest.json"]
        for name in unrelated:
            path = self.home / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"keep exactly\n")
        self.run_operation()
        self.run_operation("uninstall")
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.state.exists())
        for name in unrelated:
            self.assertEqual((self.home / name).read_bytes(), b"keep exactly\n")

    def test_missing_install_refused_for_destructive_and_verify(self):
        for operation in ("verify", "rollback", "uninstall"):
            self.assert_refuses_unchanged(operation)

    def test_unowned_even_empty_destination_refused(self):
        self.destination.mkdir(parents=True)
        self.assert_refuses_unchanged()

    def test_unowned_existing_payload_refused(self):
        self.destination.mkdir(parents=True)
        (self.destination / "manifest.json").write_text("mine")
        self.assert_refuses_unchanged()

    def test_unknown_owned_paths_refused_all_operations(self):
        self.run_operation()
        (self.destination / "plugin/unknown.qml").write_text("unowned")
        for operation in ("install", "verify", "rollback", "uninstall"):
            self.assert_refuses_unchanged(operation)

    def test_unknown_empty_directory_refused(self):
        self.run_operation()
        (self.destination / "assets").mkdir()
        self.assert_refuses_unchanged("uninstall")

    def test_modified_update_refuses_rollback_and_all_operations(self):
        self.install_update()
        (self.destination / "plugin/Panel.qml").write_text("user modification")
        for operation in ("install", "verify", "rollback", "uninstall"):
            self.assert_refuses_unchanged(operation)

    def test_modified_mode_refused(self):
        self.run_operation()
        (self.destination / "plugin/pick_folder.py").chmod(0o644)
        self.assert_refuses_unchanged("rollback")

    def test_missing_owned_file_refused(self):
        self.run_operation()
        (self.destination / "plugin/Panel.qml").unlink()
        self.assert_refuses_unchanged("uninstall")

    def test_destination_file_symlink_refused(self):
        self.run_operation()
        path = self.destination / "plugin/Panel.qml"
        path.unlink()
        path.symlink_to(self.source / "plugin/Panel.qml")
        self.assert_refuses_unchanged("uninstall")

    def test_destination_directory_symlinks_refused(self):
        for relative in (".config", ".config/omarchy", str(support.DESTINATION)):
            with self.subTest(relative=relative):
                path = self.home / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                target = self.base / "target"
                target.mkdir(exist_ok=True)
                path.symlink_to(target, target_is_directory=True)
                self.assert_refuses_unchanged()
                path.unlink()

    def test_home_parent_symlink_refused(self):
        alias = self.base / "alias"
        alias.symlink_to(self.base, target_is_directory=True)
        before = snapshot(self.home)
        with self.assertRaises(support.InstallError):
            support.run("install", alias / "home", self.source)
        self.assertEqual(before, snapshot(self.home))

    def test_dangling_destination_symlink_refused(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.symlink_to(self.base / "missing")
        self.assert_refuses_unchanged()

    def test_state_parent_symlink_refused(self):
        (self.home / ".local").mkdir()
        (self.home / ".local/state").symlink_to(self.source, target_is_directory=True)
        self.assert_refuses_unchanged()

    def test_state_file_symlink_refused(self):
        self.state.parent.mkdir(parents=True)
        self.state.symlink_to(self.source / "manifest.json")
        self.assert_refuses_unchanged()

    def test_source_file_symlink_refused(self):
        path = self.source / "plugin/Panel.qml"
        path.unlink()
        path.symlink_to(self.source / "plugin/Service.qml")
        self.assert_refuses_unchanged()

    def test_source_parent_symlink_refused(self):
        (self.source / "plugin").rename(self.source / "real-plugin")
        (self.source / "plugin").symlink_to(self.source / "real-plugin", target_is_directory=True)
        self.assert_refuses_unchanged()

    def test_missing_source_refused_even_dry_run(self):
        (self.source / "plugin/Panel.qml").unlink()
        self.assert_refuses_unchanged()
        with self.assertRaises(support.InstallError):
            self.run_operation(dry_run=True)

    def test_manifest_contract_rejected_without_native_validation(self):
        original = json.loads((self.source / "manifest.json").read_text())
        changes = [{"id": "other"}, {"schemaVersion": True},
                   {"entryPoints": {"service": "../../escape"}}, {"kinds": []}, {"version": ""}]
        for change in changes:
            (self.source / "manifest.json").write_text(json.dumps(original | change))
            self.assert_refuses_unchanged()

    def test_corrupt_metadata_and_previous_refused(self):
        self.install_update()
        original = json.loads(self.state.read_text())
        for generation in ("current", "previous"):
            data = json.loads(json.dumps(original))
            data[generation]["plugin/Panel.qml"]["sha256"] = "bad"
            self.state.write_text(json.dumps(data))
            self.assert_refuses_unchanged("rollback")
        self.state.write_text("not json")
        self.assert_refuses_unchanged("uninstall")

    def test_failure_injection_restores_first_install_exactly(self):
        for phase in ("staged", "old-moved", "new-placed", "metadata-written"):
            with self.subTest(phase=phase):
                before = snapshot(self.home)
                def fail(name, phase=phase):
                    if name == phase:
                        raise OSError("injected")
                with mock.patch.object(support, "checkpoint", side_effect=fail):
                    with self.assertRaisesRegex(support.InstallError, "original files restored"):
                        self.run_operation()
                self.assertEqual(before, snapshot(self.home))

    def test_failure_injection_restores_update_uninstall_rollback(self):
        self.install_update()
        self.update_source()
        for operation in ("install", "uninstall", "rollback"):
            for phase in ("staged", "old-moved", "new-placed", "metadata-written"):
                with self.subTest(operation=operation, phase=phase):
                    before = snapshot(self.home)
                    def fail(name, phase=phase):
                        if name == phase:
                            raise RuntimeError("injected")
                    with mock.patch.object(support, "checkpoint", side_effect=fail):
                        with self.assertRaisesRegex(support.InstallError, "original files restored"):
                            self.run_operation(operation)
                    self.assertEqual(before, snapshot(self.home))
                    self.run_operation("verify")

    def test_keyboard_interrupt_restores_first_install_after_each_mutation_phase(self):
        for phase in ("staged", "old-moved", "new-placed", "metadata-written"):
            before = snapshot(self.home)
            def interrupt(name):
                if name == phase:
                    raise KeyboardInterrupt("injected primary interrupt")
            with self.subTest(phase=phase), \
                    mock.patch.object(support, "checkpoint", side_effect=interrupt), \
                    self.assertRaisesRegex(support.InstallError, "original files restored") as caught:
                self.run_operation()
            self.assertIsInstance(caught.exception.__cause__, KeyboardInterrupt)
            self.assertEqual(snapshot(self.home), before)

    def test_keyboard_interrupt_restores_update_uninstall_and_rollback(self):
        self.install_update()
        self.update_source()
        for operation in ("install", "uninstall", "rollback"):
            for phase in ("staged", "old-moved", "new-placed", "metadata-written"):
                before = snapshot(self.home)
                def interrupt(name):
                    if name == phase:
                        raise KeyboardInterrupt("injected primary interrupt")
                with self.subTest(operation=operation, phase=phase), \
                        mock.patch.object(support, "checkpoint", side_effect=interrupt), \
                        self.assertRaisesRegex(support.InstallError, "original files restored"):
                    self.run_operation(operation)
                self.assertEqual(snapshot(self.home), before)
                self.run_operation("verify")

    def test_repeated_keyboard_interrupt_retains_original_backup(self):
        self.install_update()
        before = snapshot(self.destination)
        old_metadata = self.state.read_bytes()
        self.update_source()
        original_rename = support.rename_noreplace
        def interrupt_primary(name):
            if name == "new-placed":
                raise KeyboardInterrupt("primary interrupted")
        def interrupt_recovery(source, destination):
            if destination.name == "failed-new":
                raise KeyboardInterrupt("recovery interrupted")
            original_rename(source, destination)
        with mock.patch.object(support, "checkpoint", side_effect=interrupt_primary), \
                mock.patch.object(support, "rename_noreplace", side_effect=interrupt_recovery), \
                self.assertRaisesRegex(support.InstallError, "recovery failed.*recovery interrupted.*backup retained") as caught:
            self.run_operation()
        self.assertNotIn("original files restored", str(caught.exception))
        backups = list(self.state.parent.glob(".plugin-transaction-*/old"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(snapshot(backups[0]), before)
        self.assertEqual(self.state.read_bytes(), old_metadata)

    def test_keyboard_interrupt_preserves_unknown_published_paths_and_backup(self):
        self.install_update()
        before = snapshot(self.destination)
        self.update_source()
        def add_unknown_and_interrupt(name):
            if name == "new-placed":
                (self.destination / "unknown.txt").write_bytes(b"keep unknown path")
                raise KeyboardInterrupt("primary interrupted")
        with mock.patch.object(support, "checkpoint", side_effect=add_unknown_and_interrupt), \
                self.assertRaisesRegex(support.InstallError, "backup retained"):
            self.run_operation()
        self.assertEqual((self.destination / "unknown.txt").read_bytes(), b"keep unknown path")
        backups = list(self.state.parent.glob(".plugin-transaction-*/old"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(snapshot(backups[0]), before)

    def test_staging_write_failure_cleans_created_paths(self):
        before = snapshot(self.home)
        with mock.patch.object(support, "write_record", side_effect=OSError("disk full")):
            with self.assertRaises(support.InstallError):
                self.run_operation()
        self.assertEqual(before, snapshot(self.home))

    def test_failed_tree_rename_keeps_existing_install(self):
        self.install_update()
        before = snapshot(self.home)
        with mock.patch.object(support, "rename_noreplace", side_effect=OSError("different filesystem")):
            with self.assertRaises(support.InstallError):
                self.run_operation("rollback")
        self.assertEqual(before, snapshot(self.home))

    def test_first_install_publication_does_not_replace_racing_empty_directory(self):
        identities = []

        def create_unowned(name):
            if name == "old-moved":
                self.destination.mkdir()
                identities.append(support.tree_identity(self.destination))

        with mock.patch.object(support, "checkpoint", side_effect=create_unowned):
            with self.assertRaisesRegex(support.InstallError, "original files restored"):
                self.run_operation()
        self.assertEqual(identities, [support.tree_identity(self.destination)])
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertFalse(self.state.exists())
        self.assertEqual(list(self.home.rglob(".plugin-transaction-*")), [])

    def test_update_publication_and_recovery_preserve_racing_empty_directory(self):
        self.install_update()
        old_tree = snapshot(self.destination)
        old_state = self.state.read_bytes()
        self.update_source()
        identities = []

        def create_unowned(name):
            if name == "old-moved":
                self.destination.mkdir()
                identities.append(support.tree_identity(self.destination))

        with mock.patch.object(support, "checkpoint", side_effect=create_unowned):
            with self.assertRaisesRegex(support.InstallError, "backup retained"):
                self.run_operation()
        self.assertEqual(identities, [support.tree_identity(self.destination)])
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertEqual(old_state, self.state.read_bytes())
        backups = list(self.state.parent.glob(".plugin-transaction-*/old"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(old_tree, snapshot(backups[0]))

    def test_recovery_does_not_replace_racing_empty_directory(self):
        self.install_update()
        old_tree = snapshot(self.destination)
        original_rename = support.rename_noreplace
        identities = []

        def race_restore(source, destination):
            if source.name == "old" and destination == self.destination:
                destination.mkdir()
                identities.append(support.tree_identity(destination))
            original_rename(source, destination)

        def fail(name):
            if name == "new-placed":
                raise OSError("injected")

        with mock.patch.object(support, "rename_noreplace", side_effect=race_restore), \
                mock.patch.object(support, "checkpoint", side_effect=fail):
            with self.assertRaisesRegex(support.InstallError, "backup retained"):
                self.run_operation("rollback")
        self.assertEqual(identities, [support.tree_identity(self.destination)])
        self.assertEqual(list(self.destination.iterdir()), [])
        backups = list(self.state.parent.glob(".plugin-transaction-*/old"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(old_tree, snapshot(backups[0]))
        self.assertEqual(len(list(self.state.parent.glob(".plugin-transaction-*/failed-new"))), 1)

    def test_recovery_preserves_unexpected_files_in_published_tree(self):
        self.install_update()
        old_tree = snapshot(self.destination)
        self.update_source()

        def add_unknown(name):
            if name == "new-placed":
                (self.destination / "unexpected.txt").write_text("keep me")
                raise OSError("injected after external edit")

        with mock.patch.object(support, "checkpoint", side_effect=add_unknown):
            with self.assertRaisesRegex(support.InstallError, "backup retained"):
                self.run_operation()
        self.assertEqual((self.destination / "unexpected.txt").read_text(), "keep me")
        backups = list(self.state.parent.glob(".plugin-transaction-*/old"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(old_tree, snapshot(backups[0]))

    def test_recovery_preserves_replaced_published_tree(self):
        self.run_operation()
        self.update_source()
        saved = self.home / "externally-moved"
        identities = []

        def replace_tree(name):
            if name == "new-placed":
                self.destination.rename(saved)
                self.destination.mkdir()
                (self.destination / "unexpected.txt").write_text("keep me")
                identities.append(support.tree_identity(self.destination))
                raise OSError("injected")

        with mock.patch.object(support, "checkpoint", side_effect=replace_tree):
            with self.assertRaisesRegex(support.InstallError, "backup retained"):
                self.run_operation()
        self.assertEqual(identities, [support.tree_identity(self.destination)])
        self.assertEqual((self.destination / "unexpected.txt").read_text(), "keep me")
        self.assertTrue(saved.is_dir())

    def test_recovery_preserves_identical_but_replaced_published_tree(self):
        self.run_operation()
        self.update_source()
        saved = self.home / "externally-moved"
        identities = []

        def replace_identically(name):
            if name == "new-placed":
                self.destination.rename(saved)
                self.destination.mkdir()
                (self.destination / "plugin").mkdir()
                for relative in support.FILES:
                    support.write_record(self.destination / relative,
                                         support.read_record(saved / relative))
                identities.append(support.tree_identity(self.destination))
                raise OSError("injected")

        with mock.patch.object(support, "checkpoint", side_effect=replace_identically):
            with self.assertRaisesRegex(support.InstallError, "directory was replaced.*backup retained"):
                self.run_operation()
        self.assertEqual(identities, [support.tree_identity(self.destination)])
        self.assertEqual(support.source_generation(saved),
                         support.source_generation(self.destination))
        self.assertEqual(len(list(self.state.parent.glob(".plugin-transaction-*/old"))), 1)

    def test_backup_additions_are_never_cleaned_up(self):
        self.run_operation()
        self.update_source()

        def add_to_backup(name):
            if name == "metadata-written":
                backup = next(self.state.parent.glob(".plugin-transaction-*/old"))
                (backup / "unexpected.txt").write_text("keep backup addition")

        with mock.patch.object(support, "checkpoint", side_effect=add_to_backup):
            with self.assertRaisesRegex(support.InstallError, "backup retained"):
                self.run_operation()
        addition = next(self.state.parent.glob(".plugin-transaction-*/old/unexpected.txt"))
        self.assertEqual(addition.read_text(), "keep backup addition")

    def test_noreplace_refuses_all_occupied_destination_types(self):
        source = self.base / "rename-source"
        source.mkdir()
        destination = self.base / "rename-destination"
        for kind in ("empty", "populated", "file", "symlink"):
            with self.subTest(kind=kind):
                if kind in ("empty", "populated"):
                    destination.mkdir()
                    if kind == "populated":
                        (destination / "keep").write_text("unowned")
                elif kind == "file":
                    destination.write_text("unowned")
                else:
                    destination.symlink_to(source, target_is_directory=True)
                before = snapshot(self.base)
                identity = support.tree_identity(destination)
                with self.assertRaises(FileExistsError):
                    support.rename_noreplace(source, destination)
                self.assertEqual(before, snapshot(self.base))
                self.assertEqual(identity, support.tree_identity(destination))
                if kind in ("empty", "populated"):
                    if kind == "populated":
                        (destination / "keep").unlink()
                    destination.rmdir()
                else:
                    destination.unlink()

    def test_noreplace_fails_closed_on_missing_libc_symbol(self):
        source = self.base / "rename-source"
        source.mkdir()
        before = snapshot(self.base)
        with mock.patch.object(support.ctypes, "CDLL", return_value=object()), \
                mock.patch.object(support.os, "rename", side_effect=AssertionError("unsafe fallback")):
            with self.assertRaisesRegex(support.InstallError, "no unsafe fallback"):
                support.rename_noreplace(source, self.base / "new")
        self.assertEqual(before, snapshot(self.base))

    def test_noreplace_fails_closed_on_unsupported_kernel_or_filesystem(self):
        source = self.base / "rename-source"
        source.mkdir()
        before = snapshot(self.base)
        for error in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            library = mock.Mock()
            library.renameat2.return_value = -1
            with mock.patch.object(support.ctypes, "CDLL", return_value=library), \
                    mock.patch.object(support.ctypes, "get_errno", return_value=error), \
                    mock.patch.object(support.os, "rename", side_effect=AssertionError("unsafe fallback")):
                with self.assertRaisesRegex(support.InstallError, "unsupported"):
                    support.rename_noreplace(source, self.base / "new")
            self.assertEqual(before, snapshot(self.base))

    def test_cross_device_preflight_rejects_dry_run_and_mutations_without_mounts(self):
        original_device = support.directory_device

        def simulated_device(path):
            value = original_device(path)
            return value + 1 if path == self.state.parent else value

        for installed in (False, True):
            if installed:
                self.install_update()
                self.update_source()
            operations = ("install", "uninstall", "rollback") if installed else ("install",)
            for operation in operations:
                for dry_run in (True, False):
                    with self.subTest(installed=installed, operation=operation, dry_run=dry_run):
                        before = snapshot(self.home)
                        with mock.patch.object(support, "directory_device", side_effect=simulated_device):
                            with self.assertRaisesRegex(support.InstallError, "different filesystems"):
                                self.run_operation(operation, dry_run=dry_run)
                        self.assertEqual(before, snapshot(self.home))
            if installed:
                with mock.patch.object(support, "directory_device", side_effect=simulated_device):
                    self.run_operation("verify")  # Verification needs no tree rename.

    def test_directory_device_uses_nearest_existing_ancestor_readonly(self):
        before = snapshot(self.home)
        self.assertEqual(support.directory_device(self.home / "missing/parents/child"),
                         self.home.stat().st_dev)
        self.assertEqual(before, snapshot(self.home))

    def test_recovery_failure_retains_backup_for_manual_recovery(self):
        self.install_update()
        old_tree = snapshot(self.destination)
        self.update_source()
        original_write = support.write_record

        def fail_restore(path, item):
            if path.name == "restore-metadata":
                raise OSError("recovery write failed")
            return original_write(path, item)

        def fail_after_commit(name):
            if name == "metadata-written":
                raise RuntimeError("injected")

        with mock.patch.object(support, "checkpoint", side_effect=fail_after_commit), \
                mock.patch.object(support, "write_record", side_effect=fail_restore):
            with self.assertRaisesRegex(support.InstallError, "backup retained"):
                self.run_operation()
        backups = list(self.state.parent.glob(".plugin-transaction-*/old"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(old_tree, snapshot(backups[0]))

    def test_metadata_replace_failure_restores_update(self):
        self.install_update()
        self.update_source()
        before = snapshot(self.home)
        with mock.patch.object(support.os, "replace", side_effect=OSError("injected replace failure")):
            with self.assertRaises(support.InstallError):
                self.run_operation()
        self.assertEqual(before, snapshot(self.home))

    def test_modified_metadata_mode_refused(self):
        self.run_operation()
        self.state.chmod(0o644)
        self.assert_refuses_unchanged("uninstall")

    def test_special_source_file_refused_without_reading(self):
        path = self.source / "plugin/Panel.qml"
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises(support.InstallError):
            self.run_operation()
        self.assertFalse(self.state.exists())

    def test_special_destination_file_refused_without_reading(self):
        self.run_operation()
        path = self.destination / "plugin/Panel.qml"
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises(support.InstallError):
            self.run_operation("uninstall")
        self.assertTrue(self.state.exists())

    def test_source_root_parent_symlink_refused(self):
        alias = self.base / "alias"
        alias.symlink_to(self.base, target_is_directory=True)
        before = snapshot(self.home)
        with self.assertRaises(support.InstallError):
            support.run("install", self.home, alias / "source")
        self.assertEqual(before, snapshot(self.home))

    def test_unrelated_source_symlink_is_never_followed(self):
        (self.source / "assets").symlink_to("/", target_is_directory=True)
        self.run_operation()
        self.assertFalse((self.destination / "assets").exists())
        self.run_operation("verify")

    def test_unexpected_owned_plugin_directory_symlink_refused(self):
        self.run_operation()
        (self.destination / "plugin").rename(self.destination / "real-plugin")
        (self.destination / "plugin").symlink_to(self.source / "plugin", target_is_directory=True)
        self.assert_refuses_unchanged("rollback")

    def test_source_modes_do_not_change_installed_modes(self):
        for name in support.FILES:
            (self.source / name).chmod(0o600)
        self.run_operation()
        self.run_operation("verify")
        for name in support.FILES:
            self.assertEqual(stat.S_IMODE((self.destination / name).stat().st_mode),
                             0o755 if name.endswith(".py") else 0o644)

    def test_lock_is_exclusive_released_and_creates_nothing(self):
        before = snapshot(self.home)
        with support.locked(self.home):
            fd = os.open(self.home, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
        with support.locked(self.home):
            pass
        self.assertEqual(before, snapshot(self.home))

    def test_cli_explicit_subcommands_temp_home_and_warnings(self):
        def cli(*args):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = plugin_install.main(list(args))
            return code, out.getvalue(), err.getvalue()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            plugin_install.main([])
        common = ["--home", str(self.home)]
        self.assertEqual(cli("install", *common, "--source", str(self.source), "--dry-run")[0], 0)
        self.assertFalse(self.state.exists())
        self.assertEqual(cli("install", *common, "--source", str(self.source))[0], 0)
        self.assertEqual(cli("verify", *common)[0], 0)
        before = snapshot(self.base)
        for options in (("--dry-run",), ()):
            code, _, warning = cli("rollback", *common, *options)
            self.assertEqual(code, 1)
            self.assertIn("Disable", warning)
            self.assertIn("No previous plugin generation", warning)
            self.assertEqual(before, snapshot(self.base))
        code, _, warning = cli("uninstall", *common)
        self.assertEqual(code, 0)
        self.assertIn("Disable", warning)
        self.assertEqual(cli("verify", *common)[0], 1)


if __name__ == "__main__":
    unittest.main()
