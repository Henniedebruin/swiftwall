"""Safety/unit tests only: these tests NEVER execute systemctl or a Rust binary.

The opt-in script is the real isolated integration test. These guard tests are
not evidence of daemon/service success. Release verification is a separate suite.
"""

import hashlib
import json
import shutil
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import rehearse_setup as rehearsal
from service_control import ServiceError, UserService
from setup_support import SetupManager


def source_fixture(root):
    paths = (root / 'v01-fixture', root / 'v02-fixture')
    hashes = []
    for number, path in enumerate(paths):
        data = f'Synthetic source fixture {number}; never execute.\n'.encode()
        path.write_bytes(data)
        path.chmod(0o700)
        hashes.append(hashlib.sha256(data).hexdigest())
    return rehearsal.Sources(*paths, *hashes)


def source_args(sources):
    return ['--v01', str(sources.v01), '--v02', str(sources.v02),
            '--v01-sha256', sources.v01_sha256, '--v02-sha256', sources.v02_sha256]


class SourceTests(unittest.TestCase):
    """Real file checks only; no mocked verification or service execution."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sws-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sources = source_fixture(self.root)

    def test_explicit_sources_are_verified_without_execution(self):
        self.assertEqual(self.sources.verify(), {
            str(self.sources.v01): self.sources.v01_sha256,
            str(self.sources.v02): self.sources.v02_sha256})

    def test_each_source_hash_is_required_and_checked(self):
        for field in ('v01_sha256', 'v02_sha256'):
            with self.subTest(field=field):
                values = vars(self.sources) | {field: '0' * 64}
                with self.assertRaisesRegex(RuntimeError, 'hash differs'):
                    rehearsal.Sources(**values).verify()

    def test_changed_source_is_rejected_on_reverification(self):
        self.sources.verify()
        for path in (self.sources.v01, self.sources.v02):
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(b'changed')
                with self.assertRaisesRegex(RuntimeError, 'hash differs'):
                    self.sources.verify()
                path.write_bytes(original)

    def test_missing_nonexecutable_and_symlink_sources_are_rejected(self):
        for path in (self.sources.v01, self.sources.v02):
            original = path.read_bytes()
            for kind in ('missing', 'nonexecutable', 'symlink'):
                with self.subTest(path=path, kind=kind):
                    path.unlink()
                    if kind == 'nonexecutable':
                        path.write_bytes(original)
                        path.chmod(0o600)
                    elif kind == 'symlink':
                        path.symlink_to(self.root / 'absent')
                    with self.assertRaises((RuntimeError, rehearsal.plugin.InstallError)):
                        self.sources.verify()
                    if path.is_symlink() or path.exists():
                        path.unlink()
                    path.write_bytes(original)
                    path.chmod(0o700)

    def test_invalid_hashes_and_paths_are_rejected(self):
        for field in ('v01_sha256', 'v02_sha256'):
            for value in ('', 'a' * 63, 'a' * 65, 'g' * 64, 'A' * 64):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(RuntimeError, 'Expected SHA256'):
                        rehearsal.Sources(**(vars(self.sources) | {field: value}))
        for field in ('v01', 'v02'):
            with self.subTest(field=field):
                with self.assertRaisesRegex(RuntimeError, 'absolute'):
                    rehearsal.Sources(**(vars(self.sources) | {field: Path('relative')}))
        with self.assertRaisesRegex(RuntimeError, 'distinct'):
            rehearsal.Sources(**(vars(self.sources) | {'v02': self.sources.v01}))

    def test_cli_requires_each_of_the_four_source_arguments(self):
        args = source_args(self.sources)
        for offset in range(0, len(args), 2):
            with self.subTest(option=args[offset]):
                with self.assertRaises(SystemExit) as error:
                    rehearsal.main(['--run', *args[:offset], *args[offset + 2:]])
                self.assertEqual(error.exception.code, 2)


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sws-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sources = source_fixture(self.root)
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        self.unit = 'swiftwall-setup-rehearsal-' + 'a' * 32 + '.service'
        self.env = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin',
                    'XDG_RUNTIME_DIR': str(self.home / 'run'),
                    'SWIFTWALL_CONFIG': str(self.home / 'config.toml'),
                    'SWIFTWALL_STATE_DIR': str(self.home / 'state'),
                    'SWIFTWALL_RUNTIME_DIR': str(self.home / 'run/swiftwall'),
                    'SWIFTWALL_BACKGROUND_LINK': str(self.home / 'background'),
                    'SWIFTWALL_OMARCHY_COMMAND': str(self.home / 'fake-setter')}
        self.events = []
        self.fixture = SimpleNamespace(home=self.home, unit=self.unit, env=self.env, case='safety-test',
            bundle=self.home / 'bundle', manager=SimpleNamespace(binary=self.home / '.local/bin/swiftwall'),
            session_env={'PATH': '/usr/bin:/bin', 'XDG_RUNTIME_DIR': '/run/user/1234'},
            record=lambda event, **fields: self.events.append((event, fields)))
        self.runner = rehearsal.GuardedRunner(self.fixture)
        self.process = patch.object(rehearsal.subprocess, 'run')
        self.run_mock = self.process.start()
        self.addCleanup(self.process.stop)
        self.run_mock.return_value = subprocess.CompletedProcess([], 0, '', '')

    def rejected(self, args, **kwargs):
        self.run_mock.reset_mock()
        with self.assertRaises((RuntimeError, ValueError, TypeError)):
            self.runner(args, capture_output=True, text=True, **kwargs)
        self.run_mock.assert_not_called()

    def test_no_production_units_or_arbitrary_systemctl(self):
        for args in (
            ['systemctl', '--user', 'stop', 'swiftwall.service'],
            ['systemctl', '--user', 'show', 'swiftwall.service', '-p', 'MainPID'],
            ['systemctl', '--user', 'start', self.unit, 'swiftwall.service'],
            ['systemctl', '--user', 'restart', self.unit],
            ['systemctl', '--user', 'enable', self.unit, '--now'],
            ['systemctl', '--user', 'daemon-reload', 'swiftwall.service'],
            ['systemctl', '--system', 'stop', self.unit],
            ['systemctl', '--user', 'show', self.unit, '-p', 'ExecStart'],
            ['systemctl', '--user', 'import-environment'],
            ['systemctl', '--user', 'show-environment'],
            ['/usr/bin/systemctl', '--user', 'stop', self.unit],
        ):
            with self.subTest(argv=args):
                self.rejected(args)

    def bus_commands(self):
        load = ['busctl', '--user', 'call', 'org.freedesktop.systemd1',
                '/org/freedesktop/systemd1', 'org.freedesktop.systemd1.Manager',
                'LoadUnit', 's', self.unit]
        files = ['busctl', '--user', 'get-property', 'org.freedesktop.systemd1',
                 self.runner.object_path, 'org.freedesktop.systemd1.Service', 'EnvironmentFiles']
        return load, files

    def test_typed_bus_calls_are_unchanged_and_use_session_bus(self):
        load, files = self.bus_commands()
        expected = '/org/freedesktop/systemd1/unit/swiftwall_2dsetup_2drehearsal_2d' + 'a' * 32 + '_2eservice'
        self.assertEqual(self.runner.object_path, expected)
        self.run_mock.return_value = subprocess.CompletedProcess(load, 0, f'o "{expected}"\n', '')
        self.runner(load, capture_output=True, text=True)
        self.run_mock.return_value = subprocess.CompletedProcess(files, 0, 'a(sb) 0\n', '')
        result = self.runner(files, capture_output=True, text=True)
        self.assertEqual(result.stdout, 'a(sb) 0\n')
        argv, kwargs = self.run_mock.call_args
        self.assertEqual(argv[0], files)
        self.assertEqual(kwargs['env'], self.fixture.session_env)
        self.assertTrue(self.events[-1][1]['typed_empty_verified'])
        self.assertNotIn('stdout', self.events[-1][1])

    def test_bus_guard_rejects_other_units_properties_types_and_unconfirmed_paths(self):
        load, files = self.bus_commands()
        self.rejected(files)  # No successful canonical LoadUnit response yet.
        for index, value in ((1, '--system'), (3, 'other.bus'), (6, 'StartUnit'),
                             (7, 'o'), (8, 'swiftwall.service')):
            bad = load.copy()
            bad[index] = value
            self.rejected(bad)
        self.rejected([*load, 'extra'])
        self.run_mock.return_value = subprocess.CompletedProcess(load, 0, 'o "/wrong/unit"', '')
        self.runner(load, capture_output=True, text=True)
        self.rejected(files)
        self.run_mock.return_value = subprocess.CompletedProcess(load, 0, f'o "{self.runner.object_path}"', '')
        self.runner(load, capture_output=True, text=True)
        for index, value in ((2, 'set-property'), (3, 'other.bus'),
                             (4, '/org/freedesktop/systemd1/unit/swiftwall_2eservice'),
                             (5, 'org.freedesktop.systemd1.Manager'), (6, 'Environment')):
            bad = files.copy()
            bad[index] = value
            self.rejected(bad)
        self.rejected([*files, '--json=short'])

    def test_real_verifier_decides_typed_empty_not_runner(self):
        load, files = self.bus_commands()
        show = 'Environment=' + ' '.join(k + '=' + v for k, v in self.env.items())
        show += '\nPassEnvironment=\nUnsetEnvironment=\nPAMName=\n'
        service = UserService(self.unit, runner=self.runner)
        for typed in ('a(sb) 0', 'a(sb) 1 "/private/file" false', 'as 0', ''):
            self.run_mock.side_effect = [
                subprocess.CompletedProcess([], 0, show, ''),
                subprocess.CompletedProcess(load, 0, f'o "{self.runner.object_path}"', ''),
                subprocess.CompletedProcess(files, 0, typed, '')]
            if typed == 'a(sb) 0':
                service.verify_environment(self.env)
            else:
                with self.assertRaises(ServiceError):
                    service.verify_environment(self.env)
        self.assertNotIn('/private/file', json.dumps(self.events))

    def test_only_runtime_enable_disable(self):
        for action in ('enable', 'disable'):
            self.runner(['systemctl', '--user', action, self.unit], capture_output=True, text=True)
            args, kwargs = self.run_mock.call_args
            self.assertEqual(args[0], ['systemctl', '--user', action, self.unit, '--runtime'])
            self.assertEqual(kwargs['env'], self.fixture.session_env)
        self.assertNotIn('swiftwall.service', json.dumps(self.events))

    def test_real_property_output_is_not_fabricated_or_logged(self):
        self.run_mock.return_value = subprocess.CompletedProcess([], 0, 'Environment=SECRET=private', '')
        result = self.runner(['systemctl', '--user', 'show', self.unit, '-p', 'Environment'],
                             capture_output=True, text=True)
        self.assertEqual(result.stdout, 'Environment=SECRET=private')
        self.assertNotIn('SECRET', json.dumps(self.events))

    def test_no_shell_setter_live_binary_or_installer_execution(self):
        for args in (
            ['omarchy', 'shell', 'reload'], ['/usr/bin/omarchy', 'theme', 'bg', 'set', 'x'],
            [str(self.sources.v01), 'daemon'], [str(self.sources.v02), 'daemon'],
            [str(self.fixture.manager.binary), 'daemon'],
            ['python3', 'scripts/install.py'], ['/bin/sh', '-c', 'true'],
        ):
            with self.subTest(argv=args):
                self.rejected(args, env=self.env)
        self.rejected(['systemctl', '--user', 'stop', self.unit], shell=True)

    def test_engine_environment_and_config_are_confined(self):
        args = [str(self.fixture.manager.binary), 'status']
        self.runner(args, env=self.env, capture_output=True, text=True)
        self.run_mock.assert_called_once()
        for key in self.env:
            if key != 'SWIFTWALL_CONFIG':
                self.rejected(args, env={**self.env, key: '/outside'})
        self.rejected(args, env={**self.env, 'LD_PRELOAD': '/outside/library.so'})
        self.rejected(args, env={**self.env, 'SWIFTWALL_OTHER': '/outside'})
        for config in ('/outside', str(self.home / '../outside'), 'relative'):
            self.rejected(args, env={**self.env, 'SWIFTWALL_CONFIG': config})
        self.runner([str(self.fixture.bundle / 'bin/swiftwall'), 'check-config'],
                    env={**self.env, 'SWIFTWALL_CONFIG': str(self.home / 'candidate')},
                    capture_output=True, text=True)

    def test_health_fault_is_explicit_and_cleared_by_real_stop(self):
        self.runner.readiness_fault = True
        self.runner(['systemctl', '--user', 'start', self.unit], capture_output=True, text=True)
        args = [str(self.fixture.manager.binary), 'request',
                json.dumps({'v': 1, 'id': 'deployment-health', 'op': 'status'})]
        with patch.object(rehearsal.time, 'sleep') as sleep:
            result = self.runner(args, env=self.env, capture_output=True, text=True, timeout=.25)
        sleep.assert_called_once_with(.25)
        self.assertEqual(self.events[-1][1]['injected_readiness_delay'], .25)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(self.runner.injected)
        self.runner(['systemctl', '--user', 'stop', self.unit], capture_output=True, text=True)
        self.assertEqual(self.runner(args, env=self.env, capture_output=True, text=True).returncode, 0)
        self.assertGreaterEqual(self.run_mock.call_count, 4)  # Even masked probes really execute.

    def test_unit_uses_private_absolute_exec_and_explicit_environment(self):
        text = rehearsal.unit_text(self.home, self.env, self.unit.replace('.service', '.target'))
        self.assertIn(f'ExecStart={self.home}/.local/bin/swiftwall daemon\n', text)
        self.assertIn(f'Environment="HOME={self.home}"', text)
        self.assertIn('Restart=no\n', text)
        self.assertIn('ProtectHome=yes\n', text)
        self.assertNotIn('%h', text)
        self.assertNotIn('graphical-session.target', text)
        for bad in ('/home/real', '/tmp/sws-x/home%h', '/tmp/sws-x/home\nExecStart=/bin/sh'):
            with self.assertRaises(RuntimeError):
                rehearsal.unit_text(Path(bad), self.env, self.unit.replace('.service', '.target'))
        with self.assertRaises(RuntimeError):
            rehearsal.unit_text(self.home, {**self.env, 'HOME': '%h'}, self.unit.replace('.service', '.target'))

    def generated_unit(self):
        manager = SetupManager(home=self.home, bundle=self.fixture.bundle, env=self.env,
                               unit=self.unit, runner=self.runner)
        installed = manager._unit_bytes(rehearsal.engine.UNIT_SOURCE.read_bytes())
        intended = {key: self.env[key] for key in rehearsal.CONTRACT_KEYS}
        return installed, intended

    def test_generated_environment_is_copied_verbatim_without_fixture_defaults(self):
        installed, intended = self.generated_unit()
        text = rehearsal.generated_runtime_unit(self.home, self.unit.replace('.service', '.target'),
                                                 installed, intended)
        original = [line for line in installed.decode().splitlines() if line.startswith('Environment=')]
        copied = [line for line in text.splitlines() if line.startswith('Environment=')]
        self.assertEqual(copied, original)
        self.assertEqual(len(copied), 7)
        self.assertNotIn('Environment="PATH=', text)
        self.assertIn(f'ExecStart={self.home}/.local/bin/swiftwall daemon\n', text)
        self.assertIn('ExecStart=%h/.local/bin/swiftwall daemon\n', installed.decode())
        self.assertNotIn('graphical-session.target', text)
        self.assertIn('Restart=no\n', text)
        self.assertIn('generated-unit', rehearsal.CASES)
        self.run_mock.assert_not_called()

    def test_generated_contract_refuses_missing_extra_duplicate_or_wrong_values(self):
        installed, intended = self.generated_unit()
        lines = installed.splitlines(keepends=True)
        variants = [installed + b'Environment="UNKNOWN=/outside"\n',
                    installed + b'Environment="HOME=/outside"\n',
                    installed + b'Environment ="UNKNOWN=/outside"\n',
                    installed + b'EnvironmentFile =/outside\n',
                    installed.replace(b'\n\n[Service]\nEnvironment=', b'\n\n[Unit]\nEnvironment='),
                    installed.replace(b'ExecStart=%h/.local/bin/swiftwall daemon', b'ExecStart=/bin/sh'),
                    installed.replace(str(self.home).encode(), b'/outside')]
        variants.extend(b''.join(line for line in lines if line != removed)
                        for removed in lines if removed.startswith(b'Environment='))
        for bad in variants:
            with self.subTest(unit=bad):
                with self.assertRaises(RuntimeError):
                    rehearsal.generated_runtime_unit(self.home, self.unit.replace('.service', '.target'),
                                                     bad, intended)
        self.run_mock.assert_not_called()

    def test_metadata_candidate_changes_only_description_and_retains_real_version(self):
        original_bytes = (rehearsal.ROOT / 'manifest.json').read_bytes()
        original = json.loads(original_bytes)
        changed = json.loads(rehearsal.descriptive_metadata(original_bytes))
        self.assertNotEqual(changed['description'], original['description'])
        self.assertEqual(changed['version'], original['version'])
        changed['description'] = original['description']
        self.assertEqual(changed, original)
        self.assertEqual((rehearsal.ROOT / 'manifest.json').read_bytes(), original_bytes)
        self.assertIn('metadata-only', rehearsal.CASES)
        self.assertEqual(len(rehearsal.CASES), 8)
        self.run_mock.assert_not_called()

    def test_metadata_candidate_refuses_missing_or_nonstring_description(self):
        for metadata in ({}, {'description': None}, {'description': False}, {'description': ['text']}):
            with self.subTest(metadata=metadata):
                with self.assertRaisesRegex(RuntimeError, 'description must be a string'):
                    rehearsal.descriptive_metadata(json.dumps(metadata).encode())
        self.run_mock.assert_not_called()

    def test_private_simulator_mutates_then_recovers_without_commands(self):
        controller = rehearsal.PrivateActivation(self.home)
        before = controller.snapshot()
        controller.prepare()
        controller.fail = True
        with self.assertRaisesRegex(RuntimeError, 'injected private activation'):
            controller.activate()
        self.assertTrue(controller.snapshot()['registry'][rehearsal.PLUGIN_ID])
        controller.deactivate()
        after = controller.snapshot()
        self.assertFalse(after['registry'][rehearsal.PLUGIN_ID])
        self.assertEqual(after['config'], before['config'])
        self.assertEqual(after['persisted'], before['persisted'])
        self.assertEqual(controller.path.stat().st_mode & 0o777, 0o600)
        self.run_mock.assert_not_called()

    def test_identity_health_checks_are_inherited_not_mocked(self):
        for name in ('healthy', 'running_environment', 'socket_owner', 'running_executable',
                     '_start', '_files', 'rollback', 'uninstall'):
            self.assertIs(getattr(rehearsal.FaultManager, name), getattr(SetupManager, name))

    def test_explicit_opt_in_and_preflight_refusal(self):
        with patch.object(rehearsal, 'preflight') as check:
            with self.assertRaises(SystemExit):
                rehearsal.main(source_args(self.sources))
            check.assert_not_called()
        with patch.object(rehearsal, 'preflight', side_effect=RuntimeError('manager unavailable')) as check:
            with patch.object(rehearsal, 'Rehearsal') as fixture:
                with self.assertRaisesRegex(RuntimeError, 'manager unavailable'):
                    rehearsal.main(['--run', *source_args(self.sources)])
                fixture.assert_not_called()
                check.assert_called_once_with(self.sources)
        self.run_mock.assert_not_called()

    def test_runner_refuses_production_name_even_if_fixture_is_misconfigured(self):
        self.fixture.unit = 'swiftwall.service'
        with self.assertRaisesRegex(RuntimeError, 'generated disposable'):
            rehearsal.GuardedRunner(self.fixture)
        self.run_mock.assert_not_called()

    def cleanup_fixture(self, pid):
        fixture = rehearsal.Rehearsal('fresh', self.root, self.sources)
        self.addCleanup(lambda: shutil.rmtree(fixture.root, ignore_errors=True))
        fixture.unit_path = self.root / fixture.unit
        fixture.unit_path.write_text('private unit fixture')
        fixture.unit_owned = True
        manager = patch.object(fixture, 'manager',
                               SimpleNamespace(service=SimpleNamespace(state=lambda: {'MainPID': str(pid)})))
        manager.start()
        self.addCleanup(manager.stop)
        return fixture

    def test_cleanup_after_failure_stops_disables_and_deletes_only_owned_paths(self):
        fixture = self.cleanup_fixture(0)
        sentinel = self.root / 'unrelated'
        sentinel.write_text('keep')
        with patch.object(fixture, 'ctl') as ctl:
            fixture.cleanup()
        self.assertEqual([call.args[0] for call in ctl.call_args_list], ['stop', 'disable', 'daemon-reload'])
        self.assertFalse(fixture.root.exists())
        self.assertFalse(fixture.unit_path.exists())
        self.assertEqual(sentinel.read_text(), 'keep')
        self.run_mock.assert_not_called()

    def test_cleanup_exports_only_regular_private_evidence(self):
        fixture = self.cleanup_fixture(0)
        source = fixture.home / 'manager-evidence'
        source.mkdir(mode=0o700)
        (source / 'events.jsonl').write_text('{"event":"private-test"}\n')
        with patch.object(fixture, 'ctl'):
            fixture.cleanup()
        exported = self.root / 'fresh/events.jsonl'
        self.assertEqual(exported.read_text(), '{"event":"private-test"}\n')
        self.assertEqual(exported.stat().st_mode & 0o777, 0o600)
        self.assertFalse(fixture.root.exists())
        self.run_mock.assert_not_called()

    def test_export_failure_does_not_prevent_shutdown_or_fixture_cleanup(self):
        fixture = self.cleanup_fixture(0)
        source = fixture.home / 'manager-evidence'
        source.mkdir(mode=0o700)
        (source / 'linked-evidence').symlink_to(self.root / 'outside')
        with patch.object(fixture, 'ctl') as ctl:
            with self.assertRaisesRegex(RuntimeError, 'evidence export failed; daemon/fixture cleaned'):
                fixture.cleanup()
        self.assertEqual([call.args[0] for call in ctl.call_args_list], ['stop', 'disable', 'daemon-reload'])
        self.assertFalse(fixture.root.exists())
        self.assertFalse(fixture.unit_path.exists())
        self.run_mock.assert_not_called()

    def test_cleanup_refuses_to_delete_binary_when_daemon_may_still_run(self):
        fixture = self.cleanup_fixture(123)
        with patch.object(fixture, 'ctl', side_effect=RuntimeError('injected stop failure')):
            with self.assertRaisesRegex(RuntimeError, 'Daemon may still run'):
                fixture.cleanup()
        self.assertTrue(fixture.unit_path.exists())
        self.assertTrue(fixture.root.exists())
        self.run_mock.assert_not_called()

    def test_png_is_generated(self):
        self.assertTrue(rehearsal.png().startswith(b'\x89PNG\r\n\x1a\n'))


if __name__ == '__main__':
    unittest.main()
