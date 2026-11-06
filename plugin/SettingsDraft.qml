import QtQuick

QtObject {
    id: root
    property var snapshotSettings: null
    property string snapshotRevision: ""
    property double snapshotGeneration: 0
    property int intervalSeconds: 300
    property string selection: "shuffle"
    property var folders: []
    property string baseRevision: ""
    property bool dirty: false
    readonly property bool stale: dirty && baseRevision !== snapshotRevision
    readonly property bool valid: intervalSeconds >= 1 && intervalSeconds <= 31536000
                                  && (selection === "shuffle" || selection === "sequential") && folders.length > 0

    function ingest(settings, revision) {
        if (snapshotRevision !== revision || !sameSettings(snapshotSettings, settings))
            snapshotGeneration++
        snapshotSettings = settings
        snapshotRevision = revision
        if (!dirty) revert()
    }
    function beginEdit() {
        if (!dirty) { baseRevision = snapshotRevision; dirty = true }
    }
    function setInterval(value) {
        if (value === intervalSeconds) return
        beginEdit()
        intervalSeconds = value
    }
    function setSelection(value) {
        if (value === selection) return
        beginEdit()
        selection = value
    }
    function setFolders(value) {
        if (value.length === folders.length && value.every(function(path, index) { return path === root.folders[index] })) return
        beginEdit()
        folders = value.slice()
    }
    function removeFolder(index) {
        if (folders.length <= 1 || index < 0 || index >= folders.length) return false
        var value = folders.slice()
        value.splice(index, 1)
        setFolders(value)
        return true
    }
    function value() {
        return { interval_seconds: intervalSeconds, selection: selection, folders: folders.slice() }
    }
    function revert() {
        if (!snapshotSettings) return
        intervalSeconds = snapshotSettings.interval_seconds
        selection = snapshotSettings.selection
        folders = snapshotSettings.folders.slice()
        baseRevision = snapshotRevision
        dirty = false
    }
    function sameSettings(left, right) {
        // Signal/property values may expose QVariantList rather than JS Array.
        if (!left || !right || left.interval_seconds !== right.interval_seconds
                || left.selection !== right.selection || !left.folders || !right.folders
                || left.folders.length !== right.folders.length) return false
        for (let i = 0; i < left.folders.length; ++i)
            if (left.folders[i] !== right.folders[i]) return false
        return true
    }
    function committed(reply, dispatchedGeneration) {
        // Accept the transaction response only if no later subscription authority exists.
        if (reply.ok && reply.settings && reply.revision) {
            // Subscription and command sockets are unordered. Never replace a
            // snapshot received after dispatch with an older command reply.
            if (dispatchedGeneration !== snapshotGeneration) {
                // The subscription may have delivered this commit first.
                if (snapshotRevision === reply.revision && sameSettings(snapshotSettings, reply.settings))
                    revert()
                return
            }
            dirty = false
            ingest(reply.settings, reply.revision)
        }
    }
}
