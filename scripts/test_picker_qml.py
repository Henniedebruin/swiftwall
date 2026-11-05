"""Exercise the external-picker QML process boundary; no desktop or portal access."""
import os
import shutil
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PickerProcessTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("qs") and shutil.which("dbus-run-session"), "Quickshell required")
    def test_bounded_process_lifecycle(self):
        with tempfile.TemporaryDirectory(prefix="swiftwall-picker-qml-") as temporary:
            root = Path(temporary)
            (root / "runtime").mkdir(mode=0o700)
            shutil.copy2(ROOT / "plugin/FolderPicker.qml", root / "FolderPicker.qml")
            # Replace only the disposable helper, never invoke a real chooser.
            (root / "pick_folder.py").write_text('''import json, os, resource, signal, sys, time
from pathlib import Path
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
assert sys.argv[1] == "--watch-parent" and len(sys.argv) == 3
assert int(sys.argv[2]) == os.getppid()
p = Path(__file__).with_name("calls")
i = int(p.read_text()) if p.exists() else 0
p.write_text(str(i+1))
with p.with_name("pids").open("a") as log: log.write(str(os.getpid()) + "\\n")
selected = json.dumps({"v":1,"result":"selected","url":"file:///tmp/caf%C3%A9%20%23%25%22"})
def term(signum, frame):
    with p.with_name("terminated").open("a") as log: log.write(str(i) + "\\n")
if i == 0:
    for char in selected + "\\n":
        os.write(1, char.encode("ascii")); time.sleep(.001)
elif i in (13, 16): print(selected)
elif i == 1: print('{"v":1,"result":"cancelled"}')
elif i == 2:
    print('{"v":1,"result":"error","message":"test portal failure"}'); raise SystemExit(1)
elif i == 3: os.kill(os.getpid(), signal.SIGSEGV)
elif i == 4:
    signal.signal(signal.SIGTERM, term)
    time.sleep(30)
elif i == 5: print("not json")
elif i == 6:
    signal.signal(signal.SIGTERM, term)
    for chunk in range(33):
        os.write(1, b"x" * 1024); time.sleep(.005)
    time.sleep(30)
elif i == 7: print(selected + " " * (32767 - len(selected)))
elif i == 8: print(selected + "\\n" + selected)
elif i == 9:
    print(selected, flush=True); time.sleep(.1); print(selected)
elif i == 10: os.write(1, selected.encode("ascii"))
elif i == 11: print('{"v":1,"result":"error","message":"café"}')
elif i == 12:
    signal.signal(signal.SIGTERM, term)
    print(" ", end="", flush=True)
    time.sleep(.2)
    print(selected, flush=True)
    time.sleep(30)
elif i == 14: print(selected + " " * (32768 - len(selected)))
elif i == 15: print("null")
''')
            (root / "shell.qml").write_text('''import QtQuick
import Quickshell
ShellRoot {
    id: host
    property int step: 0
    property int selections: 0
    property int errors: 0
    property int dismissals: 0
    property string lastError: ""
    property double started: 0
    function check(value, message) {
        if (!value) { console.log("PICKER_FAIL " + step + " " + message); Qt.quit() }
    }
    FolderPicker {
        id: picker
        timeoutMilliseconds: host.step === 4 ? 300 : 5000
        onFolderChosen: url => {
            host.check([0, 7, 13, 16].indexOf(host.step) >= 0, "no selection for invalid/cancelled frames")
            host.selections++
            host.check(url === "file:///tmp/caf%C3%A9%20%23%25%22", "URL preserved")
        }
        onFailed: message => { host.errors++; host.lastError = message }
        onDismissed: {
            host.dismissals++
            host.check(!active && frame === "", "released singleton and buffer")
            host.check(receivedBytes <= 32768, "bounded retained wire bytes")
            if (host.step === 0) host.check(host.selections === 1, "fragmented selection")
            if (host.step === 1) host.check(host.errors === 0 && host.selections === 1, "portal cancel")
            if (host.step === 2) host.check(host.lastError === "test portal failure", "portal failure")
            if (host.step === 3) host.check(host.errors === 2, "crash isolated from host")
            if (host.step === 4) host.check(host.lastError.indexOf("timed out") >= 0 && Date.now() - host.started >= 2000, "timeout TERM then KILL")
            if (host.step === 5 || host.step === 15) host.check(host.lastError.indexOf("invalid response") >= 0, "malformed JSON")
            if (host.step === 6) host.check(host.lastError.indexOf("oversized") >= 0 && Date.now() - host.started < 4500, "unterminated chunked output rejected before timeout")
            if (host.step === 7) host.check(host.selections === 2 && receivedBytes === 32768, "exact byte limit includes newline")
            if (host.step === 8 || host.step === 9) host.check(host.lastError.indexOf("duplicate") >= 0, "duplicate frame rejected")
            if (host.step === 10) host.check(host.lastError.indexOf("failed") >= 0, "newline required")
            if (host.step === 11) host.check(host.lastError.indexOf("ASCII") >= 0, "non-ASCII rejected")
            if (host.step === 12) host.check(host.errors === 9 && host.selections === 2 && Date.now() - host.started >= 2000, "explicit cancel drops late reply, TERM then KILL")
            if (host.step === 14) host.check(host.lastError.indexOf("oversized") >= 0, "newline counts toward byte limit")
            host.step++
            if (host.step === 17) {
                host.check(host.selections === 4 && host.errors === 11 && host.dismissals === 17, "final counts")
                console.log("PICKER_PASS 17 lifecycles; child crash did not kill host")
                Qt.quit()
            } else again.restart()
        }
    }
    Timer {
        interval: 10; running: true; repeat: true
        onTriggered: {
            host.check(picker.frame.length < 32768, "bounded partial frame")
            if (host.step === 12 && picker.active && picker.receivedBytes > 0 && !picker.cancelling) {
                picker.cancel(); picker.cancel()
                picker.open()
                host.check(picker.active, "cancel retains ownership until child reaped")
            }
        }
    }
    Timer {
        id: again; interval: 30
        onTriggered: {
            host.started = Date.now()
            picker.open(); picker.open()
            host.check(picker.active && picker.receivedBytes === 0 && picker.reply === null && !picker.cancelling, "fresh reopen state")
        }
    }
    Component.onCompleted: again.start()
}
''')
            env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "LANG": "C.UTF-8",
                   "XDG_RUNTIME_DIR": str(root / "runtime"), "QT_QPA_PLATFORM": "offscreen",
                   "DBUS_SYSTEM_BUS_ADDRESS": "unix:path=" + str(root / "no-system-bus"),
                   "QT_QUICK_BACKEND": "software", "QSG_RHI_BACKEND": "software"}
            process = subprocess.Popen(["dbus-run-session", "--", "qs", "--no-color", "--path", str(root)],
                                       env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, start_new_session=True)
            try:
                output, _ = process.communicate(timeout=35)
            finally:
                # Also reap disposable descendants if a test fails or hangs.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
            self.assertEqual(process.returncode, 0, output)
            self.assertNotIn("PICKER_FAIL", output)
            self.assertIn("PICKER_PASS 17", output)
            self.assertEqual((root / "calls").read_text(), "17")
            self.assertEqual((root / "terminated").read_text().splitlines(), ["4", "6", "12"])
            for pid in (root / "pids").read_text().splitlines():
                with self.assertRaises(ProcessLookupError, msg="helper still alive: " + pid):
                    os.kill(int(pid), 0)


if __name__ == "__main__":
    unittest.main()
