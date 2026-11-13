"""Explicit terminal setup wizard. No service/shell operations on import or dry-run."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time

from release_bundle import binary_version, validate_version
from setup_download import DownloadError, fetch_release, verify_directory
from setup_support import SetupError, SetupManager

SOURCE = Path(__file__).resolve().parents[1]
LOCAL_FS = frozenset(('ext2', 'ext3', 'ext4', 'btrfs', 'xfs', 'f2fs', 'zfs', 'tmpfs',
                      'vfat', 'exfat', 'ntfs3', 'overlay'))


def dependencies(operation, which=shutil.which, run=subprocess.run):
    if sys.version_info < (3, 11):
        raise SetupError('Python 3.11 or newer is required')
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise SetupError('Only Linux x86_64 release bundles are supported initially')
    names = ('systemctl', 'busctl') if operation == 'status' else ('systemctl', 'busctl', 'omarchy', 'omarchy-shell', 'findmnt', 'ss')
    missing = [name for name in names if which(name) is None]
    if missing:
        raise SetupError('Missing dependencies: ' + ', '.join(missing) + '. Install through Omarchy/Arch; setup does not install packages.')
    if operation in ('install', 'update'):
        result = run(['/usr/bin/python3', '-c', 'from gi.repository import Gio'],
                     capture_output=True, timeout=5, check=False)
        if result.returncode:
            raise SetupError('Folder picker needs python-gobject (Gio) and a working XDG FileChooser portal')
        # Confirm a chooser interface without opening a chooser or altering bus settings.
        result = run(['busctl', '--user', 'get-property', 'org.freedesktop.portal.Desktop',
                      '/org/freedesktop/portal/desktop', 'org.freedesktop.portal.FileChooser', 'version'],
                     capture_output=True, text=True, timeout=5, check=False)
        if result.returncode or not result.stdout.startswith('u '):
            raise SetupError('XDG Desktop Portal FileChooser is unavailable in this graphical session')


def environment(home):
    env = dict(os.environ)
    if any(k.startswith('SWIFTWALL_') for k in env):
        raise SetupError('Unset SWIFTWALL_ test overrides before ordinary setup')
    runtime = env.get('XDG_RUNTIME_DIR', '')
    if not Path(runtime).is_absolute():
        raise SetupError('Run setup inside your logged-in Omarchy graphical session (XDG_RUNTIME_DIR missing)')
    env['HOME'] = str(home)
    defaults = {'SWIFTWALL_CONFIG': str(Path(env.get('XDG_CONFIG_HOME', home / '.config')) / 'swiftwall/config.toml'),
                'SWIFTWALL_STATE_DIR': str(Path(env.get('XDG_STATE_HOME', home / '.local/state')) / 'swiftwall'),
                'SWIFTWALL_RUNTIME_DIR': str(Path(runtime) / 'swiftwall'),
                'SWIFTWALL_BACKGROUND_LINK': str(home / '.local/state/omarchy/current/background'),
                'SWIFTWALL_OMARCHY_COMMAND': shutil.which('omarchy') or '/usr/bin/omarchy'}
    env.update(defaults)
    return env


def folder(value, run=subprocess.run):
    # Do not normpath/abspath first: symlink/.. resolution must use filesystem semantics.
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise SetupError('Wallpaper root must be an existing local directory')
    result = run(['findmnt', '-T', str(path), '-n', '-o', 'FSTYPE'],
                 capture_output=True, text=True, timeout=5, check=False)
    if result.returncode or result.stdout.strip() not in LOCAL_FS:
        raise SetupError('Choose a supported local folder, not network/GVFS/FUSE storage')
    with os.scandir(path) as entries:
        if not any(Path(entry.name).suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp')
                   and entry.is_file() for entry in entries):
            raise SetupError('Folder has no JPEG, PNG or WebP files directly inside it (no recursive discovery)')
    return str(path)


def config_bytes(folders, interval, selection):
    if not 1 <= interval <= 31536000 or selection not in ('shuffle', 'sequential') or not folders:
        raise SetupError('Choose folders, a valid interval and Shuffle or Sequential')
    # JSON string escaping is a TOML-compatible basic-string subset (no shell quoting).
    return ('interval_seconds = ' + str(interval) + '\nselection = ' + json.dumps(selection)
            + '\nfolders = [' + ', '.join(json.dumps(p, ensure_ascii=False) for p in folders) + ']\n').encode()


def first_run(args, prompt=input, normalize=folder):
    roots = list(args.folder)
    if not roots and not args.yes:
        print('\nSwiftWall Setup — choose local wallpaper folders (nonrecursive).')
        print('Enter each full path without shell quotes. A blank line finishes the list.')
        while True:
            value = prompt('Wallpaper folder: ')
            if not value:
                break
            try:
                roots.append(normalize(value))
            except (OSError, RuntimeError, ValueError) as exc:
                print('Folder rejected: ' + str(exc))
    if not roots:
        raise SetupError('At least one --folder is required for noninteractive first-time installation')
    canonical = list(dict.fromkeys(normalize(p) for p in roots))
    interval = args.interval
    selection = args.selection
    if not args.yes:
        value = prompt('Rotation interval in seconds [300]: ')
        if value:
            interval = int(value)
        value = prompt('Selection [shuffle/sequential, default shuffle]: ')
        if value:
            selection = value.lower()
    return config_bytes(canonical, interval, selection)


@contextmanager
def bundle_for(args):
    root = Path(args.bundle).absolute() if args.bundle else SOURCE
    if (root / 'release.json').is_file():
        info = verify_directory(root, args.version if args.bundle else None)
        if args.bundle or args.version is None or args.version == info['version']:
            yield root
            return
        # A portable older launcher can fetch an explicitly selected newer release.
        # An explicit --bundle, in contrast, must match its requested version.
    if args.bundle:
        raise SetupError('--bundle must be an extracted verified release, not a source checkout')
    if not args.version:
        raise SetupError('No release bundle found. Supply --version X.Y.Z to download an exact published release; no release is downloaded by default.')
    with tempfile.TemporaryDirectory(prefix='swiftwall-setup-') as temporary:
        root, _info = fetch_release(args.version, Path(temporary), args.sha256)
        yield root


def widget_connection(manager):
    """Kernel peer proof only; not a fabricated GUI handshake/status assertion."""
    result = manager.command(['ss', '-xnpH'])
    lines = [line.split() for line in result.splitlines()]
    pid = manager.service.state().get('MainPID', '0')
    matches = []
    for fields in lines:
        if len(fields) < 9 or fields[4] != str(manager.control_socket) or ('pid=' + pid + ',') not in ' '.join(fields[8:]):
            continue
        local, peer = fields[5], fields[7]
        for remote in lines:
            if len(remote) >= 9 and remote[5] == peer and remote[7] == local and 'quickshell' in ' '.join(remote[8:]):
                matches.append({'daemon_pid': pid, 'socket': str(manager.control_socket), 'peer': 'quickshell'})
    return {'connected_kernel_peer': bool(matches), 'connections': matches,
            'gui_handshake_and_subscription': 'Confirm the live panel countdown and controls manually; a socket alone does not prove them.'}


def wait_widget_connection(manager, timeout=35, clock=time.monotonic, sleep=time.sleep):
    # Retry only this bounded installation check; no resident subprocess polling.
    deadline = clock() + timeout
    while True:
        report = widget_connection(manager)
        if report['connected_kernel_peer']:
            return report
        remaining = deadline - clock()
        if remaining <= 0:
            raise SetupError('No verified widget socket connection within the bounded reconnect window')
        sleep(min(0.5, remaining))


def parser():
    p = argparse.ArgumentParser(description='SwiftWall Setup: install and maintain the local carousel without Cargo.')
    p.add_argument('operation', choices=('install', 'update', 'status', 'uninstall'))
    p.add_argument('--version', help='exact published release version, never latest')
    p.add_argument('--sha256', help='optional independently obtained archive SHA-256')
    p.add_argument('--bundle', type=Path, help='already-extracted release bundle (inventory rechecked)')
    p.add_argument('--dry-run', action='store_true', help='no downloads, writes, commands or service changes; report a plan only')
    p.add_argument('--yes', action='store_true', help='explicitly confirm the displayed plan; first install also needs --folder')
    p.add_argument('--folder', action='append', default=[], help='first-install local folder; repeat for more roots')
    p.add_argument('--interval', type=int, default=300)
    p.add_argument('--selection', choices=('shuffle', 'sequential'), default='shuffle')
    p.add_argument('--enable-widget', action='store_true', help='explicitly opt into one widget enable on first installation')
    p.add_argument('--remove-data', action='store_true', help='uninstall only: also remove exact SwiftWall config/state files, never images')
    return p


def main(argv=None, prompt=input):
    args = parser().parse_args(argv)
    try:
        if args.version:
            validate_version(args.version)
        if args.remove_data and args.operation != 'uninstall':
            raise SetupError('--remove-data is only valid with uninstall')
        home = Path.home()
        env = environment(home)
        root = Path(args.bundle).absolute() if args.bundle else SOURCE
        local_bundle = root if (root / 'release.json').is_file() else None
        manager = SetupManager(home, bundle=local_bundle, env=env)
        if args.dry_run:
            report = manager.plan('status')
            data_action = ('remove exact SwiftWall config/state files after backup; NEVER wallpaper images'
                           if args.operation == 'uninstall' and args.remove_data
                           else 'retain config/state' if args.operation == 'uninstall'
                           else 'preserve existing; require validated first-run folders if absent')
            report.update(operation=args.operation, dry_run=True, downloads=False, changes=False,
                          version=args.version, config=data_action, remove_data=args.remove_data,
                          release_validation='not performed; dry-run does not execute binaries or download',
                          widget='disable/remove only owned SwiftWall references' if args.operation == 'uninstall'
                                 else 'enable once only if explicitly requested and first install')
            print(json.dumps(report, indent=2))
            return 0
        if os.geteuid() == 0:
            raise SetupError('Run as your ordinary desktop user, not root or sudo')
        dependencies(args.operation)
        if args.operation == 'status':
            report = manager.status()
            if report.get('service', {}).get('ActiveState') == 'active':
                report['health'] = manager.healthy(manager._version(), allow_inherited=True)
                report['widget'] = widget_connection(manager)
            print(json.dumps(report, indent=2))
            return 0
        if args.operation == 'uninstall':
            print(json.dumps(manager.plan('uninstall'), indent=2))
            print('Remove owned SwiftWall engine/service/plugin; never images. Config/state: ' + ('REMOVE exact files (backup retained).' if args.remove_data else 'KEEP.'))
            if not args.yes and prompt('Type uninstall to confirm: ') != 'uninstall':
                print('Cancelled; no changes made.')
                return 0
            print(json.dumps(manager.uninstall(remove_data=args.remove_data), indent=2))
            return 0
        if args.folder and manager.plan('status')['engine_owned']:
            raise SetupError('--folder is first-install only; updates preserve existing settings')
        with bundle_for(args) as bundle:
            expected_version = json.loads((bundle / 'release.json').read_text())['version']
            if binary_version(bundle / 'bin/swiftwall') != expected_version:
                raise SetupError('Binary version differs from the verified release identity; no installation performed')
            manager = SetupManager(home, bundle=bundle, env=env)
            proposed = manager.plan(args.operation)
            new_config = None
            if not manager.config.exists():
                if proposed['engine_owned']:
                    raise SetupError('Existing installation has missing configuration; recover it before updating')
                new_config = first_run(args, prompt)
                print(new_config.decode())
            enable = args.enable_widget
            if not proposed['plugin_owned'] and not args.yes and not enable:
                enable = prompt('Enable the SwiftWall bar widget? [y/N]: ').lower() == 'y'
            print(json.dumps(proposed, indent=2))
            print('Install the verified bundle, validate configuration, use the existing service, and preserve images/settings/history.')
            if not args.yes and prompt('Type install to confirm: ') != 'install':
                print('Cancelled; no installation changes made.')
                return 0
            report = manager.install(config_bytes=new_config, enable_widget=enable) if args.operation == 'install' else manager.update(enable_widget=enable)
            try:
                check_widget = enable
                if proposed['plugin_owned'] and manager.service.state().get('ActiveState') == 'active':
                    snapshot = manager._controller().snapshot()
                    check_widget = snapshot['registry'].get('io.github.henniedebruin.swiftwall') is True
                if check_widget:
                    report['widget'] = wait_widget_connection(manager)
                    if not args.yes:
                        if prompt('Open the panel. Does its countdown work and do Pause/Resume controls respond? [y/N]: ').lower() != 'y':
                            raise SetupError('Manual widget acceptance not confirmed')
                    else:
                        report['manual_acceptance'] = 'pending: --yes cannot confirm actual panel behaviour'
            except (Exception, KeyboardInterrupt):
                manager.rollback()
                raise
            print(json.dumps(report, indent=2))
            print('Keep this extracted setup bundle for status/update/uninstall. No helper is silently copied into PATH.')
            return 0
    except (SetupError, DownloadError, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print('SwiftWall setup: ' + str(exc), file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print('\nCancelled. Check swiftwall-setup status if installation had already begun.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
