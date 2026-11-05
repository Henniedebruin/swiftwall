#!/usr/bin/env python3
"""Private, offscreen Service.qml reproduction, not a host-bar acceptance test.

Run: python3 scripts/test_service_entrypoint.py
Requires the existing target/release/swiftwall, qs, and dbus-run-session. Never
builds, installs, activates a service, or inherits a desktop/session environment.
The minimal variant is deliberately not asserted to fail: this is a comparison
probe, and an absent differential is a useful (and explicitly reported) result.
Use --report /tmp/entrypoint.json to retain complete QML replies and proxy traffic.
Only disposable copies of QML are patched. All subprocess groups are bounded and
reaped in finally blocks; proxy threads/sockets are also closed before deletion.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target/release/swiftwall"
WAIT = 9

# A valid 1x1 PNG, used for all three eligible, distinct paths.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)

HOST = r'''import QtQuick
import Quickshell
import Quickshell.Io
ShellRoot {
    id: host
    property var services: ({})
    property int lastCommand: 0
    function trace(kind, data) {
        console.log("ENTRYPOINT " + JSON.stringify({kind: kind, data: data}))
    }
    function sample() {
        var s = services.swiftwall
        if (!s) return
        trace("sample", {socketPath:s.socketPath, ready:s.ready, online:s.online,
            compatible:s.compatible, connectionState:s.connectionState,
            error:s.error, instance:s.instance, seq:s.seq, status:s.status,
            serial:s.serial, active:s.activeRequest, queued:s.queue.length,
            commandPath:s.commandSocket ? s.commandSocket.path : null,
            eventsPath:s.eventsSocket ? s.eventsSocket.path : null})
    }
    FileView { id: control; path: "@CONTROL@"; blockLoading: true }
    Timer {
        interval: 100; running: true; repeat: true
        onTriggered: {
            control.reload()
            var c
            try { c = JSON.parse(control.text()) } catch (e) { return }
            if (c.serial === host.lastCommand) return
            host.lastCommand = c.serial
            var s = host.services.swiftwall
            if (!s) return
            var id = c.op === "status" ? s.requestStatus() : s[c.op]()
            host.trace("issued", {label:c.label, id:id, op:c.op})
        }
    }
    Timer { interval: 200; running: true; repeat: true; onTriggered: host.sample() }
    Component.onCompleted: {
        var component = Qt.createComponent(Qt.resolvedUrl("Service.qml"), Component.PreferSynchronous)
        trace("component", {status:component.status, error:component.errorString()})
        if (component.status !== Component.Ready) { Qt.quit(); return }
        var service = component.createObject(@PARENT@)
        if (!service) { trace("creationFailed", component.errorString()); Qt.quit(); return }
        services.swiftwall = service
        service.requestFinished.connect(function(id, op, reply) {
            host.trace("reply", {id:id, op:op, reply:reply})
        })
        service.snapshotReceived.connect(function(snapshot) { host.trace("snapshot", snapshot) })
        service.disconnected.connect(function(reason) { host.trace("disconnected", reason) })
        trace("created", {parented:@PARENTED@, socketPath:service.socketPath})
        sample()
    }
}
'''

INSTRUMENTED = r'''import QtQuick
import Quickshell.Io
SharedService {
    id: previewService
    socketPath: @SOCKET@
    property var evidenceReplies: []
    property FileView evidenceSink: FileView { path: @SINK@ }
    onRequestFinished: (id, op, reply) => {
        var evidence = {id:id, op:op, reply:reply}
        evidenceReplies = evidenceReplies.concat([evidence])
        evidenceSink.setText(JSON.stringify(evidenceReplies))
        console.log("service.reply " + JSON.stringify(evidence))
    }
}
'''


def stop(process):
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    finally:
        # Include dbus/qs descendants even if the session leader already exited.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=3)


class Proxy:
    """Byte-transparent, private Unix proxy; capture complete NDJSON frames."""

    def __init__(self, path, backend):
        self.path, self.backend = path, backend
        self.records, self.threads = [], []
        self.lock = threading.Lock()
        self.closing = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(path))
        self.listener.listen(16)
        self.listener.settimeout(.1)
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()

    def record(self, **record):
        with self.lock:
            self.records.append({"time": time.monotonic(), **record})

    def accept(self):
        number = 0
        while not self.closing.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            number += 1
            worker = threading.Thread(target=self.forward, args=(client, number), daemon=True)
            self.threads.append(worker)
            worker.start()

    def forward(self, client, number):
        upstream = socket.socket(socket.AF_UNIX)
        self.record(connection=number, event="accepted", socket=str(self.path), backend=str(self.backend))
        try:
            upstream.settimeout(1)
            upstream.connect(str(self.backend))
            client.settimeout(1)
            buffers = {client: b"", upstream: b""}
            while not self.closing.is_set():
                readable, _, _ = select.select([client, upstream], [], [], .1)
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    dest = upstream if source is client else client
                    dest.sendall(data)
                    buffers[source] += data
                    if len(buffers[source]) > 2 * 1024 * 1024:
                        raise RuntimeError("proxy frame bound exceeded")
                    while b"\n" in buffers[source]:
                        line, buffers[source] = buffers[source].split(b"\n", 1)
                        self.record(connection=number, direction="request" if source is client else "reply",
                                    frame=json.loads(line))
        except (OSError, ValueError, RuntimeError) as error:
            self.record(connection=number, event="error", error=str(error))
        finally:
            client.close()
            upstream.close()

    def close(self):
        self.closing.set()
        self.listener.close()
        self.thread.join(timeout=2)
        for worker in self.threads:
            worker.join(timeout=2)
        assert not self.thread.is_alive() and not any(t.is_alive() for t in self.threads)


class Probe:
    def __init__(self, root, variant, parented):
        self.root, self.variant, self.parented = root, variant, parented
        self.daemon = self.qs = self.proxy = None
        self.files = []
        self.serial = 0
        for name in ("home", "config", "state", "cache", "data", "runtime", "backend", "walls"):
            (root / name).mkdir(mode=0o700)
        self.env = {
            "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(root / "home"),
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_STATE_HOME": str(root / "state"),
            "XDG_CACHE_HOME": str(root / "cache"), "XDG_DATA_HOME": str(root / "data"),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
            "SWIFTWALL_CONFIG": str(root / "config.toml"),
            "SWIFTWALL_STATE_DIR": str(root / "state"),
            "SWIFTWALL_RUNTIME_DIR": str(root / "backend"),
            "SWIFTWALL_BACKGROUND_LINK": str(root / "background"),
            "SWIFTWALL_OMARCHY_COMMAND": str(root / "fake-setter"),
            "DBUS_SYSTEM_BUS_ADDRESS": "unix:path=" + str(root / "no-system-bus"),
            "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software",
            "QSG_RHI_BACKEND": "software",
        }
        for index in range(3):
            (root / "walls" / f"{index}.png").write_bytes(PNG)
        (root / "background").symlink_to(root / "walls/0.png")
        (root / "config.toml").write_text(
            'interval_seconds = 3600\nselection = "sequential"\nfolders = ['
            + json.dumps(str(root / "walls")) + ']\n')
        # No shell, executable lookup, or external command in this backend.
        (root / "fake-setter").write_text('''#!/usr/bin/python3
import json, os, sys
from pathlib import Path
root = Path(__file__).resolve().parent
assert sys.argv[1:4] == ["theme", "bg", "set"] and len(sys.argv) == 5
image = Path(sys.argv[4]).resolve()
assert image.parent == root / "walls"
link = Path(os.environ["SWIFTWALL_BACKGROUND_LINK"])
assert link == root / "background"
with (root / "setter.jsonl").open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
link.unlink(missing_ok=True)
link.symlink_to(image)
''')
        (root / "fake-setter").chmod(0o700)
        self.socket_path = root / "preview.sock"
        source = (ROOT / "plugin/SharedService.qml").read_text()
        begin = source.index("    property string socketPath:")
        end = source.index("    property bool enabled:", begin)
        # Pin the base default *before* any object or Component.onCompleted runs.
        source = source[:begin] + "    property string socketPath: " + json.dumps(str(self.socket_path)) + "\n" + source[end:]
        (root / "SharedService.qml").write_text(source)
        if variant == "minimal":
            source = 'import QtQuick\nSharedService { socketPath: ' + json.dumps(str(self.socket_path)) + ' }\n'
        else:
            source = INSTRUMENTED.replace("@SOCKET@", json.dumps(str(self.socket_path))).replace(
                "@SINK@", json.dumps(str(root / "evidence.json")))
        (root / "Service.qml").write_text(source)
        (root / "control.json").write_text('{"serial":0}')
        (root / "shell.qml").write_text(HOST.replace("@CONTROL@", str(root / "control.json"))
            .replace("@PARENT@", "host" if parented else "null")
            .replace("@PARENTED@", "true" if parented else "false"))

    def spawn(self, argv, name):
        log = (self.root / name).open("a")
        self.files.append(log)
        return subprocess.Popen(argv, env=self.env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    def events(self):
        events = []
        for line in (self.root / "qs.log").read_text(errors="replace").splitlines():
            if "ENTRYPOINT " in line:
                try:
                    events.append(json.loads(line.split("ENTRYPOINT ", 1)[1]))
                except ValueError:
                    pass
        return events

    def wait(self, predicate, timeout=WAIT, after=0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in reversed(self.events()[after:]):
                if predicate(event):
                    return event["data"]
            if self.qs.poll() is not None:
                raise AssertionError("private qs exited: " + (self.root / "qs.log").read_text())
            time.sleep(.05)
        return None

    def request(self, label, op):
        self.serial += 1
        temporary = self.root / "control.new"
        temporary.write_text(json.dumps({"serial": self.serial, "label": label, "op": op}))
        temporary.replace(self.root / "control.json")
        issued = self.wait(lambda e: e["kind"] == "issued" and e["data"]["label"] == label)
        assert issued, "host did not issue " + label
        reply = self.wait(lambda e: e["kind"] == "reply" and e["data"]["id"] == issued["id"])
        assert reply, "no requestFinished for " + label
        return {"label": label, **reply}

    def start_daemon(self):
        self.daemon = self.spawn([str(BINARY), "daemon"], "daemon.log")
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            if self.daemon.poll() is not None:
                raise AssertionError("daemon exited: " + (self.root / "daemon.log").read_text())
            try:
                response = self.direct_status()
                if response.get("ok") and response["status"].get("eligible_images") == 3 and not response["status"].get("scanning"):
                    return response
            except (OSError, ValueError):
                pass
            time.sleep(.05)
        raise AssertionError("private daemon startup timeout")

    def direct_status(self):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(1)
            client.connect(str(self.root / "backend/control.sock"))
            client.sendall(b'{"v":1,"id":"python-verification","op":"status"}\n')
            with client.makefile("rb") as stream:
                line = stream.readline(1048577)
                assert len(line) <= 1048576, "oversized status"
                return json.loads(line)

    def run(self):
        result = {"variant": self.variant, "parented": self.parented, "requests": []}
        self.result = result
        try:
            self.proxy = Proxy(self.socket_path, self.root / "backend/control.sock")
            self.qs = self.spawn([shutil.which("dbus-run-session"), "--", shutil.which("qs"),
                                  "--no-color", "--path", str(self.root)], "qs.log")
            created = self.wait(lambda e: e["kind"] == "created")
            assert created and created["socketPath"] == str(self.socket_path), "creation/socket selection failed"
            initial = self.request("initial-unavailable", "status")
            result["requests"].append(initial)
            assert initial["reply"].get("code") == "unavailable", initial
            result["startup_status"] = self.start_daemon()
            online = self.wait(lambda e: e["kind"] == "sample" and e["data"]["online"])
            result["online_after_start"] = online
            for index in range(1, 4):
                result["requests"].append(self.request("status" + str(index), "status"))
            for op, paused in (("pause", True), ("resume", False), ("next", None)):
                before_action = len(self.events())
                response = self.request(op, op)
                result["requests"].append(response)
                if online:
                    assert response["reply"].get("ok"), response
                    if paused is not None:
                        snapshot = self.wait(lambda e: e["kind"] == "sample" and
                            e["data"]["status"].get("paused") is paused, after=before_action)
                        assert snapshot, "subscription did not observe " + op
                        assert self.direct_status()["status"]["paused"] is paused
            if online:
                completed = self.wait(lambda e: e["kind"] == "sample" and
                    not e["data"]["status"].get("applying", True) and
                    e["data"]["status"].get("observed_wallpaper") == str(self.root / "walls/1.png"),
                    after=before_action)
                assert completed, "next did not complete through fake backend"
                assert all(r["reply"].get("ok") for r in result["requests"][1:])
                assert (self.root / "setter.jsonl").exists(), "next never reached fake backend"
                assert (self.root / "background").resolve() == self.root / "walls/1.png"
            result["after_actions_status"] = self.direct_status()
            # A watermark excludes pre-stop offline observations without dropping
            # the original hello/reply evidence or changing the service/host map.
            before_stop = len(self.events())
            stop(self.daemon)
            self.daemon = None
            result["offline_after_stop"] = self.wait(lambda e: e["kind"] == "sample" and not e["data"]["online"], 3, after=before_stop)
            result["restart_status"] = self.start_daemon()
            result["online_after_restart"] = self.wait(lambda e: e["kind"] == "sample" and e["data"]["online"] and
                (not online or e["data"]["instance"] != online["instance"]))
            result["requests"].append(self.request("restart-status", "status"))
            if online:
                assert result["offline_after_stop"] and result["online_after_restart"], "restart/reconnect failed"
                assert result["requests"][-1]["reply"].get("ok")
            result["final_status"] = self.direct_status()
            result["setter_calls"] = [json.loads(line) for line in (self.root / "setter.jsonl").read_text().splitlines()] if (self.root / "setter.jsonl").exists() else []
            if self.variant == "instrumented":
                result["evidenceReplies"] = json.loads((self.root / "evidence.json").read_text())
                assert len(result["evidenceReplies"]) >= len(result["requests"]), "incomplete FileView evidence"
            result["outcome"] = "working" if online and result["online_after_restart"] else "not-online"
        finally:
            stop(self.qs)
            stop(self.daemon)
            if self.proxy:
                self.proxy.close()
                result["traffic"] = self.proxy.records
            for log in self.files:
                log.close()
            result["qml_log"] = (self.root / "qs.log").read_text(errors="replace") if (self.root / "qs.log").exists() else ""
            result["daemon_log"] = (self.root / "daemon.log").read_text(errors="replace") if (self.root / "daemon.log").exists() else ""
        requests = [r["frame"] for r in result["traffic"] if r.get("direction") == "request"]
        result["wire_operations"] = [r.get("op") for r in requests]
        if result["online_after_start"]:
            for op in ("hello", "subscribe", "status", "pause", "resume", "next"):
                assert op in result["wire_operations"], "missing proxy evidence: " + op
            assert sum(r.get("op") == "hello" for r in requests) >= 2, "no restart handshake"
            assert sum(r.get("op") == "subscribe" for r in requests) >= 2, "no restart subscription"
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="retain full evidence JSON outside the disposable fixture")
    args = parser.parse_args()
    for executable in ("qs", "dbus-run-session"):
        if not shutil.which(executable):
            parser.error(executable + " is required")
    if not BINARY.is_file() or not os.access(BINARY, os.X_OK):
        parser.error("existing executable target/release/swiftwall required (no build performed)")
    report = {"binary": str(BINARY), "binary_sha256": hashlib.sha256(BINARY.read_bytes()).hexdigest(),
              "scope": "private offscreen only; no installed host/bar scoping claim", "cases": []}
    try:
        for parented in (False, True):
            for variant in ("minimal", "instrumented"):
                print(f"Running {variant}, parent={'root' if parented else 'null'}", flush=True)
                with tempfile.TemporaryDirectory(prefix="sw-entry-") as temporary:
                    probe = Probe(Path(temporary), variant, parented)
                    try:
                        result = probe.run()
                    except Exception as error:
                        probe.result["error"] = str(error)
                        report["cases"].append(probe.result)
                        print((probe.root / "qs.log").read_text(errors="replace") if (probe.root / "qs.log").exists() else "no qs log", flush=True)
                        print((probe.root / "daemon.log").read_text(errors="replace") if (probe.root / "daemon.log").exists() else "no daemon log", flush=True)
                        raise
                    report["cases"].append(result)
                    print(json.dumps({"variant": variant, "parented": parented, "outcome": result["outcome"],
                        "wire_operations": result["wire_operations"], "setter_calls": len(result["setter_calls"]),
                        "replies": [{"label": r["label"], "ok": r["reply"].get("ok"), "code": r["reply"].get("code")} for r in result["requests"]]}), flush=True)
        report["differential_reproduced"] = any(
            report["cases"][i]["outcome"] != report["cases"][i + 1]["outcome"] for i in (0, 2))
        print("Variant differential reproduced: " + str(report["differential_reproduced"]), flush=True)
        assert all(c["outcome"] == "working" for c in report["cases"] if c["variant"] == "instrumented"), "instrumented baseline failed"
    finally:
        if args.report:
            args.report.write_text(json.dumps(report, indent=2) + "\n")
            print("Evidence: " + str(args.report), flush=True)


if __name__ == "__main__":
    main()
