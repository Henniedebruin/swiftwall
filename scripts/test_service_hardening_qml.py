"""Execute SharedService QML logic with explicit Quickshell/Io stubs, no sockets.

This proves ordering/selection logic, not Quickshell transport integration. The
separate test_widget.py harness exercises the real imports and socket transport.
"""

import os
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = "/usr/lib/qt6/bin/qmltestrunner" if Path("/usr/lib/qt6/bin/qmltestrunner").is_file() else shutil.which("qmltestrunner6")


class ServiceHardeningTests(unittest.TestCase):
    @unittest.skipUnless(RUNNER, "Qt 6 QML test runner required")
    def test_ordering_and_runtime_environment(self):
        cases = ((None, None, ""), ("/private/explicit", None, "/private/explicit/control.sock"),
                 (None, "/private/xdg", "/private/xdg/swiftwall/control.sock"),
                 ("/private/explicit", "/private/xdg", "/private/explicit/control.sock"),
                 ("relative", "/private/xdg", "relative/control.sock"))
        with tempfile.TemporaryDirectory(prefix="sw-service-qml-") as temporary:
            root = Path(temporary)
            (root / "run").mkdir(mode=0o700)
            shutil.copyfile(ROOT / "plugin/SharedService.qml", root / "SharedService.qml")
            shutil.copyfile(ROOT / "tests/qml/service_hardening.qml", root / "tst_hardening.qml")
            imports = root / "imports/Quickshell"
            (imports / "Io").mkdir(parents=True)
            (imports / "qmldir").write_text("module Quickshell\nsingleton Quickshell 1.0 Quickshell.qml\n")
            (imports / "Io/qmldir").write_text("module Quickshell.Io\nSocket 1.0 Socket.qml\nSplitParser 1.0 SplitParser.qml\n")
            (imports / "Io/Socket.qml").write_text('''import QtQuick
QtObject {
    property string path: ""
    property bool connected: false
    property QtObject parser: null
    signal error()
    function write(data) { throw new Error("transport must remain unused") }
    function flush() { throw new Error("transport must remain unused") }
}
''')
            (imports / "Io/SplitParser.qml").write_text('''import QtQuick
QtObject { property string splitMarker: ""; signal read(string data) }
''')
            for override, xdg, expected in cases:
                with self.subTest(override=override, xdg=xdg):
                    values = {"TEST_EXPECTED_SOCKET": expected}
                    if override is not None:
                        values["SWIFTWALL_RUNTIME_DIR"] = override
                    if xdg is not None:
                        values["XDG_RUNTIME_DIR"] = xdg
                    (imports / "Quickshell.qml").write_text('pragma Singleton\nimport QtQuick\nQtObject {\n'
                        + ' property var values: (' + json.dumps(values) + ')\n'
                        + ' function env(key) { return values[key] || "" }\n}\n')
                    env = {"PATH": os.defpath, "HOME": str(root), "QT_QPA_PLATFORM": "offscreen",
                           "QML_DISABLE_DISK_CACHE": "1", "XDG_RUNTIME_DIR": str(root / "run"),
                           "DBUS_SESSION_BUS_ADDRESS": "disabled:"}
                    result = subprocess.run([RUNNER, "-import", str(root / "imports"), "-input", str(root / "tst_hardening.qml"),
                                             "-platform", "offscreen"], env=env, capture_output=True, text=True, timeout=10)
                    output = result.stdout + result.stderr
                    self.assertEqual(result.returncode, 0, output)
                    self.assertIn("HARDENING_PASS", output)
                    self.assertNotIn("HARDENING_FAIL", output)
                    for error in ("ReferenceError", "TypeError", "Failed to load configuration"):
                        self.assertNotIn(error, output)


if __name__ == "__main__":
    unittest.main()
