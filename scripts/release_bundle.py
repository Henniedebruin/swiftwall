"""Build the allowlisted Linux release archive; never install or publish anything.

Python 3.11+; standard library only. See docs/RELEASES.md for the verifier contract.
"""

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import subprocess
import tarfile
import tempfile
import tomllib


TARGET = "x86_64-unknown-linux-musl"
ARCHIVE_PREFIX = "swiftwall-"  # Append VERSION + '/' for the single archive root.
PLUGIN_FILES = (
    "manifest.json", "plugin/BarWidget.qml", "plugin/FolderPicker.qml",
    "plugin/Panel.qml", "plugin/Service.qml", "plugin/SettingsDraft.qml",
    "plugin/SharedService.qml", "plugin/pick_folder.py",
)
SCRIPT_FILES = tuple("scripts/" + name + ".py" for name in (
    "setup", "setup_support", "setup_download", "install", "install_support",
    "uninstall", "plugin_install", "plugin_support", "plugin_activation",
    "service_control", "deployment", "evidence_support", "validate_plugin",
    "release_bundle",
))
SOURCE_FILES = (
    "swiftwall-setup", *SCRIPT_FILES, *PLUGIN_FILES,
    "packaging/swiftwall.service", "packaging/INSTALL.md",
    "packaging/PLUGIN_INSTALL.md", "config.example.toml", "LICENSE",
    "README.md", "docs/RELEASES.md", "docs/SETUP.md",
    "docs/MARKETPLACE_INSTALLATION.md", "docs/ADVANCED_INSTALL.md",
    "docs/V02.md", "docs/VALIDATION.md", "BENCHMARKS.md",
)
PAYLOAD_FILES = ("bin/swiftwall", *SOURCE_FILES)
EXECUTABLE_FILES = {"bin/swiftwall", "swiftwall-setup", "plugin/pick_folder.py"}
VERSION_PATTERN = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?"


class BundleError(ValueError):
    pass


def validate_version(version):
    if not isinstance(version, str) or re.fullmatch(VERSION_PATTERN, version) is None:
        raise BundleError("Version must be a safe major.minor.patch[-prerelease] value")
    return version


def archive_name(version):
    return f"{ARCHIVE_PREFIX}{validate_version(version)}-linux-x86_64.tar.gz"


def archive_root(version):
    return f"{ARCHIVE_PREFIX}{validate_version(version)}/"


def safe_path(name):
    path = PurePosixPath(name)
    if (not name or "\\" in name or path.is_absolute()
            or any(part in ("", ".", "..") for part in name.split("/"))
            or path.as_posix() != name):
        raise BundleError(f"Unsafe archive path: {name!r}")
    return name


def read_regular(path):
    """Reject symlinks in every component, including the supplied source root."""
    path = Path(path).absolute()
    for parent in reversed(path.parents):
        if not stat.S_ISDIR(parent.lstat().st_mode):
            raise BundleError(f"Source ancestor is not an unlinked directory: {parent}")
    if not stat.S_ISREG(path.lstat().st_mode):
        raise BundleError(f"Source is not a regular unlinked file: {path}")
    return path.read_bytes()


def validate_elf(data):
    """Check Linux-compatible static ELF64/x86_64 shape, not libc provenance.

    Inspect the exact bytes to be archived. No external tools or section-header
    parsing are needed; reject extended or excessive program-header counts.
    """
    if len(data) < 64:
        raise BundleError("Binary must have a complete ELF64 header")
    ident = data[:16]
    if (ident[:7] != b"\x7fELF\x02\x01\x01" or ident[7] not in (0, 3)
            or ident[8] != 0):
        raise BundleError("Binary must be little-endian ELF64 with System V/Linux ABI")
    (kind, machine, version, entry, phoff, _shoff, flags, ehsize,
     phentsize, phnum, _shentsize, _shnum, _shstrndx) = struct.unpack_from(
        "<HHIQQQIHHHHHH", data, 16)
    if kind not in (2, 3) or machine != 62 or version != 1 or flags != 0 or ehsize != 64:
        raise BundleError("Binary must be an x86_64 ELF executable or static PIE")
    if (phentsize != 56 or not 1 <= phnum <= 128 or phoff < 64
            or phoff > len(data) or phnum * phentsize > len(data) - phoff):
        raise BundleError("Invalid or excessive ELF program-header table")
    executable_entry = False
    for index in range(phnum):
        (segment, permissions, offset, address, _physical, filesz, memsz,
         alignment) = struct.unpack_from("<IIQQQQQQ", data, phoff + index * phentsize)
        if segment == 3:  # PT_INTERP: dynamically linked GNU and musl are both forbidden.
            raise BundleError("ELF PT_INTERP is forbidden; a static executable is required")
        if offset > len(data) or filesz > len(data) - offset:
            raise BundleError("ELF segment extends beyond binary bytes")
        if segment == 1:  # PT_LOAD
            if (filesz > memsz or address + memsz > 1 << 64
                    or (alignment > 1 and (alignment & (alignment - 1)
                                          or address % alignment != offset % alignment))):
                raise BundleError("Invalid ELF load segment size or alignment")
            if permissions & 1 and address <= entry < address + memsz:
                executable_entry = True
    if not executable_entry:
        raise BundleError("ELF entry point must lie in an executable load segment")


