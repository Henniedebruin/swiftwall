# SwiftWall v0.2.0

> Published GitHub Release notes for SwiftWall v0.2.0. This file is the reviewed
> source of the GitHub Release body; the release, tag and assets are recorded in
> [PUBLICATION_0.2.0.md](PUBLICATION_0.2.0.md).

## Automatic wallpapers, native Omarchy controls

SwiftWall is a lightweight native Rust wallpaper carousel for Omarchy. Choose
local image folders, then use the Omarchy bar panel to pause/resume rotation, go
back or skip ahead. SwiftWall uses Omarchy's existing wallpaper renderer and
never modifies your original images.

This first stable downloadable release includes a prebuilt **Linux x86_64 musl**
engine and a guided terminal setup wizard. **No Rust compiler, Cargo or repository
clone is needed.**

### Highlights

- Five-minute shuffle rotation by default, exhausting the shuffle bag before
  reshuffling and avoiding immediate repeats; sequential order is also available.
- JPEG, PNG and WebP from multiple nonrecursive local wallpaper folders.
- Native Previous, Pause/Resume and Next controls, optional bar countdown,
  interval/mode settings, folder drafts, Save and Revert.
- Native folder selection through a short-lived external portal helper.
- Current wallpaper preserved at startup; manual wallpaper changes reset the
  rotation timer without clearing an explicit pause.
- Durable carousel history, shuffle state and pause state.
- User-local installation with verified downloads, ownership checks, configuration
  validation before service start, bounded readiness checks and recovery backups.
- Uninstall preserves wallpaper images and retains settings/state by default.

## Downloads and verification

Download these two assets from this release:

- `swiftwall-0.2.0-linux-x86_64.tar.gz`
- `SHA256SUMS`

Archive SHA-256:

```text
9c85bfd0a963d8054faa64a276458c3f98f602e52021cc2ffb00225ca17305a4
```

Source commit: `2ba15cf7a9b727302997633dca764add81dbd958`.
Target: `x86_64-unknown-linux-musl`. Other architectures are not included.
Checksums provide integrity, not an independent publisher signature.

## Quick Install

1. Put the archive and `SHA256SUMS` in a new directory. Open a terminal there:

   ```sh
   sha256sum --check SHA256SUMS
   ```

   Continue only if the archive reports `OK`.

2. Extract the verified archive into your own non-shared directory:

   ```sh
   tar -xzf swiftwall-0.2.0-linux-x86_64.tar.gz
   cd swiftwall-0.2.0
   ./swiftwall-setup install
   ```

3. Enter one or more full local wallpaper-folder paths without shell quotes; a
   blank line finishes the list. Accept **300 seconds** and **Shuffle** for the
   defaults, or select Sequential. Choose whether to enable the widget, review
   the proposed changes and type `install` to confirm.
4. Open the panel and confirm countdown and controls when prompted.

Run as your normal logged-in Omarchy user, **not with sudo**. Keep the extracted
folder for maintenance. Setup checks dependencies; it does not install system
packages or upgrade Omarchy.

Requirements: Omarchy/Hyprland on Linux x86_64, Python 3.11+, systemd user services,
`busctl`, `ss`, `findmnt`, PyGObject/Gio (`python-gobject` on Arch), and a working
XDG Desktop Portal FileChooser backend. New network/GVFS/FUSE collections are not
supported. Select subfolders individually.

## Status, update and uninstall

From the retained setup folder:

```sh
./swiftwall-setup status
./swiftwall-setup uninstall
```

To upgrade a supported older owned installation, download and verify this bundle,
extract it, then run its helper:

```sh
./swiftwall-setup update --version 0.2.0 --dry-run
./swiftwall-setup update --version 0.2.0
```

Updates preserve wallpaper folders, settings, history, pause and existing service
enablement/running state. Dry-run makes no changes and does not download or execute
artifacts. Keep the portable helper: it is not installed into PATH or silently
self-updated. Obtain a newer verified bundle for future installer fixes.

Uninstall **never deletes wallpaper images**. Configuration/state remain unless
`uninstall --remove-data` is explicitly selected. That option removes only known
settings/state files; recovery backups and the extracted bundle remain separately.

## Known limitations and recovery

- **Changed QML/helper payloads or structural plugin updates are refused before
  mutation.** Identical payloads and compatible version/descriptive manifest-only
  updates are supported. There is no automatic Quickshell restart or universal
  changed-QML migration.
- Modified or unowned engine/plugin files are refused. Marketplace/Git-managed
  plugin directories are not adopted. Do not manage the same directory with both
  installers; inspect ownership and plan any migration deliberately.
- Ordinary setup failures attempt operation-scoped recovery. Cross-process
  rollback after interruption/power loss is manual using inspected private
  backups; there is no automatic cross-process `rollback` CLI. Never restore a
  stale wallpaper snapshot over the currently active selection.
- Uninterruptible filesystem I/O cannot be cancelled by a userspace timeout.
  Plugin-tree and ownership-metadata persistence are not one power-loss-atomic
  transaction.
- Setter success is not proof of visual rendering. No thumbnails, recursive
  discovery, per-monitor configuration or replacement renderer are included.
  The panel omits an aggregate image-count label; daemon status reports the count.

See the bundle's `docs/SETUP.md`, `docs/RELEASES.md` and packaging guides for
ownership, security and recovery details. Some bundled preparation-status text
predates candidate acceptance; these release notes provide the final acceptance
status. The accepted archive has not been rewritten to change those historical
statements.

## Validation and provenance

Built from the exact source commit above using locked Cargo dependencies:

- [Build-only musl CI](https://github.com/Henniedebruin/swiftwall/actions/runs/43480558465):
  106 Rust tests, release build, extracted-launcher and daemon smoke passed.
- [Offline CI](https://github.com/Henniedebruin/swiftwall/actions/runs/43480558392):
  662 Python tests, Rust checks/tests and Rust 1.89 MSRV check passed.
- Independent downloaded-artifact verification checked the 38-file inventory,
  hashes, permissions, exact source identity and actual binary execution.
- Disposable packaged-CLI tests exercised fresh setup, real isolated user-service
  startup, shutdown/restart persistence, owned dev-to-stable engine update,
  injected-failure recovery and image-safe uninstall with Rust/Cargo absent from
  the setup CLI's PATH.

The old-engine upgrade test used a fixture-owned receipt and generated isolated
service, not a recreation of a historical installer environment. Widget activation
was declined, and registry/bar and portal probing were simulated. **Native widget
activation, compositor focus and the native chooser were not retested with this
downloaded bundle.** Build provenance is recorded, but byte-reproducible builds
and independent artifact signatures are not claimed.

## Why the marketplace says Manual setup

The marketplace installs plugin files, not SwiftWall's mandatory companion daemon
and user service. The prebuilt setup wizard simplifies that separate step;
**Manual setup remains accurate**. Existing marketplace verification covers its
reviewed snapshot, not automatically this release. Any stable marketplace update
requires separate approval and maintainer verification.
