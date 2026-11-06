"""Bounded, supervised systemd user-service operations; no implicit restart loop.

Callers own backups, file installation, rollback and enablement decisions. Merely
importing this module does nothing. Unit names and binary paths are structured
arguments; there is no shell execution. Filesystem/kernel stalls are not cancellable
by these userspace deadlines.
"""

import json
import shlex
import subprocess
import time
from pathlib import Path


class ServiceError(RuntimeError):
    def __init__(self, category, detail):
        self.category = category
        super().__init__(f"{category}: {detail}")


class UserService:
    def __init__(self, unit="swiftwall.service", runner=subprocess.run,
                 clock=time.monotonic, sleep=time.sleep):
        self.unit = unit
        self.runner = runner
        self.clock = clock
        self.sleep = sleep

    def command(self, args, timeout=10):
        try:
            return self.runner(args, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise ServiceError("command-timeout", str(args)) from exc

    def systemctl(self, action, timeout=10):
        result = self.command(["systemctl", "--user", action, self.unit], timeout)
        if result.returncode:
            raise ServiceError("activation-failure" if action == "start" else "manager-failure",
                               result.stderr.strip() or result.stdout.strip())
        return result

    def state(self, timeout=5):
        result = self.command([
            "systemctl", "--user", "show", self.unit,
            "-p", "LoadState", "-p", "ActiveState", "-p", "SubState",
            "-p", "MainPID", "-p", "NRestarts", "-p", "Result",
            "-p", "UnitFileState",
        ], timeout)
        if result.returncode:
            raise ServiceError("manager-failure", result.stderr.strip())
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

    def verify_environment(self, expected):
        """Require explicit loaded unit values; systemctl's own env is irrelevant.

        Deliberately narrow contract: no environment files, PAM injection or
        pass/unset directives. Ambiguous C-escaped show output is refused rather
        than decoded with shell rules. Never create a drop-in here.
        """
        properties = ("Environment", "EnvironmentFiles", "PassEnvironment", "UnsetEnvironment", "PAMName")
        query_failed = False
        try:
            result = self.command(["systemctl", "--user", "show", self.unit,
                                   *[arg for key in properties for arg in ("-p", key)]])
        except Exception:
            # A runner may include partial environment output in an exception.
            query_failed = True
        if query_failed:
            raise ServiceError("environment-unverified", "Cannot inspect loaded unit environment")
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if result.returncode or any(key not in values for key in properties):
            raise ServiceError("environment-unverified", "Cannot inspect loaded unit environment")
        if any(values[key] for key in properties[1:]):
            raise ServiceError("environment-unverified", "Use explicit Environment= values; EnvironmentFiles, pass/unset and PAM are unsupported")
        ambiguous = False
        try:
            if "\\" in values["Environment"]:
                raise ValueError("escaped output")
            entries = shlex.split(values["Environment"])
            configured = dict(entry.split("=", 1) for entry in entries)
            if len(configured) != len(entries):
                raise ValueError("duplicate entries")
        except ValueError:
            ambiguous = True
        if ambiguous:
            raise ServiceError("environment-unverified", "Ambiguous unit Environment output")
        mismatched = sorted(key for key, value in expected.items() if configured.get(key) != value)
        unexpected = sorted(key for key in configured if key.startswith("SWIFTWALL_") and key not in expected)
        if mismatched or unexpected:
            # Do not echo untrusted names or values from the service environment.
            raise ServiceError("environment-mismatch", "Required values differ or unsupported SWIFTWALL_ overrides")

    def stop(self):
        # Preserve enabled/disabled state. Stop waits for shutdown, within a bound.
        self.systemctl("stop", timeout=30)
        state = self.state()
        if state.get("ActiveState") not in ("inactive", "failed") or state.get("MainPID") != "0":
            raise ServiceError("shutdown-failure", str(state))

    def start(self):
        state = self.state()
        if state.get("ActiveState") not in ("inactive", "failed"):
            raise ServiceError("unsafe-start", str(state))
        # A normally inactive/unloaded unit is not a failed application. Start loads
        # an available unit itself. Never unconditionally reset an unloaded unit.
        if state.get("LoadState") == "loaded" and state.get("ActiveState") == "failed":
            self.systemctl("reset-failed")
        self.systemctl("start", timeout=15)

    def wait_ready(self, binary, *, version, protocol=None, timeout=10):
        deadline = self.clock() + timeout
        expected_pid = None
        initial_restarts = None
        last_delay = "socket not ready"

        def budget(limit):
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise ServiceError("readiness-timeout", last_delay)
            return min(limit, remaining)

        def bounded(operation):
            try:
                return operation()
            except ServiceError as exc:
                if exc.category == "command-timeout" and self.clock() >= deadline:
                    raise ServiceError("readiness-timeout", last_delay) from exc
                raise

        while self.clock() < deadline:
            state = bounded(lambda: self.state(timeout=budget(3)))
            if (state.get("ActiveState") in ("failed", "inactive", "deactivating")
                    or state.get("SubState") == "auto-restart"):
                raise ServiceError("daemon-exit", str(state))
            pid = state.get("MainPID", "0")
            restarts = state.get("NRestarts", "0")
            if initial_restarts is None:
                initial_restarts = restarts
            if restarts != initial_restarts:
                raise ServiceError("daemon-restarted", str(state))
            if pid != "0":
                if expected_pid is not None and pid != expected_pid:
                    raise ServiceError("daemon-restarted", str(state))
                expected_pid = pid
            # The public status CLI deliberately returns the legacy envelope even
            # on v0.2. Negotiate metadata through the explicit JSON IPC client.
            probe = ([str(Path(binary)), "request", json.dumps({
                "v": protocol, "id": "deployment-health", "op": "status",
            })] if protocol is not None else [str(Path(binary)), "status"])
            result = bounded(lambda: self.command(probe, timeout=budget(2)))
            if result.returncode:
                diagnostic = result.stderr + result.stdout
                if "Daemon unavailable" not in diagnostic:
                    raise ServiceError("ipc-failure", diagnostic.strip())
                last_delay = diagnostic.strip()
            else:
                try:
                    reply = json.loads(result.stdout)
                    status = reply["status"]
                    if not reply.get("ok") or not isinstance(status, dict):
                        raise ValueError("unsuccessful status response")
                    if protocol is not None and (reply.get("protocol_version") != protocol
                                                 or reply.get("daemon_version") != version):
                        raise ValueError("unexpected protocol/daemon version")
                except (ValueError, KeyError, TypeError) as exc:
                    raise ServiceError("protocol-incompatible", result.stdout[:1024]) from exc
                if any(status.get(key) for key in ("error", "last_persistence_error", "config_worker_error")):
                    raise ServiceError("daemon-unhealthy", str(status))
                if not status.get("scanning") and not status.get("persistence_pending"):
                    installed = bounded(lambda: self.command([str(Path(binary)), "--version"], timeout=budget(2)))
                    if installed.returncode or installed.stdout.strip() != f"swiftwall {version}":
                        raise ServiceError("version-incompatible", installed.stdout + installed.stderr)
                    if not expected_pid or state.get("ActiveState") != "active":
                        raise ServiceError("daemon-unhealthy", str(state))
                    return reply
                last_delay = "daemon responding; initial scan/persistence pending"
            self.sleep(min(.1, max(0, deadline - self.clock())))
        raise ServiceError("readiness-timeout", last_delay)
