#!/usr/bin/env python3
"""Opt-in real SetupManager/systemd rehearsal, never a production deployment.

Run: python3 -B scripts/rehearse_setup.py --run \
    --v01 /absolute/path/to/v01 --v01-sha256 EXPECTED_V01_SHA256 \
    --v02 /absolute/path/to/v02 --v02-sha256 EXPECTED_V02_SHA256
Tests: python3 -B -m unittest discover -s scripts -p test_rehearse_setup.py

Only the two caller-selected engine files, pinned by independently obtained
expected SHA256 values, are read as binary sources. No artifact path or hash
has a default. This is a source rehearsal, NOT downloaded-artifact acceptance. Bundles
use the plugin's explicit allowlist and artificial release.json binary metadata;
this is NOT release authentication/inventory verification (test_release_bundle.py
and setup_download's tests cover that separate boundary). No setup CLI is used.

Activation is a PRIVATE JSON registry/bar simulator, NOT an Omarchy shell test.
All engine/plugin transactions, IPC, systemd, proc/executable and SO_PEERCRED
checks are real. Faults are explicitly labelled: masked readiness replies,
postcommit installer exception, and simulated activation exception.
The generated-unit case starts without a loaded service, then loads the actual
installed unit's seven Environment directives verbatim into isolation scaffolding.
Only its copied ExecStart replaces %h with the private home; loaded values are
queried and compared exactly before the first real start. The other cases
retain their explicit fixture environment, not a pure inherited legacy contract.
The metadata-only case changes only a private plugin description, retaining the
actual v0.2 binary and manifest version, and exercises real update and rollback.

The runner adds --runtime to enable/disable; it never rewrites unit names or
systemctl output. Typed busctl fallback calls are restricted to LoadUnit for the
selected name and EnvironmentFiles on its confirmed canonical object path. Only
service_control interprets the real typed response; no property is fabricated.
SetupManager currently rejects enabled-runtime on subsequent operations. That
API limitation is recorded, then active update/rollback tests
use a genuinely disabled disposable unit. No production unit is even queried.
SIGINT/SIGTERM trigger finally cleanup; SIGKILL/power loss cannot be recovered.
"""

import argparse
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
import zlib

sys.dont_write_bytecode = True
import install_support as engine
import plugin_support as plugin
from plugin_activation import PLUGIN_ID, ActivationOutcome, without_plugin

from setup_support import SetupManager, SetupError

ROOT = Path(__file__).resolve().parents[1]

CASES = ('fresh', 'v01-update-rollback', 'identical-update', 'readiness',
         'installer', 'activation', 'generated-unit', 'metadata-only')
CONTRACT_KEYS = frozenset({'HOME', 'XDG_RUNTIME_DIR', 'SWIFTWALL_CONFIG',
                           'SWIFTWALL_STATE_DIR', 'SWIFTWALL_RUNTIME_DIR',
                           'SWIFTWALL_BACKGROUND_LINK', 'SWIFTWALL_OMARCHY_COMMAND'})


def require(value, message):
    if not value:
        raise RuntimeError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class Sources:
    v01: Path
    v02: Path
    v01_sha256: str
    v02_sha256: str

    def __post_init__(self):
        for path in (self.v01, self.v02):
            require(path.is_absolute(), 'Binary source paths must be absolute')
        require(self.v01 != self.v02, 'Binary source paths must be distinct')
        for expected in (self.v01_sha256, self.v02_sha256):
            require(re.fullmatch(r'[0-9a-f]{64}', expected),
                    'Expected SHA256 must be 64 lowercase hexadecimal characters')

    def verify(self):
        hashes = {}
        for path, expected in ((self.v01, self.v01_sha256), (self.v02, self.v02_sha256)):
            plugin.check_path(path)
            require(path.is_file() and os.access(path, os.X_OK), f'Missing approved executable: {path}')
            actual = digest(path)
            require(actual == expected, f'Approved binary hash differs: {path}')
            hashes[str(path)] = actual
        return hashes


def private_directory(path):
    plugin.check_path(path, directory=True)
    info = path.stat()
    require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
            f'Not an owned private directory: {path}')


def png():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\0\x30\x60\x90')) + chunk(b'IEND', b''))


def unit_text(home, env, target):
    # Generated paths are deliberately restricted: no quoting/specifier ambiguity.
    require(re.fullmatch(r'/tmp/sws-[A-Za-z0-9_-]+/home', str(home)), 'Unsafe private home')
    require(re.fullmatch(r'swiftwall-setup-rehearsal-[a-f0-9]{32}\.target', target), 'Unsafe target')
    require(all(re.fullmatch(r'[A-Z_]+', k) and re.fullmatch(r'[A-Za-z0-9_./:-]+', v)
                for k, v in env.items()), 'Unsafe unit environment')
    return ('[Unit]\nDescription=Disposable SwiftWall setup rehearsal\n'
            '[Service]\nType=simple\nRestart=no\nKillMode=control-group\n'
            'TimeoutStopSec=5\nUMask=0077\nNoNewPrivileges=yes\n'
            'ProtectHome=yes\nProtectSystem=strict\n'
            f'ReadWritePaths={home}\nWorkingDirectory={home}\n'
            f'ExecStart={home}/.local/bin/swiftwall daemon\n'
            + ''.join(f'Environment="{k}={v}"\n' for k, v in sorted(env.items()))
            + f'[Install]\nWantedBy={target}\n')


