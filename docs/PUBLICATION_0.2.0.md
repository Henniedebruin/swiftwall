# SwiftWall 0.2.0 — publication record

**Completed. The accepted source is tagged, the GitHub Release is published, and
its two assets are live.** Public release notes:
[RELEASE_0.2.0_DRAFT.md](RELEASE_0.2.0_DRAFT.md). This record supersedes the
pre-acceptance status in STABLE_RELEASE_PLAN.md; neither the accepted source nor
its archive was rewritten by preparing these external publication documents.

Publication outcome (recheck against GitHub before acting on any step below):

- `v0.2.0` annotated tag peels to `2ba15cf7a9b727302997633dca764add81dbd958`.
- GitHub Release `SwiftWall v0.2.0` is published (not a draft, not a prerelease).
- Exactly two assets are attached: `swiftwall-0.2.0-linux-x86_64.tar.gz` and
  `SHA256SUMS`, matching the accepted hashes below.
- The marketplace listing still carries **Manual setup** and its verified snapshot
  predates this release; a stable marketplace update needs its own approval and
  maintainer verification (see "Separate marketplace follow-up plan").

The sections below describe the confirmed publication steps as performed. Any
further mutation (asset replacement, new tag, marketplace submission) still needs
its own explicit approval.

## Accepted identity

