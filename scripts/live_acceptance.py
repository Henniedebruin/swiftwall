"""Explicitly authorized LIVE acceptance test. Changes wallpaper, then restores it.

Run only with --allow-live-desktop. --allow-installed-service additionally
authorizes stopping/restoring the exact installed user unit, without changing
enablement or installation files. The test daemon always uses private paths.
Scope: engine/CLI and SwiftView integration, NOT native widget/chooser acceptance.
The independent restoration guardian survives parent interruption. Artifacts include
private desktop captures and a local emergency backup: never publish artifacts/.
"""

import argparse
import fcntl
import hashlib
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
COLLECTION = Path("/usr/share/omarchy/themes/tokyo-night/backgrounds")
LINK = Path.home() / ".local/state/omarchy/current/background"
THEME = LINK.parent / "theme.name"
REPORT = ROOT / "artifacts/live-acceptance"
BINARY = ROOT / "target/release/swiftwall"
PROC = Path("/proc")
UNIT = "swiftwall.service"
SWIFTVIEW_MAX_RGBA_BYTES = 128 * 1024 * 1024
SERVICE_PROPERTIES = (
    "Id", "LoadState", "ActiveState", "SubState", "UnitFileState", "MainPID",
    "FragmentPath", "ControlGroup", "NeedDaemonReload", "DropInPaths",
)
DESKTOP_VARIABLES = (
    "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE",
    "DBUS_SESSION_BUS_ADDRESS", "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE",
    "DISPLAY", "XAUTHORITY",
)


