# File-only SwiftWall plugin lifecycle

The plugin installer is independent of the SwiftWall engine installer. It does
not build, install, launch, stop, or manage the engine. An appropriate separately
installed engine is needed for working wallpaper controls. No external Python
dependencies are required; the installer uses Python 3 and Linux `flock`.

## Commands

Run from the repository root. Every operation is an explicit subcommand:

```sh
python3 scripts/plugin_install.py install --dry-run
python3 scripts/plugin_install.py install
python3 scripts/plugin_install.py verify
python3 scripts/plugin_install.py uninstall --dry-run
python3 scripts/plugin_install.py uninstall
python3 scripts/plugin_install.py rollback --dry-run
python3 scripts/plugin_install.py rollback
```

These are alternatives, not a sequence to execute automatically. All subcommands
accept `--home /absolute/path/to/existing/home`, including temporary homes.
`install` also accepts `--source /path/to/repository`; its default is the root
containing this script. `verify` is always read-only; the other subcommands accept
`--dry-run`, which validates inputs/ownership without creating files, directories,
lock files, stages, or import caches. The target home must already exist.

**Disable an existing widget/plugin manually before updates, uninstall or rollback.** The CLI warns before uninstall/rollback, including
when dry-running. It does not call live IPC to perform this for you. Removing
files cannot unload code already held by a running shell.

## Placement is not enablement

Installation only places files at:

```text
~/.config/omarchy/plugins/io.github.henniedebruin.swiftwall/
    manifest.json
    plugin/BarWidget.qml
    plugin/FolderPicker.qml
    plugin/Panel.qml
    plugin/Service.qml
    plugin/SettingsDraft.qml
    plugin/SharedService.qml
    plugin/pick_folder.py
```

This is the complete eight-file allowlist. Repository contents, assets, caches,
extra source files, engine binaries, and services are never copied. Source extras
are ignored, not removed or rewritten. QML/JSON files are installed as `0644` and
`pick_folder.py` as `0755`, independently of source modes and umask.

The registry **may autodiscover the plugin when files are placed in its watched
directory**. That is discovery, not a request by this installer to enable the
plugin or insert a widget. There are no registration, rescan, enable, systemctl,
shell IPC, or other external commands. The installer never edits configuration or
bar settings. Existing configuration may affect what a running shell does when
it discovers these files; disable an existing widget/plugin first if necessary.

The source manifest is checked with pure Python for schema version 1, the exact
plugin ID, expected kinds, a nonempty version, and the two fixed local entrypoints.
No native validator, QML runtime, payload script, or engine is executed. This is
an identity/layout check, not a full QML or runtime compatibility validator.

## Ownership and generations

Ownership metadata lives outside the watched plugin tree:

```text
~/.local/state/swiftwall/plugin-install-manifest.json
```

The private (`0600`) metadata stores the exact allowlist, SHA-256 hashes,
base64-encoded bytes, and file modes for the current and previous generations.
An update records the formerly current generation as previous. An identical
reinstall is a no-op and preserves that previous generation.

- An existing plugin directory without valid ownership metadata is refused,
  even if empty or its contents happen to match the source.
- Missing or changed owned files, changed modes, unknown files or directories
  inside the owned plugin, invalid metadata, and symlink paths (including source,
  home, destination, metadata, and their parents) are refused.
- Only explicit source paths and the two expected destination directories are
  inspected; no arbitrary directory trees or root symlinks are followed.
- Uninstall removes only a fully verified owned tree and its ownership metadata.
- Rollback restores the previous generation and consumes that saved generation.
  When no previous generation exists (including a first install or after a
  successful rollback), rollback refuses without changing any files or metadata.
  This also applies to dry-run. Repeated rollback never uninstalls the plugin or
  toggles between versions; use explicit `uninstall` for removal.
- Other plugins, engine installation metadata, bar configuration, wallpaper
  collections, and unrelated files are preserved. Empty ancestor directories
  may remain after successful removal; they are never recursively removed.

## Transactions and limits

Cooperating installers serialize with an advisory `flock` on the existing home
directory inode. Read-only operations take a shared lock; changes take an
exclusive lock. No process-ID lock file can become stale after a process exits.
This lock serializes these plugin tools, not the separate engine installer; never
run engine and plugin file transactions concurrently.

Payload and metadata are staged in a private temporary directory under the state
directory. The old verified plugin tree is renamed there as a backup, then the
new tree is renamed into place and metadata is atomically replaced. All tree
renames, including backup creation and recovery, use Linux
`renameat2(RENAME_NOREPLACE)` through stdlib `ctypes`. A destination that appears
after ownership verification is never replaced, even if it is an empty directory.
Missing libc support or unsupported kernel/filesystem flags fail closed; there is
no check-then-rename or ordinary `rename` fallback. Dry-run does not probe rename
support by writing temporary files, so it cannot certify kernel/filesystem support.

Uninstall uses the same backup mechanism. Ordinary exceptions restore the prior
tree and metadata and remove staging artifacts when safe. Before recovery moves
a published tree, its exact file set, contents, modes, and directory identity are
checked. Backup contents and identity are also checked before recovery, and backup
contents are checked again before successful cleanup. Unexpected replacements or
additions are preserved rather than moved into recursive cleanup. A conflicting
recovery destination is never replaced. In these cases the error reports the
retained transaction directory containing the backup (and possibly the staged or
failed new generation); do not remove it before manual recovery. The unexpected
destination and any added files remain untouched. Ownership metadata may still
refer to a generation that could not be restored; subsequent operations refuse
that mismatch rather than adopting the conflicting directory.

The state and plugin directories must be on the same filesystem for tree
renames. Both dry-run and real mutations compare `st_dev` for the state directory
and destination, using their nearest existing directory ancestors when paths are
not yet created. A detected mismatch is refused before creating any directories
or staging files. The check is repeated after creating staging parents. This is
a read-only preflight, not a guarantee against mount changes or special bind-mount
rename restrictions: an actual cross-device rename failure still fails safely
with recovery, never an in-place copy. Tests simulate device mismatches and
rename failures without making real mounts.

This is not a multi-path power-loss transaction: a crash or power failure between
renames can leave a backup and mismatched metadata requiring manual recovery.
Atomic replacement also does not guarantee atomic discovery by a live shell.
Metadata and advisory locks are trusted local-user state, not a security boundary
against hostile same-UID writers, manual file edits during a transaction, or
uncooperative programs. No running processes or live configuration are restored.

## Isolated tests

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts -p test_plugin_install.py -v
```

Tests use temporary source trees and homes only. They cover payload allowlisting,
source contract validation, hashes/bytes/modes, unowned/modified/unknown paths,
symlinks, read-only operations, updates, removal, rollback, injected transaction
failures, no-clobber publication/recovery races, unsupported rename primitives,
simulated cross-device preflight, locking, configuration preservation, and refusal
to launch processes.
No live installation or activation is part of the tests.

## Distribution review

File ownership and manifest validation do not establish marketplace acceptance
or release readiness. See the [hardening results and remaining checklist](../docs/PUBLIC_READINESS.md).
The widget now fails closed when runtime environment/overrides cannot identify a
valid absolute Unix socket path, with a diagnostic in its connection error.
`SWIFTWALL_RUNTIME_DIR` takes precedence over `XDG_RUNTIME_DIR`; an invalid explicit
override does not silently select the fallback daemon. Status replies on the
command socket cannot overwrite a countdown after a newer subscribed snapshot
or instance was accepted.
