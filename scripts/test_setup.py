"""Wizard and CLI tests: no production service, shell, network or chooser access."""
import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import setup


class WizardTests(unittest.TestCase):
    def args(self, **changes):
        values = dict(folder=[], yes=False, interval=300, selection='shuffle')
        values.update(changes)
        return argparse.Namespace(**values)

    def test_terminal_wizard_unicode_defaults_and_dedup(self):
        replies = iter(['/wall café 雪 # % "quotes"', '/wall café 雪 # % "quotes"', '', '', ''])
        result = setup.first_run(self.args(), lambda _: next(replies), normalize=lambda p: p)
        import tomllib
        parsed = tomllib.loads(result.decode())
        self.assertEqual(parsed, {'folders': ['/wall café 雪 # % "quotes"'], 'selection': 'shuffle', 'interval_seconds': 300})

    def test_noninteractive_requires_folders(self):
        with self.assertRaises(setup.SetupError): setup.first_run(self.args(yes=True), normalize=lambda p: p)

    def test_explicit_interval_and_selection(self):
        result = setup.first_run(self.args(yes=True, folder=['/fixture'], interval=600, selection='sequential'), normalize=lambda p: p)
        self.assertIn(b'600', result)
        self.assertIn(b'sequential', result)

    def test_invalid_interval_rejected(self):
        for value in (0, -1, 31536001):
            with self.subTest(value=value), self.assertRaises(setup.SetupError): setup.config_bytes(['/x'], value, 'shuffle')

    def test_directory_symlink_and_missing_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'walls').mkdir()
            (root / 'walls/a.PNG').write_bytes(b'fixture')
            (root / 'link').symlink_to(root / 'walls', target_is_directory=True)
            local = Mock(return_value=subprocess.CompletedProcess([], 0, 'ext4\n', ''))
            self.assertEqual(setup.folder(str(root / 'link'), local), str(root / 'walls'))
            with self.assertRaises(OSError): setup.folder(str(root / 'missing'), local)
            (root / 'loop').symlink_to(root / 'loop')
            with self.assertRaises((OSError, RuntimeError)): setup.folder(str(root / 'loop'), local)
            (root / 'remote').mkdir()
            (root / 'remote/a.jpg').write_bytes(b'fixture')
            remote = Mock(return_value=subprocess.CompletedProcess([], 0, 'cifs\n', ''))
            with self.assertRaises(setup.SetupError): setup.folder(str(root / 'remote'), remote)

    def test_empty_root_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            local = Mock(return_value=subprocess.CompletedProcess([], 0, 'ext4\n', ''))
            with self.assertRaises(setup.SetupError): setup.folder(temporary, local)

    def test_missing_dependencies(self):
        with self.assertRaises(setup.SetupError): setup.dependencies('install', which=lambda _: None)

    def test_unavailable_portal_rejected(self):
        runner = Mock(side_effect=[subprocess.CompletedProcess([], 0, b'', b''), subprocess.CompletedProcess([], 1, '', '')])
        with self.assertRaises(setup.SetupError): setup.dependencies('install', which=lambda name: '/usr/bin/' + name, run=runner)

    def test_test_environment_overrides_refused(self):
        with patch.dict(os.environ, {'SWIFTWALL_RUNTIME_DIR': '/private/fixture'}):
            with self.assertRaises(setup.SetupError): setup.environment(Path.home())

    def test_dry_run_no_commands_or_downloads(self):
        manager = Mock()
        manager.plan.return_value = {'engine_owned': False, 'plugin_owned': False}
        with patch.object(setup, 'environment', return_value={'HOME': '/fixture'}), patch.object(setup, 'SetupManager', return_value=manager), patch.object(setup, 'dependencies') as deps, patch.object(setup, 'fetch_release') as fetch:
            self.assertEqual(setup.main(['install', '--dry-run', '--version', '0.2.0']), 0)
        deps.assert_not_called()
        fetch.assert_not_called()
        manager.plan.assert_called_once_with('status')
        manager.install.assert_not_called()

    def test_destructive_uninstall_dry_run_discloses_data_removal(self):
        manager = Mock()
        manager.plan.return_value = {'engine_owned': True}
        output = io.StringIO()
        with patch.object(setup, 'environment', return_value={}), patch.object(setup, 'SetupManager', return_value=manager), patch.object(setup.sys, 'stdout', output):
            self.assertEqual(setup.main(['uninstall', '--remove-data', '--dry-run']), 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report['remove_data'])
        self.assertIn('remove exact', report['config'])
        self.assertIn('NEVER wallpaper images', report['config'])
        manager.uninstall.assert_not_called()

    def test_no_release_or_unverified_source_refused(self):
        args = setup.parser().parse_args(['install'])
        with self.assertRaises(setup.SetupError):
            with setup.bundle_for(args): self.fail('unverified source accepted')

    def test_older_portable_launcher_downloads_explicit_new_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            old = Path(temporary)
            (old / 'release.json').write_text('{}')
            args = setup.parser().parse_args(['update', '--version', '0.3.0'])
            with patch.object(setup, 'SOURCE', old), patch.object(setup, 'verify_directory', return_value={'version': '0.2.0'}), patch.object(setup, 'fetch_release', return_value=(old / 'new', {})) as fetch:
                with setup.bundle_for(args) as selected:
                    self.assertEqual(selected, old / 'new')
                self.assertEqual(fetch.call_args.args[0], '0.3.0')

    def test_status_uses_verified_legacy_environment_path(self):
        manager = Mock()
        manager.status.return_value = {'service': {'ActiveState': 'active'}}
        manager._version.return_value = '0.2.0-dev'
        manager.healthy.return_value = {'ok': True}
        with patch.object(setup, 'environment', return_value={}), patch.object(setup, 'SetupManager', return_value=manager), patch.object(setup, 'dependencies'), patch.object(setup, 'widget_connection', return_value={}), patch.object(setup.os, 'geteuid', return_value=1000):
            self.assertEqual(setup.main(['status']), 0)
        manager.healthy.assert_called_once_with('0.2.0-dev', allow_inherited=True)

    def test_bundle_verification_precedes_use(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / 'release.json').write_text('{}')
            args = setup.parser().parse_args(['install', '--bundle', str(path)])
            with self.assertRaises(ValueError):
                with setup.bundle_for(args): self.fail('invalid bundle accepted')

    def test_chooser_connection_is_not_claimed_from_cli_probe(self):
        manager = Mock()
        manager.control_socket = Path('/private/control.sock')
        manager.service.state.return_value = {'MainPID': '12'}
        manager.command.return_value = 'u_str ESTAB 0 0 /private/control.sock 10 * 11 users:(("swiftwall",pid=12,fd=7))\nu_str ESTAB 0 0 * 11 * 10 users:(("quickshell",pid=13,fd=5))\n'
        report = setup.widget_connection(manager)
        self.assertTrue(report['connected_kernel_peer'])
        self.assertIn('manually', report['gui_handshake_and_subscription'])

    def test_widget_reconnection_wait_is_bounded_and_does_not_enable(self):
        manager = Mock()
        times = iter((0, 0, 0.5, 1))
        with patch.object(setup, 'widget_connection', return_value={'connected_kernel_peer': False}):
            with self.assertRaises(setup.SetupError):
                setup.wait_widget_connection(manager, timeout=1, clock=lambda: next(times), sleep=lambda _: None)
        manager._controller.assert_not_called()
        with patch.object(setup, 'widget_connection', side_effect=[{'connected_kernel_peer': False}, {'connected_kernel_peer': True}]):
            self.assertTrue(setup.wait_widget_connection(manager, clock=lambda: 0, sleep=lambda _: None)['connected_kernel_peer'])

    def test_running_environment_is_checked_only_after_ipc_readiness(self):
        from setup_support import SetupManager
        manager = object.__new__(SetupManager)
        manager.service = Mock()
        manager.engine_environment = {}
        manager.binary = Path('/fixture/bin/swiftwall')
        manager.timeout = 10
        order = []
        manager.service.wait_ready.side_effect = lambda *a, **k: order.append('ready') or {'ok': True}
        manager.running_environment = lambda: order.append('environment')
        manager.socket_owner = lambda: order.append('peer')
        manager.running_executable = lambda: order.append('executable')
        self.assertEqual(manager.healthy('0.1.0'), {'ok': True})
        self.assertEqual(order, ['ready', 'environment', 'peer', 'executable'])

    def test_unrelated_socket_not_widget_proof(self):
        manager = Mock()
        manager.control_socket = Path('/private/control.sock')
        manager.service.state.return_value = {'MainPID': '12'}
        manager.command.return_value = 'u_str ESTAB 0 0 /different/control.sock 10 * 11 users:(("swiftwall",pid=12,fd=7))\n'
        self.assertFalse(setup.widget_connection(manager)['connected_kernel_peer'])


if __name__ == '__main__':
    unittest.main()
