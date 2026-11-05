#!/usr/bin/env python3
"""Private QQmlEngine same-URL cache probe; no installed shell or real setter.

Run: python3 scripts/test_qml_service_cache.py
Requires qs and dbus-run-session only. Retains source, wire, QML log and report
in a mode-0700 /tmp/sw-qml-cache-* directory. A confirmed stale-source result is
reported, not asserted away: either cache outcome is a valid experiment result.
No cache-clearing method is invoked, no daemon is launched, and only hello,
subscribe and status are accepted by the recording fixtures.
"""

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time

import plugin_support
from preview_bundle import PRODUCTION_ID, stage_preview

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "QML_CACHE "

HOST = r'''import QtQuick
import Quickshell
import Quickshell.Io
ShellRoot {
    id: host
    property var service: null
    property var component: null
    property int serial: 0
    property string label: ""
    property var retired: []
    function trace(kind, data) {
        console.log("QML_CACHE " + JSON.stringify({kind:kind, data:data}))
    }
    // Called synchronously by the child's destruction handler. The later timer
    // belongs to the host; it never captures a deleted service/child context.
    function deferRetired(token) { retired = retired.concat([token]); retirement.restart() }
    Component {
        id: witness
        QtObject {
            property string token
            Component.onDestruction: host.deferRetired(token)
        }
    }
    Timer {
        id: retirement; interval: 150
        onTriggered: {
            host.trace("retired", {tokens:host.retired, serviceReleased:host.service === null,
                componentReleased:host.component === null})
            host.retired = []
        }
    }
    function sample() {
        if (!service) return
        trace("sample", {label:label, socketPath:service.socketPath, online:service.online,
            compatible:service.compatible, connectionState:service.connectionState,
            error:service.error, commandPath:service.commandSocket ? service.commandSocket.path : null,
            eventsPath:service.eventsSocket ? service.eventsSocket.path : null})
    }
    FileView { id: control; path: @CONTROL@; blockLoading: true }
    Timer {
        interval: 75; running: true; repeat: true
        onTriggered: {
            control.reload()
            var c
            try { c = JSON.parse(control.text()) } catch (e) { return }
            if (c.serial === host.serial) return
            host.serial = c.serial
            try {
                if (c.op === "load") {
                    if (host.service || host.component) throw new Error("not released")
                    host.label = c.label
                    host.component = Qt.createComponent(c.url, Component.PreferSynchronous)
                    host.trace("component", {label:c.label, url:c.url, status:host.component.status,
                        error:host.component.errorString()})
                    if (host.component.status !== Component.Ready) throw new Error(host.component.errorString())
                    host.service = host.component.createObject(null)
                    if (!host.service) throw new Error(host.component.errorString())
                    witness.createObject(host.service, {token:c.label})
                    host.trace("created", {label:c.label, socketPath:host.service.socketPath})
                    host.sample()
                } else if (c.op === "destroy") {
                    host.service.destroy()
                    host.service = null
                    host.component.destroy()
                    host.component = null
                    host.trace("released", {label:host.label})
                } else if (c.op === "quit") Qt.quit()
            } catch (e) { host.trace("failure", String(e)); Qt.quit() }
        }
    }
    Timer { interval: 100; running: true; repeat: true; onTriggered: host.sample() }
    Component.onCompleted: trace("ready", {clearComponentCache:typeof Qt.clearComponentCache,
        trimComponentCache:typeof Qt.trimComponentCache, gc:typeof gc,
        cacheCleared:false, mode:"Component.PreferSynchronous", parent:"createObject(null)"})
}
'''


class RecordingServer:
    """A is deliberately legacy; B implements the read-only v0.2 handshake."""

    def __init__(self, path, capable):
        self.path, self.capable = path, capable
        self.records, self.errors, self.workers = [], [], []
        self.lock = threading.Lock()
        self.closing = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(path))
        self.listener.listen(16)
        self.listener.settimeout(.1)
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()

    def record(self, **data):
        with self.lock:
            self.records.append({"time": time.monotonic(), **data})

    def accept(self):
        while not self.closing.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            worker = threading.Thread(target=self.serve, args=(client,), daemon=True)
            self.workers.append(worker)
            worker.start()

    def serve(self, client):
        client.settimeout(.1)
        buffer = b""
        try:
            with client:
                while not self.closing.is_set():
                    try:
                        data = client.recv(65536)
                    except socket.timeout:
                        continue
                    if not data:
                        return
                    buffer += data
                    if len(buffer) > 65536:
                        raise ValueError("oversized request")
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        request = json.loads(line)
                        self.record(direction="request", frame=request)
                        if request.get("op") not in ("hello", "subscribe", "status"):
                            raise ValueError("unexpected operation: " + repr(request))
                        if not self.capable:
                            reply = {"ok": False, "error": "legacy sentinel A: versioned protocol unavailable"}
                        else:
                            reply = {"v": 1, "protocol_version": 1, "daemon_version": "0.2.0-cache-fixture",
                                     "capabilities": ["settings", "subscribe", "sequential"],
                                     "id": request["id"], "ok": True, "revision": "fixture-1",
                                     "settings": {"interval_seconds": 3600, "selection": "sequential",
                                                  "folders": [str(self.path.parent)]},
                                     "status": {"paused": True, "next_change_in_seconds": 3600}}
                            if request["op"] == "subscribe":
                                reply.update(event="snapshot", instance="private-B", seq=1)
                        self.record(direction="reply", frame=reply)
                        client.sendall((json.dumps(reply, ensure_ascii=True) + "\n").encode("ascii"))
        except (OSError, ValueError, KeyError) as error:
            # A peer may close during deliberate service destruction.
            if not isinstance(error, OSError):
                self.errors.append(str(error))
            self.record(event="connection_end", error=str(error))

    def close(self):
        self.closing.set()
        self.listener.close()
        self.thread.join(2)
        for worker in self.workers:
            worker.join(2)
        if self.thread.is_alive() or any(t.is_alive() for t in self.workers):
            raise RuntimeError("fixture thread did not stop")


