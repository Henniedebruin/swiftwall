#!/usr/bin/env python3
"""Opt-in real-binary/systemd rehearsal. Importing this module runs nothing.

No production units or desktop files are queried. The isolation guard confines all
engine paths, setters, installer homes and systemctl unit arguments to the fixture.
Production hashes/PID/bar/theme baselines, if wanted, are the caller's responsibility.
Activation uses the real controller and private fake Omarchy subprocesses, never a live shell.
Each case gets a fresh installation. Requires an already running user manager.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
import zlib

sys.dont_write_bytecode = True
from deployment import (Deployment, digest, private_command_output,
                        private_evidence_directory, private_evidence_log)
from service_control import ServiceError
import plugin_activation

PLUGIN_ID = plugin_activation.PLUGIN_ID

V01 = "e878ecca22dfa66823e3b973701a83a61a9ae99acdb82d15d8426ab7a9b079d0"
V02 = "8779f3b1bc7ad5b76942e29a63e24a39e9a5574c10b2b48e5d05a9b9d3cec20a"
FAULT_CASES = ("startup", "readiness", "ipc", "installer", "engine-postcommit",
               "plugin-postcommit", "plugin-timeout", "activation-failure")
ACTIVATION_CASES = ("activation-slow", "activation-timeout-complete", "activation-partial",
                    "activation-rejected", "activation-timeout-inactive", "activation-query-failure",
                    "activation-disk-drift", "activation-registry-drift", "activation-plugin-drift")
PENDING_CASES = ("activation-query-failure", "activation-disk-drift",
                 "activation-registry-drift", "activation-plugin-drift")
FAULT_CASES += ACTIVATION_CASES[2:]
DEFAULT_CASES = ("success", *FAULT_CASES, "success-disabled", "stale-socket",
                 *ACTIVATION_CASES[:2])
CASES = DEFAULT_CASES
SCRIPTS = Path(__file__).resolve().parent


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def absolute_existing(value):
    path = Path(value)
    if not path.is_absolute() or not path.exists() or path.is_symlink():
        raise argparse.ArgumentTypeError("must be an absolute, existing, non-symlink path")
    return path.resolve(strict=True)


def selected_cases(value):
    cases = value.split(",")
    if not cases or any(case not in CASES for case in cases) or len(set(cases)) != len(cases):
        raise argparse.ArgumentTypeError("use unique comma-separated cases: " + ",".join(CASES))
    return cases


def unit_quote(value):
    # systemd specifiers expand even inside quotes; these paths are not shell words.
    value = str(value)
    require(not any(c in value for c in "\n\r\0"), "unsafe unit value")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def simple_unit_path(value):
    # WorkingDirectory takes a path, not ExecStart words.
    # Our /tmp fixture paths need neither quoting nor specifier expansion.
    value = str(value)
    require(re.fullmatch(r"/tmp/swr-[A-Za-z0-9_-]+(?:/[A-Za-z0-9_.-]+)*", value)
            and all(part not in {".", ".."} for part in Path(value).parts),
            "unsafe generated unit path")
    return value


def write_launcher(root):
    """Embed expectations independently of unit directives; never infer defaults."""
    root = Path(simple_unit_path(root))
    launcher = root / "launch.py"
    expected = engine_environment(root)
    launcher.write_text('''#!/usr/bin/python3
import os
from pathlib import Path
import sys


def require(condition, message):
    if not condition:
        sys.exit("rehearsal isolation guard: " + message)


''' + f"root = Path({str(root)!r})\nexpected = {expected!r}\n" + '''
require(root.resolve(strict=True) == root, "fixture root is linked")
require(Path(__file__).resolve() == root / "launch.py", "unexpected launcher path")
for key, value in expected.items():
    require(os.environ.get(key) == value, "missing or mismatched " + key)
require(not any(key.startswith("SWIFTWALL_") and key not in expected
                for key in os.environ), "unexpected inherited SWIFTWALL override")
# These are the ONLY variables passed to execve, including the ONLY XDG keys.
# Manager metadata (including unrelated XDG session variables) is discarded.
for key, value in expected.items():
    if key == "HOME" or key.startswith(("SWIFTWALL_", "XDG_")):
        path = Path(value)
        require(path.is_absolute() and path.is_relative_to(root)
                and path.resolve().is_relative_to(root), "unconfined " + key)
setter = root / "setter"
background = root / "background"
require(expected["SWIFTWALL_OMARCHY_COMMAND"] == str(setter)
        and setter.is_file() and not setter.is_symlink(), "setter not whitelisted")
require(expected["SWIFTWALL_BACKGROUND_LINK"] == str(background)
        and background.is_symlink()
        and background.resolve(strict=True).is_relative_to(root / "images"),
        "background not private")
binary = root / "home/.local/bin/swiftwall"
require(binary.is_file() and not binary.is_symlink()
        and binary.resolve().is_relative_to(root), "binary not private")
os.execve(str(binary), [str(binary), "daemon"], expected)
''')
    launcher.chmod(0o700)
    return launcher


def isolation_dropin(root):
    root = Path(simple_unit_path(root))
    return ("[Service]\nExecStart=\nExecStart=/usr/bin/python3 " + unit_quote(root / "launch.py") + "\n"
            "EnvironmentFile=\nEnvironment=\n"
            + "".join("Environment=" + unit_quote(key + "=" + value) + "\n"
                      for key, value in engine_environment(root).items()) +
            "TimeoutStartSec=10\nTimeoutStopSec=10\nUMask=0077\n"
            "WorkingDirectory=" + simple_unit_path(root / "home") + "\n")


def engine_environment(root):
    home = root / "home"
    return {
        "HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_RUNTIME_DIR": str(root / "run"),
        "SWIFTWALL_CONFIG": str(home / ".config/swiftwall/config.toml"),
        "SWIFTWALL_STATE_DIR": str(home / ".local/state/swiftwall"),
        "SWIFTWALL_RUNTIME_DIR": str(root / "run/swiftwall"),
        "SWIFTWALL_BACKGROUND_LINK": str(root / "background"),
        "SWIFTWALL_OMARCHY_COMMAND": str(root / "setter"),
    }


def png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0\x30\x60\x90")) + chunk(b"IEND", b""))


class Rehearsal:
    def __init__(self, args, case, evidence):
        self.args, self.case, self.evidence = args, case, evidence
        self.root = Path(tempfile.mkdtemp(prefix="swr-", dir="/tmp"))
        self.env = engine_environment(self.root)
        self.home = Path(self.env["HOME"])
        self.binary = self.home / ".local/bin/swiftwall"
        self.name = "swiftwall-rehearse-" + uuid.uuid4().hex
        self.unit, self.target = self.name + ".service", self.name + ".target"
        self.manager_env = {k: v for k, v in os.environ.items() if not k.startswith("SWIFTWALL_")}
        self.unit_dir = Path(self.manager_env["XDG_RUNTIME_DIR"]) / "systemd/user"
        self.unit_path = self.unit_dir / self.unit
        self.target_path = self.unit_dir / self.target
        self.drop_dir = self.unit_dir / (self.unit + ".d")
        self.injected = False
        self.fault_active = False
        self.deployment = None
        self.owned = []


    def record(self, event, **fields):
        with private_evidence_directory(self.evidence) as directory:
            with private_evidence_log(directory, create=True, name="trace.jsonl") as stream:
                stream.write(json.dumps({"time": time.time(), "event": event, **fields}) + "\n")

    def installer_commands(self):
        engine = ["python3", "-B", str(SCRIPTS / "install.py"), "--home", str(self.home)]
        commands = {}
        for tail in (["--binary", str(self.args.v01)], ["--binary", str(self.args.v02)],
                     ["--binary", str(self.root / "nonexistent-v02")], ["--rollback"]):
            for dry in ([], ["--dry-run"]):
                commands[tuple(engine + tail + dry)] = ("engine", not dry)
        for operation in ("install", "verify", "uninstall", "rollback"):
            plugin = ["python3", "-B", str(SCRIPTS / "plugin_install.py"), operation,
                      "--home", str(self.home)]
            if operation == "install":
                plugin += ["--source", str(self.args.plugin_source)]
            for dry in ([[]] if operation == "verify" else [[], ["--dry-run"]]):
                commands[tuple(plugin + dry)] = ("plugin", not dry and operation != "verify")
        return commands

    def wrapper_command(self, case):
        require(case in {"engine-postcommit", "plugin-postcommit", "plugin-timeout"},
                "unknown installer wrapper fault")
        return ["python3", "-B", str(self.root / "fault-installer.py"), case,
                "--home", str(self.home)]

    def write_fault_wrapper(self):
        # The wrapper accepts no arbitrary program, source or home. Its child is
        # always one of these exact real, file-only installer invocations.
        commands = {
            "engine-postcommit": [str(SCRIPTS / "install.py"), "--home", str(self.home),
                                  "--binary", str(self.args.v02)],
            "plugin-postcommit": [str(SCRIPTS / "plugin_install.py"), "install", "--home",
                                  str(self.home), "--source", str(self.args.plugin_source)],
        }
        commands["plugin-timeout"] = commands["plugin-postcommit"]
        path = self.root / "fault-installer.py"
        path.write_text('''import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

''' + f"root = Path({str(self.root)!r})\ncommands = {commands!r}\nexpected = {self.env!r}\n" + '''
if (len(sys.argv) != 4 or sys.argv[1] not in commands
        or sys.argv[2:] != ["--home", expected["HOME"]]
        or any(os.environ.get(k) != v for k, v in expected.items())):
    sys.exit("rehearsal wrapper: unexpected arguments or environment")
case = sys.argv[1]
parent = os.getpid()


def child_guard():
    # A supervising timeout/SIGINT may kill this wrapper even while its installer
    # is running. Linux kills that private child too; the installers spawn none.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
        os._exit(75)


argv = [sys.executable, "-B", *commands[case]]
trace = root / ("fault-" + case + "-child.json")
with subprocess.Popen(argv, env=expected, preexec_fn=child_guard) as child:
    trace.write_text(json.dumps({"argv": argv, "pid": child.pid}))
    try:
        code = child.wait(timeout=8)
    except BaseException:
        child.kill()
        child.wait()
        raise
if code:
    sys.exit(code)
# Only commit this marker after the successful installer child has been REAPED.
(root / ("fault-" + case + "-commit.json")).write_text(json.dumps(
    {"argv": argv, "child_returncode": code, "wrapper_pid": os.getpid()}))
print("rehearsal wrapper: committed " + case, flush=True)
if case == "plugin-timeout":
    time.sleep(30)
os._exit(74)
''')
        path.chmod(0o700)

    def sync_runtime_unit(self):
        if self.unit_path not in self.owned:
            return  # Initial file-only installation precedes runtime reservation.
        installed = self.home / ".config/systemd/user/swiftwall.service"
        require(installed.is_file() and not installed.is_symlink(), "missing private unit template")
        template = installed.read_bytes()
        require(b"graphical-session.target" in template, "unexpected installed unit template")
        transformed = template.replace(b"graphical-session.target", self.target.encode())
        if self.unit_path.read_bytes() != transformed:
            self.unit_path.write_bytes(transformed)
            self.record("runtime_template_synced", installed_sha256=digest(installed),
                        runtime_sha256=digest(self.unit_path))
            self.reload()

    def raw(self, argv, **kwargs):
        argv = list(map(str, argv))
        is_manager = argv[0] == "systemctl"
        if is_manager:
            require(argv[:2] == ["systemctl", "--user"], "only user manager allowed")
            allowed = argv[2:] == ["daemon-reload"]
            if len(argv) >= 4 and argv[3] in {self.unit, self.target}:
                action, extra = argv[2], argv[4:]
                if action in {"start", "stop", "reset-failed"}:
                    allowed = not extra
                elif action in {"enable", "disable"}:
                    allowed = extra == ["--runtime"]
                elif action == "show":
                    properties = {"LoadState", "ActiveState", "SubState", "MainPID",
                                  "NRestarts", "Result", "UnitFileState", "Environment",
                                  "EnvironmentFiles", "PassEnvironment", "UnsetEnvironment", "PAMName"}
                    allowed = len(extra) % 2 == 0 and all(
                        extra[i] == "-p" and extra[i + 1] in properties
                        for i in range(0, len(extra), 2))
            require(allowed, "non-fixture systemctl forbidden")
        elif argv[0] == "journalctl":
            require(argv[1:4] == ["--user", "--unit", self.unit], "non-fixture journal forbidden")
        else:
            wrappers = {tuple(self.wrapper_command(case)) for case in
                        ("engine-postcommit", "plugin-postcommit", "plugin-timeout")}
            require(argv[0] == str(self.binary) or tuple(argv) in self.installer_commands()
                    or tuple(argv) in wrappers, "unexpected executable/home/installer arguments")
        operation = self.installer_commands().get(tuple(argv))
        engine_mutation = operation == ("engine", True) or argv == self.wrapper_command("engine-postcommit")
        kwargs.update(env=self.manager_env if is_manager or argv[0] == "journalctl" else self.env,
                      capture_output=True, text=True)
        kwargs["timeout"] = min(kwargs.get("timeout", 20), 35)
        self.record("exec", argv=argv, environment="manager" if is_manager else "isolated")
        private = private_command_output(argv)
        query_failed = False
        try:
            result = subprocess.run(argv, **kwargs)
        except BaseException as exc:
            if private:
                if not isinstance(exc, Exception):
                    self.record("exec_exception", argv=argv, output_omitted=True, error="Service property query interrupted")
                    raise
                query_failed = True
            else:
                fields = {}
                if isinstance(exc, subprocess.TimeoutExpired):
                    for key in ("stdout", "stderr"):
                        output = getattr(exc, key)
                        fields[key] = output.decode(errors="replace") if isinstance(output, bytes) else output
                self.record("exec_exception", argv=argv, error=repr(exc), **fields)
                raise
        finally:
            # A failed wrapper or lost reply does NOT mean installation failed to
            # commit. Re-read the actual installed template even on timeout/error.
            if engine_mutation:
                self.sync_runtime_unit()
        if query_failed:
            self.record("exec_exception", argv=argv, output_omitted=True, error="Service property query failed")
            raise RuntimeError("Service property query failed; output omitted")
        output = {"output_omitted": True} if private else {"stdout": result.stdout, "stderr": result.stderr}
        self.record("result", argv=argv, returncode=result.returncode, **output)
        return result

    def command(self, argv):
        result = self.raw(argv)
        if result.returncode and private_command_output(list(map(str, argv))):
            raise RuntimeError("Service property query failed; output omitted")
        require(result.returncode == 0, f"Command failed: {argv}: {result.stderr}")
        return result.stdout

    def ctl(self, action, unit=None, *extra):
        return self.command(["systemctl", "--user", action, unit or self.unit, *extra])

    def reload(self):
        self.command(["systemctl", "--user", "daemon-reload"])

    def fault_override(self, command=None):
        path = self.drop_dir / "90-fault.conf"
        if command:
            path.write_text("[Service]\nExecStart=\nExecStart=" + command + "\n")
            self.fault_active = True
        else:
            path.unlink(missing_ok=True)
            self.fault_active = False
        self.reload()

    def runner(self, argv, **kwargs):
        argv = list(map(str, argv))
        install = (argv[:3] == ["python3", "-B", str(SCRIPTS / "install.py")]
                   and "--binary" in argv and "--dry-run" not in argv
                   and argv[argv.index("--binary") + 1] == str(self.args.v02))
        # These are boundary faults, never fabricated command responses.
        if install and self.case == "installer" and not self.injected:
            require(self.deployment.backup is not None, "installer fault before stopped backup")
            argv[argv.index("--binary") + 1] = str(self.root / "nonexistent-v02")
            self.injected = True
        if argv[:4] == ["systemctl", "--user", "stop", self.unit] and self.fault_active:
            # The next stop is rollback; remove the override before its restart.
            self.fault_override()
        ipc_fault = False
        if (self.case == "ipc" and not self.injected and argv[:2] == [str(self.binary), "request"]
                and digest(self.binary) == V02):
            request = json.loads(argv[2])
            request["v"] = 999
            argv[2] = json.dumps(request)
            ipc_fault = True
        plugin_install = argv == ["python3", "-B", str(SCRIPTS / "plugin_install.py"),
                                  "install", "--home", str(self.home),
                                  "--source", str(self.args.plugin_source)]
        wrapper_fault = ((install and self.case == "engine-postcommit") or
                         (plugin_install and self.case in {"plugin-postcommit", "plugin-timeout"}))
        if wrapper_fault and not self.injected:
            require(self.deployment.backup is not None, "wrapper fault before stopped backup")
            self.write_fault_wrapper()
            argv = self.wrapper_command(self.case)
            self.injected = True
            # Child deadline is 8s; the postcommit hang is 30s. The outer deadline
            # leaves time to reap the installer before timing out the wrapper.
            kwargs["timeout"] = 12
        result = self.raw(argv, **kwargs)
        if ipc_fault and "Daemon unavailable" not in (result.stdout + result.stderr):
            # A pre-listen connection failure did not deliver the bad frame. Keep
            # the injection armed until the real daemon actually receives it.
            self.injected = True
        if install and result.returncode == 0 and self.case in {"startup", "readiness"}:
            self.fault_override("/usr/bin/false" if self.case == "startup" else "/usr/bin/sleep 30")
            self.injected = True
            if self.case == "readiness":
                Path(self.env["SWIFTWALL_RUNTIME_DIR"], "control.sock").unlink(missing_ok=True)
        return result

    def setup_activation(self):
        """Create only private state and executables; construction sends no commands."""
        if hasattr(self, "activation_controller"):
            return
        shell = self.home / ".config/omarchy/shell.json"
        shell.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        config = {"bar": {"height": 32, "layout": {
            "left": [{"id": "local.news-ticker", "settings": {"feed": "private", "limit": 4}}],
            "center": [{"id": "clock", "format": "hh:mm", "settings": {"seconds": False}}],
            "right": [{"id": "tray", "spacing": 7}]}},
            "plugins": ["local.news-ticker"], "theme": {"name": "private"}}
        self.shell_baseline = (json.dumps(config, indent=2) + "\n").encode()
        shell.write_bytes(self.shell_baseline)
        self.activation_state = self.root / "fake-omarchy-state.json"
        self.activation_state.write_text(json.dumps({"registry": {
            PLUGIN_ID: False, "local.news-ticker": True}, "effective": config,
            "case": self.case, "query_failure": False}))
        # The fake validates installed ownership bytes/shape, not native QML semantics.
        source = '''#!/usr/bin/python3
import json, os, sys, time
from pathlib import Path
''' + f"sys.path.insert(0, {str(SCRIPTS)!r})\nroot = Path({str(self.root)!r})\n" + '''
import plugin_support
from plugin_activation import PLUGIN_ID, without_plugin
home = root / "home"
state_path = root / "fake-omarchy-state.json"
assert root.resolve(strict=True) == root
assert os.environ["HOME"] == str(home)
assert os.environ["REHEARSAL_OMARCHY_STATE"] == str(state_path)
assert os.environ["OMARCHY_SHELL_IPC_TIMEOUT"] == "10s"
state = json.loads(state_path.read_text())
shell = home / ".config/omarchy/shell.json"
config = json.loads(shell.read_text())
effective = state["effective"]
args = sys.argv[1:]
# Simulate publication-triggered discovery even if the installer reply is lost.
if (Path(sys.argv[0]).name == "fake-omarchy-shell"
        and state["case"] in ("plugin-postcommit", "plugin-timeout")
        and (home / plugin_support.DESTINATION).is_dir()
        and not state.get("publication_discovered")):
    for value in (config, effective):
        value["bar"]["layout"]["right"].insert(0, {"id": PLUGIN_ID})
    state["registry"][PLUGIN_ID] = True
    state["publication_discovered"] = True
    shell.write_text(json.dumps(config, indent=2) + "\\n")
    state_path.write_text(json.dumps(state))
with (root / "fake-omarchy-invocations.jsonl").open("a") as stream:
    stream.write(json.dumps({"argv": sys.argv, "home": os.environ["HOME"],
                            "state": str(state_path),
                            "ipc_timeout": os.environ["OMARCHY_SHELL_IPC_TIMEOUT"]}) + "\\n")
if Path(sys.argv[0]).name == "fake-omarchy-shell":
    assert args in [["shell", method] for method in
                    ("listPlugins", "listShellConfig", "rescanPlugins")]
    if args[1] == "listShellConfig":
        if state["query_failure"]:
            sys.exit("private listShellConfig unavailable after mutation")
        print(json.dumps(effective))
    else:
        print(json.dumps([{"id": key, "enabled": value} for key, value in state["registry"].items()]))
    sys.exit(0)
if args == ["plugin", "validate", str(home / plugin_support.DESTINATION)]:
    manifest = plugin_support.load_manifest(home)
    assert manifest is not None, "installed ownership manifest required"
    plugin_support.verify_owned(home, manifest)
    print("private package shape and bytes verified; native validation is separate")
    sys.exit(0)
if args == ["plugin", "disable", PLUGIN_ID]:
    config = without_plugin(config)
    state["effective"] = without_plugin(effective)
    state["registry"][PLUGIN_ID] = False
    state["query_failure"] = False
else:
    assert args == ["plugin", "enable", PLUGIN_ID, "--section", "right", "--index", "0"]
    case = state["case"]
    if case == "activation-slow":
        time.sleep(2.2)
    if case == "activation-rejected":
        sys.exit("omarchy-plugin-enable: private rejection")
    if case == "activation-timeout-inactive":
        print("omarchy-shell is not responding (private timeout)", file=sys.stderr)
        sys.exit(124)
    for value in (config, effective):
        value["bar"]["layout"]["right"].insert(0, {"id": PLUGIN_ID})
    if case == "activation-disk-drift":
        config["theme"]["name"] = "private-disk-drift"
    if case == "activation-registry-drift":
        state["registry"]["local.news-ticker"] = False
    if case != "activation-partial":
        state["registry"][PLUGIN_ID] = True
    state["query_failure"] = case == "activation-query-failure"
shell.write_text(json.dumps(config, indent=2) + "\\n")
state_path.write_text(json.dumps(state))
if args[1] == "enable" and state["case"] == "activation-timeout-complete":
    print("omarchy-shell is not responding (private timeout after commit)", file=sys.stderr)
    sys.exit(124)
'''
        for name in ("fake-omarchy", "fake-omarchy-shell"):
            path = self.root / name
            path.write_text(source)
            path.chmod(0o700)
        env = dict(self.env, REHEARSAL_OMARCHY_STATE=str(self.activation_state))
        self.activation_controller = plugin_activation.OmarchyActivation(
            home=self.home, evidence=self.evidence / "activation",
            runner=self.activation_runner, env=env)

    def activation_runner(self, argv, *, env, timeout, **kwargs):
        """Exact canonical allowlist, then absolute private mapping, never PATH lookup."""
        argv = list(map(str, argv))
        allowed = [["omarchy-shell", "shell", method] for method in
                   ("listPlugins", "listShellConfig", "rescanPlugins")]
        allowed += [["omarchy", "plugin", "validate",
                     str(self.home / ".config/omarchy/plugins" / PLUGIN_ID)],
                    ["omarchy", "plugin", "enable", PLUGIN_ID, "--section", "right", "--index", "0"],
                    ["omarchy", "plugin", "disable", PLUGIN_ID]]
        require(argv in allowed, "non-private activation command forbidden")
        expected = dict(self.env, REHEARSAL_OMARCHY_STATE=str(self.activation_state),
                        OMARCHY_SHELL_IPC_TIMEOUT="10s")
        require(env == expected and timeout == plugin_activation.PROCESS_TIMEOUT,
                "non-private activation environment/deadline forbidden")
        executable = self.root / ("fake-omarchy" if argv[0] == "omarchy" else "fake-omarchy-shell")
        require(self.root.resolve(strict=True) == self.root and executable.is_file()
                and not executable.is_symlink() and executable.resolve().parent == self.root,
                "private activation executable missing or linked")
        self.record("private_activation_exec", canonical=argv, executable=str(executable))
        return plugin_activation.process([str(executable), *argv[1:]], env=expected,
                                         timeout=timeout, **kwargs)

    def activation(self, enabled):
        self.setup_activation()
        controller = self.activation_controller
        if not enabled and controller.baseline is None:
            # Compatibility for fixture initialization: observe, never mutate.
            controller.baseline = controller.snapshot()
        if enabled and self.case.startswith("activation-"):
            require(self.deployment.backup is not None, "activation fault before stopped backup")
            self.injected = True
        outcome = controller.activate() if enabled else controller.deactivate()
        self.record("private_activation", enabled=enabled, outcome=outcome.as_dict())
        if enabled and self.case == "activation-plugin-drift":
            panel = self.home / ".config/omarchy/plugins" / PLUGIN_ID / "plugin/Panel.qml"
            panel.write_bytes(panel.read_bytes() + b"\n// intentional private owned-file drift\n")
            raise RuntimeError("intentional private plugin drift after activation")
        if enabled and self.case == "activation-failure":
            raise RuntimeError("intentional private activation failure")
        return outcome

    def setup(self):
        for key in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
                    "XDG_DATA_HOME", "XDG_RUNTIME_DIR", "SWIFTWALL_STATE_DIR", "SWIFTWALL_RUNTIME_DIR"):
            Path(self.env[key]).mkdir(parents=True, exist_ok=True, mode=0o700)
        for key, value in self.env.items():
            if key.startswith("SWIFTWALL_") or key == "HOME" or key.startswith("XDG_"):
                require(Path(value).is_relative_to(self.root), "engine path outside fixture")
        require(len(os.fsencode(self.env["SWIFTWALL_RUNTIME_DIR"] + "/control.sock")) < 108,
                "Unix socket path too long")
        images = self.root / "images"
        images.mkdir(mode=0o700)
        allowed = []
        for number in range(8):
            image = images / f"{number}.png"
            image.write_bytes(png())
            allowed.append(str(image))
        background = Path(self.env["SWIFTWALL_BACKGROUND_LINK"])
        background.symlink_to(allowed[0])
        setter = Path(self.env["SWIFTWALL_OMARCHY_COMMAND"])
        setter.write_text("#!/usr/bin/python3\n" +
            "import hashlib,json,os,sys\nfrom pathlib import Path\n" +
            f"allowed = {dict.fromkeys(allowed, hashlib.sha256(png()).hexdigest())!r}\n" +
            "assert len(sys.argv)==5 and sys.argv[1:4]==['theme','bg','set']\n" +
            "p=Path(sys.argv[4])\nassert str(p) in allowed and not p.is_symlink()\n" +
            "assert hashlib.sha256(p.read_bytes()).hexdigest()==allowed[str(p)]\n" +
            f"link=Path({str(background)!r})\n" +
            f"with open({str(self.root / 'setter.jsonl')!r},'a') as s: s.write(json.dumps({{'argv':sys.argv}})+'\\n')\n" +
            "tmp=link.with_name('background-new')\ntmp.symlink_to(p)\nos.replace(tmp,link)\n")
        setter.chmod(0o700)
        config = Path(self.env["SWIFTWALL_CONFIG"])
        config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        config.write_text('interval_seconds = 300\nselection = "shuffle"\nfolders = ' + json.dumps([str(images)]) + '\n')
        (self.home / ".config/omarchy").mkdir(mode=0o700)
        self.setup_activation()
        self.command(["python3", "-B", str(SCRIPTS / "install.py"), "--home", self.home,
                      "--binary", self.args.v01])
        require(digest(self.binary) == V01, "initial installation hash mismatch")
        self.unit_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(self.unit_dir.resolve() == self.unit_dir, "runtime unit directory must not be linked")
        # Reserve paths exclusively; cleanup only paths successfully reserved here.
        for path in (self.unit_path, self.target_path):
            with path.open("x"):
                pass
            self.owned.append(path)
        self.drop_dir.mkdir(mode=0o700)
        self.owned.append(self.drop_dir)

        self.target_path.write_text("[Unit]\nDescription=Private Swiftwall rehearsal target\n")
        write_launcher(self.root)
        (self.drop_dir / "10-isolation.conf").write_text(isolation_dropin(self.root))
        self.sync_runtime_unit()
        # Start the empty target first: starting it after enable would implicitly
        # start its wanted service before the coordinator's explicit start.
        self.ctl("start", self.target)
        if self.case != "success-disabled":
            self.ctl("enable", self.unit, "--runtime")
        self.deployment = Deployment(
            home=self.home, unit=self.unit, env=self.env, v01_hash=V01,
            v02=self.args.v02, v02_hash=V02, plugin_source=self.args.plugin_source,
            backup_root=self.root / "backups", evidence=self.evidence / "coordinator",
            activate=lambda: self.activation(True), deactivate=lambda: self.activation(False),
            readiness_timeout=4, runner=self.runner,
            activation_controller=self.activation_controller)
        self.deployment.start_service()
        self.deployment.healthy("0.1.0")
        for op in ("next", "next", "pause"):
            reply = json.loads(self.command([self.binary, op]))
            require(reply.get("ok"), f"fixture {op} failed: {reply}")
            self.settled()
            self.deployment.healthy("0.1.0")
        state = json.loads(self.deployment.state.read_text())
        require(state["paused"] and len(state["carousel"]["history"]) >= 2
                and state["carousel"]["bag"], "fixture must have paused history and bag")
        self.initial_wallpaper = os.readlink(background)
        self.setter_before = (self.root / "setter.jsonl").read_bytes()
        enabled = self.deployment.service.state()["UnitFileState"]
        require(enabled == ("disabled" if self.case == "success-disabled" else "enabled-runtime"),
                f"unexpected actual enablement: {enabled}")
        self.record("fixture_ready", state=state, enabled=enabled, wallpaper=self.initial_wallpaper)

    def settled(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            reply = json.loads(self.command([self.binary, "status"]))
            status = reply["status"]
            require(reply.get("ok") and not status.get("error"), f"unhealthy fixture: {reply}")
            if not any(status.get(key) for key in ("applying", "scanning", "persistence_pending")):
                return reply
            time.sleep(.05)
        raise RuntimeError("fixture did not settle within five seconds")

    def save_sequential(self):
        settings = json.loads(self.command([self.binary, "settings"]))
        request = {"v": 1, "id": "rehearsal-save", "op": "updatesettings",
                   "if_revision": settings["revision"],
                   "settings": {**settings["settings"], "selection": "sequential"}}
        reply = json.loads(self.command([self.binary, "request", json.dumps(request)]))
        require(reply.get("ok"), f"real settings save failed: {reply}")
        self.record("sequential_settings_saved", request=request, reply=reply)
        self.deployment.healthy("0.2.0-dev", 1)
        saved = json.loads(self.command([self.binary, "settings"]))
        require(saved["settings"]["selection"] == "sequential", "settings not committed")
        require('selection = "sequential"' in self.deployment.config.read_text(), "config not persisted")

    def run(self):
        self.setup()
        d = self.deployment
        if self.case == "stale-socket":
            # Separate from readiness injection: a real stale bound Unix socket.
            d.service.stop()
            d.control_socket.unlink(missing_ok=True)
            with socket.socket(socket.AF_UNIX) as stale:
                stale.bind(str(d.control_socket))
            self.record("stale_socket_created", path=str(d.control_socket))
            d.service.start()
            d.healthy("0.1.0")
        expected = {"startup": {"daemon-exit", "activation-failure"},
                    "readiness": {"readiness-timeout"},
                    "ipc": {"ipc-failure", "protocol-incompatible"}}
        failure = None
        try:
            d.upgrade()
        except Exception as exc:
            failure = exc
        if self.case in FAULT_CASES:
            require(self.injected, "intended boundary fault was not exercised")
            require(failure is not None, "fault unexpectedly upgraded successfully")
            if self.case in expected:
                require(isinstance(failure, ServiceError) and failure.category in expected[self.case],
                        f"wrong failure boundary: {failure!r}")
            elif self.case == "installer":
                require("nonexistent-v02" in str(failure), f"wrong installer failure: {failure!r}")
            elif self.case in {"engine-postcommit", "plugin-postcommit", "plugin-timeout"}:
                marker = self.root / f"fault-{self.case}-commit.json"
                require(marker.is_file() and json.loads(marker.read_text())["child_returncode"] == 0,
                        "wrapper did not confirm a committed, reaped installer child")
                if self.case == "plugin-timeout":
                    require(isinstance(failure, subprocess.TimeoutExpired), f"wrong timeout: {failure!r}")
                else:
                    require(f"committed {self.case}" in str(failure) and "exit 74" in str(failure),
                            f"wrong postcommit failure: {failure!r}")
            elif self.case in ACTIVATION_CASES[2:]:
                outcome = self.activation_controller.last_outcome
                state, transport = {
                    "activation-partial": ("partial", "ok"),
                    "activation-rejected": ("rejected", "error"),
                    "activation-timeout-inactive": ("ambiguous", "timeout"),
                    "activation-query-failure": ("ambiguous", "ok"),
                    "activation-disk-drift": ("ambiguous", "ok"),
                    "activation-registry-drift": ("ambiguous", "ok"),
                    "activation-plugin-drift": ("success", "ok"),
                }[self.case]
                require(outcome is not None and (outcome.state, outcome.transport) == (state, transport),
                        f"wrong activation outcome: {outcome!r}")
            elif self.case == "activation-failure":
                require("intentional private activation failure" in str(failure),
                        f"wrong activation failure: {failure!r}")
            self.record("expected_failure", error=repr(failure))
        else:
            if failure is not None:
                raise failure
            self.save_sequential()
            d.rollback()
        require(d.backup is not None, "no coordinator backup")
        # Require verified rollback, or verified engine recovery with pending cleanup.
        events = [json.loads(line) for line in (d.evidence / "events.jsonl").read_text().splitlines()]
        pending = self.case in PENDING_CASES
        required_event = "rollback_engine_recovered" if pending else "rollback_passed"
        require(any(e["event"] == required_event for e in events), f"missing {required_event}")
        if pending:
            require(not any(e["event"] == "rollback_passed" for e in events),
                    "ambiguous cleanup must not claim rollback passed")
            require("engine recovered" in str(failure), "pending cleanup did not recover engine")
            require(d.widget_active == (self.case != "activation-plugin-drift"),
                    "widget cleanup state does not match failure boundary")
        reply = d.healthy("0.1.0")
        d.verify_recovery(reply)
        require(digest(self.binary) == V01, "final binary not approved v01")
        shell = self.home / ".config/omarchy/shell.json"
        if not pending:
            require(shell.read_bytes() == self.shell_baseline,
                    "private default shell configuration not restored exactly")
            state = json.loads(self.activation_state.read_text())
            require(state["registry"][PLUGIN_ID] is False, "private registry still enabled")
            require(state["effective"] == json.loads(self.shell_baseline),
                    "private effective shell configuration not restored")
        plugin_tree = self.home / ".config/omarchy/plugins/io.github.henniedebruin.swiftwall"
        plugin_manifest = self.home / ".local/state/swiftwall/plugin-install-manifest.json"
        require(os.path.lexists(plugin_tree) == pending and os.path.lexists(plugin_manifest) == pending,
                "plugin files/ownership manifest retention does not match cleanup state")
        if pending:
            state = json.loads(self.activation_state.read_text())
            if self.case == "activation-disk-drift":
                require(json.loads(shell.read_text())["theme"]["name"] == "private-disk-drift"
                        and state["effective"] == json.loads(self.shell_baseline),
                        "disk-only drift lost or leaked into effective configuration")
            elif self.case == "activation-registry-drift":
                require(state["registry"]["local.news-ticker"] is False,
                        "unrelated registry drift was overwritten")
            elif self.case == "activation-plugin-drift":
                require(b"intentional private owned-file drift" in
                        (plugin_tree / "plugin/Panel.qml").read_bytes(), "owned-file drift lost")
            self.record("pending_cleanup_verified", plugin_files="retained", engine="v01")
        require(self.unit_path.read_bytes() ==
                (self.home / ".config/systemd/user/swiftwall.service").read_bytes().replace(
                    b"graphical-session.target", self.target.encode()), "final runtime template mismatch")
        require(os.readlink(d.background) == self.initial_wallpaper, "wallpaper changed across starts")
        require((self.root / "setter.jsonl").read_bytes() == self.setter_before,
                "a deployment start invoked wallpaper setter")
        self.record("case_passed", case=self.case, state=json.loads(d.state.read_text()),
                    service=d.service.state(), reply=reply)

    def cleanup(self):
        errors = []
        # Never erase the manager's definitions (or disable/reset them) until
        # stop is independently confirmed. Retain the complete fixture if unsure.
        if self.owned:
            for unit in (self.unit, self.target):
                try:
                    self.ctl("stop", unit)
                except BaseException as exc:
                    errors.append(f"stop {unit}: {exc!r}")
            try:
                output = self.command(["systemctl", "--user", "show", self.unit,
                                       "-p", "ActiveState", "-p", "MainPID"])
                state = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
                require(state.get("ActiveState") in {"inactive", "failed"}
                        and state.get("MainPID") == "0", f"shutdown unconfirmed: {state}")
                self.record("cleanup_shutdown_confirmed", state=state)
            except BaseException as exc:
                errors.append(f"shutdown confirmation: {exc!r}")
            if not errors:
                for action, unit in (("disable", self.unit), ("disable", self.target),
                                     ("reset-failed", self.unit), ("reset-failed", self.target)):
                    try:
                        argv = ["systemctl", "--user", action, unit]
                        if action == "disable":
                            argv.append("--runtime")
                        result = self.raw(argv, timeout=30)
                        if result.returncode and action != "reset-failed":
                            errors.append(f"{action} {unit}: {result.stderr}")
                    except BaseException as exc:
                        errors.append(repr(exc))
            try:
                self.raw(["journalctl", "--user", "--unit", self.unit, "--no-pager", "-n", "300"], timeout=10)
            except BaseException as exc:
                errors.append(repr(exc))
        # Capture before removing private roots, without following background symlinks.
        try:
            destination = self.evidence / "fixture"
            shutil.copytree(self.root, destination, symlinks=True,
                            ignore=shutil.ignore_patterns("control.sock"))
            for source in self.owned:
                if source.is_file():
                    shutil.copy2(source, self.evidence / source.name)
                elif source.is_dir():
                    shutil.copytree(source, self.evidence / source.name)
        except BaseException as exc:
            errors.append("evidence copy: " + repr(exc))
        if errors:
            self.record("cleanup_retained_definitions", root=str(self.root),
                        paths=list(map(str, self.owned)), errors=errors)
            return errors
        for path in reversed(self.owned):
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            except BaseException as exc:
                errors.append(repr(exc))
        if self.owned:
            try:
                self.reload()
            except BaseException as exc:
                errors.append(repr(exc))
        # Do not remove an executable/root from underneath a possibly surviving daemon.
        if not errors:
            shutil.rmtree(self.root)
        else:
            self.record("cleanup_retained_root", path=str(self.root), errors=errors)
        return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("v01", "v02", "plugin-source", "evidence"):
        parser.add_argument("--" + option, required=True, type=absolute_existing)
    parser.add_argument("--cases", type=selected_cases, default=list(DEFAULT_CASES),
                        help="comma-separated cases; default: " + ",".join(DEFAULT_CASES))
    parser.add_argument("--case", action="append", choices=CASES, default=[],
                        help="append an optional case, e.g. --case success-disabled")
    args = parser.parse_args(argv)
    require(args.v01.is_file() and digest(args.v01) == V01, "v01 is not the approved artifact")
    require(args.v02.is_file() and digest(args.v02) == V02, "v02 is not the approved artifact")
    require(args.plugin_source.is_dir() and args.evidence.is_dir(), "source/evidence must be directories")
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", "/nonexistent"))
    require(runtime.is_absolute() and runtime.is_dir() and runtime.stat().st_uid == os.getuid()
            and not stat.S_IMODE(runtime.stat().st_mode) & 0o077, "private real XDG_RUNTIME_DIR required")
    cases = list(dict.fromkeys([*args.cases, *args.case]))
    run_root = Path(tempfile.mkdtemp(prefix="deployment-rehearsal-", dir=args.evidence))
    summary = {"simulation": "real activation controller with private fake Omarchy; REAL binaries and systemd",
               "production_baseline": "caller-owned; no production queries performed",
               "v01": str(args.v01), "v02": str(args.v02), "hashes": [V01, V02],
               "cases": [], "argv": sys.argv if argv is None else argv}

    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for case in cases:
            evidence = run_root / case
            evidence.mkdir(mode=0o700)
            fixture = Rehearsal(args, case, evidence)
            result = {"case": case, "passed": False}
            try:
                fixture.run()
                result["passed"] = True
            except BaseException as exc:
                result.update(error=repr(exc), traceback=traceback.format_exc())
            finally:
                # A second signal must not interrupt fixture cleanup.
                for sig in previous:
                    signal.signal(sig, signal.SIG_IGN)
                result["cleanup_errors"] = fixture.cleanup()
                for sig in previous:
                    signal.signal(sig, interrupted)
                if result["cleanup_errors"]:
                    result["passed"] = False
                (evidence / "result.json").write_text(json.dumps(result, indent=2) + "\n")
                summary["cases"].append(result)
                (run_root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
            print(f"{case}: {'PASS' if result['passed'] else 'FAIL'} ({evidence})", flush=True)
            if not result["passed"]:
                break
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(f"Evidence: {run_root}")
    return 0 if len(summary["cases"]) == len(cases) and all(c["passed"] for c in summary["cases"]) else 1


if __name__ == "__main__":
    sys.exit(main())
