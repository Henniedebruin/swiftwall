"""Offline archive/binary/launcher smoke for a future packaged release (Python 3.11+).

No downloads, installation, systemd, Quickshell, or real Omarchy commands. This is
NOT full clean-user setup/systemd/widget acceptance. Only run approved artifacts:
checksum/inventory checks and private paths are not an adversarial-code sandbox.
The static ELF verifier checks shape, not musl provenance; the build must supply
that provenance. Successful smoke always executes the actual extracted binary.
"""
import argparse
import hashlib
import json
import os
import re
import resource
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from contextlib import contextmanager
from itertools import chain
from pathlib import Path

# Support imports must not leave source-tree bytecode behind either.
sys.dont_write_bytecode = True

from release_bundle import TARGET, archive_name, validate_version
from setup_download import checksum, unique_json, unpack, verify_directory

LIMITATION = ('ARCHIVE/BINARY/LAUNCHER smoke only; NOT full clean-user setup, '
              'systemd or widget acceptance; static ELF validation does not prove musl provenance.')
MAX_OUTPUT = 1024 * 1024
TIMEOUT = 10


class SmokeError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise SmokeError(message)


@contextmanager
def private_umask():
    old = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(old)


def child_limits():
    # Bound file-backed diagnostics as well as memory consumption of responses.
    resource.setrlimit(resource.RLIMIT_FSIZE, (2 * MAX_OUTPUT, 2 * MAX_OUTPUT))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def start(argv, cwd, env, output):
    return subprocess.Popen([str(arg) for arg in argv], cwd=cwd, env=env,
                            stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                            # This smoke runner is single-threaded; only sets rlimits before exec.
                            start_new_session=True, preexec_fn=child_limits)  # noqa: PLW1509


def exited(process):
    # Do not reap until group cleanup: reserve the leader PID against reuse.
    return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


def await_exit(process, timeout: float = TIMEOUT):
    deadline = time.monotonic() + timeout
    while not exited(process):
        if time.monotonic() >= deadline:
            raise SmokeError('Extracted process exceeded its bounded deadline')
        time.sleep(0.02)


def stop_reap(process):
    # Give daemon workers a bounded opportunity to clean their own child groups.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await_exit(process, timeout=2)
    except SmokeError:
        pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=3)


def run(argv, cwd, env):
    with tempfile.TemporaryFile() as output:
        process = start(argv, cwd, env, output)
        try:
            await_exit(process)
        finally:
            stop_reap(process)
        output.seek(0)
        raw = output.read(MAX_OUTPUT + 1)
    require(process.returncode == 0, 'Extracted command failed: ' + str(argv[-1])
            + ' (exit ' + str(process.returncode) + '): '
            + raw[:4096].decode('utf-8', errors='replace'))
    require(len(raw) <= MAX_OUTPUT, 'Extracted command exceeded output limit')
    return raw.decode('utf-8')


def clean_environment(root):
    """Return an allowlisted environment, never a copy of the caller's session."""
    home = root / 'home'
    # The ordinary setup CLI supports home-local config/state. Sibling XDG roots
    # are an invalid user environment, not a valid clean-user installation test.
    paths = {'HOME': home, **{key: home / name for key, name in (
        ('XDG_CONFIG_HOME', 'config'), ('XDG_STATE_HOME', 'state'),
        ('XDG_DATA_HOME', 'data'), ('XDG_CACHE_HOME', 'cache'),
        ('XDG_RUNTIME_DIR', 'runtime'))}, 'TMPDIR': root / 'tmp', 'PATH': root / 'tools'}
    for path in paths.values():
        path.mkdir(mode=0o700)
    # No cargo/rustc, shell, systemctl, busctl, Omarchy or Quickshell in PATH.
    (paths['PATH'] / 'python3').symlink_to(Path(sys.executable).resolve())
    return {**{key: str(path) for key, path in paths.items()},
            'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
            'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1'}


def user_tree(env):
    keys = ('HOME', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_DATA_HOME',
            'XDG_CACHE_HOME', 'XDG_RUNTIME_DIR')
    result = {}
    for key in keys:
        root = Path(env[key])
        records = {}
        for path in chain((root,), root.rglob('*')):
            require(len(records) < 256, 'Unexpectedly large dry-run filesystem tree')
            info = path.lstat()
            content = None
            if stat.S_ISREG(info.st_mode):
                require(info.st_size <= MAX_OUTPUT, 'Unexpectedly large dry-run file')
                content = hashlib.sha256(path.read_bytes()).hexdigest()
            elif stat.S_ISLNK(info.st_mode):
                content = os.readlink(path)
            records[str(path.relative_to(root))] = (
                info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
                info.st_mtime_ns, content)
        result[key] = records
    return result


