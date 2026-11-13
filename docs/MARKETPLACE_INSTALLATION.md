# Omarchy marketplace installation constraints for SwiftWall

Research date: **2026-11-12**. Official marketplace sources were read through the
GitHub API at commit
[`59a441ea47e700d2a45cc5773eb62f5caf5cfa39`](https://github.com/omacom/omarchy-plugin-marketplace/commit/59a441ea47e700d2a45cc5773eb62f5caf5cfa39).
Installed `/usr/share/omarchy/bin/omarchy-plugin-{add,update,enable,disable,remove,validate}`
were inspected as text, not executed. Local script observations below describe
that installed implementation; linked upstream scripts are reference locations,
not a claim that the local package matches a particular upstream commit.

This is installation-policy research, not a listing approval, security scan,
release certification, or live acceptance report. No plugin activation, service
change, installation, update, removal, commit, or push was performed.

## Conclusion for SwiftWall

**Recommendation based on the official criteria:** retain/request `manual-setup`
while the standard Omarchy command cannot provide a functioning SwiftWall because
its required Rust daemon must be separately built or installed. A disconnected
widget or a friendly missing-engine message does not supply wallpaper-carousel
functionality. Manual setup is not disqualification from listing or verification.

A release asset, package recipe, or setup helper can improve the separate setup
experience, but its mere existence does not make the standard plugin command
install the daemon. Eligibility must be assessed on a clean supported system,
not a machine where the engine was already installed.

## 1. Official manual-setup criteria and representation

The marketplace's own contributor policy says:

> Use a manual installation override only when that command cannot produce a
> functioning plugin, such as when a required native binary is absent or a
> mandatory build must run before enablement.

It explicitly excludes **optional feature setup, credentials, API configuration,
data sources, bar placement, and layout customization** as reasons to mark an
otherwise functioning plugin manual. Thus “has dependencies,” “contains an
installer,” and “requires choosing wallpaper folders” are not sufficient criteria
on their own. A missing mandatory engine is different. [S1]

Official implementation details: [S1–S3]

- A root community plugin normally gets
  `omarchy plugin add <repository-git-url> --enable`.
- A curated `plugins.<id>.installation` entry in marketplace `registry.json`
  contains exactly `mode: "manual"` and a nonempty user-facing `note`.
  This is **marketplace registry metadata**, not a new SwiftWall manifest field.
- A manual override produces `installAvailable: false`, an empty `installCommand`,
  the curated note, and the display status `Manual setup`. It must survive passed,
  failed, and unreachable upstream checks. Arbitrary installer commands are not
  published through this override. It applies to root plugin repositories.
- For initial approval, maintainers finalize setup labels, including adding or
  removing `manual-setup`, **before** applying `approved-and-verified` last.
  Approval maps that decision into the registry override; the current generated
  note is “This plugin requires additional setup before it can be enabled. Follow
  the upstream installation instructions.”
- Manual setup and verification are separate axes: exact-commit security evidence
  does not establish standard-installation readiness.

## 2. Schema and submission boundaries

**Official facts:** one public GitHub repository per new submission, with root
`manifest.json`, README, and license file; installation/removal instructions and
external dependencies must be documented. IDs are permanent, globally unique,
lowercase for community submissions, and outside reserved `omarchy.*`; retired
IDs remain unavailable. [S4]

The marketplace's executable manifest validator requires numeric
`schemaVersion: 1`, nonempty strings for `id`, `name`, `version`, `author`, and
`description`, supported nonempty `kinds`, and an `entryPoints` object with an
entry for every kind. Entry points must be existing safe relative files, and
plugin folders cannot contain symlinks. An optional manifest `license` must be a
nonempty string when supplied; the root license file is independently required.
This validator currently accepts only `left`, `center`, or `right` for
`barWidget.defaultSection`. The inspected installed validator accepts additional
sections, so local validation alone is not proof of marketplace compatibility.
SwiftWall currently uses `right`, which is within both sets. [S2, S4]

SwiftWall's root manifest declares `service` and `bar-widget` QML entry points.
The `service` kind is a shell component, **not** a request to install or enable a
systemd unit. See the official shell plugin reference. [S8]

## 3. No automatic install hooks or binary provisioning

**Observed installed behavior:** `omarchy plugin add` checks the Git URL, asks for
trust confirmation, clones into a staging directory, validates the plugin,
rejects duplicate IDs/existing target paths, moves the checkout into
`~/.config/omarchy/plugins/<id>/`, and requests a plugin rescan. With `--enable`
it then discovers/enables the plugin; interactive use without that flag can
still prompt to enable. `--yes` skips confirmation, not dependency installation.
[S9]

There is no install-hook phase, Cargo build, release-asset download, package
installation, engine provisioning, or systemd activation in that script.
`omarchy plugin update` fetches `origin HEAD`, offers a diff/confirmation,
fast-forwards, validates, and rescans after a successful change. It likewise
has no build/setup/update hook. A failed post-merge validation triggers
`git reset --hard ORIG_HEAD`. [S10]

**Important distinction:** this is an absence of lifecycle-hook support, **not an
official blanket ban on shipping native code or installers**. Enabled QML is
unsandboxed and can execute processes. Hiding installation or downloads inside
QML startup would be runtime code execution, not a supported installer hook or a
safe workaround. **Recommendation:** keep provisioning explicit, reviewable, and
separate from discovery/enablement. [S5, S9]

## 4. Releases, packages, helpers, and security eligibility

| Distribution choice | Effect under the inspected official rules |
| --- | --- |
| Git tag or GitHub Release with an engine asset | Does not change `add`/`update`: neither selects tags nor downloads release assets. Does not alone remove mandatory manual setup. |
| PKGBUILD/package or separately installed engine | Can provide an explicit engine installation path, but plugin add does not run a package manager. Package-manager/privilege evidence can require security review; package availability alone does not satisfy clean-system standard installation. |
| Explicit setup/build helper | Not called by Omarchy's lifecycle. Installer and submitted-repository build capabilities require review, not automatic rejection. Mandatory invocation before the plugin works still meets the manual criterion. |
| Executable native binary committed in the plugin checkout | Can change whether a required binary is present, but detected ELF/PE/Mach-O executable files produce `bundled-executable-binary` review capability; static scanning does not inspect their behavior. Architecture, compatibility, trust, and functional completeness still need assessment. |
| Optional folder-picker helper/dependencies | Optional setup alone is not a manual-setup reason if the core plugin functions without it. Disclose it and handle absence safely. |

**Official security facts:** `installer`, `package-manager`, `privilege`,
`remote-build`, `bundled-executable-binary`, `service-management`, and
`sudoers-modification` are review capabilities, not findings by themselves.
Execution/build sourced only from the submitted repository is not automatically
rejected; it produces `remote-build` review. External unpinned Git execution and
download-to-shell paths produce findings. The active selective policy allows
exact maintainer acceptance only of eligible evidence; dangerous passwordless
sudoers and privileged process control from predictable shared temporary state
remain selectively blocking. Incomplete scans fail closed. Do not conceal
capabilities or claim a predicted scan outcome as an actual result. [S5]

**Recommendation:** retain SwiftWall's file-only installers and explicit service
activation. Publish checksummed, versioned engine artifacts or a package if useful,
but describe them as improving manual provisioning, not as marketplace automation
or guaranteed approval. Review helper code and dependencies along with the plugin.

## 5. Supported activation and removal: separate ownership domains

The following are **future operator instructions, not commands executed during
this research**. They are not a sequence to run blindly.

### Plugin lifecycle

After reviewing/provisioning the required engine and configuration, use supported
plugin commands for discovery, enablement, placement, disablement, and removal:

```sh
omarchy plugin validate ~/.config/omarchy/plugins/io.github.henniedebruin.swiftwall
omarchy plugin list --json
omarchy plugin enable io.github.henniedebruin.swiftwall --section right
omarchy plugin disable io.github.henniedebruin.swiftwall
omarchy plugin remove io.github.henniedebruin.swiftwall
```

`enable` delegates to the shell's `enablePlugin`; `disable` delegates to
`setPluginEnabled`. No full `shell.json` replacement or shell restart is necessary
in those scripts. Removal checks enabled state and requests disablement first.
For a symlink it unlinks only the link; for a Git checkout it **recursively deletes
the checkout without a backup**; other directories are moved to timestamped hidden
backups. Preserve local work before removal. These operations do not uninstall
SwiftWall's separate engine, unit, configuration, or image collections. [S9–S13]

**Recommendation:** validate and review before enabling; inspect authoritative
plugin state after a failed/timed-out IPC rather than repeatedly enabling. A
listed/enabled flag alone does not prove that QML loaded or connected to the engine.

### Engine lifecycle

SwiftWall's [engine installation guide](../packaging/INSTALL.md) owns the binary,
user unit, and installer metadata. Its file installer does not execute the binary
or call systemctl. Only after explicit operator review, the documented activation
is `systemctl --user daemon-reload` followed by
`systemctl --user enable --now swiftwall.service`. Starting the engine can change
wallpapers through its normal operation.

For engine update/removal, that guide requires stopping/disabling the service
first, then the appropriate owned-file installer/uninstaller, and a manual
manager reload where needed. Plugin disable/removal is **not** engine shutdown;
engine shutdown is **not** plugin disablement. Preserve configuration, state, and
wallpapers unless their separate deletion is explicitly requested.

### Git-managed conflicts: choose one plugin-file owner

**Observed official-script behavior:** add refuses an already used ID or existing
path; it does not adopt/overwrite it. Update requires a checkout with a `.git`
directory and attempts `merge --ff-only`; it can refuse divergent history or
conflicting local changes. It is not a pristine-worktree guarantee or a
SwiftWall-owned transaction. Its validation rollback can hard-reset tracked
files. Remove deletes Git-managed trees. [S9, S10, S13]

**SwiftWall-specific facts:** the [file-only plugin installer](../packaging/PLUGIN_INSTALL.md)
uses an eight-file allowlist and a separate ownership manifest. It refuses an
existing unowned tree, unknown files/directories, changed owned bytes/modes, and
symlinks. A marketplace Git checkout is not its owned payload; `.git` is an
additional directory, not an ownership handoff token.

**Recommendation:** use either Omarchy's Git-managed plugin lifecycle **or**
SwiftWall's owned-file plugin lifecycle at the plugin destination, never both.
A separate engine installer can coexist because it owns different paths. Before
switching plugin managers, explicitly disable, preserve local changes, remove the
old installation with its actual owner, and resolve ownership metadata/conflicts
before installing through the other manager. Do not force-copy over a checkout,
delete `.git` to evade a check, use hard reset to discard unknown work, or remove
unrelated bar/configuration entries. No automatic migration is established here.

## 6. Verification does not pin installation or remove manual setup

**Official facts:** snapshot verification is bound to an exact full commit SHA.
Current add/update commands obtain mutable upstream HEAD and do not accept the
marketplace SHA. A newer observed commit produces `Update unverified`; a tag or
release does not bind the command to the reviewed snapshot. Review the installed
commit before enablement. Verification is not a security audit, endorsement,
warranty, or guarantee. [S4–S7]

For an existing manual listing, the official verification form includes
**“Verify the listed snapshot and enable standard installation.”** It requires
one listed root plugin with a valid manual override, the exact listed commit,
standard-installation acknowledgment, current passing compatibility, and an
authenticated write-authorized maintainer's `standard-installation-approved`
event. Security eligibility requires a passing baseline or an existing valid,
non-revoked **installer-only** maintainer review with matching fresh evidence;
findings/additional capabilities do not qualify for that exception. Publication
removes only the manual override, preserving the baseline/review. It changes
installation presentation, not mutable-HEAD semantics. [S1, S6, S7]

**Recommendation for SwiftWall:** do not request that transition merely because
a release/package/helper exists or a security review succeeded. First demonstrate
that the standard command genuinely yields a functioning plugin without mandatory
extra provisioning. No such demonstration or policy approval was made here.

## Exact official sources

Marketplace links are pinned to the inspected commit; all accessed 2026-11-12.

- **S1 — contributor policy/manual criteria:** <https://github.com/omacom/omarchy-plugin-marketplace/blob/59a441ea47e700d2a45cc5773eb62f5caf5cfa39/AGENTS.md#L140-L166>
- **S2 — manifest validation and install projection:** <https://github.com/omacom/omarchy-plugin-marketplace/blob/59a441ea47e700d2a45cc5773eb62f5caf5cfa39/scripts/build-catalog.mjs#L745-L858> and <https://github.com/omacom/omarchy-plugin-marketplace/blob/59a441ea47e700d2a45cc5773eb62f5caf5cfa39/scripts/build-catalog.mjs#L1382-L1422>
- **S3 — manual label mapping:** <https://github.com/omacom/omarchy-plugin-marketplace/blob/59a441ea47e700d2a45cc5773eb62f5caf5cfa39/scripts/approve-submission.mjs#L98-L128>
- **S4 — submission rules:** <https://github.com/omacom/omarchy-plugin-marketplace/blob/59a441ea47e700d2a45cc5773eb62f5caf5cfa39/SUBMISSION.md>
- **S5 — security scope, capabilities, findings, enforcement:** <https://github.com/omacom/omarchy-plugin-marketplace/blob/59a441ea47e700d2a45cc5773eb62f5caf5cfa39/SECURITY.md#automated-security-baseline>
- **S6 — verification and installation boundary:** <https://github.com/omacom/omarchy-plugin-marketplace/blob/59a441ea47e700d2a45cc5773eb62f5caf5cfa39/VERIFICATION.md#installation-boundary>
- **S7 — standard-installation implementation:** <https://github.com/omacom/omarchy-plugin-marketplace/blob/59a441ea47e700d2a45cc5773eb62f5caf5cfa39/scripts/plugin-verification.mjs> and <https://github.com/omacom/omarchy-plugin-marketplace/blob/59a441ea47e700d2a45cc5773eb62f5caf5cfa39/scripts/plugin-verification-request.mjs>
- **S8 — official Quattro plugin reference:** <https://github.com/omacom/omarchy/blob/quattro/shell/plugins/README.md>
- **S9 — add:** <https://github.com/omacom/omarchy/blob/quattro/bin/omarchy-plugin-add>
- **S10 — update:** <https://github.com/omacom/omarchy/blob/quattro/bin/omarchy-plugin-update>
- **S11 — enable:** <https://github.com/omacom/omarchy/blob/quattro/bin/omarchy-plugin-enable>
- **S12 — disable:** <https://github.com/omacom/omarchy/blob/quattro/bin/omarchy-plugin-disable>
- **S13 — remove:** <https://github.com/omacom/omarchy/blob/quattro/bin/omarchy-plugin-remove>
- **S14 — marketplace overview:** <https://github.com/omacom/omarchy-plugin-marketplace/blob/59a441ea47e700d2a45cc5773eb62f5caf5cfa39/README.md>
