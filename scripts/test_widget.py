"""Run only isolated, offscreen QML tests; never select an installed daemon.

Usage: python3 scripts/test_widget.py [--binary target/debug/swiftwall]
No build, install, desktop reload, network access, or production IPC is performed.
"""

import argparse
import base64
import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def terminate(process):
    if process is None:
        return
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)


def bounded_run(command, *, env, timeout, capture=False):
    process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE if capture else None,
                               stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        output, _ = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(command, process.returncode, output)
    finally:
        terminate(process)


class FixtureServer:
    """Private bounded AF_UNIX peers, never a production socket or daemon."""

    def __init__(self, path: Path, mode: str, folder: Path):
        self.mode = mode
        self.folder = folder
        self.stop = threading.Event()
        self.operations: list[str] = []
        self.errors: list[str] = []
        self.clients: list[socket.socket] = []
        self.workers: list[threading.Thread] = []
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(8)
        self.listener.settimeout(0.2)
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()

    def accept(self):
        while not self.stop.is_set():
            try:
                client, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            self.clients.append(client)
            worker = threading.Thread(target=self.respond, args=(client,), daemon=True)
            self.workers.append(worker)
            worker.start()

    def respond(self, client: socket.socket):
        try:
            client.settimeout(3)
            data = b""
            while b"\n" not in data and len(data) < 65536:
                chunk = client.recv(4096)
                if not chunk:
                    return
                data += chunk
            request = json.loads(data.split(b"\n", 1)[0])
            self.operations.append(request["op"])
            if self.mode == "legacy":
                reply = {"ok": False, "error": "Unknown command: JSON (Swiftwall 0.1)"}
            else:
                settings = {"interval_seconds": 301, "selection": "sequential", "folders": [str(self.folder)]}
                status = {"state": "ready", "paused": False, "applying": False,
                          "observed_wallpaper": str(self.folder / "café 雪 😀.png"),
                          "warnings": ['café 雪 😀 # % "quoted" \\ slash\nline']}
                reply = {"v": 1, "protocol_version": 1, "daemon_version": "0.2.0-dev",
                         "capabilities": ["settings", "subscribe", "sequential"],
                         "id": request["id"], "ok": True, "settings": settings, "status": status,
                         "revision": "fragment:1", "instance": "fragment-café 雪 😀", "seq": 1}
                if request["op"] == "subscribe":
                    reply["event"] = "snapshot"
            wire = (json.dumps(reply, ensure_ascii=True) + "\n").encode("ascii")
            # Single-byte writes with an event-loop yield split escape sequences,
            # surrogate pairs and JSON tokens without splitting UTF-8 on the wire.
            for byte in wire:
                if self.stop.is_set():
                    return
                client.sendall(bytes([byte]))
                if self.stop.wait(0.001):
                    return
            if request["op"] == "subscribe":
                self.stop.wait(30)
        except (OSError, ValueError, KeyError) as error:
            if not self.stop.is_set() and not isinstance(error, (BrokenPipeError, ConnectionResetError)):
                self.errors.append(str(error))
        finally:
            client.close()

    def close(self):
        self.stop.set()
        self.listener.close()
        self.thread.join(timeout=1)
        for client in self.clients:
            client.close()
        for worker in self.workers:
            worker.join(timeout=1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "target/debug/swiftwall",
                        help="explicit test binary (default: repository target/debug/swiftwall; no PATH fallback)")
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        parser.error(f"not an executable test binary: {binary}")
    tools = {}
    for name in ("qs", "qmltestrunner", "dbus-run-session"):
        tools[name] = ("/usr/lib/qt6/bin/qmltestrunner" if name == "qmltestrunner"
                       and Path("/usr/lib/qt6/bin/qmltestrunner").is_file() else shutil.which(name))
        if not tools[name]:
            parser.error(f"required installed tool not found: {name}")
    installed = Path("/usr/share/omarchy/shell")
    for name in ("Commons", "Ui"):
        if not (installed / name).is_dir():
            parser.error(f"read-only staging source missing: {installed / name}")

    with tempfile.TemporaryDirectory(prefix="swiftwall-qml-") as temporary, ExitStack() as resources:
        root = Path(temporary)
        extra = root / "extra café 雪 😀 folder # percent%"
        for name in ("home", "config", "state", "cache", "data", "runtime", "xdg-runtime", "desktop", "walls", extra.name, "stage/tests/qml", "stage/plugin", "bin"):
            (root / name).mkdir(parents=True, exist_ok=True)
        (root / "xdg-runtime").chmod(0o700)
        # Deliberately do not inherit DISPLAY, WAYLAND_DISPLAY, session buses,
        # Qt import overrides, LD_PRELOAD, or any SWIFTWALL_* values.
        env = {
            "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(root / "home"),
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_STATE_HOME": str(root / "state"),
            "XDG_CACHE_HOME": str(root / "cache"), "XDG_DATA_HOME": str(root / "data"),
            "XDG_RUNTIME_DIR": str(root / "xdg-runtime"), "XDG_CONFIG_DIRS": str(root / "config"),
            "XDG_DATA_DIRS": "/usr/share", "QT_QPA_PLATFORM": "offscreen",
            "QT_QUICK_BACKEND": "software", "QSG_RHI_BACKEND": "software",
            "QT_QUICK_CONTROLS_STYLE": "Basic", "QT_LOGGING_RULES": "qt.qpa.services=false",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(root / "no-session-bus"),
            "DBUS_SYSTEM_BUS_ADDRESS": "unix:path=" + str(root / "no-system-bus"),
            "SWIFTWALL_RUNTIME_DIR": str(root / "runtime"),
            "SWIFTWALL_CONFIG": str(root / "config/config.toml"),
            "SWIFTWALL_STATE_DIR": str(root / "state"),
            "SWIFTWALL_BACKGROUND_LINK": str(root / "desktop/background"),
            "SWIFTWALL_OMARCHY_COMMAND": str(root / "bin/fake-omarchy"),
            "TEST_ROOT": str(root), "TEST_EXTRA_FOLDER": str(extra),
            "TEST_FOLDER_URL": extra.as_uri(),
            "TEST_UNICODE_IMAGE": str(extra / "café 雪 😀.png"),
            "TEST_LEGACY_SOCKET": str(root / "legacy.sock"),
            "TEST_FRAGMENT_SOCKET": str(root / "fragment.sock"),
            "TEST_STALL_SOCKET": str(root / "stall.sock"), "NO_COLOR": "1",
        }
        # This executable cannot reach the real setter, and validates every write.
        setter = root / "bin/fake-omarchy"
        setter.write_text('''#!/bin/sh
set -eu
[ "$#" -eq 4 ]
[ "$1" = theme ] && [ "$2" = bg ] && [ "$3" = set ]
[ "$SWIFTWALL_BACKGROUND_LINK" = "$TEST_ROOT/desktop/background" ]
case "$4" in "$TEST_ROOT"/*) ;; *) exit 91 ;; esac
printf '%s\\n' "$4" >> "$TEST_ROOT/applied"
ln -sfn -- "$4" "$SWIFTWALL_BACKGROUND_LINK"
''')
        setter.chmod(0o700)
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
        for name in ("a.png", "b.png", "c.png"):
            (root / "walls" / name).write_bytes(png)
        (extra / "café 雪 😀.png").write_bytes(png)
        (root / "desktop/manual.png").write_bytes(png)
        (root / "desktop/background").symlink_to(root / "desktop/manual.png")
        (root / "config/config.toml").write_text(
            'interval_seconds = 300\nselection = "sequential"\nfolders = ["' + str(root / "walls") + '"]\n')
        for source in (ROOT / "plugin").iterdir():
            if source.is_file() and source.suffix in (".qml", ".py"):
                shutil.copy2(source, root / "stage/plugin" / source.name)
        for source in (ROOT / "tests/qml").glob("*.qml"):
            shutil.copy2(source, root / "stage/tests/qml" / source.name)
        for name in ("Commons", "Ui"):
            shutil.copytree(installed / name, root / "stage" / name)
        # The real surface is first probed below. Only its offscreen-unsupported
        # window backend is replaced for the remaining form/controls assertions.
        shutil.copy2(ROOT / "tests/qml/runtime.qml", root / "stage/shell.qml")
        # runtime.qml's local plugin import is relative to tests/qml. Keep that
        # location and use a root wrapper so qs resolves qs.Ui/qs.Commons here.
        (root / "stage/shell.qml").write_text('import "tests/qml"\nRuntime {}\n')
        (root / "stage/tests/qml/runtime.qml").rename(root / "stage/tests/qml/Runtime.qml")

        daemon = runner = None
        servers: list[FixtureServer] = []
        stall_thread = None
        stall = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stalled_clients = []
        stop_stall = threading.Event()
        daemon_log = resources.enter_context(open(root / "daemon.log", "w+"))

        def start_daemon():
            process = subprocess.Popen([str(binary), "daemon"], env=env, stdin=subprocess.DEVNULL,
                                       stdout=daemon_log, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    daemon_log.seek(0)
                    raise RuntimeError("isolated daemon exited: " + daemon_log.read())
                if (root / "runtime/control.sock").exists():
                    return process
                time.sleep(0.02)
            terminate(process)
            raise RuntimeError("isolated daemon socket did not appear")

        def accept_stalled():
            while not stop_stall.is_set():
                try:
                    client, _ = stall.accept()
                    stalled_clients.append(client)  # Intentionally accept but never reply.
                except TimeoutError:
                    continue
                except OSError:
                    break

        try:
            pure = bounded_run([tools["dbus-run-session"], "--", tools["qmltestrunner"],
                                "-input", str(root / "stage/tests/qml/tst_SettingsDraft.qml"),
                                "-platform", "offscreen"], env=env, timeout=30)
            if pure.returncode:
                raise RuntimeError(f"SettingsDraft QtTest failed ({pure.returncode})")
            probe = bounded_run([tools["dbus-run-session"], "--", tools["qs"], "--no-color", "--path", str(root / "stage")],
                                env=env, timeout=5, capture=True)
            if "No PanelWindow backend loaded" not in probe.stdout:
                raise RuntimeError("unexpected production-surface probe result: " + probe.stdout)
            print("LIMIT: production Ui.KeyboardPanel cannot load offscreen: No PanelWindow backend loaded", flush=True)
            shutil.copy2(ROOT / "tests/qml/OffscreenKeyboardPanel.qml", root / "stage/Ui/KeyboardPanel.qml")
            daemon = start_daemon()
            servers.append(FixtureServer(root / "legacy.sock", "legacy", extra))
            servers.append(FixtureServer(root / "fragment.sock", "fragment", extra))
            stall.bind(str(root / "stall.sock")); stall.listen(4); stall.settimeout(0.2)
            stall_thread = threading.Thread(target=accept_stalled, daemon=True)
            stall_thread.start()
            runner = subprocess.Popen([tools["dbus-run-session"], "--", tools["qs"], "--no-color", "--path", str(root / "stage")],
                                      env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True, start_new_session=True)
            lines: queue.Queue[str | None] = queue.Queue()
            output = runner.stdout
            assert output is not None

            def read_output():
                for line in output:
                    lines.put(line)
                lines.put(None)

            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            deadline = time.monotonic() + 90
            passed = False
            while time.monotonic() < deadline:
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    break
                print(line, end="", flush=True)
                if "SWIFTWALL_CONTROL stop" in line:
                    terminate(daemon); daemon = None
                elif "SWIFTWALL_CONTROL restart" in line:
                    daemon = start_daemon()
                elif "SWIFTWALL_RUNTIME_PASS" in line:
                    passed = True
                elif "SWIFTWALL_RUNTIME_FAIL" in line:
                    raise RuntimeError(line.strip())
            if not passed:
                raise RuntimeError("offscreen runtime did not report success (load failure or timeout)")
            runner.wait(timeout=5)
            if runner.returncode:
                raise RuntimeError(f"qs exited with {runner.returncode}")
            if servers[0].operations != ["hello"]:
                raise RuntimeError(f"legacy peer received unexpected requests: {servers[0].operations}")
            if sorted(servers[1].operations) != ["hello", "status", "subscribe"]:
                raise RuntimeError(f"fragment peer requests: {servers[1].operations}")
            for server in servers:
                if server.errors:
                    raise RuntimeError(f"fixture server failed: {server.errors}")
            print("PASS: isolated QtTest, native Quickshell sockets and plugin forms (test-only item surface)")
            print("PASS: Unicode roots/counts 2/4 -> 1/3; v0.1 blocked; fragmented ASCII JSON Unicode decoded")
            print("LIMIT: idle check measures request serial stability, not full CPU usage.")
            print("LIMIT: offscreen lifecycle only; no compositor focus/parenting or native portal dialog opening tested.")
        finally:
            terminate(runner)
            terminate(daemon)
            for server in servers:
                server.close()
            stop_stall.set(); stall.close()
            if stall_thread is not None:
                stall_thread.join(timeout=1)
            for client in stalled_clients:
                client.close()



if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        raise SystemExit(f"FAIL: {error}")
