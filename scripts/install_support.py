"""Stdlib-only, file-only installation transactions for Swiftwall.

The manifest is trusted local installer metadata, not a security boundary against
another process running as the same user. Never run install, rollback, or
uninstall concurrently. These operations never invoke a service manager; file
recovery cannot restore running processes or service settings.
"""

import base64
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

FILES = {
    "binary": Path(".local/bin/swiftwall"),
    "unit": Path(".config/systemd/user/swiftwall.service"),
}
MANIFEST = Path(".local/state/swiftwall/install-manifest.json")
UNIT_SOURCE = Path(__file__).resolve().parent.parent / "packaging/swiftwall.service"


class InstallError(Exception):
    pass


def home_path(value):
    return Path(os.path.abspath(os.path.expanduser(str(value))))


def check_path(home, path):
    """Reject symlinks, including dangling links and linked parent directories."""
    path.relative_to(home)
    for part in reversed((path, *path.parents)):
        if part.is_symlink():
            raise InstallError(f"Refusing symlink: {part}")
        if part != path and part.exists() and not part.is_dir():
            raise InstallError(f"Not a directory: {part}")
    if path.exists() and not path.is_file():
        raise InstallError(f"Not a regular file: {path}")


def record(data, mode):
    return {
        "data": base64.b64encode(data).decode("ascii"),
        "sha256": hashlib.sha256(data).hexdigest(),
        "mode": mode,
    }


def read_record(home, path):
    check_path(home, path)
    if not path.exists():
        return None
    return record(path.read_bytes(), stat.S_IMODE(path.stat().st_mode))


def validate_records(records):
    if not isinstance(records, dict) or set(records) != set(FILES):
        raise InstallError("Invalid manifest file set")
    for item in records.values():
        if not isinstance(item, dict) or set(item) != {"data", "sha256", "mode"}:
            raise InstallError("Invalid manifest record")
        try:
            data = base64.b64decode(item["data"], validate=True)
        except (ValueError, TypeError) as exc:
            raise InstallError("Invalid manifest data") from exc
        if (type(item["mode"]) is not int or not 0 <= item["mode"] <= 0o777
                or record(data, item["mode"]) != item):
            raise InstallError("Invalid manifest hash or mode")


def load_manifest(home):
    path = home / MANIFEST
    saved = read_record(home, path)
    if saved is None:
        return None
    try:
        manifest = json.loads(base64.b64decode(saved["data"]))
        if (not isinstance(manifest, dict)
                or set(manifest) != {"version", "current", "previous"}
                or manifest["version"] != 1):
            raise InstallError("Unsupported installation manifest")
        validate_records(manifest["current"])
        if manifest["previous"] is not None:
            validate_records(manifest["previous"])
        return manifest
    except (ValueError, TypeError, KeyError) as exc:
        raise InstallError(f"Invalid installation manifest: {path}") from exc


def verify_owned(home, manifest):
    for name, relative in FILES.items():
        actual = read_record(home, home / relative)
        expected = manifest["current"][name] if manifest else None
        if actual != expected:
            raise InstallError(f"Refusing unowned, missing, or modified path: {home / relative}")


def make_parents(home, path, created):
    check_path(home, path)
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        created.append(directory)


def atomic_write(path, item):
    """Replace a regular file without exposing partially written contents."""
    fd, temporary = tempfile.mkstemp(prefix=".swiftwall-install-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(base64.b64decode(item["data"]))
            stream.flush()
            os.fchmod(stream.fileno(), item["mode"])
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def transaction(home, changes, dry_run=False):
    """Roll back completed writes on ordinary exceptions (not power loss).

    Catch all ordinary exceptions so unexpected write failures also trigger
    recovery; collect individual restore failures to attempt every remaining path.
    """
    before = {path: read_record(home, path) for path in changes}
    if dry_run:
        return
    created = []
    attempted = []
    try:
        for path, item in changes.items():
            check_path(home, path)
            if read_record(home, path) != before[path]:
                raise InstallError(f"Path changed during transaction: {path}")
            attempted.append(path)
            if item is None:
                if path.exists():
                    path.unlink()
            else:
                make_parents(home, path, created)
                atomic_write(path, item)
    except Exception as exc:
        failures = []
        for path in reversed(attempted):
            try:
                check_path(home, path)
                if before[path] is None:
                    if path.exists():
                        path.unlink()
                else:
                    atomic_write(path, before[path])
            except Exception as restore_error:  # noqa: BLE001
                failures.append(f"{path}: {restore_error}")
        for directory in reversed(created):
            try:
                directory.rmdir()
            except OSError:
                pass  # Never remove unrelated files created in the meantime.
        suffix = f"; recovery failed: {'; '.join(failures)}" if failures else "; original files restored"
        raise InstallError(f"File transaction failed: {exc}{suffix}") from exc


def manifest_record(current, previous):
    data = json.dumps({"version": 1, "current": current, "previous": previous},
                      indent=2, sort_keys=True).encode() + b"\n"
    return record(data, 0o600)


def install(home, binary, dry_run=False):
    manifest = load_manifest(home)
    verify_owned(home, manifest)
    source = Path(binary).expanduser()
    if source.is_symlink() or not source.is_file():
        raise InstallError(f"Binary source must be a regular, non-symlink file: {source}")
    current = {
        "binary": record(source.read_bytes(), 0o755),
        "unit": record(UNIT_SOURCE.read_bytes(), 0o644),
    }
    # An identical reinstall must not erase the useful rollback generation.
    if manifest and current == manifest["current"]:
        return
    changes = {home / relative: current[name] for name, relative in FILES.items()}
    changes[home / MANIFEST] = manifest_record(current, manifest["current"] if manifest else None)
    transaction(home, changes, dry_run)


def rollback(home, dry_run=False):
    manifest = load_manifest(home)
    if manifest is None:
        raise InstallError("No installation to roll back")
    verify_owned(home, manifest)
    previous = manifest["previous"]
    if previous is None:
        raise InstallError("No previous owned version to restore; use uninstall.py to remove this installation")
    changes = {home / relative: previous[name] for name, relative in FILES.items()}
    changes[home / MANIFEST] = manifest_record(previous, None)
    transaction(home, changes, dry_run)


def uninstall(home, dry_run=False):
    manifest = load_manifest(home)
    verify_owned(home, manifest)
    if manifest is None:
        return
    changes = {home / relative: None for relative in FILES.values()}
    changes[home / MANIFEST] = None
    transaction(home, changes, dry_run)
