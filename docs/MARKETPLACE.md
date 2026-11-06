# Omarchy marketplace preparation

SwiftWall is being prepared for public distribution. It is **not listed or
approved** merely because this document exists. The current development manifest
is not a stable-release declaration.

Official references:
- <https://plugins.omarchy.org/publish.html>
- <https://github.com/omacom/omarchy-plugin-marketplace/blob/main/SUBMISSION.md>
- <https://github.com/omacom/omarchy-plugin-marketplace/blob/main/SECURITY.md>

## Repository and identity

- Repository: <https://github.com/Henniedebruin/swiftwall>
- Permanent plugin ID: `io.github.henniedebruin.swiftwall`
- License: [MIT](../LICENSE)
- Proposed category: `Appearance`
- Proposed tags: `bar`, `hyprland`, `quickshell`

Check ID availability against the current marketplace before submitting; names
from retired listings may remain reserved.

## Manual engine setup is required

SwiftWall is not just QML. The plugin connects to a separately built Rust daemon.
Installing the plugin files alone does **not** build, start or enable that daemon.
No install hooks silently execute source code or enable a service.

Follow the [engine installation guide](../packaging/INSTALL.md) and then the
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

## Publication gates

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

## Submission status

The owner approved the complete issue text and confirmed all five required
checklist statements. Submitted request:
<https://github.com/omacom/omarchy-plugin-marketplace/issues/14219>.

The repository is public, PR #1 is merged, and main-branch CI passed at
`00076fedaddc50fc4640d1648391333339c498e4`. This is still a `0.2.0-dev`
development snapshot. Submission is **pending marketplace validation and
maintainer approval**, not an approved listing or tagged stable release.
No preview asset was submitted. Manual engine setup and the unverified isolated
systemd coordinator rehearsal are disclosed in the issue.

## Submission template

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

## After submission

Keep one submission issue. Address validation feedback there rather than opening
repeated submissions. Approval is bound to the exact validated commit and requires
a marketplace maintainer's `approved-and-verified` decision. Later upstream commits
are not automatically covered by that snapshot's verification. Listing is not a
security audit, certification, warranty or endorsement.
