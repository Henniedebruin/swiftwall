import QtQuick

QtObject {
    id: root
    property var snapshotSettings: null
    property string snapshotRevision: ""
    property int intervalSeconds: 300
    property string selection: "shuffle"
    property var folders: []
    property string baseRevision: ""
    property bool dirty: false
    readonly property bool stale: dirty && baseRevision !== snapshotRevision
    readonly property bool valid: intervalSeconds >= 1 && intervalSeconds <= 31536000
                                  && (selection === "shuffle" || selection === "sequential") && folders.length > 0

    function ingest(settings, revision) {
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
    function committed(reply) {
        // A successful transaction response is authoritative, not optimistic UI.
        if (reply.ok && reply.settings && reply.revision) {
            dirty = false
            ingest(reply.settings, reply.revision)
        }
    }
}
