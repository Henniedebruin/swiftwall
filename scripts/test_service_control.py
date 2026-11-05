"""Service regressions: fake unit commands and a temp-only release integration test.

The integration test runs only the repository release binary with a cleared
session environment and a fake setter; no real service manager is executed.
"""

import copy
import json
import os
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest import mock

import install_support as support
import service_control as control


UNIT = "swiftwall.service"
# Deliberately nonexistent: the injected runner must never execute this path.
BINARY = Path("/nonexistent/swiftwall-test-binary")
VERSION = "0.2.0"


def probe(binary=BINARY, protocol=1):
    if protocol is None:
        return [str(binary), "status"]
    return [str(binary), "request", json.dumps({
        "v": protocol, "id": "deployment-health", "op": "status",
    })]


def state(**changes):
    result = dict(LoadState="loaded", ActiveState="active", SubState="running",
                  MainPID="123", NRestarts="0", Result="success", UnitFileState="disabled")
    result.update(changes)
    return result


def reply(version=VERSION, **status):
    # v0.2 metadata wraps the same status object used by v0.1.
    return dict(ok=True, protocol_version=1, daemon_version=version,
                status=dict(scanning=False, persistence_pending=False, **status))


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, delay):
        if not 0 < delay <= .1:
            raise AssertionError(f"Unexpected polling delay: {delay}")
        self.sleeps.append(delay)
        self.now += delay
        if len(self.sleeps) > 100:
            raise AssertionError("Unbounded readiness loop")


class ScriptedRunner:
    """Consume exact expected commands; never delegate to a real subprocess."""

    def __init__(self, test):
        self.test = test
        self.steps = []
        self.calls = []

    def expect(self, args, stdout="", stderr="", code=0, timeout=10, error=None):
        self.steps.append((list(args), stdout, stderr, code, timeout, error))

    def manager(self, action, **kwargs):
        self.expect(["systemctl", "--user", action, UNIT], **kwargs)

    def show(self, value, timeout=3):
        self.expect(["systemctl", "--user", "show", UNIT,
                     "-p", "LoadState", "-p", "ActiveState", "-p", "SubState",
                     "-p", "MainPID", "-p", "NRestarts", "-p", "Result",
                     "-p", "UnitFileState"],
                    stdout="\n".join(f"{key}={item}" for key, item in value.items()),
                    timeout=timeout)

    def status(self, value=None, *, binary=BINARY, protocol=1, **kwargs):
        self.expect(probe(binary, protocol),
                    stdout=json.dumps(reply() if value is None else value),
                    timeout=kwargs.pop("timeout", 2), **kwargs)

    def version(self, version=VERSION, *, binary=BINARY, **kwargs):
        self.expect([str(binary), "--version"], stdout=f"swiftwall {version}\n",
                    timeout=kwargs.pop("timeout", 2), **kwargs)

    def __call__(self, args, *, capture_output, text, timeout):
        self.test.assertTrue(capture_output)
        self.test.assertTrue(text)
        self.test.assertTrue(self.steps, f"Unexpected command: {args}")
        expected, stdout, stderr, code, bound, error = self.steps.pop(0)
        self.test.assertEqual(args, expected)
        self.test.assertAlmostEqual(timeout, bound)
        self.calls.append(list(args))
        if error is not None:
            raise error
        return subprocess.CompletedProcess(args, code, stdout, stderr)

    def assert_done(self):
        self.test.assertEqual(self.steps, [], "Expected commands were not issued")


