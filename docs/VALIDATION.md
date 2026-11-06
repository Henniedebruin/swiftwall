# Validation and acceptance scope

This page records the historical **v0.1 milestone**, not the current installation.
See [V02.md](V02.md) for the v0.2 widget, protocol, local-deployment boundaries and
current validation commands. Machine-specific acceptance reports are not versioned.
The separate [public-readiness review](PUBLIC_READINESS.md) records the current
hardening checks and sandbox blockers; historical live passes are not new evidence.

SwiftWall v0.1 has passed isolated automated tests, an explicitly authorized live
Omarchy acceptance test, and a production user-service deployment verification.
This document describes the technical coverage without including personal
collection paths, filenames, wallpaper hashes, host identifiers or screenshots.
Machine-specific reports and raw evidence are intentionally not versioned.

## Automated checks

Run from the repository root:

```sh
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
cargo build --release --locked
python3 -m unittest discover -s scripts -p 'test_*.py' -v
```

The initial milestone has 46 Rust unit tests, 14 daemon integration tests and
24 offline Python tests. They generate temporary image fixtures and use isolated
configuration, runtime and state locations. Subprocess tests use fake setters;
offline restoration tests mock every desktop command. These commands do not
install the application, enable a service or change the real wallpaper.

Coverage includes:

- Shuffle-bag exhaustion/refill, boundary repeat avoidance, manual-current-image
  handling, transactional selection, bounded previous/forward history and restart
  serialization.
- Shallow multi-root discovery, canonical deduplication, extension filtering,
  missing roots, repaired invalid files, bounded header validation and native
  non-UTF-8 filenames.
- Timer reset after external changes, preservation of user pause, own-change
  classification, protective suspension, concurrent IPC and fresh startup timing.
- Subprocess argument integrity, unavailable commands, nonzero exits, flooding,
  timeouts and descendant cleanup.
- Atomic state recovery, oversized/corrupt state, persistence failures and bounded
  scan/persistence work queues.
- File ownership, modified-file/symlink refusal, installer failure recovery,
  upgrade rollback, uninstall preservation and restoration-guardian decisions.

A read-only configuration/collection check reuses the production parser and
validation pipeline:

```sh
cargo run --release --example check -- /absolute/path/to/config.toml
```

It reports supported-extension candidates, canonical duplicates, eligible files,
rejections and discovery warnings. Its output contains local paths: keep reports
under ignored `artifacts/` rather than committing them.

## Controlled live acceptance

The authorized live test used an isolated 15-second configuration, a release
binary and an existing local collection. The original theme, exact background
link target, source hash and readability were recorded before any wallpaper
change. Cleanup used `finally` and an independent restoration guardian.

Verified behavior:

- No immediate change on daemon startup.
- Multiple distinct automatic selections, next/previous, pause and resume.
- Actual SwiftView **Set as Wallpaper** actions resetting the countdown while
  running and preserving an existing explicit pause.
- Correct status, graceful shutdown and persisted carousel state.
- No unexpected command or notification failures.
- Original wallpaper restored through Omarchy's public command, with exact link,
  source hash, unchanged theme and background screenshot comparisons checked.

The live harness is **not** part of the offline test suite. It changes actual
wallpapers, uses the installed desktop session and requires fresh explicit user
authorization plus `--allow-live-desktop`. It assumes an uninstalled/inactive
production service; do not invoke it against a running production carousel.
The virtual-pointer helper is acceptance tooling only, not a SwiftWall runtime
dependency. Private output is generated beneath ignored `artifacts/`.

## Production service verification

The approved production deployment used two nonrecursive local roots, a
300-second interval and the persistent shuffle bag. Verification included:

- Config parsing, complete candidate header validation and canonical deduplication
  before activation.
- Installer dry-run, installed/release binary hash match and systemd unit checks.
- Graphical-session target linkage, inherited Wayland/Omarchy environment and
  exactly one daemon matching systemd's MainPID.
- A real five-minute first rotation, another automatic rotation later, and
  distinct explicit selections.
- Next/previous while paused, resume, restart recovery without an immediate
  wallpaper replacement, and real SwiftView timer-reset/pause behavior.
- Complete source-file hash, size and modification-time comparisons, unchanged
  theme, and no unexpected journal errors or automatic failure restarts.
- Short enabled-idle samples showing no additional measured CPU time.

See [the installation guide](../packaging/INSTALL.md) for service lifecycle and
rollback procedures. Private production configuration, state, installer manifests
and deployment reports are not repository templates.

## Limits of the evidence

Live tests covered a single-monitor Omarchy environment. A disruptive fresh
logout/login, multi-monitor hotplug and real hardware suspend/resume were not
performed. Target enablement and the running service environment were verified
instead. Automated bag tests do not claim that an entire large production
collection was observed over many days.

Header inspection does not prove complete pixel integrity. Omarchy's command can
update its state link before its renderer request succeeds, and exposes no visual
success acknowledgement or compare-and-swap setter operation. Normal status
therefore retains `visually_verified: "unknown"`; acceptance screenshot evidence
is not a new runtime capability. Short idle measurements and observed timing
include accounting/polling limitations and are not universal performance guarantees.
See [the benchmark baseline](../BENCHMARKS.md) and [README](../README.md) for details.

## Repository privacy boundary

The Git milestone includes code, synthetic tests, dependency lockfile, generic
configuration and service templates, installers and technical documentation.
It excludes builds, real image assets, screenshots, recovery backups, logs,
generated state, ownership manifests, local configuration, environment/credential
files and machine-specific acceptance/deployment reports. Do not force-add ignored
files without a separate privacy review. Standard Git author metadata is managed
by Git configuration, not embedded in project documentation or configuration.
