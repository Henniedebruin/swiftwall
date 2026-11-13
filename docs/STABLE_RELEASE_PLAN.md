# SwiftWall 0.2.0 stable release plan — review draft

**Local implementation and local validation complete.** A native GNU
release binary has been built and reports `swiftwall 0.2.0`. No stable musl release
archive or checksum has been built or published. Further builds, commits,
pushes/CI runs, merges, tags, drafts, public releases and marketplace updates require
separate explicit approval. Production changes are not authorized.

## Recommendation

Use engine/plugin/bundle version **0.2.0**, with proposed tag **v0.2.0** and release
title **SwiftWall v0.2.0**. Rebuild from the approved stable source commit. Do not
rename or relabel the existing 0.2.0-dev executable/archive.

The validated development baseline is commit
`217a0969b8902b268ece1c224492ee25e2e8a676`. Its musl CI, downloaded-binary
shutdown/restart smoke, and packaged-CLI disposable-systemd acceptance passed.
That evidence supports preparation, not certification of the still-unbuilt stable
musl release artifact.
Previous development hashes are not the stable release's hashes.

At preparation time, GitHub returned not-found for both `refs/tags/v0.2.0` and the
release tagged v0.2.0. Recheck before any approved creation; never move an existing
tag or overwrite conflicting release assets.

## Local implementation and validation complete

All version/source/test preparation changes below are locally implemented and
local validation has passed, as recorded below. The native GNU
release binary reports `swiftwall 0.2.0`, but is not the planned musl release
archive. No stable musl archive or checksum has been built or published.

| Location | Local implementation / validation checkpoint |
| --- | --- |
| `Cargo.toml` package version | Locally changed from `0.2.0-dev` to `0.2.0`. |
| `Cargo.lock` SwiftWall root-package record | Root package version locally changed to `0.2.0`; dependency graph/checksums retained. No dependency update. |
| `manifest.json` version | Version locally changed to `0.2.0`; ID, schema, entrypoints, QML/helper code and structural settings retained. |
| `src/main.rs` `--version` | No source edit: `env!("CARGO_PKG_VERSION")` produces `swiftwall 0.2.0`, confirmed by the native GNU release binary. |
| `src/protocol.rs` metadata | No source edit: the compiled package version is `daemon_version: "0.2.0"`. Keep protocol 1 and existing capabilities. |
| `scripts/release_bundle.py` | No format/allowlist change: existing equality checks require Cargo, plugin and executable versions to match. Generate new metadata from the stable commit. |
| `scripts/setup.py` / `setup_download.py` / `setup_support.py` | Main setup path already validates exact artifact/binary/manifest identity dynamically. Preserve acceptance of existing dev installations; do not globally replace prerelease test fixtures. |
| `scripts/deployment.py` | Local implementation replaces the legacy `healthy("0.2.0-dev", 1)` pin with an explicit validated target version tied to the verified artifact; local validation has passed. Preserve an explicit dev value for historical rehearsals. The shipped modern setup path uses `SetupManager`, not `Deployment.upgrade()`. |
| `scripts/rehearse_deployment.py` | The old hash-pinned fixture/version pair is retained as historical. If testing stable with this legacy tool, supply a separate verified stable target version/hash rather than replacing the historical hash blindly. |
| `scripts/test_release_candidate.py` | The local adjustment derives the real-checkout packaging regression version from Cargo metadata, matching the real manifest. Generated dev fixtures remain as prerelease coverage; full Python discovery passed. |
| Other Python/QML version fixtures | Deliberate 0.2.0-dev compatibility cases and explicit stable 0.2.0 handshake, bundle, metadata-update and rollback coverage are retained in the local changes. A fixture's dev value is not necessarily a release pin. |
| `.github/workflows/release.yml` | Versions already come from Cargo and are compared to the plugin/executable. Stable 0.2.0 satisfies the existing no-prerelease publication check. Do not enable publishing during candidate validation. |
| README / `docs/RELEASES.md` / `docs/SETUP.md` | 0.2.0 download/update examples are prepared, planned availability is explicit, and development/schema examples remain labeled. |
| `docs/MARKETPLACE.md` | Documentation now identifies the existing listing, marks the old submission narrative historical, and links the planned prebuilt setup guide. Do not claim a new stable snapshot is marketplace-verified until maintainers actually verify it. |

The setup helper has no separate embedded semantic version or self-update channel.
Its release identity is the enclosing `release.json` plus the inventory hashes of
its Python files. `swiftwall-setup --version` is a target-release option, not the
helper's own version display. No new helper-version mechanism is needed here.

## Compatibility and stable-readiness assessment

