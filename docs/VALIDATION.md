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
authorization plus `--allow-live-desktop`. Its default preflight still requires a
not-found, inactive production service, no user unit file and no SwiftWall process.
The virtual-pointer helper is acceptance tooling only, not a SwiftWall runtime
dependency. Private output is generated beneath ignored `artifacts/`.

An installed service requires the additional explicit `--allow-installed-service`
opt-in. For a later authorized run, after review, from the selected graphical
session:

```sh
python3 scripts/live_acceptance.py --allow-live-desktop --allow-installed-service
```

The parent launching this command must supply the selected session's
`XDG_RUNTIME_DIR`, `WAYLAND_DISPLAY` and `HYPRLAND_INSTANCE_SIGNATURE`; the Wayland
socket must exist, and Omarchy shell IPC and Hyprland queries must succeed. Supply
`DBUS_SESSION_BUS_ADDRESS` and any needed `XDG_CURRENT_DESKTOP`, `XDG_SESSION_TYPE`,
`DISPLAY` or `XAUTHORITY` from that same session as appropriate. A gateway without
these variables fails before any service/desktop command. An authorized parent
may obtain these selected graphical variables from the installed service's
environment before invocation. The harness neither imports its whole environment
nor guesses display values; never evaluate raw environment contents as shell code.

Before stopping anything, the harness records the exact unit ID, fragment/hash,
active/substate, enabled state, MainPID/start time, process cgroup, running binary
path/hash and the original wallpaper target/hash, theme and backup. It requires a
stable loaded unit with no drop-ins or pending daemon reload, and either one
verified managed SwiftWall process or an inactive unit without competitors.
Aliases, changed PIDs and unmanaged SwiftWall processes are refused. An independent
guardian must acknowledge the durable restoration record before the selected
unit is stopped. Only `systemctl --user stop/start swiftwall.service` lifecycle
operations are used; there is no disable, enable, unit edit or daemon reload.

The test executes the repository release binary with temporary config/state/runtime
paths and removes inherited `SWIFTWALL_*` overrides. It does not edit, replace or
restore installed binaries, configuration or carousel state. The installed
daemon's own normal shutdown/startup persistence remains part of its lifecycle.
Cleanup first stops the exact test child, restores and verifies the original
wallpaper, then restores the unit's original active state and checks enablement
is unchanged. An originally inactive service stays inactive; an active disabled
service is restarted without enabling it. Service definition/binary drift or
competitors block unsafe restoration and leave the guard armed with failure
evidence and the backup retained.

The guardian runs in a separate session and takes over on parent-pipe EOF, without
a timer racing the test. It shares a restoration lock with normal cleanup, waits
boundedly for in-flight service jobs, and recognizes an already completed restart
after interruption. A session lock prevents competing harnesses and remains held
by the guardian after parent exit. Existing armed reports are never overwritten;
original test failures remain reported even when cleanup also fails. Review
`result.json`, `guardian-restoration.json` (when used) and `restore-armed` before
claiming successful restoration. These safeguards cannot restore a lost desktop
session or recover from machine shutdown; failures retain recovery evidence.

This procedure exercises engine/CLI rotation, SwiftView interaction and wallpaper
restoration. It does **not** open or verify the native SwiftWall widget/settings
panel or native folder chooser. The new installed-service path is covered by
offline fake-runner regression tests; that is not evidence of a new live run.

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
