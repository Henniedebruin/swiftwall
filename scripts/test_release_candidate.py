"""Offline rejection/safety tests. Generated ELF fixtures are NEVER musl execution.

Only helper-process and fake-setter tests execute Python; no fixture is substituted
for the release smoke binary and no successful artifact acceptance is claimed.
"""
import contextlib
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
import unittest
import zlib
from pathlib import Path
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
