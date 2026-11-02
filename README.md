# SwiftWall

A small, Linux-only wallpaper carousel for Omarchy/Hyprland. SwiftWall selects
images; **Omarchy remains responsible for rendering them**. SwiftView is a
separate project and requires no changes.

**v0.1:** a resident, event-driven carousel with a five-minute default interval,
persistent shuffle bag, CLI controls and systemd user-service integration. Isolated
tests and authorized live Omarchy verification have passed. See
[docs/VALIDATION.md](docs/VALIDATION.md) for validation scope and
[packaging/INSTALL.md](packaging/INSTALL.md) for installation and rollback.
Machine-specific deployment reports and private acceptance evidence are excluded
from version control.

## Behavior

- Default interval: **300 seconds**; configurable in TOML.
- Random shuffle bag: exhaust eligible images before refilling, avoid immediate
  repeats across boundaries, and preserve the bag across clean restarts.
- An external wallpaper change **resets the countdown**, without pausing rotation
  or clearing a user pause. A manual selection that is the last remaining bag
  member counts as observed/consumed rather than stalling the bag.
- Startup adopts the existing wallpaper without setting anything and starts a
  full interval. A persisted pause or protective suspension is retained.
- `next` and `previous` work while user-paused. History is bounded to 128 successful
  SwiftWall selections. Previous/forward navigation intentionally revisits history
  without consuming fresh bag entries; previous does not wrap.
- One shared wallpaper across all monitors, using Omarchy's existing behavior.
- JPEG, PNG and WebP, case-insensitive extensions, multiple nonrecursive roots.
- Missing/unreadable folders produce warnings; usable folders continue working.
  Empty collections retain the current wallpaper. A one-image collection already
  displayed is a no-op, not a repeated setter invocation.
- No catch-up loop after suspend. The next completed application starts a fresh
  interval. Scheduling uses monotonic `Instant` deadlines, not wall-clock time.

## Build and configure

Requirements: Linux, Rust stable (MSRV 1.89), Omarchy exposing
`omarchy theme bg set <path>`, and its normal graphical-session environment.
Python 3 is needed only for packaging tests, file installation and benchmarks.

```sh
cargo build --release
```

Review `config.example.toml`. After approval to configure/install, place a copy at
`~/.config/swiftwall/config.toml` and set your real **local** collection roots:

```toml
interval_seconds = 300
selection = "shuffle"
folders = ["~/Pictures/Wallpapers", "/absolute/path/to/another/collection"]
```

Relative paths resolve against the configuration directory; `~/` is expanded.
Unknown fields, unsupported selection modes, empty folder lists and invalid
intervals are rejected. The supported interval range is 1–31,536,000 seconds.
Folders need not exist at startup. Nested folders must be listed explicitly.
Original images are never modified, copied, converted or overwritten by SwiftWall.

Configuration is user-authored; SwiftWall never rewrites it. Edit via your
editor's atomic save and invoke `reload`. Invalid reloads retain the prior
configuration. Membership selection is gated until the new configuration's scan
completes, so removed roots cannot supply stale selections.

## Commands

```text
swiftwall daemon
swiftwall next
swiftwall previous
swiftwall pause
swiftwall resume
swiftwall status
swiftwall rescan
swiftwall reload
```

Start the daemon manually **only after approving a live test**: although it does
not set a wallpaper at startup, its first deadline will change the wallpaper.
Clients never auto-launch another daemon. A lifetime-held file lock prevents
multiple daemons, including manual and service instances, from controlling rotation.

Commands return JSON. `next`/`previous` acknowledge an asynchronous application;
inspect `status` for completion. While one application is active, another is
rejected as busy instead of queued. `pause` does not undo an already-running
application. `resume` starts a full interval and clears protective suspension only
when the background link is readable and no application is running.

Status includes state, pause/suspension reason, observed/requested wallpaper,
interval, selection mode, eligible count, scan/application progress, remaining
seconds or why no change is scheduled, bounded warnings, last application result,
and visual verification (`unknown`).

**Persistence acknowledgement:** successful control commands acknowledge daemon
memory, not crash-durable storage. Before relying on saved state, poll status until
`persistence_pending` is false and `last_persistence_error` is null. Graceful
SIGTERM/SIGINT shutdown finishes an in-flight application and flushes pending
state while retaining the singleton lock. A persistence failure protectively
suspends rotation. A later successful write does not silently clear suspension.

