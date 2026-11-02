"""Explicitly authorized LIVE acceptance test. Changes wallpaper, then restores it.

Run only with --allow-live-desktop. No installation or service operations.
The independent restoration guardian survives parent interruption. Artifacts include
private desktop captures and a local emergency backup: never publish artifacts/.
"""

import argparse
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


def proc_start(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def restore(record):
    errors = []
    pid = record.get("daemon_pid")
    if pid and proc_start(pid) == record.get("daemon_start"):
        os.kill(pid, signal.SIGTERM)
        until = time.monotonic() + 15
        while proc_start(pid) == record.get("daemon_start") and time.monotonic() < until:
            # A zombie is no longer controlling wallpapers.
            if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                break
            time.sleep(0.1)
        else:
            if proc_start(pid) == record.get("daemon_start"):
                os.kill(pid, signal.SIGKILL)
                errors.append("Daemon needed forced shutdown before restoration")
    for attempt in range(3):
        try:
            target = Path(record["original_target"])
            if digest(target) != record["original_hash"]:
                raise RuntimeError("Original source unavailable or its hash changed")
            run([record["omarchy"], "theme", "bg", "set", target])
            # Quiet Omarchy setter success is insufficient: explicitly check IPC.
            run([record["shell"], "shell", "ping"])
            time.sleep(1)
            if os.readlink(record["link"]) != record["original_target"]:
                raise RuntimeError("Exact original symlink target was not restored")
            if digest(target) != record["original_hash"]:
                raise RuntimeError("Original image hash mismatch after restore")
            if Path(record["theme_path"]).read_text() != record["theme_bytes"]:
                raise RuntimeError("Theme changed during test")
            return {"restored": True, "attempt": attempt + 1, "errors": errors}
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            errors.append(str(error))
            time.sleep(1)
    return {
        "restored": False,
        "errors": errors,
        "emergency_backup": record["backup"],
        "warning": "DESKTOP RESTORATION FAILED; backup retained, do not claim restored",
    }


def guardian(record_path, fd):
    # EOF means the parent exited or finished. A separate session keeps this
    # cleanup guard alive if the invoking terminal process group is terminated.
    select.select([fd], [], [], 330)
    if (REPORT / "restore-armed").exists():
        result = restore(json.loads(record_path.read_text()))
        write_json(REPORT / "guardian-restoration.json", result)
        if result["restored"]:
            (REPORT / "restore-armed").unlink(missing_ok=True)


class Acceptance:
    def __init__(self):
        self.results: dict[str, Any] = {"tests": [], "events": []}
        self.daemon: subprocess.Popen[bytes] | None = None
        self.viewer: subprocess.Popen[bytes] | None = None
        self.guard: subprocess.Popen[bytes] | None = None
        self.guard_write: int | None = None
        self.env = os.environ.copy()
        self.started = time.monotonic()
        self.initial_workspace = None
        self.record: dict[str, Any] | None = None
        self.temp: tempfile.TemporaryDirectory[str] | None = None
        self.changed = False

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

    def preflight(self):
        REPORT.mkdir(parents=True, exist_ok=True)
        if (REPORT / "restore-armed").exists():
            raise RuntimeError("A previous restoration guard is armed; resolve it first")
        assert BINARY.is_file()
        competing = subprocess.run(["pgrep", "-a", "-x", "swiftwall"], capture_output=True, check=False)
        assert competing.returncode == 1, competing.stdout.decode()
        self.service_before = run(["systemctl", "--user", "--no-pager", "show", "swiftwall.service", "-p", "LoadState", "-p", "ActiveState", "-p", "UnitFileState"]).stdout.decode()
        assert "LoadState=not-found" in self.service_before and "ActiveState=inactive" in self.service_before
        assert not (Path.home()/".config/systemd/user/swiftwall.service").exists()
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
        }
        write_json(REPORT / "restoration-record.json", self.record)
        self.results["preflight"] = {**self.record, "local_collection": str(COLLECTION), "mount": mount, "collection_before": self.collection_before, "service_before": self.service_before}
        run(["cc", "-Wall", "-Wextra", "-Werror", ROOT/"scripts/live_pointer.c", "-o", REPORT/"live-pointer", "-lwayland-client"])
        self.visual_check(backup, "original-before")
        self.temp = tempfile.TemporaryDirectory(prefix="sw-live-", dir=os.environ["XDG_RUNTIME_DIR"])
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
        (REPORT/"restore-armed").touch()
        read_fd, self.guard_write = os.pipe()
        guard_log = (REPORT/"guardian.log").open("w")
        self.guard = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--guardian", str(read_fd)],
            pass_fds=(read_fd,), start_new_session=True, stdout=guard_log, stderr=guard_log,
        )
        guard_log.close()
        os.close(read_fd)
        self.passed("preflight", collection_count=len(self.images), original_hash=original_hash)

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
            time.sleep(1.2)
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
        log = (REPORT/"daemon.log").open("w")
        self.daemon = subprocess.Popen([BINARY, "daemon"], env=self.env, stdout=log, stderr=log, start_new_session=True)
        log.close()
        self.record.update(daemon_pid=self.daemon.pid, daemon_start=proc_start(self.daemon.pid))
        write_json(REPORT/"restoration-record.json", self.record)
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
        manual = next(p for p in self.images if str(p) != current)
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
        manual_paused = next(p for p in self.images if str(p) != current and p != manual)
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
        if self.viewer and self.viewer.poll() is None:
            self.viewer.terminate()
            self.viewer.wait(timeout=10)
        if self.daemon and self.daemon.poll() is None:
            self.daemon.terminate()
            self.daemon.wait(timeout=15)
        if self.record and (REPORT/"restore-armed").exists():
            result = restore(self.record)
            self.results["restoration"] = result
            if result["restored"]:
                try:
                    self.results["restoration"]["visual_rmse"] = self.visual_check(self.record["backup"], "original-restored")
                except (AssertionError, OSError, RuntimeError, subprocess.SubprocessError) as error:
                    self.results["restoration"]["visual_error"] = str(error)
                (REPORT/"restore-armed").unlink(missing_ok=True)
            self.event("restoration", **result)
        if self.guard_write is not None:
            os.close(self.guard_write)
            self.guard_write = None
        if self.guard:
            self.guard.wait(timeout=80)
        if self.record:
            final_hashes = {str(p): {"sha256": digest(p), "size": p.stat().st_size, "mtime_ns": p.stat().st_mtime_ns} for p in self.images}
            self.results["collection_integrity_unchanged"] = final_hashes == self.collection_before
            self.results["original_hash_after"] = digest(self.record["original_target"])
            self.results["original_exact_target_after"] = os.readlink(LINK)
            self.results["theme_after"] = THEME.read_text()
            self.results["service_after"] = run(["systemctl", "--user", "--no-pager", "show", "swiftwall.service", "-p", "LoadState", "-p", "ActiveState", "-p", "UnitFileState"]).stdout.decode()
            assert self.results["service_after"] == self.service_before
        if self.initial_workspace:
            run(["hyprctl", "dispatch", "workspace", str(self.initial_workspace)])
            if self.initial_window:
                run(["hyprctl", "dispatch", "focuswindow", f"address:{self.initial_window}"])
            run([REPORT/"live-pointer", self.initial_cursor["x"], self.initial_cursor["y"],
                 round(self.monitor["width"]/self.monitor["scale"]),
                 round(self.monitor["height"]/self.monitor["scale"]), 0])
        if self.temp:
            self.temp.cleanup()
        write_json(REPORT/"result.json", self.results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-live-desktop", action="store_true")
    parser.add_argument("--guardian", type=int)
    args = parser.parse_args()
    if args.guardian is not None:
        guardian(REPORT/"restoration-record.json", args.guardian)
        return
    if not args.allow_live_desktop:
        parser.error("Requires explicit --allow-live-desktop after user authorization")
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt("Live acceptance interrupted; restoring wallpaper")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    acceptance = Acceptance()
    try:
        acceptance.preflight()
        acceptance.tests()
        acceptance.results["all_tests_passed"] = True
    except BaseException as error:
        acceptance.results["all_tests_passed"] = False
        acceptance.event("TEST FAILURE", error=repr(error))
        raise
    finally:
        acceptance.cleanup()


if __name__ == "__main__":
    main()