def png(rgb):
    def chunk(kind, data):
        return (struct.pack('>I', len(data)) + kind + data
                + struct.pack('>I', zlib.crc32(kind + data)))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\0' + bytes(rgb))) + chunk(b'IEND', b''))


def daemon_fixture(root, env):
    pictures = root / 'pictures'
    pictures.mkdir(mode=0o700)
    images = [pictures / 'one.png', pictures / 'two.png']
    for image, color in zip(images, ((255, 0, 0), (0, 0, 255))):
        image.write_bytes(png(color))
    config = root / 'daemon.toml'
    config.write_text('interval_seconds = 3600\nselection = "sequential"\nfolders = ['
                      + json.dumps(str(pictures)) + ']\n', encoding='utf-8')
    link = root / 'background'
    link.symlink_to(images[0])
    calls = root / 'setter-calls.jsonl'
    setter = root / 'fake-omarchy'
    # Absolute interpreter, hardcoded private allowlist, no actual desktop calls.
    setter.write_text('#!' + str(Path(sys.executable).resolve()) + '\n'
                      'import json, os, sys\n'
                      'allowed = ' + repr([str(p) for p in images]) + '\n'
                      'if len(sys.argv) != 5 or sys.argv[1:4] != ["theme", "bg", "set"] or sys.argv[4] not in allowed:\n'
                      '    sys.exit(97)\n'
                      'link = ' + repr(str(link)) + '\n'
                      'temporary = link + ".setter"\n'
                      'os.symlink(sys.argv[4], temporary)\n'
                      'os.replace(temporary, link)\n'
                      'with open(' + repr(str(calls)) + ', "a", encoding="utf-8") as stream:\n'
                      '    stream.write(json.dumps(sys.argv[1:]) + "\\n")\n', encoding='utf-8')
    setter.chmod(0o700)
    overrides = {
        'SWIFTWALL_CONFIG': str(config),
        'SWIFTWALL_STATE_DIR': str(root / 'daemon-state'),
        'SWIFTWALL_RUNTIME_DIR': str(root / 'daemon-runtime'),
        'SWIFTWALL_BACKGROUND_LINK': str(link),
        'SWIFTWALL_OMARCHY_COMMAND': str(setter),
    }
    return {**env, **overrides}, config, link, calls, images


def request(path, op, version):
    deadline = time.monotonic() + 2
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(2)
        peer.connect(str(path))
        peer.sendall(json.dumps({'v': 1, 'id': op, 'op': op}).encode() + b'\n')
        raw = bytearray()
        while b'\n' not in raw:
            remaining = deadline - time.monotonic()
            require(remaining > 0, 'Protocol response deadline exceeded')
            peer.settimeout(remaining)
            block = peer.recv(min(65536, MAX_OUTPUT + 1 - len(raw)))
            require(bool(block), 'Protocol response ended before newline')
            raw.extend(block)
            require(len(raw) <= MAX_OUTPUT, 'Protocol response exceeded limit')
    require(raw.endswith(b'\n') and raw.count(b'\n') == 1, 'Expected exactly one NDJSON response')
    value = unique_json(raw)
    require(isinstance(value, dict) and value.get('ok') is True
            and type(value.get('v')) is int and value['v'] == 1
            and type(value.get('protocol_version')) is int and value['protocol_version'] == 1
            and value.get('id') == op and value.get('daemon_version') == version,
            'Invalid versioned/correlated protocol response for ' + op)
    return value


def wait_status(process, path, version, predicate):
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        require(not exited(process), 'Extracted daemon exited unexpectedly')
        try:
            value = request(path, 'status', version)['status']
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(0.03)
            continue
        if predicate(value):
            return value
        time.sleep(0.03)
    raise SmokeError('Daemon status condition exceeded bounded deadline')