## Omarchy integration and race safety

The verified Omarchy 4.0.4-1 interface resolves a regular-file path, replaces
`~/.local/state/omarchy/current/background`, and requests a background update via
`omarchy-shell -q background set`. SwiftWall executes:

```text
omarchy theme bg set <absolute-image-path>
```

Arguments are structured native OS arguments, never shell-interpolated. SwiftWall
never directly writes the Omarchy symlink, edits Hyprland configuration, starts a
replacement renderer, or changes the theme.

The daemon watches the symlink's **parent directory**, not the replaceable link
inode. It reconciles the link before automatic applications, again in worker
preflight, and after command completion, even on failure. Own target observations
are not classified as external changes. Existing external changes reset the timer;
brief missing-link gaps are debounced. A persistently absent/unreadable link or an
observed overlapping setter race causes a reported **protective suspension**.
Inspect the desktop, then explicitly `resume` when safe. Ordinary manual changes
do not trigger that suspension or change a user-requested pause.

Limitations of the current Omarchy interface:

- The link update happens before the renderer request, and quiet IPC can report
  success when the shell is unavailable. A matching link and command success are
  **not proof of visual display**. SwiftWall reports visual verification as unknown.
- There is no compare-and-swap, shared setter lock, provenance tag or visual
  acknowledgement. An external change overwritten between observations can go
  undetected, and an external application selecting the same path is not a
  detectable state change. SwiftWall cannot make unrelated setters transactional.
- A command timeout/failure may occur after the link or display changed. SwiftWall
  always reconciles and uses a full-interval backoff rather than an immediate retry.
- Timeout cleanup kills the child process group before reaping its reserved leader
  PID. Descendants that deliberately create a new session/process group can escape;
  this is cleanup, not sandboxing. Filesystem calls and uninterruptible kernel I/O
  cannot be given a hard userspace timeout.

## Collections and validation

Discovery canonicalizes and deduplicates paths, follows symlinks to regular files,
ignores subdirectories, and tolerates per-entry errors. Distinct hard-link paths
are distinct candidates. Native non-UTF-8 filenames are supported in execution and
state: JSON uses `{ "unix_bytes": [...] }` when a normal path string is impossible.
Status uses a lossy human-readable path representation.

A scan worker performs discovery and header inspection, keeping IPC responsive.
Validation reads at most 1 MiB of header input without decoding pixels, restricts
dimensions to 32,768 per side / 100 million pixels, and applies a 64 MiB decoder
allocation limit. Extremely large but otherwise valid image headers may be rejected.
Results, including invalid images, are cached by device/inode/size/modification time;
unchanged invalid files are not repeatedly decoded/validated on rescans. Replacing
or modifying a file invalidates its cache entry. Selected files are rechecked before
application; changes between validation and the renderer opening the path remain
possible because Omarchy accepts paths rather than open file descriptors.

Header validation **cannot prove full image integrity**. Corrupt pixel data may
still pass and fail inside Omarchy without an acknowledgement. SwiftWall does not
claim to detect those failures or decode full-resolution images to work around it.

Inotify drives debounced rescans, with at most one scan in flight and one coalesced
follow-up. Missing roots are watched through their nearest existing ancestor;
watches move down as directories reappear. Overflow triggers reconciliation.
There is no periodic scan. Changes to symlink targets outside watched roots, and
filesystems without reliable notifications, may require `rescan`. Network-mounted
and removable paths are best-effort: unavailability is reported, not repaired by
restoring a stale image. Prefer ordinary local collection folders for v0.1.

## Files and architecture

```text
src/main.rs         CLI entry point
src/config.rs       strict TOML configuration and XDG locations
src/daemon.rs       blocking readiness loop, IPC, workers and reconciliation
src/runtime.rs      singleton, inotify, signalfd and poll
src/collection.rs   discovery, fingerprints and bounded header validation
src/carousel.rs     shuffle bag and back/forward history
src/backend.rs      structured Omarchy execution and process-group cleanup
src/state.rs        versioned atomic JSON persistence
src/path_serde.rs   native Unix path persistence
packaging/          user service and installation/rollback guide
scripts/            file-only installer, tests and isolated benchmark
```

Runtime locations:

