# Simple local installation and maintenance

This branch prepares prebuilt distribution. **No release has been published by this
work.** The commands below become available from an approved release bundle; they
are not a claim that a release download exists today. Source installation remains
available in [ADVANCED_INSTALL.md](ADVANCED_INSTALL.md).

## Before starting

Use a logged-in Omarchy graphical session on Linux x86_64, as your ordinary user.
Do not use sudo. Requirements are Python 3.11+, Omarchy/Quickshell, systemd user
services, `busctl`, `ss` and `findmnt` (normally supplied by systemd/iproute2/util-linux),
plus PyGObject/Gio and a working XDG Desktop Portal FileChooser backend. Setup
checks requirements; it never installs packages or upgrades your desktop.

A filesystem-backed wallpaper collection must contain JPEG, PNG or WebP files
immediately inside each folder. Subfolders are not included automatically. Network,
GVFS and FUSE folders are rejected for new collections. Existing settings are not
replaced during an update.

## Get and verify a release

Once a release is approved, download its Linux x86_64 archive and `SHA256SUMS` from
[SwiftWall Releases](https://github.com/Henniedebruin/swiftwall/releases). Verify the
archive before extracting or executing its setup helper:

```sh
sha256sum --check SHA256SUMS
```

Extract the archive using your file manager, open a terminal in the extracted
folder, and run:

```sh
./swiftwall-setup install
```

The wizard:

1. Checks dependencies and the session's portal.
2. Verifies the release's exact file inventory and ownership of existing files.
3. Asks for one or more local wallpaper folder paths, without shell quotes.
4. Offers a 300-second interval and shuffle selection; sequential is optional.
5. Offers to enable the Omarchy widget and displays the proposed changes.
6. Requires the word `install` before it changes installation files or services.
7. Validates configuration through the bundled engine before starting a service.
8. Installs the engine, user service and plugin, checks bounded IPC readiness and
   offers a manual panel check. It does not launch a second daemon.

Only file-placement and service changes begin after confirmation. Temporary download
and validation work can happen earlier. The default interval is five minutes, and
startup preserves the current wallpaper. Once running, automatic rotation changes
wallpaper at the configured deadline. The images themselves are never modified.

Keep the extracted directory: the helper is portable and is deliberately not copied
into PATH or installed as an opaque self-updater. From that directory:

```sh
./swiftwall-setup status
./swiftwall-setup install --dry-run
```

Dry-run does not download, execute binaries, write files or query/change services.
It checks local file ownership and describes the plan. It cannot certify a release
not yet downloaded, a working session, or future payload compatibility.

For noninteractive setup, provide explicit folder and widget choices:

```sh
./swiftwall-setup install --yes --folder "$HOME/Pictures/Wallpapers" --enable-widget
```

`--yes` is installation consent, not proof of visual/panel correctness. The helper
reports manual acceptance pending in that mode. Read the status and open the panel.
The helper checks kernel socket peers and the daemon's hello/status, but does not
pretend an independent CLI probe proves the widget's handshake or subscription.
A working panel countdown and controls must be confirmed manually. After activation
or an update to an enabled widget, setup allows a bounded reconnect window; it does
not send a second enable request to force a connection.

## Updating

Select an exact approved release version, not a moving `latest` URL:

```text
./swiftwall-setup update --version X.Y.Z --dry-run
./swiftwall-setup update --version X.Y.Z
```

Replace `X.Y.Z` with a real published release. The helper downloads only the fixed
SwiftWall GitHub release asset and SHA256SUMS over HTTPS, bounds downloads and
extraction, verifies every file and checks the binary's exact version. An optional
`--sha256 DIGEST` pins an independently obtained archive checksum for remote downloads.
No artifact is executed when its checksum or inventory fails. `update` continues
running the setup helper you launched; it does not replace that helper with code
from the downloaded archive. To use new installer fixes or a changed release
contract, download, verify and extract the newer setup bundle first, then run its
helper. An older helper may safely reject a newer inventory/schema.

An update preserves current folders, interval, mode, history, pause state and the
original enabled/running state. It stops the existing service, makes a verified
independent stopped-state backup, replaces only owned files, and waits for readiness
before accepting success. It refuses conflicting environment overrides and never
silently adopts a Git-managed marketplace plugin or unowned engine.

### Current update boundary

**Changed QML plugin payloads are refused before mutation.** Long-running Quickshell
can cache component URLs; replacing files and reenabling a plugin is not proven to
load the new generation. Identical plugin payloads are left untouched while the
engine can update. Version/descriptive manifest metadata may update when every QML
and helper file is byte-identical and the structural plugin contract is unchanged;
this lets a new engine release carry its matching plugin version without new code
at cached URLs or another enable request. Engine-only v0.1 installations can gain the first plugin through
the same supervised setup path. An existing modified/unowned/Git-managed plugin
requires deliberate manual migration, not an automatic ownership takeover.

This initial helper is therefore not a universal updater. Before a release with
changed QML, maintainers must provide a separately tested shell-safe migration.
The helper does not restart Quickshell or the graphical session to work around it.

## Uninstalling

```sh
./swiftwall-setup uninstall --dry-run
./swiftwall-setup uninstall
```

Type `uninstall` to confirm. The helper verifies ownership, disables only its widget,
preserves unrelated bar entries/settings, stops/disables the owned service and
removes verified engine/plugin files. Wallpaper directories and images are never
removed. Configuration and state remain by default.

To explicitly remove the known SwiftWall configuration and state files as well:

```sh
./swiftwall-setup uninstall --remove-data
```

This removes only those exact regular files, never their parent directories or a
collection. It refuses symlinks. Private recovery backups/evidence are retained;
they may include copies of settings/state even after `--remove-data`. This is not
a secure-erasure command. Remove retained backups separately only after review.
The portable extracted setup bundle is also left for you to delete yourself.

## Recovery and troubleshooting

- **Missing dependencies/portal:** install the named packages through your normal
  Omarchy/Arch process. Setup makes no system-wide changes. A native chooser still
  needs manual focus/accept/cancel checks on your desktop.
- **Missing release or checksum mismatch:** no installation is performed. Check the
  exact version and upstream release. Never bypass the check to proceed.
- **No configuration:** first install needs readable local folders before starting.
  For an existing installation, recover its missing configuration instead of
  silently replacing it with defaults.
- **Changed/unowned files:** setup refuses overwrite/removal. Back up and inspect
  the conflicting installation before a deliberate migration.
- **Unavailable/ambiguous service environment:** setup fails closed. Systemd 262
  can omit an empty EnvironmentFiles property; the compatibility path requires an
  authoritative typed-empty D-Bus reply, not an assumed empty value.
- **Widget enable times out:** setup sends at most one enable and reconciles
  authoritative registry/effective/persisted state. It never blindly retries.
- **Failure during install/update:** the same operation attempts one verified
  recovery, preserving the active wallpaper and compatible latest carousel state.
  An unverified shell cleanup is reported as pending and evidence is retained.
- **Interrupted process/power failure:** private backups live under the helper's
  `~/.local/state/swiftwall/setup-evidence/`. They contain file bytes/modes, ownership
  records, service metadata and stopped state. Inspect before recovery; do not replay
  arbitrary or stale files over the current wallpaper or unrelated shell settings.
  The initial helper has no cross-process automatic `rollback` CLI. Its rollback
  checkpoint is operation-scoped, and independent backups support manual recovery.
- **Status:** `./swiftwall-setup status` reports ownership, service and IPC health.
  Use `journalctl --user -u swiftwall.service --no-pager` for service logs. The panel
  currently has no aggregate image-count label; daemon status reports the count.

Filesystem stalls in uninterruptible kernel I/O are not cancellable by a userspace
timeout. File-tree and metadata commits are not a multi-path power-loss transaction.
See [PLUGIN_INSTALL.md](../packaging/PLUGIN_INSTALL.md) for these limitations.

## Marketplace and security

SwiftWall's verified marketplace snapshot is still correctly `manual-setup`.
Omarchy's plugin installer does not provision the companion Rust daemon. A release
bundle/helper simplifies the separate step but does not remove the requirement.
Do not let both Omarchy's Git-based plugin manager and SwiftWall's ownership-based
installer manage the same plugin directory. See
[MARKETPLACE_INSTALLATION.md](MARKETPLACE_INSTALLATION.md).

SHA-256 checksums downloaded alongside an asset detect corruption, not compromise
of the upstream GitHub account. Inventory hashes do not authenticate a bundle by
themselves. Use trusted upstream downloads and an independently trusted digest when
available. See [RELEASES.md](RELEASES.md) for provenance and release approval.
No `curl | bash`, hidden package installation, marketplace reclassification, automatic
public release or silently executing a moving upstream checkout is involved.
