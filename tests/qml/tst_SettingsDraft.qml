import QtQuick
import QtTest
import "../../plugin"

TestCase {
    name: "SettingsDraft"
    SettingsDraft { id: draft }
    function settings() { return {interval_seconds: 300, selection: "shuffle", folders: ["/one", "/two"]} }
    function init() {
        draft.dirty = false
        draft.ingest(settings(), "instance:1")
    }
    function test_initialRevisionAndCopies() {
        compare(draft.baseRevision, "instance:1")
        verify(!draft.dirty); verify(!draft.stale); verify(draft.valid)
        var value = draft.value(); value.folders.push("/three")
        compare(draft.folders.length, 2)
        var paths = ["/a", "/b"]; draft.setFolders(paths); paths.push("/c")
        compare(draft.folders.length, 2)
    }
    function test_noOpSettersKeepPristine() {
        draft.setInterval(draft.intervalSeconds)
        verify(!draft.dirty)
        draft.setSelection(draft.selection)
        verify(!draft.dirty)
        draft.setFolders(draft.folders.slice())
        verify(!draft.dirty); verify(!draft.stale)
        compare(draft.baseRevision, "instance:1")
        var newer = settings(); newer.interval_seconds = 600
        draft.ingest(newer, "instance:2")
        compare(draft.intervalSeconds, 600)
        compare(draft.baseRevision, "instance:2")
        verify(!draft.dirty)
    }
    function test_stalePreservesOriginalRevision() {
        draft.setInterval(42)
        var newer = settings(); newer.interval_seconds = 600
        draft.ingest(newer, "instance:2")
        verify(draft.stale); compare(draft.intervalSeconds, 42)
        compare(draft.baseRevision, "instance:1")
        draft.setSelection("sequential")
        compare(draft.baseRevision, "instance:1")
        draft.revert()
        compare(draft.intervalSeconds, 600); compare(draft.baseRevision, "instance:2")
        verify(!draft.dirty); verify(!draft.stale)
    }
    function test_noOptimisticCommit() {
        draft.setInterval(42)
        var dispatchedAt = draft.snapshotGeneration
        draft.committed({ok: false, code: "revision_conflict"}, dispatchedAt)
        verify(draft.dirty); compare(draft.snapshotSettings.interval_seconds, 300)
        draft.committed({ok: true}, dispatchedAt)
        verify(draft.dirty)
        var saved = settings(); saved.interval_seconds = 42
        draft.committed({ok: true, settings: saved, revision: "instance:2"}, dispatchedAt)
        verify(!draft.dirty); compare(draft.intervalSeconds, 42)
        compare(draft.baseRevision, "instance:2")
    }
    function test_unchangedAuthoritySnapshotDoesNotInvalidateSaveResponse() {
        draft.setInterval(42)
        var dispatchedAt = draft.snapshotGeneration
        draft.ingest(settings(), "instance:1")
        compare(draft.snapshotGeneration, dispatchedAt)
        var saved = settings(); saved.interval_seconds = 42
        draft.committed({ok: true, settings: saved, revision: "instance:2"}, dispatchedAt)
        verify(!draft.dirty)
        compare(draft.intervalSeconds, 42)
        compare(draft.baseRevision, "instance:2")
    }
    function test_snapshotBeforeOldResponsePreservesNewerAuthorityAndDirtyDraft() {
        draft.setInterval(42)
        var dispatchedAt = draft.snapshotGeneration
        var newer = settings(); newer.interval_seconds = 600; newer.selection = "sequential"
        draft.ingest(newer, "opaque-newer")

        var saved = settings(); saved.interval_seconds = 42
        draft.committed({ok: true, settings: saved, revision: "opaque-older"}, dispatchedAt)

        compare(draft.snapshotRevision, "opaque-newer")
        compare(draft.snapshotSettings.interval_seconds, 600)
        compare(draft.snapshotSettings.selection, "sequential")
        compare(draft.intervalSeconds, 42)
        compare(draft.baseRevision, "instance:1")
        verify(draft.dirty)
        verify(draft.stale)
    }
    function test_matchingSnapshotBeforeResponseCompletesOrdinarySave() {
        draft.setInterval(42)
        var dispatchedAt = draft.snapshotGeneration
        var saved = settings(); saved.interval_seconds = 42
        draft.ingest(saved, "opaque-saved")

        draft.committed({ok: true, settings: saved, revision: "opaque-saved"}, dispatchedAt)

        compare(draft.intervalSeconds, 42)
        compare(draft.snapshotRevision, "opaque-saved")
        compare(draft.baseRevision, "opaque-saved")
        verify(!draft.dirty)
        verify(!draft.stale)
    }
    function test_conflictingSnapshotWithSameOpaqueRevisionStillWins() {
        draft.setInterval(42)
        var dispatchedAt = draft.snapshotGeneration
        var newer = settings(); newer.interval_seconds = 800
        draft.ingest(newer, "opaque-token")

        var saved = settings(); saved.interval_seconds = 42
        draft.committed({ok: true, settings: saved, revision: "opaque-token"}, dispatchedAt)

        compare(draft.snapshotSettings.interval_seconds, 800)
        compare(draft.intervalSeconds, 42)
        verify(draft.dirty)
        verify(draft.stale)
    }
    function test_noOpSaveResponseClearsDirtyDraft() {
        draft.setInterval(42)
        draft.setInterval(300)
        verify(draft.dirty)
        var dispatchedAt = draft.snapshotGeneration

        draft.committed({ok: true, settings: settings(), revision: "instance:1"}, dispatchedAt)

        compare(draft.intervalSeconds, 300)
        compare(draft.baseRevision, "instance:1")
        verify(!draft.dirty)
        verify(!draft.stale)
    }
    function test_failureAfterNewerSnapshotKeepsDraftAndAuthority() {
        draft.setInterval(42)
        var dispatchedAt = draft.snapshotGeneration
        var newer = settings(); newer.interval_seconds = 900
        draft.ingest(newer, "restart:opaque")

        draft.committed({ok: false, code: "disconnected"}, dispatchedAt)

        compare(draft.snapshotRevision, "restart:opaque")
        compare(draft.snapshotSettings.interval_seconds, 900)
        compare(draft.intervalSeconds, 42)
        verify(draft.dirty)
        verify(draft.stale)
    }
    function test_responseBeforeNewerSnapshotFollowsNewestAuthority() {
        draft.setInterval(42)
        var dispatchedAt = draft.snapshotGeneration
        var saved = settings(); saved.interval_seconds = 42
        draft.committed({ok: true, settings: saved, revision: "opaque-saved"}, dispatchedAt)

        var newer = settings(); newer.interval_seconds = 700; newer.selection = "sequential"
        draft.ingest(newer, "opaque-newer")

        compare(draft.intervalSeconds, 700)
        compare(draft.selection, "sequential")
        compare(draft.baseRevision, "opaque-newer")
        verify(!draft.dirty)
        verify(!draft.stale)
    }
    function test_validation_data() {
        return [{tag: "zero", interval: 0, mode: "shuffle", folders: ["/a"], valid: false},
                {tag: "lower", interval: 1, mode: "shuffle", folders: ["/a"], valid: true},
                {tag: "upper", interval: 31536000, mode: "sequential", folders: ["/a"], valid: true},
                {tag: "overflow", interval: 31536001, mode: "shuffle", folders: ["/a"], valid: false},
                {tag: "mode", interval: 300, mode: "random", folders: ["/a"], valid: false},
                {tag: "empty", interval: 300, mode: "shuffle", folders: [], valid: false}]
    }
    function test_validation(data) {
        draft.setInterval(data.interval); draft.setSelection(data.mode); draft.setFolders(data.folders)
        compare(draft.valid, data.valid)
    }
    function test_removeGuards() {
        verify(!draft.removeFolder(-1)); verify(!draft.removeFolder(2)); verify(!draft.dirty)
        verify(draft.removeFolder(0)); compare(draft.folders[0], "/two")
        verify(!draft.removeFolder(0)); compare(draft.folders.length, 1)
    }
    function test_cleanSnapshotFollowsDaemon() {
        var value = settings(); value.interval_seconds = 99
        draft.ingest(value, "restart:1")
        compare(draft.intervalSeconds, 99); compare(draft.baseRevision, "restart:1")
        verify(!draft.dirty)
    }
}
