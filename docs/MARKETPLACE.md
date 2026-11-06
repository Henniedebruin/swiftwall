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

- [ ] Independent review of final hardening diff complete; no blocking findings.
- [ ] Offline Rust, Python, manifest/QML lint and runtime tests pass, with any
  unavailable checks or harness limitations explicitly documented.
- [ ] Fresh authorized native widget/chooser acceptance for the final revision.
- [ ] Supported service environment contract checked against a real isolated
  systemd manager; mocks are not proof of serialization compatibility.
- [ ] Claimed minimum Rust version checked or claim corrected.
- [ ] GitHub CI observed at the proposed commit, including optional skips.
- [ ] Tracked files and reachable Git history reviewed for private data/secrets.
- [x] Owner selected MIT licensing.
- [ ] Default branch contains the intended submission revision and metadata.
- [ ] Owner-authorized visibility change made only after required checks pass.
- [ ] Plugin ID checked for global availability.
- [ ] Owner reviewed the completed marketplace issue and confirmed all five
  ownership/consent/checklist statements before submission.

A preview image is optional. Add only an actual screenshot with permission to
publish it; do not fabricate screenshots or copy another plugin's artwork.

## Draft submission (not submitted)

Title: `[Plugin]: SwiftWall`

The following issue body preserves the marketplace headings. Boxes intentionally
remain unchecked until the owner confirms every statement; do not submit this
incomplete draft.

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