def binary_version(binary):
    # A file-backed output avoids unbounded in-memory capture from a bad executable.
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(
                [str(Path(binary).absolute()), "--version"], stdin=subprocess.DEVNULL,
                stdout=output, stderr=output, timeout=5, check=False, shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BundleError(f"Binary --version failed: {exc}") from exc
        output.seek(0)
        raw = output.read(4097)
    if result.returncode != 0 or len(raw) > 4096:
        raise BundleError("Binary --version failed or exceeded 4096 output bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise BundleError("Binary --version was not UTF-8") from exc
    match = re.fullmatch(r"swiftwall (" + VERSION_PATTERN + r")\n?", text)
    if match is None:
        raise BundleError("Expected binary --version output: swiftwall VERSION")
    return match.group(1)


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def build_bundle(source, binary, output, version, source_commit):
    """Return (archive, SHA256SUMS); fail closed on missing allowlisted inputs.

    release.json files maps relative payload paths to {sha256, mode}; it excludes
    release.json itself. Modes are JSON integers (420 = 0644, 493 = 0755).
    """
    validate_version(version)
    if re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
        raise BundleError("source_commit must be a full lowercase 40-hex Git SHA")
    source, binary, output = Path(source), Path(binary), Path(output)
    cargo = tomllib.loads(read_regular(source / "Cargo.toml").decode("utf-8"))
    manifest = json.loads(read_regular(source / "manifest.json"))
    if cargo["package"]["version"] != version or manifest["version"] != version:
        raise BundleError("Requested, Cargo, and plugin manifest versions must match")
    payload = {safe_path(name): read_regular(source / name) for name in SOURCE_FILES}
    payload["bin/swiftwall"] = read_regular(binary)
    validate_elf(payload["bin/swiftwall"])
    if binary_version(binary) != version:
        raise BundleError("Binary version must match Cargo and plugin manifest versions")
    files = {
        name: {"sha256": hashlib.sha256(data).hexdigest(),
               "mode": 0o755 if name in EXECUTABLE_FILES else 0o644}
        for name, data in sorted(payload.items())
    }
    payload["release.json"] = json_bytes({
        "schema": 1, "version": version, "target": TARGET,
        "source_commit": source_commit, "files": files,
    })
    output.mkdir(parents=True, exist_ok=True)
    archive = output / archive_name(version)
    checksums = output / "SHA256SUMS"
    if archive.exists() or archive.is_symlink() or checksums.exists() or checksums.is_symlink():
        raise BundleError("Refusing to overwrite existing release outputs")
    # Stage locally so failed compression cannot leave apparently complete assets.
    with tempfile.TemporaryDirectory(prefix=".release-", dir=output) as staging:
        staged = Path(staging) / archive.name
        with staged.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as tar:
                    for name, data in sorted(payload.items()):
                        info = tarfile.TarInfo(archive_root(version) + safe_path(name))
                        info.size = len(data)
                        info.mode = files[name]["mode"] if name in files else 0o644
                        info.uid = info.gid = info.mtime = 0
                        info.uname = info.gname = ""
                        tar.addfile(info, io.BytesIO(data))
        digest = hashlib.sha256(staged.read_bytes()).hexdigest()
        staged_sums = Path(staging) / "SHA256SUMS"
        staged_sums.write_text(f"{digest}  {archive.name}\n", encoding="ascii")
        staged.rename(archive)
        staged_sums.rename(checksums)
    return archive, checksums


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args(argv)
    try:
        paths = build_bundle(args.source, args.binary, args.output, args.version, args.source_commit)
    except (BundleError, OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"release bundle: {exc}\n")
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
