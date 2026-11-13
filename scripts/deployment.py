"""Supervised deployment coordinator shared by rehearsal and future live deployment.

No import side effects or default live CLI. The caller explicitly supplies the
home, service, environment, artifact hashes and backup/evidence locations. Real-home
calls default to reconciled Omarchy activation; isolated callers must provide a
private controller/transport. Engine/plugin installers remain file-only.
"""

import hashlib
import json
import os
import secrets
from pathlib import Path
import shutil
import socket
import stat
import struct
import subprocess
import tempfile

from service_control import UserService
from plugin_activation import OmarchyActivation
# Re-export for existing rehearsal callers; activation uses the same safe writer.
from evidence_support import private_evidence_directory, private_evidence_log
import install_support
from release_bundle import validate_version


PLUGIN_ID = "io.github.henniedebruin.swiftwall"
ENGINE_ENVIRONMENT = ("HOME", "SWIFTWALL_CONFIG", "SWIFTWALL_STATE_DIR",
                      "SWIFTWALL_RUNTIME_DIR", "SWIFTWALL_BACKGROUND_LINK", "SWIFTWALL_OMARCHY_COMMAND")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def private_command_output(args):
    # Suppress all show output, regardless of property syntax. In particular,
    # Environment may contain unrelated API credentials. Validation still gets
    # the unmodified result in memory.
    return Path(args[0]).name == "systemctl" and "show" in args[1:]


