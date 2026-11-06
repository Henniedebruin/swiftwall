"""Offline safety tests: every wallpaper/session command is mocked."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import live_acceptance as live


class SafetyCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sw-restore-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(live, "REPORT", self.root).start()
        patch.object(live, "print", create=True).start()
        # No test in this module may accidentally invoke a real desktop/service
        # command, process launcher or signal, even on an unexpected branch.
        patch.object(live, "run", side_effect=AssertionError("Unexpected external command")).start()
        patch.object(live.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"")).start()
        patch.object(live.subprocess, "Popen", side_effect=AssertionError("Unexpected process launch")).start()
        patch.object(live.os, "kill", side_effect=AssertionError("Unexpected process signal")).start()
        patch.object(live.os, "pidfd_open", side_effect=AssertionError("Unexpected process handle")).start()
        patch.object(live.signal, "pidfd_send_signal", side_effect=AssertionError("Unexpected process signal")).start()
        patch.object(live.os, "execv", side_effect=AssertionError("Unexpected daemon exec")).start()
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

class SwiftViewCandidateTests(SafetyCase):
    def test_oversized_images_are_excluded_only_from_manual_viewer_candidates(self):
        acceptance = live.Acceptance()
        acceptance.images = [Path("/walls/oversized.webp"), Path("/walls/a.png"),
                             Path("/walls/b.png"), Path("/walls/limit.png")]
        original = list(acceptance.images)
        dimensions = [b"8192 4608", b"1920 1080", b"2560 1440", b"8192 4096"]
        with patch.object(live, "run", side_effect=[
                subprocess.CompletedProcess([], 0, value) for value in dimensions]):
            self.assertEqual(acceptance.swiftview_candidates(), original[1:])
        self.assertEqual(acceptance.images, original)

    def test_too_few_compatible_images_is_an_explicit_precondition_failure(self):
        acceptance = live.Acceptance()
        acceptance.images = [Path("/walls/a.png"), Path("/walls/b.png")]
        with patch.object(live, "run", return_value=subprocess.CompletedProcess([], 0, b"1920 1080")):
            with self.assertRaisesRegex(RuntimeError, "fewer than three"):
                acceptance.swiftview_candidates()

    def test_malformed_dimensions_fail_instead_of_silently_skipping(self):
        acceptance = live.Acceptance()
        acceptance.images = [Path("/walls/a.png")]
        with patch.object(live, "run", return_value=subprocess.CompletedProcess([], 0, b"unknown")):
            with self.assertRaisesRegex(RuntimeError, "Cannot inspect"):
                acceptance.swiftview_candidates()


class RestorationTests(SafetyCase):
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


class FakeDesktop:
    """Local files plus strict command fakes; never talks to the real session."""

    def __init__(self, case):
        self.root = case.root
        self.calls = []
        self.extra_pids = set()
        self.on_stop = None
        self.on_start = None
        self.setter_fails = False
        self.guard_ready = True
        self.on_guard = None
        self.child_gate = None
        self.original = self.root / "original.png"
        self.original.write_bytes(b"original wallpaper fixture")
        self.link = self.root / "background"
        self.link.symlink_to(self.original)
        self.unit = self.root / "swiftwall.service"
        self.unit.write_text("[Service]\nExecStart=/fixture/bin/swiftwall daemon\n")
        self.binary = self.root / "bin/swiftwall"
        self.binary.parent.mkdir()
        self.binary.write_bytes(b"installed binary fixture")
        self.config = self.root / "config.toml"
        self.config.write_bytes(b"installed config fixture")
        self.installed_state = self.root / "state.json"
        self.installed_state.write_bytes(b"installed state fixture")
        self.proc = self.root / "proc"
        self.state = dict(zip(live.SERVICE_PROPERTIES, (
            live.UNIT, "loaded", "active", "running", "enabled", "123",
            str(self.unit), "/fixture/swiftwall.service", "no", "",
        )))
        self.add_process(123, "old-start")
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        # Socket binding is intentionally unavailable in the offline sandbox.
        # Model only the selected fixture display, without opening a socket.
        display_path = self.runtime / "wayland-fixture"
        patch.object(live.Path, "is_socket", autospec=True,
                     side_effect=lambda path: path == display_path).start()
        self.env = {"XDG_RUNTIME_DIR": str(self.runtime), "WAYLAND_DISPLAY": "wayland-fixture",
                    "HYPRLAND_INSTANCE_SIGNATURE": "fixture", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/fixture/bus"}
        collection = self.root / "collection"
        collection.mkdir()
        for index in range(4):
            (collection / f"image{index}.png").write_bytes(f"fixture {index}".encode())
        for name, value in (("LINK", self.link), ("THEME", Path(case.record["theme_path"])),
                            ("COLLECTION", collection), ("BINARY", self.root / "release/swiftwall"),
                            ("PROC", self.proc)):
            patch.object(live, name, value).start()
        live.BINARY.parent.mkdir()
        live.BINARY.write_bytes(b"test release fixture")
        patch.dict(live.os.environ, self.env, clear=True).start()
        patch.object(live.Path, "home", return_value=self.root).start()
        patch.object(live.shutil, "which", side_effect=lambda name: f"/fake/{name}").start()
        patch.object(live, "run", side_effect=self.run).start()
        patch.object(live.subprocess, "run", side_effect=self.pgrep).start()
        patch.object(live.subprocess, "Popen", side_effect=self.popen).start()
        patch.object(live.time, "sleep").start()
        self.visual = patch.object(live.Acceptance, "visual_check", return_value=0.0).start()
        self.files_before = {p: p.read_bytes() for p in (self.binary, self.unit, self.config, self.installed_state)}
        case.addCleanup(self.close_gate)

    def close_gate(self):
        if self.child_gate is not None:
            os.close(self.child_gate)
            self.child_gate = None

    def add_process(self, pid, start):
        directory = self.proc / str(pid)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "stat").write_text(f"{pid} (swiftwall) " + " ".join(["S"] + ["0"] * 18 + [start]))
        (directory / "cgroup").write_text("0::/fixture/swiftwall.service\n")
        (directory / "exe").symlink_to(self.binary)

    def inactive(self):
        self.state.update(ActiveState="inactive", SubState="dead", MainPID="0", ControlGroup="")

    def pgrep(self, args, **kwargs):
        if args != ["pgrep", "-x", "swiftwall"]:
            raise AssertionError(f"Unexpected subprocess: {args}")
        pids = self.extra_pids | ({int(self.state["MainPID"])} if self.state["MainPID"] != "0" else set())
        return subprocess.CompletedProcess(args, 0 if pids else 1, "\n".join(map(str, pids)).encode())

    def run(self, args, **kwargs):
        args = list(map(str, args))
        self.calls.append(args)
        out = b""
        if args[:4] == ["systemctl", "--user", "--no-pager", "show"]:
            if args[4] != live.UNIT:
                raise AssertionError("Wrong unit queried")
            out = "\n".join(f"{key}={value}" for key, value in self.state.items()).encode()
        elif args == ["systemctl", "--user", "stop", live.UNIT]:
            self.inactive()
            if self.on_stop:
                self.on_stop()
        elif args == ["systemctl", "--user", "start", live.UNIT]:
            if self.on_start:
                self.on_start()
            self.add_process(456, "new-start")
            self.state.update(ActiveState="active", SubState="running", MainPID="456", ControlGroup="/fixture/swiftwall.service")
        elif args[:4] == ["/fake/omarchy", "theme", "bg", "set"]:
            if self.setter_fails:
                raise subprocess.TimeoutExpired(args, 20)
            self.link.unlink()
            self.link.symlink_to(args[4])
        elif args == ["/fake/omarchy-shell", "shell", "ping"]:
            out = b"ok"
        elif args[:2] == ["hyprctl", "-j"]:
            out = json.dumps({
                "monitors": [{"focused": True, "name": "fixture", "width": 100, "height": 100, "scale": 1}],
                "activeworkspace": {"id": 1}, "activewindow": {}, "cursorpos": {"x": 0, "y": 0},
            }[args[2]]).encode()
        elif args[:2] == ["hyprctl", "dispatch"]:
            pass
        elif args[0] == "realpath":
            out = args[1].encode()
        elif args[0] == "findmnt":
            out = b"ext4 fixture"
        elif args[0] not in ("magick", "cc"):
            raise AssertionError(f"Unexpected command: {args}")
        return subprocess.CompletedProcess(args, 0, out)

    def popen(self, args, **kwargs):
        child = Mock(pid=789)
        child.poll.return_value = None
        child.wait.return_value = 0
        if "--guardian" in args:
            self.calls.append(["guardian-start"])
            record = json.loads((self.root / "restoration-record.json").read_text())
            if record["changes_authorized"] or not (self.root / "restore-armed").exists():
                raise AssertionError("Guardian must start armed before authorizing changes")
            if self.on_guard:
                self.on_guard()
            if self.guard_ready:
                os.write(int(args[args.index("--guardian-ready") + 1]), b"ready")
        elif "--test-daemon" in args:
            self.child_gate = os.dup(int(args[-1]))
            self.add_process(child.pid, "test-start")
        else:
            raise AssertionError(f"Unexpected launcher: {args}")
        return child


class InstalledServiceTests(SafetyCase):
    def setUp(self):
        super().setUp()
        self.desktop = FakeDesktop(self)

    def acceptance(self, allow=True):
        acceptance = live.Acceptance(allow_installed_service=allow)
        # Release fixtures even when a test intentionally leaves a guard armed.
        self.addCleanup(lambda: acceptance.report_lock.close() if acceptance.report_lock else None)
        self.addCleanup(lambda: os.close(acceptance.guard_write) if acceptance.guard_write is not None else None)
        self.addCleanup(lambda: acceptance.temp.cleanup() if acceptance.temp else None)
        return acceptance

    def record_data(self):
        return {**self.record, "original_target": str(self.desktop.original),
                "original_hash": live.digest(self.desktop.original), "link": str(self.desktop.link),
                "omarchy": "/fake/omarchy", "shell": "/fake/omarchy-shell", "desktop_env": self.desktop.env}

    def saved_record(self):
        _, installed = live.snapshot_service(True)
        return {**self.record_data(), "installed_service": installed}

    def test_default_refuses_installed_service_without_changes(self):
        acceptance = self.acceptance(False)
        with self.assertRaisesRegex(RuntimeError, "allow-installed-service"):
            acceptance.preflight()
        self.assertFalse(any(c[0] == "guardian-start" or "stop" in c for c in self.desktop.calls))

    def test_guard_and_durable_snapshot_precede_exact_stop(self):
        acceptance = self.acceptance()
        acceptance.preflight()
        calls = self.desktop.calls
        self.assertLess(calls.index(["guardian-start"]), calls.index(["systemctl", "--user", "stop", live.UNIT]))
        record = json.loads((self.root / "restoration-record.json").read_text())
        self.assertTrue(record["changes_authorized"])
        self.assertEqual(record["installed_service"]["process"], {"pid": 123, "start": "old-start"})
        self.assertEqual(record["original_hash"], live.digest(self.desktop.original))
        for key in ("SWIFTWALL_CONFIG", "SWIFTWALL_STATE_DIR", "SWIFTWALL_RUNTIME_DIR"):
            self.assertTrue(Path(acceptance.env[key]).is_relative_to(Path(acceptance.temp.name)))
        self.assertEqual(self.desktop.state["UnitFileState"], "enabled")
        acceptance.cleanup()
        self.assertEqual(self.desktop.state["ActiveState"], "active")
        self.assertEqual(self.desktop.state["MainPID"], "456")
        self.assertFalse((self.root / "restore-armed").exists())
        self.assertEqual(self.desktop.files_before, {p: p.read_bytes() for p in self.desktop.files_before})

    def test_disabled_but_active_service_remains_disabled_after_cleanup(self):
        self.desktop.state["UnitFileState"] = "disabled"
        acceptance = self.acceptance()
        acceptance.preflight()
        acceptance.cleanup()
        self.assertEqual(self.desktop.state["UnitFileState"], "disabled")
        self.assertEqual(self.desktop.state["ActiveState"], "active")

    def test_installed_inactive_service_is_not_started(self):
        self.desktop.inactive()
        acceptance = self.acceptance()
        acceptance.preflight()
        acceptance.cleanup()
        self.assertFalse(any("stop" in c or "start" in c for c in self.desktop.calls))
        self.assertEqual(self.desktop.state["ActiveState"], "inactive")

    def test_guard_start_failure_never_authorizes_service_changes(self):
        self.desktop.guard_ready = False
        acceptance = self.acceptance()
        with self.assertRaisesRegex(RuntimeError, "did not become ready"):
            live.execute(acceptance)
        self.assertFalse(any("stop" in c or "start" in c for c in self.desktop.calls))
        self.assertFalse((self.root / "restore-armed").exists())

    def test_guard_launcher_exception_never_stops_installed_service(self):
        with patch.object(live.subprocess, "Popen", side_effect=OSError("guardian spawn failed")):
            with self.assertRaisesRegex(OSError, "guardian spawn failed"):
                live.execute(self.acceptance())
        self.assertFalse(any("stop" in c or "start" in c for c in self.desktop.calls))
        self.assertFalse((self.root / "restore-armed").exists())

    def test_missing_desktop_environment_fails_before_external_command(self):
        with patch.dict(live.os.environ, {}, clear=True):
            acceptance = self.acceptance()
        with self.assertRaisesRegex(RuntimeError, "desktop environment"):
            live.execute(acceptance)
        self.assertEqual(self.desktop.calls, [])

    def test_missing_wayland_socket_fails_before_external_command(self):
        acceptance = self.acceptance()
        acceptance.env["WAYLAND_DISPLAY"] = "missing-socket"
        with self.assertRaisesRegex(RuntimeError, "runtime/socket unavailable"):
            live.execute(acceptance)
        self.assertEqual(self.desktop.calls, [])

    def test_unmanaged_competitor_is_refused_without_signals_or_stop(self):
        self.desktop.extra_pids.add(999)
        with self.assertRaisesRegex(RuntimeError, "Unmanaged"):
            self.acceptance().preflight()
        self.assertFalse(any("stop" in c for c in self.desktop.calls))

    def test_wrong_process_cgroup_is_refused_without_stop(self):
        (self.desktop.proc / "123/cgroup").write_text("0::/unmanaged\n")
        with self.assertRaisesRegex(RuntimeError, "ownership"):
            self.acceptance().preflight()
        self.assertFalse(any("stop" in c for c in self.desktop.calls))

    def test_unit_alias_and_dropins_are_refused(self):
        for key, value in (("Id", "other.service"), ("DropInPaths", "/fixture/override.conf")):
            with self.subTest(key=key), patch.dict(self.desktop.state, {key: value}):
                with self.assertRaisesRegex(RuntimeError, "exact, loaded"):
                    live.snapshot_service(True)

    def test_incomplete_unit_evidence_is_refused(self):
        self.desktop.state.pop("UnitFileState")
        with self.assertRaisesRegex(RuntimeError, "Incomplete service state"):
            live.snapshot_service(True)

    def test_replaced_mainpid_after_guard_is_not_stopped(self):
        def replace():
            self.desktop.add_process(321, "replacement")
            self.desktop.state["MainPID"] = "321"
        self.desktop.on_guard = replace
        with self.assertRaisesRegex(RuntimeError, "PID changed"):
            live.execute(self.acceptance())
        self.assertFalse(any("stop" in c for c in self.desktop.calls))
        self.assertTrue((self.root / "restore-armed").exists())

    def test_stop_timeout_after_stopping_restores_and_keeps_original_failure(self):
        def timeout():
            raise subprocess.TimeoutExpired("fake stop", 20)
        self.desktop.on_stop = timeout
        acceptance = self.acceptance()
        with self.assertRaises(subprocess.TimeoutExpired):
            live.execute(acceptance)
        self.assertEqual(self.desktop.state["ActiveState"], "active")
        self.assertFalse(acceptance.results["all_tests_passed"])
        self.assertEqual(acceptance.results["events"][0]["name"], "TEST FAILURE")

    def test_interrupt_after_stop_restores_wallpaper_then_service(self):
        acceptance = self.acceptance()
        def interrupt():
            raise KeyboardInterrupt("fixture interruption")
        self.desktop.on_stop = interrupt
        with self.assertRaisesRegex(KeyboardInterrupt, "fixture interruption"):
            live.execute(acceptance)
        setter = next(i for i, c in enumerate(self.desktop.calls) if c[:4] == ["/fake/omarchy", "theme", "bg", "set"])
        restart = self.desktop.calls.index(["systemctl", "--user", "start", live.UNIT])
        self.assertLess(setter, restart)
        self.assertTrue(acceptance.results["restoration"]["restored"])

    def test_guardian_restores_stopped_installed_service_on_eof(self):
        record = self.saved_record()
        live.stop_installed(record)
        path = self.root / "restoration-record.json"
        live.write_json(path, record)
        (self.root / "restore-armed").touch()
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        try:
            live.guardian(path, read_fd)
        finally:
            os.close(read_fd)
        self.assertEqual(self.desktop.state["ActiveState"], "active")
        self.assertFalse((self.root / "restore-armed").exists())
        self.assertTrue(json.loads((self.root / "guardian-restoration.json").read_text())["restored"])

    def test_restore_waits_for_in_flight_stop_before_starting(self):
        record = self.saved_record()
        self.desktop.state.update(ActiveState="deactivating", SubState="stop-sigterm")
        with patch.object(live.time, "sleep", side_effect=lambda delay: self.desktop.inactive()):
            self.assertTrue(live.restore(record)["restored"])
        self.assertEqual(self.desktop.state["ActiveState"], "active")

    def test_unsettled_service_job_keeps_restoration_failed(self):
        record = self.saved_record()
        self.desktop.state.update(ActiveState="deactivating", SubState="stop-sigterm")
        with patch.object(live.time, "monotonic", side_effect=[0, 31]):
            result = live.restore(record)
        self.assertFalse(result["restored"])
        self.assertIn("did not settle", result["errors"][0])
        self.assertFalse(any("start" in c or "set" in c for c in self.desktop.calls))

    def test_failed_unit_after_interrupted_stop_can_be_started(self):
        record = self.saved_record()
        self.desktop.inactive()
        self.desktop.state.update(ActiveState="failed", SubState="failed")
        self.assertTrue(live.restore(record)["restored"])
        self.assertEqual(self.desktop.state["ActiveState"], "active")

    def test_guardian_interruption_before_stop_can_restore_original_pid(self):
        record = self.saved_record()
        result = live.restore(record)
        self.assertTrue(result["restored"])
        self.assertIn(["systemctl", "--user", "stop", live.UNIT], self.desktop.calls)

    def test_wallpaper_failure_still_restores_original_service(self):
        record = self.saved_record()
        live.stop_installed(record)
        self.desktop.setter_fails = True
        result = live.restore(record)
        self.assertFalse(result["restored"])
        self.assertTrue(result["service_restored"])
        self.assertEqual(self.desktop.state["ActiveState"], "active")

    def test_restart_cannot_hide_wallpaper_change(self):
        record = self.saved_record()
        live.stop_installed(record)
        def change_wallpaper():
            self.desktop.link.unlink()
            self.desktop.link.symlink_to(self.root / "different.png")
        self.desktop.on_start = change_wallpaper
        result = live.restore(record)
        self.assertFalse(result["restored"])
        self.assertFalse(result["wallpaper_restored"])
        self.assertTrue(result["service_restored"])

    def test_enablement_drift_is_reported_without_start_or_disable(self):
        record = self.saved_record()
        live.stop_installed(record)
        self.desktop.state["UnitFileState"] = "disabled"
        result = live.restore(record)
        self.assertFalse(result["restored"])
        self.assertIn("UnitFileState changed", result["errors"][0])
        self.assertFalse(any("start" in c or "disable" in c for c in self.desktop.calls))

    def test_unit_content_drift_refuses_lifecycle_operations(self):
        record = self.saved_record()
        self.desktop.unit.write_text("changed unit")
        result = live.restore(record)
        self.assertFalse(result["restored"])
        self.assertFalse(any("start" in c or "stop" in c for c in self.desktop.calls))

    def test_installed_binary_drift_refuses_lifecycle_operations(self):
        record = self.saved_record()
        self.desktop.binary.write_bytes(b"binary changed externally")
        result = live.restore(record)
        self.assertFalse(result["restored"])
        self.assertIn("binary changed", result["errors"][0])
        self.assertFalse(any("start" in c or "stop" in c for c in self.desktop.calls))

    def test_competitor_during_restore_prevents_setter_and_restart(self):
        record = self.saved_record()
        live.stop_installed(record)
        self.desktop.extra_pids.add(999)
        result = live.restore(record)
        self.assertFalse(result["restored"])
        self.assertFalse(any("start" in c or "set" in c for c in self.desktop.calls))

    def test_guardian_after_interrupted_start_verifies_without_second_stop(self):
        record = self.saved_record()
        live.stop_installed(record)
        def interrupted_start():
            self.desktop.add_process(456, "new-start")
            self.desktop.state.update(ActiveState="active", SubState="running", MainPID="456", ControlGroup="/fixture/swiftwall.service")
            raise KeyboardInterrupt("during start")
        self.desktop.on_start = interrupted_start
        with self.assertRaises(KeyboardInterrupt):
            live.restore(record)
        saved = json.loads((self.root / "restoration-record.json").read_text())
        self.desktop.calls.clear()
        self.assertTrue(live.restore(saved)["restored"])
        self.assertFalse(any("start" in c or "stop" in c or "set" in c for c in self.desktop.calls))

    def test_failed_restart_keeps_armed_report_and_original_test_failure(self):
        def failed_start():
            raise subprocess.TimeoutExpired("fake start", 20)
        self.desktop.on_start = failed_start
        acceptance = self.acceptance()
        with patch.object(acceptance, "tests", side_effect=ValueError("original engine failure")):
            with self.assertRaisesRegex(ValueError, "original engine failure") as raised:
                live.execute(acceptance)
        self.assertTrue(any("Cleanup also failed" in note for note in raised.exception.__notes__))
        result = json.loads((self.root / "result.json").read_text())
        self.assertFalse(result["all_tests_passed"])
        self.assertTrue(any(e["name"] == "TEST FAILURE" and "original engine failure" in e["error"] for e in result["events"]))
        self.assertTrue((self.root / "restore-armed").exists())

    def test_daemon_gate_releases_only_after_durable_identity(self):
        acceptance = self.acceptance()
        acceptance.preflight()
        acceptance.start_daemon()
        record = json.loads((self.root / "restoration-record.json").read_text())
        self.assertEqual(record["daemon_pid"], 789)
        self.assertEqual(record["daemon_start"], "test-start")
        self.assertEqual(os.read(self.desktop.child_gate, 2), b"go")
        for key in ("SWIFTWALL_CONFIG", "SWIFTWALL_STATE_DIR", "SWIFTWALL_RUNTIME_DIR"):
            self.assertNotIn(acceptance.env[key], map(str, self.desktop.files_before))

    def test_daemon_not_released_when_identity_record_write_fails(self):
        acceptance = self.acceptance()
        acceptance.preflight()
        with patch.object(live, "write_json", side_effect=OSError("record write failed")):
            with self.assertRaisesRegex(OSError, "record write failed"):
                acceptance.start_daemon()
        self.assertEqual(os.read(self.desktop.child_gate, 2), b"")

    def test_inherited_swiftwall_paths_and_unknown_overrides_are_removed(self):
        with patch.dict(live.os.environ, {"SWIFTWALL_CONFIG": str(self.desktop.config),
                                         "SWIFTWALL_STATE_DIR": str(self.desktop.installed_state.parent),
                                         "SWIFTWALL_UNKNOWN": "production"}):
            acceptance = self.acceptance()
        acceptance.preflight()
        self.assertNotIn("SWIFTWALL_UNKNOWN", acceptance.env)
        for key in ("SWIFTWALL_CONFIG", "SWIFTWALL_STATE_DIR", "SWIFTWALL_RUNTIME_DIR"):
            self.assertTrue(Path(acceptance.env[key]).is_relative_to(acceptance.temp.name))
        acceptance.cleanup()

    def test_clean_install_path_still_requires_not_found_and_no_competitor(self):
        self.desktop.inactive()
        self.desktop.state.update(LoadState="not-found", FragmentPath="", UnitFileState="")
        acceptance = self.acceptance(False)
        acceptance.preflight()
        self.assertIsNone(acceptance.record["installed_service"])
        acceptance.cleanup()
        self.assertFalse(any("start" in c or "stop" in c for c in self.desktop.calls))

    def test_clean_install_rejects_unit_file_even_if_manager_says_not_found(self):
        self.desktop.inactive()
        self.desktop.state.update(LoadState="not-found", FragmentPath="", UnitFileState="")
        user_unit = self.root / ".config/systemd/user/swiftwall.service"
        user_unit.parent.mkdir(parents=True)
        user_unit.write_text("unloaded unit fixture")
        with self.assertRaisesRegex(RuntimeError, "Unexpected installed unit file"):
            self.acceptance(False).preflight()


class OwnershipTests(SafetyCase):
    def test_armed_report_is_never_overwritten_even_on_failure(self):
        contents = {"restoration-record.json": b"original record", "result.json": b"original failure", "restore-armed": b"armed"}
        for name, data in contents.items():
            (self.root / name).write_bytes(data)
        with self.assertRaisesRegex(RuntimeError, "previous restoration guard"):
            live.execute(live.Acceptance(True))
        self.assertEqual(contents, {name: (self.root / name).read_bytes() for name in contents})

    def test_second_controller_cannot_claim_owned_report(self):
        first = live.Acceptance()
        first.claim_report()
        try:
            with self.assertRaisesRegex(RuntimeError, "owns this report"):
                live.execute(live.Acceptance())
        finally:
            first.report_lock.close()

    def test_unreleased_launcher_eof_does_not_exec(self):
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        with patch.object(live.os, "execv") as execute:
            live.launch_test_daemon(read_fd)
        execute.assert_not_called()

    def test_launcher_refuses_unregistered_process_even_with_gate_token(self):
        live.write_json(self.root / "restoration-record.json", {"daemon_pid": -1})
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"go")
        os.close(write_fd)
        with patch.object(live.os, "execv") as execute:
            with self.assertRaisesRegex(RuntimeError, "not registered"):
                live.launch_test_daemon(read_fd)
        execute.assert_not_called()

    def test_launcher_executes_only_registered_identity_and_private_environment(self):
        env = {"SWIFTWALL_CONFIG": "/fixture/private/config.toml"}
        live.write_json(self.root / "restoration-record.json", {
            "changes_authorized": True, "daemon_pid": 901, "daemon_start": "registered", "test_env": env,
        })
        (self.root / "restore-armed").touch()
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"go")
        os.close(write_fd)
        with (patch.object(live.os, "getpid", return_value=901),
              patch.object(live, "proc_start", return_value="registered"),
              patch.dict(live.os.environ, env), patch.object(live.os, "execv") as execute):
            live.launch_test_daemon(read_fd)
        execute.assert_called_once_with(str(live.BINARY), [str(live.BINARY), "daemon"])

    def test_reused_pid_is_never_signalled(self):
        record = {**self.record, "daemon_pid": 99, "daemon_start": "old"}
        with patch.object(live, "proc_start", return_value="new"):
            live.stop_test_daemon(record)
        live.os.kill.assert_not_called()
        live.os.pidfd_open.assert_not_called()

    def test_pid_reuse_after_opening_process_handle_never_signals_replacement(self):
        record = {**self.record, "daemon_pid": 99, "daemon_start": "old"}
        with (
            patch.object(live, "proc_start", side_effect=["old", "new"]),
            patch.object(live.os, "pidfd_open", return_value=901),
            patch.object(live.os, "close") as close,
        ):
            live.stop_test_daemon(record)
        live.signal.pidfd_send_signal.assert_not_called()
        close.assert_called_once_with(901)

    def test_registered_test_child_is_stopped_through_pinned_handle(self):
        record = {**self.record, "daemon_pid": 99, "daemon_start": "old"}
        with (
            patch.object(live, "proc_start", side_effect=["old", "old", None]),
            patch.object(live.os, "pidfd_open", return_value=901),
            patch.object(live.os, "close"),
            patch.object(live.signal, "pidfd_send_signal") as send,
        ):
            live.stop_test_daemon(record)
        send.assert_called_once_with(901, live.signal.SIGTERM)
        live.os.kill.assert_not_called()

    def test_forced_shutdown_targets_only_pinned_child_and_waits_for_exit(self):
        record = {**self.record, "daemon_pid": 99, "daemon_start": "old"}
        with (
            patch.object(live, "proc_start", side_effect=["old", "old", "old", None]),
            patch.object(live.os, "pidfd_open", return_value=901),
            patch.object(live.os, "close"),
            patch.object(live.signal, "pidfd_send_signal") as send,
            patch.object(live.Path, "read_text", return_value="99 (swiftwall) S"),
            patch.object(live.time, "monotonic", side_effect=[0, 16, 16]),
            patch.object(live.time, "sleep"),
        ):
            self.assertTrue(live.stop_test_daemon(record))
        self.assertEqual([call.args for call in send.call_args_list],
                         [(901, live.signal.SIGTERM), (901, live.signal.SIGKILL)])
        live.os.kill.assert_not_called()

    def test_forced_shutdown_is_preserved_in_restoration_evidence(self):
        with (patch.object(live, "stop_test_daemon", return_value=True),
              patch.object(live, "digest", return_value="expected-hash"),
              patch.object(live, "run"),
              patch.object(live.os, "readlink", return_value=self.record["original_target"]),
              patch.object(live.time, "sleep")):
            result = live.restore(self.record)
        self.assertTrue(result["restored"])
        self.assertIn("Daemon needed forced shutdown before restoration", result["errors"])

    def test_unstoppable_test_child_blocks_wallpaper_and_service_restore(self):
        record = {**self.record, "daemon_pid": 99, "daemon_start": "old"}
        with patch.object(live, "stop_test_daemon", side_effect=RuntimeError("Test daemon did not stop")):
            result = live.restore(record)
        self.assertFalse(result["restored"])
        live.run.assert_not_called()

    def test_guardian_acknowledges_then_waits_for_eof_without_timer(self):
        path = self.root / "record.json"
        live.write_json(path, self.record)
        order = []
        with (
            patch.object(live.os, "write", side_effect=lambda fd, data: order.append("ready")),
            patch.object(live.os, "close"),
            patch.object(live.os, "read", side_effect=lambda fd, size: order.append("eof") or b""),
            patch.object(live.select, "select", side_effect=AssertionError("No guardian timer")),
        ):
            live.guardian(path, 901, 902)
        self.assertEqual(order, ["ready", "eof"])

    def test_guardian_failure_retains_armed_marker_and_reports_exception(self):
        path = self.root / "record.json"
        live.write_json(path, self.record)
        (self.root / "restore-armed").touch()
        with patch.object(live.os, "read", return_value=b""), patch.object(live, "restore", side_effect=OSError("fixture failure")):
            live.guardian(path, 901)
        result = json.loads((self.root / "guardian-restoration.json").read_text())
        self.assertFalse(result["restored"])
        self.assertIn("fixture failure", result["errors"][0])
        self.assertTrue((self.root / "restore-armed").exists())

    def test_restoration_lock_is_acquired_before_touching_process_or_desktop(self):
        order = []
        with (
            patch.object(live.fcntl, "flock", side_effect=lambda fd, mode: order.append("lock")),
            patch.object(live, "restore_locked", side_effect=lambda record: order.append("restore") or {"restored": True}),
        ):
            live.restore(self.record)
        self.assertEqual(order, ["lock", "restore"])


if __name__ == "__main__":
    unittest.main()
