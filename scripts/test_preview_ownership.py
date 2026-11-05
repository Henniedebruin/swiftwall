"""Temporary-home, file-only tests: never install into a live home or run commands."""


import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

import plugin_support as support
import preview_bundle
import preview_ownership as ownership


ROOT = Path(__file__).resolve().parents[1]


def snapshot(root):
    result = {}
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        result[str(path.relative_to(root))] = (
            stat.S_IMODE(mode), os.readlink(path) if stat.S_ISLNK(mode)
            else path.read_bytes() if stat.S_ISREG(mode) else None)
    return result


class PreviewOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="po-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / "home"
        self.source = self.base / "source"
        self.bundles = self.base / "bundles"
        self.runtime = self.base / "runtime"
        self.evidence = self.base / "evidence"
        for path in (self.home, self.source, self.bundles, self.runtime, self.evidence):
            path.mkdir(mode=0o700)
        (self.source / "plugin").mkdir()
        for name in support.FILES:
            (self.source / name).write_bytes((ROOT / name).read_bytes())
        self.state = self.evidence / "ownership.json"
        self.watched = self.home / ownership.WATCHED
        self.watched.mkdir(parents=True)
        self.other = self.watched / "unrelated-plugin"
        self.other.mkdir()
        (self.other / "data").write_bytes(b"other plugin")
        self.production = self.home / support.DESTINATION
        self.production.mkdir()
        (self.production / "sentinel").write_bytes(b"production plugin")
        self.production_state = self.home / support.MANIFEST
        self.production_state.parent.mkdir(parents=True)
        self.production_state.write_bytes(b"production evidence")
        self.config = self.home / ".config/omarchy/settings.json"
        self.config.write_text('{"plugins":["production"],"bar":{"widgets":[1,2]}}')
        self.protected = {p: snapshot(p) for p in (self.other, self.production, self.source)}
        self.protected_files = {p: p.read_bytes() for p in (self.config, self.production_state)}
        self.globals = (support.PLUGIN_ID, support.DESTINATION, support.MANIFEST, support.FILES)
        self.enterContext(mock.patch.object(Path, "home", return_value=self.home))
        for name in ("subprocess.Popen", "subprocess.run", "os.system", "os.fork", "os.posix_spawn"):
            self.enterContext(mock.patch(name, side_effect=AssertionError("No process calls")))
        # All test writes must remain in this temporary fixture. Reads of project
        # sources are allowed; caches are disabled by the test invocation.
        original_open = Path.open

        def guarded_open(path, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+") and not path.is_relative_to(self.base):
                raise AssertionError(f"Non-temporary write: {path}")
            return original_open(path, mode, *args, **kwargs)

        self.enterContext(mock.patch.object(Path, "open", guarded_open))
        self.bundle = self.stage()

    def tearDown(self):
        for path, expected in self.protected.items():
            self.assertEqual(snapshot(path), expected)
        for path, expected in self.protected_files.items():
            self.assertEqual(path.read_bytes(), expected)
        self.assertEqual((support.PLUGIN_ID, support.DESTINATION, support.MANIFEST, support.FILES),
                         self.globals)

    def stage(self):
        return preview_bundle.stage_preview(self.source, self.bundles, self.runtime)["bundle"]

    def install(self, bundle=None, state=None):
        return ownership.install_preview(bundle or self.bundle, self.home, state or self.state)

    def intent(self):
        return json.loads(self.state.read_text())

    def assert_refused_unchanged(self, function, *args):
        before = snapshot(self.base)
        with self.assertRaises((support.InstallError, OSError)):
            function(*args)
        self.assertEqual(snapshot(self.base), before)

    def test_two_successive_fresh_ids_exact_eight_bytes_hashes_modes_and_removal(self):
        first = self.bundle
        second = self.stage()
        self.assertNotEqual(first.name, second.name)
        for index, bundle in enumerate((first, second)):
            with self.subTest(identity=bundle.name):
                state = self.evidence / f"{index}.json"
                original = snapshot(bundle)
                target = self.install(bundle, state)
                self.assertEqual(target, self.watched / bundle.name)
                self.assertEqual(ownership.verify_preview(self.home, state), target)
                data = json.loads(state.read_text())
                self.assertEqual(set(data["generation"]), set(support.FILES))
                self.assertEqual(len(data["generation"]), 8)
                self.assertEqual(data["id"], bundle.name)
                self.assertEqual(data["home"], str(self.home))
                self.assertEqual(data["target"], str(target))
                self.assertEqual(data["intent"], "install-preview")
                self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
                workspace = Path(data["workspace"])
                self.assertFalse(workspace.is_relative_to(self.watched))
                self.assertEqual(stat.S_IMODE(workspace.stat().st_mode), 0o700)
                self.assertEqual({str(p.relative_to(target)) for p in target.rglob("*")
                                  if p.is_file()}, set(support.FILES))
                for name in support.FILES:
                    expected = support.read_record(bundle / name)
                    self.assertEqual(data["generation"][name], expected)
                    self.assertEqual(support.read_record(target / name), expected)
                ownership.uninstall_preview(self.home, state)
                self.assertFalse(target.exists())
                self.assertFalse(state.exists())
                self.assertFalse(workspace.exists())
                self.assertEqual(snapshot(bundle), original)

    def test_instrumented_qml_within_allowlist_is_accepted(self):
        path = self.bundle / "plugin/Service.qml"
        path.write_bytes(path.read_bytes() + b"\n// approved test instrumentation\n")
        target = self.install()
        self.assertEqual((target / "plugin/Service.qml").read_bytes(), path.read_bytes())
        ownership.uninstall_preview(self.home, self.state)

    def test_only_exact_id_and_matching_bundle_basename(self):
        manifest_path = self.bundle / "manifest.json"
        original = manifest_path.read_bytes()
        for identity in (support.PLUGIN_ID, "local.swiftwall-preview-" + "A" * 32,
                         "local.swiftwall-preview-" + "a" * 31,
                         "local.swiftwall-preview-" + "a" * 32 + "\n",
                         "local.swiftwall-preview-" + "b" * 32):
            with self.subTest(identity=identity):
                manifest = json.loads(original)
                manifest["id"] = identity
                manifest_path.write_text(json.dumps(manifest))
                self.assert_refused_unchanged(self.install)
        manifest_path.write_bytes(original)
        renamed = self.bundle.with_name(support.PLUGIN_ID)
        self.bundle.rename(renamed)
        self.assert_refused_unchanged(self.install, renamed)

    def test_manifest_entrypoints_and_kinds_are_fixed(self):
        path = self.bundle / "manifest.json"
        original = json.loads(path.read_text())
        for changes in ({"entryPoints": {"service": "evil.qml"}}, {"kinds": ["service"]},
                        {"schemaVersion": True}, {"version": ""}):
            path.write_text(json.dumps(dict(original, **changes)))
            self.assert_refused_unchanged(self.install)

    def test_source_extras_missing_and_wrong_modes_refused(self):
        extra = self.bundle / "extra"
        extra.write_text("unowned")
        self.assert_refused_unchanged(self.install)
        extra.unlink()
        path = self.bundle / "plugin/Service.qml"
        path.chmod(0o600)
        self.assert_refused_unchanged(self.install)
        path.chmod(0o644)
        path.unlink()
        self.assert_refused_unchanged(self.install)

    def test_source_file_directory_and_ancestor_symlinks_refused(self):
        path = self.bundle / "plugin/Service.qml"
        external = self.base / "external"
        path.rename(external)
        path.symlink_to(external)
        self.assert_refused_unchanged(self.install)
        path.unlink()
        external.rename(path)
        directory = self.bundle / "plugin"
        directory.rename(self.base / "payload")
        directory.symlink_to(self.base / "payload", target_is_directory=True)
        self.assert_refused_unchanged(self.install)
        alias = self.base / "alias"
        alias.symlink_to(self.bundles, target_is_directory=True)
        self.assert_refused_unchanged(self.install, alias / self.bundle.name)

    def test_existing_empty_unowned_target_and_ownership_never_clobbered(self):
        target = self.watched / self.bundle.name
        target.mkdir()
        self.assert_refused_unchanged(self.install)
        target.rmdir()
        self.state.touch()
        self.assert_refused_unchanged(self.install)

    def test_evidence_watched_location_and_production_metadata_refused(self):
        for state in (self.watched / "evidence", self.watched / "extra/state",
                      self.production_state):
            self.assert_refused_unchanged(self.install, None, state)

    def test_home_evidence_and_watched_ancestor_symlinks_refused(self):
        alias = self.base / "home-alias"
        alias.symlink_to(self.home, target_is_directory=True)
        self.assert_refused_unchanged(ownership.install_preview, self.bundle, alias, self.state)
        alias = self.base / "state-alias"
        alias.symlink_to(self.evidence, target_is_directory=True)
        self.assert_refused_unchanged(self.install, None, alias / "state")
        moved = self.base / "watched"
        self.watched.rename(moved)
        self.watched.symlink_to(moved, target_is_directory=True)
        try:
            self.assert_refused_unchanged(self.install)
        finally:
            self.watched.unlink()
            moved.rename(self.watched)

    def test_unowned_verify_and_uninstall_refused_without_changes(self):
        target = self.watched / self.bundle.name
        target.mkdir()
        for function in (ownership.verify_preview, ownership.uninstall_preview):
            self.assert_refused_unchanged(function, self.home, self.state)

    def test_modified_extra_symlink_and_mode_changes_preserved(self):
        target = self.install()
        path = target / "plugin/Service.qml"
        original = path.read_bytes()
        for change in ("bytes", "mode", "extra", "symlink", "directory-symlink"):
            with self.subTest(change=change):
                extra = target / "extra"
                external = self.base / "original"
                if change == "bytes":
                    path.write_bytes(original + b"modified")
                elif change == "mode":
                    path.chmod(0o600)
                elif change == "extra":
                    extra.mkdir()
                    (extra / "user-file").write_text("preserve")
                elif change == "symlink":
                    path.rename(external)
                    path.symlink_to(external)
                else:
                    (target / "plugin").rename(external)
                    (target / "plugin").symlink_to(external, target_is_directory=True)
                for function in (ownership.verify_preview, ownership.uninstall_preview):
                    self.assert_refused_unchanged(function, self.home, self.state)
                if change == "bytes":
                    path.write_bytes(original)
                elif change == "mode":
                    path.chmod(0o644)
                elif change == "extra":
                    (extra / "user-file").unlink()
                    extra.rmdir()
                elif change == "symlink":
                    path.unlink()
                    external.rename(path)
                else:
                    (target / "plugin").unlink()
                    external.rename(target / "plugin")
        ownership.uninstall_preview(self.home, self.state)

    def test_modified_metadata_and_mismatched_home_refused(self):
        self.install()
        original = self.state.read_bytes()
        self.state.chmod(0o644)
        self.assert_refused_unchanged(ownership.uninstall_preview, self.home, self.state)
        self.state.chmod(0o600)
        value = json.loads(original)
        value["id"] = support.PLUGIN_ID
        self.state.write_text(json.dumps(value))
        self.assert_refused_unchanged(ownership.uninstall_preview, self.home, self.state)
        self.state.write_bytes(original)
        other_home = self.base / "other-home"
        other_home.mkdir()
        self.assert_refused_unchanged(ownership.verify_preview, other_home, self.state)

    def test_intent_exists_before_publish_and_exception_after_rename_is_recoverable(self):
        real_rename = support.rename_noreplace

        def rename_then_fail(source, destination):
            intent = self.intent()
            self.assertEqual(intent["target"], str(destination))
            self.assertEqual(intent["generation"], {
                name: support.read_record(source / name) for name in support.FILES})
            self.assertFalse(source.is_relative_to(self.watched))
            real_rename(source, destination)
            raise OSError("injected after rename")

        with mock.patch.object(support, "rename_noreplace", side_effect=rename_then_fail):
            with self.assertRaisesRegex(support.InstallError, "retained"):
                self.install()
        target = ownership.verify_preview(self.home, self.state)
        self.assertEqual(target, self.watched / self.bundle.name)
        ownership.uninstall_preview(self.home, self.state)
        self.assertFalse(target.exists())

    def test_exception_after_publication_checkpoint_keeps_recoverable_intent(self):
        def fail(name):
            if name == "published":
                raise OSError("publication failure")

        with mock.patch.object(ownership, "checkpoint", side_effect=fail):
            with self.assertRaisesRegex(support.InstallError, "retained"):
                self.install()
        ownership.verify_preview(self.home, self.state)
        ownership.uninstall_preview(self.home, self.state)

    def test_pending_intent_before_publish_can_be_uninstalled(self):
        with mock.patch.object(ownership, "checkpoint", side_effect=OSError("before rename")):
            with self.assertRaises(support.InstallError):
                self.install()
        self.assertFalse((self.watched / self.bundle.name).exists())
        ownership.uninstall_preview(self.home, self.state)
        self.assertFalse(self.state.exists())

    def test_target_race_no_clobber_and_recovery_does_not_delete_racer(self):
        real_rename = support.rename_noreplace

        def race(source, destination):
            destination.mkdir()
            (destination / "racer").write_text("do not delete")
            real_rename(source, destination)

        with mock.patch.object(support, "rename_noreplace", side_effect=race):
            with self.assertRaisesRegex(support.InstallError, "retained"):
                self.install()
        target = self.watched / self.bundle.name
        self.assertEqual((target / "racer").read_text(), "do not delete")
        self.assert_refused_unchanged(ownership.uninstall_preview, self.home, self.state)

    def test_metadata_race_is_exclusive_even_for_empty_file(self):
        real_write = support.write_record

        def race(path, item):
            if path == self.state:
                path.touch()
            real_write(path, item)

        with mock.patch.object(support, "write_record", side_effect=race):
            with self.assertRaises(support.InstallError):
                self.install()
        self.assertEqual(self.state.read_bytes(), b"")
        self.assertFalse((self.watched / self.bundle.name).exists())

    def test_cross_filesystem_preflight_has_no_writes(self):
        def device(path):
            return 1 if path == self.evidence else 2

        with mock.patch.object(support, "directory_device", side_effect=device):
            self.assert_refused_unchanged(self.install)

    def test_cross_filesystem_recheck_before_rename_keeps_intent(self):
        with mock.patch.object(support, "directory_device", side_effect=[1, 1, 1, 2]):
            with self.assertRaisesRegex(support.InstallError, "different filesystems"):
                self.install()
        self.assertFalse((self.watched / self.bundle.name).exists())
        ownership.uninstall_preview(self.home, self.state)

    def test_exact_copy_is_verified_before_intent_or_publish(self):
        real_write = support.write_record

        def corrupt(path, item):
            real_write(path, item)
            if path.name == "BarWidget.qml":
                path.write_bytes(b"corrupt copy")

        with mock.patch.object(support, "write_record", side_effect=corrupt):
            with self.assertRaisesRegex(support.InstallError, "modified"):
                self.install()
        self.assertFalse(self.state.exists())
        self.assertFalse((self.watched / self.bundle.name).exists())

    def test_quarantine_reverification_preserves_modified_tree_and_reports_path(self):
        target = self.install()
        workspace = Path(self.intent()["workspace"])

        def modify(name):
            self.assertEqual(name, "quarantined")
            (workspace / "quarantine/plugin/Service.qml").write_bytes(b"changed after move")

        with mock.patch.object(ownership, "checkpoint", side_effect=modify):
            with self.assertRaisesRegex(support.InstallError, str(workspace / "quarantine")):
                ownership.uninstall_preview(self.home, self.state)
        self.assertFalse(target.exists())
        self.assertEqual((workspace / "quarantine/plugin/Service.qml").read_bytes(),
                         b"changed after move")
        self.assertTrue(self.state.exists())
        self.assert_refused_unchanged(ownership.uninstall_preview, self.home, self.state)

    def test_quarantine_exception_is_recoverable(self):
        self.install()
        with mock.patch.object(ownership, "checkpoint", side_effect=OSError("after quarantine")):
            with self.assertRaisesRegex(support.InstallError, "retained path"):
                ownership.uninstall_preview(self.home, self.state)
        ownership.uninstall_preview(self.home, self.state)
        self.assertFalse(self.state.exists())

    def test_quarantine_destination_race_never_overwrites(self):
        target = self.install()
        workspace = Path(self.intent()["workspace"])
        real_rename = support.rename_noreplace

        def race(source, destination):
            destination.mkdir()
            real_rename(source, destination)

        with mock.patch.object(support, "rename_noreplace", side_effect=race):
            with self.assertRaisesRegex(support.InstallError, "retained path"):
                ownership.uninstall_preview(self.home, self.state)
        self.assertTrue(target.is_dir())
        self.assertEqual(list((workspace / "quarantine").iterdir()), [])
        self.assert_refused_unchanged(ownership.uninstall_preview, self.home, self.state)

    def test_replaced_identical_tree_is_not_owned(self):
        target = self.install()
        old = self.base / "old-tree"
        target.rename(old)
        target.mkdir()
        (target / "plugin").mkdir()
        for name in support.FILES:
            support.write_record(target / name, support.read_record(old / name))
        self.assert_refused_unchanged(ownership.verify_preview, self.home, self.state)
        self.assert_refused_unchanged(ownership.uninstall_preview, self.home, self.state)

    def test_symlinked_evidence_is_never_read_or_overwritten(self):
        original = self.base / "external-evidence"
        original.write_bytes(b"unowned evidence")
        self.state.symlink_to(original)
        self.assert_refused_unchanged(self.install)
        for function in (ownership.verify_preview, ownership.uninstall_preview):
            self.assert_refused_unchanged(function, self.home, self.state)

    def test_target_symlink_is_refused_for_all_operations(self):
        target = self.watched / self.bundle.name
        target.symlink_to(self.bundle, target_is_directory=True)
        self.assert_refused_unchanged(self.install)
        target.unlink()
        self.install()
        saved = self.base / "saved-target"
        target.rename(saved)
        target.symlink_to(saved, target_is_directory=True)
        for function in (ownership.verify_preview, ownership.uninstall_preview):
            self.assert_refused_unchanged(function, self.home, self.state)

    def test_symlinked_private_workspace_is_refused(self):
        self.install()
        workspace = Path(self.intent()["workspace"])
        saved = self.base / "saved-workspace"
        workspace.rename(saved)
        workspace.symlink_to(saved, target_is_directory=True)
        for function in (ownership.verify_preview, ownership.uninstall_preview):
            self.assert_refused_unchanged(function, self.home, self.state)

    def test_extra_file_after_quarantine_is_preserved_with_entire_payload(self):
        self.install()
        workspace = Path(self.intent()["workspace"])
        quarantine = workspace / "quarantine"

        def add_extra(name):
            (quarantine / "unowned").write_text("retain me")

        with mock.patch.object(ownership, "checkpoint", side_effect=add_extra):
            with self.assertRaisesRegex(support.InstallError, str(quarantine)):
                ownership.uninstall_preview(self.home, self.state)
        self.assertEqual((quarantine / "unowned").read_text(), "retain me")
        for name in support.FILES:
            self.assertEqual(support.read_record(quarantine / name),
                             support.read_record(self.bundle / name))
        self.assert_refused_unchanged(ownership.uninstall_preview, self.home, self.state)

    def test_metadata_generation_tampering_is_refused(self):
        self.install()
        value = self.intent()
        value["generation"]["plugin/Service.qml"]["sha256"] = "0" * 64
        self.state.write_text(json.dumps(value))
        for function in (ownership.verify_preview, ownership.uninstall_preview):
            self.assert_refused_unchanged(function, self.home, self.state)

    def test_uninstall_cross_filesystem_refused_without_moving(self):
        self.install()
        with mock.patch.object(support, "directory_device", side_effect=[1, 2]):
            self.assert_refused_unchanged(ownership.uninstall_preview, self.home, self.state)


if __name__ == "__main__":
    unittest.main()
