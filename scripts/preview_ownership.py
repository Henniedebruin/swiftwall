"""File-only ownership for explicitly approved, fresh-namespace previews.

API (all paths are explicit; no live home is inferred):
    install_preview(bundle, home, ownership_path) -> Path of published plugin
    verify_preview(home, ownership_path) -> Path of verified published plugin
    uninstall_preview(home, ownership_path) -> None

The bundle is a stage_preview-produced directory, optionally instrumented within
its eight files. ownership_path must not exist; its existing parent must be owned
by this UID and outside plugin discovery. Immutable 0600 intent records exact
bytes/modes, home, ID, target, and private recovery paths BEFORE publication.
An exception can leave intent and a private stage or published tree: inspect the
reported paths, then verify/uninstall with the same evidence path. Never retry an
install over that evidence. Uninstall also recovers a quarantined or unpublished
owned generation. No enablement, registration, subprocesses, or configuration
edits occur. Publication itself may trigger the plugin directory watcher.

Like plugin_support, this is cooperative local ownership, not a security boundary
against a hostile same-UID process. Home-directory flock serializes cooperating
callers; every rename refuses occupied destinations and every deletion is bounded
to verified payload paths (never recursive or forced).
"""

import base64
import json
import os
from pathlib import Path
import re
import stat
import tempfile

import plugin_support as support


PreviewError = support.InstallError
ID_PATTERN = re.compile(r"local\.swiftwall-preview-[0-9a-f]{32}")
WATCHED = Path(".config/omarchy/plugins")


def checkpoint(name):
    """Exception-injection seam; never executes external commands."""


def _path(value, directory=False):
    path = Path(os.path.expanduser(str(value)))
    if not path.is_absolute():
        path = Path.cwd() / path
    support.check_path(path, directory=directory)
    return Path(os.path.abspath(path))


def _outside(path, watched):
    if (path == watched or path.is_relative_to(watched)
            or "/omarchy/plugins" in str(path)
            or "/usr/share/omarchy" in str(path)):
        raise PreviewError(f"Evidence/staging must be outside watched plugins: {path}")


def _context(home, ownership_path):
    home = _path(home, directory=True)
    if not home.is_dir():
        raise PreviewError("An existing explicit home is required")
    state = _path(ownership_path)
    watched = home / WATCHED
    _outside(state, watched)
    _outside(state.parent, watched)
    if state == home / support.MANIFEST:
        raise PreviewError("Production ownership metadata is forbidden")
    _private_owner(state.parent)
    return home, state, watched


def _private_owner(path, private=False):
    support.check_path(path, directory=True)
    info = path.stat()
    if info.st_uid != os.getuid() or (private and stat.S_IMODE(info.st_mode) != 0o700):
        raise PreviewError(f"Caller-owned private recovery directory required: {path}")


def _generation(generation, identity):
    if not isinstance(identity, str) or ID_PATTERN.fullmatch(identity) is None:
        raise PreviewError("Only exact fresh preview IDs are accepted")
    if not isinstance(generation, dict) or set(generation) != set(support.FILES):
        raise PreviewError("Invalid preview eight-file allowlist")
    try:
        for name, item in generation.items():
            mode = 0o755 if name.endswith(".py") else 0o644
            if (not isinstance(item, dict) or set(item) != {"bytes", "sha256", "mode"}
                    or type(item["mode"]) is not int or item["mode"] != mode
                    or support.record(base64.b64decode(item["bytes"], validate=True), mode) != item):
                raise ValueError("bytes/hash/mode")
        manifest = json.loads(base64.b64decode(generation["manifest.json"]["bytes"]))
        if (not isinstance(manifest, dict)
                or type(manifest.get("schemaVersion")) is not int
                or manifest["schemaVersion"] != 1 or manifest.get("id") != identity
                or not isinstance(manifest.get("version"), str) or not manifest["version"]
                or manifest.get("kinds") != ["service", "bar-widget"]
                or manifest.get("entryPoints") != {
                    "service": "plugin/Service.qml", "barWidget": "plugin/BarWidget.qml"}):
            raise ValueError("preview manifest contract")
    except (ValueError, TypeError, KeyError) as exc:
        raise PreviewError("Invalid preview generation or manifest contract") from exc


def _same_fs(source, destination):
    if support.directory_device(source) != support.directory_device(destination):
        raise PreviewError("Preview stage and target are on different filesystems")


def _read_intent(home, state, watched):
    item = support.read_record(state)
    if item is None or item["mode"] != 0o600:
        raise PreviewError("Missing or modified preview ownership evidence (expected 0600)")
    try:
        value = json.loads(base64.b64decode(item["bytes"]))
        if (not isinstance(value, dict) or set(value) != {
                "version", "intent", "id", "home", "target", "workspace", "identity", "generation"}
                or type(value["version"]) is not int or value["version"] != 1
                or value["intent"] != "install-preview" or value["home"] != str(home)):
            raise ValueError("intent schema/home")
        _generation(value["generation"], value["id"])
        target = watched / value["id"]
        if value["target"] != str(target):
            raise ValueError("target")
        workspace = _path(value["workspace"], directory=True)
        if (str(workspace) != value["workspace"] or workspace.parent != state.parent
                or not workspace.name.startswith(".preview-ownership-")):
            raise ValueError("workspace")
        _outside(workspace, watched)
        _private_owner(workspace, private=True)
        if (not isinstance(value["identity"], list) or len(value["identity"]) != 2
                or any(type(n) is not int or n < 0 for n in value["identity"])):
            raise ValueError("tree identity")
        return value, item, target, workspace
    except (ValueError, TypeError, KeyError) as exc:
        raise PreviewError("Invalid preview ownership intent") from exc


