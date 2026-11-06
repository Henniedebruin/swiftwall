import QtQuick
import Quickshell
import Quickshell.Io

// The portal backend owns the native window, never the desktop shell process.
Item {
    id: root
    visible: false
    readonly property bool active: pending
    property bool pending: false
    property var reply: null
    property string failure: ""
    property bool exiting: false
    property bool cancelling: false
    property string frame: ""
    property int receivedBytes: 0
    property int timeoutMilliseconds: 185000
    signal folderChosen(string url)
    signal failed(string message)
    signal dismissed()

    function open() {
        if (pending) return
        pending = true
        reply = null
        failure = ""
        exiting = false
        cancelling = false
        frame = ""
        receivedBytes = 0
        let helper = decodeURIComponent(String(Qt.resolvedUrl("pick_folder.py")).replace(/^file:\/\//, ""))
        process.command = ["/usr/bin/python3", helper, "--watch-parent", String(Quickshell.processId)]
        deadline.restart()
        process.running = true
    }
    function cancel() {
        if (!pending || cancelling) return
        cancelling = true
        deadline.stop()
        frame = ""
        reply = null
        if (process.running) {
            process.signal(15)
            killDeadline.restart()
        }
    }
    function reject(message) {
        failure = message
        cancel()
    }
    function readChunk(data) {
        if (!pending || cancelling || data.length === 0) return
        // ASCII makes JS code units equal wire bytes. Never concatenate an
        // unbounded chunk, and reserve the final byte for the required newline.
        if (data.length > 32768 - receivedBytes) {
            reject("Folder picker returned an oversized response")
            return
        }
        receivedBytes += data.length
        if (/[^\x00-\x7f]/.test(data)) {
            reject("Folder picker returned an invalid response (expected ASCII)")
            return
        }
        if (reply !== null) {
            reject("Folder picker returned a duplicate response")
            return
        }
        let newline = data.indexOf("\n")
        if (newline < 0) {
            if (receivedBytes >= 32768) reject("Folder picker returned an oversized unterminated response")
            else frame += data
            return
        }
        if (newline !== data.length - 1) {
            reject("Folder picker returned an invalid or duplicate response")
            return
        }
        let text = frame + data.slice(0, newline)
        frame = ""
        try {
            let value = JSON.parse(text)
            if (!value || value.v !== 1 || ["selected", "cancelled", "error"].indexOf(value.result) < 0
                || (value.result === "selected" && (typeof value.url !== "string" || !value.url.startsWith("file:///")))
                || (value.result === "error" && typeof value.message !== "string")) throw new Error("Invalid response")
            reply = value
        } catch (error) {
            reject("Folder picker returned an invalid response")
        }
    }
    function complete(exitCode, crashed) {
        if (!pending) return
        deadline.stop()
        killDeadline.stop()
        pending = false
        frame = ""
        if (failure) failed(failure)
        else if (cancelling) { /* Deliberate cancellation must never deliver a selection. */ }
        else if (crashed || !reply || (exitCode !== 0 && reply.result !== "error"))
            failed("Folder picker failed or exited unexpectedly. Check Python/Gio and the desktop portal.")
        else if (reply.result === "selected") folderChosen(reply.url)
        else if (reply.result === "error") failed(reply.message || "Folder picker failed")
        dismissed()
    }
    Process {
        id: process
        running: false
        stdout: SplitParser {
            splitMarker: ""
            onRead: data => root.readChunk(data)
        }
        onExited: (exitCode, exitStatus) => {
            root.exiting = true
            root.complete(exitCode, exitStatus !== 0)
        }
        onRunningChanged: {
            if (!running && root.pending) Qt.callLater(function() {
                // FailedToStart does not emit exited on every Quickshell version.
                if (root.pending && !process.running && !root.exiting) root.complete(-1, true)
            })
        }
    }
    Timer {
        id: deadline
        interval: root.timeoutMilliseconds
        onTriggered: {
            root.failure = "Folder picker timed out"
            root.cancel()
            if (!process.running) root.complete(-1, true)
        }
    }
    Timer {
        id: killDeadline
        interval: 2000
        onTriggered: if (process.running) process.signal(9)
    }
    Component.onDestruction: if (process.running) process.signal(15)
}
