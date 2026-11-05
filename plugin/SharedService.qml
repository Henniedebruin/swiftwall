pragma ComponentBehavior: Bound

import QtQuick
import Quickshell
import Quickshell.Io

// Connection model only. Never starts, activates, or configures a daemon implicitly.
QtObject {
    id: root

    property string socketPath: (Quickshell.env("SWIFTWALL_RUNTIME_DIR")
                                || (Quickshell.env("XDG_RUNTIME_DIR") ? Quickshell.env("XDG_RUNTIME_DIR") + "/swiftwall" : "")) + "/control.sock"
    property bool enabled: true
    property bool online: false
    property bool compatible: false
    property string connectionState: "offline"
    property string error: ""
    property var status: ({})
    property var settings: null
    property string revision: ""
    property string instance: ""
    property double seq: -1
    property double countdownDeadline: 0
    readonly property bool busy: activeRequest !== null || queue.length > 0
    readonly property int requestLimit: 65536
    readonly property int responseLimit: 1048576
    property int reconnectDelay: 500
    property var queue: []
    property var activeRequest: null
    property var commandSocket: null
    property var eventsSocket: null
    property int serial: 0
    property bool stopping: false
    property bool ready: false

    signal requestFinished(string id, string op, var reply)
    signal snapshotReceived(var snapshot)
    signal disconnected(string reason)

    function byteLength(text) {
        // Count UTF-8 bytes without allocating an encoded copy of an oversized frame.
        var size = 0
        for (var i = 0; i < text.length; ++i) {
            var c = text.charCodeAt(i)
            if (c < 128) size++
            else if (c < 2048) size += 2
            else if (c >= 0xd800 && c <= 0xdbff && i + 1 < text.length
                     && text.charCodeAt(i + 1) >= 0xdc00 && text.charCodeAt(i + 1) <= 0xdfff) { size += 4; i++ }
            else size += 3
        }
        return size
    }

    function failure(code, message) { return { ok: false, code: code, error: message } }
    function notifyLater(id, op, reply) {
        Qt.callLater(function() { root.requestFinished(id, op, reply) })
    }
    function validSettings(value) {
        return value && Number.isInteger(value.interval_seconds)
            && value.interval_seconds >= 1 && value.interval_seconds <= 31536000
            && (value.selection === "shuffle" || value.selection === "sequential")
            && Array.isArray(value.folders) && value.folders.length > 0
            && value.folders.every(function(path) { return typeof path === "string" && path.charAt(0) === "/" })
    }
    function supports(reply) {
        return reply.v === 1 && reply.protocol_version === 1 && typeof reply.daemon_version === "string"
            && Array.isArray(reply.capabilities)
            && ["settings", "subscribe", "sequential"].every(function(c) { return reply.capabilities.indexOf(c) >= 0 })
    }
    function request(op, values, internal) {
        var id = "swiftwall-" + (++serial)
        if (!internal && (!online || !compatible)) {
            notifyLater(id, op, failure("unavailable", error || "Swiftwall is offline"))
            return id
        }
        if (queue.length >= 32) {
            notifyLater(id, op, failure("busy", "Too many queued requests"))
            return id
        }
        var frame = { v: 1, id: id, op: op }
        if (values) for (var key in values) frame[key] = values[key]
        var wire = JSON.stringify(frame) + "\n"
        if (byteLength(wire) > requestLimit) {
            notifyLater(id, op, failure("frame_too_large", "Request exceeds 64 KiB"))
            return id
        }
        queue = queue.concat([{ id: id, op: op, wire: wire }])
        pump()
        return id
    }
    function hello() { return request("hello") }
    function getSettings() { return request("getsettings") }
    function requestStatus() {
        if (activeRequest && activeRequest.op === "status") return activeRequest.id
        for (var i = 0; i < queue.length; ++i) if (queue[i].op === "status") return queue[i].id
        return request("status")
    }
    function next() { return request("next") }
    function previous() { return request("previous") }
    function pause() { return request("pause") }
    function resume() { return request("resume") }
    function updateSettings(value, baseRevision) {
        return request("updatesettings", { if_revision: baseRevision, settings: value })
    }
    function validateFolders(folders) { return request("validatefolders", { folders: folders }) }

    function pump() {
        if (stopping || activeRequest || queue.length === 0 || !enabled) return
        activeRequest = queue[0]
        queue = queue.slice(1)
        commandSocket = socketComponent.createObject(root, { eventStream: false, path: socketPath })
        commandTimeout.restart()
        commandSocket.connected = true
    }
    function disposeCommand() {
        commandTimeout.stop()
        var socket = commandSocket
        commandSocket = null
        if (socket) { socket.retired = true; socket.connected = false; socket.destroy() }
    }
    function finish(reply) {
        var current = activeRequest
        if (!current) return
        activeRequest = null
        disposeCommand()
        if (current.op === "hello" && !compatible) {
            if (!reply.ok || !supports(reply) || !validSettings(reply.settings) || typeof reply.revision !== "string") {
                requestFinished(current.id, current.op, reply)
                offline("Requires Swiftwall 0.2 with settings, subscribe and sequential capabilities", true)
                return
            }
            compatible = true
            error = ""
            eventsSocket = socketComponent.createObject(root, { eventStream: true, path: socketPath })
            subscriptionTimeout.restart()
            eventsSocket.connected = true
        }
        // Active status/settings are authoritative subscription snapshots only.
        // A targeted status response merely re-anchors the local countdown.
        if (current.op === "status" && reply.ok && reply.status) anchorCountdown(reply.status)
        if (!reply.ok) error = reply.error || "Request failed"
        else error = ""
        requestFinished(current.id, current.op, reply)
        Qt.callLater(root.pump)
    }
    function anchorCountdown(value) {
        var seconds = value.next_change_in_seconds
        countdownDeadline = typeof seconds === "number" && isFinite(seconds)
            ? Date.now() + Math.max(0, seconds) * 1000 : 0
    }
    function offline(message, unsupported, retry) {
        if (stopping) return
        stopping = true
        online = false
        compatible = false
        connectionState = unsupported ? "unsupported" : "offline"
        error = message
        countdownDeadline = 0
        subscriptionTimeout.stop()
        reconnectTimer.stop()
        disposeCommand()
        var socket = eventsSocket
        eventsSocket = null
        if (socket) { socket.retired = true; socket.connected = false; socket.destroy() }
        var pending = activeRequest ? [activeRequest].concat(queue) : queue
        activeRequest = null
        queue = []
        for (var i = 0; i < pending.length; ++i)
            notifyLater(pending[i].id, pending[i].op, failure("disconnected", message))
        stopping = false
        if (enabled && !unsupported && retry !== false) {
            reconnectTimer.interval = reconnectDelay
            reconnectDelay = Math.min(30000, reconnectDelay * 2)
            reconnectTimer.restart()
        }
        // A listener may reconnect immediately; let it cancel this retry rather
        // than scheduling a stale timer after its new connection has started.
        disconnected(message)
    }
    function reconnect() {
        reconnectTimer.stop()
        offline("Connecting to Swiftwall", false, false)
        seq = -1
        if (!enabled || !socketPath || socketPath.charAt(0) !== "/") return
        connectionState = "connecting"
        request("hello", null, true)
    }
    function receive(socket, chunk) {
        if (socket.retired) return
        // Server NDJSON is ASCII-only on the wire: non-ASCII code points use
        // JSON escapes (including surrogate pairs), decoded after frame assembly.
        // A raw replacement character violates that contract; fail closed.
        if (chunk.indexOf("\ufffd") >= 0) {
            offline("Unexpected replacement character in ASCII-only Swiftwall JSON wire data", false)
            return
        }
        // Empty SplitParser marker delivers chunks, not an unbounded internal line
        // buffer. Bound our partial NDJSON frame before appending or JSON.parse.
        // ASCII wire data survives arbitrary chunk boundaries. Quickshell's
        // temporary readAll() allocation is outside the QML buffer limit.
        var offset = 0
        while (offset < chunk.length && !socket.retired) {
            var end = chunk.indexOf("\n", offset)
            var piece = chunk.substring(offset, end < 0 ? chunk.length : end)
            var bytes = byteLength(piece)
            if (socket.bufferBytes + bytes + 1 > responseLimit) {
                offline("Response exceeds 1 MiB", false)
                return
            }
            socket.buffer += piece
            socket.bufferBytes += bytes
            if (end < 0) return
            var line = socket.buffer
            socket.buffer = ""
            socket.bufferBytes = 0
            var reply
            try { reply = JSON.parse(line) } catch (e) {
                offline("Invalid Swiftwall response; requires Swiftwall 0.2", connectionState === "connecting")
                return
            }
            if (!reply || typeof reply !== "object" || Array.isArray(reply)) {
                offline("Invalid protocol response; requires Swiftwall 0.2", true)
                return
            }
            if (socket.eventStream) {
                if (!supports(reply) || reply.event !== "snapshot" || !reply.status || !validSettings(reply.settings)
                        || typeof reply.instance !== "string" || !Number.isSafeInteger(reply.seq)
                        || typeof reply.revision !== "string") {
                    offline(reply.error || "Invalid subscription snapshot", false)
                    return
                }
                if (instance !== reply.instance || reply.seq > seq) {
                    instance = reply.instance
                    seq = reply.seq
                    status = reply.status
                    settings = reply.settings
                    revision = reply.revision
                    anchorCountdown(status)
                    snapshotReceived(reply)
                }
                var wasOnline = online
                online = true
                connectionState = "online"
                error = ""
                reconnectDelay = 500
                subscriptionTimeout.stop()
                reconnectTimer.stop()
                if (!wasOnline) Qt.callLater(function() { if (root.online) root.requestStatus() })
            } else {
                if (!activeRequest) return
                // v0.1 errors have no correlation envelope. Never send mutations.
                if (!supports(reply) || reply.id !== activeRequest.id || typeof reply.ok !== "boolean") {
                    offline("Requires Swiftwall 0.2 (versioned protocol unavailable)", true)
                    return
                }
                finish(reply)
                return
            }
            offset = end + 1
        }
    }

    onSocketPathChanged: if (ready) reconnect()
    onEnabledChanged: if (ready) {
        if (enabled) reconnect()
        else { reconnectTimer.stop(); offline("Connection disabled", false) }
    }
    Component.onCompleted: { ready = true; if (enabled) reconnect() }
    Component.onDestruction: { stopping = true; disposeCommand(); if (eventsSocket) eventsSocket.connected = false }

    property Timer reconnectTimer: Timer {
        onTriggered: if (root.enabled && !root.online) root.reconnect()
    }
    property Timer commandTimeout: Timer {
        interval: 30000
        onTriggered: root.offline("Swiftwall request timed out after 30 seconds; commit outcome may be unknown", false)
    }
    property Timer subscriptionTimeout: Timer {
        interval: 30000
        onTriggered: root.offline("Swiftwall subscription timed out after 30 seconds", false)
    }
    property Component socketComponent: Component {
        Socket {
            id: socket
            property bool eventStream: false
            property bool retired: false
            property string buffer: ""
            property int bufferBytes: 0
            parser: SplitParser {
                splitMarker: ""
                onRead: data => root.receive(socket, data)
            }
            onConnectedChanged: {
                if (retired) return
                if (connected) {
                    if (eventStream) {
                        write(JSON.stringify({ v: 1, id: "subscribe-" + (++root.serial), op: "subscribe" }) + "\n")
                    } else if (root.activeRequest) write(root.activeRequest.wire)
                    flush()
                } else root.offline("Swiftwall disconnected", false)
            }
            onError: if (!retired) root.offline("Cannot connect to Swiftwall at " + root.socketPath, false)
        }
    }
}