def run(args, **kwargs):
    args = [str(a) for a in args]
    # The installed Hyprland 0.56 uses Lua dispatch expressions. Values remain
    # quoted data in a structured argv entry; no shell is involved.
    if args[:3] == ["hyprctl", "dispatch", "workspace"]:
        args = ["hyprctl", "dispatch", "hl.dsp.focus({ workspace = " + json.dumps(args[3]) + " })"]
    elif args[:3] == ["hyprctl", "dispatch", "focuswindow"]:
        args = ["hyprctl", "dispatch", "hl.dsp.focus({ window = " + json.dumps(args[3]) + " })"]
    return subprocess.run(args, check=True, capture_output=True, timeout=20, **kwargs)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, data):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(data, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def proc_start(pid):
    try:
        return (PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def service_state():
    response = run(["systemctl", "--user", "--no-pager", "show", UNIT,
                    *[arg for key in SERVICE_PROPERTIES for arg in ("-p", key)]])
    state = dict(line.split("=", 1) for line in response.stdout.decode().splitlines() if "=" in line)
    if any(key not in state for key in SERVICE_PROPERTIES):
        raise RuntimeError("Incomplete service state evidence")
    return state


def competitors(expected=()):
    result = subprocess.run(["pgrep", "-x", "swiftwall"], capture_output=True, check=False, timeout=5)
    if result.returncode not in (0, 1):
        raise RuntimeError("Cannot inspect SwiftWall competitors")
    pids = {int(pid) for pid in result.stdout.split()}
    if (result.returncode == 1 and pids) or pids != set(expected):
        raise RuntimeError(f"Unmanaged or changed SwiftWall processes: {sorted(pids)}; expected {sorted(expected)}")


def service_identity(state):
    if (state["Id"] != UNIT or state["LoadState"] != "loaded"
            or state["NeedDaemonReload"] != "no" or state["DropInPaths"]
            or not state["UnitFileState"]):
        raise RuntimeError("Installed unit must be exact, loaded and unchanged, with no drop-ins")
    fragment = Path(state["FragmentPath"])
    if not fragment.is_absolute() or not fragment.is_file():
        raise RuntimeError("Installed unit fragment unavailable")
    return {"fragment_hash": digest(fragment)}


def managed_pid(state, allow_failed=False):
    pid = int(state["MainPID"])
    if state["ActiveState"] == "inactive" and state["SubState"] == "dead" and pid == 0:
        return None
    if allow_failed and state["ActiveState"] == "failed" and pid == 0:
        return None
    if state["ActiveState"] != "active" or state["SubState"] != "running" or pid <= 1:
        raise RuntimeError("Service is not stably running or inactive")
    group = state["ControlGroup"]
    groups = [line.split(":", 2)[2] for line in (PROC / str(pid) / "cgroup").read_text().splitlines()]
    if not group or group not in groups or (PROC / str(pid) / "exe").resolve().name != "swiftwall":
        raise RuntimeError("MainPID ownership could not be verified")
    start = proc_start(pid)
    if not start:
        raise RuntimeError("MainPID disappeared")
    return {"pid": pid, "start": start}


def snapshot_service(allow_installed):
    state = service_state()
    if not allow_installed:
        if state["LoadState"] != "not-found" or state["ActiveState"] != "inactive":
            raise RuntimeError("Installed service requires explicit --allow-installed-service")
        user_unit = Path.home()/".config/systemd/user"/UNIT
        if user_unit.exists() or user_unit.is_symlink():
            raise RuntimeError("Unexpected installed unit file")
        competitors()
        return state, None
    identity = service_identity(state)
    process = managed_pid(state)
    if process:
        binary = (PROC / str(process["pid"]) / "exe").resolve(strict=True)
        identity["binary"] = {"path": str(binary), "hash": digest(binary)}
    competitors([process["pid"]] if process else [])
    if service_state() != state:
        raise RuntimeError("Service changed during snapshot")
    return state, {"state": state, **identity, "process": process}


def verify_service(record):
    original = record["installed_service"]
    state = service_state()
    if service_identity(state)["fragment_hash"] != original["fragment_hash"]:
        raise RuntimeError("Installed unit contents changed")
    if original.get("binary") and digest(original["binary"]["path"]) != original["binary"]["hash"]:
        raise RuntimeError("Installed binary changed")
    for key in ("Id", "LoadState", "FragmentPath", "UnitFileState", "DropInPaths"):
        if state[key] != original["state"][key]:
            raise RuntimeError(f"Installed service {key} changed; refusing lifecycle operation")
    return state


def stable_service(record):
    # A killed systemctl client does not cancel a manager job. Give an in-flight
    # stop/start bounded time to finish before deciding whether to restore it.
    until = time.monotonic() + 30
    while True:
        state = verify_service(record)
        if state["ActiveState"] not in ("activating", "deactivating", "reloading"):
            return state
        if time.monotonic() >= until:
            raise RuntimeError("Service lifecycle job did not settle; restoration remains armed")
        time.sleep(0.1)


def stop_installed(record):
    state = verify_service(record)
    process = managed_pid(state)
    if process != record["installed_service"]["process"]:
        raise RuntimeError("Installed service PID changed before stop")
    competitors([process["pid"]] if process else [])
    if process:
        run(["systemctl", "--user", "stop", UNIT])
    state = verify_service(record)
    if managed_pid(state) is not None:
        raise RuntimeError("Installed service did not stop")
    competitors()


def stop_test_daemon(record):
    pid, start = record.get("daemon_pid"), record.get("daemon_start")
    if not pid or not start or proc_start(pid) != start:
        return
    # Pin the process before the second identity check. A numeric PID can be
    # reused between reading /proc and signalling; a pidfd cannot target its
    # replacement. No fallback to broad process-name kills or numeric signals.
    try:
        process_fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        if proc_start(pid) != start:
            return False
        try:
            signal.pidfd_send_signal(process_fd, signal.SIGTERM)
        except ProcessLookupError:
            return False
        forced = False
        until = time.monotonic() + 15
        while proc_start(pid) == start:
            try:
                if (PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                    return forced
            except FileNotFoundError:
                return forced
            if time.monotonic() >= until:
                if forced:
                    raise RuntimeError("Test daemon did not stop; refusing wallpaper/service restoration")
                # Retain the old forced-shutdown fallback, pinned to our exact
                # registered child, and verify exit before resuming a controller.
                try:
                    signal.pidfd_send_signal(process_fd, signal.SIGKILL)
                except ProcessLookupError:
                    return forced
                forced = True
                until = time.monotonic() + 5
            time.sleep(0.1)
        return forced
    finally:
        os.close(process_fd)


def restore(record):
    # Parent and independent guardian serialize the whole operation, including
    # service restart. The guardian never races the live parent on a timer.
    with (REPORT / "restoration.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return restore_locked(record)


def restore_locked(record):
    if record.get("changes_authorized") is False:
        return {"restored": True, "unchanged": True, "errors": []}
    errors = []
    failed = {"restored": False, "errors": errors, "emergency_backup": record["backup"],
              "warning": "DESKTOP/SERVICE RESTORATION FAILED; backup retained, do not claim restored"}
    try:
        if stop_test_daemon(record):
            errors.append("Daemon needed forced shutdown before restoration")
        if record.get("installed_service"):
            state = stable_service(record)
            if managed_pid(state, allow_failed=True) is not None:
                if record.get("resume_requested"):
                    # Parent may have died during/after systemctl start. Verify
                    # its completed restoration without stopping a new PID.
                    process = managed_pid(state)
                    binary = record["installed_service"].get("binary")
                    if binary and str((PROC / str(process["pid"]) / "exe").resolve()) != binary["path"]:
                        raise RuntimeError("Resumed service executable differs from original")
                    competitors([process["pid"]])
                    verify_wallpaper(record)
                    return {"restored": True, "errors": [], "service_already_resumed": True}
                # Interruption may have occurred just BEFORE the parent's stop.
                stop_installed(record)
        competitors()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        errors.append(str(error))
        return failed
    desktop_env = os.environ.copy()
    desktop_env.update(record.get("desktop_env", {}))
    command_env = {"env": desktop_env} if "desktop_env" in record else {}
    wallpaper_restored = False
    for attempt in range(3):
        try:
            target = Path(record["original_target"])
            if digest(target) != record["original_hash"]:
                raise RuntimeError("Original source unavailable or its hash changed")
            run([record["omarchy"], "theme", "bg", "set", target], **command_env)
            # Quiet Omarchy setter success is insufficient: explicitly check IPC.
            run([record["shell"], "shell", "ping"], **command_env)
            time.sleep(1)
            verify_wallpaper(record)
            wallpaper_restored = True
            break
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            errors.append(str(error))
            time.sleep(1)
    service_restored = True
    if record.get("installed_service"):
        try:
            state = stable_service(record)
            if managed_pid(state, allow_failed=True) is not None:
                raise RuntimeError("Another controller appeared before service restoration")
            competitors()
            if record["installed_service"]["process"]:
                record["resume_requested"] = True
                write_json(REPORT / "restoration-record.json", record)
                run(["systemctl", "--user", "start", UNIT])
            state = verify_service(record)
            process = managed_pid(state)
            binary = record["installed_service"].get("binary")
            if process and binary and str((PROC / str(process["pid"]) / "exe").resolve()) != binary["path"]:
                raise RuntimeError("Restored service executable differs from original")
            if bool(process) != bool(record["installed_service"]["process"]):
                raise RuntimeError("Original service active state was not restored")
            competitors([process["pid"]] if process else [])
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            service_restored = False
            errors.append(str(error))
    if wallpaper_restored and service_restored:
        try:
            # Restart success alone must not erase wallpaper failure evidence.
            verify_wallpaper(record)
        except (OSError, RuntimeError) as error:
            wallpaper_restored = False
            errors.append(str(error))
    if wallpaper_restored and service_restored:
        return {"restored": True, "attempt": attempt + 1, "errors": errors}
    return {**failed, "wallpaper_restored": wallpaper_restored, "service_restored": service_restored}


def verify_wallpaper(record):
    if os.readlink(record["link"]) != record["original_target"]:
        raise RuntimeError("Exact original symlink target was not restored")
    if digest(record["original_target"]) != record["original_hash"]:
        raise RuntimeError("Original image hash mismatch after restore")
    if Path(record["theme_path"]).read_text() != record["theme_bytes"]:
        raise RuntimeError("Theme changed during test")


def launch_test_daemon(fd):
    # EOF without authorization exits harmlessly if the parent died before
    # registering this child. exec preserves the registered PID/start time.
    try:
        allowed = os.read(fd, 2) == b"go"
    finally:
        os.close(fd)
    if allowed:
        record = json.loads((REPORT / "restoration-record.json").read_text())
        if (not (REPORT / "restore-armed").exists() or not record.get("changes_authorized")
                or record.get("daemon_pid") != os.getpid()
                or record.get("daemon_start") != proc_start(os.getpid())
                or not record.get("test_env")
                or any(os.environ.get(key) != value for key, value in record["test_env"].items())):
            raise RuntimeError("Test launcher is not registered with an authorized restoration guard")
        os.execv(str(BINARY), [str(BINARY), "daemon"])


def guardian(record_path, fd, ready_fd=None):
    # EOF means the parent exited or finished. A separate session keeps this
    # cleanup guard alive if the invoking terminal process group is terminated.
    # Read and validate the durable record before acknowledging readiness. EOF
    # alone triggers restoration; a slow live test must never create two owners.
    json.loads(record_path.read_text())
    if ready_fd is not None:
        os.write(ready_fd, b"ready")
        os.close(ready_fd)
    while os.read(fd, 1):
        pass
    if (REPORT / "restore-armed").exists():
        try:
            result = restore(json.loads(record_path.read_text()))
        except Exception as error:
            result = {"restored": False, "errors": [repr(error)],
                      "warning": "Guardian restoration failed; restoration remains armed"}
        write_json(REPORT / "guardian-restoration.json", result)
        if result["restored"]:
            (REPORT / "restore-armed").unlink(missing_ok=True)


class Acceptance:
    def __init__(self, allow_installed_service=False):
        self.results: dict[str, Any] = {"tests": [], "events": []}
        self.daemon: subprocess.Popen[bytes] | None = None
        self.viewer: subprocess.Popen[bytes] | None = None
        self.guard: subprocess.Popen[bytes] | None = None
        self.guard_write: int | None = None
        self.env = os.environ.copy()
        self.started = time.monotonic()
        self.initial_workspace = None
        self.initial_window = None
        self.initial_cursor = None
        self.record: dict[str, Any] | None = None
        self.temp: tempfile.TemporaryDirectory[str] | None = None
        self.changed = False
        self.allow_installed_service = allow_installed_service
        self.report_lock = None
        self.owns_report = False

    def event(self, name, **details):
        event = {"name": name, "elapsed_seconds": time.monotonic() - self.started, **details}
        self.results["events"].append(event)
        print(json.dumps(event), flush=True)
        write_json(REPORT / "result.json", self.results)

    def passed(self, name, **details):
        self.results["tests"].append({"name": name, "passed": True, **details})
        self.event(name, **details)

    def status(self):
        response = json.loads(run([BINARY, "status"], env=self.env).stdout)
        assert response["ok"], response
        status = response["status"]
        assert not status["protective_suspension"], status
        assert not status["last_persistence_error"], status
        return status

    def command(self, command):
        response = json.loads(run([BINARY, command], env=self.env).stdout)
        assert response["ok"], response
        return response

    def wait_status(self, predicate, timeout=22):
        assert self.daemon is not None
        status = None
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if self.daemon.poll() is not None:
                raise RuntimeError(f"Daemon exited: {self.daemon.returncode}")
            try:
                status = self.status()
            except subprocess.CalledProcessError:
                time.sleep(0.05)
                continue
            if predicate(status):
                return status
            time.sleep(0.1)
        raise RuntimeError(f"Status timeout; last status: {status}")

    def settled(self):
        return self.wait_status(lambda s: not s["applying"] and not s["scanning"] and not s["persistence_pending"])

    def changed_to_new(self, old):
        return self.wait_status(lambda s: not s["applying"] and s["observed_wallpaper"] != old)

    def display_capture(self, name):
        active = json.loads(run(["hyprctl", "-j", "activeworkspace"]).stdout)
        window = json.loads(run(["hyprctl", "-j", "activewindow"]).stdout)
        existing = json.loads(run(["hyprctl", "-j", "workspaces"]).stdout)
        empty = max([100] + [w["id"] for w in existing if w["id"] > 0]) + 1
        try:
            run(["hyprctl", "dispatch", "workspace", str(empty)])
            time.sleep(0.6)
            output = REPORT / f"{name}.png"
            run(["grim", "-o", self.monitor["name"], output])
            return output
        finally:
            run(["hyprctl", "dispatch", "workspace", str(active["id"])])
            if window.get("address"):
                run(["hyprctl", "dispatch", "focuswindow", f"address:{window['address']}"])

    def visual_check(self, image, name):
        time.sleep(0.6)
        actual = self.display_capture(name)
        dimensions = run(["magick", "identify", "-format", "%wx%h", actual]).stdout.decode()
        width, height = map(int, dimensions.split("x"))
        crop = f"{width-40}x{height-100}+20+80"
        expected = REPORT / f"{name}-expected.png"
        # Omarchy's Image.PreserveAspectCrop: fill, then center crop.
        run(["magick", image, "-resize", f"{width}x{height}^", "-gravity", "center", "-extent", dimensions,
             "-gravity", "northwest", "-crop", crop, "+repage", "-resize", "64x36!", expected])
        sample = REPORT / f"{name}-actual.png"
        run(["magick", actual, "-crop", crop, "+repage", "-resize", "64x36!", sample])
        difference = subprocess.run(
            ["magick", "compare", "-metric", "RMSE", str(expected), str(sample), "null:"],
            capture_output=True, timeout=15, check=False,
        )
        if difference.returncode not in (0, 1):
            raise RuntimeError(difference.stderr.decode())
        normalized = float(difference.stderr.decode().split("(")[1].split(")")[0])
        self.event("visual comparison", image=str(image), capture=str(actual), normalized_rmse=normalized)
        assert normalized < 0.08, f"Displayed wallpaper mismatch: {normalized}"
        return normalized

    def claim_report(self):
        REPORT.mkdir(parents=True, exist_ok=True)
        self.report_lock = (REPORT / "session.lock").open("a")
        try:
            fcntl.flock(self.report_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.report_lock.close()
            self.report_lock = None
            raise RuntimeError("Another live acceptance/guardian owns this report") from error
        if (REPORT / "restore-armed").exists():
            self.report_lock.close()
            self.report_lock = None
            raise RuntimeError("A previous restoration guard is armed; resolve it first")
        self.owns_report = True
        (REPORT / "guardian-restoration.json").unlink(missing_ok=True)

    def arm_guardian(self):
        assert self.record is not None and self.report_lock is not None
        write_json(REPORT / "restoration-record.json", self.record)
        (REPORT / "restore-armed").touch(exist_ok=False)
        read_fd, self.guard_write = os.pipe()
        ready_read, ready_write = os.pipe()
        try:
            with (REPORT / "guardian.log").open("w") as guard_log:
                self.guard = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "--guardian", str(read_fd),
                     "--guardian-ready", str(ready_write)],
                    # Inherit the session lock too: a new test cannot overwrite
                    # reports while an orphaned guardian is still restoring.
                    pass_fds=(read_fd, ready_write, self.report_lock.fileno()),
                    start_new_session=True, stdout=guard_log, stderr=guard_log,
                )
            os.close(ready_write)
            ready_write = None
            readable, _, _ = select.select([ready_read], [], [], 10)
            if not readable or os.read(ready_read, 5) != b"ready" or self.guard.poll() is not None:
                raise RuntimeError("Independent restoration guardian did not become ready")
        finally:
            os.close(read_fd)
            if ready_write is not None:
                os.close(ready_write)
            os.close(ready_read)

    def preflight(self):
        self.claim_report()
        # Do not borrow the service's whole environment or guess a display.
        # An authorized caller may explicitly supply selected graphical vars.
        required = ("XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE")
        if any(not self.env.get(key) for key in required):
            raise RuntimeError("Live acceptance requires an explicit Wayland/Hyprland desktop environment")
        runtime = Path(self.env["XDG_RUNTIME_DIR"])
        display = Path(self.env["WAYLAND_DISPLAY"])
        if not runtime.is_absolute() or not runtime.is_dir() or not (runtime/display).is_socket():
            raise RuntimeError("Selected Wayland runtime/socket unavailable")
        assert BINARY.is_file()
        self.service_before, installed = snapshot_service(self.allow_installed_service)
        self.omarchy = shutil.which("omarchy")
        self.shell = shutil.which("omarchy-shell")
        self.swiftview = shutil.which("swiftview")
        assert self.omarchy and self.shell and self.swiftview
        assert run([self.shell, "shell", "ping"]).stdout.strip() == b"ok"
        self.monitor = next(m for m in json.loads(run(["hyprctl", "-j", "monitors"]).stdout) if m["focused"])
        self.initial_workspace = json.loads(run(["hyprctl", "-j", "activeworkspace"]).stdout)["id"]
        self.initial_window = json.loads(run(["hyprctl", "-j", "activewindow"]).stdout).get("address")
        self.initial_cursor = json.loads(run(["hyprctl", "-j", "cursorpos"]).stdout)
        target = os.readlink(LINK)
        assert os.path.isabs(target) and Path(target).is_file() and os.access(target, os.R_OK)
        assert run(["realpath", target]).stdout.decode().strip() == target, "Setter cannot preserve exact original target"
        original_hash = digest(target)
        backup = REPORT / "original-backup.png"
        shutil.copyfile(target, backup)
        assert digest(backup) == original_hash
        run(["magick", "identify", "-regard-warnings", backup])
        mount = run(["findmnt", "-T", COLLECTION, "-n", "-o", "FSTYPE,SOURCE"]).stdout.decode().strip()
        assert mount.split()[0] in {"btrfs", "ext4", "xfs", "f2fs", "zfs"}, mount
        self.images = sorted(p.resolve() for p in COLLECTION.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} and p.is_file())
        assert len(self.images) >= 4
        self.collection_before = {str(p): {"sha256": digest(p), "size": p.stat().st_size, "mtime_ns": p.stat().st_mtime_ns} for p in self.images}
        self.record = {
            "theme_bytes": THEME.read_text(), "theme_path": str(THEME), "link": str(LINK),
            "original_target": target, "original_hash": original_hash, "backup": str(backup),
            "omarchy": self.omarchy, "shell": self.shell,
            "installed_service": installed,
            "changes_authorized": False,
            "desktop_env": {key: self.env[key] for key in DESKTOP_VARIABLES if key in self.env},
        }
        write_json(REPORT / "restoration-record.json", self.record)
        self.results["preflight"] = {**self.record, "local_collection": str(COLLECTION), "mount": mount, "collection_before": self.collection_before, "service_before": self.service_before}
        run(["cc", "-Wall", "-Wextra", "-Werror", ROOT/"scripts/live_pointer.c", "-o", REPORT/"live-pointer", "-lwayland-client"])
        self.visual_check(backup, "original-before")
        self.temp = tempfile.TemporaryDirectory(prefix="sw-live-", dir=self.env["XDG_RUNTIME_DIR"])
        temporary = Path(self.temp.name)
        (temporary/"config.toml").write_text(f"interval_seconds = 15\nselection = 'shuffle'\nfolders = [{json.dumps(str(COLLECTION))}]\n")
        for key in list(self.env):
            if key.startswith("SWIFTWALL_"):
                del self.env[key]
        self.env.update({
            "SWIFTWALL_CONFIG": str(temporary/"config.toml"),
            "SWIFTWALL_STATE_DIR": str(temporary/"state"),
            "SWIFTWALL_RUNTIME_DIR": str(temporary/"runtime"),
            "SWIFTWALL_BACKGROUND_LINK": str(LINK),
            "SWIFTWALL_OMARCHY_COMMAND": self.omarchy,
        })
        self.record["test_env"] = {key: value for key, value in self.env.items() if key.startswith("SWIFTWALL_")}
        self.arm_guardian()
        authorized = {**self.record, "changes_authorized": True}
        write_json(REPORT / "restoration-record.json", authorized)
        self.record = authorized
        if installed:
            stop_installed(self.record)
        self.passed("preflight", collection_count=len(self.images), original_hash=original_hash)

    def start_daemon(self):
        assert self.record is not None
        if not self.guard or self.guard.poll() is not None or not (REPORT / "restore-armed").exists():
            raise RuntimeError("Restoration guardian is not armed/alive")
        if self.record.get("installed_service"):
            if managed_pid(verify_service(self.record)) is not None:
                raise RuntimeError("Installed service is still controlling wallpaper")
        competitors()
        gate_read, gate_write = os.pipe()
        try:
            with (REPORT / "daemon.log").open("w") as log:
                # Child cannot exec the daemon until its exact PID/start time
                # has been durably registered for the independent guardian.
                self.daemon = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "--test-daemon", str(gate_read)],
                    pass_fds=(gate_read,), env=self.env, stdout=log, stderr=log, start_new_session=True,
                )
            start = proc_start(self.daemon.pid)
            if not start:
                raise RuntimeError("Test daemon launcher disappeared")
            self.record.update(daemon_pid=self.daemon.pid, daemon_start=start)
            write_json(REPORT / "restoration-record.json", self.record)
            os.write(gate_write, b"go")
        finally:
            os.close(gate_read)
            os.close(gate_write)

    def swiftview_candidates(self):
        candidates = []
        for path in self.images:
            dimensions = run([
                "magick", "identify", "-format", "%w %h", path,
            ]).stdout.decode().split()
            if len(dimensions) != 2:
                raise RuntimeError(f"Cannot inspect SwiftView candidate dimensions: {path}")
            width, height = map(int, dimensions)
            if width > 0 and height > 0 and width * height * 4 <= SWIFTVIEW_MAX_RGBA_BYTES:
                candidates.append(path)
        if len(candidates) < 3:
            raise RuntimeError("Live collection has fewer than three SwiftView-decodable images")
        return candidates

    def viewer_set(self, path):
        assert self.viewer is None
        assert self.swiftview is not None
        log = (REPORT/"swiftview.log").open("a")
        self.viewer = subprocess.Popen([self.swiftview, str(path)], stdout=log, stderr=log)
        log.close()
        try:
            until = time.monotonic()+8
            while time.monotonic() < until:
                clients = json.loads(run(["hyprctl", "-j", "clients"]).stdout)
                client = next((c for c in clients if c["pid"] == self.viewer.pid), None)
                if client and client.get("mapped"):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("SwiftView window did not appear")
            run(["hyprctl", "dispatch", "focuswindow", f"address:{client['address']}"])
            expected_title = f"SwiftView — {path}"
            until = time.monotonic()+8
            while time.monotonic() < until:
                clients = json.loads(run(["hyprctl", "-j", "clients"]).stdout)
                client = next((c for c in clients if c["pid"] == self.viewer.pid), None)
                if not client:
                    raise RuntimeError("SwiftView window disappeared while loading")
                if client.get("title") == expected_title:
                    break
                if " — loading" not in client.get("title", ""):
                    raise RuntimeError(f"SwiftView could not load integration target: {client.get('title')}")
                time.sleep(0.1)
            else:
                raise RuntimeError("SwiftView integration target did not finish loading")
            assert json.loads(run(["hyprctl", "-j", "activewindow"]).stdout)["pid"] == self.viewer.pid
            width = round(self.monitor["width"]/self.monitor["scale"])
            height = round(self.monitor["height"]/self.monitor["scale"])
            x, y = width//3, height//3
            run([REPORT/"live-pointer", x, y, width, height, 273])
            time.sleep(0.2)
            run(["grim", "-o", self.monitor["name"], REPORT/"swiftview-context-menu.png"])
            assert json.loads(run(["hyprctl", "-j", "activewindow"]).stdout)["pid"] == self.viewer.pid
            run([REPORT/"live-pointer", x+60, y+20, width, height, 272])
            until = time.monotonic()+6
            while time.monotonic() < until:
                if os.readlink(LINK) == str(path):
                    return time.monotonic()
                time.sleep(0.05)
            raise RuntimeError("SwiftView context-menu wallpaper action did not update the link")
        finally:
            if self.viewer.poll() is None:
                self.viewer.terminate()
            self.viewer.wait(timeout=10)
            self.viewer = None
            if self.initial_window:
                run(["hyprctl", "dispatch", "focuswindow", f"address:{self.initial_window}"])

    def measure(self):
        assert self.daemon is not None
        stat = Path(f"/proc/{self.daemon.pid}/stat").read_text().rsplit(")", 1)[1].split()
        status = {}
        for line in Path(f"/proc/{self.daemon.pid}/status").read_text().splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                status[key] = value.strip()
        return {"ticks": int(stat[11])+int(stat[12]), "rss_kib": int(status["VmRSS"].split()[0]), "voluntary": int(status["voluntary_ctxt_switches"]), "involuntary": int(status["nonvoluntary_ctxt_switches"]), "time": time.monotonic()}

    def tests(self):
        assert self.record is not None
        assert self.temp is not None
        self.start_daemon()
        startup = self.wait_status(lambda s: not s["scanning"] and not s["persistence_pending"])
        assert startup["eligible_images"] == len(self.images), startup
        assert startup["interval_seconds"] == 15 and startup["selection"] == "shuffle"
        assert startup["observed_wallpaper"] == self.record["original_target"]
        time.sleep(2)
        assert os.readlink(LINK) == self.record["original_target"]
        self.passed("startup does not change wallpaper", status=startup)
        self.changed = True
        previous = self.record["original_target"]
        chosen = []
        for index in range(3):
            before = self.status()
            expected = time.monotonic()+before["next_change_in_seconds"]
            after = self.changed_to_new(previous)
            actual = time.monotonic()
            assert after["last_application_result"]["success"], after
            assert after["observed_wallpaper"] in map(str, self.images)
            assert after["observed_wallpaper"] not in chosen
            chosen.append(after["observed_wallpaper"])
            self.visual_check(after["observed_wallpaper"], f"automatic-{index+1}")
            self.passed(f"automatic rotation {index+1}", selected=chosen[-1], observed_drift_seconds=actual-expected)
            previous = chosen[-1]
        old = previous
        began = time.monotonic()
        self.command("next")
        next_status = self.changed_to_new(old)
        next_observation_elapsed = time.monotonic()-began
        assert next_observation_elapsed < 5
        next_path = next_status["observed_wallpaper"]
        self.visual_check(next_path, "explicit-next")
        self.passed("explicit next", elapsed_seconds_to_observation=next_observation_elapsed)
        self.command("previous")
        self.wait_status(lambda s: not s["applying"] and s["observed_wallpaper"] == old)
        self.visual_check(old, "explicit-previous")
        self.passed("previous restores previous selection")
        self.command("pause")
        paused_wallpaper = os.readlink(LINK)
        self.settled()
        cpu_before = self.measure()
        time.sleep(17)
        cpu_after = self.measure()
        assert os.readlink(LINK) == paused_wallpaper
        paused = self.status()
        assert paused["paused"] and paused["next_change_in_seconds"] is None
        self.results["resource_observations"] = {"before": cpu_before, "after": cpu_after, "idle_cpu_seconds": (cpu_after["ticks"]-cpu_before["ticks"])/os.sysconf("SC_CLK_TCK"), "sample_seconds": cpu_after["time"]-cpu_before["time"]}
        self.passed("pause stops automatic rotation for longer than interval")
        self.command("resume")
        resumed = self.status()
        assert not resumed["paused"] and 13 < resumed["next_change_in_seconds"] <= 15
        self.changed_to_new(paused_wallpaper)
        self.passed("resume restarts full countdown and rotation")
        time.sleep(3)
        current = self.status()["observed_wallpaper"]
        swiftview_images = self.swiftview_candidates()
        manual = next(p for p in swiftview_images if str(p) != current)
        manual_time = self.viewer_set(manual)
        reset = self.wait_status(lambda s: s["observed_wallpaper"] == str(manual) and not s["applying"])
        assert not reset["paused"] and reset["next_change_in_seconds"] > 12
        self.visual_check(manual, "swiftview-manual-running")
        # Check after the OLD deadline, but before the reset deadline.
        while time.monotonic() < manual_time+12:
            time.sleep(0.1)
        assert os.readlink(LINK) == str(manual)
        after_manual = self.changed_to_new(str(manual))
        self.passed("SwiftView manual change resets timer without pausing", reset_status=reset, elapsed_until_next_rotation=time.monotonic()-manual_time, selected=after_manual["observed_wallpaper"])
        self.command("pause")
        current = self.settled()["observed_wallpaper"]
        manual_paused = next(p for p in swiftview_images if str(p) != current and p != manual)
        self.viewer_set(manual_paused)
        still_paused = self.wait_status(lambda s: s["observed_wallpaper"] == str(manual_paused))
        assert still_paused["paused"] and still_paused["next_change_in_seconds"] is None
        self.visual_check(manual_paused, "swiftview-manual-paused")
        time.sleep(16)
        assert os.readlink(LINK) == str(manual_paused)
        self.passed("SwiftView manual change preserves pause", status=self.status())
        self.command("resume")
        accurate = self.settled()
        assert accurate["state"] == "running" and accurate["eligible_images"] == len(self.images)
        assert accurate["visually_verified"] == "unknown"
        assert accurate["observed_wallpaper"] == os.readlink(LINK)
        assert 12 < accurate["next_change_in_seconds"] <= 15
        self.passed("status reports accurate collection state and deadline", status=accurate)
        before_shutdown = os.readlink(LINK)
        self.daemon.terminate()
        self.daemon.wait(timeout=15)
        assert self.daemon.returncode == 0
        assert os.readlink(LINK) == before_shutdown
        state = json.loads((Path(self.temp.name)/"state/state.json").read_text())
        assert state["observed"] == before_shutdown
        shutil.copyfile(Path(self.temp.name)/"state/state.json", REPORT/"final-test-state.json")
        self.passed("graceful shutdown preserves state and wallpaper")
        assert not (REPORT/"daemon.log").read_text().strip(), (REPORT/"daemon.log").read_text()
        assert not (REPORT/"swiftview.log").read_text().strip(), (REPORT/"swiftview.log").read_text()
        self.passed("no daemon or SwiftView subprocess diagnostics")

    def cleanup(self):
        if not self.owns_report:
            return
        errors = self.results.setdefault("cleanup_errors", [])

        def attempt(operation):
            try:
                return operation()
            except (AssertionError, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                errors.append(repr(error))
                return None

        def stop_child(child, timeout):
            if child and child.poll() is None:
                child.terminate()
                child.wait(timeout=timeout)

        try:
            try:
                attempt(lambda: stop_child(self.viewer, 10))
                attempt(lambda: stop_child(self.daemon, 15))
                if self.record and (REPORT/"restore-armed").exists():
                    result = attempt(lambda: restore(self.record))
                    if result is not None:
                        self.results["restoration"] = result
                        if result["restored"]:
                            if not result.get("unchanged"):
                                visual = attempt(lambda: self.visual_check(self.record["backup"], "original-restored"))
                                result["visual_rmse"] = visual
                            (REPORT/"restore-armed").unlink(missing_ok=True)
                        self.event("restoration", **result)
            finally:
                # Always hand off, even if reporting, child shutdown or a second
                # interruption fails. The independent guard retains session.lock.
                if self.guard_write is not None:
                    os.close(self.guard_write)
                    self.guard_write = None
                if self.guard:
                    attempt(lambda: self.guard.wait(timeout=80))
            if (REPORT/"restore-armed").exists():
                errors.append("Restoration remains armed; inspect guardian-restoration.json and retained backup")
            if (REPORT/"guardian-restoration.json").exists():
                self.results["guardian_restoration"] = json.loads((REPORT/"guardian-restoration.json").read_text())
            if self.record:
                attempt(self.verify_final_state)
            if self.initial_workspace:
                attempt(lambda: run(["hyprctl", "dispatch", "workspace", str(self.initial_workspace)]))
                if self.initial_window:
                    attempt(lambda: run(["hyprctl", "dispatch", "focuswindow", f"address:{self.initial_window}"]))
                if self.initial_cursor and (REPORT/"live-pointer").is_file():
                    attempt(lambda: run([REPORT/"live-pointer", self.initial_cursor["x"], self.initial_cursor["y"],
                        round(self.monitor["width"]/self.monitor["scale"]),
                        round(self.monitor["height"]/self.monitor["scale"]), 0]))
            if self.temp:
                attempt(self.temp.cleanup)
            if errors:
                self.results["all_tests_passed"] = False
            write_json(REPORT/"result.json", self.results)
            if errors:
                raise RuntimeError(f"Live acceptance cleanup failed: {errors}")
        finally:
            if self.report_lock:
                self.report_lock.close()
                self.report_lock = None

    def verify_final_state(self):
        assert self.record is not None
        final_hashes = {str(p): {"sha256": digest(p), "size": p.stat().st_size, "mtime_ns": p.stat().st_mtime_ns} for p in self.images}
        self.results["collection_integrity_unchanged"] = final_hashes == self.collection_before
        self.results["original_hash_after"] = digest(self.record["original_target"])
        self.results["original_exact_target_after"] = os.readlink(LINK)
        self.results["theme_after"] = THEME.read_text()
        after = service_state()
        self.results["service_after"] = after
        # MainPID legitimately changes after restart. Enablement and definition
        # must not change; check active/substate against the captured evidence.
        for key in ("Id", "LoadState", "ActiveState", "SubState", "UnitFileState", "FragmentPath", "DropInPaths"):
            if after[key] != self.service_before[key]:
                raise RuntimeError(f"Service {key} not restored")
        if self.record.get("installed_service"):
            verify_service(self.record)
        assert self.results["collection_integrity_unchanged"], "Collection changed during test"


def execute(acceptance):
    failure = None
    try:
        acceptance.preflight()
        acceptance.tests()
        acceptance.results["all_tests_passed"] = True
    except BaseException as error:
        failure = error
        acceptance.results["all_tests_passed"] = False
        # An existing armed report belongs to its original run. Never overwrite
        # its record/result on refusal, including through the failure reporter.
        if acceptance.owns_report:
            try:
                acceptance.event("TEST FAILURE", error=repr(error))
            except Exception as report_error:
                acceptance.results["report_error"] = repr(report_error)
    try:
        acceptance.cleanup()
    except BaseException as cleanup_error:
        acceptance.results["all_tests_passed"] = False
        if failure is None:
            raise
        failure.add_note(f"Cleanup also failed: {cleanup_error!r}")
    if failure is not None:
        raise failure.with_traceback(failure.__traceback__)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-live-desktop", action="store_true")
    parser.add_argument("--allow-installed-service", action="store_true",
                        help="Authorize temporary stop/start of the exact installed swiftwall.service")
    parser.add_argument("--guardian", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--guardian-ready", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--test-daemon", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.guardian is not None:
        guardian(REPORT/"restoration-record.json", args.guardian, args.guardian_ready)
        return
    if args.test_daemon is not None:
        launch_test_daemon(args.test_daemon)
        return
    if not args.allow_live_desktop:
        parser.error("Requires explicit --allow-live-desktop after user authorization")
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt("Live acceptance interrupted; restoring wallpaper")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    execute(Acceptance(allow_installed_service=args.allow_installed_service))


if __name__ == "__main__":
    main()
