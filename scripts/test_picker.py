#!/usr/bin/env python3
"""Pure picker tests + private-D-Bus integration tests; no real portal or GUI.

Run: /usr/bin/python3 scripts/test_picker.py
Pure tests also run without PyGObject; integration tests skip if /usr/bin/python3
with Gio, or dbus-daemon, is unavailable. Child processes exist only in this
harness (private bus, fake service, helper), never in the helper itself.
"""

import contextlib
import importlib.util
import io
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

# Importing the helper must not create assets outside the two source files.
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "plugin" / "pick_folder.py"
PYTHON = "/usr/bin/python3"
SPEC = importlib.util.spec_from_file_location("pick_folder", HELPER)
assert SPEC is not None and SPEC.loader is not None
picker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(picker)


def fake_portal():
    """Minimal structured Gio service, reachable only via the private bus."""
    from gi.repository import Gio, GLib

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    bus.set_exit_on_close(False)
    loop = GLib.MainLoop()
    bus.connect("closed", lambda *_: loop.quit())
    registrations = []
    mode = os.environ["FAKE_MODE"]
    log = Path(os.environ["FAKE_LOG"])

    def event(kind, **fields):
        with log.open("a") as stream:
            stream.write(json.dumps(dict(kind=kind, **fields)) + "\n")

    request_info = Gio.DBusNodeInfo.new_for_xml('''<node>
      <interface name="org.freedesktop.portal.Request">
        <method name="Close"/>
      </interface></node>''').interfaces[0]

    def close(_bus, _sender, path, _interface, _method, _params, invocation):
        event("close", path=path)
        if mode == "ignore_close":
            return
        invocation.return_value(GLib.Variant("()", ()))

    chooser_info = Gio.DBusNodeInfo.new_for_xml('''<node>
      <interface name="org.freedesktop.portal.FileChooser">
        <method name="OpenFile">
          <arg type="s" direction="in"/><arg type="s" direction="in"/>
          <arg type="a{sv}" direction="in"/><arg type="o" direction="out"/>
        </method>
      </interface></node>''').interfaces[0]

    def opened(_bus, sender, _path, _interface, _method, params, invocation):
        parent, title, options = params.unpack()
        path = picker.DESKTOP + "/request/" + sender[1:].replace(".", "_") + "/" + options["handle_token"]
        registrations.append(bus.register_object(path, request_info, close, None, None))
        event("open", path=path, parent=parent, title=title, options=options)
        if mode == "method_error":
            invocation.return_dbus_error("org.example.Failed", "Deliberate method failure")
            return
        if mode == "method_malformed":
            reply = Gio.DBusMessage.new_method_reply(invocation.get_message())
            reply.set_body(GLib.Variant("(s)", ("not an object path",)))
            bus.send_message(reply, Gio.DBusSendMessageFlags.NONE)
            return
        if mode == "stall_method":
            return

        def respond():
            if mode == "malformed":
                response = GLib.Variant("(s)", ("bad",))
            else:
                code = 1 if mode == "cancel" else 2 if mode == "failed" else 0
                values = {}
                if mode != "missing":
                    if mode == "wrong_type":
                        values["uris"] = GLib.Variant("s", "not an array")
                    else:
                        uris = json.loads(os.environ.get("FAKE_URIS", "[]"))
                        values["uris"] = GLib.Variant("as", uris)
                response = GLib.Variant("(ua{sv})", (code, values))
            bus.emit_signal(sender, path, picker.REQUEST, "Response", response)
            return GLib.SOURCE_REMOVE

        if mode == "early":
            respond()  # Signal is sent before the method reply, intentionally.
        invocation.return_value(GLib.Variant("(o)", (
            path + "_unexpected" if mode == "mismatch" else path,)))
        if mode == "disappear":
            def disappear():
                bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                              "org.freedesktop.DBus", "ReleaseName",
                              GLib.Variant("(s)", (picker.PORTAL,)), None,
                              Gio.DBusCallFlags.NONE, 1000, None)
                return GLib.SOURCE_REMOVE
            GLib.timeout_add(50, disappear)
        elif mode not in ("early", "wait", "mismatch", "ignore_close"):
            GLib.timeout_add(20, respond)

    registrations.append(bus.register_object(picker.DESKTOP, chooser_info, opened, None, None))
    bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                  "org.freedesktop.DBus", "RequestName",
                  GLib.Variant("(su)", (picker.PORTAL, 0)), None,
                  Gio.DBusCallFlags.NONE, 1000, None)
    print("READY", flush=True)
    loop.run()
    for registration in registrations:
        bus.unregister_object(registration)


