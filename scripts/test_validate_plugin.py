"""Package-staging regressions; no installed shell, subprocess or desktop access."""

from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import plugin_support
import validate_plugin


class ValidationPackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="swiftwall-validator-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for name in plugin_support.FILES:
            destination = self.source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(validate_plugin.ROOT / name, destination)

    def test_only_payload_is_staged_even_with_ignored_evidence_symlinks(self):
        evidence = self.source / "artifacts/private"
        evidence.mkdir(parents=True)
        (evidence / "background").symlink_to("/nonexistent/private-image")
        (evidence / "private.txt").write_text("must not enter package")
        with patch.object(validate_plugin.subprocess, "run", side_effect=AssertionError("No subprocess")):
            package = validate_plugin.stage_package(self.source, self.root / "package")
        actual = sorted(str(path.relative_to(package)) for path in package.rglob("*") if path.is_file())
        self.assertEqual(actual, sorted(plugin_support.FILES))
        for name in plugin_support.FILES:
            self.assertEqual((package / name).read_bytes(), (self.source / name).read_bytes())
            self.assertEqual((package / name).stat().st_mode & 0o777, 0o755 if name.endswith(".py") else 0o644)

    def test_symlink_in_payload_fails_closed(self):
        path = self.source / "plugin/Panel.qml"
        path.unlink()
        path.symlink_to(validate_plugin.ROOT / "plugin/Panel.qml")
        with self.assertRaises(plugin_support.InstallError):
            validate_plugin.stage_package(self.source, self.root / "package")
        self.assertFalse((self.root / "package").exists())


if __name__ == "__main__":
    unittest.main()