The supported first-release path has passed candidate acceptance:

- The prebuilt engine needs no Rust compiler at installation/runtime. The packaged
  CLI was exercised without Rust/Cargo in its PATH, using real disposable services.
- Configuration is validated and created before service activation.
- Existing owned installations retain configuration, carousel history/bag/cursor,
  pause and autostart. Injected failure recovery restored the original engine/data.
- Both engine and plugin installers verify ownership; modified/unowned files are
  refused. Marketplace Git-managed plugin directories are not silently adopted.
- Manifest-only version/descriptive changes are supported when QML/helper bytes,
  modes and structural plugin settings stay unchanged.
- Changed QML/helper or structural-manifest updates refuse before mutation because
  live component caching has no proven safe general migration here.
- Default uninstall retains settings/state. Explicit data removal is bounded to
  known files; images/collection directories are never removed.
- Manual setup remains correct: marketplace installation does not provision the
  companion engine and user service. A prebuilt helper does not change that contract.

No new fatal carousel/installation defect was established by this preparation
review. The known update restrictions are acceptable only if prominently disclosed:
this is not a universal updater for future changed-QML releases.

### Gates still blocking publication

1. Obtain explicit approval for each commit and push/CI run of the reviewed stable
   source; agree the exact source identity and publication path.
2. Run the separately approved build-only musl workflow to produce a **new** stable
   musl archive/checksum, then independently accept the downloaded artifact against
   the provenance and packaged-installation requirements below. Completed local
   validation and native GNU smoke do not constitute new stable musl acceptance.
3. Separately approve each merge, tag, draft, public release and marketplace update.
   Recheck source/tag/asset identities at each boundary.
4. Before using the automated draft job, configure and verify environment
   protections. The prior repository review found **zero GitHub environments**, so
   no `release-draft` protection was configured. Remote protections still require
   verification; a YAML declaration alone does not require approval and may create
   an unprotected environment.

## Stable archive and provenance requirements

Expected release assets:

```text
swiftwall-0.2.0-linux-x86_64.tar.gz
SHA256SUMS
```

The archive root must be `swiftwall-0.2.0/`. With the unchanged allowlist, it contains
38 regular files including `release.json`: the engine, exact eight-file plugin,
setup/ownership/service helpers, unit template, generic example config, license and
documentation. No tests, private reports, configuration/state, images or backups.
Executable modes are 0755 for the engine, setup launcher and picker helper; other
payload files are 0644. `release.json` is excluded from its own file-hash mapping.

Require all of:

- Cargo package, lock root, plugin manifest, executable output and release metadata
  say 0.2.0; archive filename/root also match.
- `release.json.target` is `x86_64-unknown-linux-musl`.
- `release.json.source_commit` equals the exact approved source/tag commit.
- Every allowlisted path, file mode and SHA-256 matches the downloaded archive.
- ELF is static x86_64 with no interpreter; CI logs independently show the musl
  target/toolchain and locked build. Static ELF shape alone is not libc provenance.
- CI run ID, source SHA, toolchain versions and successful smoke are recorded.
- The downloaded outer GitHub artifact digest and inner archive checksum verify.
- Actual extracted launcher and binary pass independent local tests with private
  paths/fake setter, followed by actual packaged-CLI installation/update/recovery/
  uninstall in a disposable user-service environment.

Keep redacted smoke/state diagnostics as CI/review evidence, outside the public
release archive. Do not attach private local acceptance logs to the public release.
Checksums are same-origin integrity, not signatures; builds are not promised to be
byte-reproducible across rolling toolchains or runner images.

## Workflow and publication plan

Existing gates:

- Pushes to `dev/simplified-setup` build/test/upload CI artifacts only.
- Default workflow permissions are `contents: read`; checkout does not persist
  credentials. Repo-level defaults were also read-only per the prior review;
  verify remote settings before using the publication path.
- The draft job needs `workflow_dispatch` plus `publish=true`, depends on the build,
  requests `contents: write`, and requires an existing matching `vVERSION` tag.
- It creates a **draft**, never automatically publishes it. No marketplace deploy
  step exists. Do not dispatch this write-capable path without explicit approval.

Recommended sequence, with explicit later approval for each commit, push/CI run,
merge, tag, draft, public release and marketplace update (no bundled authorization):

1. **Local preparation (current):** version/source/test/coordinator changes and
   stable docs are locally implemented, and local validation is complete, including
   the full Python suite and native GNU daemon smoke. No live installation changes.
2. **Separate commit and push/CI approvals:** obtain approval before committing,
   then separately before pushing the reviewed stable source on the development
   branch (a push triggers CI); keep publishing disabled. Record its exact SHA, build and
   download the new 0.2.0 artifact, then repeat acceptance.
