# SwiftWall

**Automatic wallpapers, with native controls in your Omarchy bar.** Choose local
image folders and SwiftWall rotates your wallpaper every five minutes. Pause,
resume, go back or skip ahead from the pop-out panel.

SwiftWall uses Omarchy's existing wallpaper renderer. It does not replace the
backend, modify original images, or require changes to SwiftView.

## Quick Install

**Release candidate preparation: no downloadable release has been published yet.**
Once an approved Linux x86_64 release is available:

1. Download its archive and `SHA256SUMS` from
   [GitHub Releases](https://github.com/Henniedebruin/swiftwall/releases) into a new folder.
2. Open a terminal there and verify the archive; continue only if it reports `OK`:
   ```sh
   sha256sum --check SHA256SUMS
   ```
3. Extract the archive with your file manager into your own, non-shared directory.
   Open a terminal inside the extracted folder and run:
   ```sh
   ./swiftwall-setup install
   ```

The wizard asks for wallpaper folders, offers five-minute shuffle rotation, and
asks before making changes. **No Rust compiler, Cargo or repository clone is needed.**
Run as your normal Omarchy user, not with sudo. Keep the extracted folder for updates
and removal. Requirements: Python 3.11+, systemd/Omarchy tools, PyGObject/Gio and a
working XDG folder chooser. See [full setup and recovery instructions](docs/SETUP.md).

Until a release is approved, [building from source](docs/ADVANCED_INSTALL.md) remains
an advanced alternative—not a requirement for the planned prebuilt installation.

## Everyday use

Click the SwiftWall bar icon to open its controller:

- Previous, Pause/Resume and Next.
- Optional countdown in the bar.
- Add local folders using a native chooser; remove folders from settings without
  deleting images.
- Change the interval or choose Shuffle/Sequential.
- **Save** to apply a draft; **Revert** to discard it.

JPEG, PNG and WebP are supported. Folders are nonrecursive. Shuffle exhausts a bag
before reshuffling and avoids immediate repeats. Manual wallpaper changes reset
the countdown while preserving an explicit pause. Startup preserves the current
wallpaper and starts a full interval. One wallpaper is shared across monitors.
The panel currently omits a total-image-count label; status reports the count.

## Update or remove

From the retained setup folder:

```sh
./swiftwall-setup status
./swiftwall-setup uninstall
```

Updates select an exact published version:

```text
./swiftwall-setup update --version X.Y.Z --dry-run
./swiftwall-setup update --version X.Y.Z
```

Replace `X.Y.Z` with the desired release version. Dry-run makes no changes and does
not download or execute artifacts. Normal operations require confirmation. Updates
preserve folders, settings, history, pause and existing autostart state. Uninstall
retains configuration/state by default and **never deletes wallpaper images**.
Use `uninstall --remove-data` only to explicitly remove the known SwiftWall settings
and state files; private recovery backups are retained separately.

**Initial update limitation:** an installed plugin with different QML payloads is
refused before mutation because live-shell component caching makes blind replacement
unsafe. Identical plugin payloads are left untouched during engine updates; version and
descriptive manifest metadata can update when all QML/helper code is unchanged. No
automatic Quickshell restart is attempted. See [setup limitations](docs/SETUP.md).

## Requirements and safety

- Omarchy/Hyprland on Linux x86_64 with its normal graphical-session environment.
- Python 3.11+, systemd user services, `busctl`, `ss`, `findmnt`.
- For the native folder chooser: PyGObject/Gio (`python-gobject` on Arch) and a
  working XDG Desktop Portal FileChooser backend.
- Existing local wallpaper folders. New network/GVFS/FUSE roots are not supported.

The setup tool installs nothing system-wide, uses no `curl | bash`, checks exact
release checksums and payload inventories, refuses modified/unowned files, creates
verified backups, and uses bounded service/activation readiness checks. Ordinary
failures attempt one recovery. It never silently adopts another installer's plugin
files. Whole-process interruption and power loss can still require manual recovery.

Checksums provide integrity, not an independent publisher signature. Build provenance
and release approval are described in [RELEASES.md](docs/RELEASES.md). The daemon
remains a small native Rust utility; Python setup/picker helpers run only on demand.

## Omarchy Marketplace

[SwiftWall is listed on the Omarchy Plugin Marketplace](https://plugins.omarchy.org/plugin.html?id=io.github.henniedebruin.swiftwall).
Its **Manual setup** label remains appropriate: the marketplace installs a plugin,
not the mandatory Rust daemon and user service. The release helper simplifies that
separate step; it does not change official eligibility. Do not install the same
plugin directory through both Omarchy's Git manager and SwiftWall setup. See the
[marketplace contract review](docs/MARKETPLACE_INSTALLATION.md).

## Development and troubleshooting

- [Setup, updates, uninstall and recovery](docs/SETUP.md)
- [Advanced installation, Cargo and CLI/IPC](docs/ADVANCED_INSTALL.md)
- [v0.2 architecture and limitations](docs/V02.md)
- [Engine file ownership and rollback](packaging/INSTALL.md)
- [Plugin file ownership and rollback](packaging/PLUGIN_INSTALL.md)
- [Historical validation](docs/VALIDATION.md) and [benchmarks](BENCHMARKS.md)

No thumbnails, recursive discovery, per-monitor configuration or replacement
renderer are included. Machine-specific configuration, state, deployment reports,
wallpapers, backups and screenshots are excluded from version control.