def validate_state(state, images):
    """Validate the exact durable outcome of one successful sequential apply."""
    require(type(state) is dict, 'Invalid durable state object')
    require(type(state.get('version')) is int and state['version'] == 1
            and state.get('paused') is True
            and 'protective_suspension' in state and state['protective_suspension'] is None
            and state.get('observed') == str(images[1])
            and 'requested' in state and state['requested'] is None
            and type(state.get('last_result')) is dict
            and state['last_result'].get('success') is True,
            'Unexpected durable apply/pause state')
    carousel = state.get('carousel')
    require(type(carousel) is dict, 'Invalid durable carousel')
    paths, bag, history = (carousel.get(key) for key in ('paths', 'bag', 'history'))
    expected = sorted(str(image) for image in images)
    require(type(paths) is list and paths == expected, 'Invalid durable carousel paths')
    require(type(bag) is list and all(type(p) is str for p in bag)
            and len(bag) == len(set(bag)) and set(bag) <= set(expected)
            and bag == [str(images[0])], 'Invalid durable carousel bag')
    require(type(history) is list and history == [str(images[1])]
            and type(carousel.get('cursor')) is int and carousel['cursor'] == 0
            and history[carousel['cursor']] == state['observed'],
            'Invalid durable carousel history/cursor')
    return state


def paused_settled(status):
    return (type(status) is dict and status.get('paused') is True
            and status.get('applying') is False and status.get('scanning') is False
            and status.get('persistence_pending') is False
            and type(status.get('eligible_images')) is int and status['eligible_images'] == 2)


def validate_paused_status(status, images):
    require(paused_settled(status) and status.get('state') == 'paused'
            and 'next_change_in_seconds' in status and status['next_change_in_seconds'] is None
            and status.get('observed_wallpaper') == str(images[1])
            and 'requested_wallpaper' in status and status['requested_wallpaper'] is None
            and all(key in status and status[key] is None for key in
                    ('last_persistence_error', 'error', 'protective_suspension')),
            'Unexpected settled paused status')


def redact_evidence(value, aliases):
    """Typed allowlist: never preserve arbitrary strings, keys, messages or paths."""
    if type(value) is not dict:
        return {'invalid_type': type(value).__name__}
    result = {}
    for key in ('version', 'cursor', 'eligible_images'):
        if key in value:
            result[key] = value[key] if type(value[key]) is int else {'invalid_type': type(value[key]).__name__}
    for key in ('paused', 'applying', 'scanning', 'persistence_pending', 'success'):
        if key in value:
            result[key] = value[key] if type(value[key]) is bool else {'invalid_type': type(value[key]).__name__}
    for key in ('observed', 'requested', 'observed_wallpaper', 'requested_wallpaper'):
        if key in value:
            item = value[key]
            result[key] = None if item is None else aliases.get(item, 'unknown-path') if type(item) is str else {'invalid_type': type(item).__name__}
    for key in ('paths', 'bag', 'history'):
        if key in value:
            items = value[key]
            result[key] = ([aliases.get(item, 'unknown-path') if type(item) is str else
                            {'invalid_type': type(item).__name__} for item in items[:32]]
                           if type(items) is list else {'invalid_type': type(items).__name__})
    for key in ('carousel', 'last_result', 'last_application_result'):
        if key in value:
            result[key] = None if value[key] is None else redact_evidence(value[key], aliases)
    for key in ('error', 'last_persistence_error', 'protective_suspension', 'config_worker_error',
                'next_change_in_seconds'):
        if key in value:
            result[key + '_present'] = value[key] is not None
    return result


def file_evidence(path, aliases, parse=False):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1):
                return {'unavailable': True}
            raw = stream.read(MAX_OUTPUT + 1)
        result = {'size': info.st_size, 'sha256': hashlib.sha256(raw).hexdigest(),
                  'hash_scope': 'prefix' if len(raw) > MAX_OUTPUT else 'complete'}
        if parse and len(raw) <= MAX_OUTPUT:
            try:
                result['state'] = redact_evidence(unique_json(raw), aliases)
            except ValueError:
                result['invalid_json'] = True
                result['type'] = 'bytes'
        return result
    except OSError:
        return {'unavailable': True}


