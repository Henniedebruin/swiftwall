# File-only installation

Run these commands from the repository root, supplying the path to an already
built executable (the installer does not build or execute it):

```sh
python3 scripts/install.py --binary /absolute/path/to/built/swiftwall --dry-run
python3 scripts/install.py --binary /absolute/path/to/built/swiftwall
```

Python 3 and its standard library are the only installer dependencies. The
installer writes only these files beneath the selected home:

- `.local/bin/swiftwall` (0755)
- `.config/systemd/user/swiftwall.service` (0644)
- `.local/state/swiftwall/install-manifest.json` (0600)

It creates missing parent directories with mode 0700, leaving existing directory
modes alone. The manifest contains hashes, bytes and modes for the current and at
most one previous owned version. It includes executable bytes, so budget disk
space accordingly.

**All scripts are strictly file-only.** They never invoke systemctl, reload a
service manager, enable, disable, start, stop, or restart a service, execute the
daemon, or change wallpapers. A first install does not enable autostart. Existing
manual enablement is left untouched, including systemd wants symlinks. The former
`--enable-autostart` flag is rejected before any file changes, even with
`--dry-run` or when installing into the actual home directory.

## Manual activation — only after explicit review

For installation into your actual home only: review the installed executable,
`~/.config/systemd/user/swiftwall.service`, and application configuration. Only if
you explicitly want graphical-session autostart and an immediate daemon start,
run these commands yourself **after that review**:

```sh
systemctl --user daemon-reload
systemctl --user enable --now swiftwall.service
```

These are manual instructions, not installer actions. They require a running
user systemd manager. Starting the daemon may have its normal wallpaper effects.
Do not run them for an isolated `--home` installation: systemctl addresses your
live user manager, not that alternate home.

The optional supervised `Deployment` coordinator has a stricter environment
contract than these manual instructions. It verifies preconfigured loaded-unit
`Environment=` values and the running process environment, refuses unsupported
environment sources, and never creates automatic overrides. See
[the coordinator contract](../docs/V02.md#installation-boundaries) before using it.

Manual service commands are not transactional. If a command fails, inspect the
live service state and resolve it manually. Installer rollback cannot undo a
manual enable/start, restore live service settings or running processes, or
reverse wallpaper effects.

## Before update, rollback, or uninstall

If the service is active or enabled, manually disable and stop it **before any
file-changing update, rollback, or uninstall**:

```sh
systemctl --user disable --now swiftwall.service
```

Confirm that it succeeded before proceeding. If you started a daemon outside
systemd, stop that process separately as well. The scripts do not detect or stop
running processes. File replacement and automatic recovery on write failure
cannot guarantee live-process rollback; even a successful explicit rollback
restores files only.

## Upgrade and rollback

After the stop/disable step above when applicable, run the regular install
command with a new binary to upgrade. Existing paths must match the manifest's
bytes and modes. Unowned files, missing owned files, and symlinks (including parent
directory symlinks) are refused without replacement. An identical reinstall
preserves the existing rollback generation.

Restore the previous owned binary **and** unit after an upgrade:

```sh
python3 scripts/install.py --rollback --dry-run
python3 scripts/install.py --rollback
```

Rollback consumes the one previous generation. It refuses locally changed files
and does not run systemctl. There is no previous generation on a first install;
use uninstall to remove it. After an upgrade or rollback, leave the service
disabled unless you explicitly review the resulting files and choose to repeat
the manual activation procedure above. Nothing automatically re-enables it.

## Uninstall

After the manual stop/disable step above when applicable:

```sh
python3 scripts/uninstall.py --dry-run
python3 scripts/uninstall.py
```

If the unit was previously loaded into your live user manager, manually refresh
that manager after successful uninstall:

```sh
systemctl --user daemon-reload
```

With no previous activation or loaded unit, only the Python commands are needed.
Uninstall removes the unchanged owned binary, unit and installation manifest
(including its previous generation), preserving neighboring files, config, state
and directories. Modified owned files cause the entire uninstall to be refused;
there is no force flag. Restore their installed contents/modes or resolve
ownership manually first.

## Isolation and validation

Both scripts accept `--home /path/to/isolated/home` and `--dry-run`. Neither live
nor isolated installation ever calls a service manager. Dry runs validate without
writing files or running commands. Run the tests without installing anything or
calling systemctl:

```sh
python3 -m unittest discover -s scripts -p 'test_install.py' -v
```

Transactions restore original bytes and modes on ordinary write exceptions and
remove newly created empty directories. This is **not** a power-loss journal or
a defense against concurrent hostile filesystem edits. There is no singleton
lock: **never run install, rollback, or uninstall concurrently**, including from
another terminal. The ownership manifest is trusted local metadata; do not edit
it. The service has `After=`/`PartOf=graphical-session.target`, is wanted by that
target, and uses a control-group kill policy and bounded failure restarts. Its
`WantedBy=` declaration alone does not enable it.