class PureTests(unittest.TestCase):
    @staticmethod
    def unix_modules(glib_unix=object(), glib=object()):
        events = []

        class Gi:
            @staticmethod
            def require_version(namespace, version):
                events.append(("require", namespace, version))

        def load():
            events.append(("load",))
            if isinstance(glib_unix, BaseException):
                raise glib_unix
            return glib_unix

        return Gi, glib, load, events

    def test_new_glibunix_signal_api_is_preferred_after_version_requirement(self):
        signal_add = lambda *_args: None
        fd_add = lambda *_args: None
        glib_unix = type("GLibUnix", (), {"signal_add": signal_add, "fd_add_full": fd_add})
        glib = type("GLib", (), {"unix_signal_add": lambda *_args: None,
                                  "unix_fd_add_full": lambda *_args: None})
        gi, glib, load, events = self.unix_modules(glib_unix, glib)

        self.assertEqual(picker.unix_apis(gi, glib, load), (signal_add, fd_add))
        self.assertEqual(events, [("require", "GLibUnix", "2.0"), ("load",)])

    def test_old_glibunix_without_signal_add_uses_glib_signal_api(self):
        signal_add = lambda *_args: None
        fd_add = lambda *_args: None
        glib_unix = type("GLibUnix", (), {"fd_add_full": fd_add})
        glib = type("GLib", (), {"unix_signal_add": signal_add,
                                  "unix_fd_add_full": lambda *_args: None})
        gi, glib, load, _events = self.unix_modules(glib_unix, glib)

        self.assertEqual(picker.unix_apis(gi, glib, load), (signal_add, fd_add))

    def test_unavailable_glibunix_uses_glib_unix_apis(self):
        signal_add = lambda *_args: None
        fd_add = lambda *_args: None
        glib = type("GLib", (), {"unix_signal_add": signal_add, "unix_fd_add_full": fd_add})
        gi, glib, load, _events = self.unix_modules(ImportError("missing typelib"), glib)

        self.assertEqual(picker.unix_apis(gi, glib, load), (signal_add, fd_add))

    def test_missing_signal_apis_is_a_clean_json_error(self):
        gi, glib, load, _events = self.unix_modules(type("GLibUnix", (), {}), type("GLib", (), {}))
        with self.assertRaisesRegex(ValueError, "GLib Unix signal API is unavailable"):
            picker.unix_apis(gi, glib, load)

        with mock.patch.object(picker, "unix_apis",
                               side_effect=ValueError("GLib Unix signal API is unavailable")), \
                contextlib.redirect_stdout(io.StringIO()) as stdout, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            status = picker.main([])

        self.assertEqual(status, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue()), {
            "v": 1, "result": "error", "message": "GLib Unix signal API is unavailable"})

    def test_unicode_and_reserved_names(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'caf\u00e9 space # % "quote" \'single\'\nnewline'
            path.mkdir()
            self.assertEqual(picker.validate_uri(path.as_uri()), path.as_uri())
            self.assertEqual(picker.validate_uri(path.as_uri().replace("file:///", "file://localhost/", 1)), path.as_uri())

    def test_invalid_uris(self):
        with tempfile.TemporaryDirectory() as root:
            file = Path(root) / "file"
            file.write_text("not a directory")
            invalid = [None, 42, "", "file:relative", "file:/tmp", "file://remote/tmp",
                       "file://user@localhost/tmp", "file://localhost:80/tmp", "https:///tmp",
                       "file:////tmp", "file:///%2Ftmp", "file:///tmp?", "file:///tmp#",
                       "file:///tmp?x=1", "file:///tmp#fragment", "file:///tmp/%",
                       "file:///tmp/%zz", "file:///tmp/%ff", "file:///tmp/%c0%af",
                       "file:///tmp/%00", "file:///tmp/\0", "file:///tmp/\n",
                       "file:///tmp/\t", "file:///tmp/space here", 'file:///tmp/"quote"',
                       "file:///tmp/\u00e9", "file:///tmp/\\", "file:///tmp/%ED%A0%80",
                       file.as_uri(), (Path(root) / "missing").as_uri()]
            for uri in invalid:
                with self.subTest(uri=uri), self.assertRaises((ValueError, UnicodeError)):
                    picker.validate_uri(uri)

    def test_initial_folder(self):
        for path in ("relative", "", "/tmp/\0", "/tmp/\udcff"):
            with self.subTest(path=path), self.assertRaises((ValueError, UnicodeError)):
                picker.local_folder(path)

    def test_unreadable_folder(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root bypasses directory permission checks")
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "unreadable"
            folder.mkdir(mode=0)
            try:
                with self.assertRaises(ValueError):
                    picker.validate_uri(folder.as_uri())
            finally:
                folder.chmod(0o700)

    def test_no_gtk_or_subprocess_imports(self):
        import ast
        tree = ast.parse(HELPER.read_text())
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.extend([node.module or ""] + [alias.name for alias in node.names])
        self.assertFalse(any("Gtk" in name or name in ("subprocess", "ctypes") for name in imports))
        self.assertTrue(HELPER.read_text().startswith("#!/usr/bin/python3\n"))


def integration_available():
    if not Path(PYTHON).exists() or not shutil.which("dbus-daemon"):
        return False
    probe = subprocess.run([PYTHON, "-c", "from gi.repository import Gio, GLib"],
                           capture_output=True, timeout=5, check=False)
    return probe.returncode == 0


@unittest.skipUnless(integration_available(), "requires system Python Gio and dbus-daemon")
class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "events.jsonl"
        self.folder = self.root / 'caf\u00e9 space # % "quotes" \'single\'\nnewline'
        self.folder.mkdir()
        # Do not use --session: its system service directories could activate a
        # real installed portal on our private bus. This bus has NO services.
        config = self.root / "bus.conf"
        config.write_text('''<busconfig>
          <type>session</type><listen>unix:tmpdir=/tmp</listen><auth>EXTERNAL</auth>
          <policy context="default">
            <allow user="*"/><allow own="*"/>
            <allow send_destination="*"/><allow receive_sender="*"/>
          </policy>
        </busconfig>''')
        self.bus = subprocess.Popen(["dbus-daemon", "--config-file=" + str(config),
                                     "--nofork", "--nopidfile", "--print-address=1"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.terminate, self.bus)
        assert self.bus.stdout is not None
        self.assertTrue(select.select([self.bus.stdout], [], [], 5)[0], "private bus not ready")
        address = self.bus.stdout.readline().strip()
        self.assertTrue(address.startswith("unix:"))
        self.env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=address,
                        FAKE_LOG=str(self.log), FAKE_URIS=json.dumps([self.folder.as_uri()]))
        # No fallback to the user's real session address is possible.
        self.env.pop("DBUS_STARTER_ADDRESS", None)
        self.env.pop("DBUS_STARTER_BUS_TYPE", None)
        self.fake = None

    @staticmethod
    def terminate(process):
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=3)

    def start_fake(self, mode="success", uris=None):
        env = dict(self.env, FAKE_MODE=mode)
        if uris is not None:
            env["FAKE_URIS"] = json.dumps(uris)
        self.fake = subprocess.Popen([PYTHON, str(Path(__file__).resolve()), "--fake-portal"],
                                     env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.terminate, self.fake)
        assert self.fake.stdout is not None
        self.assertTrue(select.select([self.fake.stdout], [], [], 5)[0], "fake not ready")
        self.assertEqual(self.fake.stdout.readline().strip(), "READY")

    def events(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def wait_event(self, kind):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            events = [event for event in self.events() if event["kind"] == kind]
            if events:
                return events[-1]
            time.sleep(0.01)
        self.fail("Missing fake event: " + kind)

    def launch(self, *args):
        process = subprocess.Popen([PYTHON, str(HELPER), *args], env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.terminate, process)
        return process

    def result(self, process, expected, timeout=8):
        stdout, stderr = process.communicate(timeout=timeout)
        self.assertEqual(stderr, b"", stderr.decode(errors="replace"))
        self.assertTrue(stdout.isascii())
        self.assertEqual(len(stdout.splitlines()), 1, stdout)
        value = json.loads(stdout)
        self.assertEqual(value["v"], 1)
        self.assertEqual(value["result"], expected, value)
        self.assertEqual(set(value), {"v", "result", "url"} if expected == "selected" else
                         {"v", "result", "message"} if expected == "error" else {"v", "result"})
        self.assertEqual(process.returncode, 1 if expected == "error" else 0)
        if expected == "error":
            self.assertTrue(value["message"])
        return value

    def test_success_and_initial_folder(self):
        self.start_fake()
        value = self.result(self.launch("--initial-folder", str(self.folder)), "selected")
        self.assertEqual(value["url"], self.folder.as_uri())
        options = self.wait_event("open")["options"]
        self.assertIs(options["directory"], True)
        self.assertIs(options["multiple"], False)
        self.assertEqual(bytes(options["current_folder"]), str(self.folder).encode() + b"\0")
        self.assertFalse(any(event["kind"] == "close" for event in self.events()))

    def test_response_before_method_reply_and_repeated_calls(self):
        self.start_fake("early")
        for _ in range(5):
            self.assertEqual(self.result(self.launch(), "selected")["url"], self.folder.as_uri())
        paths = [event["path"] for event in self.events() if event["kind"] == "open"]
        self.assertEqual(len(paths), 5)
        self.assertEqual(len(set(paths)), 5)
        assert self.fake is not None
        self.assertIsNone(self.fake.poll())

    def test_cancellation(self):
        self.start_fake("cancel")
        self.result(self.launch(), "cancelled")
        self.wait_event("close")

    def test_timeout_closes(self):
        self.start_fake("wait")
        value = self.result(self.launch("--timeout", "0.3"), "error")
        self.assertIn("timed out", value["message"])
        self.wait_event("close")

    def test_unresponsive_close_is_bounded(self):
        self.start_fake("ignore_close")
        process = self.launch("--timeout", "0.2")
        # Measure the bounded portal/Close path after startup, not Python/Gio
        # import and private-bus scheduling. Production Close has a 500ms guard.
        self.wait_event("open")
        start = time.monotonic()
        self.result(process, "error", timeout=2)
        self.assertLess(time.monotonic() - start, 1.5)
        self.wait_event("close")

    def test_absent_portal_is_error_without_activation(self):
        # No service directories exist in the private bus configuration.
        self.result(self.launch(), "error")

    def test_initial_method_timeout_closes(self):
        self.start_fake("stall_method")
        start = time.monotonic()
        value = self.result(self.launch("--timeout", "20"), "error")
        self.assertIn("5 seconds", value["message"])
        self.assertLess(time.monotonic() - start, 7)
        self.wait_event("close")

    def test_sigterm_and_sigint_close(self):
        self.start_fake("wait")
        for sig in (signal.SIGTERM, signal.SIGINT):
            process = self.launch()
            deadline = time.monotonic() + 4
            expected_count = 1 if sig == signal.SIGTERM else 2
            while len([e for e in self.events() if e["kind"] == "open"]) < expected_count:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            process.send_signal(sig)
            self.result(process, "cancelled")
        self.assertEqual(len([e for e in self.events() if e["kind"] == "close"]), 2)

    def test_parent_death_closes(self):
        if not hasattr(os, "pidfd_open"):
            self.skipTest("Linux pidfd unavailable")
        self.start_fake("wait")
        wrapper = subprocess.Popen([PYTHON, str(Path(__file__).resolve()), "--parent-wrapper"],
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.terminate, wrapper)
        self.wait_event("open")
        wrapper.terminate()
        stdout, stderr = wrapper.communicate(timeout=4)
        self.assertEqual(stderr, b"")
        self.assertEqual(json.loads(stdout), {"v": 1, "result": "cancelled"})
        self.wait_event("close")

    def test_service_disappears(self):
        self.start_fake("disappear")
        value = self.result(self.launch(), "error")
        self.assertIn("disappeared", value["message"])

    def test_bus_closes(self):
        self.start_fake("wait")
        process = self.launch()
        self.wait_event("open")
        self.terminate(self.bus)
        value = self.result(process, "error")
        # dbus-daemon may announce the service's name loss before closing peers.
        self.assertTrue("bus closed" in value["message"] or "disappeared" in value["message"])

    def check_bad_mode(self, mode):
        self.start_fake(mode)
        self.result(self.launch(), "error")
        self.wait_event("close")

    def test_method_failure(self):
        self.check_bad_mode("method_error")

    def test_malformed_method_reply(self):
        self.check_bad_mode("method_malformed")

    def test_mismatched_handle(self):
        self.check_bad_mode("mismatch")

    def test_malformed_response(self):
        self.check_bad_mode("malformed")

    def test_missing_uris(self):
        self.check_bad_mode("missing")

    def test_wrong_uri_type(self):
        self.check_bad_mode("wrong_type")

    def test_failed_response(self):
        self.check_bad_mode("failed")

    def test_multiple_uris(self):
        self.start_fake(uris=[self.folder.as_uri(), self.folder.as_uri()])
        self.result(self.launch(), "error")
        self.wait_event("close")

    def test_empty_uris(self):
        self.start_fake(uris=[])
        self.result(self.launch(), "error")

    def test_invalid_portal_uris(self):
        # Each run has its own helper connection; the fake stays on this bus.
        for uri in ("file://remote/tmp", "file:///tmp/%ff", "file:///tmp/%00",
                    "file:///tmp/%xx", "file:///tmp#fragment", "file:relative"):
            with self.subTest(uri=uri):
                if self.fake is not None:
                    self.terminate(self.fake)
                self.start_fake(uris=[uri])
                self.result(self.launch(), "error")

    def test_invalid_arguments_do_not_contact_portal(self):
        self.start_fake()
        for args in (("--timeout", "nan"), ("--timeout", "0"), ("--timeout", "inf"),
                     ("--timeout", "-1"), ("--initial-folder", "relative"),
                     ("--command", "anything"), ("--watch-parent", "0"), ("--help",)):
            with self.subTest(args=args):
                self.result(self.launch(*args), "error")
        self.assertEqual(self.events(), [])


if __name__ == "__main__":
    if sys.argv[1:] == ["--fake-portal"]:
        fake_portal()
    elif sys.argv[1:] == ["--parent-wrapper"]:
        child = subprocess.Popen([PYTHON, str(HELPER), "--watch-parent", str(os.getpid())])
        sys.exit(child.wait())
    else:
        unittest.main()