- Source: `2ba15cf7a9b727302997633dca764add81dbd958`.
- Version/tag/title: `0.2.0` / `v0.2.0` / `SwiftWall v0.2.0`.
- Target: `x86_64-unknown-linux-musl`.
- Archive: `swiftwall-0.2.0-linux-x86_64.tar.gz`, 1,045,768 bytes.
- Archive SHA-256: `9c85bfd0a963d8054faa64a276458c3f98f602e52021cc2ffb00225ca17305a4`.
- `SHA256SUMS` file SHA-256: `484c83ad6db08a00fcda9bb7581c4d2c27fdbecdd0b7d16100e2e87cc6f5ab3c`.
- GitHub artifact ZIP SHA-256: `745b807843120927658e1d21ebc181974766f190c2af43c80febac1ae87f5866`.
- Engine SHA-256: `704a14bdbc29b084ffde68b7415ae948b0bad1eeb74d32db4ed764eb8b9fbeef`.
- Release workflow run: [43480558465](https://github.com/Henniedebruin/swiftwall/actions/runs/43480558465).
- Offline workflow run: [43480558392](https://github.com/Henniedebruin/swiftwall/actions/runs/43480558392).
- GitHub artifact: `14064029055`, `swiftwall-linux-x86_64`.

Publication-readiness review reconfirmed the archive and checksum bytes against
the accepted GitHub ZIP, all 38 regular files, exact allowlist, payload hashes and
permissions, schema 1, stable version, target and full source commit. Every bundled
source file matches that commit. Saved successful CI and independent acceptance
reports match the same source/archive. No binary execution or lifecycle test was
needed or repeated in this documentation-only review.

## Current Git/GitHub inspection

At review time (before publication):

- `main`: `3eecd56be284d88d14bd246324def16766edcde9`.
- `dev/simplified-setup`: accepted source above; four commits ahead, no divergence.
- Local and remote branch refs agree.
- No `v0.2.0` remote tag or GitHub Release existed.

State after publication:

- `v0.2.0` annotated tag peels to the accepted source SHA on the remote.
- GitHub Release `SwiftWall v0.2.0` is published (not a draft, not a prerelease)
  with exactly the two accepted assets.
- Repository is public; visibility unchanged.
- Zero GitHub environments; no `release-draft` reviewers configured.
- Workflow default permissions: read-only; no repository rulesets reported.
  This is not a statement that all possible legacy branch protections are absent.

Recheck immediately before each approved mutation. Stop if refs, assets, release
existence or protection assumptions differ; never overwrite a conflicting tag or
asset or force-push a branch.

## Recommended merge/tag strategy

Prefer an explicitly approved **fast-forward-only** of main to the exact accepted
commit, then create the annotated release tag on that SHA. This aligns default
branch installation/docs with the accepted source without creating an untested
merge commit. Do not squash, rebase or rewrite history. Do not include the new local
publication-doc edits in that fast-forward/tag target.

If repository policy forbids a fast-forward or main advances, do not improvise.
An explicitly approved tag directly on the accepted commit is also valid without
a merge. A merge/squash commit has a new source identity: do not tag it as the
accepted build. Build and independently accept any new proposed release source.

Local final-note/checklist edits are publication material outside the accepted
archive. Do not commit them into the release target and silently change the tag
identity. A later documentation-only commit needs its own approval. GitHub's
release notes can be supplied from a reviewed local file without changing source.

## Safe exact-asset promotion (separate approvals required)

GitHub does **not** require a rebuild to create a Release. Use manual exact-asset
promotion, not the current workflow's publish dispatch.

1. **Approve source alignment.** If still fast-forwardable, approve main advancing
   only to the accepted SHA. Verify remote main afterward. No mutation now.
2. **Approve annotated tag creation and tag-only push.** Tag `v0.2.0` must peel to
   `2ba15cf7a9b727302997633dca764add81dbd958`. For an annotated tag, distinguish the
   tag-object SHA from its peeled commit. Verify the remote tag and `^{}` target.
3. **Approve draft creation with exactly two assets.** Recheck accepted archive
   and checksum hashes. Read the reviewed public-notes body, omitting its review
   notice, into a local staging file. Do not edit/repack the archive or include
   private reports, smoke diagnostics, GitHub ZIP, generated images or backups.
4. Create the draft using explicit asset paths and `--verify-tag --draft`, never
   automatic tag creation or auto-generated release notes. Command shape below is
   illustrative, not authorization. Execute from a controlled staging directory
   containing the exact accepted assets and reviewed `release-notes.md`:

   ```sh
   gh release create v0.2.0 \
     swiftwall-0.2.0-linux-x86_64.tar.gz SHA256SUMS \
     --repo Henniedebruin/swiftwall \
     --verify-tag --target 2ba15cf7a9b727302997633dca764add81dbd958 \
     --draft --title 'SwiftWall v0.2.0' --notes-file release-notes.md
   ```

   `--target` is not a substitute for verifying the existing tag's peeled commit.
   Stop if a tag/release or conflicting assets already exist. Never use clobber,
   force, or delete/recreate to bypass an identity mismatch.
5. **Verify draft before publication.** Confirm draft=true, prerelease=false,
   correct tag target, exact title/notes and only the two intended assets. Download
   the uploaded draft assets to a fresh private directory and independently hash
   them against the accepted values. Review links, permissions and limitations.
   Release creation/upload is not one atomic operation: a partial draft remains a
   draft for inspection, not permission to publish or retry blindly.
6. **Obtain separate public-publication approval.** Publish only that inspected
   draft, without replacing assets. Verify public download bytes/checksums, tag,
   source link, architecture and installation instructions afterward. A failed
   verification stops follow-up; do not overwrite published artifacts automatically.
7. **Obtain separate marketplace-update approval.** Follow the plan below.

Archive promotion needs no Cargo, workflow dispatch, workflow write permission,
admin permission, repository-setting change or environment creation. A fine-grained
authorization needs repository Contents read/write for tag/release operations and
Metadata read; Actions read is sufficient for retrieving CI artifacts. Read-only
preflight uses no write permission. Existing login permissions are broader than
these operation requirements; no credentials/settings were changed.

## Existing workflow risk and alternative

`.github/workflows/release.yml` has default `contents: read`; branch pushes only
build/test/upload artifacts. The draft job requires `workflow_dispatch` with
`publish=true`, declares `release-draft`, and requests `contents: write`. The build
job requires a stable version and dispatch from the existing matching version tag.
The draft job creates a draft, not a public release, and has no marketplace step.

However, the declared environment currently has no configured protection. Its
name alone does not enforce human approval. Do not dispatch that write-capable
path under this plan. It also reruns the build using rolling toolchains and uploads
that new run's assets, not the accepted run's bytes. If this route is chosen later,
first obtain approval for environment configuration and verify reviewers/tag
restrictions, then obtain dispatch approval and independently accept all newly
built/downloaded bytes before publication. Same source does not imply same bytes.

## Separate marketplace follow-up plan

Only after stable publication and public asset verification, and under separate
approval:

1. Inspect current official update/verification requirements and the existing
   marketplace record/issue; do not open a duplicate initial submission.
2. Name the exact snapshot to verify. The v0.2.0 tag remains the accepted source;
   if default-branch documentation changes later, record its different SHA rather
   than implying that the binary was rebuilt from it.
3. Link the public `v0.2.0` release and checksum instructions. Explain the wizard's
   folder selection, default 300-second shuffle, optional sequential mode and
   widget enablement. Provide exact-version update and image-safe uninstall steps.
4. Retain plugin ID `io.github.henniedebruin.swiftwall` and **Manual setup**: the
   marketplace does not provision the mandatory companion engine/service. Do not
   claim a new snapshot verified until maintainers confirm it.
5. Warn that setup refuses marketplace/Git-managed plugin directories; users must
   not run both installers on the same directory. Any migration needs an explicit,
   ownership-safe plan preserving settings and unrelated widgets.
6. Request review through the supported existing update channel. Do not alter
   marketplace records or verification issues during this preparation task.

Proposed marketplace-facing summary, to submit only after publication:

> SwiftWall v0.2.0 adds a verified prebuilt Linux x86_64 download and guided setup,
> so no Cargo or repository clone is needed. Download the archive and SHA256SUMS
> from https://github.com/Henniedebruin/swiftwall/releases/tag/v0.2.0, verify the
> checksum, extract privately, and run `./swiftwall-setup install`. Choose local
> wallpaper folders, a rotation interval (default five minutes), Shuffle or
> Sequential, and whether to enable native bar controls. Keep the extracted helper
> for `status`, exact-version `update`, and `uninstall`; images are never deleted.
> Manual setup remains required for the companion daemon. Marketplace-managed
> plugin files are not automatically adopted; do not use both installers on the
> same directory.

## Remaining risks and limitations

- CI artifact retention is 14 days. Keep verified copies safely; promote the exact
  accepted assets before evidence expires, or re-establish provenance if necessary.
- Archive docs include pre-acceptance/planned-status wording. Preserve the accepted
  bytes and clarify this in the release notes. Repacking would create a different
  artifact requiring acceptance. Later repository docs may be updated separately.
- Checksums are same-origin integrity, not independent signatures. Provenance is
  commit/run/inventory evidence, not a byte-reproducibility or signed-attestation claim.
- Changed-QML updates remain refused; manual cross-process recovery and marketplace
  ownership boundaries remain disclosed. No native widget/portal/focus retest was
  performed with the accepted downloadable bundle.
- Owned dev-to-stable artifact acceptance seeds an owned receipt and private
  generated unit, not a historical installer environment.
- No current production upgrade or desktop changes are part of release publication.