def environment_assignments(words):
    entries = shlex.split(words)
    values = dict(entry.split('=', 1) for entry in entries)
    require(len(values) == len(entries), 'Duplicate environment assignments')
    return values


def descriptive_metadata(data):
    """A private descriptive-only candidate, never an artificial release version."""
    metadata = json.loads(data)
    require(isinstance(metadata.get('description'), str), 'Plugin description must be a string')
    metadata['description'] += ' Private isolated setup rehearsal description.'
    return (json.dumps(metadata, indent=2, ensure_ascii=False) + '\n').encode('utf-8')


def generated_runtime_unit(home, target, installed, intended):
    """Copy generated directives verbatim into isolation scaffolding, never fill gaps."""
    require(set(intended) == CONTRACT_KEYS, 'Expected exactly the seven-value service contract')
    lines = installed.decode('utf-8').splitlines(keepends=True)
    starts = [line for line in lines if line.startswith('ExecStart=')]
    require(starts == ['ExecStart=%h/.local/bin/swiftwall daemon\n'], 'Unexpected generated ExecStart')
    directives = [line for line in lines if line.startswith('Environment=')]
    section = None
    for line in lines:
        if line.strip().startswith('['):
            section = line.strip()
        if re.match(r'\s*(?:Environment\w*|PassEnvironment|UnsetEnvironment|PAMName)\s*=', line):
            require(section == '[Service]' and line.startswith('Environment='),
                    'Noncanonical or non-Service generated environment directive')
        if line.startswith('ExecStart='):
            require(section == '[Service]', 'Generated ExecStart is outside Service')
    values = environment_assignments(' '.join(line.removeprefix('Environment=').strip()
                                             for line in directives))
    require(values == intended, 'Generated environment differs from intended seven-value contract')
    require(len(directives) == 7 and all(line.endswith('\n') for line in directives),
            'Generated environment must contain seven complete directives')

    # Keep the private target, no restart loop, and isolation hardening. The ONLY
    # copied execution directive edit is %h -> private home, never Environment=.
    execution = starts[0].replace('%h', str(home), 1)
    scaffold = unit_text(home, {}, target)
    require(execution in scaffold, 'Isolated ExecStart does not match generated command')
    return scaffold.replace('[Install]\n', ''.join(directives) + '[Install]\n')


class PrivateActivation:
    """Private on-disk registry/bar simulator; never invokes any subprocess."""
    def __init__(self, home):
        self.path = home / 'activation-simulator.json'
        self.baseline = None
        self.enable_attempted = False
        self.disable_attempted = False
        self.fail = False
        self.save({'registry': {}, 'config': {'bar': {'layout': {'right': [{'id': 'clock'}]}},
                                              'theme': 'private'}, 'persisted': {'theme': 'private'}})

    def save(self, value):
        self.path.write_text(json.dumps(value, sort_keys=True))
        self.path.chmod(0o600)

    def snapshot(self):
        return json.loads(self.path.read_text())

    def prepare(self):
        self.baseline = deepcopy(self.snapshot())
        require(not self.baseline['registry'].get(PLUGIN_ID), 'Already active simulator')

    def activate(self):
        self.enable_attempted = True
        value = self.snapshot()
        value['registry'][PLUGIN_ID] = True
        value['config']['bar']['layout']['right'].insert(0, {'id': PLUGIN_ID})
        self.save(value)
        if self.fail:
            self.fail = False
            raise RuntimeError('injected private activation failure after mutation')
        return ActivationOutcome('success', 'ok', 'private simulator, not real shell')

    def deactivate(self):
        self.disable_attempted = True
        value = self.snapshot()
        value['registry'][PLUGIN_ID] = False
        for key in ('config', 'persisted'):
            value[key] = without_plugin(value[key])
        self.save(value)


