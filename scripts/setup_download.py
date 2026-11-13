"""Bounded, exact-version GitHub downloads and allowlisted release verification."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from release_bundle import (EXECUTABLE_FILES, PAYLOAD_FILES, TARGET, archive_name,
                            archive_root, safe_path, validate_elf, validate_version)

MAX_ARCHIVE = 64 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
MAX_MANIFEST = 128 * 1024
HOSTS = frozenset(('github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'))
BASE = 'https://github.com/Henniedebruin/swiftwall/releases/download/'


class DownloadError(ValueError):
    pass


def unique_json(data):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DownloadError('Duplicate JSON object key in release metadata')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=object_pairs)


def bundle_directory(path, *, ancestor=False):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise DownloadError('Bundle directories must not be symlinks or special files')
    if ancestor:
        # A root-owned sticky /tmp protects user-owned child entries. An ordinary
        # writable ancestor, or one owned by another user, does not.
        sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if info.st_uid not in (0, os.getuid()) or (info.st_mode & 0o022 and not sticky_root):
            raise DownloadError('Bundle ancestor permits replacement by another user')
    elif info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise DownloadError('Bundle directories must be user-owned and not group/world writable')


def trusted_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme != 'https' or p.hostname not in HOSTS or p.username or p.password or p.port not in (None, 443):
        raise DownloadError('Release download redirected outside trusted HTTPS GitHub hosts')
    return url


class Redirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        trusted_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, target, limit, opener=None):
    """Exclusive output; socket operations have a timeout; no URL/secrets in errors."""
    trusted_url(url)
    opener = opener or urllib.request.build_opener(Redirect())
    target = Path(target)
    created = False
    deadline = time.monotonic() + 60
    try:
        with opener.open(urllib.request.Request(url, headers={'User-Agent': 'SwiftWall-Setup/1'}), timeout=15) as response:
            trusted_url(response.geturl())
            with target.open('xb') as output:
                created = True
                total = 0
                while True:
                    if time.monotonic() >= deadline:
                        raise DownloadError('Release download deadline exceeded')
                    chunk = response.read(min(65536, limit - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise DownloadError('Release download exceeded size limit')
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
    except FileExistsError:
        raise DownloadError('Refusing an existing download destination') from None
    except (OSError, urllib.error.URLError, ValueError) as exc:
        # Never unlink a path that predated this call.
        if created:
            target.unlink(missing_ok=True)
        if isinstance(exc, DownloadError):
            raise
        raise DownloadError('Release download unavailable or interrupted; no installation performed') from None


def checksum(text, filename):
    if len(text) > 65536:
        raise DownloadError('Oversized checksum document')
    found = []
    for line in text.splitlines():
        match = re.fullmatch(r'([a-f0-9]{64})  ([A-Za-z0-9_.-]+)', line)
        if match is None:
            raise DownloadError('Malformed SHA256SUMS')
        if match[2] == filename:
            found.append(match[1])
    if len(found) != 1:
        raise DownloadError('Expected exactly one checksum for the selected version/architecture')
    return found[0]


def checked_hash(path, expected):
    if not isinstance(expected, str) or re.fullmatch('[a-f0-9]{64}', expected) is None:
        raise DownloadError('An exact SHA-256 digest is required')
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(65536):
            digest.update(block)
    if digest.hexdigest() != expected:
        raise DownloadError('Release checksum mismatch; refusing all execution and installation')


def validate_inventory(payload, version):
    try:
        info = unique_json(payload['release.json'])
        if set(info) != {'schema', 'version', 'target', 'source_commit', 'files'}:
            raise ValueError('schema')
        if type(info['schema']) is not int or info['schema'] != 1 or info['version'] != version or info['target'] != TARGET:
            raise ValueError('identity')
        if re.fullmatch('[a-f0-9]{40}', info['source_commit']) is None:
            raise ValueError('commit')
        if set(info['files']) != set(PAYLOAD_FILES) or set(payload) != {*PAYLOAD_FILES, 'release.json'}:
            raise ValueError('allowlist')
        for name in PAYLOAD_FILES:
            item = info['files'][name]
            mode = 0o755 if name in EXECUTABLE_FILES else 0o644
            if set(item) != {'sha256', 'mode'} or type(item['mode']) is not int or item['mode'] != mode:
                raise ValueError('mode')
            if hashlib.sha256(payload[name]).hexdigest() != item['sha256']:
                raise ValueError('hash')
        validate_elf(payload['bin/swiftwall'])
        manifest = unique_json(payload['manifest.json'])
        if manifest['version'] != version or manifest['id'] != 'io.github.henniedebruin.swiftwall':
            raise ValueError('plugin identity')
        return info
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise DownloadError('Invalid release inventory, version, or payload hash') from exc


def unpack(archive, destination, version, expected_sha256):
    """Validate every byte/path before exclusive, manual extraction. No extractall."""
    validate_version(version)
    archive, destination = Path(archive), Path(destination).absolute()
    for parent in destination.parents:
        bundle_directory(parent, ancestor=True)
    if archive.stat().st_size > MAX_ARCHIVE:
        raise DownloadError('Archive exceeds size limit')
    checked_hash(archive, expected_sha256)
    if destination.exists() or destination.is_symlink():
        raise DownloadError('Extraction destination must not exist')
    payload, modes = {}, {}
    prefix = archive_root(version)
    # Cap decompression before tarfile parses potentially large extension headers.
    with tempfile.TemporaryFile() as expanded:
        try:
            with gzip.open(archive, 'rb') as stream:
                total = 0
                while block := stream.read(65536):
                    total += len(block)
                    if total > MAX_EXPANDED:
                        raise DownloadError('Expanded archive exceeds size limit')
                    expanded.write(block)
            expanded.seek(0)
            with tarfile.open(fileobj=expanded, mode='r:') as tar:
                for member in tar:
                    if len(payload) >= len(PAYLOAD_FILES) + 1:
                        raise DownloadError('Too many archive members')
                    if not member.isfile() or not member.name.startswith(prefix):
                        raise DownloadError('Only regular files within the exact version root are accepted')
                    name = safe_path(member.name[len(prefix):])
                    if name not in {*PAYLOAD_FILES, 'release.json'} or name in payload:
                        raise DownloadError('Unexpected or duplicate archive path')
                    limit = MAX_MANIFEST if name == 'release.json' else MAX_ARCHIVE
                    if member.size < 0 or member.size > limit:
                        raise DownloadError('Oversized archive member')
                    mode = 0o755 if name in EXECUTABLE_FILES else 0o644
                    if member.mode != mode:
                        raise DownloadError('Unexpected archive file permissions')
                    payload[name] = tar.extractfile(member).read(limit + 1)
                    modes[name] = mode
                    if len(payload[name]) != member.size:
                        raise DownloadError('Truncated archive member')
        except (OSError, tarfile.TarError, EOFError) as exc:
            raise DownloadError('Invalid or truncated release archive') from exc
    info = validate_inventory(payload, version)
    destination.mkdir(mode=0o700)
    try:
        for name, data in payload.items():
            path = destination / name
            parent = destination
            for part in Path(name).parts[:-1]:
                parent = parent / part
                parent.mkdir(mode=0o700, exist_ok=True)
            with path.open('xb') as output:
                output.write(data)
            path.chmod(modes[name])
    except BaseException:
        shutil.rmtree(destination)
        raise
    return destination, info


def verify_directory(directory, version=None):
    """Recheck a downloaded/extracted portable bundle before using any executable."""
    root = Path(directory).absolute()
    for parent in root.parents:
        bundle_directory(parent, ancestor=True)
    bundle_directory(root)
    expected = {*PAYLOAD_FILES, 'release.json'}
    allowed_directories = {str(parent) for name in expected
                           for parent in Path(name).parents if str(parent) != '.'}
    actual = set()
    payload = {}
    total = 0
    for path in root.rglob('*'):
        name = path.relative_to(root).as_posix()
        info = path.lstat()
        mode = info.st_mode
        if stat.S_ISLNK(mode):
            raise DownloadError('Symlinks are not permitted in a release bundle')
        if stat.S_ISDIR(mode):
            if name not in allowed_directories:
                raise DownloadError('Unexpected bundle directory')
            bundle_directory(path)
            continue
        if (not stat.S_ISREG(mode) or name not in expected or info.st_size > MAX_ARCHIVE
                or info.st_uid != os.getuid() or info.st_nlink != 1):
            raise DownloadError('Unexpected bundle file')
        total += path.stat().st_size
        if total > MAX_EXPANDED:
            raise DownloadError('Bundle exceeds size limit')
        payload[name] = path.read_bytes()
        actual.add(name)
        if stat.S_IMODE(mode) != (0o755 if name in EXECUTABLE_FILES else 0o644):
            raise DownloadError('Bundle file permissions changed')
    if actual != expected or len(payload['release.json']) > MAX_MANIFEST:
        raise DownloadError('Incomplete or oversized release inventory')
    try:
        recorded = unique_json(payload['release.json'])['version']
        validate_version(recorded)
    except (ValueError, KeyError, TypeError) as exc:
        raise DownloadError('Malformed release version') from exc
    if version is not None and recorded != version:
        raise DownloadError('Requested version differs from bundle version')
    return validate_inventory(payload, recorded)


def fetch_release(version, destination, expected_sha256=None, opener=None):
    validate_version(version)
    destination = Path(destination)
    name = archive_name(version)
    base = BASE + 'v' + version + '/'
    sums = destination / 'SHA256SUMS'
    archive = destination / name
    download(base + 'SHA256SUMS', sums, 65536, opener)
    try:
        digest = checksum(sums.read_text(encoding='ascii'), name)
    except (UnicodeError, ValueError) as exc:
        raise DownloadError('Invalid release checksums') from exc
    if expected_sha256 is not None and digest != expected_sha256:
        raise DownloadError('Published checksum differs from the independently supplied digest')
    download(base + name, archive, MAX_ARCHIVE, opener)
    return unpack(archive, destination / ('swiftwall-' + version), version, digest)