3. **Merge/tag approval:** approve the intended default-branch contents and tag
   target explicitly. Merge only when authorized. `v0.2.0` must point to the exact
   validated stable source commit, not automatically to an untested merge commit.
   A newly registered manual workflow requires its file on the default branch.
4. **Draft approval:** create a draft only after tag/source/asset identities have
   been rechecked. Prefer attaching the **exact already accepted assets** rather
   than silently replacing them with a new build. If using `publish=true`, the
   current workflow rebuilds; its new outputs are a new artifact identity and need
   their own independent checks before publication. Configure required reviewers
   and approved release-tag deployment restrictions before using that job.
5. **Public-release approval:** explicitly authorize publication of the reviewed
   draft/assets. Finalize planned-availability language and verify public download
   links and checksums afterward. No asset clobbering or tag rewriting.
6. **Marketplace-update approval:** separately request verification of the intended
   stable/default-branch snapshot, retaining Manual setup. Do not equate a GitHub
   tag/release with marketplace verification.

No step above grants permission to upgrade the current production installation.

## Proposed marketplace-facing copy

> **SwiftWall — automatic wallpapers with native Omarchy controls**
>
> Rotate JPEG, PNG and WebP wallpapers from your chosen local folders. Use the bar
> panel to pause/resume, go back, skip ahead, or change interval and selection mode.
> SwiftWall uses Omarchy's existing renderer and never modifies your original images.
>
> **Manual setup:** download and verify the Linux x86_64 prebuilt bundle from the
> SwiftWall GitHub release, extract it, and run `./swiftwall-setup install` as your
> normal desktop user. No Rust compiler or repository clone is needed. The wizard
> asks for local folders, offers a five-minute shuffle default, creates configuration
> before service start, and offers to enable the widget.
>
> Keep the extracted setup folder. Use `./swiftwall-setup status`,
> `./swiftwall-setup update --version 0.2.0` when updating a supported older owned
> installation, or `./swiftwall-setup uninstall`. Updating helper code/contracts
> requires obtaining the newer setup bundle. Uninstall preserves images and retains
> settings/state by default. Changed-QML updates and unowned/Git-managed plugin
> directories require deliberate manual migration.
>
> Requirements: Omarchy/Hyprland on Linux x86_64, Python 3.11+, systemd user tools,
> `busctl`, `ss`, `findmnt`, PyGObject/Gio and an XDG FileChooser portal backend.
> The marketplace installs plugins, not the required companion daemon/service;
> therefore Manual setup remains intentional.

This copy is for review. Its download wording must not be submitted as a claim of
stable availability before the release is actually published.

## Completed local validation and remaining artifact acceptance

Post-change local validation passed:

- Formatting and locked strict Clippy passed.
- Locked Rust tests passed: **106 tests** (77 unit, 4 config CLI, 14 daemon,
  11 v02).
- The locked native GNU release build passed; the binary reports `swiftwall 0.2.0`.
- Full Python discovery passed: **662 tests in 164.961 seconds**. The initial
  200-second bounded run was incomplete; the completed run supersedes it.
- Manifest validation passed, with an informational lint count of 41.
- Offscreen QML/widget runtime passed, with missing-theme `QColor` warnings and a
  deliberate disconnect reporting `peer-closed`. This is not live-shell acceptance.
- Native GNU release `daemon_smoke` passed against the actual versioned binary:
  `0.2.0`/protocol 1 hello, pause/resume, Next through a fake setter, durable flush
  and restart recovery. The smoke made no real background change, left
  configuration intact and exited 0.

These results complete local validation, **not new stable musl artifact
acceptance**. No stable musl release archive/checksum has been built or published.

Reference commands for local validation:

```sh
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
cargo build --release --locked
python3 -B -m unittest discover -s scripts -p 'test_*.py'
python3 -B scripts/validate_plugin.py
python3 -B scripts/test_widget.py --binary target/debug/swiftwall
```

Build the debug binary first if needed when rerunning the widget harness. The next
artifact gate requires separate Git/push/CI approval for the build-only musl workflow
and independent acceptance of its actual downloaded stable archive.
The 652-test candidate baseline is historical evidence; the completed local run is
662 Python tests. Include stable 0.2.0 identity, dev-to-stable metadata-only
migration, rejection of mixed versions, pause/history preservation, rollback,
changed-QML refusal, unowned/modified ownership refusal and image-safe uninstall.
Exercise the real-source packaged launcher regression rather than only generated
fixtures. Native bar/portal interaction remains outside an approval that excludes
the live shell; do not report it as newly tested.