def daemon_smoke(binary, root, cwd, env, version, evidence=None):
    env, config, link, calls, images = daemon_fixture(root, env)
    original = config.read_bytes()
    initial_link = os.readlink(link)
    initial_inode = link.lstat().st_ino
    control = Path(env['SWIFTWALL_RUNTIME_DIR']) / 'control.sock'
    state_path = Path(env['SWIFTWALL_STATE_DIR']) / 'state.json'
    aliases = {str(image): 'fixture-' + str(index + 1) for index, image in enumerate(images)}
    evidence = evidence if evidence is not None else {}

    def capture(phase):
        evidence[phase + '_state'] = file_evidence(state_path, aliases, parse=True)

    def wait(process, predicate):
        def observe(status):
            evidence['status'] = redact_evidence(status, aliases)
            return predicate(status)
        return wait_status(process, control, version, observe)

    def read_state():
        require(state_path.stat().st_size <= MAX_OUTPUT, 'Oversized flushed state')
        require(stat.S_IMODE(state_path.stat().st_mode) == 0o600, 'State is not private')
        return validate_state(unique_json(state_path.read_bytes()), images)

    def shutdown(process):
        require(config.read_bytes() == original, 'Daemon changed configuration')
        os.killpg(process.pid, signal.SIGTERM)
        await_exit(process)

    def revalidate_shutdown(process):
        require(process.returncode == 0, 'Daemon did not exit cleanly on SIGTERM')
        require(config.read_bytes() == original and not control.exists(), 'Unclean daemon shutdown')
        return read_state()

    evidence['stage'] = 'initial-startup-and-controls'
    with tempfile.TemporaryFile() as output:
        process = start([binary, 'daemon'], cwd, env, output)
        try:
            status = wait(process, lambda s: s['eligible_images'] == 2 and s['scanning'] is False)
            require(status['state'] == 'running' and not status['applying'], 'Unexpected startup state')
            require(os.readlink(link) == initial_link and link.lstat().st_ino == initial_inode
                    and not calls.exists(), 'Startup changed the private background')
            hello = request(control, 'hello', version)
            require(hello['settings']['folders'] == [str(images[0].parent)]
                    and hello['settings']['interval_seconds'] == 3600
                    and hello['settings']['selection'] == 'sequential', 'Unexpected hello settings')
            require(request(control, 'pause', version)['status']['paused'] is True, 'Pause failed')
            require(request(control, 'resume', version)['status']['paused'] is False, 'Resume failed')
            request(control, 'next', version)
            status = wait(process, lambda s: s['applying'] is False and s['last_application_result'] is not None)
            require(status['last_application_result']['success'] is True
                    and status['error'] is None and status['protective_suspension'] is None,
                    'Fake setter application failed')
            records = [unique_json(line) for line in calls.read_bytes().splitlines()]
            require(records == [['theme', 'bg', 'set', str(images[1])]]
                    and os.readlink(link) == str(images[1])
                    and status['observed_wallpaper'] == str(images[1]), 'Unexpected setter invocation/link')
            # Leave an unmistakable final state for the SIGTERM durability assertion.
            request(control, 'pause', version)
            evidence['stage'] = 'initial-persistence-settle'
            validate_paused_status(wait(process, paused_settled), images)
            evidence['stage'] = 'initial-sigterm'
            shutdown(process)
        finally:
            try:
                stop_reap(process)
            finally:
                capture('sigterm')
                output.seek(0)
                raw = output.read(MAX_OUTPUT + 1)
                evidence['first_diagnostics'] = {'size': output.seek(0, 2),
                    'sha256': hashlib.sha256(raw).hexdigest(), 'hash_scope': 'bounded-prefix'}
        evidence['stage'] = 'initial-durable-state-validation'
        state = revalidate_shutdown(process)
        require(output.tell() <= MAX_OUTPUT, 'Daemon diagnostics exceeded limit')

    before = (os.readlink(link), link.lstat().st_ino, calls.read_bytes(), config.read_bytes())
    evidence['stage'] = 'restart-recovery'
    with tempfile.TemporaryFile() as output:
        # Same extracted executable, cwd, environment and durable fixture paths.
        process = start([binary, 'daemon'], cwd, env, output)
        try:
            validate_paused_status(wait(process, paused_settled), images)
            recovered = read_state()
            require(recovered == state, 'Restart changed recovered durable state')
            require((os.readlink(link), link.lstat().st_ino, calls.read_bytes(), config.read_bytes())
                    == before, 'Restart changed private background/setter/config')
            shutdown(process)
        finally:
            try:
                stop_reap(process)
            finally:
                capture('restart')
                output.seek(0)
                raw = output.read(MAX_OUTPUT + 1)
                evidence['restart_diagnostics'] = {'size': output.seek(0, 2),
                    'sha256': hashlib.sha256(raw).hexdigest(), 'hash_scope': 'bounded-prefix'}
        require(revalidate_shutdown(process) == state, 'Restart shutdown changed durable state')
        require((os.readlink(link), link.lstat().st_ino, calls.read_bytes(), config.read_bytes())
                == before, 'Restart shutdown changed private fixtures')
        require(output.tell() <= MAX_OUTPUT, 'Daemon diagnostics exceeded limit')
    evidence['stage'] = 'restart-and-shutdown-verified'
    return {'startup_link_unchanged': True, 'protocol': 1,
            'restart_recovered': True, 'restart_background_unchanged': True,
            'pause_resume': True, 'fake_setter_success': True, 'config_intact': True,
            'sigterm_state_flushed': True, 'exit_code': process.returncode}


