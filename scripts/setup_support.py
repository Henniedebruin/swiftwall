"""Small, supervised setup lifecycle (stdlib only; importing performs no I/O).

The caller verifies the release bundle and obtains confirmation. plan() is strictly
file-read-only, including no service/shell probes or evidence writes. Mutation
methods serialize cooperating installers using plugin_support's home lock.

Limitations: plugin code or structural manifest changes are refused even with an
inactive shell (no safe QML cache invalidation); descriptive/version-only manifest
updates are allowed with identical non-manifest bytes and modes. No shell restart
is attempted. rollback()
uses this manager's last successful install/update checkpoint, not arbitrary old
installer generations. Backups survive the process for manual recovery. Recovery
covers ordinary exceptions and Ctrl-C during install/update/uninstall, not power loss or
hostile same-UID concurrent writes. Interrupted recovery retains independent backups
for manual recovery; it is never retried automatically.
plan/status versions come only from owned plugin metadata and a supplied verified
bundle whose engine hash matches the installation; unavailable versions are None.
status queries the service manager, not the engine binary or daemon IPC.
New owned units explicitly pin HOME, runtime and engine destinations. Legacy
inheritance is accepted only with verified unit directives and manager environment;
active services additionally require the actual process environment to match.
"""

import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
from copy import deepcopy
from pathlib import Path

import install_support as engine
import plugin_support as plugin
from deployment import Deployment
from evidence_support import private_evidence_directory, private_evidence_log
from plugin_activation import PLUGIN_ID, OmarchyActivation, without_plugin
from service_control import ServiceError, UserService


class SetupError(RuntimeError):
    pass


ACTIVATION = Path('.local/state/swiftwall/setup-activation.json')
OPERATIONS = ('install', 'update', 'status', 'uninstall', 'rollback')


def decoded(item):
    return base64.b64decode(item['data']) if item is not None else None


def plugin_update_action(before, current):
    """Permit only URL-cache-safe metadata changes, never new QML/helper code."""
    plugin.validate_generation(current)
    if before is None:
        return 'install files'
    plugin.validate_generation(before)
    if before == current:
        return 'identical; untouched'
    same_payload = all(before[name] == current[name] for name in plugin.FILES if name != 'manifest.json')

    def structural_manifest(generation):
        manifest = json.loads(base64.b64decode(generation['manifest.json']['bytes']))
        for key in ('version', 'name', 'description', 'author', 'license'):
            manifest.pop(key, None)
        if 'barWidget' in manifest:
            if not isinstance(manifest['barWidget'], dict):
                raise SetupError('Invalid barWidget metadata; refusing plugin update')
            for key in ('displayName', 'description', 'category', 'aliases'):
                manifest['barWidget'].pop(key, None)
        # JSON comparison preserves distinctions such as false vs 0 in defaults.
        return json.dumps(manifest, sort_keys=True, separators=(',', ':'))

    if not same_payload or structural_manifest(before) != structural_manifest(current):
        raise SetupError('Differing plugin code or structural manifest refused before mutation: active-shell QML URL caching cannot be safely invalidated; no automatic shell restart is supported')
    return 'metadata-only update'


def systemd_quote(value):
    """One literal systemd.syntax word, not shell syntax or ExecStart expansion."""
    if any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
        raise SetupError('Control characters are not allowed in service environment values')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