| Purpose | Default |
| --- | --- |
| Configuration | `$XDG_CONFIG_HOME/swiftwall/config.toml` (`~/.config` fallback) |
| State | `$XDG_STATE_HOME/swiftwall/state.json` (`~/.local/state` fallback) |
| IPC and singleton | `$XDG_RUNTIME_DIR/swiftwall/` (required session environment) |
| Omarchy observation | `~/.local/state/omarchy/current/background` (currently hardcoded by Omarchy) |

The private runtime directory is mode 0700. IPC is newline-framed, bounded to
4 KiB requests and 64 connected clients, with short read/write deadlines. One
blocking event loop sleeps in `poll`; there is no async runtime, GUI or renderer.
The scan and persistence workers block on channels when idle; apply workers exist
only during a command. State writes use unique same-directory temporary files,
file sync, atomic rename and directory sync. State has an 8 MiB limit; malformed
or unsupported state is preserved and startup fails with a diagnostic rather than
silently discarding a pause or history. Back up/move the bad state manually before
choosing to reconstruct it. Serialization/cloning costs and collection memory
scale with the number and length of paths; huge libraries are not constant-memory.

Direct dependencies:

- `serde` / `serde_json`: versioned state and JSON status/IPC.
- `toml`: human-editable configuration.
- `rand`: shuffle-bag randomization.
- `libc`: narrow Linux readiness, signal, notification and process-group APIs.
- `image`: bounded header inspection; only JPEG, PNG and WebP features enabled.

No shared SwiftView crate, recursive collections, favourites, categories, GUI,
per-monitor selection, or alternative rendering backend is included.

## Installation, autostart and rollback

See [packaging/INSTALL.md](packaging/INSTALL.md) for the full procedure and limits.
After installation approval, from this repository:

```sh
python3 scripts/install.py --binary target/release/swiftwall --dry-run
python3 scripts/install.py --binary target/release/swiftwall
```

These are **file-only** operations. They do not execute SwiftWall or invoke
systemctl. The installer refuses unowned/modified files and symlinked installation
paths, records one owned rollback generation, and restores original files on
ordinary transaction failures. It is not a power-loss journal; do not run installers
concurrently. Config/state survive uninstall. The installer uses fixed conventional
home-relative binary/unit/manifest paths, independently of runtime XDG overrides.

The unit is associated with `graphical-session.target` and restarts on failure.
The file-only installer does not enable autostart. Enable it only after reviewing
configuration, installation and live-test results:

```sh
systemctl --user daemon-reload
systemctl --user enable --now swiftwall.service
```

Before updating, rolling back or uninstalling an active service, explicitly stop
and disable it and verify success (`systemctl --user disable --now swiftwall.service`).
Stop a manually launched daemon separately. Restore the previous owned files with
`python3 scripts/install.py --rollback`, or remove unchanged owned files with
`python3 scripts/uninstall.py`. Refresh the user manager manually afterward when
needed. File rollback never restores running processes, wallpaper, or service
settings; reactivation is a separate decision.

## Validation and resource measurements

```sh
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
cargo build --release
python3 -m unittest discover -s scripts -p 'test_install.py' -v
python3 scripts/benchmark.py --binary target/release/swiftwall --seconds 10
```

Automated Rust tests and the normal Python unittest suite use temporary directories,
isolated runtime/background locations, and fake/mocked setters, never the real
Omarchy command. Run all offline Python checks with
`python3 -m unittest discover -s scripts -p 'test_*.py' -v`.
See [BENCHMARKS.md](BENCHMARKS.md) for the measured baseline and
[scripts/benchmark_README.md](scripts/benchmark_README.md) for reproducible details.
The separate `scripts/live_acceptance.py --allow-live-desktop` harness **does change
real wallpapers** and must not be run without fresh user approval. It uses
`scripts/live_pointer.c` for temporary native Wayland input, not as a runtime
SwiftWall dependency. Private live evidence is excluded under `artifacts/`.

For isolated tooling/tests, explicit environment overrides are available:
`SWIFTWALL_CONFIG`, `SWIFTWALL_STATE_DIR`, `SWIFTWALL_RUNTIME_DIR`,
`SWIFTWALL_BACKGROUND_LINK`, `SWIFTWALL_OMARCHY_COMMAND`. They affect only SwiftWall;
setting a fake background link while leaving the real Omarchy command selected
**does not isolate a test**. Always override both together, as the test suite and
benchmark do. Production needs none of these overrides.