def check_candidate(archive, checksums, version, source_commit, evidence=None):
    """Return schema-1 success report; collect redacted evidence before cleanup."""
    validate_version(version)
    require(re.fullmatch('[a-f0-9]{40}', source_commit) is not None,
            'source_commit must be exactly 40 lowercase hex characters')
    archive, checksums = Path(archive).absolute(), Path(checksums).absolute()
    require(archive.name == archive_name(version), 'Archive filename must match exact requested version')
    with checksums.open('rb') as stream:
        sums = stream.read(65537)
    digest = checksum(sums.decode('ascii'), archive.name)
    with private_umask(), tempfile.TemporaryDirectory(prefix='swrc-', dir='/tmp') as temporary:
        root = Path(temporary)
        bundle, inventory = unpack(archive, root / 'bundle', version, digest)
        verified = verify_directory(bundle, version)
        require(verified == inventory and verified['source_commit'] == source_commit
                and verified['target'] == TARGET, 'Release source commit/target identity mismatch')
        env = clean_environment(root)
        initial_user_tree = user_tree(env)
        cwd = root / 'unrelated'
        cwd.mkdir(mode=0o700)
        binary = bundle / 'bin/swiftwall'
        require(run([binary, '--version'], cwd, env) in
                ('swiftwall ' + version, 'swiftwall ' + version + '\n'),
                'Actual extracted binary version mismatch')
        launcher = bundle / 'swiftwall-setup'
        help_text = run([launcher, '--help'], cwd, env)
        require('SwiftWall Setup' in help_text and '--dry-run' in help_text, 'Launcher help mismatch')
        dry = unique_json(run([launcher, 'install', '--dry-run'], cwd, env))
        require(dry.get('operation') == 'install' and dry.get('dry_run') is True
                and dry.get('downloads') is False and dry.get('changes') is False,
                'Launcher did not return a safe dry-run plan')
        require(user_tree(env) == initial_user_tree, 'Launcher dry-run wrote private user data')
        daemon = daemon_smoke(binary, root, cwd, env, version, evidence=evidence)
        verify_directory(bundle, version)  # No modified helpers or bytecode left behind.
        return {'schema': 1, 'ok': True, 'scope': 'archive/binary/launcher',
                'limitation': LIMITATION, 'version': version, 'source_commit': source_commit,
                'target': TARGET, 'archive_sha256': digest,
                'checks': {'inventory': True, 'actual_binary_version': True,
                           'actual_launcher_help': True, 'actual_launcher_install_dry_run': True,
                           'daemon': daemon}, 'temporary_paths_removed': True}


def main(argv=None, evidence=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--checksums', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    evidence = evidence if evidence is not None else {}
    try:
        report = check_candidate(args.archive, args.checksums, args.version, args.source_commit,
                                 evidence=evidence)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        report = {'schema': 1, 'ok': False, 'scope': 'archive/binary/launcher',
                  'limitation': LIMITATION, 'error': 'Release candidate check failed',
                  'error_type': type(exc).__name__}
    if evidence:
        report['evidence'] = evidence
    text = json.dumps(report, sort_keys=True, indent=2) + '\n'
    if args.report:
        try:
            # Never clobber artifacts, caller files, or follow an existing symlink.
            with args.report.open('x', encoding='utf-8') as stream:
                stream.write(text)
        except OSError as exc:
            print('release candidate report: ' + str(exc), file=sys.stderr)
            return 1
    print(text, end='')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
