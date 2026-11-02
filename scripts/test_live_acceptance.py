"""Offline safety tests: every wallpaper/session command is mocked."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import live_acceptance as live


class RestorationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="swiftwall-restore-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        theme = self.root / "theme.name"
        theme.write_text("tokyo-night\n")
        self.record = {
            "original_target": "/local/image with 'quotes' and $symbols.png",
            "original_hash": "expected-hash",
            "backup": str(self.root / "backup.png"),
            "link": str(self.root / "background"),
            "omarchy": "/fake/omarchy",
            "shell": "/fake/omarchy-shell",
            "theme_path": str(theme),
            "theme_bytes": "tokyo-night\n",
        }

    def test_restore_uses_structured_arguments_and_exact_target(self):
        with (
            patch.object(live, "digest", return_value="expected-hash"),
            patch.object(live, "run") as command,
            patch.object(live.os, "readlink", return_value=self.record["original_target"]),
            patch.object(live.time, "sleep"),
        ):
            result = live.restore(self.record)
        self.assertTrue(result["restored"])
        command.assert_any_call([
            "/fake/omarchy", "theme", "bg", "set", Path(self.record["original_target"])
        ])
        self.assertEqual(command.call_count, 2)

    def test_changed_original_never_claims_restoration_or_executes_setter(self):
        with (
            patch.object(live, "digest", return_value="different-hash"),
            patch.object(live, "run") as command,
            patch.object(live.time, "sleep"),
        ):
            result = live.restore(self.record)
        self.assertFalse(result["restored"])
        self.assertEqual(len(result["errors"]), 3)
        command.assert_not_called()

    def test_setter_failure_is_reported_after_bounded_retries(self):
        with (
            patch.object(live, "digest", return_value="expected-hash"),
            patch.object(live, "run", side_effect=subprocess.TimeoutExpired("fake", 20)) as command,
            patch.object(live.time, "sleep"),
        ):
            result = live.restore(self.record)
        self.assertFalse(result["restored"])
        self.assertEqual(command.call_count, 3)
        self.assertIn("FAILED", result["warning"])

    def test_disarmed_guardian_never_restores(self):
        record_path = self.root / "record.json"
        record_path.write_text(json.dumps(self.record))
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        try:
            with patch.object(live, "REPORT", self.root), patch.object(live, "restore") as restore:
                live.guardian(record_path, read_fd)
                restore.assert_not_called()
        finally:
            os.close(read_fd)

    def test_armed_guardian_restores_on_parent_pipe_eof(self):
        record_path = self.root / "record.json"
        record_path.write_text(json.dumps(self.record))
        (self.root / "restore-armed").touch()
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        try:
            with (
                patch.object(live, "REPORT", self.root),
                patch.object(live, "restore", return_value={"restored": True}) as restore,
            ):
                live.guardian(record_path, read_fd)
                restore.assert_called_once_with(self.record)
                self.assertFalse((self.root / "restore-armed").exists())
                self.assertTrue((self.root / "guardian-restoration.json").exists())
        finally:
            os.close(read_fd)


if __name__ == "__main__":
    unittest.main()
