"""Offline hostile-archive and interrupted-download tests; no network execution."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import stat
import os
import unittest
from unittest.mock import Mock, patch
import urllib.error

import release_bundle as bundle
import setup_download as download
from test_release_bundle import minimal_elf

VERSION = '0.2.0-dev'


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / 'source'
        source.mkdir()
        for name in bundle.SOURCE_FILES:
            p = source / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('generated fixture\n')
        (source / 'Cargo.toml').write_text('[package]\nversion = "' + VERSION + '"\n')
        (source / 'manifest.json').write_text(json.dumps({'version': VERSION, 'id': 'io.github.henniedebruin.swiftwall'}))
        binary = self.root / 'binary'
        binary.write_bytes(minimal_elf())  # Parsed but never executed by this suite.
        binary.chmod(0o755)
        with patch.object(bundle, 'binary_version', return_value=VERSION):
            self.archive, self.sums = bundle.build_bundle(source, binary, self.root / 'out', VERSION, 'a' * 40)
        self.sha = hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def extract(self):
        return download.unpack(self.archive, self.root / 'extracted', VERSION, self.sha)

    def mutate_tar(self, transform):
        target = self.root / 'changed.tar.gz'
        with tarfile.open(self.archive) as src, tarfile.open(target, 'w:gz') as dst:
            for item in src:
                raw = src.extractfile(item).read()
                transform(item, dst)
                dst.addfile(item, io.BytesIO(raw))
        self.archive = target
        self.sha = hashlib.sha256(target.read_bytes()).hexdigest()

    def test_roundtrip_and_directory_verification(self):
        root, info = self.extract()
        self.assertEqual(info['source_commit'], 'a' * 40)
        self.assertEqual(download.verify_directory(root, VERSION), info)

    def test_writable_root_and_subdirectories_are_refused(self):
        root, _ = self.extract()
        for target in (root, root / 'bin', root / 'scripts'):
            with self.subTest(target=target):
                original = target.stat().st_mode & 0o777
                try:
                    target.chmod(0o777)
                    with self.assertRaises(download.DownloadError): download.verify_directory(root)
                finally:
                    target.chmod(original)
        self.assertEqual(download.verify_directory(root)['version'], VERSION)

    def test_writable_nonsticky_ancestor_is_refused(self):
        root, _ = self.extract()
        mode = self.root.stat().st_mode & 0o777
        try:
            self.root.chmod(0o777)
            with self.assertRaises(download.DownloadError): download.verify_directory(root)
        finally:
            self.root.chmod(mode)

    def test_extraction_uses_private_directories_even_with_permissive_umask(self):
        previous = os.umask(0)
        try:
            root, _ = self.extract()
        finally:
            os.umask(previous)
        for target in (root, root / 'bin', root / 'scripts', root / 'plugin'):
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)
        download.verify_directory(root)

    def test_extraction_refuses_writable_nonsticky_parent_before_writing(self):
        mode = self.root.stat().st_mode & 0o777
        try:
            self.root.chmod(0o777)
            with self.assertRaises(download.DownloadError): self.extract()
        finally:
            self.root.chmod(mode)
        self.assertFalse((self.root / 'extracted').exists())

    def test_foreign_directory_owner_is_refused(self):
        path = Mock()
        path.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=os.getuid() + 1)
        with self.assertRaises(download.DownloadError): download.bundle_directory(path)
        with self.assertRaises(download.DownloadError): download.bundle_directory(path, ancestor=True)

    def test_unexpected_empty_directory_and_hardlinked_file_are_refused(self):
        root, _ = self.extract()
        (root / 'extra-directory').mkdir()
        with self.assertRaises(download.DownloadError): download.verify_directory(root)
        (root / 'extra-directory').rmdir()
        os.link(root / 'LICENSE', self.root / 'linked-license')
        with self.assertRaises(download.DownloadError): download.verify_directory(root)

    def test_json_duplicate_keys_are_rejected_at_every_depth(self):
        for raw in ('{"a":1,"a":1}', '{"nested":{"x":1,"x":2}}'):
            with self.subTest(raw=raw), self.assertRaises(download.DownloadError): download.unique_json(raw)
        root, _ = self.extract()
        info = root / 'release.json'
        raw = info.read_text()
        info.write_text(raw.replace('"schema": 1', '"schema": 1, "schema": 1', 1))
        with self.assertRaises(download.DownloadError): download.verify_directory(root)

    def test_plugin_json_duplicate_key_is_rejected_even_with_matching_hash(self):
        root, _ = self.extract()
        manifest = root / 'manifest.json'
        manifest.write_text(manifest.read_text().replace('{', '{"version": "' + VERSION + '",', 1))
        info = root / 'release.json'
        metadata = json.loads(info.read_text())
        metadata['files']['manifest.json']['sha256'] = hashlib.sha256(manifest.read_bytes()).hexdigest()
        info.write_text(json.dumps(metadata))
        with self.assertRaises(download.DownloadError): download.verify_directory(root)

    def test_duplicate_inventory_json_rejected_before_extraction(self):
        target = self.root / 'duplicate-json.tar.gz'
        with tarfile.open(self.archive) as source, tarfile.open(target, 'w:gz') as dest:
            for member in source:
                raw = source.extractfile(member).read()
                if member.name.endswith('/release.json'):
                    raw = raw.replace(b'"schema": 1', b'"schema": 1, "schema": 1', 1)
                member.size = len(raw)
                dest.addfile(member, io.BytesIO(raw))
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        with self.assertRaises(download.DownloadError):
            download.unpack(target, self.root / 'not-extracted', VERSION, digest)
        self.assertFalse((self.root / 'not-extracted').exists())

    def test_corrupt_checksum_is_rejected_before_extraction(self):
        with self.assertRaises(download.DownloadError):
            download.unpack(self.archive, self.root / 'extracted', VERSION, '0' * 64)
        self.assertFalse((self.root / 'extracted').exists())

    def test_exact_version_not_latest_or_path_injection(self):
        for version in ('latest', '../x', '1.0.0/../../x', '1.0.0?x', ''):
            with self.subTest(version=version), self.assertRaises(ValueError):
                download.fetch_release(version, self.root)

    def test_checksums_strict_and_unique(self):
        name = self.archive.name
        self.assertEqual(download.checksum(self.sums.read_text(), name), self.sha)
        for value in ('', self.sums.read_text() * 2, 'x\n', 'a' * 65537):
            with self.subTest(value=value[:10]), self.assertRaises(download.DownloadError):
                download.checksum(value, name)

    def test_unexpected_path_traversal_and_links(self):
        for kind in ('traversal', 'absolute', 'symlink', 'hardlink', 'extra'):
            with self.subTest(kind=kind):
                path = self.root / (kind + '.tar.gz')
                with tarfile.open(path, 'w:gz') as tar:
                    item = tarfile.TarInfo(bundle.archive_root(VERSION) + 'release.json')
                    if kind == 'traversal': item.name += '/../escape'
                    if kind == 'absolute': item.name = '/escape'
                    if kind == 'extra': item.name += '.extra'
                    if kind in ('symlink', 'hardlink'):
                        item.type = tarfile.SYMTYPE if kind == 'symlink' else tarfile.LNKTYPE
                        item.linkname = '/escape'
                    item.mode = 0o644
                    tar.addfile(item, io.BytesIO(b''))
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                with self.assertRaises(ValueError):
                    download.unpack(path, self.root / ('dest-' + kind), VERSION, digest)

    def test_duplicate_archive_member(self):
        once = []
        def duplicate(item, dst):
            if not once:
                once.append(True)
                dup = tarfile.TarInfo(item.name)
                dup.size = 0
                dup.mode = item.mode
                dst.addfile(dup, io.BytesIO(b''))
        self.mutate_tar(duplicate)
        with self.assertRaises(download.DownloadError): self.extract()

    def test_changed_permissions(self):
        self.mutate_tar(lambda item, dst: setattr(item, 'mode', 0o777))
        with self.assertRaises(download.DownloadError): self.extract()

    def test_wrong_payload_hash(self):
        root, _ = self.extract()
        (root / 'plugin/Panel.qml').write_text('modified')
        with self.assertRaises(download.DownloadError): download.verify_directory(root)

    def test_inventory_cannot_mislabel_a_script_as_the_static_engine(self):
        root, _ = self.extract()
        binary = root / 'bin/swiftwall'
        binary.write_bytes(b'#!/bin/sh\nexit 0\n')
        metadata = json.loads((root / 'release.json').read_text())
        metadata['files']['bin/swiftwall']['sha256'] = hashlib.sha256(binary.read_bytes()).hexdigest()
        (root / 'release.json').write_text(json.dumps(metadata))
        with self.assertRaises(download.DownloadError): download.verify_directory(root)

    def test_wrong_release_version(self):
        root, _ = self.extract()
        with self.assertRaises(download.DownloadError): download.verify_directory(root, '9.9.9')

    def test_added_files_and_symlinks(self):
        root, _ = self.extract()
        extra = root / 'secret'
        extra.write_text('fixture')
        with self.assertRaises(download.DownloadError): download.verify_directory(root)
        extra.unlink()
        extra.symlink_to(self.root / 'outside')
        with self.assertRaises(download.DownloadError): download.verify_directory(root)

    def test_bounded_decompression(self):
        with patch.object(download, 'MAX_EXPANDED', 128):
            with self.assertRaises(download.DownloadError): self.extract()

    def test_existing_destination_preserved(self):
        path = self.root / 'extracted'
        path.mkdir()
        (path / 'keep').write_text('keep')
        with self.assertRaises(download.DownloadError): self.extract()
        self.assertEqual((path / 'keep').read_text(), 'keep')

    def test_strict_redirect_hosts(self):
        for url in ('http://github.com/x', 'https://evil.test/x', 'https://user@github.com/x',
                    'https://github.com:8080/x', 'file:///x'):
            with self.subTest(url=url), self.assertRaises(download.DownloadError): download.trusted_url(url)
        self.assertEqual(download.trusted_url('https://release-assets.githubusercontent.com/x'), 'https://release-assets.githubusercontent.com/x')

    def test_download_size_limit_cleans_only_new_file(self):
        class Response(io.BytesIO):
            def geturl(self): return 'https://github.com/x'
        class Opener:
            def open(self, request, timeout): return Response(b'x' * 32)
        target = self.root / 'download'
        with self.assertRaises(download.DownloadError): download.download('https://github.com/x', target, 16, Opener())
        self.assertFalse(target.exists())
        target.write_text('original')
        with self.assertRaises(download.DownloadError): download.download('https://github.com/x', target, 16, Opener())
        self.assertEqual(target.read_text(), 'original')

    def test_unavailable_download_preserves_preexisting_file(self):
        class Opener:
            def open(self, request, timeout): raise urllib.error.URLError('fixture offline')
        target = self.root / 'keep'
        target.write_text('keep')
        with self.assertRaises(download.DownloadError): download.download('https://github.com/x', target, 16, Opener())
        self.assertEqual(target.read_text(), 'keep')

    def test_download_total_deadline(self):
        class Response(io.BytesIO):
            def geturl(self): return 'https://github.com/x'
        class Opener:
            def open(self, request, timeout): return Response(b'x')
        with patch.object(download.time, 'monotonic', side_effect=(0, 61)):
            with self.assertRaises(download.DownloadError): download.download('https://github.com/x', self.root / 'partial', 16, Opener())
        self.assertFalse((self.root / 'partial').exists())


if __name__ == '__main__':
    unittest.main()
