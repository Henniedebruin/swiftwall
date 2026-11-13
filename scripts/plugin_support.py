"""File-only plugin transactions; no engine, shell, registry, or process control.

The metadata is trusted local state, not protection against a hostile same-UID
process. flock serializes cooperating installers. Ordinary exceptions recover;
power loss between tree and metadata renames requires manual recovery.
"""

import base64
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

PLUGIN_ID = "io.github.henniedebruin.swiftwall"
DESTINATION = Path(".config/omarchy/plugins") / PLUGIN_ID
MANIFEST = Path(".local/state/swiftwall/plugin-install-manifest.json")
FILES = ("manifest.json", "plugin/BarWidget.qml", "plugin/FolderPicker.qml",
         "plugin/Panel.qml", "plugin/Service.qml", "plugin/SettingsDraft.qml",
         "plugin/SharedService.qml", "plugin/pick_folder.py")


class InstallError(Exception):
    pass


def absolute(path):
    # Check the original spelling before abspath can collapse symlink/.. pairs.
    candidate = Path(os.path.expanduser(str(path)))
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    check_path(candidate, directory=True)
    return Path(os.path.abspath(candidate))


def check_path(path, directory=False):
    """Inspect every ancestor without traversing a symlink or special file."""
    for part in reversed((path, *path.parents)):
        try:
            mode = part.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise InstallError(f"Refusing symlink: {part}")
        expected_dir = part != path or directory
        if not (stat.S_ISDIR(mode) if expected_dir else stat.S_ISREG(mode)):
            raise InstallError(f"Unexpected file type: {part}")


@contextmanager
def locked(home, readonly=False):
    check_path(home, directory=True)
    # Lock an existing inode: even dry-run/verify creates no lock file, and a
    # terminated process releases its lock automatically. Home must exist.
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH if readonly else fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def record(data, mode):
    return {"bytes": base64.b64encode(data).decode("ascii"),
            "sha256": hashlib.sha256(data).hexdigest(), "mode": mode}


def read_record(path):
    check_path(path)
    if not path.exists():
        return None
    return record(path.read_bytes(), stat.S_IMODE(path.stat().st_mode))


def validate_contract(data):
    try:
        manifest = json.loads(data)
        if (not isinstance(manifest, dict)
                or type(manifest.get("schemaVersion")) is not int
                or manifest["schemaVersion"] != 1
                or manifest.get("id") != PLUGIN_ID
                or not isinstance(manifest.get("version"), str)
                or not manifest["version"]
                or manifest.get("kinds") != ["service", "bar-widget"]
                or manifest.get("entryPoints") != {
                    "service": "plugin/Service.qml", "barWidget": "plugin/BarWidget.qml"}):
            raise InstallError("Source manifest does not match the SwiftWall plugin contract")
    except (ValueError, TypeError) as exc:
        raise InstallError("Invalid plugin manifest JSON") from exc


def validate_generation(generation):
    if not isinstance(generation, dict) or set(generation) != set(FILES):
        raise InstallError("Invalid ownership file allowlist")
    for item in generation.values():
        try:
            if not isinstance(item, dict) or set(item) != {"bytes", "sha256", "mode"}:
                raise ValueError("record fields")
            data = base64.b64decode(item["bytes"], validate=True)
            if (type(item["mode"]) is not int or item["mode"] not in (0o644, 0o755)
                    or record(data, item["mode"]) != item):
                raise ValueError("hash or mode")
        except (ValueError, TypeError) as exc:
            raise InstallError("Invalid ownership bytes, hash, or mode") from exc
    for name, item in generation.items():
        if item["mode"] != (0o755 if name.endswith(".py") else 0o644):
            raise InstallError("Unexpected payload mode")
    validate_contract(base64.b64decode(generation["manifest.json"]["bytes"]))


