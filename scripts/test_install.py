"""Isolated installer tests: no executable or live systemctl is ever run."""

import configparser
import contextlib
import io
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import install as installer
import install_support as support
import uninstall as uninstaller


class InstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="swiftwall-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.binary = self.root / "fake-binary"
        self.binary.write_bytes(b"fake binary version one\n")
        self.runner = mock.patch.object(subprocess, "Popen",
                                       side_effect=AssertionError("No subprocesses allowed in tests"))
        self.systemctl = self.runner.start()
        self.addCleanup(self.runner.stop)
        self.addCleanup(self.systemctl.assert_not_called)

    def install(self):
        support.install(self.home, self.binary)

    def installed_manifest(self):
        manifest = support.load_manifest(self.home)
        assert manifest is not None, "Expected an installed ownership manifest"
        return manifest

    def snapshot(self):
        return {str(path.relative_to(self.home)): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                for path in self.home.rglob("*") if path.is_file()}

    def cli(self, function, args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return function(args)

    def test_install_modes_manifest_and_no_activation(self):
        self.assertEqual(self.cli(installer.main, ["--home", str(self.home), "--binary", str(self.binary)]), 0)
        self.assertEqual((self.home / support.FILES["binary"]).read_bytes(), self.binary.read_bytes())
        for relative, mode in [(support.FILES["binary"], 0o755), (support.FILES["unit"], 0o644),
                               (support.MANIFEST, 0o600)]:
            self.assertEqual(stat.S_IMODE((self.home / relative).stat().st_mode), mode)
        self.assertIsNone(self.installed_manifest()["previous"])
        self.systemctl.assert_not_called()

    def test_dry_run_creates_nothing(self):
        missing_home = self.home / "not-created"
        support.install(missing_home, self.binary, dry_run=True)
        self.assertFalse(missing_home.exists())
        self.assertEqual(list(self.home.iterdir()), [])
        self.systemctl.assert_not_called()

    def test_uninstall_preserves_neighbors_config_state_and_directories(self):
        self.install()
        neighbors = [self.home / ".local/bin/another-app", self.home / ".config/swiftwall/config.json",
                     self.home / ".local/state/swiftwall/state.json",
                     self.home / ".config/systemd/user/other.service"]
        for path in neighbors:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"leave alone")
        support.uninstall(self.home, dry_run=True)
        self.assertTrue((self.home / support.MANIFEST).exists())
        self.assertEqual(self.cli(uninstaller.main, ["--home", str(self.home)]), 0)
        for path in neighbors:
            self.assertEqual(path.read_bytes(), b"leave alone")
        for relative in [*support.FILES.values(), support.MANIFEST]:
            self.assertFalse((self.home / relative).exists())
        self.assertTrue((self.home / ".local/state/swiftwall").is_dir())
        support.uninstall(self.home)  # Idempotent, still file-only.
        self.systemctl.assert_not_called()

    def test_modified_content_and_mode_refuse_all_operations(self):
        self.install()
        self.binary.write_bytes(b"version two")
        for relative in support.FILES.values():
            path = self.home / relative
            original = support.read_record(self.home, path)
            for change in (lambda path=path: path.write_bytes(b"user edited"),
                           lambda path=path: path.chmod(0o600)):
                change()
                before = self.snapshot()
                for operation in (self.install, lambda: support.uninstall(self.home),
                                  lambda: support.rollback(self.home)):
                    with self.assertRaises(support.InstallError):
                        operation()
                    self.assertEqual(self.snapshot(), before)
                support.atomic_write(path, original)

    def test_missing_owned_file_refused(self):
        self.install()
        (self.home / support.FILES["unit"]).unlink()
        before = self.snapshot()
        with self.assertRaises(support.InstallError):
            support.uninstall(self.home)
        self.assertEqual(before, self.snapshot())

    def test_unowned_file_refused(self):
        path = self.home / support.FILES["unit"]
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not installer owned")
        before = self.snapshot()
        with self.assertRaises(support.InstallError):
            self.install()
        self.assertEqual(self.snapshot(), before)

    def test_symlink_targets_and_parents_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        for relative in [Path(".config"), Path(".local/bin"), *support.FILES.values(), support.MANIFEST]:
            with self.subTest(path=relative):
                path = self.home / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(outside / "missing")  # Includes dangling links.
                with self.assertRaises(support.InstallError):
                    self.install()
                self.assertTrue(path.is_symlink())
                path.unlink()
        self.assertEqual(list(outside.iterdir()), [])
        linked_home = self.root / "linked-home"
        linked_home.symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(support.InstallError):
            support.install(linked_home, self.binary)

    def test_owned_symlink_refused(self):
        self.install()
        path = self.home / support.FILES["binary"]
        path.unlink()
        path.symlink_to(self.binary)
        for operation in (self.install, lambda: support.uninstall(self.home), lambda: support.rollback(self.home)):
            with self.assertRaises(support.InstallError):
                operation()
        self.assertTrue(path.is_symlink())
        self.assertEqual(self.binary.read_bytes(), b"fake binary version one\n")

    def fail_write_once(self, number):
        original = support.atomic_write
        calls = []

        def write(path, item):
            calls.append(path)
            if len(calls) == number:
                raise OSError("injected write failure")
            return original(path, item)

        return mock.patch.object(support, "atomic_write", side_effect=write)

    def test_partial_first_install_restores_empty_home(self):
        for failure in (1, 2, 3):
            with (
                self.subTest(failure=failure),
                self.fail_write_once(failure),
                self.assertRaisesRegex(support.InstallError, "original files restored"),
            ):
                self.install()
            self.assertEqual(list(self.home.iterdir()), [])

    def test_partial_upgrade_restores_bytes_modes_and_manifest(self):
        self.install()
        original = self.snapshot()
        self.binary.write_bytes(b"version two")
        for failure in (1, 2, 3):
            with (
                self.subTest(failure=failure),
                self.fail_write_once(failure),
                self.assertRaises(support.InstallError),
            ):
                self.install()
            self.assertEqual(self.snapshot(), original)

    def test_upgrade_explicit_rollback_consumes_one_generation(self):
        self.install()
        first = self.installed_manifest()["current"]
        # Exercise unit restoration too without changing the repository unit.
        alternate_unit = self.root / "alternate.service"
        alternate_unit.write_bytes(b"alternate unit for upgrade test")
        self.binary.write_bytes(b"version two")
        with mock.patch.object(support, "UNIT_SOURCE", alternate_unit):
            self.install()
            self.install()  # Identical reinstall must retain the previous version.
        self.assertEqual(self.installed_manifest()["previous"], first)
        upgraded = self.snapshot()
        support.rollback(self.home, dry_run=True)
        self.assertEqual(self.snapshot(), upgraded)
        self.assertEqual(self.cli(installer.main, ["--home", str(self.home), "--rollback"]), 0)
        manifest = self.installed_manifest()
        self.assertEqual(manifest["current"], first)
        self.assertIsNone(manifest["previous"])
        support.verify_owned(self.home, manifest)
        with self.assertRaisesRegex(support.InstallError, "No previous owned version"):
            support.rollback(self.home)
        self.systemctl.assert_not_called()

    def test_failed_rollback_restores_upgrade(self):
        self.install()
        self.binary.write_bytes(b"version two")
        self.install()
        before = self.snapshot()
        with self.fail_write_once(3), self.assertRaises(support.InstallError):
            support.rollback(self.home)
        self.assertEqual(self.snapshot(), before)

    def test_failed_uninstall_restores_installation(self):
        self.install()
        before = self.snapshot()
        original = Path.unlink
        unit = self.home / support.FILES["unit"]

        def unlink(path, *args, **kwargs):
            if path == unit:
                raise OSError("injected unlink failure")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", unlink), self.assertRaises(support.InstallError):
            support.uninstall(self.home)
        self.assertEqual(self.snapshot(), before)

    def test_corrupt_manifest_refused(self):
        self.install()
        path = self.home / support.MANIFEST
        for data in (b"not json", b"[]", b'{"version":1,"current":{},"previous":null}'):
            path.write_bytes(data)
            before = self.snapshot()
            with self.assertRaises(support.InstallError):
                self.install()
            self.assertEqual(self.snapshot(), before)

    def assert_activation_refused(self, args):
        error = io.StringIO()
        before = self.snapshot()
        with contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            installer.main(args)
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("scripts never change live service settings", error.getvalue())
        self.assertIn("packaging/INSTALL.md", error.getvalue())
        self.assertEqual(self.snapshot(), before)
        self.systemctl.assert_not_called()

    def test_activation_refused_for_isolated_home_before_writes(self):
        args = ["--home", str(self.home), "--binary", str(self.binary), "--enable-autostart"]
        for suffix in ([], ["--dry-run"]):
            self.assert_activation_refused(args + suffix)
            self.assertEqual(list(self.home.iterdir()), [])

    def test_activation_refused_for_actual_home_before_writes(self):
        with mock.patch.object(Path, "home", return_value=self.home):
            for suffix in ([], ["--dry-run"]):
                self.assert_activation_refused(["--binary", str(self.binary), "--enable-autostart"] + suffix)
                self.assertEqual(list(self.home.iterdir()), [])

    def test_activation_refused_without_upgrading_or_rolling_back(self):
        self.install()
        self.binary.write_bytes(b"version two")
        self.install()
        self.binary.write_bytes(b"version three")
        for operation in (["--binary", str(self.binary)], ["--rollback"]):
            for suffix in ([], ["--dry-run"]):
                self.assert_activation_refused(["--home", str(self.home), "--enable-autostart"]
                                               + operation + suffix)

    def test_service_enablement_neighbors_never_changed(self):
        # Mimic preexisting manual enablement without using systemctl. All file
        # operations, including failure recovery, must leave it exactly alone.
        wants = self.home / ".config/systemd/user/graphical-session.target.wants"
        wants.mkdir(parents=True)
        enabled = wants / "swiftwall.service"
        target = "../swiftwall.service"
        enabled.symlink_to(target)
        self.install()
        self.binary.write_bytes(b"version two")
        self.install()
        self.binary.write_bytes(b"version three")
        with self.fail_write_once(2), self.assertRaises(support.InstallError):
            self.install()
        support.rollback(self.home)
        support.uninstall(self.home)
        self.assertTrue(enabled.is_symlink())
        self.assertEqual(str(enabled.readlink()), target)
        self.assertEqual(self.snapshot(), {})

    def test_unit_contract(self):
        unit = configparser.ConfigParser(interpolation=None)
        unit.read(support.UNIT_SOURCE)
        expected = {
            "Unit": {"After": "graphical-session.target", "PartOf": "graphical-session.target",
                     "StartLimitIntervalSec": "60", "StartLimitBurst": "5"},
            "Service": {"Type": "simple", "ExecStart": "%h/.local/bin/swiftwall daemon",
                        "Restart": "on-failure", "RestartSec": "5", "KillMode": "control-group"},
            "Install": {"WantedBy": "graphical-session.target"},
        }
        for section, entries in expected.items():
            for key, value in entries.items():
                self.assertEqual(unit[section][key], value)


if __name__ == "__main__":
    unittest.main()
