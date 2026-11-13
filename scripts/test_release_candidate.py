"""Offline rejection/safety tests. Generated ELF fixtures are NEVER musl execution.

Only helper-process and fake-setter tests execute Python; no fixture is substituted
for the release smoke binary and no successful artifact acceptance is claimed.
"""
import contextlib
import copy
import hashlib
import io
import json
import os
import signal
import socket
import struct
import sys
import tarfile
import tempfile
import threading
import tomllib
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import check_release_candidate as smoke
import release_bundle as bundle
import setup_download as download
from test_release_bundle import minimal_elf

VERSION = '0.2.0-dev'
COMMIT = 'a' * 40


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rc-test-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / 'source'
        source.mkdir()
        for name in bundle.SOURCE_FILES:
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('generated fixture\n')
        (source / 'Cargo.toml').write_text('version = "unused"\n[package]\nversion = "' + VERSION + '"\n')
        (source / 'manifest.json').write_text(json.dumps(
            {'id': 'io.github.henniedebruin.swiftwall', 'version': VERSION}))
        binary = self.root / 'unexecuted-elf'
        binary.write_bytes(minimal_elf())
        binary.chmod(0o755)
        # Packaging metadata fixture only: never execute the generated ELF.
        with patch.object(bundle, 'binary_version', return_value=VERSION):
            self.archive, self.sums = bundle.build_bundle(
                source, binary, self.root / 'out', VERSION, COMMIT)

    def check(self, commit=COMMIT):
        return smoke.check_candidate(self.archive, self.sums, VERSION, commit)

    def extract(self):
        return download.unpack(self.archive, self.root / 'extracted', VERSION,
                               hashlib.sha256(self.archive.read_bytes()).hexdigest())[0]

    def rewrite(self, change):
        records = []
        with tarfile.open(self.archive) as source:
            for item in source:
                stream = source.extractfile(item)
                assert stream is not None
                records.append((item, stream.read()))
        with tarfile.open(self.archive, 'w:gz') as target:
            for item, data in records:
                data = change(item, data)
                item.size = len(data)
                target.addfile(item, io.BytesIO(data))
        self.sums.write_text(hashlib.sha256(self.archive.read_bytes()).hexdigest()
                             + '  ' + self.archive.name + '\n')

    def assert_no_execution(self):
        with patch.object(smoke, 'start', side_effect=AssertionError('must not execute')) as start:
            with self.assertRaises((ValueError, OSError)):
                self.check()
            start.assert_not_called()

    def test_checksum_mismatch_fails_before_execution(self):
        self.sums.write_text('0' * 64 + '  ' + self.archive.name + '\n')
        self.assert_no_execution()

    def test_checksum_duplicates_and_oversize_rejected(self):
        original = self.sums.read_text()
        for text in (original * 2, 'x' * 65537):
            self.sums.write_text(text)
            self.assert_no_execution()

    def test_exact_commit_mismatch_before_execution(self):
        with patch.object(smoke, 'start') as start:
            with self.assertRaisesRegex(smoke.SmokeError, 'identity mismatch'):
                self.check('b' * 40)
            start.assert_not_called()

    def test_invalid_commit_and_archive_name(self):
        for commit in ('a' * 39, 'A' * 40, 'latest'):
            with self.assertRaises(ValueError):
                self.check(commit)
        self.archive = self.archive.rename(self.archive.with_name('wrong.tar.gz'))
        self.assert_no_execution()

    def test_duplicate_json_rejected_before_execution(self):
        def duplicate(item, data):
            if item.name.endswith('/release.json'):
                return data.replace(b'"schema": 1', b'"schema": 1, "schema": 1')
            return data
        self.rewrite(duplicate)
        self.assert_no_execution()
        with self.assertRaises(download.DownloadError):
            download.unique_json('{"files":{"x":1,"x":2}}')

    def test_wrong_target_rejected(self):
        self.rewrite(lambda item, data: data.replace(TARGET_BYTES, b'x86_64-unknown-linux-gnu')
                     if item.name.endswith('/release.json') else data)
        self.assert_no_execution()

    def test_script_binary_rejected_even_with_matching_inventory(self):
        script = b'#!/bin/sh\nexit 0\n'
        def replace(item, data):
            if item.name.endswith('/bin/swiftwall'):
                return script
            if item.name.endswith('/release.json'):
                info = json.loads(data)
                info['files']['bin/swiftwall']['sha256'] = hashlib.sha256(script).hexdigest()
                return json.dumps(info).encode()
            return data
        self.rewrite(replace)
        self.assert_no_execution()

    def test_dynamic_gnu_elf_rejected_even_with_matching_inventory(self):
        elf = bytearray(minimal_elf())
        # Add a PT_INTERP program header (the verifier forbids GNU and musl dynamic ELF).
        phoff = struct.unpack_from('<Q', elf, 32)[0]
        struct.pack_into('<I', elf, phoff, 3)
        def replace(item, data):
            if item.name.endswith('/bin/swiftwall'):
                return bytes(elf)
            if item.name.endswith('/release.json'):
                info = json.loads(data)
                info['files']['bin/swiftwall']['sha256'] = hashlib.sha256(elf).hexdigest()
                return json.dumps(info).encode()
            return data
        self.rewrite(replace)
        self.assert_no_execution()

    def test_writable_directories_and_ancestor_rejected(self):
        root = self.extract()
        for path in (root, root / 'bin', root / 'scripts', self.root):
            old = path.stat().st_mode & 0o777
            try:
                path.chmod(0o777)
                with self.assertRaises(download.DownloadError):
                    download.verify_directory(root, VERSION)
            finally:
                path.chmod(old)

    def test_permissions_inventory_and_links_rechecked(self):
        root = self.extract()
        binary = root / 'bin/swiftwall'
        binary.chmod(0o777)
        with self.assertRaises(download.DownloadError):
            download.verify_directory(root, VERSION)
        binary.chmod(0o755)
        (root / 'extra').symlink_to(self.root / 'outside')
        with self.assertRaises(download.DownloadError):
            download.verify_directory(root, VERSION)

    def test_environment_is_private_and_allowlisted(self):
        with patch.dict(os.environ, {'PYTHONPATH': '/repo', 'RUSTUP_HOME': '/rust',
                                    'DBUS_SESSION_BUS_ADDRESS': 'real',
                                    'SWIFTWALL_CONFIG': '/real/config'}):
            env = smoke.clean_environment(self.root)
        for key in ('PYTHONPATH', 'RUSTUP_HOME', 'DBUS_SESSION_BUS_ADDRESS', 'SWIFTWALL_CONFIG'):
            self.assertNotIn(key, env)
        self.assertEqual([p.name for p in Path(env['PATH']).iterdir()], ['python3'])
        for key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_RUNTIME_DIR'):
            self.assertEqual(Path(env[key]).stat().st_mode & 0o777, 0o700)
        daemon_env, config, link, calls, images = smoke.daemon_fixture(self.root, env)
        self.assertEqual({key for key in daemon_env if key.startswith('SWIFTWALL_')}, {
            'SWIFTWALL_CONFIG', 'SWIFTWALL_STATE_DIR', 'SWIFTWALL_RUNTIME_DIR',
            'SWIFTWALL_BACKGROUND_LINK', 'SWIFTWALL_OMARCHY_COMMAND'})
        self.assertTrue(config.exists())
        self.assertFalse(calls.exists())
        self.assertEqual(link.resolve(), images[0])
        # Validate generated PNG chunk lengths, CRCs, and actual pixel stream.
        for image in images:
            data = image.read_bytes()
            self.assertEqual(data[:8], b'\x89PNG\r\n\x1a\n')
            offset = 8
            while offset < len(data):
                length = struct.unpack_from('>I', data, offset)[0]
                kind = data[offset + 4:offset + 8]
                payload = data[offset + 8:offset + 8 + length]
                self.assertEqual(struct.unpack_from('>I', data, offset + 8 + length)[0],
                                 zlib.crc32(kind + payload))
                if kind == b'IDAT':
                    self.assertEqual(len(zlib.decompress(payload)), 4)
                offset += length + 12

    def test_real_packaged_launcher_dry_run_uses_supported_clean_user_xdg_paths(self):
        # Real launcher/modules, generated non-executed ELF only. This is a CLI
        # regression for the CI environment bug, NOT musl artifact acceptance.
        source = Path(__file__).resolve().parents[1]
        binary = self.root / 'cli-only-elf'
        binary.write_bytes(minimal_elf())
        binary.chmod(0o755)
        version = tomllib.loads((source / 'Cargo.toml').read_text())['package']['version']
        self.assertEqual(json.loads((source / 'manifest.json').read_text())['version'], version)
        with patch.object(bundle, 'binary_version', return_value=version):
            archive, sums = bundle.build_bundle(source, binary, self.root / 'real-cli-out', version, COMMIT)
        digest = download.checksum(sums.read_text(), archive.name)
        extracted, _ = download.unpack(archive, self.root / 'real-cli-bundle', version, digest)
        environment_root = self.root / 'clean-user'
        environment_root.mkdir(mode=0o700)
        env = smoke.clean_environment(environment_root)
        other = self.root / 'unrelated-cwd'
        other.mkdir()
        for key in ('XDG_CONFIG_HOME', 'XDG_STATE_HOME'):
            self.assertTrue(Path(env[key]).is_relative_to(Path(env['HOME'])))
        sentinel = Path(env['HOME']) / 'unchanged-user-file'
        sentinel.write_text('original fixture')
        before = smoke.user_tree(env)
        output = smoke.run([extracted / 'swiftwall-setup', 'install', '--dry-run'], other, env)
        self.assertTrue(json.loads(output)['dry_run'])
        self.assertEqual(smoke.user_tree(env), before)
        sentinel.write_text('changed fixture')
        self.assertNotEqual(smoke.user_tree(env), before)
        changed = smoke.user_tree(env)
        sentinel.chmod(0o400)
        self.assertNotEqual(smoke.user_tree(env), changed)
        (Path(env['XDG_CONFIG_HOME']) / 'unexpected').write_text('fixture')
        self.assertNotEqual(smoke.user_tree(env), before)
        self.assertEqual(list(Path(env['HOME']).rglob('config.toml')), [])
        self.assertFalse((Path(env['HOME']) / '.local/bin/swiftwall').exists())

    def test_fake_setter_accepts_only_exact_fixture_paths(self):
        env = smoke.clean_environment(self.root)
        env, _, link, calls, images = smoke.daemon_fixture(self.root, env)
        setter = env['SWIFTWALL_OMARCHY_COMMAND']
        with self.assertRaises(smoke.SmokeError):
            smoke.run([setter, 'theme', 'bg', 'set', '/not/a/fixture.png'], self.root, env)
        self.assertFalse(calls.exists())
        self.assertEqual(link.resolve(), images[0])
        smoke.run([setter, 'theme', 'bg', 'set', images[1]], self.root, env)
        self.assertEqual(link.resolve(), images[1])
        self.assertEqual(json.loads(calls.read_text()), ['theme', 'bg', 'set', str(images[1])])

    def test_helper_timeout_kills_and_reaps_group(self):
        env = smoke.clean_environment(self.root)
        with tempfile.TemporaryFile() as output:
            process = smoke.start([sys.executable, '-c', 'import time; time.sleep(30)'], self.root, env, output)
            try:
                with self.assertRaises(smoke.SmokeError):
                    smoke.await_exit(process, timeout=0.05)
            finally:
                smoke.stop_reap(process)
        self.assertEqual(process.returncode, -signal.SIGTERM)
        with self.assertRaises(ProcessLookupError):
            os.killpg(process.pid, 0)

    def test_execution_boundary_uses_unmodified_extracted_binary_and_cleans_failure(self):
        observed = {}
        def reject(argv, cwd, env, output):
            binary = Path(argv[0])
            observed['root'] = binary.parents[2]
            self.assertEqual(argv[1:], ['--version'])
            self.assertEqual(binary.read_bytes(), minimal_elf())
            self.assertEqual(binary.relative_to(observed['root']).as_posix(), 'bundle/bin/swiftwall')
            self.assertEqual(cwd, observed['root'] / 'unrelated')
            self.assertNotIn('PYTHONPATH', env)
            self.assertNotIn('SWIFTWALL_CONFIG', env)
            raise smoke.SmokeError('Intentional boundary stop; fixture must never execute')
        with patch.object(smoke, 'start', side_effect=reject) as start:
            with self.assertRaisesRegex(smoke.SmokeError, 'Intentional boundary stop'):
                self.check()
            self.assertEqual(start.call_count, 1)
        self.assertFalse(observed['root'].exists())

    def test_archive_permissions_and_traversal_rejected(self):
        def unsafe(item, data):
            item.mode = 0o777
            return data
        self.rewrite(unsafe)
        self.assert_no_execution()
        def traversal(item, data):
            item.name = bundle.archive_root(VERSION) + '../outside'
            return data
        self.rewrite(traversal)
        self.assert_no_execution()
        self.assertFalse((self.root / 'outside').exists())

    def test_evidence_forwarded_before_cleanup_and_never_added_to_artifact(self):
        evidence = {}
        roots = []
        def daemon(binary, root, cwd, env, version, evidence=None):
            roots.append(root)
            evidence['sigterm_state'] = {'state': {'requested': None, 'paused': True}}
            raise smoke.SmokeError('generated isolated failure')
        before = self.archive.read_bytes()
        outputs = ['swiftwall ' + VERSION, 'SwiftWall Setup --dry-run',
                   json.dumps({'operation': 'install', 'dry_run': True,
                               'downloads': False, 'changes': False})]
        with patch.object(smoke, 'run', side_effect=outputs), \
                patch.object(smoke, 'daemon_smoke', side_effect=daemon):
            with self.assertRaises(smoke.SmokeError):
                smoke.check_candidate(self.archive, self.sums, VERSION, COMMIT, evidence)
        self.assertFalse(roots[0].exists())
        self.assertIsNone(evidence['sigterm_state']['state']['requested'])
        self.assertEqual(self.archive.read_bytes(), before)
        with tarfile.open(self.archive) as archive:
            payload = {name.split('/', 1)[1] for name in archive.getnames()}
            self.assertEqual(payload, set(bundle.PAYLOAD_FILES) | {'release.json'})

    def test_main_embeds_only_collected_evidence_not_exception_strings(self):
        report = self.root / 'release-smoke.json'
        def fail(*args, evidence=None):
            evidence['status'] = {'last_persistence_error_present': True}
            raise smoke.SmokeError('/home/foreign-user/secret error')
        argv = ['--archive', str(self.archive), '--checksums', str(self.sums),
                '--version', VERSION, '--source-commit', COMMIT, '--report', str(report)]
        with patch.object(smoke, 'check_candidate', side_effect=fail), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(smoke.main(argv), 1)
        value = json.loads(report.read_text())
        self.assertTrue(value['evidence']['status']['last_persistence_error_present'])
        self.assertNotIn('foreign-user', report.read_text())

    def test_failure_report_and_existing_report_preserved(self):
        report = self.root / 'report.json'
        argv = ['--archive', str(self.archive), '--checksums', str(self.sums),
                '--version', VERSION, '--source-commit', 'b' * 40, '--report', str(report)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(smoke.main(argv), 1)
        value = json.loads(report.read_text())
        self.assertFalse(value['ok'])
        self.assertIn('NOT full clean-user', value['limitation'])
        original = report.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(smoke.main(argv), 1)
        self.assertEqual(report.read_bytes(), original)


class DurableStateTests(unittest.TestCase):
    """Generated JSON/process mocks only; NOT musl artifact execution."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rc-state-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.images = [self.root / 'one.png', self.root / 'two.png']
        first, second = map(str, self.images)
        self.state = {'version': 1, 'paused': True, 'protective_suspension': None,
                      'observed': second, 'requested': None,
                      'last_result': {'success': True, 'message': 'fixture only'},
                      'carousel': {'paths': [first, second], 'bag': [first],
                                   'history': [second], 'cursor': 0}}

    def test_completed_apply_requested_none_not_last_target(self):
        self.assertIs(smoke.validate_state(self.state, self.images), self.state)
        self.state['requested'] = str(self.images[1])
        with self.assertRaises(smoke.SmokeError):
            smoke.validate_state(self.state, self.images)

    def test_every_required_field_missing_or_corrupt_rejected(self):
        for key in self.state:
            for wrong in ('missing', 'foreign-string', [], False, 9):
                state = copy.deepcopy(self.state)
                if wrong == 'missing':
                    del state[key]
                else:
                    state[key] = wrong
                with self.subTest(key=key, wrong=wrong), self.assertRaises(smoke.SmokeError):
                    smoke.validate_state(state, self.images)
        for key in self.state['carousel']:
            for wrong in ('missing', None, True, -1, {}, [], ['foreign'],
                          [str(self.images[0])] * 2):
                state = copy.deepcopy(self.state)
                if wrong == 'missing':
                    del state['carousel'][key]
                else:
                    state['carousel'][key] = wrong
                with self.subTest(key=key, wrong=wrong), self.assertRaises(smoke.SmokeError):
                    smoke.validate_state(state, self.images)
        for wrong in (None, {}, {'success': 1}, {'success': False}):
            state = {**self.state, 'last_result': wrong}
            with self.assertRaises(smoke.SmokeError):
                smoke.validate_state(state, self.images)
        for wrong in (True, 1.0):
            state = {**self.state, 'version': wrong}
            with self.assertRaises(smoke.SmokeError):
                smoke.validate_state(state, self.images)

    def test_redactor_never_copies_unknown_strings_or_keys(self):
        secret = '/home/foreign-user/secret wallpaper ERROR environment'
        aliases = {str(self.images[0]): 'fixture-1', str(self.images[1]): 'fixture-2'}
        value = copy.deepcopy(self.state)
        value.update({secret: secret, 'error': secret, 'last_persistence_error': secret,
                      'observed': secret, 'paused': secret, 'cursor': secret,
                      'history': [secret, str(self.images[0]), {secret: secret}],
                      'requested': {secret: secret}})
        value['last_result']['message'] = secret
        text = json.dumps(smoke.redact_evidence(value, aliases))
        self.assertNotIn(secret, text)
        self.assertNotIn(str(self.root), text)
        self.assertIn('fixture-1', text)
        self.assertIn('error_present', text)
        self.assertNotIn('message', text)

    def test_invalid_json_evidence_hash_size_type_only(self):
        path = self.root / 'state.json'
        raw = b'not json /home/foreign-user/private'
        path.write_bytes(raw)
        value = smoke.file_evidence(path, {}, parse=True)
        self.assertEqual(value['size'], len(raw))
        self.assertEqual(value['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(value['type'], 'bytes')
        self.assertTrue(value['invalid_json'])
        self.assertNotIn('foreign-user', json.dumps(value))

    def test_missing_state_and_linked_evidence_are_not_accepted(self):
        path = self.root / 'missing-state.json'
        self.assertEqual(smoke.file_evidence(path, {}, parse=True), {'unavailable': True})
        outside = self.root / 'unrelated-private-file'
        outside.write_text('/home/foreign-user/never-copy-this')
        path.symlink_to(outside)
        self.assertEqual(smoke.file_evidence(path, {}, parse=True), {'unavailable': True})
        path.unlink()
        os.link(outside, path)
        self.assertEqual(smoke.file_evidence(path, {}, parse=True), {'unavailable': True})

    def test_settled_status_requires_explicit_typed_fields(self):
        status = {'state': 'paused', 'paused': True, 'applying': False, 'scanning': False,
                  'eligible_images': 2, 'persistence_pending': False,
                  'last_persistence_error': None, 'error': None, 'protective_suspension': None,
                  'next_change_in_seconds': None, 'requested_wallpaper': None,
                  'observed_wallpaper': str(self.images[1])}
        smoke.validate_paused_status(status, self.images)
        for key in status:
            for wrong in ('missing', 'foreign', 1):
                value = dict(status)
                if wrong == 'missing':
                    del value[key]
                else:
                    value[key] = wrong
                with self.subTest(key=key, wrong=wrong), self.assertRaises(smoke.SmokeError):
                    smoke.validate_paused_status(value, self.images)

    def lifecycle(self, corrupt=None):
        env = smoke.clean_environment(self.root)
        status = {'state': 'running', 'paused': False, 'applying': False, 'scanning': False,
                  'eligible_images': 2, 'persistence_pending': False, 'last_persistence_error': None,
                  'error': None, 'protective_suspension': None, 'requested_wallpaper': None,
                  'next_change_in_seconds': None, 'last_application_result': None}
        starts = []
        evidence = {}
        def start(argv, cwd, actual_env, output):
            output.write(b'/home/foreign-user/private diagnostic message\n')
            starts.append((argv, cwd, actual_env))
            if len(starts) == 2 and corrupt:
                corrupt(status, actual_env)
            return SimpleNamespace(pid=999999, returncode=0)
        def request(control, op, version):
            images = [self.root / 'pictures' / name for name in ('one.png', 'two.png')]
            if op == 'hello':
                return {'settings': {'folders': [str(images[0].parent)],
                                     'interval_seconds': 3600, 'selection': 'sequential'}}
            if op in ('pause', 'resume'):
                status['paused'] = op == 'pause'
                status['state'] = 'paused' if status['paused'] else 'running'
            if op == 'next':
                link = self.root / 'background'
                link.unlink()
                link.symlink_to(images[1])
                (self.root / 'setter-calls.jsonl').write_text(json.dumps(
                    ['theme', 'bg', 'set', str(images[1])]) + '\n')
                status['observed_wallpaper'] = str(images[1])
                status['last_application_result'] = {'success': True}
            if op == 'pause' and status['last_application_result']:
                state = copy.deepcopy(self.state)
                state.update(observed=str(images[1]), carousel={
                    'paths': list(map(str, images)), 'bag': [str(images[0])],
                    'history': [str(images[1])], 'cursor': 0})
                directory = self.root / 'daemon-state'
                directory.mkdir(exist_ok=True)
                path = directory / 'state.json'
                path.write_text(json.dumps(state))
                path.chmod(0o600)
            return {'status': status}
        def wait(process, control, version, predicate):
            self.assertTrue(predicate(status))
            return status
        binary = self.root / 'unexecuted-binary'
        with patch.object(smoke, 'start', side_effect=start), \
                patch.object(smoke, 'request', side_effect=request), \
                patch.object(smoke, 'wait_status', side_effect=wait), \
                patch.object(smoke, 'await_exit'), patch.object(smoke, 'stop_reap'), \
                patch.object(smoke.os, 'killpg'):
            try:
                result = smoke.daemon_smoke(binary, self.root, self.root, env, VERSION, evidence)
            finally:
                self.evidence = evidence
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[0], starts[1])
        self.assertNotIn('environment', result)
        return result

    def test_restart_same_binary_fixtures_and_recovered_state(self):
        self.assertTrue(self.lifecycle()['restart_recovered'])
        self.assertIn('restart_state', self.evidence)
        self.assertNotIn(str(self.root), json.dumps(self.evidence))
        self.assertNotIn('foreign-user', json.dumps(self.evidence))
        self.assertGreater(self.evidence['restart_diagnostics']['size'], 0)
        self.assertIn('sha256', self.evidence['first_diagnostics'])

    def test_restart_deadline_or_requested_or_persistence_error_rejected(self):
        # Each subcase needs fresh private fixtures.
        for field, wrong in (('next_change_in_seconds', 1), ('requested_wallpaper', 'foreign'),
                             ('last_persistence_error', 'foreign')):
            with tempfile.TemporaryDirectory(prefix='rc-restart-', dir='/tmp') as temporary:
                self.root = Path(temporary)
                with self.subTest(field=field), self.assertRaises(smoke.SmokeError):
                    self.lifecycle(lambda status, env: status.update({field: wrong}))
                self.assertIn('restart_state', self.evidence)
                self.assertNotIn('foreign', json.dumps(self.evidence))

    def test_restart_mutated_state_and_background_rejected(self):
        def state_changed(status, env):
            path = Path(env['SWIFTWALL_STATE_DIR']) / 'state.json'
            value = json.loads(path.read_text())
            value['carousel']['bag'] = []
            path.write_text(json.dumps(value))
        def background_changed(status, env):
            Path(env['SWIFTWALL_BACKGROUND_LINK']).unlink()
            Path(env['SWIFTWALL_BACKGROUND_LINK']).symlink_to(self.root / 'pictures/one.png')
        for corrupt in (state_changed, background_changed):
            with tempfile.TemporaryDirectory(prefix='rc-restart-', dir='/tmp') as temporary:
                self.root = Path(temporary)
                with self.assertRaises(smoke.SmokeError):
                    self.lifecycle(corrupt)
                self.assertIn('restart_state', self.evidence)


TARGET_BYTES = bundle.TARGET.encode()


class ProtocolTests(unittest.TestCase):
    def exchange(self, raw):
        with tempfile.TemporaryDirectory(prefix='rc-sock-', dir='/tmp') as temporary:
            path = Path(temporary) / 'control.sock'
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(path))
                server.listen(1)
                def serve():
                    with server.accept()[0] as peer:
                        peer.recv(4096)
                        try:
                            peer.sendall(raw)
                        except BrokenPipeError:
                            pass
                worker = threading.Thread(target=serve, daemon=True)
                worker.start()
                try:
                    return smoke.request(path, 'hello', VERSION)
                finally:
                    worker.join(timeout=3)
                    self.assertFalse(worker.is_alive())

    def test_versioned_bounded_correlated_response(self):
        value = {'ok': True, 'v': 1, 'protocol_version': 1, 'id': 'hello', 'daemon_version': VERSION}
        self.assertEqual(self.exchange(json.dumps(value).encode() + b'\n'), value)
        for field, wrong in (('v', 2), ('protocol_version', True), ('id', 'wrong'),
                             ('daemon_version', '9.9.9'), ('ok', False)):
            with self.subTest(field=field), self.assertRaises(smoke.SmokeError):
                self.exchange(json.dumps({**value, field: wrong}).encode() + b'\n')
        for raw in (b'{}', b'x' * (smoke.MAX_OUTPUT + 1), b'{}\n{}\n'):
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                self.exchange(raw)


if __name__ == '__main__':
    unittest.main()
