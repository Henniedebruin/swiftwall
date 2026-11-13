# Advanced installation and development

Most users should use the [prebuilt setup guide](SETUP.md) once an approved release
exists. This guide is the working source-build fallback; it requires Rust stable
(MSRV 1.89), Python 3 and the normal Omarchy graphical-session environment.

## Build and test

```sh
cargo build --release --locked
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
python3 -B -m unittest discover -s scripts -p 'test_*.py'
python3 -B scripts/validate_plugin.py
cargo build
python3 -B scripts/test_widget.py --binary target/debug/swiftwall
```

Tests generate images and use private sockets/fake setters. The explicit native/live
harnesses are separate and require desktop-changing approval; do not use them as an
ordinary test command. Musl release CI and package provenance are documented in
[RELEASES.md](RELEASES.md).

## Configuration before service start

Review `config.example.toml` in the repository root. Place a configuration at
`~/.config/swiftwall/config.toml` before starting the daemon:

```toml
interval_seconds = 300
selection = "shuffle"
folders = ["~/Pictures/Wallpapers", "/absolute/path/to/another/collection"]
```

Use your own existing local roots. Discovery is nonrecursive; new network/GVFS/FUSE
roots are rejected by settings updates. Modes are `shuffle` and `sequential`.
Intervals range from 1 to 31,536,000 seconds. Unknown fields and empty folder lists
are invalid. Validate without requiring a running daemon:

```sh
target/release/swiftwall check-config
```

## File-only engine and plugin installation

These tools verify ownership and do not start services or enable widgets:

```sh
python3 -B scripts/install.py --binary target/release/swiftwall --dry-run
python3 -B scripts/install.py --binary target/release/swiftwall
python3 -B scripts/plugin_install.py install --dry-run
python3 -B scripts/plugin_install.py install
python3 -B scripts/plugin_install.py verify
```

Stop an existing daemon before replacing its binary. Do not disable autostart just
to update; preserve the original enabled/running state and keep a verified stopped
backup. Never run a second daemon. The supervised deployment APIs and simplified
helper provide bounded service/activation checks; file installation alone does not.

After reviewing the configuration and first-install effects:

```sh
systemctl --user daemon-reload
systemctl --user enable --now swiftwall.service
swiftwall status
omarchy plugin enable io.github.henniedebruin.swiftwall --section right --index 0
```

The last command changes the live bar. Do not repeat it automatically after a lost
reply. Query the actual registry, effective bar and persisted settings first. The
hardened controller uses a scoped 10-second IPC timeout and a 12-second outer bound.
No Quickshell restart is part of normal activation. Full file rollback/uninstall
contracts are in [INSTALL.md](../packaging/INSTALL.md) and
[PLUGIN_INSTALL.md](../packaging/PLUGIN_INSTALL.md).

## CLI, IPC and state

```sh
swiftwall status
swiftwall pause
swiftwall resume
swiftwall next
swiftwall previous
swiftwall settings
swiftwall rescan
swiftwall reload
swiftwall request '{"v":1,"id":"inspect","op":"hello"}'
```

Clients do not launch the daemon. `next`/`previous` acknowledge asynchronous work;
inspect status to verify completion. Status reports the eligible image count,
observed wallpaper, last result, errors, pause/protective suspension, and remaining
deadline. Visual verification remains `unknown`: Omarchy's command/link update is
not proof of successful rendering.

Defaults:

- Config: `$XDG_CONFIG_HOME/swiftwall/config.toml`, with `~/.config` fallback.
- State: `$XDG_STATE_HOME/swiftwall/state.json`, with `~/.local/state` fallback.
- Socket and singleton: `$XDG_RUNTIME_DIR/swiftwall/` (private directory).
- Omarchy observation: `~/.local/state/omarchy/current/background`.

The daemon has a blocking event-driven loop, monotonic scheduling, filesystem
notifications and bounded workers. Header validation avoids decoding full-resolution
images. Rust dependencies remain serde, serde_json, toml, rand, libc and image with
only JPEG/PNG/WebP features. No GUI framework, renderer or async runtime is added to
the engine. The optional QML controller shares Omarchy's shell.

Settings transactions require a matching revision and atomic TOML replacement.
Comments/formatting are not preserved by a widget Save. State retains schema-1
history/shuffle bag and explicit pause across compatible restarts. Manual wallpaper
changes reset the deadline without clearing a user pause. v0.1 rollback requires
shuffle-compatible TOML; do not replace the current wallpaper with an old backup.

For isolated tooling only, the engine supports `SWIFTWALL_CONFIG`,
`SWIFTWALL_STATE_DIR`, `SWIFTWALL_RUNTIME_DIR`, `SWIFTWALL_BACKGROUND_LINK` and
`SWIFTWALL_OMARCHY_COMMAND`. Always isolate both observation and setter: a fake link
with the real setter is not isolated. The simple production helper refuses these
test overrides. See [V02.md](V02.md) for detailed protocol, race and worker limits,
and [BENCHMARKS.md](../BENCHMARKS.md) for historical synthetic measurements.