def source_generation(source):
    check_path(source, directory=True)
    result = {}
    for name in FILES:
        item = read_record(source / name)
        if item is None:
            raise InstallError(f"Missing source: {source / name}")
        data = base64.b64decode(item["bytes"])
        result[name] = record(data, 0o755 if name.endswith(".py") else 0o644)
    validate_generation(result)
    return result


def load_manifest(home):
    item = read_record(home / MANIFEST)
    if item is None:
        return None
    if item["mode"] != 0o600:
        raise InstallError("Modified ownership metadata mode (expected 0600)")
    try:
        value = json.loads(base64.b64decode(item["bytes"]))
        if (not isinstance(value, dict) or set(value) != {"version", "id", "current", "previous"}
                or type(value["version"]) is not int or value["version"] != 1
                or value["id"] != PLUGIN_ID):
            raise ValueError("metadata schema")
        validate_generation(value["current"])
        if value["previous"] is not None:
            validate_generation(value["previous"])
        return value
    except (ValueError, TypeError, KeyError) as exc:
        raise InstallError("Invalid plugin ownership metadata") from exc


def verify_owned(home, manifest):
    root = home / DESTINATION
    check_path(root, directory=True)
    if manifest is None:
        if root.exists():
            raise InstallError(f"Refusing unowned plugin directory: {root}")
        return
    verify_tree(root, manifest["current"])


def verify_tree(root, generation):
    check_path(root, directory=True)
    if not root.is_dir():
        raise InstallError("Owned plugin directory is missing")
    # Only descend into the one allowed directory, never an arbitrary subtree.
    for directory, expected in ((root, {"manifest.json", "plugin"}),
                                (root / "plugin", {Path(p).name for p in FILES[1:]})):
        check_path(directory, directory=True)
        if not directory.is_dir() or {p.name for p in directory.iterdir()} != expected:
            raise InstallError(f"Unknown or missing paths in owned plugin: {directory}")
    for name in FILES:
        if read_record(root / name) != generation[name]:
            raise InstallError(f"Refusing missing or modified owned file: {root / name}")


def make_directory(path, created):
    check_path(path, directory=True)
    if path.exists():
        return
    make_directory(path.parent, created)
    path.mkdir(mode=0o700)
    created.append(path)


def write_record(path, item):
    with path.open("xb") as stream:
        stream.write(base64.b64decode(item["bytes"]))
        stream.flush()
        os.fchmod(stream.fileno(), item["mode"])
        os.fsync(stream.fileno())


def metadata(current, previous):
    if current is None:
        return None
    return record((json.dumps({"version": 1, "id": PLUGIN_ID,
                              "current": current, "previous": previous},
                             sort_keys=True, indent=2) + "\n").encode(), 0o600)


def checkpoint(name):
    """Test seam for ordinary-exception recovery; never executes external code."""


def rename_noreplace(source, destination):
    """Atomically refuse any occupied destination, including an empty directory.

    No check-then-rename fallback: older kernels/libcs/filesystems fail closed.
    """
    check_path(source, directory=True)
    check_path(destination.parent, directory=True)
    try:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise InstallError("renameat2(RENAME_NOREPLACE) is required; no unsafe fallback") from exc
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                       ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        if error in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise InstallError("renameat2(RENAME_NOREPLACE) unsupported; refusing unsafe rename")
        raise OSError(error, os.strerror(error), str(destination))


def directory_device(path):
    """Device of an existing directory or its closest existing ancestor."""
    check_path(path, directory=True)
    while not path.exists():
        path = path.parent
    return path.stat().st_dev


def check_filesystem(home):
    if directory_device(home / MANIFEST.parent) != directory_device(home / DESTINATION):
        raise InstallError("Plugin staging and destination are on different filesystems; "
                           "cross-device tree renames are not supported")


def tree_identity(path):
    value = path.lstat()
    return value.st_dev, value.st_ino