class GuardedRunner:
    """Fail closed BEFORE subprocess execution, including during recovery."""
    def __init__(self, fixture):
        require(re.fullmatch(r'swiftwall-setup-rehearsal-[a-f0-9]{32}\.service', fixture.unit),
                'Only generated disposable unit names are allowed')
        self.f = fixture
        escaped = ''.join(chr(b) if chr(b).isascii() and chr(b).isalnum() else f'_{b:02x}'
                          for b in fixture.unit.encode('ascii'))
        self.object_path = '/org/freedesktop/systemd1/unit/' + escaped
        self.loaded_object = None
        self.readiness_fault = False
        self.mask_health = False
        self.injected = False

    def validate(self, args, env):
        require(isinstance(args, (list, tuple)) and bool(args), 'Structured argv required')
        args = list(map(str, args))
        f = self.f
        if args[0] == 'busctl':
            load = ['busctl', '--user', 'call', 'org.freedesktop.systemd1',
                    '/org/freedesktop/systemd1', 'org.freedesktop.systemd1.Manager',
                    'LoadUnit', 's', f.unit]
            files = ['busctl', '--user', 'get-property', 'org.freedesktop.systemd1',
                     self.object_path, 'org.freedesktop.systemd1.Service', 'EnvironmentFiles']
            require(args == load or (args == files and self.loaded_object == self.object_path),
                    'Only typed selected-unit LoadUnit/EnvironmentFiles calls allowed')
            return args
        if args[0] == 'systemctl':
            require(args[1:2] == ['--user'] and len(args) >= 3, 'Only user systemctl allowed')
            action = args[2]
            if action == 'daemon-reload':
                require(len(args) == 3, 'Unexpected reload arguments')
            else:
                require(action in {'show', 'start', 'stop', 'enable', 'disable', 'reset-failed'},
                        'Forbidden systemctl action')
                require(len(args) >= 4 and args[3] == f.unit, 'Only this disposable unit allowed')
                if action == 'show':
                    rest = args[4:]
                    allowed = {'LoadState', 'ActiveState', 'SubState', 'MainPID', 'NRestarts',
                               'Result', 'UnitFileState', 'Environment', 'EnvironmentFiles',
                               'PassEnvironment', 'UnsetEnvironment', 'PAMName'}
                    require(len(rest) % 2 == 0 and all(rest[i] == '-p' and rest[i + 1] in allowed
                            for i in range(0, len(rest), 2)), 'Forbidden show query')
                else:
                    require(len(args) == 4, 'Unexpected lifecycle arguments')
            return args
        require(args[0] in {str(f.manager.binary), str(f.bundle / 'bin/swiftwall')},
                'Only fixture engine binaries may execute')
        require(env is not None and set(env) == set(f.env)
                and all(env.get(k) == v for k, v in f.env.items()
                        if k != 'SWIFTWALL_CONFIG'), 'Non-private engine environment')
        assert env is not None
        cfg = Path(env.get('SWIFTWALL_CONFIG', ''))
        require(cfg.is_absolute() and cfg.is_relative_to(f.home) and '..' not in cfg.parts,
                'Config must be private')
        require(args[1:] in [['--version'], ['check-config'], ['status'], ['next'], ['pause']]
                or (len(args) == 3 and args[1] == 'request'
                    and json.loads(args[2]).get('op') in {'status', 'hello'}), 'Forbidden engine command')
        return args

    def __call__(self, args, **kwargs):
        require(not kwargs.get('shell'), 'Shell execution forbidden')
        args = self.validate(args, kwargs.get('env'))
        manager = args[0] == 'systemctl'
        bus = args[0] == 'busctl'
        if manager or bus:
            kwargs['env'] = self.f.session_env
        if manager:
            if args[2] == 'daemon-reload' and self.f.case == 'generated-unit':
                self.f.sync_generated_unit()
            if args[2] in {'enable', 'disable'}:
                args = [*args, '--runtime']

            if args[2] == 'stop':
                self.mask_health = False
        kwargs['timeout'] = min(kwargs.get('timeout', 10), 30)
        result = subprocess.run(args, check=False, **kwargs)
        if (manager and args[2] == 'daemon-reload' and result.returncode == 0
                and self.f.case == 'generated-unit' and self.f.generated_synced
                                and self.f.unit_path.exists()):
            self.f.verify_generated_environment()
        if bus and args[2] == 'call':
            self.loaded_object = (self.object_path if result.returncode == 0
                                  and result.stdout.strip() == f'o "{self.object_path}"' else None)
        if manager and args[2] == 'start' and result.returncode == 0 and self.readiness_fault:
            self.readiness_fault = False
            self.mask_health = True
        health = (not manager and len(args) == 3 and args[1] == 'request'
                  and json.loads(args[2]).get('id') == 'deployment-health')
        masked = self.mask_health and health
        readiness_delay = 0
        if masked:
            self.injected = True
            # Expire the readiness probe's own bounded budget, rather than race
            # a final real systemctl query against a near-zero remaining timeout.
            readiness_delay = min(2, kwargs['timeout'])
            time.sleep(readiness_delay)
            result = subprocess.CompletedProcess(args, 1, '', 'Daemon unavailable (injected readiness)')
        query = manager and args[2] == 'show'
        requested = set(args[5::2]) if query else set()
        present = {line.split('=', 1)[0] for line in (result.stdout or '').splitlines() if '=' in line}
        self.f.record('command', argv=args, returncode=result.returncode, masked_readiness=masked,
                              injected_readiness_delay=readiness_delay,
                      **({'output_omitted': True,
                          'selected_object_verified': self.loaded_object == self.object_path,
                          'typed_empty_verified': (args[2] == 'get-property' and result.returncode == 0
                                                   and result.stdout.strip() == 'a(sb) 0')} if bus else
                         {'returned_properties': sorted(requested & present),
                          'missing_properties': sorted(requested - present)} if query else
                         {'stdout': result.stdout, 'stderr': result.stderr}))
        return result


class FaultManager(SetupManager):
    """Only installer postcommit fault injection; all lifecycle checks inherited."""
    installer_fault = False
    installer_injected = False

    def _install_engine(self, current):
        super()._install_engine(current)
        if self.installer_fault:
            self.installer_fault = False
            self.installer_injected = True
            raise RuntimeError('injected installer failure after real engine commit')


