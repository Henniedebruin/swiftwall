
"""Isolated Linux benchmark for an already-built SwiftWall binary (stdlib only)."""

import argparse
import hashlib
import json
import os
import socket
import statistics
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path


def seconds_argument(value):
    seconds = float(value)
    if not 1 <= seconds <= 120:
        raise argparse.ArgumentTypeError("seconds must be between 1 and 120")
    return seconds


def images_argument(value):
    count = int(value)
    if count < 1:
        raise argparse.ArgumentTypeError("images must be positive")
    return count


def tiny_png():
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x40\x80\xc0"))
            + chunk(b"IEND", b""))


def image_hashes(directory):
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.iterdir())}


def prepare(root, count):
    names = ("home", "config", "state", "cache", "data", "runtime", "tmp", "images")
    paths = {name: root / name for name in names}
    for path in paths.values():
        path.mkdir(mode=0o700)
    images = paths["images"]
    png = tiny_png()
    for index in range(count):
        (images / f"image-{index:08d}.png").write_bytes(png)
    background = root / "background"
    background.symlink_to(images / "image-00000000.png")
    config = paths["config"] / "config.toml"
    config.write_text(
        'interval_seconds = 300\nselection = "shuffle"\n'
        + f"folders = [{json.dumps(str(images))}]\n", encoding="utf-8")
    fake = root / "omarchy"
    # Absolute embedded paths prevent inherited environment from redirecting writes.
    fake.write_text(
        f"#!{Path(sys.executable).resolve()}\n"
        "import os, sys\nfrom pathlib import Path\n"
        f"images = Path({str(images)!r})\n"
        f"link = Path({str(background)!r})\n"
        "if len(sys.argv) != 5 or sys.argv[1:4] != ['theme', 'bg', 'set']:\n"
        "    sys.exit('unexpected fake Omarchy arguments')\n"
        "target = Path(sys.argv[4]).resolve(strict=True)\n"
        "if target.parent != images or not target.is_file() or not link.is_symlink():\n"
        "    sys.exit('refusing a path outside the benchmark fixture')\n"
        "link.unlink()\nos.symlink(target, link)\n", encoding="utf-8")
    fake.chmod(0o700)
    # Do not inherit live desktop/session, XDG, SWIFTWALL, or loader settings.
    env = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "HOME": str(paths["home"]),
        "TMPDIR": str(paths["tmp"]),
        "XDG_CONFIG_HOME": str(paths["config"]),
        "XDG_STATE_HOME": str(paths["state"]),
        "XDG_CACHE_HOME": str(paths["cache"]),
        "XDG_DATA_HOME": str(paths["data"]),
        "XDG_RUNTIME_DIR": str(paths["runtime"]),
        "XDG_CONFIG_DIRS": str(paths["config"]),
        "XDG_DATA_DIRS": str(paths["data"]),
        "SWIFTWALL_CONFIG": str(config),
        "SWIFTWALL_STATE_DIR": str(paths["state"]),
        "SWIFTWALL_RUNTIME_DIR": str(paths["runtime"]),
        "SWIFTWALL_BACKGROUND_LINK": str(background),
        "SWIFTWALL_OMARCHY_COMMAND": str(fake),
    }
    return env, paths


def status_request(path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(0.25)
        client.connect(str(path))
        client.sendall(b"status\n")
        response = bytearray()
        while True:
            part = client.recv(65536)
            if not part:
                break
            response.extend(part)
            if len(response) > 1024 * 1024:
                raise RuntimeError("IPC response exceeds 1 MiB")
    result = json.loads(response)
    if result.get("ok") is not True:
        raise RuntimeError(f"IPC status failed: {result}")
    return result["status"]


def wait_ready(process, paths, started, expected):
    deadline = started + 30
    first_status_ms = None
    while time.perf_counter() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"daemon exited during startup: {process.returncode}")
        try:
            status = status_request(paths["runtime"] / "control.sock")
        except (OSError, ValueError):
            time.sleep(0.005)
            continue
        if first_status_ms is None:
            first_status_ms = (time.perf_counter() - started) * 1000
        if status.get("error") or status.get("last_persistence_error"):
            raise RuntimeError(f"daemon reported an error: {status}")
        if (status.get("state") == "running"
                and status.get("eligible_images") == len(expected)
                and status.get("scanning") is False
                and status.get("persistence_pending") is False
                and status.get("no_schedule_reason") is None):
            try:
                saved = json.loads((paths["state"] / "state.json").read_text())
            except (FileNotFoundError, ValueError):
                saved = {}
            if set(saved.get("carousel", {}).get("paths", [])) == expected:
                return first_status_ms, (time.perf_counter() - started) * 1000
        time.sleep(0.005)
    raise RuntimeError("startup did not reach IPC + persisted collection readiness in 30s")


def proc_sample(pid):
    proc = Path("/proc") / str(pid)
    # comm (field 2) can contain spaces and parentheses; fields after its final ')'
    # begin at field 3. utime/stime are fields 14/15, across all process threads.
    fields = proc.joinpath("stat").read_text().rsplit(")", 1)[1].split()
    status = {}
    for line in proc.joinpath("status").read_text().splitlines():
        key, _, value = line.partition(":")
        status[key] = value.strip()
    return {
        "user_ticks": int(fields[11]),
        "system_ticks": int(fields[12]),
        "rss_kib": int(status["VmRSS"].split()[0]),
        "voluntary": int(status["voluntary_ctxt_switches"])
        if "voluntary_ctxt_switches" in status else None,
        "involuntary": int(status["nonvoluntary_ctxt_switches"])
        if "nonvoluntary_ctxt_switches" in status else None,
    }