def transaction(home, before, current, previous):
    root, state = home / DESTINATION, home / MANIFEST
    old_metadata = read_record(state)
    created = []
    workspace = None
    moved_old = placed_new = changed_state = False
    old_identity = new_identity = new_metadata = None
    try:
        make_directory(state.parent, created)
        make_directory(root.parent, created)
        workspace = Path(tempfile.mkdtemp(prefix=".plugin-transaction-", dir=state.parent))
        staged, backup = workspace / "new", workspace / "old"
        check_filesystem(home)
        if current is not None:
            staged.mkdir(mode=0o755)
            (staged / "plugin").mkdir(mode=0o755)
            for name, item in current.items():
                write_record(staged / name, item)
        new_metadata = metadata(current, previous)
        if new_metadata is not None:
            write_record(workspace / "metadata", new_metadata)
        checkpoint("staged")
        verify_owned(home, before)
        if read_record(state) != old_metadata:
            raise InstallError("Ownership metadata changed during transaction")
        if before is not None:
            old_identity = tree_identity(root)
            rename_noreplace(root, backup)
            moved_old = True
            verify_tree(backup, before["current"])
        checkpoint("old-moved")
        if current is not None:
            new_identity = tree_identity(staged)
            rename_noreplace(staged, root)
            placed_new = True
        checkpoint("new-placed")
        if new_metadata is None:
            state.unlink()
        else:
            os.replace(workspace / "metadata", state)
        changed_state = True
        checkpoint("metadata-written")
        if moved_old:
            assert before is not None
            verify_tree(backup, before["current"])
    except (Exception, KeyboardInterrupt) as exc:
        try:
            if workspace is not None:
                # Never move an unexpected replacement or delete additions in a
                # published tree during cleanup. Keep both sides for recovery.
                if placed_new:
                    verify_tree(root, current)
                    if tree_identity(root) != new_identity:
                        raise InstallError("Published plugin directory was replaced")
                if moved_old:
                    assert before is not None
                    verify_tree(workspace / "old", before["current"])
                    if tree_identity(workspace / "old") != old_identity:
                        raise InstallError("Backup plugin directory was replaced")
                if changed_state:
                    if read_record(state) != new_metadata:
                        raise InstallError("Published ownership metadata changed")
                    if old_metadata is None:
                        state.unlink()
                    else:
                        write_record(workspace / "restore-metadata", old_metadata)
                        os.replace(workspace / "restore-metadata", state)
                if placed_new:
                    rename_noreplace(root, workspace / "failed-new")
                if moved_old:
                    rename_noreplace(workspace / "old", root)
        except (Exception, KeyboardInterrupt) as recovery:  # noqa: BLE001 - retain backup on recovery failure
            # Keep the backup for manual recovery rather than deleting it.
            raise InstallError(f"Transaction failed: {exc}; recovery failed: {recovery}; "
                               f"backup retained at {workspace}") from exc
        if workspace is not None:
            shutil.rmtree(workspace)
        for path in reversed(created):
            try:
                path.rmdir()
            except OSError:
                pass
        raise InstallError(f"Transaction failed: {exc}; original files restored") from exc
    else:
        shutil.rmtree(workspace)


def run(operation, home, source=None, dry_run=False):
    home = absolute(home)
    with locked(home, readonly=dry_run or operation == "verify"):
        manifest = load_manifest(home)
        verify_owned(home, manifest)
        if operation == "verify":
            if manifest is None:
                raise InstallError("No owned plugin installation")
            return
        if operation == "install":
            current = source_generation(absolute(source))
            if manifest and current == manifest["current"]:
                return  # Preserve the previous generation on identical reinstall.
            previous = manifest["current"] if manifest else None
        elif operation in ("uninstall", "rollback"):
            if manifest is None:
                raise InstallError("No owned plugin installation")
            if operation == "rollback" and manifest["previous"] is None:
                raise InstallError("No previous plugin generation to roll back; use uninstall for removal")
            current = manifest["previous"] if operation == "rollback" else None
            previous = None
        else:
            raise InstallError(f"Unknown operation: {operation}")
        check_filesystem(home)
        if not dry_run:
            transaction(home, manifest, current, previous)