class Rehearsal:
    def __init__(self, case, evidence, sources):
        require(case in CASES, 'Unknown case')
        self.sources = sources
        self.case = case
        self.evidence = evidence
        self.root = Path(tempfile.mkdtemp(prefix='sws-', dir='/tmp'))
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        self.bundle = self.home / 'bundle'
        self.unit = 'swiftwall-setup-rehearsal-' + uuid.uuid4().hex + '.service'
        self.target = self.unit.removesuffix('.service') + '.target'
        self.runtime = Path('/run/user') / str(os.getuid())
        self.unit_path = self.runtime / 'systemd/user' / self.unit
        self.unit_owned = False
        self.generated_synced = False
        self.session_env = {'PATH': '/usr/bin:/bin', 'HOME': str(Path.home()),
                            'XDG_RUNTIME_DIR': str(self.runtime),
                            'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + str(self.runtime / 'bus')}
        self.env = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                    'XDG_RUNTIME_DIR': str(self.home / 'run'),
                    'XDG_CONFIG_HOME': str(self.home / '.config'),
                    'XDG_STATE_HOME': str(self.home / '.local/state'),
                    'XDG_CACHE_HOME': str(self.home / '.cache'),
                    'XDG_DATA_HOME': str(self.home / '.local/share'),
                    'SWIFTWALL_CONFIG': str(self.home / '.config/swiftwall/config.toml'),
                    'SWIFTWALL_STATE_DIR': str(self.home / '.local/state/swiftwall'),
                    'SWIFTWALL_RUNTIME_DIR': str(self.home / 'run/swiftwall'),
                    'SWIFTWALL_BACKGROUND_LINK': str(self.home / 'background'),
                    'SWIFTWALL_OMARCHY_COMMAND': str(self.home / 'fake-setter')}
        self.runner = GuardedRunner(self)
        self.manager = None

    def record(self, event, **fields):
        with (self.evidence / 'commands.jsonl').open('a') as stream:
            stream.write(json.dumps({'case': self.case, 'event': event, **fields}) + '\n')

    def ctl(self, action):
        args = ['systemctl', '--user', action]
        if action != 'daemon-reload':
            args.append(self.unit)
        result = self.runner(args, capture_output=True, text=True, timeout=30)
        require(result.returncode == 0, f'Disposable {action} failed: {result.stderr}')
        return result

    def setup(self):
        private_directory(self.root)
        private_directory(self.home)
        private_directory(self.runtime)
        require((self.runtime / 'bus').is_socket(), 'No real user session bus; cannot rehearse')
        for name in ('run/swiftwall', '.config/swiftwall', '.local/state/swiftwall', 'images'):
            (self.home / name).mkdir(mode=0o700, parents=True, exist_ok=True)
        for number in range(8):
            (self.home / f'images/{number}.png').write_bytes(png())
        self.images = {str(p): digest(p) for p in sorted((self.home / 'images').iterdir())}
        background = Path(self.env['SWIFTWALL_BACKGROUND_LINK'])
        background.symlink_to(next(iter(self.images)))
        setter = Path(self.env['SWIFTWALL_OMARCHY_COMMAND'])
        setter.write_text('#!/usr/bin/python3\nimport hashlib,json,os,sys\nfrom pathlib import Path\n'
            + f'allowed={self.images!r}\n'
            + "assert len(sys.argv)==5 and sys.argv[1:4]==['theme','bg','set']\n"
            + 'p=Path(sys.argv[4])\nassert str(p) in allowed and not p.is_symlink()\n'
            + 'assert hashlib.sha256(p.read_bytes()).hexdigest()==allowed[str(p)]\n'
            + f'link=Path({str(background)!r})\n'
            + f'with open({str(self.home / "setter.jsonl")!r},"a") as s: s.write(json.dumps(sys.argv)+"\\n")\n'
            + 'tmp=link.with_name("background-new")\ntmp.symlink_to(p)\nos.replace(tmp,link)\n')
        setter.chmod(0o700)
        self.config = ('interval_seconds = 3600\nselection = "shuffle"\nfolders = '
                       + json.dumps([str(self.home / 'images')]) + '\n').encode()
        for name in (*plugin.FILES, 'packaging/swiftwall.service'):
            dest = self.bundle / name
            dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, dest)
            dest.chmod(0o755 if name.endswith('.py') else 0o644)
        (self.bundle / 'bin').mkdir(mode=0o700)
        shutil.copyfile(self.sources.v02, self.bundle / 'bin/swiftwall')
        (self.bundle / 'bin/swiftwall').chmod(0o755)
        self.activation = PrivateActivation(self.home)
        self.manager = FaultManager(home=self.home, bundle=self.bundle, env=self.env, unit=self.unit,
                                    runner=self.runner, activation_controller=self.activation,
                                    evidence=self.home / 'manager-evidence')
        self.manager.timeout = 1
        version = self.manager._version(self.bundle / 'bin/swiftwall')
        require(version != '0.1.0', 'Current binary must support protocol 1')
        self.version = version
        (self.bundle / 'release.json').write_text(json.dumps({'version': version, 'rehearsal_only': True,
            'files': {'bin/swiftwall': {'sha256': self.sources.v02_sha256}}}))
        self.unit_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        plugin.check_path(self.unit_path.parent, directory=True)
        if self.case != 'generated-unit':
            with self.unit_path.open('x') as stream:
                self.unit_owned = True
                stream.write(unit_text(self.home, self.env, self.target))
            self.unit_path.chmod(0o600)
        else:
            require(not os.path.lexists(self.unit_path), 'Disposable generated-unit name already exists')
        self.record('isolation', home=str(self.home), unit=self.unit, env=self.env,
                    runtime_unit=self.unit_path.read_text() if self.unit_owned else None,
                    bundle_verification='not exercised')
        self.ctl('daemon-reload')
        if self.case != 'generated-unit':
            self.manager.service.verify_environment(self.manager.engine_environment)
        initial = self.manager.status()
        state = self.manager.service.state()
        if self.case == 'generated-unit':
            require(state['LoadState'] == 'not-found' and state['MainPID'] == '0',
                    'Generated-unit first install must start without a loaded service')
        else:
            require(state['UnitFileState'] == 'disabled', 'Fresh private service must initially be disabled')
        require(not initial['engine_owned'] and not initial['plugin_owned'], 'Fresh home is not empty')
        self.record('fresh-manager-status', report=initial, service=state)

    def sync_generated_unit(self):
        assert self.case == 'generated-unit' and self.manager is not None
        installed_path = self.home / engine.FILES['unit']
        if not installed_path.exists():
            return  # Initial no-unit load, or real uninstall/recovery removed it.
        engine.verify_owned(self.home, engine.load_manifest(self.home))
        require(self.manager.service.state()['MainPID'] == '0', 'Refusing to replace a running unit')
        installed = installed_path.read_bytes()
        intended = {key: self.env[key] for key in CONTRACT_KEYS}
        text = generated_runtime_unit(self.home, self.target, installed, intended)
        plugin.check_path(self.unit_path)
        with self.unit_path.open('w' if self.unit_owned else 'x') as stream:
            self.unit_owned = True
            stream.write(text)
        self.unit_path.chmod(0o600)
        self.generated_synced = True
        self.record('generated-unit-copied', source_sha256=hashlib.sha256(installed).hexdigest(),
                    installed_unit=installed.decode('utf-8'), runtime_unit=text,
                    intended_environment=intended)

    def verify_generated_environment(self):
        assert self.manager is not None
        result = self.manager.service.command(['systemctl', '--user', 'show', self.unit,
                                               '-p', 'Environment'])
        require(result.returncode == 0 and result.stdout.startswith('Environment='),
                'Cannot inspect generated unit loaded environment')
        loaded = environment_assignments(result.stdout.removeprefix('Environment=').strip())
        intended = {key: self.env[key] for key in CONTRACT_KEYS}
        require(loaded == intended, 'Loaded Environment differs from actual generated seven-value contract')
        self.manager.service.verify_environment(intended)
        self.record('generated-environment-verified', exact_contract=True, keys=sorted(loaded))

    def settled(self):
        assert self.manager is not None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            reply = json.loads(self.manager.command([self.manager.binary, 'status']))
            status = reply['status']
            require(reply.get('ok') and not status.get('error'), f'Engine unhealthy: {reply}')
            if not any(status.get(k) for k in ('applying', 'scanning', 'persistence_pending')):
                return reply
            time.sleep(.05)
        raise RuntimeError('Private daemon did not settle in five seconds')

    def preserved(self):
        assert self.manager is not None
        self.settled()
        state = json.loads(self.manager.state.read_text())
        return {'paused': state['paused'], 'carousel': state['carousel'],
                'wallpaper': os.readlink(self.manager.background),
                'setter': (self.home / 'setter.jsonl').read_bytes() if (self.home / 'setter.jsonl').exists() else b'',
                'config': self.manager.config.read_bytes()}

    def seed_v01(self):
        assert self.manager is not None
        # Bootstrap an actual legacy file-only install, not a fabricated manifest.
        engine.install(self.home, self.sources.v01)
        self.manager.config.write_bytes(self.config)
        self.manager.config.chmod(0o600)
        self.manager.service.start()
        # Type=simple can return before exec; legacy bootstrap must await real IPC
        # before asking SetupManager to inspect the running process environment.
        self.manager.service.wait_ready(self.manager.binary, version='0.1.0', timeout=5)
        self.manager.healthy('0.1.0')
        for op in ('next', 'next', 'pause'):
            require(json.loads(self.manager.command([self.manager.binary, op])).get('ok'), f'{op} failed')
            self.settled()
        snapshot = self.preserved()
        require(snapshot['paused'] and len(snapshot['carousel']['history']) >= 2
                and snapshot['carousel']['bag'], 'Real legacy history was not populated')
        self.record('legacy-seeded', preserved={k: v for k, v in snapshot.items() if not isinstance(v, bytes)})
        return snapshot

    def verify_images(self):
        assert self.manager is not None
        require(all(Path(p).is_file() and digest(Path(p)) == h for p, h in self.images.items()),
                'Image deleted or changed')
        require(self.manager.background.is_symlink(), 'Background symlink deleted')

    def uninstall(self, remove_data):
        assert self.manager is not None
        self.manager.uninstall(remove_data=remove_data)
        self.verify_images()
        require(not self.manager.binary.exists() and plugin.load_manifest(self.home) is None,
                'Uninstall left managed payload')
        require(self.manager.config.exists() != remove_data and self.manager.state.exists() != remove_data,
                'Uninstall data retention mismatch')
        require(self.manager.service.state()['MainPID'] == '0', 'Uninstall left daemon running')
        self.record('uninstall-verified', remove_data=remove_data, images_preserved=True)

    def metadata_update(self):
        m = self.manager
        assert m is not None
        installed = m.install(self.config, enable_widget=False)
        require(installed['health'] is not None and digest(m.binary) == self.sources.v02_sha256,
                'Metadata case did not install the actual approved v0.2 binary')
        require(m.service.state()['UnitFileState'] == 'enabled-runtime', 'Expected runtime-only first enable')
        self.ctl('disable')  # Use the manager-supported actual disabled state for update/rollback.
        for operation in ('next', 'next', 'pause'):
            require(json.loads(m.command([m.binary, operation])).get('ok'), f'{operation} failed')
            self.settled()
        before = self.preserved()
        require(before['paused'] and len(before['carousel']['history']) >= 2
                and before['carousel']['bag'], 'Actual v0.2 history was not populated')

        # Represent an already active PRIVATE registry/bar, without any controller
        # enable call. These simulator bytes must survive update and rollback.
        simulated = self.activation.snapshot()
        simulated['registry'].update({PLUGIN_ID: True, 'private.unrelated': False})
        simulated['config']['bar']['layout']['right'].insert(0, {'id': PLUGIN_ID})
        simulated['persisted'] = deepcopy(simulated['config'])
        self.activation.save(simulated)
        simulation_bytes = self.activation.path.read_bytes()
        initial_service = m.service.state()
        stable_keys = ('ActiveState', 'UnitFileState', 'NRestarts')
        service_before = {key: initial_service[key] for key in stable_keys}
        require(service_before == {'ActiveState': 'active', 'UnitFileState': 'disabled', 'NRestarts': '0'},
                'Unexpected metadata case service baseline')
        old_widget = plugin.load_manifest(self.home)
        require(old_widget is not None, 'Missing actual installed plugin')
        assert old_widget is not None
        plugin.verify_owned(self.home, old_widget)
        old_engine = engine.load_manifest(self.home)
        original = plugin.source_generation(self.bundle)
        require(old_widget['current'] == original, 'Installed baseline differs from private bundle')
        code_names = tuple(name for name in plugin.FILES if name != 'manifest.json')
        require(len(code_names) == 7, 'Expected exactly seven non-manifest plugin files')
        source_manifest = self.bundle / 'manifest.json'
        original_metadata = json.loads(source_manifest.read_bytes())
        source_manifest.write_bytes(descriptive_metadata(source_manifest.read_bytes()))
        candidate = plugin.source_generation(self.bundle)
        require(all(candidate[name] == original[name] for name in code_names), 'Code payload changed')
        candidate_metadata = json.loads(source_manifest.read_bytes())
        unchanged_metadata = deepcopy(candidate_metadata)
        unchanged_metadata['description'] = original_metadata['description']
        require(unchanged_metadata == original_metadata, 'A non-description manifest field changed')
        require(candidate_metadata['description'] != original_metadata['description'], 'Description did not change')
        require(candidate_metadata['version'] == original_metadata['version'], 'Plugin version was fabricated')
        require(digest(self.bundle / 'bin/swiftwall') == self.sources.v02_sha256, 'Bundle binary changed')
        plan = m.plan('update')
        require(plan['plugin'] == 'metadata-only update', 'Manager did not accept descriptive-only update')
        binary_inode = m.binary.stat().st_ino
        manifest_path = self.home / plugin.DESTINATION / 'manifest.json'
        manifest_inode = manifest_path.stat().st_ino

        def verify_preservation(stage):
            m.healthy(self.version)
            require(self.preserved() == before, f'{stage} changed pause/history/config/wallpaper/setter calls')
            require(self.activation.path.read_bytes() == simulation_bytes,
                    f'{stage} changed simulated registry/bar bytes')
            require(not self.activation.enable_attempted and not self.activation.disable_attempted,
                    f'{stage} unexpectedly invoked simulated activation lifecycle')
            state = m.service.state()
            require({key: state[key] for key in stable_keys} == service_before,
                    f'{stage} changed active/enable/restart state')
            require(digest(m.binary) == self.sources.v02_sha256 and m._version() == self.version,
                    f'{stage} changed actual engine bytes/version')
            require(engine.load_manifest(self.home) == old_engine, f'{stage} changed engine ownership')
            require(all(plugin.read_record(self.home / plugin.DESTINATION / name) == original[name]
                        for name in code_names), f'{stage} changed installed code bytes/modes')
            self.record('metadata-preservation-verified', stage=stage, engine_version=self.version,
                        engine_sha256=digest(m.binary), plugin_version=original_metadata['version'],
                        service=service_before, paused=before['paused'],
                        history_length=len(before['carousel']['history']),
                        simulator_sha256=hashlib.sha256(simulation_bytes).hexdigest(),
                        code_hashes={name: original[name]['sha256'] for name in code_names})

        result = m.update()
        require(result['plugin'] == 'metadata-only update', 'Wrong actual plugin update action')
        current = plugin.load_manifest(self.home)
        plugin.verify_owned(self.home, current)
        require(current is not None and current['current'] == candidate and current['previous'] == original,
                'Actual plugin transaction did not publish metadata/previous generation')
        require(manifest_path.read_bytes() == source_manifest.read_bytes()
                and manifest_path.stat().st_ino != manifest_inode,
                'Plugin manifest was not replaced by the actual transaction')
        require(m.binary.stat().st_ino == binary_inode, 'Metadata update replaced the engine binary')
        verify_preservation('metadata-update')
        require(m.rollback().get('restored'), 'Metadata rollback() did not restore its same-manager checkpoint')
        require(plugin.load_manifest(self.home) == old_widget, 'Metadata rollback lost original plugin generation')
        plugin.verify_owned(self.home, old_widget)
        verify_preservation('metadata-rollback')
        self.record('metadata-transaction-verified', changed_fields=['description'],
                    before_description=original_metadata['description'],
                    after_description=candidate_metadata['description'],
                    version_unchanged=True, rollback_restored=True, live_shell_enable=False)
        self.uninstall(remove_data=False)

    def run(self):
        self.setup()
        m = self.manager
        assert m is not None
        if self.case == 'metadata-only':
            self.metadata_update()
        elif self.case in {'fresh', 'activation', 'generated-unit'}:
            before = os.readlink(m.background)
            self.activation.fail = self.case == 'activation'
            if self.case == 'activation':
                self.expect_failure(lambda: m.install(self.config, enable_widget=True), 'activation')
                require(not m.binary.exists() and plugin.load_manifest(self.home) is None,
                        'Activation failure did not remove newly installed files')
                require(not self.activation.snapshot()['registry'].get(PLUGIN_ID), 'Simulator still active')
                require(m.service.state()['MainPID'] == '0', 'Failure left daemon running')
            else:
                m.install(self.config, enable_widget=True)
                m.healthy(self.version)
                if self.case == 'generated-unit':
                    require(self.generated_synced, 'Generated installed unit was never loaded')
                    self.verify_generated_environment()
                require(m.service.state()['UnitFileState'] == 'enabled-runtime', 'Not runtime-only enabled')
                # Expose, do not normalize away, the current manager API mismatch.
                try:
                    m.update()
                except SetupError as exc:
                    require('Unsupported autostart state' in str(exc), f'Unexpected API rejection: {exc}')
                    self.record('api-limitation', detail=str(exc), state='enabled-runtime')
                else:
                    raise RuntimeError('API contract changed: review enabled-runtime handling')
                self.ctl('disable')
                self.uninstall(remove_data=False)
            require(os.readlink(m.background) == before and not (self.home / 'setter.jsonl').exists(),
                    'Startup or recovery invoked setter/changed wallpaper')
        else:
            before = self.seed_v01()
            if self.case in {'readiness', 'installer'}:
                self.runner.readiness_fault = self.case == 'readiness'
                m.installer_fault = self.case == 'installer'
                self.expect_failure(m.update, self.case)
                require(self.runner.injected if self.case == 'readiness' else m.installer_injected,
                        'Intended failure boundary not reached')
                require(digest(m.binary) == self.sources.v01_sha256, 'Recovery did not restore v01')
                m.healthy('0.1.0')
            else:
                m.update()
                m.healthy(self.version)
                require(self.preserved() == before, 'Update lost pause/history/config/wallpaper')
                if self.case == 'identical-update':
                    owned = {name: engine.read_record(self.home, self.home / name)
                             for name in (engine.MANIFEST, plugin.MANIFEST)}
                    widget = {name: plugin.read_record(self.home / plugin.DESTINATION / name)
                              for name in plugin.FILES}
                    inodes = {name: (self.home / plugin.DESTINATION / name).stat().st_ino for name in plugin.FILES}
                    m.update()
                    require(owned == {name: engine.read_record(self.home, self.home / name) for name in owned},
                            'Identical update changed ownership metadata')
                    require(widget == {name: plugin.read_record(self.home / plugin.DESTINATION / name) for name in widget}
                            and inodes == {name: (self.home / plugin.DESTINATION / name).stat().st_ino for name in inodes},
                            'Identical update touched plugin files')
                    m.healthy(self.version)
                else:
                    require(m.rollback().get('restored'), 'rollback() did not restore checkpoint')
                    require(digest(m.binary) == self.sources.v01_sha256, 'rollback() did not restore v01')
                    m.healthy('0.1.0')
            require(self.preserved() == before, 'Lifecycle changed pause/history/config/wallpaper/setter calls')
            require(m.service.state()['UnitFileState'] == 'disabled', 'Update changed disabled autostart')
            self.uninstall(remove_data=self.case == 'identical-update')
        self.verify_images()
        self.record('case-passed')

    def expect_failure(self, operation, boundary):
        try:
            operation()
        except SetupError as exc:
            require('original owned files/service recovered' in str(exc), f'Recovery incomplete: {exc}')
            causes = []
            cause = exc
            while cause is not None:
                causes.append(repr(cause))
                cause = cause.__cause__
            require(any(('readiness-timeout' if boundary == 'readiness' else 'injected ' +
                         ('private activation' if boundary == 'activation' else 'installer')) in c for c in causes),
                    f'Wrong failure boundary: {causes}')
            self.record('expected-failure', boundary=boundary, causes=causes)
        else:
            raise RuntimeError(f'{boundary} injection unexpectedly succeeded')

    def cleanup(self):
        # Validation candidates, like config/state, stay inside the private home.
        # Export only regular manager evidence files before removing the fixture.
        source = self.home / 'manager-evidence'
        export_error = None
        try:
            if source.exists():
                plugin.check_path(source, directory=True)
                destination = self.evidence / self.case
                destination.mkdir(mode=0o700)
                for path in source.iterdir():
                    plugin.check_path(path)
                    shutil.copyfile(path, destination / path.name)
                    (destination / path.name).chmod(0o600)
        except Exception as exc:
            export_error = exc  # Evidence failure must never prevent daemon shutdown.
        # Never delete fixture binaries before proving that the selected unit stopped.
        if self.unit_owned:
            failures = []
            for action in ('stop', 'disable'):
                try:
                    self.ctl(action)
                except Exception as exc:
                    failures.append(str(exc))
            if self.manager is not None:
                require(self.manager.service.state().get('MainPID') == '0',
                        f'Daemon may still run; retained {self.root} and {self.unit_path}')
            self.unit_path.unlink()
            wants = self.unit_path.parent / (self.target + '.wants')
            link = wants / self.unit
            if link.is_symlink():
                require(os.readlink(link) == '../' + self.unit or Path(os.readlink(link)) == self.unit_path,
                        'Unexpected enablement link; refusing deletion')
                link.unlink()
            if wants.exists():
                wants.rmdir()  # Empty, unique directory only.
            self.ctl('daemon-reload')
            self.record('cleanup', stopped=True, runtime_unit_removed=not self.unit_path.exists(),
                        disable_errors=failures)
            require(not failures, f'Cleanup commands failed: {failures}')
        shutil.rmtree(self.root)
        self.record('fixture-removed', root=str(self.root))
        if export_error is not None:
            raise RuntimeError('Manager evidence export failed; daemon/fixture cleaned') from export_error


