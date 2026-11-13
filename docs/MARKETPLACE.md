# Omarchy marketplace status and stable preparation

[SwiftWall is listed on the Omarchy Plugin Marketplace](https://plugins.omarchy.org/plugin.html?id=io.github.henniedebruin.swiftwall).
Its **Manual setup** label remains legitimate: the marketplace installs plugin
files, not the required companion daemon and user service. Listing verification
covers the exact reviewed snapshot, not every later upstream commit or release.

**Stable 0.2.0 is local preparation only.** Local implementation and local validation
are complete; see the [validation record](STABLE_RELEASE_PLAN.md#completed-local-validation-and-remaining-artifact-acceptance).
This is not new stable musl artifact acceptance. A native GNU release binary reports
`swiftwall 0.2.0`; no stable musl release archive or checksum has been built or
published.
The [planned prebuilt quick install](SETUP.md#planned-stable-020-quick-install)
will require no Rust compiler, Cargo or repository clone once approved assets are
published. It does not reclassify marketplace eligibility or establish verification
of the stable snapshot.

Official references:
- <https://plugins.omarchy.org/publish.html>
- <https://github.com/omacom/omarchy-plugin-marketplace/blob/main/SUBMISSION.md>
- <https://github.com/omacom/omarchy-plugin-marketplace/blob/main/SECURITY.md>

## Repository and identity

- Repository: <https://github.com/Henniedebruin/swiftwall>
- Permanent plugin ID: `io.github.henniedebruin.swiftwall`
- License: [MIT](../LICENSE)
- Submission category: `Appearance`
- Submission tags: `bar`, `hyprland`, `quickshell`

Keep the existing plugin identity and submission issue; do not open a duplicate
submission for stable 0.2.0.

## Manual engine setup is required

SwiftWall is not just QML. The plugin connects to a separately built Rust daemon.
Installing the plugin files alone does **not** build, start or enable that daemon.
No install hooks silently execute source code or enable a service.

See [setup and recovery](SETUP.md) for the planned prebuilt path. Source/manual
installation remains an advanced alternative: follow the
[engine installation guide](../packaging/INSTALL.md) and then the
[plugin installation guide](../packaging/PLUGIN_INSTALL.md). These cover
configuration, file ownership, activation, updates, removal and rollback.
The daemon requires Linux and the documented Omarchy wallpaper interface.
The optional folder picker needs Python 3, PyGObject/Gio and a working XDG Desktop
Portal FileChooser backend. Rust is required when building from source. See the
README for the supported image formats and collection limitations.

Marketplace reviewers should be told about the separate engine setup. The
repository contains installers and user-service management tooling; those are
expected review capabilities, not claims of a capability-free security scan.
Do not remove or disguise those capabilities to obtain a passing scan.

## Stable 0.2.0 boundaries and approval gates

- The helper refuses marketplace-owned/Git-managed plugin files; it does not adopt
  them. Do not use both managers on the same plugin directory. Inspect and plan a
  deliberate migration before changing ownership.
- Changed QML/helper payloads and structural plugin updates are blocked before
  mutation. Metadata-only compatible updates do not establish a safe changed-QML
  migration, and the helper does not restart Quickshell.
- Cross-process rollback is manual using inspected retained private backups; no
  automatic cross-process `rollback` CLI exists. Do not replay stale wallpaper state.
- Keep the extracted bundle helper for status/update/uninstall. It is not silently
  self-updated; download and verify a newer bundle for new helper code.
- Complete stable version/build/downloaded-artifact validation before publication;
  development-snapshot evidence is not stable-artifact acceptance.
- Require explicit later approval separately for each commit, push/CI run, merge,
  tag, draft, public release and marketplace update. Production changes are not
  authorized. Marketplace updates also need maintainer verification of the exact
  intended snapshot; a GitHub Release does not confer that verification.
- Branch pushes build CI artifacts only. Manual `publish=true` dispatch is a
  write-capable draft path, not public publication. Per the prior review, no
  protected environment was configured; the YAML declaration alone is not an
  approval gate. Configure and verify protections before using that path. See
  [release gates](RELEASES.md#workflow-provenance-and-release-review).

## Historical original-submission record

The checklist, pending status and template below describe the original submission,
not current listing status or newly completed stable 0.2.0 validation. They are
retained as history; do not treat their checked boxes as current release approval.

### Historical publication gates

- [x] Independent review of hardening and save-order fixes found no blockers.
- [x] Offline Rust, Python, manifest/QML lint and runtime tests passed; optional
  hosted skips and native/offscreen boundaries are documented.
- [x] Supervised native engine/SwiftView acceptance and widget/chooser open/cancel
  checks completed. The later save-order patch passed focused/offscreen tests;
  broad native chooser selection/focus/accessibility testing remains outside scope.
- [ ] Stricter deployment coordinator checked against a real isolated systemd
  manager. This remains unverified and is disclosed to marketplace reviewers;
  manual installed-service acceptance does not establish this separate contract.
- [x] Minimum Rust 1.89 checked against locked dependencies.
- [x] GitHub CI observed at the reviewed commit, including optional skips.
- [x] Reachable Git history and staged changes secret-scanned; private raw evidence
  stays ignored and no preview asset is submitted.
- [x] Owner selected MIT licensing.
- [x] Default branch contains the intended submission payload and metadata.
- [x] Owner-authorized visibility change completed after applicable checks passed.
- [x] Plugin ID absent from current registry and no duplicate submission found.
- [x] Owner reviewed the completed marketplace issue and confirmed all five
  ownership/consent/checklist statements before submission.

A preview image is optional. Add only an actual screenshot with permission to
publish it; do not fabricate screenshots or copy another plugin's artwork.

### Historical submission status

The owner approved the complete issue text and confirmed all five required
checklist statements. Submitted request:
<https://github.com/omacom/omarchy-plugin-marketplace/issues/14219>.

The repository is public, PR #1 is merged, and main-branch CI passed at
`00076fedaddc50fc4640d1648391333339c498e4`. This is still a `0.2.0-dev`
development snapshot. Submission is **pending marketplace validation and
maintainer approval**, not an approved listing or tagged stable release.
No preview asset was submitted. Manual engine setup and the unverified isolated
systemd coordinator rehearsal are disclosed in the issue.

### Historical submission template

Title: `[Plugin]: SwiftWall`

The following is the reusable template; the approved submitted issue above is
the authoritative request. Do not open a duplicate submission. For future use,
confirm each checklist statement again before checking boxes.

### Repository URL

https://github.com/Henniedebruin/swiftwall

### Category

Appearance

### Tags

bar, hyprland, quickshell

### Suggest a missing tag

_No response_

### Maintainer notes

SwiftWall is a Linux wallpaper carousel with an Omarchy bar widget and settings
panel. Omarchy remains responsible for rendering wallpapers. The QML plugin
requires a separately built/configured Rust daemon and manual user-service setup;
plugin installation alone does not install or activate that daemon. Installation,
external dependencies, removal and rollback are documented in the repository.
Please review the manual setup requirements and installer/service-management
capabilities. Exact validation evidence and current limitations are recorded in
`docs/PUBLIC_READINESS.md`.

### Submission checklist

- [ ] The repository is public and contains installation and removal instructions.
- [ ] I have documented the plugin license and any external dependencies.
- [ ] I confirm that I own or have permission to submit this plugin and its preview assets.
- [ ] The plugin does not overwrite user configuration without explicit consent.
- [ ] I understand that approval is for listing and is not a security review.

## Ongoing marketplace verification

Keep one submission issue. Address validation feedback there rather than opening
repeated submissions. Approval is bound to the exact validated commit and requires
a marketplace maintainer's `approved-and-verified` decision. Later upstream commits
are not automatically covered by that snapshot's verification. Listing is not a
security audit, certification, warranty or endorsement.
