"""Portable, offline package checks; never load QML or contact a daemon."""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PluginPackageTests(unittest.TestCase):
    def test_manifest_and_entry_points(self):
        manifest = json.loads((ROOT / "manifest.json").read_text())
        self.assertEqual(manifest["schemaVersion"], 1)
        self.assertEqual(manifest["id"], "io.github.henniedebruin.swiftwall")
        self.assertEqual(set(manifest["kinds"]), {"service", "bar-widget"})
        self.assertFalse(manifest["barWidget"]["allowMultiple"])
        self.assertFalse(manifest["barWidget"]["defaults"]["showCountdown"])
        self.assertEqual(manifest["entryPoints"], {
            "service": "plugin/Service.qml", "barWidget": "plugin/BarWidget.qml",
        })
        for relative in manifest["entryPoints"].values():
            path = Path(relative)
            self.assertFalse(path.is_absolute())
            self.assertNotIn("..", path.parts)
            self.assertTrue((ROOT / path).is_file())
        for name in ("BarWidget.qml", "Panel.qml"):
            self.assertIn('moduleName: "' + manifest["id"] + '"', (ROOT / "plugin" / name).read_text())

    def test_widget_is_a_socket_client_not_a_second_controller(self):
        source = "\n".join(path.read_text() for path in (ROOT / "plugin").glob("*.qml"))
        for command in ("execDetached", "systemctl", "omarchy-theme-bg-set", "theme bg set"):
            self.assertNotIn(command, source)
        self.assertIn("Socket {", source)
        self.assertIn('op: "subscribe"', source)
        self.assertIn("if_revision", source)
        self.assertNotIn("QtQuick.Dialogs", source)
        self.assertNotIn("FolderDialog {", source)
        picker = (ROOT / "plugin/FolderPicker.qml").read_text()
        self.assertEqual(source.count("Process {"), 1)
        self.assertIn('process.command = ["/usr/bin/python3", helper,', picker)
        self.assertIn("--watch-parent", picker)
        self.assertIn("if (pending) return", picker)
        self.assertTrue((ROOT / "plugin/pick_folder.py").is_file())

    def test_runtime_assets_are_source_only(self):
        allowed = {".qml", ".js", ".md", ".json", ".py"}
        for path in (ROOT / "plugin").rglob("*"):
            self.assertFalse(path.is_symlink(), str(path))
            if "__pycache__" in path.parts:
                continue
            if path.is_file():
                self.assertIn(path.suffix, allowed, str(path))
                self.assertNotIn(b"\x00", path.read_bytes())


if __name__ == "__main__":
    unittest.main()