def preflight(sources):
    require(os.getuid() != 0, 'Do not run as root')
    hashes = sources.verify()
    runtime = Path('/run/user') / str(os.getuid())
    private_directory(runtime)
    require((runtime / 'bus').is_socket(), 'Real user manager unavailable; no substitute permitted')
    plugin.source_generation(ROOT)
    return hashes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Explicitly authorize disposable real user units')
    parser.add_argument('--case', action='append', choices=CASES)
    parser.add_argument('--v01', type=Path, required=True, help='Absolute path to approved v0.1 executable')
    parser.add_argument('--v02', type=Path, required=True, help='Absolute path to approved current executable')
    parser.add_argument('--v01-sha256', required=True, help='Independently obtained expected v0.1 SHA256')
    parser.add_argument('--v02-sha256', required=True, help='Independently obtained expected current SHA256')
    args = parser.parse_args(argv)
    if not args.run:
        parser.error('--run is required; importing/tests do not start services')
    try:
        sources = Sources(args.v01, args.v02, args.v01_sha256, args.v02_sha256)
    except RuntimeError as exc:
        parser.error(str(exc))
    hashes = preflight(sources)
    base = ROOT / 'artifacts/setup-rehearsal'
    plugin.check_path(base, directory=True)
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    evidence = base / uuid.uuid4().hex
    evidence.mkdir(mode=0o700)
    old_umask = os.umask(0o077)
    previous = {}
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f'signal {signum}')
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, interrupted)
    cases = args.case or CASES
    summary = {'command': [sys.executable, '-B', *sys.argv], 'source_hashes': hashes,
               'activation': 'private registry/bar simulator, NOT real shell',
               'bundle_verifier': 'not exercised; artificial release.json',
               'completed': False, 'cases': dict.fromkeys(cases, 'not-run')}
    try:
        for case in cases:
            fixture = Rehearsal(case, evidence, sources)
            try:
                fixture.run()
                summary['cases'][case] = 'passed'
            except BaseException:
                summary['cases'][case] = traceback.format_exc()
                raise
            finally:
                try:
                    fixture.cleanup()
                except BaseException:
                    summary['cases'][case] = 'CLEANUP FAILED: ' + traceback.format_exc()
                    raise
        require(hashes == sources.verify(), 'Binary source changed during rehearsal')
        summary['completed'] = True
        print('Rehearsal passed (runtime-enable API limitation recorded).')
        return 0
    finally:
        try:
            summary['source_hashes_after'] = sources.verify()
            summary['source_hashes_unchanged'] = summary['source_hashes_after'] == hashes
        except (OSError, RuntimeError, plugin.InstallError) as exc:
            summary['source_hash_verification_error'] = str(exc)
        (evidence / 'summary.json').write_text(json.dumps(summary, indent=2))
        print(f'Evidence: {evidence}')
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        os.umask(old_umask)


if __name__ == '__main__':
    raise SystemExit(main())
