"""Offline release tests: generated text/ELF fixtures; no real installer or images."""

import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import release_bundle as bundle


VERSION = "0.2.0-dev"
COMMIT = "1234567890abcdef1234567890abcdef12345678"


class ReleaseBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="swiftwall-release-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for name in bundle.SOURCE_FILES:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"generated fixture for {name}\n")
        (self.source / "Cargo.toml").write_text(f'[package]\nversion = "{VERSION}"\n')
        (self.source / "manifest.json").write_text(json.dumps({"version": VERSION}))
        self.binary = self.root / "fixture-binary"
        self.set_binary(f"swiftwall {VERSION}\\n")
        self.output = self.root / "out"
        # Shell fixtures exercise version handling, not ELF compatibility.
        self.elf_check = patch.object(bundle, "validate_elf")
        self.elf_check.start()
        self.addCleanup(self.elf_check.stop)

    def set_binary(self, message, status=0):
        self.binary.write_text(f"#!/bin/sh\nprintf '{message}'\nexit {status}\n")
        self.binary.chmod(0o755)

    def build(self, **kwargs):
        args = dict(source=self.source, binary=self.binary, output=self.output,
                    version=VERSION, source_commit=COMMIT)
        args.update(kwargs)
        return bundle.build_bundle(**args)

    def test_exact_inventory_hashes_metadata_and_safe_members(self):
        archive, sums = self.build()
        self.assertEqual(archive.name, f"swiftwall-{VERSION}-linux-x86_64.tar.gz")
        self.assertEqual(sums.read_text(),
                         f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n")
        with tarfile.open(archive) as tar:
            members = tar.getmembers()
            expected = set(bundle.PAYLOAD_FILES) | {"release.json"}
            prefix = bundle.archive_root(VERSION)
            self.assertEqual([m.name for m in members], sorted(prefix + n for n in expected))
            records = json.load(tar.extractfile(prefix + "release.json"))
            self.assertEqual(set(records), {"schema", "version", "target", "source_commit", "files"})
            self.assertEqual(records["schema"], 1)
            self.assertEqual(records["target"], "x86_64-unknown-linux-musl")
            self.assertEqual(records["version"], VERSION)
            self.assertEqual(records["source_commit"], COMMIT)
            self.assertEqual(set(records["files"]), set(bundle.PAYLOAD_FILES))
            for member in members:
                self.assertTrue(member.isreg())
                self.assertEqual((member.uid, member.gid, member.mtime), (0, 0, 0))
                self.assertEqual((member.uname, member.gname, member.linkname), ("", "", ""))
                name = member.name.removeprefix(prefix)
                self.assertEqual(bundle.safe_path(name), name)
                mode = 0o755 if name in bundle.EXECUTABLE_FILES else 0o644
                self.assertEqual(member.mode, mode)
                if name != "release.json":
                    self.assertEqual(records["files"][name], {
                        "sha256": hashlib.sha256(tar.extractfile(member).read()).hexdigest(),
                        "mode": mode,
                    })

    def test_deterministic_despite_source_modes_mtimes_and_extra_files(self):
        first, sums = self.build()
        original = first.read_bytes(), sums.read_bytes()
        for name in bundle.SOURCE_FILES:
            (self.source / name).chmod(0o777)
            os.utime(self.source / name, (12345, 12345))
        (self.source / "secret.txt").write_text("never package this")
        second, other_sums = self.build(output=self.root / "other")
        self.assertEqual(original, (second.read_bytes(), other_sums.read_bytes()))
        self.assertEqual(first.read_bytes()[4:8], b"\0\0\0\0")  # gzip mtime
        self.assertEqual(first.read_bytes()[3] & 8, 0)  # no original filename

    def test_launcher_layout_can_import_setup_modules_from_unrelated_cwd(self):
        # Generated contract example, not a test of the separately owned real launcher.
        (self.source / "swiftwall-setup").write_text(
            "#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parent / 'scripts'))\n"
            "import setup\nsetup.main()\n")
        (self.source / "scripts/setup.py").write_text(
            "import setup_support, setup_download\n"
            "def main():\n    print(setup_support.VALUE + setup_download.VALUE)\n")
        (self.source / "scripts/setup_support.py").write_text("VALUE = 'fixture-'\n")
        (self.source / "scripts/setup_download.py").write_text("VALUE = 'ok'\n")
        archive, _ = self.build()
        destination = self.root / "unpacked"
        with tarfile.open(archive) as tar:
            # Extract only after checking the same regular-file/path contract as above.
            for member in tar.getmembers():
                self.assertTrue(member.isreg())
                bundle.safe_path(member.name)
                path = destination / member.name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(tar.extractfile(member).read())
                path.chmod(member.mode)
        launcher = destination / bundle.archive_root(VERSION) / "swiftwall-setup"
        result = subprocess.run([sys.executable, "-B", str(launcher)], cwd=self.root,
                                capture_output=True, text=True, timeout=5, check=True)
        self.assertEqual(result.stdout, "fixture-ok\n")

    def test_missing_allowlisted_file_fails_before_outputs(self):
        (self.source / "scripts/setup_download.py").unlink()
        with self.assertRaises(FileNotFoundError):
            self.build()
        self.assertFalse(self.output.exists())

    def test_source_symlink_rejected(self):
        path = self.source / "LICENSE"
        path.unlink()
        path.symlink_to(self.binary)
        with self.assertRaisesRegex(bundle.BundleError, "regular unlinked"):
            self.build()

    def test_source_ancestor_symlink_rejected(self):
        link = self.root / "linked-source"
        link.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(bundle.BundleError, "ancestor"):
            self.build(source=link)

    def test_binary_symlink_rejected_without_execution(self):
        link = self.root / "linked-binary"
        link.symlink_to(self.binary)
        with patch.object(bundle, "binary_version") as version:
            with self.assertRaises(bundle.BundleError):
                self.build(binary=link)
            version.assert_not_called()

    def test_special_file_rejected_without_reading(self):
        path = self.source / "LICENSE"
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises(bundle.BundleError):
            self.build()

    def test_version_mismatches(self):
        for path in ("Cargo.toml", "manifest.json"):
            with self.subTest(path=path):
                original = (self.source / path).read_text()
                (self.source / path).write_text(original.replace(VERSION, "0.1.0"))
                with self.assertRaisesRegex(bundle.BundleError, "versions must match"):
                    self.build()
                (self.source / path).write_text(original)
        self.set_binary("swiftwall 0.1.0\\n")
        with self.assertRaisesRegex(bundle.BundleError, "Binary version must match"):
            self.build()

    def test_bad_binary_output_and_exit_status(self):
        for message, status in (("wrong\\n", 0), ("swiftwall 0.2.0-dev\\nextra", 0),
                                ("swiftwall 0.2.0-dev\\n", 1), ("x" * 4097, 0)):
            with self.subTest(message=message[:40], status=status):
                self.set_binary(message, status)
                with self.assertRaises(bundle.BundleError):
                    self.build()
                self.assertFalse(self.output.exists())

    def test_binary_timeout_is_bounded_structured_and_reported(self):
        with patch.object(bundle.subprocess, "run", side_effect=subprocess.TimeoutExpired("fixture", 5)) as run:
            with self.assertRaisesRegex(bundle.BundleError, "Binary --version failed"):
                self.build()
            self.assertEqual(run.call_args.args[0], [str(self.binary), "--version"])
            self.assertEqual(run.call_args.kwargs["timeout"], 5)
            self.assertFalse(run.call_args.kwargs["shell"])

    def test_unsafe_versions_commits_and_paths(self):
        for version in ("../oops", "1/2/3", "v0.2.0", "0.2.0\n", "", "01.2.0"):
            with self.subTest(version=version), self.assertRaises(bundle.BundleError):
                self.build(version=version)
        for commit in ("main", "abc", "a" * 39, "G" * 40, COMMIT + "\n"):
            with self.subTest(commit=commit), self.assertRaises(bundle.BundleError):
                self.build(source_commit=commit)
        for path in ("/absolute", "../outside", "a/../b", "./a", "a//b", "a/", "a\\b", ""):
            with self.subTest(path=path), self.assertRaises(bundle.BundleError):
                bundle.safe_path(path)

    def test_refuse_existing_outputs(self):
        archive, sums = self.build()
        original = archive.read_bytes(), sums.read_bytes()
        with self.assertRaisesRegex(bundle.BundleError, "overwrite"):
            self.build()
        self.assertEqual(original, (archive.read_bytes(), sums.read_bytes()))

    def test_cli_success_and_stable_version(self):
        version = "1.2.3"
        (self.source / "Cargo.toml").write_text(f'[package]\nversion = "{version}"\n')
        (self.source / "manifest.json").write_text(json.dumps({"version": version}))
        self.set_binary(f"swiftwall {version}\\n")
        stdout = io.StringIO()
        with patch.object(sys, "stdout", stdout):
            result = bundle.main([
                "--source", str(self.source), "--binary", str(self.binary),
                "--output", str(self.output), "--version", version, "--source-commit", COMMIT])
        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue().splitlines(), [
            str(self.output / bundle.archive_name(version)), str(self.output / "SHA256SUMS")])
        self.assertTrue((self.output / bundle.archive_name(version)).is_file())

    def test_cli_failure_is_actionable(self):
        result = subprocess.run(
            [sys.executable, "-B", str(Path(bundle.__file__)),
             "--source", str(self.source), "--binary", str(self.binary),
             "--output", str(self.output), "--version", "../unsafe", "--source-commit", COMMIT],
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("release bundle:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_real_elf_guard_rejects_scripts_before_execution_or_output(self):
        self.elf_check.stop()
        for data in (self.binary.read_bytes(),
                     minimal_elf(interpreter="/lib64/ld-linux-x86-64.so.2")):
            self.binary.write_bytes(data)
            with patch.object(bundle, "binary_version") as version:
                with self.assertRaisesRegex(bundle.BundleError, "ELF"):
                    self.build()
                version.assert_not_called()
            self.assertFalse(self.output.exists())

    def test_plugin_contract_is_exactly_eight_files(self):
        self.assertEqual(len(bundle.PLUGIN_FILES), 8)
        self.assertIn("manifest.json", bundle.PLUGIN_FILES)
        self.assertEqual(len(set(bundle.PAYLOAD_FILES)), len(bundle.PAYLOAD_FILES))
        self.assertIn("scripts/release_bundle.py", bundle.SCRIPT_FILES)
        self.assertNotIn("scripts/release_bundle.py", bundle.EXECUTABLE_FILES)
        self.assertIn("docs/SETUP.md", bundle.SOURCE_FILES)
        self.assertIn("docs/MARKETPLACE_INSTALLATION.md", bundle.SOURCE_FILES)
        for name in ("docs/ADVANCED_INSTALL.md", "docs/V02.md", "docs/VALIDATION.md", "BENCHMARKS.md"):
            self.assertIn(name, bundle.SOURCE_FILES)
        self.assertFalse(any("test_" in name or "__pycache__" in name for name in bundle.PAYLOAD_FILES))


def minimal_elf(kind=2, abi=0, interpreter=None):
    """Non-executed ELF bytes with a bounded executable PT_LOAD segment."""
    count = 1 if interpreter is None else 2
    size = 64 + 56 * count + 1
    base = 0x400000 if kind == 2 else 0
    ident = b"\x7fELF\x02\x01\x01" + bytes([abi]) + bytes(8)
    header = ident + struct.pack("<HHIQQQIHHHHHH", kind, 62, 1,
                                 base + size - 1, 64, 0, 0, 64, 56, count, 0, 0, 0)
    loader = b"" if interpreter is None else interpreter.encode("ascii") + b"\0"
    load = struct.pack("<IIQQQQQQ", 1, 5, 0, base, 0, size + len(loader),
                       size + len(loader), 0x1000)
    interp = b"" if interpreter is None else struct.pack(
        "<IIQQQQQQ", 3, 4, size, base + size, 0, len(loader), len(loader), 1)
    return header + load + interp + b"\xc3" + loader


class ELFValidationTests(unittest.TestCase):
    def test_accepts_static_executable_and_static_pie_linux_compatible_abis(self):
        for kind in (2, 3):
            for abi in (0, 3):
                with self.subTest(kind=kind, abi=abi):
                    bundle.validate_elf(minimal_elf(kind, abi))

    def test_rejects_dynamic_gnu_and_musl_interpreters(self):
        for interpreter in ("/lib64/ld-linux-x86-64.so.2", "/lib/ld-musl-x86_64.so.1"):
            with self.subTest(interpreter=interpreter):
                with self.assertRaisesRegex(bundle.BundleError, "PT_INTERP"):
                    bundle.validate_elf(minimal_elf(interpreter=interpreter))

    def test_rejects_scripts_wrong_class_endianness_architecture_and_abi(self):
        good = minimal_elf()
        invalid = [b"#!/bin/sh\nprintf fixture\\n", good[:4] + b"\x01" + good[5:],
                   good[:5] + b"\x02" + good[6:], good[:7] + b"\x09" + good[8:]]
        for offset, fmt, value in ((18, "H", 183), (16, "H", 1), (20, "I", 0),
                                   (48, "I", 1), (52, "H", 63)):
            data = bytearray(good)
            struct.pack_into("<" + fmt, data, offset, value)
            invalid.append(data)
        for index, data in enumerate(invalid):
            with self.subTest(index=index), self.assertRaises(bundle.BundleError):
                bundle.validate_elf(data)

    def test_rejects_truncated_or_unbounded_program_headers(self):
        good = minimal_elf()
        invalid = [good[:n] for n in (0, 16, 63, 64, 119)]
        for offset, fmt, value in ((32, "Q", 0), (32, "Q", 2**64 - 1),
                                   (54, "H", 55), (56, "H", 0),
                                   (56, "H", 129), (56, "H", 65535)):
            data = bytearray(good)
            struct.pack_into("<" + fmt, data, offset, value)
            invalid.append(data)
        for index, data in enumerate(invalid):
            with self.subTest(index=index), self.assertRaises(bundle.BundleError):
                bundle.validate_elf(data)

    def test_rejects_malformed_segments_and_missing_executable_entry(self):
        good = minimal_elf()
        for offset, fmt, value in ((64, "I", 0), (68, "I", 4), (72, "Q", 2**64 - 1),
                                   (96, "Q", len(good) + 1), (104, "Q", 1),
                                   (104, "Q", 2**64 - 1), (112, "Q", 3),
                                   (80, "Q", 0x400001), (24, "Q", 0)):
            data = bytearray(good)
            struct.pack_into("<" + fmt, data, offset, value)
            with self.subTest(offset=offset, value=value), self.assertRaises(bundle.BundleError):
                bundle.validate_elf(data)


if __name__ == "__main__":
    unittest.main()
