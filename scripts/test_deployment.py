"""Harness safety tests: mocks/sentinels and private file-only installers, never systemd."""

import argparse

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import deployment as coordinator
import rehearse_deployment as rehearsal
import plugin_activation
import plugin_support


class HarnessSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        args = argparse.Namespace(v01=self.root / "v01", v02=self.root / "v02",
                                  plugin_source=self.root)
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root)}):
            self.fixture = rehearsal.Rehearsal(args, "success", self.root)
        self.addCleanup(lambda: rehearsal.shutil.rmtree(self.fixture.root, ignore_errors=True))

    def test_environment_is_not_inherited(self):
        with patch.dict(os.environ, {"SWIFTWALL_CONFIG": "/production/config",
                                     "SWIFTWALL_EXTRA": "must-not-leak", "DISPLAY": ":1"}):
            env = rehearsal.engine_environment(self.root)
        self.assertNotIn("SWIFTWALL_EXTRA", env)
        self.assertNotIn("DISPLAY", env)
        self.assertTrue(Path(env["SWIFTWALL_CONFIG"]).is_relative_to(self.root))
        self.assertTrue(all(Path(value).is_relative_to(self.root) for key, value in env.items()
                            if key.startswith(("SWIFTWALL_", "XDG_")) or key == "HOME"))

    @patch.object(rehearsal.subprocess, "run")
    def test_production_commands_are_rejected_before_execution(self, run):
        for argv in (["systemctl", "--user", "stop", "swiftwall.service"],
                     ["systemctl", "stop", self.fixture.unit],
                     ["omarchy-shell", "reload"],
                     ["omarchy-shell", "shell", "listPlugins"],
                     ["omarchy", "plugin", "enable", rehearsal.PLUGIN_ID,
                      "--section", "right", "--index", "0"],
                     [str(self.fixture.root / "fake-omarchy"), "plugin", "disable", rehearsal.PLUGIN_ID],
                     ["/production/swiftwall", "status"],
                     ["journalctl", "--user", "--unit", "swiftwall.service"],
                     ["python3", "-B", str(rehearsal.SCRIPTS / "install.py"),
                      "--home", "/production", "--binary", "/tmp/anything"]):
            with self.subTest(argv=argv), self.assertRaises(RuntimeError):
                self.fixture.raw(argv)
        run.assert_not_called()

    @patch.object(rehearsal.subprocess, "run")
    def test_cli_gets_exact_isolated_environment(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "{}", "")
        self.fixture.raw([self.fixture.binary, "status"], env={"SWIFTWALL_CONFIG": "/bad"})
        self.assertEqual(run.call_args.kwargs["env"], self.fixture.env)
        self.assertLessEqual(run.call_args.kwargs["timeout"], 35)

    @patch.object(rehearsal.subprocess, "run")
    def test_manager_preserves_session_but_not_swiftwall_environment(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        self.fixture.raw(["systemctl", "--user", "show", self.fixture.unit])
        self.assertEqual(run.call_args.kwargs["env"], self.fixture.manager_env)
        self.assertFalse(any(key.startswith("SWIFTWALL_") for key in self.fixture.manager_env))

    def test_ipc_fault_changes_actual_request_once_not_output(self):
        self.fixture.case = "ipc"
        request = [str(self.fixture.binary), "request", json.dumps({"v": 1, "op": "status"})]
        response = subprocess.CompletedProcess([], 1, '{"ok":false}', "")
        with patch.object(rehearsal, "digest", return_value=rehearsal.V02), \
                patch.object(self.fixture, "raw", return_value=response) as raw:
            self.assertIs(self.fixture.runner(request), response)
            self.assertEqual(json.loads(raw.call_args.args[0][2])["v"], 999)
            self.fixture.runner(request)
            self.assertEqual(json.loads(raw.call_args.args[0][2])["v"], 1)

    def test_installer_fault_uses_real_cli_arguments_after_backup_only(self):
        self.fixture.case = "installer"
        self.fixture.deployment = argparse.Namespace(backup=None)
        argv = ["python3", "-B", str(rehearsal.SCRIPTS / "install.py"),
                "--home", str(self.fixture.home), "--binary", str(self.fixture.args.v02)]
        with patch.object(self.fixture, "raw") as raw:
            with self.assertRaises(RuntimeError):
                self.fixture.runner(argv)
            raw.assert_not_called()
            self.fixture.deployment.backup = self.root
            self.fixture.runner(argv)
            actual = raw.call_args.args[0]
            self.assertEqual(actual[-1], str(self.fixture.root / "nonexistent-v02"))
            self.assertEqual(actual[:-1], argv[:-1])

    def test_startup_fault_armed_only_after_successful_install(self):
        self.fixture.case = "startup"
        argv = ["python3", "-B", str(rehearsal.SCRIPTS / "install.py"),
                "--home", str(self.fixture.home), "--binary", str(self.fixture.args.v02)]
        with patch.object(self.fixture, "raw", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                patch.object(self.fixture, "fault_override") as override:
            self.fixture.runner([*argv, "--dry-run"])
            override.assert_not_called()
            self.fixture.runner(argv)
            override.assert_called_once_with("/usr/bin/false")

    def test_override_cleared_before_rollback_stop(self):
        self.fixture.fault_active = True
        order = []
        with patch.object(self.fixture, "fault_override", side_effect=lambda: order.append("clear")), \
                patch.object(self.fixture, "raw", side_effect=lambda *a, **kw: order.append("stop")):
            self.fixture.runner(["systemctl", "--user", "stop", self.fixture.unit])
        self.assertEqual(order, ["clear", "stop"])

    def test_absolute_existing_and_cases_validation(self):
        self.assertEqual(rehearsal.absolute_existing(str(self.root)), self.root)
        for value in ("relative", str(self.root / "missing")):
            with self.assertRaises(argparse.ArgumentTypeError):
                rehearsal.absolute_existing(value)
        for value in ("success,success", "production", ""):
            with self.assertRaises(argparse.ArgumentTypeError):
                rehearsal.selected_cases(value)
        self.assertIn("success-disabled", rehearsal.CASES)
        self.assertIn("stale-socket", rehearsal.CASES)

    def test_unit_escaping_is_not_shell_quoting(self):
        self.assertEqual(rehearsal.unit_quote('/a b/100%"\\'), '"/a b/100%%\\"\\\\"')
        self.assertEqual(rehearsal.env_quote("100%"), '"100%"')
        with self.assertRaises(RuntimeError):
            rehearsal.unit_quote("/path\nExecStart=bad")

    def make_launcher(self):
        fixture = self.fixture
        fixture.binary.parent.mkdir(parents=True)
        (fixture.root / "images").mkdir()
        (fixture.root / "images/0.png").write_bytes(rehearsal.png())
        (fixture.root / "background").symlink_to(fixture.root / "images/0.png")
        (fixture.root / "setter").write_text("fixture setter only")
        self.marker = fixture.root / "sentinel.json"
        fixture.binary.write_text(
            "#!/usr/bin/python3\nimport json,os,sys\nfrom pathlib import Path\n"
            + f"Path({str(self.marker)!r}).write_text(json.dumps("
            + "{'argv':sys.argv,'env':dict(os.environ),'pid':os.getpid()}))\n")
        fixture.binary.chmod(0o700)
        return rehearsal.write_launcher(fixture.root)

    def test_path_directives_are_raw_absolute_and_launcher_is_execstart(self):
        fixture = self.fixture
        dropin = rehearsal.isolation_dropin(fixture.root)
        lines = dropin.splitlines()
        self.assertIn("EnvironmentFile=" + str(fixture.root / "engine.env"), lines)
        self.assertIn("WorkingDirectory=" + str(fixture.home), lines)
        self.assertIn('ExecStart=/usr/bin/python3 "' + str(fixture.root / "launch.py") + '"', lines)
        self.assertIn("EnvironmentFile=", lines)
        unset = next(line for line in lines if line.startswith("UnsetEnvironment="))
        self.assertEqual(unset.split("=", 1)[1].split(),
                         ["DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS"])
        self.assertFalse(set(unset.split("=", 1)[1].split()) & fixture.env.keys())
        for path in ("/tmp/swr-safe name", "/tmp/swr-safe%", '/tmp/swr-safe"',
                     "/tmp/swr-safe/../outside", "/tmp/swr-safe\n", "/outside/swr-safe"):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                rehearsal.simple_unit_path(path)

    def test_launcher_rejects_missing_and_corrupt_environment_before_exec(self):
        launcher = self.make_launcher()
        invalid = [{}, {**self.fixture.env, "SWIFTWALL_EXTRA": "/production"}]
        for key in self.fixture.env:
            missing = dict(self.fixture.env)
            del missing[key]
            invalid.extend([missing, {**self.fixture.env, key: "/not-the-fixture"}])
        for env in invalid:
            with self.subTest(env=env):
                result = subprocess.run(["/usr/bin/python3", str(launcher)], env=env,
                                        capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("rehearsal isolation guard:", result.stderr)
                self.assertFalse(self.marker.exists(), "installed sentinel must not execute")

    def test_launcher_execs_same_pid_with_only_exact_private_environment(self):
        launcher = self.make_launcher()
        env = {**self.fixture.env, "DISPLAY": ":99", "INHERITED_NOISE": "discard",
               "XDG_SESSION_TYPE": "wayland"}
        with subprocess.Popen(["/usr/bin/python3", str(launcher)], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
            stdout, stderr = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, stderr)
            saved = json.loads(self.marker.read_text())
            self.assertEqual(saved["pid"], child.pid)
        self.assertEqual(saved["env"], self.fixture.env)
        self.assertEqual(saved["argv"], [str(self.fixture.binary), "daemon"])

    def test_launcher_rejects_background_outside_fixture(self):
        launcher = self.make_launcher()
        background = self.fixture.root / "background"
        background.unlink()
        outside = self.root / "outside.png"
        outside.write_bytes(rehearsal.png())
        background.symlink_to(outside)
        result = subprocess.run(["/usr/bin/python3", str(launcher)], env=self.fixture.env,
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rehearsal isolation guard:", result.stderr)
        self.assertFalse(self.marker.exists())

    def test_environment_file_quoting_preserves_literals(self):
        # EnvironmentFile double quotes do not expand dollars, backticks or %.
        self.assertEqual(rehearsal.env_quote('a$HOME`literal`%"\\z'),
                         '"a$HOME`literal`%\\"\\\\z"')
        with self.assertRaises(RuntimeError):
            rehearsal.env_quote("bad\nvalue")

    def reserve_runtime(self):
        fixture = self.fixture
        fixture.unit_dir.mkdir(parents=True)
        fixture.unit_path.write_bytes(b"outdated runtime copy")
        fixture.target_path.write_text("[Unit]\nDescription=fixture\n")
        fixture.drop_dir.mkdir()
        (fixture.drop_dir / "10-isolation.conf").write_text(rehearsal.isolation_dropin(fixture.root))
        fixture.owned.extend([fixture.unit_path, fixture.target_path, fixture.drop_dir])

    def install_file_fixture(self):
        fixture = self.fixture
        fixture.home.mkdir()
        # These files are installer INPUTS only, never executed as engines.
        fixture.args.v01.write_bytes(b"nonexecutable file-only v01 test artifact")
        fixture.args.v02.write_bytes(b"nonexecutable file-only v02 test artifact")
        fixture.args.plugin_source = rehearsal.SCRIPTS.parent
        result = fixture.raw(["python3", "-B", str(rehearsal.SCRIPTS / "install.py"),
                              "--home", str(fixture.home), "--binary", str(fixture.args.v01)])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_runtime_sync_only_changes_target_and_reloads_on_byte_changes(self):
        self.install_file_fixture()
        self.reserve_runtime()
        fixture = self.fixture
        installed = fixture.home / ".config/systemd/user/swiftwall.service"
        dropin = (fixture.drop_dir / "10-isolation.conf").read_bytes()
        with patch.object(fixture, "reload") as reload:
            fixture.sync_runtime_unit()
            reload.assert_called_once_with()
            expected = installed.read_bytes().replace(b"graphical-session.target", fixture.target.encode())
            self.assertEqual(fixture.unit_path.read_bytes(), expected)
            fixture.sync_runtime_unit()
            self.assertEqual(reload.call_count, 1)
            installed.write_bytes(installed.read_bytes() + b"\n# newly installed template\n")
            fixture.sync_runtime_unit()
            self.assertEqual(reload.call_count, 2)
            self.assertEqual(fixture.unit_path.read_bytes(), expected + b"\n# newly installed template\n")
        self.assertEqual((fixture.drop_dir / "10-isolation.conf").read_bytes(), dropin)

    def test_real_engine_postcommit_wrapper_and_real_rollback_both_sync(self):
        self.install_file_fixture()
        self.reserve_runtime()
        fixture = self.fixture
        fixture.case = "engine-postcommit"
        fixture.deployment = argparse.Namespace(backup=fixture.root)
        argv = ["python3", "-B", str(rehearsal.SCRIPTS / "install.py"), "--home", str(fixture.home),
                "--binary", str(fixture.args.v02)]
        with patch.object(fixture, "reload") as reload:
            result = fixture.runner(argv)
            self.assertEqual(result.returncode, 74)
            self.assertEqual(fixture.binary.read_bytes(), fixture.args.v02.read_bytes())
            self.assertIn("committed engine-postcommit", result.stdout)
            reload.assert_called_once_with()
            marker = json.loads((fixture.root / "fault-engine-postcommit-commit.json").read_text())
            self.assertEqual(marker["child_returncode"], 0)
            self.assertEqual(marker["argv"][2:], argv[2:])
            child = json.loads((fixture.root / "fault-engine-postcommit-child.json").read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(child["pid"], 0)
            fixture.unit_path.write_bytes(b"old v02 runtime template")
            result = fixture.runner(["python3", "-B", str(rehearsal.SCRIPTS / "install.py"),
                                     "--home", str(fixture.home), "--rollback"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(fixture.binary.read_bytes(), fixture.args.v01.read_bytes())
            self.assertEqual(reload.call_count, 2)
            self.assertEqual(fixture.unit_path.read_bytes(),
                             (fixture.home / ".config/systemd/user/swiftwall.service").read_bytes().replace(
                                 b"graphical-session.target", fixture.target.encode()))

    def test_real_plugin_postcommit_wrapper_commits_before_exit_and_uninstalls(self):
        self.install_file_fixture()
        fixture = self.fixture
        fixture.write_fault_wrapper()
        result = fixture.raw(fixture.wrapper_command("plugin-postcommit"), timeout=12)
        self.assertEqual(result.returncode, 74, result.stderr)
        self.assertTrue((fixture.root / "fault-plugin-postcommit-commit.json").is_file())
        plugin = fixture.home / ".config/omarchy/plugins/io.github.henniedebruin.swiftwall"
        self.assertTrue(plugin.is_dir())
        result = fixture.raw(["python3", "-B", str(rehearsal.SCRIPTS / "plugin_install.py"),
                              "uninstall", "--home", str(fixture.home)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(plugin.exists())

    def test_real_timeout_wrapper_has_reaped_installer_before_parent_timeout(self):
        self.install_file_fixture()
        fixture = self.fixture
        fixture.write_fault_wrapper()
        # Use the real bounded wrapper path (12s), not a synthesized timeout/result.
        with self.assertRaises(subprocess.TimeoutExpired):
            fixture.raw(fixture.wrapper_command("plugin-timeout"), timeout=12)
        commit = json.loads((fixture.root / "fault-plugin-timeout-commit.json").read_text())
        child = json.loads((fixture.root / "fault-plugin-timeout-child.json").read_text())
        self.assertEqual(commit["child_returncode"], 0)
        for pid in (commit["wrapper_pid"], child["pid"]):
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    @patch.object(rehearsal.subprocess, "run")
    def test_fault_wrapper_whitelist_rejects_other_homes_and_extra_commands(self, run):
        command = self.fixture.wrapper_command("engine-postcommit")
        for bad in ([*command[:-1], "/other/home"], [*command, "--home", "/other/home"],
                    [*command, "/usr/bin/false"], [*command[:3], "arbitrary", *command[4:]],
                    ["python3", "-B", str(rehearsal.SCRIPTS / "install.py"),
                     "--home", str(self.fixture.home), "--home", "/other/home", "--rollback"]):
            with self.subTest(argv=bad), self.assertRaises(RuntimeError):
                self.fixture.raw(bad)
        run.assert_not_called()

    def test_wrappers_never_fault_initial_install_or_preflight_and_require_backup(self):
        fixture = self.fixture
        engine = ["python3", "-B", str(rehearsal.SCRIPTS / "install.py"),
                  "--home", str(fixture.home), "--binary"]
        plugin = ["python3", "-B", str(rehearsal.SCRIPTS / "plugin_install.py"),
                  "install", "--home", str(fixture.home), "--source", str(fixture.args.plugin_source)]
        fixture.deployment = argparse.Namespace(backup=None)
        for case in ("engine-postcommit", "plugin-postcommit", "plugin-timeout"):
            fixture.case = case
            with self.subTest(case=case), \
                    patch.object(fixture, "raw", return_value=subprocess.CompletedProcess([], 0, "", "")) as raw:
                fixture.runner([*engine, str(fixture.args.v01)])
                fixture.runner([*engine, str(fixture.args.v02), "--dry-run"])
                fixture.runner([*plugin, "--dry-run"])
                self.assertFalse(fixture.injected)
                self.assertFalse((fixture.root / "fault-installer.py").exists())
                raw.reset_mock()
                mutation = [*engine, str(fixture.args.v02)] if case == "engine-postcommit" else plugin
                with self.assertRaisesRegex(RuntimeError, "before stopped backup"):
                    fixture.runner(mutation)
                raw.assert_not_called()

    def test_wrapper_rejects_bad_home_before_spawning_child(self):
        self.fixture.write_fault_wrapper()
        command = self.fixture.wrapper_command("engine-postcommit")
        result = subprocess.run([*command[:-1], "/other/home"], env=self.fixture.env,
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected arguments", result.stderr)
        self.assertFalse(list(self.fixture.root.glob("fault-*-child.json")))

    def test_ipc_unavailable_does_not_consume_bad_frame_injection(self):
        fixture = self.fixture
        fixture.case = "ipc"
        request = [str(fixture.binary), "request", json.dumps({"v": 1, "op": "status"})]
        unavailable = subprocess.CompletedProcess([], 1, "", "Daemon unavailable: not listening")
        bad_version = subprocess.CompletedProcess([], 1, '{"ok":false,"code":"unsupported_version"}', "")
        with patch.object(rehearsal, "digest", return_value=rehearsal.V02), \
                patch.object(fixture, "raw", side_effect=[unavailable, unavailable, bad_version, bad_version]) as raw:
            fixture.runner(request)
            self.assertFalse(fixture.injected)
            fixture.runner(request)
            self.assertFalse(fixture.injected)
            fixture.runner(request)
            self.assertTrue(fixture.injected)
            fixture.runner(request)
        self.assertEqual([json.loads(call.args[0][2])["v"] for call in raw.call_args_list],
                         [999, 999, 999, 1])

    def test_activation_failure_writes_true_then_deactivation_restores_false(self):
        fixture = self.fixture
        fixture.case = "activation-failure"
        fixture.deployment = argparse.Namespace(backup=fixture.root)
        fixture.setup_activation()
        plugin_support.run("install", fixture.home, source=rehearsal.SCRIPTS.parent)
        with self.assertRaisesRegex(RuntimeError, "intentional private activation failure"):
            fixture.activation(True)
        shell = fixture.home / ".config/omarchy/shell.json"
        self.assertEqual(json.loads(shell.read_text())["bar"]["layout"]["right"][0],
                         {"id": rehearsal.PLUGIN_ID})
        self.assertTrue(fixture.injected)
        fixture.activation(False)
        self.assertEqual(shell.read_bytes(), fixture.shell_baseline)
        events = [json.loads(line) for line in (fixture.evidence / "trace.jsonl").read_text().splitlines()]
        self.assertEqual([event["enabled"] for event in events if event["event"] == "private_activation"],
                         [True, False])
        self.assertTrue(fixture.activation_controller.disable_attempted)

    def cleanup_result(self, state, stop_error=False):
        self.reserve_runtime()
        calls = []

        def raw(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "systemctl" and argv[2] == "show":
                return subprocess.CompletedProcess(argv, 0, state, "")
            code = 1 if stop_error and argv[0] == "systemctl" and argv[2] == "stop" else 0
            return subprocess.CompletedProcess(argv, code, "", "test stop failure" if code else "")

        with patch.object(self.fixture, "raw", side_effect=raw):
            errors = self.fixture.cleanup()
        return errors, calls

    def assert_pending_cleanup_retains_evidence(self, case):
        fixture = self.fixture
        fixture.case = case
        fixture.deployment = argparse.Namespace(backup=fixture.root)
        fixture.setup_activation()
        fixture.activation_controller.prepare()
        plugin_support.run("install", fixture.home, source=rehearsal.SCRIPTS.parent)
        with self.assertRaises(RuntimeError):
            fixture.activation(True)
        if case == "activation-plugin-drift":
            fixture.activation(False)
        else:
            with self.assertRaises(plugin_activation.ActivationError):
                fixture.activation(False)
        errors, calls = self.cleanup_result("ActiveState=inactive\nMainPID=0\n")
        self.assertEqual(errors, [])
        stops = [argv for argv in calls if argv[:3] == ["systemctl", "--user", "stop"]]
        self.assertEqual(stops, [["systemctl", "--user", "stop", fixture.unit],
                                 ["systemctl", "--user", "stop", fixture.target]])
        self.assertFalse(fixture.root.exists())
        retained = fixture.evidence / "fixture"
        self.assertTrue((retained / "home" / plugin_support.DESTINATION).is_dir())
        self.assertTrue((retained / "home" / plugin_support.MANIFEST).is_file())
        state = json.loads((retained / "fake-omarchy-state.json").read_text())
        self.assertEqual(state["query_failure"], case == "activation-query-failure")
        if case == "activation-disk-drift":
            disk = json.loads((retained / "home/.config/omarchy/shell.json").read_text())
            self.assertEqual(disk["theme"]["name"], "private-disk-drift")
        elif case == "activation-registry-drift":
            self.assertFalse(state["registry"]["local.news-ticker"])
        elif case == "activation-plugin-drift":
            self.assertIn(b"intentional private owned-file drift",
                          (retained / "home" / plugin_support.DESTINATION / "plugin/Panel.qml").read_bytes())
        commands = [json.loads(line) for line in
                    (retained / "fake-omarchy-invocations.jsonl").read_text().splitlines()]
        self.assertEqual(any(call["argv"][1:3] == ["plugin", "disable"] for call in commands),
                         case != "activation-query-failure")
        self.assertTrue((fixture.evidence / "activation/activation.jsonl").is_file())

    def test_pending_activation_cleanup_stops_only_fixture_and_retains_evidence(self):
        self.assert_pending_cleanup_retains_evidence("activation-query-failure")

    def test_disk_drift_cleanup_stops_only_fixture_and_retains_evidence(self):
        self.assert_pending_cleanup_retains_evidence("activation-disk-drift")

    def test_registry_drift_cleanup_stops_only_fixture_and_retains_evidence(self):
        self.assert_pending_cleanup_retains_evidence("activation-registry-drift")

    def test_plugin_drift_cleanup_stops_only_fixture_and_retains_evidence(self):
        self.assert_pending_cleanup_retains_evidence("activation-plugin-drift")

    def test_cleanup_retains_full_definitions_when_still_running(self):
        errors, calls = self.cleanup_result("ActiveState=active\nMainPID=123\n")
        self.assertTrue(errors)
        self.assertTrue(self.fixture.root.is_dir())
        self.assertTrue(all(path.exists() for path in self.fixture.owned))
        self.assertTrue((self.fixture.evidence / "fixture").is_dir())
        self.assertFalse(any(argv[2] in {"disable", "reset-failed", "daemon-reload"}
                             for argv in calls if argv[0] == "systemctl"))

    def test_cleanup_retains_definitions_when_pid_is_missing(self):
        errors, calls = self.cleanup_result("ActiveState=inactive\n")
        self.assertTrue(errors)
        self.assertTrue(all(path.exists() for path in self.fixture.owned))

    def test_cleanup_retains_definitions_on_failed_stop_even_if_show_is_inactive(self):
        errors, calls = self.cleanup_result("ActiveState=inactive\nMainPID=0\n", stop_error=True)
        self.assertTrue(errors)
        self.assertTrue(all(path.exists() for path in self.fixture.owned))

    def test_cleanup_removes_definitions_only_after_confirmed_stop(self):
        errors, calls = self.cleanup_result("ActiveState=inactive\nMainPID=0\n")
        self.assertEqual(errors, [])
        self.assertFalse(self.fixture.root.exists())
        self.assertFalse(any(path.exists() for path in self.fixture.owned))
        show = next(i for i, argv in enumerate(calls) if argv[0] == "systemctl" and argv[2] == "show")
        disable = next(i for i, argv in enumerate(calls) if argv[0] == "systemctl" and argv[2] == "disable")
        self.assertLess(show, disable)

    def test_running_executable_mismatch_is_rejected_by_coordinator(self):

        deployment = object.__new__(rehearsal.Deployment)
        deployment.binary = self.fixture.binary
        deployment.v01_hash = rehearsal.V01
        deployment.v02_hash = rehearsal.V02
        deployment.timeout = 1
        deployment.service = Mock()
        deployment.service.wait_ready.return_value = {"ok": True, "status": {}}
        deployment.service.state.return_value = {"MainPID": "12345", "ActiveState": "active",
                                                 "NRestarts": "0"}
        deployment.socket_owner = Mock()
        deployment.record = Mock()

        def digest(path):
            return "unapproved-running-sleep-hash" if str(path).startswith("/proc/") else rehearsal.V01

        with patch("deployment.digest", side_effect=digest) as hashed:
            with self.assertRaisesRegex(RuntimeError, "Running executable differs from installed engine"):
                deployment.healthy("0.1.0")
        self.assertEqual([call.args[0] for call in hashed.call_args_list],
                         [Path("/proc/12345/exe"), deployment.binary])
        deployment.service.wait_ready.assert_called_once()
        deployment.socket_owner.assert_called_once_with()
        deployment.record.assert_not_called()

    def test_all_cases_are_defaults(self):
        expected = {"success", "startup", "readiness", "ipc", "installer", "engine-postcommit",
                    "plugin-postcommit", "plugin-timeout", "activation-failure", "success-disabled",
                    "stale-socket", *rehearsal.ACTIVATION_CASES}
        self.assertEqual(set(rehearsal.DEFAULT_CASES), expected)
        self.assertEqual(len(rehearsal.DEFAULT_CASES), len(expected))
        self.assertEqual(rehearsal.selected_cases(",".join(rehearsal.DEFAULT_CASES)), list(rehearsal.DEFAULT_CASES))
        self.assertEqual(set(rehearsal.DEFAULT_CASES), set(rehearsal.CASES))

    def test_private_activation_cases_use_real_process_reconciliation(self):
        for case, state, transport in (
                ("activation-slow", "success", "ok"),
                ("activation-timeout-complete", "success", "timeout"),
                ("activation-partial", "partial", "ok"),
                ("activation-rejected", "rejected", "error"),
                ("activation-timeout-inactive", "ambiguous", "timeout"),
                ("activation-query-failure", "ambiguous", "ok"),
                ("activation-disk-drift", "ambiguous", "ok"),
                ("activation-registry-drift", "ambiguous", "ok")):
            with self.subTest(case=case):
                args = self.fixture.args
                with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root)}):
                    fixture = rehearsal.Rehearsal(args, case, self.root / case)
                fixture.evidence.mkdir()
                self.addCleanup(lambda root=fixture.root: rehearsal.shutil.rmtree(root, ignore_errors=True))
                fixture.deployment = argparse.Namespace(backup=fixture.root)
                fixture.setup_activation()
                fixture.activation_controller.prepare()
                self.assertEqual(fixture.activation_controller.baseline["persisted"],
                                 json.loads(fixture.shell_baseline))
                plugin_support.run("install", fixture.home, source=rehearsal.SCRIPTS.parent)
                started = time.monotonic()
                if state == "success":
                    result = fixture.activation(True)
                else:
                    with self.assertRaises(plugin_activation.ActivationError) as raised:
                        fixture.activation(True)
                    result = raised.exception.outcome
                self.assertEqual((result.state, result.transport), (state, transport))
                if case == "activation-slow":
                    self.assertGreaterEqual(time.monotonic() - started, 2.2)
                controller = fixture.activation_controller
                if case == "activation-query-failure":
                    with self.assertRaises(plugin_activation.ActivationError) as raised:
                        fixture.activation(False)
                    self.assertEqual(raised.exception.outcome.transport, "query-error")
                    self.assertFalse(controller.disable_attempted)
                    self.assertTrue((fixture.home / plugin_support.DESTINATION).exists())
                elif case in {"activation-disk-drift", "activation-registry-drift"}:
                    with self.assertRaises(plugin_activation.ActivationError):
                        fixture.activation(False)
                    self.assertTrue(controller.disable_attempted)
                    state_after = json.loads(fixture.activation_state.read_text())
                    disk_after = json.loads((fixture.home / ".config/omarchy/shell.json").read_text())
                    self.assertEqual(state_after["effective"], json.loads(fixture.shell_baseline))
                    self.assertFalse(state_after["registry"][rehearsal.PLUGIN_ID])
                    if case == "activation-disk-drift":
                        self.assertEqual(disk_after["theme"]["name"], "private-disk-drift")
                        self.assertEqual(state_after["effective"]["theme"]["name"], "private")
                    else:
                        self.assertFalse(state_after["registry"]["local.news-ticker"])
                        self.assertEqual(disk_after, json.loads(fixture.shell_baseline))
                else:
                    fixture.activation(False)
                    self.assertTrue(controller.disable_attempted)
                    self.assertEqual((fixture.home / ".config/omarchy/shell.json").read_bytes(),
                                     fixture.shell_baseline)
                    registry = json.loads(fixture.activation_state.read_text())["registry"]
                    self.assertFalse(registry[rehearsal.PLUGIN_ID])
                    self.assertTrue(registry["local.news-ticker"])
                calls = [json.loads(line) for line in
                         (fixture.root / "fake-omarchy-invocations.jsonl").read_text().splitlines()]
                enables = [call for call in calls if call["argv"][1:3] == ["plugin", "enable"]]
                self.assertEqual(len(enables), 1)
                for call in calls:
                    self.assertEqual(call["ipc_timeout"], "10s")
                    self.assertEqual(call["home"], str(fixture.home))
                    self.assertEqual(call["state"], str(fixture.activation_state))
                    self.assertIn(Path(call["argv"][0]),
                                  [fixture.root / "fake-omarchy", fixture.root / "fake-omarchy-shell"])

    def test_fake_disable_preserves_independent_unrelated_disk_and_effective_changes(self):
        fixture = self.fixture
        fixture.setup_activation()
        fixture.activation_controller.prepare()
        plugin_support.run("install", fixture.home, source=rehearsal.SCRIPTS.parent)
        fixture.activation(True)
        shell = fixture.home / ".config/omarchy/shell.json"
        disk = json.loads(shell.read_text())
        state = json.loads(fixture.activation_state.read_text())
        disk["bar"]["layout"]["left"][0]["settings"]["feed"] = "persisted-only"
        state["effective"]["bar"]["layout"]["center"][0]["format"] = "effective-only"
        shell.write_text(json.dumps(disk))
        fixture.activation_state.write_text(json.dumps(state))
        with self.assertRaises(plugin_activation.ActivationError):
            fixture.activation(False)
        after = json.loads(fixture.activation_state.read_text())
        self.assertEqual(json.loads(shell.read_text()), plugin_activation.without_plugin(disk))
        self.assertEqual(after["effective"], plugin_activation.without_plugin(state["effective"]))
        self.assertFalse(after["registry"][rehearsal.PLUGIN_ID])
        self.assertTrue(after["registry"]["local.news-ticker"])

    def test_private_transport_rejects_noncanonical_commands_and_environment(self):
        fixture = self.fixture
        fixture.setup_activation()
        env = fixture.activation_controller.env
        with patch.object(plugin_activation, "process") as process:
            for argv in (["omarchy", "plugin", "enable", rehearsal.PLUGIN_ID],
                         ["omarchy-shell", "reload"],
                         ["/usr/bin/omarchy", "plugin", "disable", rehearsal.PLUGIN_ID],
                         ["omarchy", "plugin", "validate", "/production/plugin"]):
                with self.assertRaises(RuntimeError):
                    fixture.activation_runner(argv, env=env, timeout=12)
            canonical = ["omarchy-shell", "shell", "listPlugins"]
            for changed in (dict(env, HOME="/production"),
                            dict(env, REHEARSAL_OMARCHY_STATE="/production/state"),
                            dict(env, OMARCHY_SHELL_IPC_TIMEOUT="2s")):
                with self.assertRaises(RuntimeError):
                    fixture.activation_runner(canonical, env=changed, timeout=12)
            (fixture.root / "fake-omarchy-shell").unlink()
            (fixture.root / "fake-omarchy-shell").symlink_to("/usr/bin/false")
            with self.assertRaises(RuntimeError):
                fixture.activation_runner(canonical, env=env, timeout=12)
        process.assert_not_called()

    def test_private_validation_rejects_corrupted_installed_bytes_before_enable(self):
        fixture = self.fixture
        fixture.setup_activation()
        plugin_support.run("install", fixture.home, source=rehearsal.SCRIPTS.parent)
        (fixture.home / plugin_support.DESTINATION / "plugin/BarWidget.qml").write_text("corrupt")
        with self.assertRaises(plugin_activation.ActivationError) as raised:
            fixture.activation(True)
        self.assertEqual(raised.exception.outcome.state, "rejected")
        self.assertFalse(fixture.activation_controller.enable_attempted)
        fixture.activation(False)
        self.assertEqual((fixture.home / ".config/omarchy/shell.json").read_bytes(), fixture.shell_baseline)

    def test_isolation_does_not_override_production_restart_policy(self):
        dropin = rehearsal.isolation_dropin(self.fixture.root)
        self.assertNotIn("Restart=", dropin)
        self.assertNotIn("RestartSec=", dropin)

    def test_fixture_png_signature(self):
        self.assertTrue(rehearsal.png().startswith(b"\x89PNG\r\n\x1a\n"))


class CoordinatorRegressionTests(unittest.TestCase):
    """Real private filesystem, fake engine bytes and fully mocked service/IPC."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.v01 = self.root / "v01"
        self.v02 = self.root / "v02"
        self.v01.write_bytes(b"file-only coordinator fixture v01, never executed")
        self.v02.write_bytes(b"file-only coordinator fixture v02, never executed")
        coordinator.install_support.install(self.home, self.v01)
        env = rehearsal.engine_environment(self.root)
        self.config = Path(env["SWIFTWALL_CONFIG"])
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text('interval_seconds = 300\nselection = "shuffle"\nfolders = []\n')
        self.state = Path(env["SWIFTWALL_STATE_DIR"]) / "state.json"
        self.state.write_text(json.dumps({"paused": True, "carousel": {
            "bag": ["private-next.png"], "history": ["private-current.png"], "cursor": 0}}))
        self.wallpaper = self.root / "image.png"
        self.wallpaper.write_bytes(rehearsal.png())
        Path(env["SWIFTWALL_BACKGROUND_LINK"]).symlink_to(self.wallpaper)
        self.shell = self.home / ".config/omarchy/shell.json"
        self.shell.parent.mkdir(parents=True)
        self.shell.write_text("{}")
        self.runner = Mock(side_effect=AssertionError("regression must not execute any command"))
        self.addCleanup(self.runner.assert_not_called)
        self.d = coordinator.Deployment(
            home=self.home, unit="swiftwall-rehearse-test.service", env=env,
            v01_hash=coordinator.digest(self.v01), v02=self.v02, v02_hash=coordinator.digest(self.v02),
            plugin_source=self.root, backup_root=self.root / "backups", evidence=self.root / "evidence",
            activate=Mock(), deactivate=Mock(), runner=self.runner)
        self.d.service = Mock()
        self.d.service.state.return_value = {
            "UnitFileState": "enabled-runtime", "ActiveState": "inactive", "MainPID": "0", "NRestarts": "0"}
        self.d.healthy = Mock(return_value={"ok": True, "status": {"observed_wallpaper": str(self.wallpaper)}})

    def copy_fault(self, fault):
        real_copy = coordinator.shutil.copy2

        def copy(source, destination):
            result = real_copy(source, destination)
            if Path(destination).name == "config":
                if fault == "hash":
                    Path(destination).write_bytes(b"corrupt copied config")
                elif fault == "mode":
                    Path(destination).chmod(stat.S_IMODE(Path(source).stat().st_mode) ^ 0o100)
                elif fault == "io":
                    raise OSError("injected interrupted private backup")
            return result

        return patch("deployment.shutil.copy2", side_effect=copy)

    def assert_partial_backup_unpublished(self, fault):
        original = self.config.read_bytes()
        with self.copy_fault(fault):
            with self.assertRaises((RuntimeError, OSError)):
                self.d.capture_backup()
        self.assertIsNone(self.d.backup)
        pending = list(self.d.backup_root.iterdir())
        self.assertEqual(len(pending), 1)
        self.assertTrue((pending[0] / "binary").is_file())
        self.assertFalse((pending[0] / "manifest.json").exists())
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(self.d.service.mock_calls)
        self.assertFalse((self.d.evidence / "events.jsonl").exists())

    def test_hash_corruption_leaves_partial_backup_checkpoint_none(self):
        self.assert_partial_backup_unpublished("hash")

    def test_mode_corruption_leaves_partial_backup_checkpoint_none(self):
        self.assert_partial_backup_unpublished("mode")

    def test_interrupted_copy_leaves_partial_backup_checkpoint_none(self):
        self.assert_partial_backup_unpublished("io")

    def test_directory_fsync_failure_does_not_publish_complete_looking_backup(self):
        real_fsync = coordinator.os.fsync

        def fsync(fd):
            self.assertIsNone(self.d.backup)
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("injected private backup directory fsync failure")
            return real_fsync(fd)

        with patch("deployment.os.fsync", side_effect=fsync):
            with self.assertRaisesRegex(OSError, "directory fsync failure"):
                self.d.capture_backup()
        self.assertIsNone(self.d.backup)
        pending = next(self.d.backup_root.iterdir())
        self.assertTrue((pending / "manifest.json").is_file())
        self.assertFalse((self.d.evidence / "events.jsonl").exists())

    def test_backup_checkpoint_published_only_after_manifest_and_directory_fsyncs(self):
        real_fsync = coordinator.os.fsync
        events = []

        def fsync(fd):
            self.assertIsNone(self.d.backup)
            info = os.fstat(fd)
            events.append("directory" if stat.S_ISDIR(info.st_mode) else "file")
            return real_fsync(fd)

        with patch("deployment.os.fsync", side_effect=fsync):
            self.d.capture_backup()
        self.assertEqual(events[-3:], ["file", "directory", "directory"])
        self.assertTrue(all(event == "file" for event in events[:-2]))
        manifest_path = self.d.backup / "manifest.json"
        self.assertEqual(stat.S_IMODE(manifest_path.stat().st_mode), 0o600)
        manifest = json.loads(manifest_path.read_text())
        for name, record in manifest.items():
            self.assertEqual(coordinator.digest(self.d.backup / name), record["sha256"])
            self.assertEqual(stat.S_IMODE((self.d.backup / name).stat().st_mode), record["mode"])
        events = [json.loads(line) for line in (self.d.evidence / "events.jsonl").read_text().splitlines()]
        self.assertEqual([event["event"] for event in events], ["backup_verified"])

    def test_preexisting_plugin_tree_metadata_or_settings_rejected_before_health_or_stop(self):
        plugin = self.home / ".config/omarchy/plugins" / coordinator.PLUGIN_ID
        metadata = self.home / ".local/state/swiftwall/plugin-install-manifest.json"
        plugin.parent.mkdir(parents=True)
        for kind in ("tree", "dangling-link", "metadata", "settings-enabled", "settings-disabled"):
            with self.subTest(kind=kind):
                if kind == "tree":
                    plugin.mkdir()
                elif kind == "dangling-link":
                    plugin.symlink_to(self.root / "missing-private-plugin")
                elif kind == "metadata":
                    metadata.write_text("{}")
                else:
                    self.shell.write_text(json.dumps({"plugin": coordinator.PLUGIN_ID,
                                                     "enabled": kind == "settings-enabled"}))
                with patch.object(self.d, "installer") as installer, patch.object(self.d, "plugin") as install_plugin:
                    with self.assertRaisesRegex(RuntimeError, "refuses existing SwiftWall plugin/settings"):
                        self.d.upgrade()
                    installer.assert_not_called()
                    install_plugin.assert_not_called()
                self.d.healthy.assert_not_called()
                self.assertFalse(self.d.service.mock_calls)
                self.assertIsNone(self.d.backup)
                self.assertFalse(self.d.engine_install_attempted)
                self.assertFalse(self.d.plugin_install_attempted)
                if plugin.is_symlink():
                    plugin.unlink()
                elif plugin.exists():
                    plugin.rmdir()
                metadata.unlink(missing_ok=True)
                self.shell.write_text("{}")

    def test_initial_harness_private_config_has_no_preexisting_target_settings(self):
        args = argparse.Namespace(v01=self.v01, v02=self.v02, plugin_source=self.root)
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root)}):
            fixture = rehearsal.Rehearsal(args, "success", self.root)
        self.addCleanup(lambda: rehearsal.shutil.rmtree(fixture.root, ignore_errors=True))
        (fixture.home / ".config/omarchy").mkdir(parents=True)
        fixture.activation(False)
        self.shell.write_bytes((fixture.home / ".config/omarchy/shell.json").read_bytes())
        self.assertNotIn(coordinator.PLUGIN_ID, self.shell.read_text())

    def test_backup_corruption_recovers_unchanged_v01_without_using_partial_checkpoint(self):
        with self.copy_fault("hash"), patch.object(self.d, "installer") as installer, \
                patch.object(self.d, "plugin") as plugin, patch.object(self.d, "rollback") as rollback:
            with self.assertRaisesRegex(RuntimeError, "Stopped backup verification failed"):
                self.d.upgrade()
        self.assertIsNone(self.d.backup)
        self.d.service.stop.assert_called_once_with()
        self.d.service.start.assert_called_once_with()
        rollback.assert_not_called()
        installer.assert_called_once_with("--binary", self.d.v02, "--dry-run")
        plugin.assert_called_once_with("install", dry_run=True)
        self.assertEqual([call.args for call in self.d.healthy.call_args_list], [("0.1.0",), ("0.1.0",)])
        self.assertEqual(self.d.binary.read_bytes(), self.v01.read_bytes())
        self.assertFalse(self.d.engine_install_attempted)
        self.assertFalse(self.d.plugin_install_attempted)

    def assert_failed_stop_reconciles_actual_state(self, active):
        failure = rehearsal.ServiceError("manager-failure", "injected lost stop response")
        self.d.service.stop.side_effect = failure
        self.d.service.state.return_value.update(
            ActiveState="active" if active else "inactive", MainPID="12345" if active else "0")
        with patch.object(self.d, "installer"), patch.object(self.d, "plugin"), \
                patch.object(self.d, "capture_backup") as capture, patch.object(self.d, "rollback") as rollback:
            with self.assertRaises(rehearsal.ServiceError) as raised:
                self.d.upgrade()
        self.assertIs(raised.exception, failure)
        capture.assert_not_called()
        rollback.assert_not_called()
        if active:
            self.d.service.start.assert_not_called()
        else:
            self.d.service.start.assert_called_once_with()
        self.assertEqual(self.d.healthy.call_count, 2)
        self.assertIsNone(self.d.backup)

    def test_failed_stop_does_not_restart_service_that_remains_active(self):
        self.assert_failed_stop_reconciles_actual_state(active=True)

    def test_failed_stop_restarts_when_actual_service_is_stopped(self):
        self.assert_failed_stop_reconciles_actual_state(active=False)

    def test_activation_failure_arms_deactivation_before_callback_and_runs_real_rollback(self):
        order = []
        failure = RuntimeError("injected partial private activation")

        def activate():
            self.assertTrue(self.d.widget_active, "rollback must be armed BEFORE activation")
            self.assertTrue(self.d.plugin_install_attempted)
            self.shell.write_text(json.dumps({"plugin": coordinator.PLUGIN_ID, "enabled": True}))
            order.append("activate")
            raise failure

        def deactivate():
            self.assertTrue(self.d.widget_active)
            self.assertTrue(json.loads(self.shell.read_text())["enabled"])
            self.shell.write_text(json.dumps({"plugin": coordinator.PLUGIN_ID, "enabled": False}))
            order.append("deactivate")

        def installer(*args):
            if "--rollback" in args:
                order.append("engine-rollback")
                coordinator.install_support.rollback(self.home, dry_run="--dry-run" in args)
            else:
                coordinator.install_support.install(self.home, self.v02, dry_run="--dry-run" in args)

        self.d.activate = activate
        self.d.deactivate = deactivate
        with patch.object(self.d, "installer", side_effect=installer), patch.object(self.d, "plugin"), \
                patch.object(self.d, "command", return_value="{}"):
            with self.assertRaises(RuntimeError) as raised:
                self.d.upgrade()
        self.assertIs(raised.exception, failure)
        self.assertEqual(order, ["activate", "deactivate", "engine-rollback", "engine-rollback"])
        self.assertFalse(self.d.widget_active)
        self.assertFalse(json.loads(self.shell.read_text())["enabled"])
        self.assertEqual(self.d.binary.read_bytes(), self.v01.read_bytes())
        events = [json.loads(line) for line in (self.d.evidence / "events.jsonl").read_text().splitlines()]
        self.assertIn("rollback_passed", [event["event"] for event in events])

    def test_isolated_constructor_rejects_default_or_unpaired_callbacks_without_commands(self):
        options = dict(home=self.home, unit="swiftwall-rehearse-test.service", env=self.d.env,
                       v01_hash=self.d.v01_hash, v02=self.v02, v02_hash=self.d.v02_hash,
                       plugin_source=self.root, backup_root=self.root / "backups",
                       evidence=self.root / "constructor-evidence", runner=self.runner)
        with patch.object(coordinator, "OmarchyActivation") as controller, \
                patch.object(subprocess, "Popen") as popen:
            for callbacks, message in (({}, "explicit private activation callbacks"),
                                       ({"activate": Mock()}, "both activation callbacks"),
                                       ({"deactivate": Mock()}, "both activation callbacks")):
                with self.subTest(callbacks=callbacks), self.assertRaisesRegex(ValueError, message):
                    coordinator.Deployment(**options, **callbacks)
        controller.assert_not_called()
        popen.assert_not_called()

    def test_real_home_constructor_rejects_unreconciled_callbacks_and_defaults_safely(self):
        options = dict(home=self.home, unit="swiftwall-rehearse-test.service", env=self.d.env,
                       v01_hash=self.d.v01_hash, v02=self.v02, v02_hash=self.d.v02_hash,
                       plugin_source=self.root, backup_root=self.root / "backups",
                       evidence=self.root / "constructor-evidence", runner=self.runner)
        activate, deactivate = Mock(), Mock()
        with patch.object(coordinator.Path, "home", return_value=self.home), \
                patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess forbidden")) as popen, \
                patch.object(subprocess, "run", side_effect=AssertionError("subprocess forbidden")) as run:
            with self.assertRaisesRegex(
                    ValueError, "Real-home activation callbacks require a reconciled controller"):
                coordinator.Deployment(**options, activate=activate, deactivate=deactivate)
            deployment = coordinator.Deployment(**options)
        activate.assert_not_called()
        deactivate.assert_not_called()
        popen.assert_not_called()
        run.assert_not_called()
        controller = deployment.activation_controller
        self.assertIsInstance(controller, plugin_activation.OmarchyActivation)
        self.assertIs(controller.runner, plugin_activation.process)
        self.assertEqual(controller.home, self.home)
        self.assertEqual(controller.env["OMARCHY_SHELL_IPC_TIMEOUT"], "10s")
        self.assertEqual(deployment.activate, controller.activate)
        self.assertEqual(deployment.deactivate, controller.deactivate)
        self.assertIsNone(controller.baseline)
        self.assertFalse(controller.activation_started)
        self.assertFalse((controller.evidence / "activation.jsonl").exists())

    def test_isolated_constructor_accepts_explicit_controller_without_sending_commands(self):
        controller = Mock()
        with patch.object(subprocess, "Popen") as popen:
            deployment = coordinator.Deployment(
                home=self.home, unit="swiftwall-rehearse-test.service", env=self.d.env,
                v01_hash=self.d.v01_hash, v02=self.v02, v02_hash=self.d.v02_hash,
                plugin_source=self.root, backup_root=self.root / "backups",
                evidence=self.root / "constructor-evidence", runner=self.runner,
                activation_controller=controller)
        self.assertIs(deployment.activation_controller, controller)
        self.assertIs(deployment.activate, controller.activate)
        self.assertIs(deployment.deactivate, controller.deactivate)
        self.assertFalse(controller.mock_calls)
        popen.assert_not_called()

    def private_activation_fixture(self, case):
        fixture = object.__new__(rehearsal.Rehearsal)
        fixture.root, fixture.home, fixture.env = self.root, self.home, self.d.env
        fixture.case, fixture.evidence = case, self.root / "fake-evidence"
        fixture.evidence.mkdir()
        fixture.deployment, fixture.injected = self.d, False
        fixture.setup_activation()
        self.d.activation_controller = fixture.activation_controller
        self.d.activate = lambda: fixture.activation(True)
        self.d.deactivate = lambda: fixture.activation(False)
        return fixture

    def assert_pending_engine_recovery(self, case, uninstall_failure=None):
        # Real controller/private commands and installers; no manager or IPC execution.
        fixture = self.private_activation_fixture(case)
        order = []

        def stopped():
            self.assertIsNotNone(fixture.activation_controller.baseline)
            if self.d.backup is None:
                self.assertFalse((self.home / plugin_support.DESTINATION).exists())
                self.assertEqual(fixture.activation_controller.baseline["persisted"],
                                 json.loads(fixture.shell_baseline))
            order.append("stop")

        self.d.service.stop.side_effect = stopped

        def installer(*args):
            if "--rollback" in args:
                order.append("engine-rollback")
                coordinator.install_support.rollback(self.home, dry_run="--dry-run" in args)
            else:
                coordinator.install_support.install(self.home, self.v02, dry_run="--dry-run" in args)

        def plugin(operation, dry_run=False):
            order.append(operation)
            if operation == "install":
                self.assertIsNotNone(fixture.activation_controller.baseline)
                if not dry_run:
                    self.assertTrue(self.d.widget_active, "arm cleanup before publication")
            if operation == "uninstall" and dry_run == uninstall_failure:
                raise OSError("intentional private uninstall failure")
            plugin_support.run(operation, self.home, source=rehearsal.SCRIPTS.parent, dry_run=dry_run)

        with patch.object(self.d, "installer", side_effect=installer), \
                patch.object(self.d, "plugin", side_effect=plugin), \
                patch.object(self.d, "command", return_value="{}"):
            with self.assertRaisesRegex(RuntimeError, "engine recovered.*plugin files retained") as raised:
                self.d.upgrade()
        if case == "activation-plugin-drift":
            self.assertIsInstance(raised.exception.__cause__, plugin_support.InstallError)
        elif uninstall_failure is not None:
            self.assertIsInstance(raised.exception.__cause__, OSError)
        else:
            self.assertIsInstance(raised.exception.__cause__, plugin_activation.ActivationError)
            self.assertEqual(raised.exception.__cause__.outcome.transport,
                             "query-error" if case == "activation-query-failure" else "ok")
        self.assertEqual(self.d.binary.read_bytes(), self.v01.read_bytes())
        self.assertEqual(self.config.read_bytes(), (self.d.backup / "config").read_bytes())
        self.assertEqual(self.state.read_bytes(), (self.d.backup / "state").read_bytes())
        widget_pending = case not in {"activation-plugin-drift", "activation-failure"}
        self.assertEqual(self.d.widget_active, widget_pending)
        self.assertTrue(self.d.plugin_installed)
        self.assertTrue((self.home / plugin_support.DESTINATION).is_dir())
        self.assertTrue((self.home / plugin_support.MANIFEST).is_file())
        if case == "activation-plugin-drift":
            with self.assertRaises(plugin_support.InstallError):
                plugin_support.verify_owned(self.home, plugin_support.load_manifest(self.home))
            self.assertIn(b"intentional private owned-file drift",
                          (self.home / plugin_support.DESTINATION / "plugin/Panel.qml").read_bytes())
        else:
            plugin_support.verify_owned(self.home, plugin_support.load_manifest(self.home))
        if uninstall_failure is None:
            self.assertNotIn("uninstall", order)
        else:
            self.assertIn("uninstall", order)
        self.assertEqual(order.count("engine-rollback"), 2)
        self.assertEqual(fixture.activation_controller.disable_attempted,
                         case != "activation-query-failure")
        state = json.loads(fixture.activation_state.read_text())
        persisted = json.loads(self.shell.read_text())
        if case == "activation-disk-drift":
            self.assertEqual(persisted["theme"]["name"], "private-disk-drift")
            self.assertEqual(state["effective"], json.loads(fixture.shell_baseline))
        elif case == "activation-registry-drift":
            self.assertFalse(state["registry"]["local.news-ticker"])
            self.assertEqual(persisted, json.loads(fixture.shell_baseline))
        elif case != "activation-query-failure":
            self.assertEqual(self.shell.read_bytes(), fixture.shell_baseline)
        prepared = fixture.activation_controller.baseline
        self.assertFalse(prepared["registry"][rehearsal.PLUGIN_ID])
        self.assertEqual(prepared["persisted"], json.loads(fixture.shell_baseline))
        self.assertEqual(self.d.healthy.call_args.args, ("0.1.0",))
        self.assertEqual(self.d.timeout, 10)
        self.d.service.start.assert_called()
        events = [json.loads(line) for line in (self.d.evidence / "events.jsonl").read_text().splitlines()]
        names = [event["event"] for event in events]
        self.assertIn("widget_cleanup_pending" if widget_pending else "plugin_cleanup_pending", names)
        self.assertNotIn("rollback_passed", names)
        recovered = next(event for event in events if event["event"] == "rollback_engine_recovered")
        self.assertEqual(recovered["widget_cleanup"], "pending")
        self.assertEqual(recovered["plugin_files"], "retained")

    def test_query_failure_recovers_engine_but_retains_plugin_files_and_pending_cleanup(self):
        self.assert_pending_engine_recovery("activation-query-failure")

    def test_disk_drift_recovers_engine_without_restoring_unrelated_persisted_settings(self):
        self.assert_pending_engine_recovery("activation-disk-drift")

    def test_registry_drift_recovers_engine_without_restoring_unrelated_plugin(self):
        self.assert_pending_engine_recovery("activation-registry-drift")

    def test_plugin_verify_failure_recovers_engine_and_retains_modified_owned_files(self):
        self.assert_pending_engine_recovery("activation-plugin-drift")

    def test_plugin_uninstall_dry_run_failure_does_not_block_engine_recovery(self):
        self.assert_pending_engine_recovery("activation-failure", uninstall_failure=True)

    def test_plugin_uninstall_mutation_failure_does_not_block_engine_recovery(self):
        self.assert_pending_engine_recovery("activation-failure", uninstall_failure=False)

    def assert_postcommit_discovery_is_deactivated(self, case):
        fixture = self.private_activation_fixture(case)
        failure = RuntimeError("intentional private postcommit reply loss")
        self.d.activate = Mock(side_effect=AssertionError("activation callback must not be reached"))

        def stopped():
            self.assertIsNotNone(fixture.activation_controller.baseline)
            if self.d.backup is None:
                self.assertFalse((self.home / plugin_support.DESTINATION).exists())

        self.d.service.stop.side_effect = stopped

        def installer(*args):
            if "--rollback" in args:
                coordinator.install_support.rollback(self.home, dry_run="--dry-run" in args)
            else:
                coordinator.install_support.install(self.home, self.v02, dry_run="--dry-run" in args)

        def plugin(operation, dry_run=False):
            if operation == "install" and not dry_run:
                self.assertTrue(self.d.widget_active, "publication must arm shell cleanup")
                self.assertIsNotNone(fixture.activation_controller.baseline)
            plugin_support.run(operation, self.home, source=rehearsal.SCRIPTS.parent, dry_run=dry_run)
            if operation == "install" and not dry_run:
                raise failure

        with patch.object(self.d, "installer", side_effect=installer), \
                patch.object(self.d, "plugin", side_effect=plugin), \
                patch.object(self.d, "command", return_value="{}"):
            with self.assertRaises(RuntimeError) as raised:
                self.d.upgrade()
        self.assertIs(raised.exception, failure)
        self.d.activate.assert_not_called()
        controller = fixture.activation_controller
        self.assertFalse(controller.enable_attempted)
        self.assertTrue(controller.disable_attempted)
        self.assertFalse(self.d.widget_active)
        self.assertEqual(self.d.binary.read_bytes(), self.v01.read_bytes())
        self.assertFalse((self.home / plugin_support.DESTINATION).exists())
        self.assertFalse((self.home / plugin_support.MANIFEST).exists())
        self.assertEqual(self.shell.read_bytes(), fixture.shell_baseline)
        state = json.loads(fixture.activation_state.read_text())
        self.assertTrue(state["publication_discovered"])
        self.assertFalse(state["registry"][rehearsal.PLUGIN_ID])
        self.assertEqual(state["effective"], json.loads(fixture.shell_baseline))
        events = [json.loads(line) for line in (self.d.evidence / "events.jsonl").read_text().splitlines()]
        self.assertIn("rollback_passed", [event["event"] for event in events])
        calls = [json.loads(line) for line in
                 (fixture.root / "fake-omarchy-invocations.jsonl").read_text().splitlines()]
        self.assertEqual(sum(call["argv"][1:3] == ["plugin", "disable"] for call in calls), 1)
        self.assertFalse(any(call["argv"][1:3] == ["plugin", "enable"] for call in calls))

    def test_postcommit_plugin_publication_reconciles_autodiscovery_before_uninstall(self):
        self.assert_postcommit_discovery_is_deactivated("plugin-postcommit")

    def test_timeout_plugin_publication_reconciles_autodiscovery_before_uninstall(self):
        self.assert_postcommit_discovery_is_deactivated("plugin-timeout")

    def test_preexisting_effective_activation_is_rejected_before_stop_or_publication(self):
        fixture = self.private_activation_fixture("success")
        state = json.loads(fixture.activation_state.read_text())
        state["registry"][rehearsal.PLUGIN_ID] = True
        state["effective"]["bar"]["layout"]["right"].insert(0, {"id": rehearsal.PLUGIN_ID})
        fixture.activation_state.write_text(json.dumps(state))
        with patch.object(self.d, "installer") as installer, patch.object(self.d, "plugin") as plugin:
            with self.assertRaisesRegex(plugin_activation.ActivationError, "Pre-existing activation"):
                self.d.upgrade()
        self.d.service.stop.assert_not_called()
        self.d.healthy.assert_not_called()
        installer.assert_not_called()
        plugin.assert_not_called()
        with self.assertRaisesRegex(plugin_activation.ActivationError, "pre-existing activation"):
            fixture.activation_controller.deactivate()
        self.assertFalse(fixture.activation_controller.disable_attempted)
        self.assertEqual(json.loads(fixture.activation_state.read_text()), state)

    def test_running_executable_rechecks_pid_and_restart_stability(self):
        for changed in ({"MainPID": "12346", "NRestarts": "0"},
                        {"MainPID": "12345", "NRestarts": "1"}):
            with self.subTest(confirmed=changed):
                self.d.service.state.side_effect = [{"MainPID": "12345"}, changed]
                with patch("deployment.digest", return_value=self.d.v01_hash):
                    with self.assertRaisesRegex(RuntimeError, "Service changed during socket/executable verification"):
                        self.d.running_executable()


if __name__ == "__main__":
    unittest.main()