class SetupManager:
    def __init__(self, home, bundle=None, env=None, unit='swiftwall.service',
                 runner=subprocess.run, activation_controller=None, evidence=None):
        self.home = Path(home)
        if not self.home.is_absolute() or '..' in self.home.parts:
            raise SetupError('home must be an explicit absolute path')
        self.bundle = Path(bundle) if bundle is not None else None
        if self.bundle is not None and not self.bundle.is_absolute():
            raise SetupError('bundle must be absolute')
        self.env = dict(env or {})
        if self.env.get('HOME') != str(self.home):
            raise SetupError('Explicit HOME must match installation home')
        self.private = self.home != Path.home()
        if not re.fullmatch(r'[A-Za-z0-9_.-]+\.service', unit):
            raise SetupError('Invalid user unit name')
        if (self.private and unit == 'swiftwall.service') or (not self.private and unit != 'swiftwall.service'):
            raise SetupError('Isolated homes require a private disposable user unit; real home requires swiftwall.service')
        self.unit = unit
        self.runner = runner
        self.evidence = Path(evidence) if evidence is not None else self.home / '.local/state/swiftwall/setup-evidence'
        if not self.evidence.is_absolute() or '..' in self.evidence.parts:
            raise SetupError('evidence must be absolute')
        self.binary = self.home / engine.FILES['binary']
        self.defaults = self._locations(self.env, overrides=False)
        allowed = set(self.defaults)
        if any(k.startswith('SWIFTWALL_') and k not in allowed for k in self.env):
            raise SetupError('Unsupported SWIFTWALL_ override')
        for key, value in self.defaults.items():
            supplied = self.env.get(key, value)
            if not Path(supplied).is_absolute() or '..' in Path(supplied).parts:
                raise SetupError('Engine destinations and setter must be explicit absolute paths')
            if not self.private and supplied != value:
                raise SetupError('Production SWIFTWALL_ override differs from default destination')
            if self.private and not Path(supplied).is_relative_to(self.home):
                raise SetupError('Private destinations and fake setter must be inside private home')
        locations = self._locations(self.env)
        self.config = Path(locations['SWIFTWALL_CONFIG'])
        self.state = Path(locations['SWIFTWALL_STATE_DIR']) / 'state.json'
        self.background = Path(locations['SWIFTWALL_BACKGROUND_LINK'])
        self.control_socket = Path(locations['SWIFTWALL_RUNTIME_DIR']) / 'control.sock'

        reserved = [self.home / p for p in (*engine.FILES.values(), engine.MANIFEST, ACTIVATION, plugin.MANIFEST)]
        reserved.append(self.home / plugin.DESTINATION)
        destinations = (self.config, self.state, self.background, self.control_socket)
        if len(set(destinations)) != len(destinations) or any(
                p == r or p.is_relative_to(r) or r.is_relative_to(p)
                for p in destinations for r in reserved):
            raise SetupError('Engine destinations overlap managed payloads or one another')
        protected = [self.home / p for p in (*engine.FILES.values(), engine.MANIFEST, ACTIVATION, plugin.MANIFEST)]
        protected += [self.config, self.state, self.home / plugin.DESTINATION]
        if any(self.evidence == p or self.evidence.is_relative_to(p) for p in protected):
            raise SetupError('Evidence directory overlaps a managed payload/data path')
        self.engine_environment = {'HOME': str(self.home), 'XDG_RUNTIME_DIR': self.env['XDG_RUNTIME_DIR'], **locations}
        for key, value in self.engine_environment.items():
            systemd_quote(key + '=' + value)  # Reject directive injection even during read-only planning.
        self.env.update(self.engine_environment)
        self.service = UserService(unit, runner=self.execute)
        self.activation_controller = activation_controller
        self.timeout = 10
        self.checkpoint = None
        self.backup = None

    def _locations(self, env, overrides=True):
        runtime = env.get('XDG_RUNTIME_DIR', '')
        if not Path(runtime).is_absolute():
            raise SetupError('Explicit absolute XDG_RUNTIME_DIR is required')
        for key in ('XDG_CONFIG_HOME', 'XDG_STATE_HOME'):
            if key in env and not Path(env[key]).is_absolute():
                raise SetupError('XDG paths must be absolute')
        # Resolve the setter using only the caller's explicit PATH, never editor env.
        setter = shutil.which('omarchy', path=env.get('PATH', '')) or '/usr/bin/omarchy'
        values = {
            'SWIFTWALL_CONFIG': str(Path(env.get('XDG_CONFIG_HOME', self.home / '.config')) / 'swiftwall/config.toml'),
            'SWIFTWALL_STATE_DIR': str(Path(env.get('XDG_STATE_HOME', self.home / '.local/state')) / 'swiftwall'),
            'SWIFTWALL_RUNTIME_DIR': str(Path(runtime) / 'swiftwall'),
            'SWIFTWALL_BACKGROUND_LINK': str(self.home / '.local/state/omarchy/current/background'),
            'SWIFTWALL_OMARCHY_COMMAND': setter,
        }
        if overrides:
            values.update({k: env[k] for k in values if k in env})
        return values

    def record(self, event, **fields):
        with private_evidence_directory(self.evidence) as directory:
            stream = private_evidence_log(directory, create=True)
            assert stream is not None
            with stream:
                stream.write(json.dumps({'event': event, **fields}) + '\n')

    def execute(self, args, **kwargs):
        args = list(map(str, args))
        # No logging of systemctl show output, even when the runner raises.
        manager = Path(args[0]).name == 'systemctl'
        kwargs['env'] = ({k: v for k, v in os.environ.items() if not k.startswith('SWIFTWALL_')}
                         if manager else dict(self.env))
        try:
            return self.runner(args, **kwargs)
        except Exception:
            if manager and ('show' in args or 'show-environment' in args):
                raise SetupError('Service property query failed; output omitted') from None
            raise

    def command(self, args, env=None):
        if env is None:
            result = self.execute(args, capture_output=True, text=True, timeout=15)
        else:
            result = self.runner(list(map(str, args)), env=env, capture_output=True,
                                 text=True, timeout=15)
        if result.returncode:
            raise SetupError('Command failed: ' + str(args[0]))
        return result.stdout

    def _owned(self):
        plugin.check_path(self.home, directory=True)
        if self.private:
            for directory in (self.home, Path(self.env['XDG_RUNTIME_DIR']), self.control_socket.parent):
                plugin.check_path(directory, directory=True)
                if not directory.exists() and directory == self.control_socket.parent:
                    continue
                info = directory.stat()
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                    raise SetupError('Private home/runtime directories must be owned by this user and mode 0700')
            setter = Path(self._locations(self.env)['SWIFTWALL_OMARCHY_COMMAND'])
            plugin.check_path(setter)
            info = setter.stat()
            if info.st_uid != os.getuid() or not info.st_mode & stat.S_IXUSR:
                raise SetupError('Private fake setter must be an owned regular executable')
        installed = engine.load_manifest(self.home)
        engine.verify_owned(self.home, installed)
        try:
            widget = plugin.load_manifest(self.home)
            plugin.verify_owned(self.home, widget)
        except plugin.InstallError as exc:
            raise SetupError(f'{exc}; remove or relocate the marketplace/git installation manually; setup will not adopt it') from exc
        for path in (self.config, self.state, self.home / ACTIVATION):
            engine.check_path(self.home, path)
        receipt = engine.read_record(self.home, self.home / ACTIVATION)
        if receipt is not None:
            if decoded(receipt) != b'{"version":1,"owned":true}\n' or receipt['mode'] != 0o600:
                raise SetupError('Unrecognized setup activation receipt')
            if widget is None:
                raise SetupError('Interrupted installation: activation receipt exists without an owned plugin; manual reconciliation required')
        if widget is None:
            shell_path = self.home / '.config/omarchy/shell.json'
            shell = engine.read_record(self.home, shell_path)
            if shell is not None:
                config = json.loads(base64.b64decode(shell['data']))
                if without_plugin(config) != config:
                    raise SetupError('Pre-existing SwiftWall shell references are unowned; remove them manually before first plugin installation')
        return installed, widget, receipt

    def _payload(self):
        if self.bundle is None:
            raise SetupError('A verified bundle is required')
        plugin.check_path(self.bundle, directory=True)
        # release.json verification belongs to setup_download/main, not this module.
        for name in ('bin/swiftwall', 'packaging/swiftwall.service', 'release.json'):
            plugin.check_path(self.bundle / name)
            if not (self.bundle / name).is_file():
                raise SetupError('Incomplete verified bundle: ' + name)
        template = (self.bundle / 'packaging/swiftwall.service').read_bytes()
        current = {'binary': engine.record((self.bundle / 'bin/swiftwall').read_bytes(), 0o755),
                   'unit': engine.record(self._unit_bytes(template), 0o644)}
        return current, plugin.source_generation(self.bundle)

    def _unit_bytes(self, template):
        # Keep the packaged ExecStart=%h and service identity. This generated unit
        # is a normal owned generation, never a drop-in over an unowned unit.
        settings = ''.join('Environment=' + systemd_quote(key + '=' + value) + '\n'
                           for key, value in sorted(self.engine_environment.items()))
        return template.rstrip(b'\n') + b'\n\n[Service]\n' + settings.encode('utf-8')

    def plan(self, operation):
        if operation not in OPERATIONS:
            raise SetupError('Unknown setup operation')
        installed, widget, receipt = self._owned()
        if operation == 'update' and installed is None:
            raise SetupError('No owned engine installation; use install')
        plugin_action = 'retain' if widget else 'absent'
        if operation in ('install', 'update'):
            _, payload = self._payload()
            plugin_action = plugin_update_action(widget['current'] if widget else None, payload)
        engine_version = None  # Legacy engine ownership manifests contain no version.
        plugin_version = (json.loads(base64.b64decode(widget['current']['manifest.json']['bytes']))['version']
                          if widget else None)
        if installed and self.bundle is not None:
            metadata = plugin.read_record(self.bundle / 'release.json')
            if metadata is not None:
                release = json.loads(base64.b64decode(metadata['bytes']))
                version = release.get('version')
                binary_metadata = release.get('files', {}).get('bin/swiftwall', {})
                if (isinstance(version, str) and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?', version)
                        and binary_metadata.get('sha256') == installed['current']['binary']['sha256']):
                    engine_version = version
        return {'operation': operation, 'engine_owned': installed is not None,
                'engine_version': engine_version, 'plugin_version': plugin_version,
                'plugin_owned': widget is not None, 'plugin': plugin_action,
                'activation_owned': receipt is not None,
                'rollback_available': self.checkpoint is not None,
                'limitations': ['Plugin code or structural manifest changes require manual shell-safe migration',
                                'Rollback checkpoints are scoped to this manager instance; evidence backups persist']}

    def _controller(self):
        if self.activation_controller is None:
            if self.private:
                raise SetupError('Private homes require an injected private activation controller')
            self.activation_controller = OmarchyActivation(home=self.home, evidence=self.evidence, env=self.env)
        return self.activation_controller

    def _wallpaper(self):
        plugin.check_path(self.background.parent, directory=True)
        if self.background.is_symlink():
            return os.readlink(self.background)
        if self.background.exists():
            raise SetupError('Background location is not a symlink')
        return None

    def _verify_effective_environment(self, actual):
        if (any(k.startswith('SWIFTWALL_') and k not in self.engine_environment for k in actual)
                or actual.get('HOME') != str(self.home)
                or actual.get('XDG_RUNTIME_DIR') != self.env['XDG_RUNTIME_DIR']
                or self._locations(actual) != self._locations(self.env)):
            raise SetupError('environment-mismatch: effective service destinations differ from explicit environment')

    def _environment_query(self, args):
        # show-environment can include unrelated credentials, including in runner
        # exceptions. Never expose its output or exception chain in diagnostics.
        failed = False
        result = None
        try:
            result = self.service.command(args)
            failed = result.returncode != 0
        except Exception:  # noqa: BLE001 - redact even runner exceptions containing partial environment output
            failed = True
        if failed or result is None:
            raise SetupError('Service environment query failed; output omitted') from None
        return result.stdout

    def verify_existing_environment(self):
        """Prove a legacy inherited environment without relaxing new-unit checks."""
        try:
            self.service.verify_environment(self.engine_environment)
            return
        except ServiceError as exc:
            if exc.category != 'environment-mismatch':
                raise
        manifest = engine.load_manifest(self.home)
        engine.verify_owned(self.home, manifest)
        if manifest is None:
            raise SetupError('Legacy environment verification requires an owned engine')
        unit = base64.b64decode(manifest['current']['unit']['data']).decode('utf-8')
        if re.search(r'(?m)^\s*Environment\s*=', unit):
            raise ServiceError('environment-mismatch', 'Explicit owned unit is missing or has mismatched loaded values; legacy inheritance is not allowed')
        output = self._environment_query(['systemctl', '--user', 'show', self.unit, '-p', 'Environment'])
        # Match UserService's narrow show-output contract; do not guess at C
        # escapes. Its full property validation below also rejects files/PAM/etc.
        configured = None
        try:
            if not output.startswith('Environment=') or '\\' in output:
                raise ValueError('ambiguous unit environment')
            entries = shlex.split(output.removeprefix('Environment=').strip())
            configured = dict(entry.split('=', 1) for entry in entries)
            if len(configured) != len(entries):
                configured = None
        except ValueError:
            pass
        if configured is None:
            raise SetupError('Legacy unit environment is ambiguous; output omitted')
        selected = {k: v for k, v in self.engine_environment.items() if k in configured}
        self.service.verify_environment(selected)
        inherited = self._environment_query(['systemctl', '--user', 'show-environment'])
        relevant = {'HOME', 'XDG_RUNTIME_DIR', 'PATH', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME'}
        effective = {}
        malformed = False
        for line in inherited.splitlines():
            key = line.partition('=')[0]
            if key not in relevant and not key.startswith('SWIFTWALL_'):
                continue  # Do not parse or retain unrelated secrets.
            try:
                if key in effective or '\\' in line or "=$'" in line:
                    raise ValueError('ambiguous inherited value')
                words = shlex.split(line)
                if len(words) != 1 or '=' not in words[0]:
                    raise ValueError('invalid inherited assignment')
                effective[key] = words[0].split('=', 1)[1]
            except ValueError:
                malformed = True
        if malformed:
            raise SetupError('Legacy manager environment is ambiguous; output omitted')
        # PATH is authoritative for a legacy relative `omarchy` setter. Do not
        # substitute the editor's PATH or assume systemd's compiled default PATH.
        if ('SWIFTWALL_OMARCHY_COMMAND' not in configured and 'SWIFTWALL_OMARCHY_COMMAND' not in effective
                and 'PATH' not in effective and 'PATH' not in configured):
            raise SetupError('Legacy setter PATH is unverified; refusing before service stop')
        effective.update({k: v for k, v in configured.items() if k in relevant or k.startswith('SWIFTWALL_')})
        self._verify_effective_environment(effective)

    def running_environment(self):
        pid = int(self.service.state()['MainPID'])
        if pid <= 0:
            raise SetupError('Service has no running PID')
        raw = (Path('/proc') / str(pid) / 'environ').read_bytes()
        actual = dict(os.fsdecode(e).split('=', 1) for e in raw.split(b'\0') if e)
        self._verify_effective_environment(actual)
        if int(self.service.state()['MainPID']) != pid:
            raise SetupError('Service changed during environment verification')

    socket_owner = Deployment.socket_owner
    running_executable = Deployment.running_executable

    def _version(self, binary=None):
        text = self.command([binary or self.binary, '--version']).strip()
        if not re.fullmatch(r'swiftwall [0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?', text):
            raise SetupError('Invalid engine version')
        return text.split(' ', 1)[1]

    def healthy(self, version, *, allow_inherited=False):
        if allow_inherited:
            self.verify_existing_environment()
        else:
            self.service.verify_environment(self.engine_environment)
        # The manager can expose MainPID before its child has exec'd the engine.
        # Loaded unit environment is checked above; inspect /proc only after IPC
        # readiness, then tie the socket and executable to the same service PID.
        protocol = None if version == '0.1.0' else 1
        reply = self.service.wait_ready(self.binary, version=version, protocol=protocol, timeout=self.timeout)
        self.running_environment()
        self.socket_owner()
        self.running_executable()
        if protocol is not None:
            hello = json.loads(self.command([self.binary, 'request', json.dumps({'v': 1, 'id': 'setup-hello', 'op': 'hello'})]))
            if (not hello.get('ok') or hello.get('protocol_version') != 1
                    or hello.get('daemon_version') != version):
                raise SetupError('Incompatible hello response')
        return reply

    def status(self):
        """Ownership, metadata versions and manager state; never execute a binary."""
        report = self.plan('status')
        if report['engine_owned']:
            self.verify_existing_environment()
            report['service'] = self.service.state()
        return report

    def _snapshot(self):
        paths = [*engine.FILES.values(), engine.MANIFEST, ACTIVATION]
        paths = [self.home / p for p in paths] + [self.config, self.state]
        return {'files': {str(p): engine.read_record(self.home, p) for p in paths},
                'plugin': plugin.load_manifest(self.home), 'service': self.service.state(),
                'wallpaper': self._wallpaper()}

    def _backup(self, snapshot, label):
        with private_evidence_directory(self.evidence) as directory:
            name = label + '-' + secrets.token_hex(16) + '.json'
            data = json.dumps(snapshot, sort_keys=True).encode('utf-8')
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            with os.fdopen(fd, 'w+b') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                stream.seek(0)
                actual = stream.read()
                info = os.fstat(stream.fileno())
                published = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (hashlib.sha256(actual).digest() != hashlib.sha256(data).digest()
                        or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_uid != os.getuid() or info.st_nlink != 1
                        or (info.st_dev, info.st_ino) != (published.st_dev, published.st_ino)):
                    raise SetupError('Backup read-back verification failed; lifecycle mutation refused')
            os.fsync(directory)
        self.backup = self.evidence / name
        return self.backup

    @staticmethod
    def _stable_service_state(state):
        if state.get('ActiveState') not in ('active', 'inactive', 'failed'):
            raise SetupError('Service is transitioning; retry after it settles')
        if state.get('UnitFileState') not in ('enabled', 'disabled', 'not-found', ''):
            raise SetupError('Unsupported autostart state; no safe restoration contract')

    def _start(self, version, wallpaper, *, allow_inherited=False):
        if allow_inherited:
            self.verify_existing_environment()
        else:
            self.service.verify_environment(self.engine_environment)
        self.service.start()
        reply = self.healthy(version, allow_inherited=allow_inherited)
        if self._wallpaper() != wallpaper:
            raise SetupError('Startup changed current wallpaper')
        if wallpaper is not None and reply['status'].get('observed_wallpaper') != wallpaper:
            raise SetupError('Startup observed wallpaper mismatch')
        return reply

    @staticmethod
    def _schema_one(state):
        # src/state.rs uses `version`, not schema_version. Keep the entire shuffle
        # carousel (paths/bag/history/cursor), including explicit paused=False.
        return (isinstance(state, dict) and type(state.get('version')) is int and state['version'] == 1
                and isinstance(state.get('paused'), bool) and isinstance(state.get('carousel'), dict))

    def _verify_preserved(self, snapshot):
        saved = snapshot['files'][str(self.state)]
        if saved is not None:
            before = json.loads(base64.b64decode(saved['data']))
            after = json.loads(self.state.read_bytes())
            if (not self._schema_one(before) or not self._schema_one(after)
                    or any(after.get(k) != before.get(k) for k in ('paused', 'carousel'))):
                raise SetupError('Startup changed pause/history state')

    def _validate_config(self, config_bytes):
        if self.bundle is None:
            raise SetupError('A verified bundle is required')
        if config_bytes is not None and not isinstance(config_bytes, bytes):
            raise SetupError('config_bytes must be bytes')
        data = config_bytes if config_bytes is not None else decoded(engine.read_record(self.home, self.config))
        if data is None:
            raise SetupError('Supply initial config_bytes before first start')
        # Private candidate: validation must not create a default config in HOME.
        with private_evidence_directory(self.evidence) as directory:
            name = 'config-' + secrets.token_hex(16)
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(data)
                env = dict(self.env, SWIFTWALL_CONFIG=str(self.evidence / name))
                self.command([self.bundle / 'bin/swiftwall', 'check-config'], env=env)
            finally:
                os.unlink(name, dir_fd=directory)

    def _verify_files(self, changes):
        for path, expected in changes.items():
            if engine.read_record(self.home, path) != expected:
                raise SetupError(f'File transaction read-back verification failed: {path}')

    def _files(self, changes):
        before = {path: engine.read_record(self.home, path) for path in changes}
        engine.transaction(self.home, changes)
        try:
            self._verify_files(changes)
        except Exception:
            # Read-back failures occur outside the helper's ordinary write-error
            # recovery. Restore this transaction before lifecycle recovery runs.
            engine.transaction(self.home, before)
            self._verify_files(before)
            raise

    def _install_engine(self, current):
        manifest = engine.load_manifest(self.home)
        engine.verify_owned(self.home, manifest)
        if manifest and manifest['current'] == current:
            return
        changes = {self.home / p: current[k] for k, p in engine.FILES.items()}
        changes[self.home / engine.MANIFEST] = engine.manifest_record(current, manifest['current'] if manifest else None)
        self._files(changes)
        engine.verify_owned(self.home, engine.load_manifest(self.home))

    def install(self, config_bytes=None, enable_widget=False):
        """config_bytes requires a fresh engine; enable_widget requires a fresh plugin."""
        return self._install('install', config_bytes, enable_widget)

    def update(self, enable_widget=False):
        return self._install('update', None, enable_widget)

    def _install(self, operation, config_bytes, enable_widget):
        with plugin.locked(self.home):
            self.plan(operation)  # All ownership/payload conflicts precede any mutation.
            installed, widget, _receipt = self._owned()
            assert self.bundle is not None  # plan() verified the bundle.
            current, payload = self._payload()
            plugin_action = plugin_update_action(widget['current'] if widget else None, payload)
            if installed is not None and config_bytes is not None:
                raise SetupError('config_bytes is only supported for a fresh engine installation; existing configuration is preserved')
            if enable_widget and widget is not None:
                raise SetupError('Enable is supported only on first plugin installation; existing activation is left untouched')
            initial = self.service.state()
            if installed is not None:
                self.verify_existing_environment()
            elif initial.get('LoadState') != 'not-found':
                self.service.verify_environment(self.engine_environment)
            self._stable_service_state(initial)
            if initial.get('ActiveState') == 'active' and installed is None:
                raise SetupError('Refusing active unowned service')
            if initial.get('ActiveState') == 'active':
                self.healthy(self._version(), allow_inherited=True)
            self._validate_config(config_bytes)
            version = self._version(self.bundle / 'bin/swiftwall')
            if version == '0.1.0':
                raise SetupError('New setup bundles require protocol-1 engine (v0.2 or newer)')
            controller = self._controller() if enable_widget else None
            if controller is not None:
                controller.prepare()
            snapshot = self._snapshot()
            self._backup(snapshot, 'before-' + operation)
            activation_armed = False
            startup_validated = False
            try:
                if installed:
                    self.service.stop()
                snapshot = self._snapshot() | {'service': initial}
                self._backup(snapshot, 'stopped-' + operation)
                self._install_engine(current)
                if config_bytes is not None:
                    self._files({self.config: engine.record(config_bytes, 0o600)})
                self.command(['systemctl', '--user', 'daemon-reload'])
                self.service.verify_environment(self.engine_environment)
                # Inactive updates stay inactive; first install always checks readiness.
                reply = None
                if installed is None or initial['ActiveState'] == 'active':
                    reply = self._start(version, snapshot['wallpaper'])
                    self._verify_preserved(snapshot)
                    expected_config = config_bytes if config_bytes is not None else decoded(snapshot['files'][str(self.config)])
                    if self.config.read_bytes() != expected_config:
                        raise SetupError('Startup changed configuration')
                startup_validated = True
                if widget is None:
                    if controller is not None:
                        activation_armed = True  # Publication may cause autodiscovery.
                        self._files({self.home / ACTIVATION: engine.record(b'{"version":1,"owned":true}\n', 0o600)})
                    plugin.transaction(self.home, None, payload, None)
                elif plugin_action == 'metadata-only update':
                    plugin.transaction(self.home, widget, payload, widget['current'])
                if controller is not None:
                    controller.activate()
                if installed is None:
                    self.service.systemctl('enable')  # Only after verified readiness.
                if installed is not None and self.service.state().get('UnitFileState') != initial.get('UnitFileState'):
                    raise SetupError('Update changed autostart state')
                self.checkpoint = snapshot
                self.record('setup-complete', operation=operation, backup=str(self.backup))
                return {'operation': operation, 'plugin': plugin_action, 'health': reply, 'backup': str(self.backup)}
            except (Exception, KeyboardInterrupt) as exc:
                try:
                    self._recover(snapshot, activation_armed=activation_armed,
                                  preserve_latest_state=startup_validated)
                except (Exception, KeyboardInterrupt) as recovery:  # noqa: BLE001 - retain backups even on a second Ctrl-C
                    detail = str(recovery) or type(recovery).__name__
                    raise SetupError(f'{operation} failed; recovery incomplete: {detail}; independent backup: {self.backup}') from exc
                raise SetupError(f'{operation} failed; original owned files/service recovered; backup: {self.backup}') from exc

    def _recover(self, snapshot, activation_armed=False, preserve_latest_state=True):
        managed_service = snapshot['files'][str(self.binary)] is not None or engine.load_manifest(self.home) is not None
        if managed_service:
            self.service.stop()
        latest = engine.read_record(self.home, self.state)
        original_backup = self.backup
        try:
            self._backup({'files': {str(p): engine.read_record(self.home, p) for p in (self.config, self.state)},
                          'service': self.service.state(), 'wallpaper': self._wallpaper()}, 'recovery-input')
        finally:
            # A second interrupt must not replace the full operation checkpoint
            # with the smaller diagnostic recovery-input snapshot.
            self.backup = original_backup
        cleanup = None
        if activation_armed:
            try:
                self._controller().deactivate()
            except Exception as exc:  # noqa: BLE001 - shell uncertainty must not block engine recovery
                cleanup = exc  # Never orphan loaded QML; still recover the engine.
        try:
            current = plugin.load_manifest(self.home)
            plugin.verify_owned(self.home, current)
            if cleanup is None and current != snapshot['plugin']:
                old = snapshot['plugin']
                plugin.transaction(self.home, current, old['current'] if old else None,
                                   old['previous'] if old else None)
        except Exception as exc:  # noqa: BLE001 - plugin failure must not block independent engine recovery
            cleanup = exc
        manifest = engine.load_manifest(self.home)
        engine.verify_owned(self.home, manifest)
        changes = {Path(p): item for p, item in snapshot['files'].items()}
        # Only trust newer history after startup validation or on explicit rollback.
        if preserve_latest_state and latest is not None:
            try:
                state = json.loads(base64.b64decode(latest['data']))
                if self._schema_one(state):
                    changes[self.state] = latest
            except (ValueError, AttributeError):
                pass
        if cleanup is not None:
            changes.pop(self.home / ACTIVATION, None)
        original = snapshot['service']
        # A failed enable may have committed. Disable while the newly installed
        # unit still exists; disabling after first-install removal can fail.
        if (managed_service and original.get('UnitFileState') != 'enabled'
                and self.service.state().get('UnitFileState') == 'enabled'):
            self.service.systemctl('disable')
        self._files(changes)
        self.command(['systemctl', '--user', 'daemon-reload'])
        now = self.service.state().get('UnitFileState')
        enabled = original.get('UnitFileState') == 'enabled'
        if managed_service and (now == 'enabled') != enabled:
            self.service.systemctl('enable' if enabled else 'disable')
        if managed_service and original.get('ActiveState') == 'active':
            wallpaper = self._wallpaper()  # Preserve current wallpaper, not stale backup.
            self._start(self._version(), wallpaper, allow_inherited=True)
            self._verify_preserved({'files': {str(self.state): changes[self.state]}})
            self._verify_files({self.config: changes[self.config]})
        if cleanup is not None:
            raise SetupError('Engine recovered; widget cleanup unverified, plugin files/receipt retained') from cleanup

    def rollback(self):
        if self.checkpoint is None:
            return {'available': False, 'reason': 'No checkpoint in this manager; use retained independent evidence for manual recovery'}
        with plugin.locked(self.home):
            self._owned()
            checkpoint = self.checkpoint
            self.checkpoint = None  # Exactly one recovery attempt, never toggle generations.
            self._recover(checkpoint, activation_armed=(checkpoint['plugin'] is None and (self.home / ACTIVATION).exists()))
            return {'available': True, 'restored': True, 'backup': str(self.backup)}

    def _remove_activation(self):
        # Explicit caller-confirmed uninstall authorizes cleanup of a VERIFIED
        # owned plugin, including installations predating setup receipts.
        widget = plugin.load_manifest(self.home)
        plugin.verify_owned(self.home, widget)
        if widget is None:
            raise SetupError('Refusing activation cleanup without an owned plugin')
        controller = self._controller()
        observed = controller.snapshot()
        normalized = without_plugin(observed['config'])
        # Rebase on CURRENT unrelated settings, not a stale saved shell.json.
        controller.baseline = deepcopy(observed)
        controller.baseline['config'] = normalized
        controller.baseline['persisted'] = without_plugin(observed['persisted'])
        controller.baseline['registry'].pop(PLUGIN_ID, None)
        controller.enable_attempted = True
        controller.deactivate()

    def uninstall(self, remove_data=False):
        with plugin.locked(self.home):
            report = self.plan('uninstall')
            installed, widget, _receipt = self._owned()
            if installed is None and widget is None:
                return report
            if installed is not None:
                self.verify_existing_environment()
            snapshot = self._snapshot()
            if installed is not None:
                self._stable_service_state(snapshot['service'])
            self._backup(snapshot, 'before-uninstall')
            widget_state = None
            mutation_started = False
            try:
                # Shell failure precedes engine/file removal. Never restore
                # shell.json or replay enable to undo a delivered disable.
                if widget is not None:
                    widget_state = 'unverified'
                    self._remove_activation()
                    widget_state = 'disabled'
                if installed is not None:
                    mutation_started = True  # Stop may commit before its reply/interrupt.
                    self.service.stop()
                snapshot = self._snapshot() | {'service': snapshot['service']}
                self._backup(snapshot, 'stopped-uninstall')
                self._owned()
                if installed is not None:
                    self.service.systemctl('disable')
                if widget is not None:
                    mutation_started = True
                    plugin.transaction(self.home, widget, None, None)
                engine.uninstall(self.home)
                changes = {self.home / ACTIVATION: None}
                if remove_data:
                    # Exact regular known files only; no recursive removal/images.
                    changes.update({self.config: None, self.state: None})
                self._files(changes)
                self._verify_files({self.home / path: None
                                    for path in (*engine.FILES.values(), engine.MANIFEST)})
                self.command(['systemctl', '--user', 'daemon-reload'])
                self.checkpoint = None
                return {'operation': 'uninstall', 'data_removed': remove_data, 'backup': str(self.backup)}
            except (Exception, KeyboardInterrupt) as exc:
                widget_note = (f'; widget state {widget_state}; widget recovery/manual activation pending; '
                               'no automatic re-enable attempted' if widget_state is not None else '')
                if not mutation_started:
                    raise SetupError(f'Uninstall failed before engine/file removal; original engine/files/service unchanged{widget_note}; backup: {self.backup}') from exc
                try:
                    self._recover(snapshot)
                except (Exception, KeyboardInterrupt) as recovery:  # noqa: BLE001 - retain backup even on a second Ctrl-C
                    detail = str(recovery) or type(recovery).__name__
                    raise SetupError(f'Uninstall failed; recovery incomplete: {detail}{widget_note}; backup: {self.backup}') from exc
                raise SetupError(f'Uninstall failed; files/engine recovered; original service state restored{widget_note}; backup: {self.backup}') from exc
