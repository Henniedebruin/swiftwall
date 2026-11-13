"""Offline archive/binary/launcher smoke for a future packaged release (Python 3.11+).

No downloads, installation, systemd, Quickshell, or real Omarchy commands. This is
NOT full clean-user setup/systemd/widget acceptance. Only run approved artifacts:
checksum/inventory checks and private paths are not an adversarial-code sandbox.
The static ELF verifier checks shape, not musl provenance; the build must supply
that provenance. Successful smoke always executes the actual extracted binary.
"""
import argparse
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
    require(process.returncode == 0, 'Extracted command failed: ' + str(argv[-1]))
    require(len(raw) <= MAX_OUTPUT, 'Extracted command exceeded output limit')
    return raw.decode('utf-8')


def clean_environment(root):
    """Return an allowlisted environment, never a copy of the caller's session."""
    paths = {key: root / name for key, name in (
        ('HOME', 'home'), ('XDG_CONFIG_HOME', 'config'), ('XDG_STATE_HOME', 'state'),
        ('XDG_DATA_HOME', 'data'), ('XDG_CACHE_HOME', 'cache'),
        ('XDG_RUNTIME_DIR', 'runtime'), ('TMPDIR', 'tmp'), ('PATH', 'tools'))}
    for path in paths.values():
        path.mkdir(mode=0o700)
    # No cargo/rustc, shell, systemctl, busctl, Omarchy or Quickshell in PATH.
    (paths['PATH'] / 'python3').symlink_to(Path(sys.executable).resolve())
    return {**{key: str(path) for key, path in paths.items()},
            'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
            'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1'}


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


def daemon_smoke(binary, root, cwd, env, version):
    env, config, link, calls, images = daemon_fixture(root, env)
    original = config.read_bytes()
    initial_link = os.readlink(link)
    initial_inode = link.lstat().st_ino
    control = Path(env['SWIFTWALL_RUNTIME_DIR']) / 'control.sock'
    with tempfile.TemporaryFile() as output:
        process = start([binary, 'daemon'], cwd, env, output)
        try:
            status = wait_status(process, control, version,
                                 lambda s: s['eligible_images'] == 2 and not s['scanning'])
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
            status = wait_status(process, control, version,
                                 lambda s: not s['applying'] and s['last_application_result'] is not None)
            require(status['last_application_result']['success'] is True
                    and status['error'] is None and status['protective_suspension'] is None,
                    'Fake setter application failed')
            records = [unique_json(line) for line in calls.read_bytes().splitlines()]
            require(records == [['theme', 'bg', 'set', str(images[1])]]
                    and os.readlink(link) == str(images[1])
                    and status['observed_wallpaper'] == str(images[1]), 'Unexpected setter invocation/link')
            # Leave an unmistakable final state for the SIGTERM durability assertion.
            request(control, 'pause', version)
            require(config.read_bytes() == original, 'Daemon changed configuration')
            os.killpg(process.pid, signal.SIGTERM)
            await_exit(process)
        finally:
            stop_reap(process)
        require(process.returncode == 0, 'Daemon did not exit cleanly on SIGTERM')
        require(output.tell() <= MAX_OUTPUT, 'Daemon diagnostics exceeded limit')
    state_path = Path(env['SWIFTWALL_STATE_DIR']) / 'state.json'
    require(state_path.stat().st_size <= MAX_OUTPUT, 'Oversized flushed state')
    state = unique_json(state_path.read_bytes())
    require(state['version'] == 1 and state['paused'] is True
            and state['observed'] == str(images[1]) and state['requested'] == str(images[1])
            and state['last_result']['success'] is True, 'SIGTERM did not flush expected state')
    require(stat.S_IMODE(state_path.stat().st_mode) == 0o600, 'State is not private')
    require(config.read_bytes() == original and not control.exists(), 'Unclean daemon shutdown')
    return {'environment': env, 'startup_link_unchanged': True, 'protocol': 1,
            'pause_resume': True, 'fake_setter_success': True, 'config_intact': True,
            'sigterm_state_flushed': True, 'exit_code': process.returncode}


def check_candidate(archive, checksums, version, source_commit):
    """Return schema-1 success report; raise on any failed check. No persistent writes."""
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
        require(all(not list(Path(env[key]).iterdir()) for key in
                    ('HOME', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_DATA_HOME',
                     'XDG_CACHE_HOME', 'XDG_RUNTIME_DIR')), 'Launcher dry-run wrote private user data')
        daemon = daemon_smoke(binary, root, cwd, env, version)
        verify_directory(bundle, version)  # No modified helpers or bytecode left behind.
        return {'schema': 1, 'ok': True, 'scope': 'archive/binary/launcher',
                'limitation': LIMITATION, 'version': version, 'source_commit': source_commit,
                'target': TARGET, 'archive_sha256': digest,
                'checks': {'inventory': True, 'actual_binary_version': True,
                           'actual_launcher_help': True, 'actual_launcher_install_dry_run': True,
                           'daemon': daemon}, 'temporary_paths_removed': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--checksums', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    try:
        report = check_candidate(args.archive, args.checksums, args.version, args.source_commit)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        report = {'schema': 1, 'ok': False, 'scope': 'archive/binary/launcher',
                  'limitation': LIMITATION, 'error': str(exc)}
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