class Deployment:
    def __init__(self, *, target_version, home, unit, env, v01_hash, v02, v02_hash,
                 plugin_source, backup_root, evidence, activate=None, deactivate=None,
                 readiness_timeout=10, runner=subprocess.run, activation_controller=None):
        self.target_version = validate_version(target_version)
        self.home = Path(home).resolve(strict=True)
        self.env = dict(env)
        self.engine_environment = {key: self.env[key] for key in ENGINE_ENVIRONMENT}
        self.v01_hash = v01_hash
        self.v02 = Path(v02).resolve(strict=True)
        self.v02_hash = v02_hash
        self.source = Path(plugin_source).resolve(strict=True)
        self.scripts = Path(__file__).resolve().parent
        self.backup_root = Path(backup_root)
        self.evidence = Path(evidence).absolute()
        with private_evidence_directory(self.evidence) as directory:
            stream = private_evidence_log(directory, create=False)
            if stream is not None:
                stream.close()
        if (activate is None) != (deactivate is None):
            raise ValueError("Supply both activation callbacks or neither")
        if (activate is not None and activation_controller is None
                and self.home == Path.home().resolve()):
            raise ValueError("Real-home activation callbacks require a reconciled controller")
        self.activation_controller = activation_controller
        if activate is None:
            if self.activation_controller is None:
                if self.home != Path.home().resolve():
                    raise ValueError("Isolated deployment requires explicit private activation callbacks")
                self.activation_controller = OmarchyActivation(home=self.home, evidence=self.evidence)
            activate = self.activation_controller.activate
            deactivate = self.activation_controller.deactivate
        self.activate = activate
        self.deactivate = deactivate
        self.timeout = readiness_timeout
        self.runner = runner
        self.service = UserService(unit, runner=self.execute)
        self.binary = self.home / ".local/bin/swiftwall"
        self.config = Path(self.env["SWIFTWALL_CONFIG"])
        self.state = Path(self.env["SWIFTWALL_STATE_DIR"]) / "state.json"
        self.background = Path(self.env["SWIFTWALL_BACKGROUND_LINK"])
        self.control_socket = Path(self.env["SWIFTWALL_RUNTIME_DIR"]) / "control.sock"
        self.backup = None
        self.plugin_installed = False
        self.engine_install_attempted = False
        self.plugin_install_attempted = False
        self.widget_active = False
        self.original = None
        # Never infer HOME or IPC destinations from the supervising editor process.
        if Path(self.env["HOME"]) != self.home:
            raise ValueError("Explicit HOME must match installation home")
        for path in (self.config, self.state, self.background, self.control_socket,
                     Path(self.env["SWIFTWALL_OMARCHY_COMMAND"])):
            if not path.is_absolute():
                raise ValueError("Explicit absolute configuration/state/background/socket required")
        if self.control_socket == Path(os.environ.get("XDG_RUNTIME_DIR", "/nonexistent")) / "swiftwall/control.sock" and self.home != Path.home():
            raise ValueError("Isolated deployment must not use the production runtime socket")

    def record(self, event, **fields):
        with private_evidence_directory(self.evidence) as directory:
            with private_evidence_log(directory, create=True) as stream:
                stream.write(json.dumps({"event": event, **fields}) + "\n")

    def execute(self, args, **kwargs):
        # systemctl needs the real session bus. Every engine CLI invocation gets
        # the explicit environment checked against the selected service. Passing
        # env to systemctl does NOT configure a service's execution environment.
        args = list(map(str, args))
        private = private_command_output(args)
        kwargs["env"] = self.env if args[0] == str(self.binary) else os.environ.copy()
        query_failed = False
        try:
            result = self.runner(args, **kwargs)
        except Exception:
            if not private:
                raise
            query_failed = True
        if query_failed:
            # Runner exceptions (including timeout/CalledProcessError payloads)
            # can contain partial stdout/stderr. Never log or chain them.
            self.record("command", args=args, output_omitted=True, outcome="query-failed")
            raise RuntimeError("Service property query failed; output omitted")
        output = {"output_omitted": True} if private else {"stdout": result.stdout, "stderr": result.stderr}
        self.record("command", args=args, returncode=result.returncode, **output)
        return result

    def command(self, args):
        result = self.execute(args, capture_output=True, text=True, timeout=20)
        if result.returncode:
            if private_command_output(list(map(str, args))):
                raise RuntimeError(f"Service property query failed (exit {result.returncode}); output omitted")
            raise RuntimeError(f"Command failed (exit {result.returncode}): {args}: {result.stderr}{result.stdout}")
        return result.stdout

    def installer(self, *args):
        return self.command(["python3", "-B", str(self.scripts / "install.py"),
                             "--home", str(self.home), *map(str, args)])

    def plugin(self, operation, *, dry_run=False):
        args = ["python3", "-B", str(self.scripts / "plugin_install.py"), operation,
                "--home", str(self.home)]
        if operation == "install":
            args += ["--source", str(self.source)]
        if dry_run:
            args += ["--dry-run"]
        return self.command(args)

    def socket_owner(self):
        expected = int(self.service.state()["MainPID"])
        if not expected:
            raise RuntimeError("Service has no PID for socket verification")
        with socket.socket(socket.AF_UNIX) as peer:
            peer.settimeout(2)
            peer.connect(str(self.control_socket))
            pid, uid, _ = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if pid != expected or uid != os.getuid():
            raise RuntimeError("Selected socket is not owned by selected service")
        self.record("socket_owner", pid=pid, uid=uid, path=str(self.control_socket))

    def running_executable(self):
        before = self.service.state()
        pid = int(before["MainPID"])
        if not pid:
            raise RuntimeError("Service has no running executable")
        # --version describes the disk binary, not necessarily the running one.
        running_hash = digest(Path("/proc") / str(pid) / "exe")
        if running_hash != digest(self.binary):
            raise RuntimeError("Running executable differs from installed engine")
        confirmed = self.service.state()
        if int(confirmed["MainPID"]) != pid or confirmed["NRestarts"] != before["NRestarts"]:
            raise RuntimeError("Service changed during socket/executable verification")
        self.record("running_executable_verified", pid=pid, sha256=running_hash)

    def healthy(self, version, protocol=None):
        self.service.verify_environment(self.engine_environment)
        self.running_environment()
        reply = self.service.wait_ready(self.binary, version=version, protocol=protocol,
                                        timeout=self.timeout)
        self.socket_owner()
        self.running_executable()
        self.record("healthy", version=version, reply=reply, service=self.service.state())
        return reply

    def running_environment(self):
        pid = int(self.service.state()["MainPID"])
        if pid <= 0:
            raise RuntimeError("Service has no PID for environment verification")
        raw = (Path("/proc") / str(pid) / "environ").read_bytes()
        actual = dict(os.fsdecode(entry).split("=", 1) for entry in raw.split(b"\0") if entry)
        if any(actual.get(key) != value for key, value in self.engine_environment.items()):
            raise RuntimeError("Running service environment differs from explicit engine destinations")
        if int(self.service.state()["MainPID"]) != pid:
            raise RuntimeError("Service changed during environment verification")
        self.record("running_environment_verified", pid=pid)

    def start_service(self):
        self.service.verify_environment(self.engine_environment)
        self.service.start()

    def capture_backup(self):
        self.backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        pending = Path(tempfile.mkdtemp(prefix="stopped-v01-", dir=self.backup_root))
        paths = {"binary": self.binary, "config": self.config, "state": self.state,
                 "unit": self.home / ".config/systemd/user/swiftwall.service",
                 "installer_manifest": self.home / ".local/state/swiftwall/install-manifest.json"}
        for name, path in (("shell_config", self.home / ".config/omarchy/shell.json"),
                           ("plugin_manifest", self.home / ".local/state/swiftwall/plugin-install-manifest.json")):
            if path.exists():
                paths[name] = path
        records = {}
        for name, path in paths.items():
            info = path.stat()
            destination = pending / name
            shutil.copy2(path, destination)
            with destination.open("rb") as stream:
                os.fsync(stream.fileno())
            records[name] = {"path": str(path), "sha256": digest(path),
                             "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid,
                             "gid": info.st_gid}
            if (digest(destination) != records[name]["sha256"]
                    or stat.S_IMODE(destination.stat().st_mode) != records[name]["mode"]):
                raise RuntimeError("Stopped backup verification failed")
        manifest = pending / "manifest.json"
        with manifest.open("x") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(records, indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        for directory in (pending, self.backup_root):
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        # Never expose an incomplete backup as an available rollback checkpoint.
        self.backup = pending
        self.record("backup_verified", path=str(self.backup), records=records)

    def verify_recovery(self, reply):
        saved = json.loads((self.backup / "state").read_text())
        current = json.loads(self.state.read_text())
        if current["carousel"] != saved["carousel"] or current["paused"] != saved["paused"]:
            raise RuntimeError("Startup changed saved carousel or pause")
        if self.config.read_bytes() != (self.backup / "config").read_bytes():
            raise RuntimeError("Startup changed configuration")
        if os.readlink(self.background) != self.original["wallpaper"]:
            raise RuntimeError("Startup changed current wallpaper")
        if reply["status"]["observed_wallpaper"] != self.original["wallpaper"]:
            raise RuntimeError("Observed wallpaper differs from pre-start state")
        if self.service.state()["UnitFileState"] != self.original["enabled"]:
            raise RuntimeError("Autostart state changed")

    def upgrade(self):
        """One attempt; automatically recover once if a post-stop step fails."""
        if self.backup is not None:
            raise RuntimeError("Coordinator already used; no implicit deployment retries")
        if digest(self.v02) != self.v02_hash or digest(self.binary) != self.v01_hash:
            raise RuntimeError("Unapproved engine artifact")
        # This coordinator currently supports first plugin installation only.
        # Updating/no-op reinstall needs generation-aware widget restoration.
        plugin_path = self.home / ".config/omarchy/plugins" / PLUGIN_ID
        shell_config = self.home / ".config/omarchy/shell.json"
        if (plugin_path.exists() or plugin_path.is_symlink()
                or (self.home / ".local/state/swiftwall/plugin-install-manifest.json").exists()
                or (shell_config.exists() and PLUGIN_ID in shell_config.read_text())):
            raise RuntimeError("First-install coordinator refuses existing SwiftWall plugin/settings")
        self.service.verify_environment(self.engine_environment)
        if self.activation_controller is not None:
            self.activation_controller.prepare()
        before = self.healthy("0.1.0")
        self.installer("--binary", self.v02, "--dry-run")
        self.plugin("install", dry_run=True)
        self.original = {"enabled": self.service.state()["UnitFileState"],
                         "wallpaper": os.readlink(self.background), "status": before}
        try:
            self.service.stop()
            # Read after shutdown, not a stale pre-stop wallpaper snapshot.
            self.original["wallpaper"] = os.readlink(self.background)
            self.capture_backup()
            self.installer("--binary", self.v02, "--dry-run")
            self.engine_install_attempted = True
            self.installer("--binary", self.v02)
            unit = self.home / ".config/systemd/user/swiftwall.service"
            if unit.read_bytes() != (self.backup / "unit").read_bytes():
                self.command(["systemctl", "--user", "daemon-reload"])
            self.start_service()
            reply = self.healthy(self.target_version, 1)
            self.verify_recovery(reply)
            settings = json.loads(self.command([str(self.binary), "settings"]))
            self.record("settings", reply=settings)
            self.plugin("install", dry_run=True)
            if self.activation_controller is not None:
                # Publication can trigger shell autodiscovery, even if the
                # installer later loses its reply or fails after committing.
                self.widget_active = True
            self.plugin_install_attempted = True
            self.plugin("install")
            self.plugin_installed = True
            self.plugin("verify")
            # Activation can partially succeed before its verification raises.
            # Arm deactivation first so rollback also covers that boundary.
            self.widget_active = True
            activation = self.activate()
            if hasattr(activation, "as_dict"):
                self.record("activation_reconciled", **activation.as_dict())
            self.record("upgrade_passed", reply=reply)
            return reply
        except Exception as exc:
            fields = {"outcome": exc.outcome.as_dict()} if hasattr(exc, "outcome") else {}
            self.record("upgrade_failed", detail=repr(exc), **fields)
            if self.backup is None:
                # No installer ran: don't attempt file rollback from an incomplete
                # backup. Reconcile stop outcome before restarting unchanged v0.1.
                state = self.service.state()
                if digest(self.binary) != self.v01_hash:
                    raise RuntimeError("Pre-install recovery requires manual inspection") from exc
                if state.get("MainPID") == "0" and state.get("ActiveState") in ("inactive", "failed"):
                    self.start_service()
                self.healthy("0.1.0")
                self.record("preinstall_recovered")
            else:
                self.rollback()
            raise

    def rollback(self):
        if self.backup is None:
            raise RuntimeError("No stopped-state backup; manual recovery required")
        # Idempotent caller guard: don't consume an unrelated previous generation.
        self.service.stop()
        # Never reuse predictable children of formerly shared evidence. Keep each
        # attempt's inputs in a newly created private directory, pinned by fd.
        with private_evidence_directory(self.evidence) as directory:
            name = "rollback-input-" + secrets.token_hex(16)
            os.mkdir(name, mode=0o700, dir_fd=directory)
            snapshot = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                               dir_fd=directory)
            try:
                for source, filename in ((self.config, "config"), (self.state, "state")):
                    fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=snapshot)
                    with os.fdopen(fd, "wb") as destination, source.open("rb") as stream:
                        shutil.copyfileobj(stream, destination)
                        destination.flush()
                        os.fsync(destination.fileno())
            finally:
                os.close(snapshot)
        self.record("rollback_inputs_captured", directory=name)
        cleanup_error = None
        if self.widget_active:
            try:
                self.deactivate()
                self.widget_active = False
            except Exception as exc:
                # Unknown shell state must not prevent restoring the engine. Keep
                # plugin files so loaded/late shell state isn't silently orphaned.
                cleanup_error = exc
                self.record("widget_cleanup_pending", detail=repr(exc))
        if self.plugin_install_attempted and cleanup_error is None:
            plugin_path = self.home / ".config/omarchy/plugins" / PLUGIN_ID
            metadata = self.home / ".local/state/swiftwall/plugin-install-manifest.json"
            try:
                if plugin_path.exists() or metadata.exists():
                    # A failed/timeout installer may already have committed.
                    # Ownership failure must not block restoring the engine.
                    self.plugin("verify")
                    self.plugin("uninstall", dry_run=True)
                    self.plugin("uninstall")
                self.plugin_installed = False
            except Exception as exc:
                cleanup_error = exc
                self.record("plugin_cleanup_pending", detail=repr(exc))
        if self.engine_install_attempted:
            try:
                manifest = install_support.load_manifest(self.home)
                install_support.verify_owned(self.home, manifest)
            except (OSError, install_support.InstallError) as exc:
                raise RuntimeError(f"Inconsistent owned engine generation; manual recovery from {self.backup}") from exc
            if manifest is None:
                raise RuntimeError(f"Missing engine ownership; recover from {self.backup}")
            current_hash = manifest["current"]["binary"]["sha256"]
            if current_hash == self.v02_hash:
                self.installer("--rollback", "--dry-run")
                self.installer("--rollback")
            elif current_hash != self.v01_hash:
                raise RuntimeError(f"Unknown owned engine generation; recover from {self.backup}")
        if digest(self.binary) != self.v01_hash:
            raise RuntimeError("Rollback did not restore approved v0.1")
        # Restore v0.1-compatible settings if the widget saved sequential mode.
        if self.config.read_bytes() != (self.backup / "config").read_bytes():
            fd, temporary = tempfile.mkstemp(prefix=".swiftwall-rollback-", dir=self.config.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write((self.backup / "config").read_bytes())
                    stream.flush()
                    os.fchmod(stream.fileno(), stat.S_IMODE((self.backup / "config").stat().st_mode))
                    os.fsync(stream.fileno())
                os.replace(temporary, self.config)
            finally:
                Path(temporary).unlink(missing_ok=True)
        # Latest schema-1 state is intentionally retained; don't restore stale history.
        self.command(["systemctl", "--user", "daemon-reload"])
        wallpaper = os.readlink(self.background)
        self.start_service()
        reply = self.healthy("0.1.0")
        if os.readlink(self.background) != wallpaper:
            raise RuntimeError("Rollback startup changed wallpaper")
        if self.service.state()["UnitFileState"] != self.original["enabled"]:
            raise RuntimeError("Rollback changed original autostart state")
        if self.config.read_bytes() != (self.backup / "config").read_bytes():
            raise RuntimeError("Rollback configuration mismatch")
        if cleanup_error is not None:
            self.record("rollback_engine_recovered", reply=reply, widget_cleanup="pending",
                        plugin_files="retained", detail=repr(cleanup_error))
            raise RuntimeError("v0.1 engine recovered; widget/plugin cleanup remains unverified; "
                               "remaining plugin files retained for manual reconciliation") from cleanup_error
        self.record("rollback_passed", reply=reply)
        return reply
