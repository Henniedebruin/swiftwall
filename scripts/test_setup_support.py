"""Isolated lifecycle tests; no live systemd, wallpaper setter or Omarchy calls.

Real engine/plugin file transactions and UserService are exercised. The executable
is a test protocol/config fixture, NOT the Rust daemon. Kernel peer/proc checks
are explicitly replaced only in FixtureManager; these tests are not live smoke.
"""

import base64
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import install_support as engine
import plugin_support as plugin
from plugin_activation import PLUGIN_ID, ActivationOutcome, without_plugin
from setup_support import (
    ACTIVATION,
    SetupError,
    SetupManager,
    plugin_update_action,
    systemd_quote,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = b'interval_seconds = 300\nselection = "shuffle"\nfolders = []\n'
PROGRAM = '''#!/usr/bin/python3
import json, os, pathlib, sys, tomllib
VERSION = "VERSION_TOKEN"
if sys.argv[1] == '--version':
    print('swiftwall ' + VERSION)
elif sys.argv[1] == 'check-config':
    path = pathlib.Path(os.environ['SWIFTWALL_CONFIG'])
    assert path.stat().st_mode & 0o777 == 0o600
    tomllib.loads(path.read_text())
else:
    request = json.loads(sys.argv[2]) if sys.argv[1] == 'request' else {'op': 'status'}
    print(json.dumps({'ok': True, 'protocol_version': 1, 'daemon_version': VERSION,
        'status': {'scanning': False, 'persistence_pending': False,
                   'observed_wallpaper': os.readlink(os.environ.get('SWIFTWALL_BACKGROUND_LINK', str(pathlib.Path(os.environ['HOME']) / '.local/state/omarchy/current/background')))}}))
'''


class Transport:
    def __init__(self):
        self.active = False
        self.enabled = False
        self.loaded = False
        self.calls = []
        self.env: dict[str, str] = {}
        self.loaded_environment: dict[str, str] = {}
        self.manager_environment: dict[str, str] = {}
        self.running_env: dict[str, str] = {}
        self.extra_properties: dict[str, str] = {}
        self.manager_query_failure = False
        self.fail_hello: bool | str = False
        self.fail_reload = False
        self.secret_query = False
        self.on_start: Callable[[], None] | None = None
        self.fail_stop = 0
        self.fail_start = False
        self.fail_enable = False
        self.require_unit_for_disable = False
        self.interrupt_after: dict[str, int] = {}

    def _load_unit(self):
        # Model the generated on-disk unit, NOT the caller's CLI environment.
        # The selected name is private; this transport never invokes systemd.
        path = Path(self.env['HOME']) / engine.FILES['unit']
        self.loaded = path.exists()
        self.loaded_environment = {}
        if self.loaded:
            for line in path.read_text().splitlines():
                if line.startswith('Environment='):
                    for word in shlex.split(line.partition('=')[2]):
                        key, value = word.replace('%%', '%').split('=', 1)
                        self.loaded_environment[key] = value

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        if args[0] != 'systemctl':
            matches_version = self.fail_hello is True or f'VERSION = "{self.fail_hello}"' in Path(args[0]).read_text()
            if self.fail_hello and matches_version and 'request' in args and json.loads(args[-1])['op'] == 'hello':
                self.fail_hello = False
                return subprocess.CompletedProcess(args, 0, '{"ok":false}', '')
            return subprocess.run(args, check=False, **kwargs)
        action = args[2]
        if action == 'show-environment':
            if self.manager_query_failure:
                raise RuntimeError('Environment=API_SECRET=do-not-leak')
            output = '\n'.join(k + '=' + shlex.quote(v) for k, v in self.manager_environment.items())
            return subprocess.CompletedProcess(args, 0, output, '')
        if action == 'show':
            if not self.loaded:
                self._load_unit()
            if self.secret_query:
                raise RuntimeError('Environment=API_SECRET=do-not-leak')
            if 'Environment' in args:
                entries = shlex.join(k + '=' + v for k, v in self.loaded_environment.items())
                values = {'Environment': entries, 'EnvironmentFiles': '', 'PassEnvironment': '', 'UnsetEnvironment': '', 'PAMName': ''}
                values.update(self.extra_properties)
                requested = {args[i + 1] for i, arg in enumerate(args) if arg == '-p'}
                output = '\n'.join(k + '=' + v for k, v in values.items() if k in requested) + '\n'
            else:
                output = '\n'.join(f'{k}={v}' for k, v in {
                    'LoadState': 'loaded' if self.loaded else 'not-found',
                    'ActiveState': 'active' if self.active else 'inactive',
                    'SubState': 'running' if self.active else 'dead',
                    'MainPID': str(os.getpid()) if self.active else '0', 'NRestarts': '0',
                    'UnitFileState': 'enabled' if self.enabled else 'disabled', 'Result': 'success'}.items())
            return subprocess.CompletedProcess(args, 0, output, '')
        if action == 'start':
            self.running_env = self.manager_environment | self.loaded_environment
            self.active = True
            if self.fail_start:
                self.fail_start = False
                return subprocess.CompletedProcess(args, 1, '', 'fixture start failure after delivery')
            if self.on_start:
                self.on_start()
        elif action == 'stop':
            if self.fail_stop:
                self.fail_stop -= 1
                return subprocess.CompletedProcess(args, 1, '', 'fixture stop failure; still active')
            self.active = False
        elif action == 'enable':
            self.enabled = True
            if self.fail_enable:
                self.fail_enable = False
                return subprocess.CompletedProcess(args, 1, '', 'fixture enable committed but response failed')
        elif action == 'disable':
            if self.require_unit_for_disable and not (Path(self.env['HOME']) / engine.FILES['unit']).exists():
                return subprocess.CompletedProcess(args, 1, '', 'fixture unit already removed')
            self.enabled = False
        elif action == 'daemon-reload':
            if self.fail_reload:
                self.fail_reload = False
                return subprocess.CompletedProcess(args, 1, '', 'fixture reload failure')
            self._load_unit()
        if self.interrupt_after.get(action, 0):
            self.interrupt_after[action] -= 1
            raise KeyboardInterrupt(f'fixture SIGINT after {action}')
        return subprocess.CompletedProcess(args, 0, '', '')


class Activation:
    """In-memory private shell transport with the hardened controller's surface."""
    def __init__(self):
        self.value = {'registry': {}, 'config': {'bar': {'layout': {'right': [{'id': 'clock'}]}}, 'theme': 'before'},
                      'persisted': {'theme': 'before'}}
        self.baseline = None
        self.enable_attempted = False
        self.disable_attempted = False
        self.activations = 0
        self.fail = False
        self.fail_disable = False

    def snapshot(self):
        return deepcopy(self.value)

    def prepare(self):
        self.baseline = self.snapshot()
        if self.value['registry'].get(PLUGIN_ID):
            raise RuntimeError('pre-existing activation')

    def activate(self):
        self.activations += 1
        self.enable_attempted = True
        self.value['registry'][PLUGIN_ID] = True
        self.value['config']['bar']['layout']['right'].insert(0, {'id': PLUGIN_ID})
        if self.fail:
            raise RuntimeError('fixture activation failure after mutation')
        return ActivationOutcome('success', 'ok', 'fixture')

    def deactivate(self):
        self.disable_attempted = True
        if self.fail_disable:
            raise RuntimeError('fixture unknown shell state')
        self.value['registry'][PLUGIN_ID] = False
        self.value['config'] = without_plugin(self.value['config'])
        self.value['persisted'] = without_plugin(self.value['persisted'])


class FixtureManager(SetupManager):
    """Only kernel/live-service identity boundaries are substituted."""
    def running_environment(self):
        assert isinstance(self.runner, Transport)
        self._verify_effective_environment(self.runner.running_env)
        self.record('fixture-running-environment')

    def socket_owner(self):
        self.record('fixture-socket-owner')

    def running_executable(self):
        self.record('fixture-running-executable')


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        (self.home / 'runtime').mkdir(mode=0o700)
        self.bundle = self.root / 'bundle'
        self.bundle.mkdir()
        for name in plugin.FILES:
            destination = self.bundle / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, destination)
        (self.bundle / 'bin').mkdir()
        (self.bundle / 'packaging').mkdir()
        shutil.copyfile(engine.UNIT_SOURCE, self.bundle / 'packaging/swiftwall.service')
        (self.bundle / 'release.json').write_text('{}')  # Upstream verifier is outside this API.
        self.version('0.2.0')
        setter = self.home / 'fake-setter'
        setter.write_text('#!/bin/sh\nexit 0\n')
        setter.chmod(0o700)
        self.env = {'HOME': str(self.home), 'PATH': '/usr/bin',
                    'XDG_RUNTIME_DIR': str(self.home / 'runtime'),
                    'SWIFTWALL_OMARCHY_COMMAND': str(setter)}
        self.transport = Transport()
        self.transport.env = self.env
        self.transport.manager_environment = dict(self.env)
        self.activation = Activation()
        self.manager = self.new_manager()
        self.manager.background.parent.mkdir(parents=True)
        self.image = self.home / 'precious.png'
        self.image.write_bytes(b'image fixture, never opened by lifecycle')
        self.manager.background.symlink_to(self.image)

    def new_manager(self, cls: type[SetupManager] = FixtureManager):
        return cls(home=self.home, bundle=self.bundle, env=self.env,
                   unit='swiftwall-setup-private-test.service', runner=self.transport,
                   activation_controller=self.activation, evidence=self.home / 'evidence')

    def version(self, version):
        path = self.bundle / 'bin/swiftwall'
        path.write_text(PROGRAM.replace('VERSION_TOKEN', version))
        path.chmod(0o755)

    def release_version(self, version):
        """Release-shaped fixture: engine, plugin and release metadata agree."""
        self.version(version)
        path = self.bundle / 'manifest.json'
        manifest = json.loads(path.read_bytes())
        manifest['version'] = version
        path.write_text(json.dumps(manifest, indent=2) + '\n')
        (self.bundle / 'release.json').write_text(json.dumps({
            'version': version, 'files': {'bin/swiftwall': {
                'sha256': hashlib.sha256((self.bundle / 'bin/swiftwall').read_bytes()).hexdigest()}}}))

    def install(self, widget=False):
        return self.manager.install(CONFIG, enable_widget=widget)

    def state(self, **changes):
        data = {'version': 1, 'paused': True,
                'carousel': {'paths': ['a', 'b', 'c'], 'bag': ['c', 'a'], 'history': ['a', 'b'], 'cursor': 1}}
        data.update(changes)
        self.manager.state.parent.mkdir(parents=True, exist_ok=True)
        self.manager.state.write_text(json.dumps(data))
        return self.manager.state.read_bytes()

    def tree(self):
        return sorted((str(p.relative_to(self.home)), p.lstat().st_mode,
                       os.readlink(p) if p.is_symlink() else p.read_bytes() if p.is_file() else None)
                      for p in self.home.rglob('*'))

    def test_constructor_and_plan_read_only_no_runner_or_evidence(self):
        before = self.tree()
        manager = self.new_manager(SetupManager)
        result = manager.plan('install')
        self.assertFalse(result['engine_owned'])
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(manager.evidence.exists())

    def test_fresh_install_checks_before_enable_real_file_installers(self):
        result = self.install()
        engine.verify_owned(self.home, engine.load_manifest(self.home))
        plugin.run('verify', self.home)
        self.assertTrue(self.transport.enabled)
        self.assertTrue(self.transport.active)
        self.assertTrue(Path(result['backup']).is_file())
        self.assertEqual(self.manager.config.read_bytes(), CONFIG)
        self.assertEqual(self.manager.config.stat().st_mode & 0o777, 0o600)
        calls = [a for a, _ in self.transport.calls]
        enable = calls.index(['systemctl', '--user', 'enable', self.manager.unit])
        hello = next(i for i, a in enumerate(calls) if 'request' in a and json.loads(a[-1])['op'] == 'hello')
        self.assertLess(hello, enable)
        check = next((a, kw) for a, kw in self.transport.calls if 'check-config' in a)
        self.assertNotEqual(check[1]['env']['SWIFTWALL_CONFIG'], str(self.manager.config))
        self.assertFalse(Path(check[1]['env']['SWIFTWALL_CONFIG']).exists())
        self.assertEqual(self.manager._wallpaper(), str(self.image))

    def test_first_install_rollback_removes_owned_files_only(self):
        self.install(widget=True)
        self.assertEqual(self.activation.activations, 1)
        self.assertTrue(self.manager.rollback()['restored'])
        self.assertIsNone(engine.load_manifest(self.home))
        self.assertFalse((self.home / plugin.DESTINATION).exists())
        self.assertFalse(self.transport.active)
        self.assertFalse(self.transport.enabled)
        self.assertTrue(self.image.exists())
        self.assertFalse(self.manager.rollback()['available'])

    def test_identical_plugin_update_does_not_touch_plugin_inodes(self):
        self.install(widget=True)
        state = self.state()
        payload = self.home / plugin.DESTINATION / 'plugin/Service.qml'
        inode = payload.stat().st_ino
        metadata = (self.home / plugin.MANIFEST).read_bytes()
        self.version('0.2.1')
        self.manager.update()
        self.assertEqual(payload.stat().st_ino, inode)
        self.assertEqual((self.home / plugin.MANIFEST).read_bytes(), metadata)
        self.assertEqual(self.manager.state.read_bytes(), state)
        self.assertEqual(self.activation.activations, 1)
        self.assertEqual(self.manager._version(), '0.2.1')

    def test_real_version_update_allows_only_metadata_without_reactivating_widget(self):
        self.release_version('0.2.0')
        self.install(widget=True)
        state = self.state(paused=False)
        original = plugin.load_manifest(self.home)
        assert original is not None
        activation = self.activation.snapshot()
        receipt = (self.home / ACTIVATION).read_bytes()
        self.release_version('0.3.0')
        path = self.bundle / 'manifest.json'
        manifest = json.loads(path.read_bytes())
        manifest.update(name='SwiftWall carousel', description='Updated description',
                        author='SwiftWall contributors', license='GPL-3.0-only')
        manifest['barWidget'].update(displayName='Carousel', description='Updated widget description',
                                     category='Desktop', aliases=['carousel', 'wallpapers'])
        path.write_text(json.dumps(manifest, indent=2) + '\n')
        before = self.tree()
        self.transport.calls.clear()
        plan = self.manager.plan('update')
        self.assertEqual(plan['plugin'], 'metadata-only update')
        self.assertEqual(plan['plugin_version'], '0.2.0')
        self.assertFalse(self.transport.calls)
        self.assertEqual(self.tree(), before)
        with (patch.object(self.activation, 'activate', side_effect=AssertionError('No repeated enable')),
              patch.object(self.activation, 'deactivate', side_effect=AssertionError('No disable'))):
            report = self.manager.update()
        self.assertEqual(report['plugin'], 'metadata-only update')
        current = plugin.load_manifest(self.home)
        assert current is not None
        plugin.verify_owned(self.home, current)
        self.assertEqual(current['previous'], original['current'])
        for name in plugin.FILES[1:]:
            self.assertEqual(current['current'][name], original['current'][name])
        self.assertEqual(json.loads((self.home / plugin.DESTINATION / 'manifest.json').read_bytes()), manifest)
        self.assertEqual(self.manager._version(), '0.3.0')
        self.assertEqual(self.manager.plan('status')['plugin_version'], '0.3.0')
        self.assertEqual(self.manager.config.read_bytes(), CONFIG)
        self.assertEqual(self.manager.state.read_bytes(), state)
        self.assertEqual(self.activation.snapshot(), activation)
        self.assertEqual(self.activation.activations, 1)
        self.assertFalse(self.activation.disable_attempted)
        self.assertEqual((self.home / ACTIVATION).read_bytes(), receipt)
        self.assertTrue(self.transport.active)
        self.assertTrue(self.transport.enabled)
        self.assertEqual(self.manager._wallpaper(), str(self.image))
        self.assertFalse(any(a[0] == 'systemctl' and a[2] in ('enable', 'disable') for a, _ in self.transport.calls))
        # Identical reinstall must leave the metadata rollback generation intact.
        root = self.home / plugin.DESTINATION
        inodes = {name: (root / name).stat().st_ino for name in plugin.FILES}
        metadata = (self.home / plugin.MANIFEST).read_bytes()
        self.assertEqual(self.manager.plan('update')['plugin'], 'identical; untouched')
        self.manager.update()
        self.assertEqual((self.home / plugin.MANIFEST).read_bytes(), metadata)
        self.assertEqual({name: (root / name).stat().st_ino for name in plugin.FILES}, inodes)
        self.assertEqual(self.activation.activations, 1)

    def test_owned_dev_to_stable_metadata_update_preserves_state_without_reenable(self):
        self.release_version('0.2.0-dev')
        self.install(widget=True)
        config = CONFIG + b'# preserved dev installation config\n'
        self.manager.config.write_bytes(config)
        state = self.state(paused=True)
        original = plugin.load_manifest(self.home)
        assert original is not None
        activation = self.activation.snapshot()
        receipt = (self.home / ACTIVATION).read_bytes()
        self.release_version('0.2.0')
        before = self.tree()
        self.transport.calls.clear()
        plan = self.manager.plan('update')
        self.assertEqual(plan['plugin'], 'metadata-only update')
        self.assertEqual(plan['plugin_version'], '0.2.0-dev')
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.transport.calls)
        with (patch.object(self.activation, 'activate', side_effect=AssertionError('No repeated enable')),
              patch.object(self.activation, 'deactivate', side_effect=AssertionError('No disable'))):
            report = self.manager.update()
        self.assertEqual(report['plugin'], 'metadata-only update')
        current = plugin.load_manifest(self.home)
        assert current is not None
        engine.verify_owned(self.home, engine.load_manifest(self.home))
        plugin.verify_owned(self.home, current)
        self.assertEqual(current['previous'], original['current'])
        for name in plugin.FILES[1:]:
            self.assertEqual(current['current'][name], original['current'][name])
        self.assertEqual((self.home / plugin.DESTINATION / 'manifest.json').read_bytes(),
                         (self.bundle / 'manifest.json').read_bytes())
        self.assertEqual(self.manager._version(), '0.2.0')
        self.assertEqual(self.manager.plan('status')['plugin_version'], '0.2.0')
        self.assertEqual(self.manager.config.read_bytes(), config)
        self.assertEqual(self.manager.state.read_bytes(), state)
        self.assertEqual(self.activation.snapshot(), activation)
        self.assertEqual((self.home / ACTIVATION).read_bytes(), receipt)
        self.assertEqual(self.activation.activations, 1)
        self.assertFalse(self.activation.disable_attempted)
        self.assertTrue(self.transport.active)
        self.assertTrue(self.transport.enabled)
        self.assertEqual(self.manager._wallpaper(), str(self.image))
        self.assertFalse(any(a[0] == 'systemctl' and a[2] in ('enable', 'disable')
                             for a, _ in self.transport.calls))

    def test_owned_dev_to_stable_rollback_restores_receipts_and_latest_valid_history(self):
        self.release_version('0.2.0-dev')
        self.install(widget=True)
        config = CONFIG + b'# original dev installation config\n'
        self.manager.config.write_bytes(config)
        self.state(paused=True)
        original_engine = engine.load_manifest(self.home)
        original_plugin = plugin.load_manifest(self.home)
        receipts = {path: (self.home / path).read_bytes()
                    for path in (engine.MANIFEST, plugin.MANIFEST, ACTIVATION)}
        activation = self.activation.snapshot()
        self.release_version('0.2.0')
        self.manager.update()
        latest = self.state(paused=False, carousel={
            'paths': ['a', 'b', 'c'], 'bag': ['b', 'a'],
            'history': ['a', 'b', 'c', 'a'], 'cursor': 3})
        self.manager.config.write_bytes(CONFIG + b'# changed after stable update\n')
        self.transport.calls.clear()
        with (patch.object(self.activation, 'activate', side_effect=AssertionError('No repeated enable')),
              patch.object(self.activation, 'deactivate', side_effect=AssertionError('No disable'))):
            self.assertTrue(self.manager.rollback()['restored'])
        self.assertEqual(engine.load_manifest(self.home), original_engine)
        self.assertEqual(plugin.load_manifest(self.home), original_plugin)
        engine.verify_owned(self.home, original_engine)
        plugin.verify_owned(self.home, original_plugin)
        for path, receipt in receipts.items():
            self.assertEqual((self.home / path).read_bytes(), receipt)
        self.assertEqual(self.manager._version(), '0.2.0-dev')
        self.assertEqual(self.manager.plan('status')['plugin_version'], '0.2.0-dev')
        self.assertEqual(self.manager.config.read_bytes(), config)
        self.assertEqual(self.manager.state.read_bytes(), latest)
        self.assertEqual(self.activation.snapshot(), activation)
        self.assertEqual(self.activation.activations, 1)
        self.assertFalse(self.activation.disable_attempted)
        self.assertTrue(self.transport.active)
        self.assertTrue(self.transport.enabled)
        self.assertEqual(self.manager._wallpaper(), str(self.image))
        self.assertFalse(any(a[0] == 'systemctl' and a[2] in ('enable', 'disable')
                             for a, _ in self.transport.calls))

    def test_owned_dev_to_stable_changed_qml_refused_before_mutation(self):
        self.release_version('0.2.0-dev')
        self.install(widget=True)
        self.state()
        self.release_version('0.2.0')
        path = self.bundle / 'plugin/Service.qml'
        path.write_bytes(path.read_bytes() + b'\n// changed dev-to-stable QML fixture\n')
        before = self.tree()
        activation = self.activation.snapshot()
        self.transport.calls.clear()
        with (patch.object(self.activation, 'activate', side_effect=AssertionError('No repeated enable')),
              patch.object(self.activation, 'deactivate', side_effect=AssertionError('No disable'))):
            for operation in (lambda: self.manager.plan('update'), self.manager.update):
                with self.subTest(operation=operation.__name__):
                    with self.assertRaisesRegex(SetupError, 'QML URL caching'):
                        operation()
                    self.assertEqual(self.tree(), before)
                    self.assertFalse(self.transport.calls)
                    self.assertEqual(self.activation.snapshot(), activation)
                    self.assertEqual(self.activation.activations, 1)
                    self.assertFalse(self.activation.disable_attempted)
                    self.assertTrue(self.transport.active)
                    self.assertTrue(self.transport.enabled)

    def test_v02_license_and_description_upgrade_without_version_change_is_supported(self):
        self.release_version('0.2.0')
        path = self.bundle / 'manifest.json'
        old = json.loads(path.read_bytes())
        old.pop('license', None)
        old['description'] = 'Old v0.2 description'
        path.write_text(json.dumps(old))
        self.install(widget=True)
        engine_before = engine.load_manifest(self.home)
        old['license'] = 'MIT'
        old['description'] = 'Updated upstream description'
        old['barWidget']['description'] = 'Updated upstream widget description'
        path.write_text(json.dumps(old))
        self.assertEqual(self.manager.plan('update')['plugin'], 'metadata-only update')
        self.manager.update()
        self.assertEqual(engine.load_manifest(self.home), engine_before)
        installed = json.loads((self.home / plugin.DESTINATION / 'manifest.json').read_bytes())
        self.assertEqual(installed, old)
        self.assertEqual(self.activation.activations, 1)
        self.assertFalse(self.activation.disable_attempted)

    def test_metadata_update_rollback_restores_both_generations_and_keeps_latest_history(self):
        self.release_version('0.2.0')
        self.install(widget=True)
        self.state()
        original_engine = engine.load_manifest(self.home)
        original_plugin = plugin.load_manifest(self.home)
        activation = self.activation.snapshot()
        self.release_version('0.3.0')
        self.manager.update()
        latest = self.state(paused=False, carousel={
            'paths': ['a', 'b'], 'bag': ['a'], 'history': ['a', 'b', 'a'], 'cursor': 2})
        with (patch.object(self.activation, 'activate', side_effect=AssertionError('No repeated enable')),
              patch.object(self.activation, 'deactivate', side_effect=AssertionError('No disable'))):
            self.manager.rollback()
        self.assertEqual(engine.load_manifest(self.home), original_engine)
        self.assertEqual(plugin.load_manifest(self.home), original_plugin)
        plugin.verify_owned(self.home, original_plugin)
        self.assertEqual(self.manager.state.read_bytes(), latest)
        self.assertEqual(self.manager.config.read_bytes(), CONFIG)
        self.assertEqual(self.activation.snapshot(), activation)
        self.assertTrue(self.transport.active)
        self.assertTrue(self.transport.enabled)

    def test_engine_failure_before_metadata_publication_keeps_plugin_untouched(self):
        self.release_version('0.2.0')
        self.install(widget=True)
        state = self.state()
        original_engine = engine.load_manifest(self.home)
        original_plugin = plugin.load_manifest(self.home)
        root = self.home / plugin.DESTINATION
        inodes = {name: (root / name).stat().st_ino for name in plugin.FILES}
        self.release_version('0.3.0')
        self.transport.fail_hello = '0.3.0'
        with self.assertRaisesRegex(SetupError, 'original owned files/service recovered'):
            self.manager.update()
        self.assertEqual(engine.load_manifest(self.home), original_engine)
        self.assertEqual(plugin.load_manifest(self.home), original_plugin)
        self.assertEqual({name: (root / name).stat().st_ino for name in plugin.FILES}, inodes)
        self.assertEqual(self.manager.state.read_bytes(), state)
        self.assertEqual(self.activation.activations, 1)
        self.assertFalse(self.activation.disable_attempted)
        self.assertTrue(self.transport.active)

    def test_failure_after_metadata_commit_restores_original_plugin_version_and_previous(self):
        self.release_version('0.2.0')
        self.install(widget=True)
        self.state()
        self.release_version('0.2.1')
        self.manager.update()  # Keep a nonempty plugin previous generation too.
        original_engine = engine.load_manifest(self.home)
        original_plugin = plugin.load_manifest(self.home)
        original_metadata = (self.home / plugin.MANIFEST).read_bytes()
        self.release_version('0.3.0')
        transaction = plugin.transaction
        failed = []
        latest = []
        def fail(home, before, current, previous):
            transaction(home, before, current, previous)
            if not failed:
                failed.append(True)
                latest.append(self.state(paused=False, carousel={
                    'paths': ['a', 'b', 'c'], 'bag': ['b'],
                    'history': ['a', 'b', 'c'], 'cursor': 2}))
                self.assertEqual(json.loads((home / plugin.DESTINATION / 'manifest.json').read_bytes())['version'], '0.3.0')
                raise RuntimeError('fixture engine failure detected after metadata commit')
        with (patch('plugin_support.transaction', side_effect=fail),
              patch.object(self.activation, 'activate', side_effect=AssertionError('No repeated enable')),
              patch.object(self.activation, 'deactivate', side_effect=AssertionError('No disable')),
              self.assertRaisesRegex(SetupError, 'original owned files/service recovered')):
            self.manager.update()
        self.assertEqual(engine.load_manifest(self.home), original_engine)
        self.assertEqual(plugin.load_manifest(self.home), original_plugin)
        self.assertEqual((self.home / plugin.MANIFEST).read_bytes(), original_metadata)
        self.assertEqual(self.manager.plan('status')['plugin_version'], '0.2.1')
        self.assertEqual(self.manager.state.read_bytes(), latest[0])
        self.assertTrue(self.transport.active)
        self.assertTrue(self.transport.enabled)

    def test_structural_manifest_changes_refused_before_any_mutation(self):
        self.release_version('0.2.0')
        self.install(widget=True)
        original = json.loads((self.bundle / 'manifest.json').read_bytes())
        self.version('0.3.0')
        mutations = {
            'id': lambda m: m.update(id='other.plugin'),
            'schemaVersion': lambda m: m.update(schemaVersion=2),
            'entryPoints': lambda m: m['entryPoints'].update(service='plugin/Other.qml'),
            'kinds': lambda m: m.update(kinds=['bar-widget']),
            'unknown top-level field': lambda m: m.update(capabilities=['new-behavior']),
            'allowMultiple': lambda m: m['barWidget'].update(allowMultiple=True),
            'defaultSection': lambda m: m['barWidget'].update(defaultSection='left'),
            'defaults': lambda m: m['barWidget']['defaults'].update(showCountdown=True),
            'defaults type': lambda m: m['barWidget']['defaults'].update(showCountdown=0),
            'schema': lambda m: m['barWidget']['schema'][0].update(label='Changed schema label'),
            'unknown barWidget field': lambda m: m['barWidget'].update(extraBehavior=True),
            'removed barWidget': lambda m: m.pop('barWidget'),
            'removed defaultSection': lambda m: m['barWidget'].pop('defaultSection'),
        }
        before = self.tree()
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                manifest = deepcopy(original)
                manifest['version'] = '0.3.0'
                mutate(manifest)
                (self.bundle / 'manifest.json').write_text(json.dumps(manifest))
                self.transport.calls.clear()
                for operation in (lambda: self.manager.plan('update'), self.manager.update):
                    with self.assertRaises((SetupError, plugin.InstallError)):
                        operation()
                self.assertEqual(self.tree(), before)
                self.assertFalse(self.transport.calls)

    def test_each_of_seven_code_payload_changes_refused_despite_safe_metadata(self):
        self.release_version('0.2.0')
        self.install()
        self.release_version('0.3.0')
        before = self.tree()
        for name in plugin.FILES[1:]:
            with self.subTest(name=name):
                path = self.bundle / name
                data = path.read_bytes()
                path.write_bytes(data + b'\n// changed code fixture\n')
                self.transport.calls.clear()
                with self.assertRaisesRegex(SetupError, 'QML URL caching'):
                    self.manager.update()
                self.assertEqual(self.tree(), before)
                self.assertFalse(self.transport.calls)
                path.write_bytes(data)

    def test_generation_modes_are_checked_before_metadata_only_classification(self):
        before = plugin.source_generation(self.bundle)
        for name in plugin.FILES:
            with self.subTest(name=name):
                current = deepcopy(before)
                item = current[name]
                current[name] = plugin.record(base64.b64decode(item['bytes']), item['mode'] ^ 0o111)
                with self.assertRaises(plugin.InstallError):
                    plugin_update_action(before, current)

    def test_metadata_transaction_interrupt_restores_owned_engine_and_plugin(self):
        self.release_version('0.2.0')
        self.install(widget=True)
        self.state()
        self.release_version('0.3.0')
        def interrupt(name):
            if name == 'new-placed':
                raise KeyboardInterrupt('fixture SIGINT during metadata update')
        with patch('plugin_support.checkpoint', side_effect=interrupt):
            self.assert_interrupt_recovers(self.manager.update)
        self.assertEqual(self.activation.activations, 1)
        self.assertFalse(self.activation.disable_attempted)

    def test_update_rollback_latest_compatible_state_original_config(self):
        self.install()
        self.state()
        self.version('0.2.1')
        self.manager.update()
        latest = self.state(carousel={'history': ['latest'], 'cursor': 0}, paused=False)
        self.manager.config.write_bytes(CONFIG + b'# changed later\n')
        self.manager.rollback()
        self.assertEqual(self.manager._version(), '0.2.0')
        self.assertEqual(self.manager.config.read_bytes(), CONFIG)
        self.assertEqual(self.manager.state.read_bytes(), latest)

    def test_update_failure_auto_recovers_once_and_retains_backups(self):
        self.install()
        self.state()
        original = engine.load_manifest(self.home)
        self.version('0.2.1')
        self.transport.fail_hello = '0.2.1'
        with self.assertRaisesRegex(SetupError, 'original owned files/service recovered'):
            self.manager.update()
        self.assertEqual(engine.load_manifest(self.home), original)
        self.assertTrue(self.transport.active)
        self.assertTrue(self.transport.enabled)
        self.assertEqual(self.manager._version(), '0.2.0')
        self.assertGreaterEqual(len(list(self.manager.evidence.glob('*update-*.json'))), 2)
        self.assertTrue(list(self.manager.evidence.glob('recovery-input-*.json')))

    def test_first_install_failure_has_no_owned_engine_or_plugin(self):
        self.transport.fail_hello = True
        with self.assertRaisesRegex(SetupError, 'recovered'):
            self.install()
        self.assertIsNone(engine.load_manifest(self.home))
        self.assertIsNone(plugin.load_manifest(self.home))
        self.assertFalse(self.transport.enabled)
        self.assertFalse(self.transport.active)

    def test_activation_failure_reconciles_then_recovers_engine(self):
        self.activation.fail = True
        with self.assertRaisesRegex(SetupError, 'recovered'):
            self.install(widget=True)
        self.assertTrue(self.activation.disable_attempted)
        self.assertIsNone(engine.load_manifest(self.home))
        self.assertIsNone(plugin.load_manifest(self.home))
        self.assertFalse((self.home / ACTIVATION).exists())

    def test_ambiguous_activation_keeps_plugin_but_recovers_engine(self):
        self.activation.fail = True
        self.activation.fail_disable = True
        with self.assertRaisesRegex(SetupError, 'recovery incomplete'):
            self.install(widget=True)
        self.assertIsNone(engine.load_manifest(self.home))
        plugin.run('verify', self.home)
        self.assertTrue((self.home / ACTIVATION).exists())

    def test_differing_plugin_refused_before_any_mutation(self):
        self.install()
        path = self.bundle / 'plugin/Service.qml'
        path.write_bytes(path.read_bytes() + b'\n// different fixture\n')
        before = self.tree()
        self.transport.calls.clear()
        for operation in (lambda: self.manager.plan('update'), self.manager.update):
            with self.assertRaisesRegex(SetupError, 'QML URL caching'):
                operation()
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.transport.calls, [])

    def test_unowned_marketplace_git_plugin_refused_actionably(self):
        path = self.home / plugin.DESTINATION
        path.mkdir(parents=True)
        (path / '.git').mkdir()
        before = self.tree()
        with self.assertRaisesRegex(SetupError, 'marketplace/git'):
            self.install()
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.transport.calls)

    def test_unowned_engine_refused(self):
        self.manager.binary.parent.mkdir(parents=True)
        self.manager.binary.write_text('unowned')
        with self.assertRaisesRegex(engine.InstallError, 'unowned'):
            self.install()
        self.assertFalse(self.transport.calls)

    def test_modified_engine_or_plugin_refused(self):
        self.install()
        for path in (self.manager.binary, self.home / plugin.DESTINATION / 'plugin/Panel.qml'):
            original = path.read_bytes()
            path.write_bytes(original + b'changed')
            self.transport.calls.clear()
            with self.assertRaises((SetupError, engine.InstallError)):
                self.manager.update()
            self.assertFalse(self.transport.calls)
            path.write_bytes(original)

    def test_existing_v01_engine_uses_ownership_and_first_plugin_install(self):
        old = self.root / 'v01'
        old.write_text(PROGRAM.replace('VERSION_TOKEN', '0.1.0'))
        engine.install(self.home, old)
        self.manager.config.parent.mkdir(parents=True, exist_ok=True)
        self.manager.config.write_bytes(CONFIG)
        saved = self.state()
        self.manager.update()
        self.assertEqual(self.manager._version(), '0.2.0')
        plugin.run('verify', self.home)
        self.assertEqual(self.manager.state.read_bytes(), saved)
        self.assertFalse(self.transport.active)  # Originally inactive.
        self.assertFalse(self.transport.enabled)

    def test_original_inactive_disabled_update_stays_inactive(self):
        self.install()
        self.transport.active = self.transport.enabled = False
        self.version('0.2.1')
        self.transport.calls.clear()
        self.manager.update()
        self.assertFalse(self.transport.active)
        self.assertFalse(self.transport.enabled)
        self.assertFalse(any(a[0] == 'systemctl' and a[2] == 'start' for a, _ in self.transport.calls))

    def test_invalid_new_config_no_stop_or_install(self):
        with self.assertRaises(SetupError):
            self.manager.install(b'not valid toml !')
        self.assertIsNone(engine.load_manifest(self.home))
        self.assertFalse(any(a[0] == 'systemctl' and a[2] in ('start', 'stop') for a, _ in self.transport.calls))

    def test_missing_initial_config_refused(self):
        with self.assertRaisesRegex(SetupError, 'initial config_bytes'):
            self.manager.install()
        self.assertFalse(self.manager.binary.exists())

    def test_startup_pause_history_change_triggers_recovery(self):
        self.install()
        saved = self.state()
        original_engine = engine.load_manifest(self.home)
        self.version('0.2.1')
        candidate = []
        def mutate_once():
            self.transport.on_start = None
            candidate.append(self.state(paused=False, carousel={
                'paths': ['a', 'b', 'c'], 'bag': ['b'],
                'history': ['c', 'a', 'c'], 'cursor': 2}))
        self.transport.on_start = mutate_once
        with self.assertRaisesRegex(SetupError, 'original owned files/service recovered') as raised:
            self.manager.update()
        self.assertIn('Startup changed pause/history state', str(raised.exception.__cause__))
        self.assertEqual(self.manager._version(), '0.2.0')
        self.assertEqual(engine.load_manifest(self.home), original_engine)
        self.assertEqual(self.manager.state.read_bytes(), saved)
        self.assertTrue(self.transport.active)
        self.assertTrue(self.transport.enabled)
        recovery_inputs = list(self.manager.evidence.glob('recovery-input-*.json'))
        self.assertEqual(len(recovery_inputs), 1)
        evidence = json.loads(recovery_inputs[0].read_bytes())
        self.assertEqual(base64.b64decode(evidence['files'][str(self.manager.state)]['data']), candidate[0])

    def test_uninstall_owned_widget_preserves_new_unrelated_settings_and_data(self):
        self.install(widget=True)
        state = self.state()
        self.activation.value['config']['theme'] = 'changed during use'
        self.activation.value['persisted']['theme'] = 'changed during use'
        self.activation.value['config']['bar']['layout']['right'].append({'id': 'another-plugin'})
        self.new_manager().uninstall()
        self.assertEqual(self.activation.value['config']['theme'], 'changed during use')
        self.assertEqual(self.activation.value['persisted']['theme'], 'changed during use')
        self.assertEqual(self.activation.value['config']['bar']['layout']['right'], [{'id': 'clock'}, {'id': 'another-plugin'}])
        self.assertEqual(self.manager.config.read_bytes(), CONFIG)
        self.assertEqual(self.manager.state.read_bytes(), state)
        self.assertFalse(self.manager.binary.exists())
        self.assertTrue(self.image.exists())
        self.assertTrue(self.manager.background.is_symlink())

    def test_plugin_only_uninstall_never_stops_or_disables_unowned_service(self):
        plugin.run('install', self.home, self.bundle)
        self.transport.active = True
        self.transport.enabled = True
        self.transport.loaded = True
        self.manager.uninstall()
        self.assertTrue(self.transport.active)
        self.assertTrue(self.transport.enabled)
        self.assertFalse((self.home / plugin.DESTINATION).exists())
        self.assertFalse(any(a[0] == 'systemctl' and a[2] in ('stop', 'disable', 'start') for a, _ in self.transport.calls))

    def test_uninstall_pre_setup_owned_widget_without_receipt_is_authorized(self):
        self.install()
        self.assertFalse((self.home / ACTIVATION).exists())
        self.activation.value['registry'][PLUGIN_ID] = True
        self.activation.value['registry']['another-plugin'] = True
        self.activation.value['config']['bar']['layout']['right'].append({'id': PLUGIN_ID})
        self.activation.value['config']['theme'] = 'current preference'
        self.activation.value['persisted'].update({'theme': 'current preference', 'plugins': [PLUGIN_ID, 'another-plugin']})
        self.new_manager().uninstall()
        self.assertTrue(self.activation.disable_attempted)
        self.assertFalse(self.activation.value['registry'][PLUGIN_ID])
        self.assertTrue(self.activation.value['registry']['another-plugin'])
        self.assertEqual(self.activation.value['config']['theme'], 'current preference')
        self.assertEqual(self.activation.value['persisted'], {'theme': 'current preference', 'plugins': ['another-plugin']})
        self.assertFalse(self.manager.binary.exists())
        self.assertFalse((self.home / plugin.DESTINATION).exists())

    def test_uninstall_unowned_plugin_never_disables_activation(self):
        path = self.home / plugin.DESTINATION
        path.mkdir(parents=True)
        (path / '.git').mkdir()
        self.activation.value['registry'][PLUGIN_ID] = True
        before = self.tree()
        with self.assertRaisesRegex(SetupError, 'marketplace/git'):
            self.manager.uninstall()
        self.assertEqual(before, self.tree())
        self.assertFalse(self.activation.disable_attempted)
        self.assertFalse(self.transport.calls)

    def test_remove_data_only_known_regular_files_backup_first(self):
        self.install()
        self.state()
        unknown = self.manager.state.parent / 'keep-me'
        unknown.write_text('not managed')
        result = self.manager.uninstall(remove_data=True)
        self.assertFalse(self.manager.config.exists())
        self.assertFalse(self.manager.state.exists())
        self.assertTrue(unknown.exists())
        self.assertTrue(self.image.exists())
        backup = json.loads(Path(result['backup']).read_bytes())
        self.assertEqual(base64.b64decode(backup['files'][str(self.manager.config)]['data']), CONFIG)

    def test_remove_data_refuses_symlink_or_directory_before_stop(self):
        self.install()
        self.manager.config.unlink()
        for kind in ('symlink', 'directory'):
            if kind == 'symlink':
                self.manager.config.symlink_to(self.image)
            else:
                self.manager.config.mkdir()
            self.transport.calls.clear()
            with self.assertRaises(engine.InstallError):
                self.manager.uninstall(remove_data=True)
            self.assertFalse(self.transport.calls)
            self.assertTrue(self.image.exists())
            if kind == 'symlink':
                self.manager.config.unlink()
            else:
                self.manager.config.rmdir()

    def test_environment_rejects_unknown_relative_and_escape_overrides(self):
        for overrides in ({'SWIFTWALL_EXTRA': 'bad'}, {'SWIFTWALL_CONFIG': 'relative'},
                          {'SWIFTWALL_RUNTIME_DIR': '/run/user/0/swiftwall'},
                          {'HOME': '/other'}, {'XDG_RUNTIME_DIR': 'relative'}):
            with self.subTest(overrides=overrides), self.assertRaises(SetupError):
                SetupManager(self.home, self.bundle, self.env | overrides, unit='private.service')

    def test_private_runtime_must_not_be_shared_or_symlinked(self):
        runtime = self.home / 'runtime'
        runtime.chmod(0o755)
        with self.assertRaisesRegex(SetupError, 'mode 0700'):
            self.manager.plan('install')
        runtime.chmod(0o700)
        runtime.rmdir()
        runtime.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(plugin.InstallError):
            self.manager.plan('install')
        self.assertFalse(self.transport.calls)

    def test_private_home_cannot_select_production_unit(self):
        with self.assertRaisesRegex(SetupError, 'private disposable'):
            SetupManager(self.home, self.bundle, self.env)

    def test_service_query_exception_is_redacted(self):
        self.transport.secret_query = True
        with self.assertRaisesRegex(SetupError, 'output omitted') as caught:
            self.manager.status() if self.manager.plan('status')['engine_owned'] else self.manager.service.state()
        self.assertNotIn('API_SECRET', str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_loaded_service_environment_mismatch_precedes_stop_and_files(self):
        self.install()
        self.transport.loaded_environment['SWIFTWALL_CONFIG'] = '/wrong/config'
        before = self.tree()
        self.transport.calls.clear()
        with self.assertRaisesRegex(RuntimeError, 'environment-mismatch'):
            self.manager.update()
        self.assertEqual(self.tree(), before)
        self.assertFalse(any(a[0] == 'systemctl' and a[2] == 'stop' for a, _ in self.transport.calls))

    def test_production_override_mismatch_refused_without_commands(self):
        env = {'HOME': str(Path.home()), 'XDG_RUNTIME_DIR': '/run/user/1000',
               'PATH': '/usr/bin', 'SWIFTWALL_CONFIG': '/tmp/not-production.toml'}
        with self.assertRaisesRegex(SetupError, 'Production SWIFTWALL_ override'):
            SetupManager(Path.home(), env=env)

    def test_evidence_symlink_refused_before_install(self):
        outside = self.root / 'outside'
        outside.mkdir()
        self.manager.evidence.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.install()
        self.assertFalse(self.manager.binary.exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_evidence_cannot_overlap_plugin_or_data(self):
        for evidence in (self.home / plugin.DESTINATION / 'evidence', self.manager.config):
            with self.assertRaisesRegex(SetupError, 'overlaps'):
                SetupManager(self.home, self.bundle, self.env, unit='private.service', evidence=evidence)

    def test_stale_unowned_shell_references_block_first_plugin_publication(self):
        shell = self.home / '.config/omarchy/shell.json'
        shell.parent.mkdir(parents=True)
        shell.write_text(json.dumps({'plugins': [PLUGIN_ID], 'bar': {'layout': {}}}))
        before = self.tree()
        with self.assertRaisesRegex(SetupError, 'shell references are unowned'):
            self.install()
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.transport.calls)

    def test_update_reload_failure_recovers_original_engine(self):
        self.install()
        self.version('0.2.1')
        self.transport.fail_reload = True
        with self.assertRaisesRegex(SetupError, 'recovered'):
            self.manager.update()
        self.assertEqual(self.manager._version(), '0.2.0')
        self.assertTrue(self.transport.active)

    def test_backup_is_unique_private_and_complete(self):
        self.install()
        original = self.manager.backup
        assert original is not None
        saved = original.read_bytes()
        self.manager.update()
        self.assertNotEqual(self.manager.backup, original)
        self.assertEqual(original.read_bytes(), saved)
        for path in self.manager.evidence.glob('*.json'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.manager.evidence.stat().st_mode & 0o777, 0o700)

    def test_public_manager_uses_deployment_identity_helpers(self):
        from deployment import Deployment
        self.assertIs(SetupManager.socket_owner, Deployment.socket_owner)
        self.assertIs(SetupManager.running_executable, Deployment.running_executable)

    def test_existing_install_rejects_config_bytes_before_commands_or_writes(self):
        self.install()
        self.version('0.2.1')
        before = self.tree()
        self.transport.calls.clear()
        for data in (CONFIG, CONFIG.replace(b'300', b'600')):
            with self.assertRaisesRegex(SetupError, 'only supported for a fresh engine'):
                self.manager.install(config_bytes=data)
        self.assertEqual(before, self.tree())
        self.assertFalse(self.transport.calls)

    def test_interrupted_receipt_without_plugin_fails_closed(self):
        engine.transaction(self.home, {self.home / ACTIVATION: engine.record(b'{"version":1,"owned":true}\n', 0o600)})
        before = self.tree()
        for operation in (lambda: self.manager.plan('install'), self.manager.status,
                          self.install, self.manager.uninstall):
            with self.assertRaisesRegex(SetupError, 'Interrupted installation'):
                operation()
        self.assertEqual(before, self.tree())
        self.assertFalse(self.transport.calls)
        self.assertFalse(self.activation.disable_attempted)

    def test_plan_and_status_report_metadata_versions_without_binary_execution(self):
        self.install()
        release = {'version': '0.2.0', 'files': {'bin/swiftwall': {
            'sha256': hashlib.sha256(self.manager.binary.read_bytes()).hexdigest()}}}
        (self.bundle / 'release.json').write_text(json.dumps(release))
        expected_plugin = json.loads((self.bundle / 'manifest.json').read_bytes())['version']
        self.transport.calls.clear()
        before = self.tree()
        plan = self.manager.plan('status')
        self.assertEqual(plan['engine_version'], '0.2.0')
        self.assertEqual(plan['plugin_version'], expected_plugin)
        self.assertFalse(self.transport.calls)
        self.assertEqual(before, self.tree())
        status = self.manager.status()
        self.assertEqual(status['engine_version'], '0.2.0')
        self.assertEqual(status['plugin_version'], expected_plugin)
        self.assertEqual(status['service']['ActiveState'], 'active')
        self.assertTrue(all(a[0] == 'systemctl' for a, _ in self.transport.calls))
        self.assertEqual(before, self.tree())
        no_bundle = self.new_manager()
        no_bundle.bundle = None
        self.assertIsNone(no_bundle.plan('status')['engine_version'])
        release['files']['bin/swiftwall']['sha256'] = '0' * 64
        (self.bundle / 'release.json').write_text(json.dumps(release))
        self.assertIsNone(self.manager.plan('update')['engine_version'])
        self.assertFalse(self.manager.plan('update')['engine_version'] == expected_plugin)

    def test_backup_disk_corruption_is_detected_before_service_mutation(self):
        self.install()
        self.transport.calls.clear()
        original = engine.load_manifest(self.home)
        fsync = os.fsync
        corrupted = []
        def corrupt(fd):
            fsync(fd)
            path = os.readlink(f'/proc/self/fd/{fd}')
            if '/before-update-' in path and not corrupted:
                os.pwrite(fd, b'!', 0)
                corrupted.append(path)
        with (patch('setup_support.os.fsync', side_effect=corrupt),
              self.assertRaisesRegex(SetupError, 'Backup read-back verification failed')):
            self.manager.update()
        self.assertTrue(corrupted)
        self.assertEqual(engine.load_manifest(self.home), original)
        self.assertFalse(any(a[0] == 'systemctl' and a[2] == 'stop' for a, _ in self.transport.calls))
        self.assertTrue(self.transport.active)

    def test_engine_transaction_readback_failure_restores_before_start(self):
        self.install()
        original = engine.load_manifest(self.home)
        self.version('0.2.1')
        write = engine.atomic_write
        corrupted = []
        def corrupt(path, item):
            write(path, item)
            if path == self.manager.binary and not corrupted:
                path.write_bytes(path.read_bytes() + b'\n# silent write corruption\n')
                corrupted.append(path)
        with (patch('install_support.atomic_write', side_effect=corrupt),
              self.assertRaisesRegex(SetupError, 'original owned files/service recovered') as caught):
            self.manager.update()
        self.assertIn('read-back verification failed', str(caught.exception.__cause__))
        self.assertEqual(engine.load_manifest(self.home), original)
        engine.verify_owned(self.home, original)
        self.assertTrue(self.transport.active)

    def assert_uninstall_recovers_without_reenabling_widget(self, operation):
        self.manager.checkpoint = None
        original = self.manager._snapshot()
        activations = self.activation.activations
        with (patch.object(self.manager, '_recover', wraps=self.manager._recover) as recover,
              patch.object(self.manager, 'rollback', side_effect=AssertionError('No public checkpoint')) as rollback,
              patch.object(self.activation, 'activate', side_effect=AssertionError('Never replay widget enable')) as activate,
              patch.object(self.activation, 'deactivate', wraps=self.activation.deactivate) as deactivate,
              self.assertRaisesRegex(SetupError, 'files/engine recovered; original service state restored') as caught):
            operation()
        recover.assert_called_once()
        rollback.assert_not_called()
        activate.assert_not_called()
        deactivate.assert_called_once()
        self.assertEqual(self.activation.activations, activations)
        self.assertFalse(self.activation.value['registry'][PLUGIN_ID])
        self.assertIn('widget state disabled', str(caught.exception))
        self.assertIn('widget recovery/manual activation pending', str(caught.exception))
        self.assertIn('no automatic re-enable attempted', str(caught.exception))
        for path, item in original['files'].items():
            self.assertEqual(engine.read_record(self.home, Path(path)), item)
        self.assertEqual(plugin.load_manifest(self.home), original['plugin'])
        plugin.verify_owned(self.home, original['plugin'])
        engine.verify_owned(self.home, engine.load_manifest(self.home))
        self.assertEqual(self.transport.active, original['service']['ActiveState'] == 'active')
        self.assertEqual(self.transport.enabled, original['service']['UnitFileState'] == 'enabled')
        self.assertEqual(self.manager._wallpaper(), original['wallpaper'])
        self.assertTrue(self.image.exists())
        self.assertIsNone(self.manager.checkpoint)
        self.manager.plan('status')  # No orphan receipt blocks the restored installation.
        backup = self.manager.backup
        assert backup is not None
        self.assertIn(str(backup), str(caught.exception))
        self.assertEqual(json.loads(backup.read_bytes())['plugin'], original['plugin'])
        return caught.exception

    def test_uninstall_interrupt_after_plugin_removal_restores_files_but_not_widget_activation(self):
        self.install(widget=True)
        self.state(paused=False)
        transaction = plugin.transaction
        interrupted = []
        def interrupt(home, before, current, previous):
            transaction(home, before, current, previous)
            if current is None and not interrupted:
                interrupted.append(True)
                self.assertFalse((home / plugin.DESTINATION).exists())
                self.assertTrue((home / ACTIVATION).exists())
                self.assertTrue(self.manager.binary.exists())
                self.activation.value['config']['theme'] = 'changed during failure'
                self.activation.value['persisted']['theme'] = 'changed during failure'
                self.activation.value['config']['bar']['layout']['right'].append({'id': 'unrelated-widget'})
                raise KeyboardInterrupt('fixture SIGINT after plugin removal')
        with patch('plugin_support.transaction', side_effect=interrupt):
            self.assert_uninstall_recovers_without_reenabling_widget(self.manager.uninstall)
        self.assertEqual(self.activation.value['config']['theme'], 'changed during failure')
        self.assertEqual(self.activation.value['persisted']['theme'], 'changed during failure')
        self.assertEqual(self.activation.value['config']['bar']['layout']['right'],
                         [{'id': 'clock'}, {'id': 'unrelated-widget'}])

    def test_uninstall_interrupt_after_engine_removal_before_receipt_cleanup_recovers(self):
        self.install(widget=True)
        self.state()
        uninstall = engine.uninstall
        def interrupt(home):
            uninstall(home)
            self.assertFalse(self.manager.binary.exists())
            self.assertTrue((home / ACTIVATION).exists())
            raise KeyboardInterrupt('fixture SIGINT before receipt cleanup')
        with patch('install_support.uninstall', side_effect=interrupt):
            self.assert_uninstall_recovers_without_reenabling_widget(self.manager.uninstall)

    def test_uninstall_interrupt_after_remove_data_restores_backed_up_config_state_and_receipt(self):
        self.install(widget=True)
        self.state()
        files = self.manager._files
        interrupted = []
        def interrupt(changes):
            files(changes)
            if changes.get(self.home / ACTIVATION, 'unchanged') is None and not interrupted:
                interrupted.append(True)
                self.assertFalse((self.home / ACTIVATION).exists())
                self.assertFalse(self.manager.config.exists())
                self.assertFalse(self.manager.state.exists())
                raise KeyboardInterrupt('fixture SIGINT after data removal')
        with patch.object(self.manager, '_files', side_effect=interrupt):
            self.assert_uninstall_recovers_without_reenabling_widget(lambda: self.manager.uninstall(remove_data=True))

    def test_uninstall_interrupt_after_stop_restores_original_active_service(self):
        self.install(widget=True)
        self.state()
        self.transport.interrupt_after['stop'] = 1
        self.assert_uninstall_recovers_without_reenabling_widget(self.manager.uninstall)

    def test_uninstall_ordinary_failure_also_reports_widget_manual_activation_pending(self):
        self.install(widget=True)
        self.state()
        with patch('install_support.uninstall', side_effect=OSError('fixture engine removal failure')):
            self.assert_uninstall_recovers_without_reenabling_widget(self.manager.uninstall)

    def test_uninstall_interrupt_during_disable_keeps_engine_unchanged_and_reports_unverified_widget(self):
        self.install(widget=True)
        self.state()
        original = self.manager._snapshot()
        deactivate = self.activation.deactivate
        def interrupt():
            deactivate()
            raise KeyboardInterrupt('fixture disable delivered; observation interrupted')
        self.transport.calls.clear()
        with (patch.object(self.activation, 'deactivate', side_effect=interrupt) as disable,
              patch.object(self.activation, 'activate', side_effect=AssertionError('No widget enable')) as enable,
              patch.object(self.manager, '_recover', wraps=self.manager._recover) as recover,
              self.assertRaisesRegex(SetupError, 'original engine/files/service unchanged') as caught):
            self.manager.uninstall()
        disable.assert_called_once()
        enable.assert_not_called()
        recover.assert_not_called()
        self.assertIn('widget state unverified', str(caught.exception))
        self.assertIn('widget recovery/manual activation pending', str(caught.exception))
        self.assertFalse(self.activation.value['registry'][PLUGIN_ID])
        self.assertTrue(self.transport.active)
        self.assertTrue(self.transport.enabled)
        self.assertFalse(any(a[0] == 'systemctl' and a[2] in ('stop', 'start', 'disable') for a, _ in self.transport.calls))
        for path, item in original['files'].items():
            self.assertEqual(engine.read_record(self.home, Path(path)), item)
        self.assertEqual(plugin.load_manifest(self.home), original['plugin'])
        backup = self.manager.backup
        assert backup is not None
        self.assertTrue(backup.is_file())
        self.assertIn(str(backup), str(caught.exception))

    def assert_uninstall_second_interrupt_retains_backup(self, stage):
        self.install(widget=True)
        self.state()
        self.manager.checkpoint = None
        original = self.manager._snapshot()
        transaction = plugin.transaction
        backup = self.manager._backup
        full_backups = []
        diagnostics = []
        interrupted = []
        def interrupt_removal(home, before, current, previous):
            transaction(home, before, current, previous)
            if current is None and not interrupted:
                interrupted.append(True)
                if stage == 'stop':
                    self.transport.interrupt_after['stop'] = 1
                raise KeyboardInterrupt('fixture first uninstall interrupt')
        def interrupt_backup(snapshot, label):
            path = backup(snapshot, label)
            if label == 'recovery-input':
                diagnostics.append(path)
                if stage == 'backup':
                    raise KeyboardInterrupt()
            else:
                full_backups.append((path, path.read_bytes()))
            return path
        with (patch('plugin_support.transaction', side_effect=interrupt_removal),
              patch.object(self.manager, '_backup', side_effect=interrupt_backup),
              patch.object(self.manager, '_recover', wraps=self.manager._recover) as recover,
              patch.object(self.manager, 'rollback', side_effect=AssertionError('No public checkpoint')) as rollback,
              patch.object(self.activation, 'activate', side_effect=AssertionError('No widget enable')) as activate,
              patch.object(self.activation, 'deactivate', wraps=self.activation.deactivate) as deactivate,
              self.assertRaisesRegex(SetupError, 'Uninstall failed; recovery incomplete:') as caught):
            self.manager.uninstall()
        recover.assert_called_once()
        rollback.assert_not_called()
        activate.assert_not_called()
        deactivate.assert_called_once()
        self.assertIn('widget state disabled', str(caught.exception))
        self.assertIn('widget recovery/manual activation pending', str(caught.exception))
        self.assertNotIn('files/engine recovered', str(caught.exception))
        self.assertNotIn('original service state restored', str(caught.exception))
        self.assertEqual(len(full_backups), 2)
        self.assertEqual(self.manager.backup, full_backups[-1][0])
        self.assertIn(str(self.manager.backup), str(caught.exception))
        for path, data in full_backups:
            self.assertEqual(path.read_bytes(), data)
            saved = json.loads(data)
            self.assertEqual(saved['plugin'], original['plugin'])
            self.assertEqual(saved['files'], original['files'])
            self.assertEqual(saved['service']['ActiveState'], 'active')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.transport.active)
        self.assertFalse(self.activation.value['registry'][PLUGIN_ID])
        self.assertIsNone(self.manager.checkpoint)
        if stage == 'backup':
            self.assertEqual(len(diagnostics), 1)
            self.assertIn('recovery incomplete: KeyboardInterrupt', str(caught.exception))
            self.assertNotIn(str(self.manager.binary), json.loads(diagnostics[0].read_bytes())['files'])

    def test_uninstall_second_interrupt_during_recovery_stop_retains_backup(self):
        self.assert_uninstall_second_interrupt_retains_backup('stop')

    def test_uninstall_second_interrupt_during_recovery_backup_retains_full_snapshot_reference(self):
        self.assert_uninstall_second_interrupt_retains_backup('backup')

    def test_engine_only_uninstall_interrupt_does_not_claim_widget_needs_activation(self):
        self.install()
        plugin.run('uninstall', self.home)
        original = engine.load_manifest(self.home)
        self.transport.interrupt_after['stop'] = 1
        with (patch.object(self.manager, '_controller', side_effect=AssertionError('No owned widget')),
              self.assertRaisesRegex(SetupError, 'files/engine recovered') as caught):
            self.manager.uninstall()
        self.assertNotIn('widget recovery', str(caught.exception))
        self.assertEqual(engine.load_manifest(self.home), original)
        self.assertTrue(self.transport.active)
        self.assertTrue(self.transport.enabled)

    def test_uninstall_verifies_engine_removal_and_recovers_if_it_did_not_commit(self):
        self.install()
        original = engine.load_manifest(self.home)
        with (patch('setup_support.engine.uninstall', return_value=None),
              self.assertRaisesRegex(SetupError, 'files/engine recovered')):
            self.manager.uninstall()
        engine.verify_owned(self.home, original)
        self.assertEqual(engine.load_manifest(self.home), original)
        plugin.run('verify', self.home)
        self.assertTrue(self.transport.active)
        self.assertTrue(self.transport.enabled)

    def test_schema_one_explicit_pause_shuffle_and_latest_rollback_preserved(self):
        for paused in (True, False):
            with self.subTest(paused=paused):
                if engine.load_manifest(self.home) is None:
                    self.install()
                saved = self.state(paused=paused)
                self.version('0.2.1')
                self.manager.update()
                self.assertEqual(self.manager.state.read_bytes(), saved)
                latest = self.state(paused=not paused, carousel={
                    'paths': ['a', 'b', 'c'], 'bag': ['b', 'a'],
                    'history': ['c', 'a', 'c'], 'cursor': 2})
                self.manager.rollback()
                self.assertEqual(self.manager.state.read_bytes(), latest)
                self.assertIn(b'selection = "shuffle"', self.manager.config.read_bytes())

    def test_rollback_does_not_accept_schema_version_or_boolean_version_as_schema_one(self):
        self.install()
        saved = self.state()
        self.version('0.2.1')
        self.manager.update()
        self.state(version=True, schema_version=1, paused=False)
        self.manager.rollback()
        self.assertEqual(self.manager.state.read_bytes(), saved)
        self.assertEqual(self.manager._version(), '0.2.0')

    def test_stop_failure_gets_one_recovery_without_installing_new_generation(self):
        self.install()
        original = engine.load_manifest(self.home)
        self.version('0.2.1')
        self.transport.calls.clear()
        self.transport.fail_stop = 1
        with self.assertRaisesRegex(SetupError, 'original owned files/service recovered'):
            self.manager.update()
        self.assertEqual(engine.load_manifest(self.home), original)
        self.assertTrue(self.transport.active)
        actions = [a[2] for a, _ in self.transport.calls if a[0] == 'systemctl']
        self.assertEqual(actions.count('stop'), 2)
        self.assertEqual(actions.count('start'), 1)

    def test_persistent_stop_failure_does_not_replace_running_engine_or_start_again(self):
        self.install()
        original = engine.load_manifest(self.home)
        self.version('0.2.1')
        self.transport.calls.clear()
        self.transport.fail_stop = 2
        with self.assertRaisesRegex(SetupError, 'recovery incomplete'):
            self.manager.update()
        self.assertEqual(engine.load_manifest(self.home), original)
        self.assertTrue(self.transport.active)
        actions = [a[2] for a, _ in self.transport.calls if a[0] == 'systemctl']
        self.assertEqual(actions.count('stop'), 2)
        self.assertNotIn('start', actions)

    def test_start_failure_after_delivery_stops_new_engine_and_recovers_original(self):
        self.install()
        original = engine.load_manifest(self.home)
        self.version('0.2.1')
        self.transport.calls.clear()
        self.transport.fail_start = True
        with self.assertRaisesRegex(SetupError, 'original owned files/service recovered'):
            self.manager.update()
        self.assertEqual(engine.load_manifest(self.home), original)
        self.assertTrue(self.transport.active)
        actions = [a[2] for a, _ in self.transport.calls if a[0] == 'systemctl']
        self.assertEqual(actions.count('start'), 2)
        self.assertEqual(actions.count('stop'), 2)

    def test_first_enable_failure_is_disabled_before_removing_unit(self):
        self.transport.fail_enable = True
        self.transport.require_unit_for_disable = True
        with self.assertRaisesRegex(SetupError, 'original owned files/service recovered'):
            self.install()
        self.assertFalse(self.transport.enabled)
        self.assertFalse(self.transport.active)
        self.assertIsNone(engine.load_manifest(self.home))
        self.assertIsNone(plugin.load_manifest(self.home))

    def assert_interrupt_recovers(self, operation):
        """Exercise real recovery without any previously published checkpoint."""
        self.manager.checkpoint = None
        installed = engine.load_manifest(self.home)
        widget = plugin.load_manifest(self.home)
        config = engine.read_record(self.home, self.manager.config)
        state = engine.read_record(self.home, self.manager.state)
        receipt = engine.read_record(self.home, self.home / ACTIVATION)
        active, enabled = self.transport.active, self.transport.enabled
        wallpaper = self.manager._wallpaper()
        with (patch.object(self.manager, '_recover', wraps=self.manager._recover) as recover,
              patch.object(self.manager, 'rollback', side_effect=AssertionError('No public checkpoint exists')) as rollback,
              self.assertRaisesRegex(SetupError, 'original owned files/service recovered') as caught):
            operation()
        recover.assert_called_once()
        rollback.assert_not_called()
        self.assertIsNone(self.manager.checkpoint)
        self.assertEqual(engine.load_manifest(self.home), installed)
        engine.verify_owned(self.home, installed)
        self.assertEqual(plugin.load_manifest(self.home), widget)
        plugin.verify_owned(self.home, widget)
        self.assertEqual(engine.read_record(self.home, self.manager.config), config)
        self.assertEqual(engine.read_record(self.home, self.manager.state), state)
        self.assertEqual(engine.read_record(self.home, self.home / ACTIVATION), receipt)
        self.assertEqual(self.transport.active, active)
        self.assertEqual(self.transport.enabled, enabled)
        self.assertEqual(self.manager._wallpaper(), wallpaper)
        backup = self.manager.backup
        assert backup is not None
        saved = json.loads(backup.read_bytes())
        self.assertIn(str(self.manager.binary), saved['files'])
        self.assertEqual(saved['service']['ActiveState'], 'active' if active else 'inactive')
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        cause = caught.exception.__cause__
        while cause is not None and not isinstance(cause, KeyboardInterrupt):
            cause = cause.__cause__
        self.assertIsInstance(cause, KeyboardInterrupt)
        return caught.exception

    def test_interrupted_stop_after_delivery_recovers_before_checkpoint_publication(self):
        self.install()
        self.state()
        self.version('0.2.1')
        self.transport.calls.clear()
        self.transport.interrupt_after['stop'] = 1
        self.assert_interrupt_recovers(self.manager.update)
        actions = [a[2] for a, _ in self.transport.calls if a[0] == 'systemctl']
        self.assertEqual(actions.count('stop'), 2)
        self.assertEqual(actions.count('start'), 1)

    def test_interrupted_engine_file_write_uses_installer_recovery_then_lifecycle_recovery(self):
        self.install()
        self.state()
        self.version('0.2.1')
        write = engine.atomic_write
        interrupted = []
        def interrupt(path, item):
            write(path, item)
            if path == self.manager.binary and not interrupted:
                interrupted.append(path)
                raise KeyboardInterrupt('fixture SIGINT after binary write')
        with patch('install_support.atomic_write', side_effect=interrupt):
            self.assert_interrupt_recovers(self.manager.update)
        self.assertEqual(interrupted, [self.manager.binary])

    def test_interrupted_engine_postcommit_recovers_original_active_generation(self):
        self.install()
        self.state()
        self.version('0.2.1')
        install = self.manager._install_engine
        def interrupt(current):
            install(current)
            raise KeyboardInterrupt('fixture SIGINT after engine commit')
        with patch.object(self.manager, '_install_engine', side_effect=interrupt):
            self.assert_interrupt_recovers(self.manager.update)

    def test_interrupted_initial_config_write_restores_original_config_and_inactive_state(self):
        original_config = CONFIG + b'# pre-existing config before first engine install\n'
        engine.transaction(self.home, {self.manager.config: engine.record(original_config, 0o600)})
        self.state()
        write = engine.atomic_write
        interrupted = []
        def interrupt(path, item):
            write(path, item)
            if path == self.manager.config and not interrupted:
                interrupted.append(path)
                raise KeyboardInterrupt('fixture SIGINT after config write')
        with patch('install_support.atomic_write', side_effect=interrupt):
            self.assert_interrupt_recovers(self.install)
        self.assertEqual(interrupted, [self.manager.config])

    def test_interrupted_initial_config_postcommit_restores_original_config(self):
        original_config = CONFIG + b'# keep existing user configuration\n'
        engine.transaction(self.home, {self.manager.config: engine.record(original_config, 0o600)})
        files = self.manager._files
        interrupted = []
        def interrupt(changes):
            files(changes)
            if self.manager.config in changes and not interrupted:
                interrupted.append(self.manager.config)
                raise KeyboardInterrupt('fixture SIGINT after config transaction')
        with patch.object(self.manager, '_files', side_effect=interrupt):
            self.assert_interrupt_recovers(self.install)

    def test_interrupted_plugin_publish_restores_original_active_legacy_engine(self):
        self.seed_legacy(active=True)
        def interrupt(name):
            if name == 'new-placed':
                raise KeyboardInterrupt('fixture SIGINT during plugin publication')
        with patch('plugin_support.checkpoint', side_effect=interrupt):
            self.assert_interrupt_recovers(lambda: self.manager.update(enable_widget=True))
        self.assertTrue(self.activation.disable_attempted)
        self.assertFalse(self.activation.enable_attempted)

    def test_interrupted_plugin_postcommit_removes_owned_publication_and_receipt(self):
        self.seed_legacy(active=True)
        transaction = plugin.transaction
        interrupted = []
        def interrupt(home, before, current, previous):
            transaction(home, before, current, previous)
            if current is not None and not interrupted:
                interrupted.append(home)
                raise KeyboardInterrupt('fixture SIGINT after plugin commit')
        with patch('plugin_support.transaction', side_effect=interrupt):
            self.assert_interrupt_recovers(lambda: self.manager.update(enable_widget=True))
        self.assertEqual(interrupted, [self.home])
        self.assertTrue(self.activation.disable_attempted)
        self.assertFalse((self.home / ACTIVATION).exists())

    def test_interrupted_start_after_delivery_stops_new_engine_and_recovers_original(self):
        self.install()
        self.state()
        self.version('0.2.1')
        self.transport.calls.clear()
        self.transport.interrupt_after['start'] = 1
        self.assert_interrupt_recovers(self.manager.update)
        actions = [a[2] for a, _ in self.transport.calls if a[0] == 'systemctl']
        self.assertEqual(actions.count('stop'), 2)
        self.assertEqual(actions.count('start'), 2)

    def test_interrupted_enable_after_delivery_disables_before_removing_new_owned_unit(self):
        self.state()
        self.transport.interrupt_after['enable'] = 1
        self.transport.require_unit_for_disable = True
        self.assert_interrupt_recovers(lambda: self.install(widget=True))
        self.assertTrue(self.activation.disable_attempted)
        self.assertFalse(self.activation.value['registry'][PLUGIN_ID])
        self.assertFalse((self.home / plugin.DESTINATION).exists())

    def test_interrupted_stopped_backup_still_recovers_original_active_service(self):
        self.install()
        self.state()
        self.version('0.2.1')
        backup = self.manager._backup
        def interrupt(snapshot, label):
            path = backup(snapshot, label)
            if label == 'stopped-update':
                raise KeyboardInterrupt('fixture SIGINT after stopped backup')
            return path
        with patch.object(self.manager, '_backup', side_effect=interrupt):
            self.assert_interrupt_recovers(self.manager.update)

    def test_second_interrupt_during_recovery_stop_retains_independent_backup_without_retry(self):
        self.install()
        self.state()
        original = engine.load_manifest(self.home)
        self.version('0.2.1')
        self.manager.checkpoint = None
        install = self.manager._install_engine
        def interrupt(current):
            install(current)
            self.transport.interrupt_after['stop'] = 1
            raise KeyboardInterrupt('fixture first interrupt after commit')
        with (patch.object(self.manager, '_install_engine', side_effect=interrupt),
              patch.object(self.manager, '_recover', wraps=self.manager._recover) as recover,
              patch.object(self.manager, 'rollback', side_effect=AssertionError('No checkpoint')) as rollback,
              self.assertRaisesRegex(SetupError, 'recovery incomplete: fixture SIGINT after stop') as caught):
            self.manager.update()
        recover.assert_called_once()
        rollback.assert_not_called()
        backup = self.manager.backup
        assert backup is not None
        self.assertIn(str(backup), str(caught.exception))
        saved = json.loads(backup.read_bytes())
        assert original is not None
        self.assertEqual(saved['files'][str(self.manager.binary)], original['current']['binary'])
        self.assertEqual(saved['service']['ActiveState'], 'active')
        self.assertFalse(self.transport.active)
        self.assertIsNone(self.manager.checkpoint)
        engine.verify_owned(self.home, engine.load_manifest(self.home))

    def test_interrupted_recovery_backup_keeps_full_checkpoint_reference(self):
        self.install()
        self.state()
        original = engine.load_manifest(self.home)
        self.version('0.2.1')
        self.manager.checkpoint = None
        install = self.manager._install_engine
        backup = self.manager._backup
        full_backups = []
        diagnostic_backups = []
        def interrupt_install(current):
            install(current)
            raise KeyboardInterrupt('fixture first interrupt after commit')
        def interrupt_backup(snapshot, label):
            path = backup(snapshot, label)
            if label == 'recovery-input':
                diagnostic_backups.append(path)
                raise KeyboardInterrupt()  # Empty exception text must still identify Ctrl-C.
            full_backups.append((path, path.read_bytes()))
            return path
        with (patch.object(self.manager, '_install_engine', side_effect=interrupt_install),
              patch.object(self.manager, '_backup', side_effect=interrupt_backup),
              patch.object(self.manager, '_recover', wraps=self.manager._recover) as recover,
              patch.object(self.manager, 'rollback', side_effect=AssertionError('No checkpoint')) as rollback,
              self.assertRaisesRegex(SetupError, 'recovery incomplete: KeyboardInterrupt') as caught):
            self.manager.update()
        recover.assert_called_once()
        rollback.assert_not_called()
        self.assertEqual(len(full_backups), 2)
        self.assertEqual(len(diagnostic_backups), 1)
        self.assertEqual(self.manager.backup, full_backups[-1][0])
        self.assertIn(str(self.manager.backup), str(caught.exception))
        assert original is not None
        for path, data in full_backups:
            self.assertEqual(path.read_bytes(), data)
            saved = json.loads(data)
            self.assertEqual(saved['files'][str(self.manager.binary)], original['current']['binary'])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(str(self.manager.binary), json.loads(diagnostic_backups[0].read_bytes())['files'])
        self.assertIsNone(self.manager.checkpoint)
        self.assertFalse(self.transport.active)

    def seed_legacy(self, active=False, version='0.1.0'):
        old = self.root / 'legacy-engine'
        old.write_text(PROGRAM.replace('VERSION_TOKEN', version))
        engine.install(self.home, old)
        self.manager.config.parent.mkdir(parents=True, exist_ok=True)
        self.manager.config.write_bytes(CONFIG)
        self.state()
        self.transport._load_unit()
        self.transport.active = active
        self.transport.enabled = active
        self.transport.running_env = dict(self.transport.manager_environment)
        return engine.load_manifest(self.home)

    def test_fresh_generated_unit_configures_all_cli_engine_values(self):
        self.env.update(self.manager.engine_environment)  # Match setup.py's explicit environment contract.
        self.manager = self.new_manager()
        self.assertEqual(self.transport.loaded_environment, {})
        self.assertNotIn(b'Environment=', (self.bundle / 'packaging/swiftwall.service').read_bytes())
        self.install()
        expected = self.manager.engine_environment
        self.assertEqual(set(expected), {'HOME', 'XDG_RUNTIME_DIR', 'SWIFTWALL_CONFIG',
                                        'SWIFTWALL_STATE_DIR', 'SWIFTWALL_RUNTIME_DIR',
                                        'SWIFTWALL_BACKGROUND_LINK', 'SWIFTWALL_OMARCHY_COMMAND'})
        self.assertEqual(self.transport.loaded_environment, expected)
        self.assertEqual({k: self.transport.running_env[k] for k in expected}, expected)
        unit = self.home / engine.FILES['unit']
        payload, _ = self.manager._payload()
        self.assertEqual(engine.read_record(self.home, unit), payload['unit'])
        manifest = engine.load_manifest(self.home)
        assert manifest is not None
        self.assertEqual(manifest['current']['unit'], payload['unit'])
        self.assertIn(b'ExecStart=%h/.local/bin/swiftwall daemon', unit.read_bytes())
        self.assertEqual(self.manager.service.unit, 'swiftwall-setup-private-test.service')
        self.assertTrue(self.transport.active)
        self.assertTrue(self.transport.enabled)

    def test_vanilla_unit_does_not_pass_new_install_readiness_checks(self):
        # Regression: the old transport returned the CLI env even for a vanilla
        # unit, hiding the real production first-install blocker.
        template = (self.bundle / 'packaging/swiftwall.service').read_bytes()
        with (patch.object(self.manager, '_unit_bytes', return_value=template),
              self.assertRaisesRegex(SetupError, 'original owned files/service recovered')):
            self.install()
        self.assertIsNone(engine.load_manifest(self.home))
        self.assertFalse(self.transport.active)
        self.assertFalse(self.transport.enabled)
        self.assertFalse(any(a[0] == 'systemctl' and a[2] == 'start' for a, _ in self.transport.calls))

    def test_generated_unit_cannot_fall_back_to_manager_inheritance(self):
        self.install()
        self.transport.loaded_environment = {}
        self.transport.manager_environment.update(self.manager.engine_environment)
        self.transport.calls.clear()
        before = self.tree()
        with self.assertRaisesRegex(RuntimeError, 'legacy inheritance is not allowed'):
            self.manager.update()
        self.assertEqual(self.tree(), before)
        self.assertFalse(any(a[0] == 'systemctl' and a[2] == 'stop' for a, _ in self.transport.calls))
        self.assertFalse(any('show-environment' in a for a, _ in self.transport.calls))

    def test_unowned_loaded_vanilla_unit_is_not_reconfigured_or_adopted(self):
        self.transport.loaded = True
        self.transport.manager_environment.update(self.manager.engine_environment)
        before = self.tree()
        with self.assertRaisesRegex(RuntimeError, 'environment-mismatch'):
            self.install()
        self.assertEqual(self.tree(), before)
        self.assertFalse(any(a[0] == 'systemctl' and a[2] in ('stop', 'start', 'daemon-reload')
                             for a, _ in self.transport.calls))

    def test_unowned_unit_file_is_never_replaced_with_generated_environment(self):
        path = self.home / engine.FILES['unit']
        path.parent.mkdir(parents=True)
        original = (self.bundle / 'packaging/swiftwall.service').read_bytes()
        path.write_bytes(original)
        self.transport.manager_environment.update(self.manager.engine_environment)
        before = self.tree()
        with self.assertRaisesRegex(engine.InstallError, 'unowned'):
            self.install()
        self.assertEqual(self.tree(), before)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(self.transport.calls)

    def test_systemd_environment_quoting_is_literal_not_shell_syntax(self):
        for value in ('/plain', '/space name', '/%h/%n/100%', '/quote"and\\backslash',
                      '/$HOME/$(not-executed);#literal', "/single'quote"):
            with self.subTest(value=value):
                quoted = systemd_quote('SWIFTWALL_CONFIG=' + value)
                self.assertTrue(quoted.startswith('"') and quoted.endswith('"'))
                self.assertEqual(shlex.split(quoted)[0].replace('%%', '%'), 'SWIFTWALL_CONFIG=' + value)
        self.assertIn('%%h', systemd_quote('/%h'))
        self.assertIn('\\"', systemd_quote('/"'))
        self.assertIn('\\\\', systemd_quote('/\\'))

    def test_control_characters_cannot_inject_systemd_directives(self):
        for code in (*range(32), *range(127, 160)):
            with self.subTest(code=code), self.assertRaisesRegex(SetupError, 'Control characters'):
                systemd_quote('HOME=/safe' + chr(code) + '[Service]')
        with self.assertRaisesRegex(SetupError, 'Control characters'):
            SetupManager(self.home, self.bundle,
                         self.env | {'SWIFTWALL_CONFIG': str(self.home / 'config\nEnvironment=INJECTED=1')},
                         unit='swiftwall-private-test.service')
        self.assertFalse(self.transport.calls)

    def test_legacy_active_v01_upgrade_and_rollback_prove_inherited_environment(self):
        original = self.seed_legacy(active=True)
        saved = self.manager.state.read_bytes()
        self.assertEqual(self.transport.loaded_environment, {})
        self.manager.update()
        self.assertEqual(self.transport.loaded_environment, self.manager.engine_environment)
        self.assertTrue(any(a == ['systemctl', '--user', 'show-environment'] for a, _ in self.transport.calls))
        self.manager.rollback()
        self.assertEqual(engine.load_manifest(self.home), original)
        self.assertEqual(self.transport.loaded_environment, {})
        self.assertTrue(self.transport.active)
        self.assertTrue(self.transport.enabled)
        self.assertEqual(self.manager.state.read_bytes(), saved)
        self.assertEqual(self.manager.config.read_bytes(), CONFIG)

    def test_legacy_v02_owned_plugin_update_and_rollback_leave_plugin_untouched(self):
        original = self.seed_legacy(active=True, version='0.2.0')
        plugin.run('install', self.home, self.bundle)
        root = self.home / plugin.DESTINATION
        inodes = {name: (root / name).stat().st_ino for name in plugin.FILES}
        metadata = (self.home / plugin.MANIFEST).read_bytes()
        self.manager.update()
        self.assertEqual(self.transport.loaded_environment, self.manager.engine_environment)
        self.manager.rollback()
        self.assertEqual(engine.load_manifest(self.home), original)
        self.assertEqual(self.transport.loaded_environment, {})
        self.assertEqual(inodes, {name: (root / name).stat().st_ino for name in plugin.FILES})
        self.assertEqual((self.home / plugin.MANIFEST).read_bytes(), metadata)
        self.assertTrue(self.transport.active)
        self.assertTrue(self.transport.enabled)
        self.assertFalse(self.activation.enable_attempted)

    def test_legacy_active_upgrade_failure_recovers_inherited_unit(self):
        original = self.seed_legacy(active=True)
        self.transport.fail_hello = True
        with self.assertRaisesRegex(SetupError, 'original owned files/service recovered'):
            self.manager.update()
        self.assertEqual(engine.load_manifest(self.home), original)
        self.assertEqual(self.transport.loaded_environment, {})
        self.assertTrue(self.transport.active)
        self.assertTrue(self.transport.enabled)

    def test_legacy_inactive_unit_can_be_verified_without_starting_it(self):
        self.seed_legacy()
        self.manager.update()
        self.assertEqual(self.transport.loaded_environment, self.manager.engine_environment)
        self.assertFalse(self.transport.active)
        self.assertFalse(self.transport.enabled)
        self.assertFalse(any(a[0] == 'systemctl' and a[2] == 'start' for a, _ in self.transport.calls))

    def test_legacy_manager_mismatch_or_missing_proof_refused_before_stop(self):
        self.seed_legacy()
        valid = dict(self.transport.manager_environment)
        variants = [valid | {'HOME': '/wrong-home'}, valid | {'XDG_RUNTIME_DIR': '/wrong-runtime'},
                    valid | {'SWIFTWALL_CONFIG': '/wrong-config'}, valid | {'SWIFTWALL_EXTRA': 'unsupported'},
                    {k: v for k, v in valid.items() if k != 'XDG_RUNTIME_DIR'}]
        for values in variants:
            with self.subTest(values=values):
                self.transport.manager_environment = values
                before = self.tree()
                self.transport.calls.clear()
                with self.assertRaises(SetupError):
                    self.manager.update()
                self.assertEqual(self.tree(), before)
                self.assertFalse(any(a[0] == 'systemctl' and a[2] == 'stop' for a, _ in self.transport.calls))

    def test_legacy_environment_files_pass_unset_and_pam_are_never_bypassed(self):
        self.seed_legacy()
        before = self.tree()
        for key in ('EnvironmentFiles', 'PassEnvironment', 'UnsetEnvironment', 'PAMName'):
            self.transport.extra_properties = {key: 'not-supported'}
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, 'environment-unverified'):
                self.manager.update()
        self.assertEqual(self.tree(), before)
        self.assertFalse(any(a[0] == 'systemctl' and a[2] == 'stop' for a, _ in self.transport.calls))

    def test_legacy_manager_query_failure_does_not_leak_unrelated_secrets(self):
        self.seed_legacy()
        self.transport.manager_query_failure = True
        with self.assertRaisesRegex(SetupError, 'output omitted') as caught:
            self.manager.update()
        import traceback
        self.assertNotIn('API_SECRET', ''.join(traceback.format_exception(caught.exception)))
        self.assertFalse(self.manager.evidence.exists())
        self.assertFalse(any(a[0] == 'systemctl' and a[2] == 'stop' for a, _ in self.transport.calls))

    def test_legacy_relative_setter_uses_authoritative_manager_path(self):
        directory = self.home / 'commands'
        directory.mkdir(mode=0o700)
        command = directory / 'omarchy'
        shutil.copyfile(self.home / 'fake-setter', command)
        command.chmod(0o700)
        self.env['PATH'] = str(directory)
        self.env['SWIFTWALL_OMARCHY_COMMAND'] = str(command)
        self.manager = self.new_manager()
        self.transport.manager_environment = {'HOME': str(self.home), 'XDG_RUNTIME_DIR': self.env['XDG_RUNTIME_DIR'],
                                              'PATH': str(directory)}
        self.seed_legacy()
        self.manager.verify_existing_environment()
        self.manager._verify_effective_environment(self.transport.manager_environment)
        self.transport.manager_environment['PATH'] = '/usr/bin'
        with self.assertRaisesRegex(SetupError, 'environment-mismatch'):
            self.manager.verify_existing_environment()
        self.transport.manager_environment.pop('PATH')
        with self.assertRaisesRegex(SetupError, 'setter PATH is unverified'):
            self.manager.verify_existing_environment()
        self.assertFalse(any(a[0] == 'systemctl' and a[2] == 'stop' for a, _ in self.transport.calls))

    def test_legacy_actual_process_environment_is_checked_before_stop(self):
        self.seed_legacy(active=True)
        self.transport.running_env['SWIFTWALL_CONFIG'] = '/wrong-actual-config'
        before = self.tree()
        with self.assertRaisesRegex(SetupError, 'environment-mismatch'):
            self.manager.update()
        # The fixture's process observation records evidence only after validation.
        self.assertEqual(self.tree(), before)
        self.assertFalse(any(a[0] == 'systemctl' and a[2] == 'stop' for a, _ in self.transport.calls))

    def test_no_checkpoint_reports_limitation_without_io(self):
        before = self.tree()
        self.assertFalse(self.manager.rollback()['available'])
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.transport.calls)


if __name__ == '__main__':
    unittest.main()