class ServiceControlTests(unittest.TestCase):
    def setUp(self):
        # Fail closed even if a future implementation bypasses runner injection.
        guard = mock.patch.object(subprocess, "Popen",
                                  side_effect=AssertionError("Live subprocess forbidden"))
        self.process = guard.start()
        self.addCleanup(guard.stop)
        self.addCleanup(self.process.assert_not_called)
        sleeper = mock.patch.object(control.time, "sleep",
                                    side_effect=AssertionError("Real sleep forbidden"))
        sleeper.start()
        self.addCleanup(sleeper.stop)
        self.fake = FakeTime()
        self.runner = ScriptedRunner(self)
        self.addCleanup(self.runner.assert_done)
        self.service = control.UserService(runner=self.runner, clock=self.fake.clock,
                                           sleep=self.fake.sleep)

    def assert_error(self, category, operation, detail=None):
        with self.assertRaises(control.ServiceError) as caught:
            operation()
        self.assertEqual(caught.exception.category, category)
        if detail is not None:
            self.assertIn(detail, str(caught.exception))
        return caught.exception

    def ready(self, **kwargs):
        return self.service.wait_ready(BINARY, version=VERSION, protocol=1, **kwargs)

    def test_stop_preserves_enabled_and_disabled_without_reset(self):
        for enabled in ("enabled", "disabled"):
            for active in ("inactive", "failed"):
                with self.subTest(enabled=enabled, active=active):
                    self.runner.manager("stop", timeout=30)
                    self.runner.show(state(ActiveState=active, MainPID="0",
                                           UnitFileState=enabled), timeout=5)
                    self.service.stop()
        self.runner.assert_done()

    def test_stop_rejects_running_state_or_remaining_pid(self):
        for changes in ({"ActiveState": "active", "MainPID": "0"},
                        {"ActiveState": "inactive", "MainPID": "123"}):
            with self.subTest(changes=changes):
                self.runner.manager("stop", timeout=30)
                self.runner.show(state(**changes), timeout=5)
                self.assert_error("shutdown-failure", self.service.stop)

    def test_inactive_and_unloaded_start_skip_reset_failed(self):
        for load in ("loaded", "not-found", "unloaded"):
            with self.subTest(load=load):
                self.runner.show(state(LoadState=load, ActiveState="inactive", MainPID="0"), timeout=5)
                self.runner.manager("start", timeout=15)
                self.service.start()

    def test_only_loaded_failed_start_resets_failed(self):
        for load in ("loaded", "not-found", "unloaded"):
            with self.subTest(load=load):
                self.runner.show(state(LoadState=load, ActiveState="failed", MainPID="0"), timeout=5)
                if load == "loaded":
                    self.runner.manager("reset-failed")
                self.runner.manager("start", timeout=15)
                self.service.start()

    def test_start_refuses_active_or_transitional_service(self):
        for active in ("active", "activating", "deactivating", "reloading"):
            with self.subTest(active=active):
                self.runner.show(state(ActiveState=active), timeout=5)
                self.assert_error("unsafe-start", self.service.start)

    def test_activation_failure_is_not_retried(self):
        self.runner.show(state(ActiveState="inactive", MainPID="0"), timeout=5)
        self.runner.manager("start", timeout=15, code=1, stderr="Start request repeated too quickly")
        self.assert_error("activation-failure", self.service.start, "Start request repeated")
        self.assertEqual(self.fake.sleeps, [])

    def test_reset_failure_prevents_start(self):
        self.runner.show(state(ActiveState="failed", MainPID="0"), timeout=5)
        self.runner.manager("reset-failed", code=1, stderr="manager unavailable")
        self.assert_error("manager-failure", self.service.start, "manager unavailable")

    def test_delayed_socket_then_success_is_bounded(self):
        for remaining, diagnostic in ((.35, dict(stderr="Daemon unavailable: no socket")),
                                      (.25, dict(stdout="Daemon unavailable: connection refused"))):
            self.runner.show(state(), timeout=remaining)
            self.runner.expect(probe(), code=1,
                               timeout=remaining, **diagnostic)
        self.runner.show(state(), timeout=.15)
        self.runner.status(timeout=.15)
        self.runner.version(timeout=.15)
        self.assertEqual(self.ready(timeout=.35), reply())
        self.assertAlmostEqual(self.fake.now, .2)
        self.assertEqual(self.fake.sleeps, [.1, .1])

    def test_socket_timeout_stops_at_deadline(self):
        for remaining in (.25, .15, .05):
            self.runner.show(state(), timeout=remaining)
            self.runner.expect(probe(), code=1,
                               stderr="Daemon unavailable: missing socket", timeout=remaining)
        self.assert_error("readiness-timeout", lambda: self.ready(timeout=.25), "missing socket")
        self.assertAlmostEqual(self.fake.now, .25)
        self.assertEqual(len(self.fake.sleeps), 3)

    def test_v01_readiness_without_protocol_metadata(self):
        legacy = {"ok": True, "status": {"scanning": False, "paused": True,
                                          "error": None, "history": ["previous.jpg"]}}
        self.runner.show(state())
        self.runner.status(legacy, protocol=None)
        self.runner.version("0.1.0")
        self.assertEqual(self.service.wait_ready(BINARY, version="0.1.0"), legacy)
        self.assertEqual(self.fake.sleeps, [])

    def test_v02_readiness_checks_protocol_and_binary_version(self):
        self.runner.show(state())
        self.runner.status()
        self.runner.version()
        self.assertEqual(self.ready(), reply())

    def test_protocol_health_uses_structured_request_never_legacy_status(self):
        self.runner.show(state())
        self.runner.status()
        self.runner.version()
        self.ready()
        self.assertNotIn([str(BINARY), "status"], self.runner.calls)
        requests = [args for args in self.runner.calls if args[1] == "request"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(requests[0]), 3)  # JSON is one argv element, not shell input.
        self.assertEqual(json.loads(requests[0][2]),
                         {"v": 1, "id": "deployment-health", "op": "status"})

    def test_initial_scan_and_persistence_pending_then_ready(self):
        for key in ("scanning", "persistence_pending"):
            pending = reply()
            pending["status"][key] = True
            self.runner.show(state())
            self.runner.status(pending)
        self.runner.show(state())
        self.runner.status()
        self.runner.version()
        self.assertEqual(self.ready(), reply())
        self.assertEqual(self.fake.sleeps, [.1, .1])

    def test_initial_scan_pending_times_out_without_version_probe(self):
        pending = reply()
        pending["status"]["scanning"] = True
        self.runner.show(state(), timeout=.1)
        self.runner.status(pending, timeout=.1)
        self.assert_error("readiness-timeout", lambda: self.ready(timeout=.1),
                          "initial scan/persistence pending")
        self.assertAlmostEqual(self.fake.now, .1)

    def test_daemon_exit_and_auto_restart_abort_before_cli(self):
        for changes in ({"ActiveState": "failed"}, {"ActiveState": "inactive"},
                        {"ActiveState": "deactivating"}, {"SubState": "auto-restart"}):
            with self.subTest(changes=changes):
                self.runner.show(state(**changes))
                self.assert_error("daemon-exit", self.ready)

    def test_restart_counter_or_pid_change_aborts_without_retry(self):
        for changes in ({"NRestarts": "1"}, {"MainPID": "456"}):
            with self.subTest(changes=changes):
                self.runner.show(state())
                self.runner.expect(probe(), code=1,
                                   stderr="Daemon unavailable", timeout=2)
                self.runner.show(state(**changes))
                self.assert_error("daemon-restarted", self.ready)

    def test_zero_pid_may_become_ready_during_activation(self):
        self.runner.show(state(ActiveState="activating", MainPID="0"))
        self.runner.expect(probe(), code=1,
                           stderr="Daemon unavailable", timeout=2)
        self.runner.show(state())
        self.runner.status()
        self.runner.version()
        self.assertEqual(self.ready(), reply())

    def test_ready_reply_requires_active_service_and_nonzero_pid(self):
        for changes in ({"MainPID": "0"}, {"ActiveState": "activating"}):
            with self.subTest(changes=changes):
                self.runner.show(state(**changes))
                self.runner.status()
                self.runner.version()
                self.assert_error("daemon-unhealthy", self.ready)

    def test_protocol_or_daemon_version_mismatch(self):
        for changes in ({"protocol_version": 2}, {"daemon_version": "0.1.0"},
                        {"protocol_version": None}, {"daemon_version": None}):
            with self.subTest(changes=changes):
                value = reply()
                value.update(changes)
                self.runner.show(state())
                self.runner.status(value)
                self.assert_error("protocol-incompatible", self.ready)

    def test_legacy_reply_rejected_when_protocol_requested(self):
        self.runner.show(state())
        self.runner.status({"ok": True, "status": {"scanning": False}})
        self.assert_error("protocol-incompatible", self.ready)

    def test_malformed_or_unsuccessful_cli_reply(self):
        for output in ("not json", "null", "[]", "{}",
                       '{"ok":false,"status":{}}', '{"ok":true,"status":[]}'):
            with self.subTest(output=output):
                self.runner.show(state())
                self.runner.expect(probe(), stdout=output, timeout=2)
                self.assert_error("protocol-incompatible", self.ready)

    def test_installed_binary_version_mismatch_or_failure(self):
        for version, code in (("0.1.0", 0), (VERSION, 1)):
            with self.subTest(version=version, code=code):
                self.runner.show(state())
                self.runner.status()
                self.runner.version(version, code=code)
                self.assert_error("version-incompatible", self.ready)

    def test_daemon_health_errors_are_not_readiness_delays(self):
        for key in ("error", "last_persistence_error", "config_worker_error"):
            with self.subTest(key=key):
                self.runner.show(state())
                self.runner.status(reply(**{key: "injected failure"}))
                self.assert_error("daemon-unhealthy", self.ready, "injected failure")
        self.assertEqual(self.fake.sleeps, [])

    def test_unexpected_cli_error_is_not_retried(self):
        self.runner.show(state())
        self.runner.expect(probe(), code=2,
                           stderr="permission denied", stdout="unexpected CLI error", timeout=2)
        self.assert_error("ipc-failure", self.ready, "permission denied")
        self.assertEqual(self.fake.sleeps, [])

    def test_manager_query_failure_prevents_cli(self):
        args = ["systemctl", "--user", "show", UNIT,
                "-p", "LoadState", "-p", "ActiveState", "-p", "SubState",
                "-p", "MainPID", "-p", "NRestarts", "-p", "Result", "-p", "UnitFileState"]
        self.runner.expect(args, code=1, stderr="No user manager", timeout=3)
        self.assert_error("manager-failure", self.ready, "No user manager")

    def test_cli_command_timeout_is_not_retried(self):
        self.runner.show(state(), timeout=.5)
        self.runner.expect(probe(), timeout=.5,
                           error=subprocess.TimeoutExpired(probe(), .5))
        self.assert_error("command-timeout", lambda: self.ready(timeout=.5))
        self.assertEqual(self.fake.sleeps, [])

    def test_global_expiry_during_manager_or_cli_is_readiness_timeout(self):
        for phase in ("manager", "status", "version"):
            with self.subTest(phase=phase):
                self.fake.now = 0
                self.runner.calls.clear()
                self.runner.show(state(), timeout=.25)
                if phase != "manager":
                    self.runner.status(timeout=.25)
                if phase == "version":
                    self.runner.version(timeout=.25)
                expected_calls = {"manager": 1, "status": 2, "version": 3}[phase]

                def expire(args, **kwargs):
                    result = self.runner(args, **kwargs)
                    if len(self.runner.calls) == expected_calls:
                        self.fake.now += kwargs["timeout"]
                        raise subprocess.TimeoutExpired(args, kwargs["timeout"])
                    return result

                self.service.runner = expire
                error = self.assert_error("readiness-timeout", lambda: self.ready(timeout=.25))
                self.assertIsInstance(error.__cause__, control.ServiceError)
                self.assertEqual(error.__cause__.category, "command-timeout")
                self.assertIsInstance(error.__cause__.__cause__, subprocess.TimeoutExpired)
                self.assertEqual(len(self.runner.calls), expected_calls)
                self.runner.assert_done()
                self.assertEqual(self.fake.now, .25)
                self.assertEqual(self.fake.sleeps, [])

    def test_early_manager_or_cli_timeout_remains_command_timeout(self):
        for phase in ("manager", "status", "version"):
            with self.subTest(phase=phase):
                self.fake.now = 0
                self.runner.calls.clear()
                self.runner.show(state(), timeout=3)
                if phase != "manager":
                    self.runner.status(timeout=2)
                if phase == "version":
                    self.runner.version(timeout=2)
                expected_calls = {"manager": 1, "status": 2, "version": 3}[phase]

                def expire_early(args, **kwargs):
                    result = self.runner(args, **kwargs)
                    if len(self.runner.calls) == expected_calls:
                        self.fake.now += kwargs["timeout"]
                        raise subprocess.TimeoutExpired(args, kwargs["timeout"])
                    return result

                self.service.runner = expire_early
                error = self.assert_error("command-timeout", lambda: self.ready(timeout=10))
                self.assertIsInstance(error.__cause__, subprocess.TimeoutExpired)
                self.assertLess(self.fake.now, 10)
                self.assertEqual(len(self.runner.calls), expected_calls)
                self.runner.assert_done()
                self.assertEqual(self.fake.sleeps, [])

    def test_remaining_budget_is_recomputed_before_each_cli_call(self):
        self.runner.show(state(), timeout=.5)
        self.runner.status(timeout=.25)
        self.runner.version(timeout=.125)
        elapsed = iter((.25, .125, .0625))

        def delayed(args, **kwargs):
            result = self.runner(args, **kwargs)
            self.fake.now += next(elapsed)
            return result

        self.service.runner = delayed
        self.assertEqual(self.ready(timeout=.5), reply())
        self.assertEqual(self.fake.now, .4375)
        self.assertEqual(len(self.runner.calls), 3)
        self.assertEqual(self.fake.sleeps, [])

    def test_stop_command_timeout_is_bounded(self):
        self.runner.manager("stop", timeout=30,
                            error=subprocess.TimeoutExpired("systemctl", 30))
        self.assert_error("command-timeout", self.service.stop)

    def test_temporary_home_owned_upgrade_rollback_preserves_data_and_enablement(self):
        for enabled in ("enabled", "disabled"):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory(prefix="swiftwall-service-test-") as root:
                root = Path(root)
                home = root / "home"
                home.mkdir()
                source = root / "synthetic-binary"
                source.write_bytes(b"synthetic v0.1; not executable code\n")
                config = home / ".config/swiftwall/config.json"
                persisted = home / ".local/state/swiftwall/state.json"
                for path, data in ((config, b'{"wallpaper_dir":"/not-real","interval":600}\n'),
                                   (persisted, b'{"paused":true,"history":["old.jpg","current.jpg"]}\n')):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                    path.chmod(0o600)
                protected = {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                             for path in (config, persisted)}
                wants = home / ".config/systemd/user/graphical-session.target.wants/swiftwall.service"
                if enabled == "enabled":
                    wants.parent.mkdir(parents=True)
                    wants.symlink_to("../swiftwall.service")

                def preserved():
                    for path, before in protected.items():
                        self.assertEqual((path.read_bytes(), stat.S_IMODE(path.stat().st_mode)), before)
                    self.assertEqual(wants.is_symlink(), enabled == "enabled")
                    if enabled == "enabled":
                        self.assertEqual(str(wants.readlink()), "../swiftwall.service")
                    else:
                        self.assertFalse(wants.exists())

                def stop():
                    self.runner.manager("stop", timeout=30)
                    self.runner.show(state(ActiveState="inactive", MainPID="0",
                                           UnitFileState=enabled), timeout=5)
                    self.service.stop()
                    preserved()

                def start_ready(version, protocol):
                    binary = home / support.FILES["binary"]
                    self.runner.show(state(ActiveState="inactive", MainPID="0",
                                           UnitFileState=enabled), timeout=5)
                    self.runner.manager("start", timeout=15)
                    self.service.start()
                    value = reply(version)
                    if protocol is None:
                        value.pop("protocol_version")
                        value.pop("daemon_version")
                    self.runner.show(state(UnitFileState=enabled))
                    self.runner.status(value, binary=binary, protocol=protocol)
                    self.runner.version(version, binary=binary)
                    self.assertEqual(self.service.wait_ready(binary, version=version, protocol=protocol), value)
                    self.runner.show(state(UnitFileState=enabled), timeout=5)
                    self.assertEqual(self.service.state()["UnitFileState"], enabled)
                    preserved()

                support.install(home, source)
                original = copy.deepcopy(support.load_manifest(home)["current"])
                installed = home / support.FILES["binary"]
                self.assertEqual(installed.read_bytes(), source.read_bytes())
                self.assertFalse(installed.is_symlink())
                self.assertNotEqual(installed.stat().st_ino, source.stat().st_ino)
                self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o755)
                start_ready("0.1.0", None)
                stop()
                source.write_bytes(b"synthetic v0.2; not executable code\n")
                support.install(home, source)
                manifest = support.load_manifest(home)
                support.verify_owned(home, manifest)
                self.assertEqual(manifest["previous"], original)
                self.assertEqual(installed.read_bytes(), source.read_bytes())
                start_ready(VERSION, 1)
                stop()
                support.rollback(home)
                restored = support.load_manifest(home)
                self.assertEqual(restored["current"], original)
                self.assertIsNone(restored["previous"])
                support.verify_owned(home, restored)
                self.assertEqual(installed.read_bytes(), b"synthetic v0.1; not executable code\n")
                start_ready("0.1.0", None)
                preserved()

    def test_temporary_home_modified_owned_binary_refuses_install_and_rollback(self):
        with tempfile.TemporaryDirectory(prefix="swiftwall-ownership-test-") as root:
            root = Path(root)
            home = root / "home"
            home.mkdir()
            source = root / "synthetic-binary"
            source.write_bytes(b"synthetic first")
            support.install(home, source)
            source.write_bytes(b"synthetic second")
            support.install(home, source)
            installed = home / support.FILES["binary"]
            installed.write_bytes(b"user modification")
            before = {path.relative_to(home): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                      for path in home.rglob("*") if path.is_file()}
            for operation in (lambda: support.install(home, source), lambda: support.rollback(home)):
                with self.assertRaisesRegex(support.InstallError, "modified path"):
                    operation()
                self.assertEqual({path.relative_to(home): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                                  for path in home.rglob("*") if path.is_file()}, before)
        self.assertEqual(self.runner.calls, [])