def stop(process):
    if process.poll() is None:
        process.terminate()  # SIGTERM, including when measurement raises.
    killed = False
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        killed = True
        process.kill()
        process.wait(timeout=15)
    return {"returncode": process.returncode, "killed_after_timeout": killed}


def benchmark(binary, seconds, count, root, subscribe=False):
    env, paths = prepare(root, count)
    originals = image_hashes(paths["images"])
    expected = {str(paths["images"] / name) for name in originals}
    version_times = []
    result = None
    for _ in range(20):
        started = time.perf_counter()
        result = subprocess.run([str(binary), "--version"], env=env, cwd=root,
                                capture_output=True, check=True, timeout=5)
        version_times.append((time.perf_counter() - started) * 1000)
    if result is None:
        raise RuntimeError("no CLI version samples collected")
    version = result.stdout.decode("utf-8").strip()
    with (root / "daemon.log").open("w+b") as log:
        started = time.perf_counter()
        process = subprocess.Popen([str(binary), "daemon"], env=env, cwd=root,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        subscriber = None
        try:
            ipc_ms, ready_ms = wait_ready(process, paths, started, expected)
            if subscribe:
                subscriber = socket.socket(socket.AF_UNIX)
                subscriber.settimeout(3)
                subscriber.connect(str(paths["runtime"] / "control.sock"))
                subscriber.sendall(b'{"v":1,"op":"subscribe"}\n')
                initial = bytearray()
                while b"\n" not in initial:
                    chunk = subscriber.recv(65536)
                    if not chunk:
                        raise RuntimeError("Subscriber disconnected before its initial snapshot")
                    initial.extend(chunk)
                    if len(initial) > 1024 * 1024:
                        raise RuntimeError("Subscriber snapshot exceeds 1 MiB")
                snapshot = json.loads(initial.split(b"\n", 1)[0])
                if snapshot.get("event") != "snapshot":
                    raise RuntimeError("Daemon does not support v0.2 subscriptions")
            time.sleep(1)
            if process.poll() is not None:
                raise RuntimeError("daemon exited before idle sampling")
            ticks_per_second = os.sysconf("SC_CLK_TCK")
            before = proc_sample(process.pid)
            idle_started = time.perf_counter()
            # Deliberately no IPC, CLI, image writes, or automatic rotation here.
            time.sleep(seconds)
            after = proc_sample(process.pid)
            elapsed = time.perf_counter() - idle_started
            if process.poll() is not None:
                raise RuntimeError("daemon exited during idle sampling")
        finally:
            if subscriber is not None:
                subscriber.close()
            shutdown = stop(process)
            if image_hashes(paths["images"]) != originals:
                raise AssertionError("original generated image files were changed")
            if sys.exc_info()[0] is not None:
                log.seek(0)
                print(log.read().decode("utf-8", errors="replace"), file=sys.stderr)
    if shutdown["returncode"] != 0 or shutdown["killed_after_timeout"]:
        raise RuntimeError(f"daemon did not shut down cleanly: {shutdown}")
    user_ticks = after["user_ticks"] - before["user_ticks"]
    system_ticks = after["system_ticks"] - before["system_ticks"]
    cpu_seconds = (user_ticks + system_ticks) / ticks_per_second

    def switch_delta(key):
        return (after[key] - before[key]
                if before[key] is not None and after[key] is not None else None)

    info = binary.stat()
    return {
        "binary": str(binary),
        "version": version,
        "binary_size_bytes": info.st_size,
        "binary_allocated_bytes": info.st_blocks * 512,
        "images": count,
        "idle_subscriber": subscribe,
        "startup": {"samples": 1, "ipc_status_ms": ipc_ms,
                    "ipc_and_persisted_collection_ms": ready_ms},
        "cli_version": {"samples": 20, "median_ms": statistics.median(version_times)},
        "idle": {
            "settle_seconds": 1,
            "requested_seconds": seconds,
            "elapsed_seconds": elapsed,
            "rss_kib_before": before["rss_kib"],
            "rss_kib_after": after["rss_kib"],
            "clock_ticks_per_second": ticks_per_second,
            "user_ticks_delta": user_ticks,
            "system_ticks_delta": system_ticks,
            "cpu_seconds_delta": cpu_seconds,
            "cpu_percent_one_core": 100 * cpu_seconds / elapsed,
            "voluntary_context_switches_delta": switch_delta("voluntary"),
            "involuntary_context_switches_delta": switch_delta("involuntary"),
        },
        "original_image_hashes_unchanged": True,
        "shutdown": shutdown,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=Path("target/release/swiftwall"))
    parser.add_argument("--seconds", type=seconds_argument, default=10.0)
    parser.add_argument("--images", type=images_argument, default=100)
    parser.add_argument("--subscribe", action="store_true", help="Hold an idle v0.2 event subscriber during sampling")
    args = parser.parse_args()
    binary = args.binary.resolve()
    if not sys.platform.startswith("linux"):
        parser.error("this benchmark requires Linux /proc and Unix sockets")
    if not binary.is_file() or not os.access(binary, os.X_OK):
        parser.error(f"release binary missing or not executable: {binary}; build it first")
    try:
        # A short fixed parent avoids both inherited TMPDIR and Unix socket limits.
        with tempfile.TemporaryDirectory(prefix="swbench-", dir="/tmp") as temporary:
            report = benchmark(binary, args.seconds, args.images, Path(temporary), args.subscribe)
        print(json.dumps(report, indent=2, sort_keys=True))
    except (OSError, ValueError, RuntimeError, AssertionError, subprocess.SubprocessError) as error:
        print(f"benchmark: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