def hashes(directory):
    return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
            for name in ("Service.qml", "SharedService.qml")}


def stage(directory, source, path, explicit):
    directory.mkdir(exist_ok=True)
    begin = source.index("    property string socketPath:")
    end = source.index("    property bool enabled:", begin)
    shared = source[:begin] + "    property string socketPath: " + json.dumps(str(path)) + "\n" + source[end:]
    (directory / "SharedService.qml").write_text(shared)
    wrapper = "import QtQuick\nSharedService {"
    if explicit:
        wrapper += " socketPath: " + json.dumps(str(path))
    (directory / "Service.qml").write_text(wrapper + " }\n")
    return hashes(directory)


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
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=3)


def main():
    tools = {name: shutil.which(name) for name in ("qs", "dbus-run-session")}
    if not all(tools.values()):
        raise SystemExit("Requires existing qs and dbus-run-session; nothing installed or built")
    root = Path(tempfile.mkdtemp(prefix="sw-qml-cache-", dir="/tmp"))
    report = {"root": str(root), "scope": "one private offscreen qs engine; recording fixtures only",
              "cache_method_invoked": False, "cases": [], "sequence": [], "tools": tools}
    process = None
    servers = []
    log = None
    serial = 0
    events = []
    cursor = 0
    control = root / "control.json"
    log_path = root / "qs.log"

    def wait_for(kind, predicate=lambda data: True):
        nonlocal cursor
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            lines = log_path.read_text(errors="replace").splitlines()
            for line in lines[cursor:]:
                if PREFIX in line:
                    events.append(json.loads(line.split(PREFIX, 1)[1]))
            cursor = len(lines)
            for event in events:
                if event["kind"] == "failure":
                    raise RuntimeError(event["data"])
            for event in events:
                if event["kind"] == kind and predicate(event["data"]):
                    return event["data"]
            if process.poll() is not None:
                raise RuntimeError("private qs exited: " + log_path.read_text(errors="replace"))
            time.sleep(.05)
        raise RuntimeError("timeout waiting for " + kind)

    def command(op, **data):
        nonlocal serial
        serial += 1
        value = {"serial": serial, "op": op, **data}
        temporary = root / "control.next"
        temporary.write_text(json.dumps(value))
        temporary.replace(control)
        report["sequence"].append(value)

    try:
        for name in ("home", "config", "state", "cache", "data", "runtime", "A", "B", "bundles"):
            (root / name).mkdir(mode=0o700)
        a, b = root / "A/control.sock", root / "B/control.sock"
        servers = [RecordingServer(a, False), RecordingServer(b, True)]
        source = (ROOT / "plugin/SharedService.qml").read_text()
        report["repository_shared_sha256"] = hashlib.sha256(source.encode()).hexdigest()
        reused, fresh = root / "reused", root / "unique-manual"
        report["initial_hashes"] = stage(reused, source, a, False)
        (root / "initial-source").mkdir()
        for name in ("Service.qml", "SharedService.qml"):
            shutil.copy2(reused / name, root / "initial-source" / name)
        control.write_text('{"serial":0}')
        (root / "shell.qml").write_text(HOST.replace("@CONTROL@", json.dumps(str(control))))
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(root / "home"),
               "XDG_CONFIG_HOME": str(root / "config"), "XDG_CONFIG_DIRS": str(root / "config"),
               "XDG_STATE_HOME": str(root / "state"), "XDG_CACHE_HOME": str(root / "cache"),
               "XDG_DATA_HOME": str(root / "data"), "XDG_DATA_DIRS": "/usr/share",
               "XDG_RUNTIME_DIR": str(root / "runtime"), "SWIFTWALL_RUNTIME_DIR": str(a.parent),
               "DBUS_SYSTEM_BUS_ADDRESS": "unix:path=" + str(root / "no-system-bus"),
               "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software",
               "QSG_RHI_BACKEND": "software", "QML_DISABLE_DISK_CACHE": "1", "NO_COLOR": "1"}
        report["environment"] = env
        argv = [tools["dbus-run-session"], "--", tools["qs"], "--no-color", "--path", str(root)]
        report["argv"] = argv
        log = log_path.open("w")
        process = subprocess.Popen(argv, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        report["process_pid"] = process.pid
        report["cache_availability"] = wait_for("ready")
        expected_hashes = None
        helper_hashes = None
        for label, directory, suffix in (("initial-A", reused, ""), ("same-url-B", reused, ""),
                                          ("helper-bundle-B", None, ""),
                                          ("query-bust-B", reused, "?cache_probe=B"),
                                          ("unique-manual-B", fresh, "")):
            if label == "same-url-B":
                expected_hashes = stage(reused, source, b, True)
                report["staged_B_hashes"] = expected_hashes
                assert stage(fresh, source, b, True) == expected_hashes
            if label == "helper-bundle-B":
                # File-only helper, invoked after the same-URL case in this engine.
                # Its parent and runtime already exist as private siblings.
                generation = plugin_support.source_generation(ROOT)
                preview = stage_preview(ROOT, root / "bundles", b.parent)
                bundle, identity = preview["bundle"], preview["id"]
                if (preview["socket"] != b or bundle.name != identity
                        or not identity.startswith("local.swiftwall-preview-")
                        or identity == PRODUCTION_ID):
                    raise RuntimeError("incoherent helper bundle identity/socket")
                files = sorted(str(p.relative_to(bundle)) for p in bundle.rglob("*") if p.is_file())
                if files != sorted(plugin_support.FILES) or len(files) != 8:
                    raise RuntimeError("helper bundle does not contain the eight-file contract")
                manifest = json.loads((bundle / "manifest.json").read_text())
                expected_manifest = json.loads(base64.b64decode(generation["manifest.json"]["bytes"]))
                expected_manifest.update(id=identity, name="SwiftWall isolated preview")
                if manifest != expected_manifest:
                    raise RuntimeError("helper manifest identity/contract mismatch")
                module_lines = {}
                for name in plugin_support.FILES:
                    raw = (bundle / name).read_bytes()
                    if name.endswith(".qml") and PRODUCTION_ID in raw.decode():
                        raise RuntimeError("production module ID remains in staged QML")
                    if name not in ("manifest.json", "plugin/Service.qml", "plugin/SharedService.qml"):
                        expected = base64.b64decode(generation[name]["bytes"])
                        if name.endswith(".qml"):
                            expected = expected.decode().replace(PRODUCTION_ID, identity).encode()
                        if raw != expected:
                            raise RuntimeError("helper payload mismatch: " + name)
                    if name in ("plugin/BarWidget.qml", "plugin/Panel.qml"):
                        lines = [line.strip() for line in raw.decode().splitlines() if "moduleName:" in line]
                        if lines != ['moduleName: ' + json.dumps(identity)]:
                            raise RuntimeError("module/manifest ID mismatch: " + name)
                        module_lines[name] = lines
                directory = bundle / "plugin"
                pinned = '    property string socketPath: ' + json.dumps(str(b)) + '\n'
                shared = (directory / "SharedService.qml").read_text()
                start = source.index("    property string socketPath:")
                end = source.index("    property bool enabled:", start)
                if shared != source[:start] + pinned + source[end:]:
                    raise RuntimeError("helper base socket not pinned before completion")
                if (directory / "Service.qml").read_text() != (
                        'import QtQuick\n\nSharedService { socketPath: ' + json.dumps(str(b)) + ' }\n'):
                    raise RuntimeError("helper entrypoint socket not pinned before completion")
                helper_hashes = hashes(directory)
                report["helper_bundle"] = {
                    "id": identity, "bundle": str(bundle), "socket": str(b),
                    "parent": str(bundle.parent), "runtime": str(b.parent),
                    "validated_before_load": time.monotonic(), "both_socket_literals_pinned": True,
                    "manifest": manifest, "module_lines": module_lines,
                    "source_hashes": {name: item["sha256"] for name, item in generation.items()},
                    "staged_hashes": {name: hashlib.sha256((bundle / name).read_bytes()).hexdigest()
                                      for name in files}}
            before = [len(server.records) for server in servers]
            hello_before = [sum(r.get("direction") == "request" and r["frame"].get("op") == "hello"
                                for r in server.records) for server in servers]
            command("load", label=label, url=(directory / "Service.qml").as_uri() + suffix)
            created = wait_for("created", lambda d: d["label"] == label)
            selected = created["socketPath"]
            if selected not in (str(a), str(b)):
                raise RuntimeError("unexpected selected socket: " + selected)
            sample = wait_for("sample", lambda d: d["label"] == label and
                              d["connectionState"] in ("online", "unsupported"))
            # Leave time for status and any pending socket callbacks before destruction.
            time.sleep(.25)
            command("destroy")
            retired = wait_for("retired", lambda d: label in d["tokens"])
            if not retired["serviceReleased"] or not retired["componentReleased"]:
                raise RuntimeError("objects/components not released before delayed witness")
            wire = {name: server.records[start:] for name, server, start in zip(("A", "B"), servers, before)}
            chosen = "A" if selected == str(a) else "B"
            if not any(r.get("direction") == "request" and r["frame"].get("op") == "hello"
                       for r in wire[chosen]):
                raise RuntimeError("no hello wire evidence for selected path")
            if wire["B" if chosen == "A" else "A"]:
                raise RuntimeError("unexpected traffic to unselected private socket")
            current_hashes = hashes(directory)
            expected = helper_hashes if label == "helper-bundle-B" else expected_hashes
            if expected is not None and current_hashes != expected:
                raise RuntimeError("staged B source changed during experiment")
            hello_after = [sum(r.get("direction") == "request" and r["frame"].get("op") == "hello"
                               for r in server.records) for server in servers]
            counts = {name: {"before": old, "after": new, "increment": new - old}
                      for name, old, new in zip(("A", "B"), hello_before, hello_after)}
            if label == "helper-bundle-B":
                requests = [r for r in wire["B"] if r.get("direction") == "request"]
                snapshots = [r for r in wire["B"] if r.get("direction") == "reply"
                             and r["frame"].get("event") == "snapshot"]
                if (selected != str(b) or not sample["online"] or not sample["compatible"]
                        or sample["eventsPath"] != str(b) or not requests
                        or requests[0]["frame"]["op"] != "hello" or not snapshots
                        or not any(r["frame"]["op"] == "subscribe" for r in requests)
                        or counts["A"]["increment"] != 0 or counts["B"]["increment"] != 1
                        or requests[0]["time"] <= report["helper_bundle"]["validated_before_load"]):
                    raise RuntimeError("helper pre-hello pinning/subscription/isolation failed")
                report["helper_bundle"]["verified"] = True
                report["helper_bundle"]["first_hello"] = requests[0]
                report["helper_bundle"]["A_hello_counts"] = counts["A"]
                report["helper_bundle"]["A_wire_increment"] = len(wire["A"])
            report["cases"].append({"label": label, "created": created, "sample": sample,
                                    "hashes": current_hashes, "wire": wire, "retired": retired,
                                    "hello_counts": counts})
        cases = report["cases"]
        if cases[0]["created"]["socketPath"] != str(a):
            raise RuntimeError("initial source did not select sentinel A")
        if cases[-1]["created"]["socketPath"] != str(b) or not cases[-1]["sample"]["online"]:
            raise RuntimeError("fresh-directory B control failed")
        stale = cases[1]["created"]["socketPath"] == str(a)
        report["outcome"] = "CONFIRMED_STALE_SAME_URL" if stale else "FRESH_SAME_URL_NO_STALE_SOURCE_OBSERVED"
        report["query_bust_selected"] = next(case["created"]["socketPath"] for case in cases
                                             if case["label"] == "query-bust-B")
        command("quit")
        process.wait(timeout=5)
        text = log_path.read_text(errors="replace")
        bad = ("deleted", "ReferenceError", "TypeError", "Error:")
        report["qml_error_lines"] = [line for line in text.splitlines() if any(word in line for word in bad)]
        if report["qml_error_lines"]:
            raise RuntimeError("QML lifecycle/runtime errors; see report")
        if any(server.errors for server in servers):
            raise RuntimeError("recording fixture protocol errors")
        print(report["outcome"])
        print("Qt.clearComponentCache: " + report["cache_availability"]["clearComponentCache"] + " (never called)")
        for case in cases:
            print(case["label"] + ": " + case["created"]["socketPath"] + " / " + case["sample"]["connectionState"])
        helper = report["helper_bundle"]
        print("Helper bundle verified: " + helper["id"])
        print("Helper A hello counts: " + json.dumps(helper["A_hello_counts"])
              + "; A wire increment: " + str(helper["A_wire_increment"]))
        return 0
    except Exception as error:
        report["error"] = str(error)
        print("FAIL: " + str(error))
        return 1
    finally:
        stop(process)
        for server in servers:
            server.close()
        if log:
            log.close()
        report["events"] = events
        report["wire_all"] = {str(server.path): server.records for server in servers}
        (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print("Retained private report: " + str(root / "report.json"))


if __name__ == "__main__":
    raise SystemExit(main())