class ReleaseIntegrationTests(unittest.TestCase):
    def test_release_legacy_status_and_structured_health_are_isolated(self):
        binary = Path(__file__).resolve().parent.parent / "target/release/swiftwall"
        if not binary.is_file():
            self.skipTest("Release binary missing: target/release/swiftwall (not built by this test)")
        self.assertTrue(os.access(binary, os.X_OK), "Release binary is not executable")

        with tempfile.TemporaryDirectory(prefix="sw-health-") as directory:
            root = Path(directory).resolve()
            for name in ("home", "config", "state", "runtime", "xdg-runtime",
                         "cache", "data", "desktop", "walls", "bin"):
                (root / name).mkdir(mode=0o700)

            # A real, tiny PNG, without an imaging dependency or desktop access.
            def chunk(kind, data):
                return (struct.pack(">I", len(data)) + kind + data
                        + struct.pack(">I", zlib.crc32(kind + data)))

            png = (b"\x89PNG\r\n\x1a\n"
                   + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                   + chunk(b"IDAT", zlib.compress(b"\x00\x13\x2f\x53"))
                   + chunk(b"IEND", b""))
            image = root / "walls/test.png"
            image.write_bytes(png)
            background = root / "desktop/background"
            background.symlink_to(image)
            config = root / "config/config.toml"
            config.write_text('interval_seconds = 3600\nselection = "shuffle"\n'
                              f'folders = [{json.dumps(str(root / "walls"))}]\n')
            config_before = config.read_bytes()
            setter = root / "bin/fake-setter"
            setter.write_text(
                f"#!{sys.executable}\n"
                "import os, pathlib, sys\n"
                "root = pathlib.Path(os.environ['TEST_ROOT']).resolve()\n"
                "link = pathlib.Path(os.environ['SWIFTWALL_BACKGROUND_LINK'])\n"
                "assert len(sys.argv) == 5 and sys.argv[1:4] == ['theme', 'bg', 'set']\n"
                "target = pathlib.Path(sys.argv[4]).resolve(strict=True)\n"
                "assert link == root / 'desktop/background'\n"
                "assert link.parent.resolve() == root / 'desktop'\n"
                "assert target.is_relative_to(root / 'walls') and target.is_file()\n"
                "with (root / 'setter-calls').open('a') as stream:\n"
                "    stream.write(str(target) + '\\n')\n"
                "link.unlink(missing_ok=True)\n"
                "link.symlink_to(target)\n")
            setter.chmod(0o700)
            # No inherited desktop session, bus address, PATH, or Swiftwall overrides.
            env = {
                "PATH": str(root / "bin"), "HOME": str(root / "home"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_RUNTIME_DIR": str(root / "xdg-runtime"),
                "SWIFTWALL_RUNTIME_DIR": str(root / "runtime"),
                "SWIFTWALL_CONFIG": str(config),
                "SWIFTWALL_STATE_DIR": str(root / "state"),
                "SWIFTWALL_BACKGROUND_LINK": str(background),
                "SWIFTWALL_OMARCHY_COMMAND": str(setter), "TEST_ROOT": str(root),
            }
            real_popen = subprocess.Popen
            calls = []

            def isolated_popen(args, **kwargs):
                self.assertIsInstance(args, list)
                self.assertEqual(args[0], str(binary))
                self.assertIn(args[1:], (["daemon"], ["status"], ["--version"],
                                        probe(binary)[1:]))
                self.assertEqual(kwargs.get("env"), env)
                self.assertEqual(kwargs.get("cwd"), root)
                self.assertFalse(kwargs.get("shell", False))
                calls.append(list(args))
                return real_popen(args, **kwargs)

            with mock.patch.object(subprocess, "Popen", side_effect=isolated_popen), \
                    (root / "daemon.log").open("w+") as log:
                daemon = subprocess.Popen([str(binary), "daemon"], env=env, cwd=root,
                                          stdin=subprocess.DEVNULL, stdout=log, stderr=log)
                try:
                    manager_calls = []

                    def runner(args, **kwargs):
                        if args[0] == "systemctl":
                            self.assertEqual(args, [
                                "systemctl", "--user", "show", UNIT,
                                "-p", "LoadState", "-p", "ActiveState", "-p", "SubState",
                                "-p", "MainPID", "-p", "NRestarts", "-p", "Result",
                                "-p", "UnitFileState"])
                            self.assertIsNone(daemon.poll(), "Isolated daemon exited")
                            manager_calls.append(list(args))
                            simulated = state(MainPID=str(daemon.pid))
                            return subprocess.CompletedProcess(
                                args, 0, "\n".join(f"{k}={v}" for k, v in simulated.items()), "")
                        return subprocess.run(args, env=env, cwd=root,
                                              stdin=subprocess.DEVNULL, **kwargs)

                    version_result = runner([str(binary), "--version"], capture_output=True,
                                            text=True, timeout=3)
                    self.assertEqual(version_result.returncode, 0, version_result.stderr)
                    self.assertRegex(version_result.stdout.strip(), r"^swiftwall 0\.2\.\d+(?:[-+][\w.-]+)?$")
                    installed_version = version_result.stdout.strip().split()[1]
                    calls.clear()
                    service = control.UserService(runner=runner, clock=time.monotonic,
                                                  sleep=time.sleep)
                    healthy = service.wait_ready(binary, version=installed_version, protocol=1, timeout=10)
                    self.assertTrue(healthy["ok"])
                    self.assertEqual(healthy["protocol_version"], 1)
                    self.assertEqual(healthy["daemon_version"], installed_version)
                    self.assertEqual(healthy["id"], "deployment-health")
                    self.assertFalse(healthy["status"]["scanning"])
                    self.assertFalse(healthy["status"]["persistence_pending"])
                    self.assertTrue(manager_calls)
                    self.assertIn(probe(binary), calls)
                    self.assertIn([str(binary), "--version"], calls)
                    # Crucially, no legacy status invocation during protocol health.
                    self.assertNotIn([str(binary), "status"], calls)

                    legacy_result = runner([str(binary), "status"], capture_output=True,
                                           text=True, timeout=3)
                    self.assertEqual(legacy_result.returncode, 0, legacy_result.stderr)
                    legacy = json.loads(legacy_result.stdout)
                    self.assertTrue(legacy["ok"])
                    self.assertIsInstance(legacy["status"], dict)
                    for key in ("protocol_version", "daemon_version", "id"):
                        self.assertNotIn(key, legacy)
                    version_result = runner([str(binary), "--version"], capture_output=True,
                                            text=True, timeout=3)
                    self.assertEqual(version_result.returncode, 0, version_result.stderr)
                    self.assertEqual(version_result.stdout.strip(), f"swiftwall {installed_version}")
                    self.assertEqual(config.read_bytes(), config_before)
                    self.assertTrue(background.resolve().is_relative_to(root / "walls"))
                except Exception:
                    log.flush()
                    log.seek(0)
                    print("Isolated daemon log:\n" + log.read(), file=sys.stderr)
                    raise
                finally:
                    if daemon.poll() is None:
                        daemon.terminate()
                    try:
                        daemon.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        daemon.kill()
                        daemon.wait(timeout=5)
                self.assertEqual(daemon.returncode, 0, "Isolated daemon did not shut down cleanly")
                self.assertFalse((root / "runtime/control.sock").exists())


if __name__ == "__main__":
    unittest.main()