def _verify(root, intent):
    support.verify_tree(root, intent["generation"])
    if list(support.tree_identity(root)) != intent["identity"]:
        raise PreviewError(f"Owned preview directory was replaced: {root}")


def install_preview(bundle, home, ownership_path):
    """Publish an exact eight-file bundle without replacing any path; return Path."""
    home, state, watched = _context(home, ownership_path)
    bundle = _path(bundle, directory=True)
    identity = bundle.name
    with support.locked(home):
        generation = {name: support.read_record(bundle / name) for name in support.FILES}
        _generation(generation, identity)
        support.verify_tree(bundle, generation)
        target = watched / identity
        support.check_path(target, directory=True)
        if target.exists() or state.exists():
            raise PreviewError("Refusing existing target or ownership evidence")
        _same_fs(state.parent, watched)
        created = []
        support.make_directory(watched, created)
        workspace = Path(tempfile.mkdtemp(prefix=".preview-ownership-", dir=state.parent))
        staged = workspace / "staged"
        try:
            staged.mkdir(mode=0o755)
            (staged / "plugin").mkdir(mode=0o755)
            for name, item in generation.items():
                support.write_record(staged / name, item)
            support.verify_tree(staged, generation)
            intent = {"version": 1, "intent": "install-preview", "id": identity,
                      "home": str(home), "target": str(target), "workspace": str(workspace),
                      "identity": list(support.tree_identity(staged)), "generation": generation}
            evidence = support.record((json.dumps(intent, sort_keys=True, indent=2) + "\n").encode(), 0o600)
            # write_record uses xb: never truncate existing, even empty evidence.
            support.check_path(state)
            support.write_record(state, evidence)
            checkpoint("intent-written")
            if support.read_record(state) != evidence:
                raise PreviewError("Ownership evidence changed before publication")
            _verify(staged, intent)
            _same_fs(staged, watched)
            support.rename_noreplace(staged, target)
            checkpoint("published")
            _verify(target, intent)
            if support.read_record(state) != evidence:
                raise PreviewError("Ownership evidence changed after publication")
            return target
        except Exception as exc:
            # Never roll back a published path: intent already permits recovery,
            # including when rename succeeded but its caller raised afterwards.
            raise PreviewError(f"Preview install failed: {exc}; target retained if published at "
                               f"{target}; recovery retained at {workspace}; evidence {state}") from exc


def verify_preview(home, ownership_path):
    """Verify immutable intent and the entire published tree; return its Path."""
    home, state, watched = _context(home, ownership_path)
    with support.locked(home, readonly=True):
        intent, evidence, target, workspace = _read_intent(home, state, watched)
        _verify(target, intent)
        if support.read_record(state) != evidence:
            raise PreviewError("Ownership evidence changed during verification")
        return target


def uninstall_preview(home, ownership_path):
    """Quarantine, reverify, then delete only the owned tree and its evidence.

    Also accepts retained private staged/quarantined generations after failure.
    Any refusal retains files and reports the recovery path; no force option.
    """
    home, state, watched = _context(home, ownership_path)
    with support.locked(home):
        intent, evidence, target, workspace = _read_intent(home, state, watched)
        quarantine, staged = workspace / "quarantine", workspace / "staged"
        retained = target
        try:
            # Ambiguous duplicate trees are never resolved by deleting either.
            candidates = [p for p in (target, quarantine, staged) if p.exists() or p.is_symlink()]
            if len(candidates) != 1:
                raise PreviewError("Missing or ambiguous owned preview generation")
            retained = candidates[0]
            _verify(retained, intent)
            if support.read_record(state) != evidence:
                raise PreviewError("Ownership evidence changed before quarantine")
            if retained != quarantine:
                _same_fs(retained, workspace)
                support.rename_noreplace(retained, quarantine)
                retained = quarantine
            checkpoint("quarantined")
            _verify(quarantine, intent)
            if support.read_record(state) != evidence:
                raise PreviewError("Ownership evidence changed before deletion")
            # Explicit unlink/rmdir only: an unexpected child is never removed.
            for name in support.FILES:
                if support.read_record(quarantine / name) != intent["generation"][name]:
                    raise PreviewError(f"Modified quarantined file: {quarantine / name}")
                (quarantine / name).unlink()
            (quarantine / "plugin").rmdir()
            quarantine.rmdir()
            workspace.rmdir()
            if support.read_record(state) != evidence:
                raise PreviewError("Ownership evidence changed after deletion")
            state.unlink()
        except Exception as exc:
            raise PreviewError(f"Preview uninstall refused/failed: {exc}; retained path {retained}; "
                               f"recovery directory {workspace}; evidence {state}") from exc
