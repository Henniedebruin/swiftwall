import QtQuick
import QtQuick.Window
import Quickshell
import "../../plugin" as Plugin

ShellRoot {
    id: root
    Plugin.SharedService { id: service }
    QtObject {
        id: hostShell
        function serviceFor(name) { return service }
        function updateEntryInline(name, entry) {}
    }
    QtObject {
        id: mockBar
        property QtObject shell: hostShell
        property bool vertical: false
        property int barSize: 32
        property string position: "top"
        property color barForeground: "white"
        property color barBackground: "black"
        property color urgent: "red"
        property string fontFamily: "sans-serif"
        property bool foregroundAnimationEnabled: false
        function hideTooltip(owner) {}
        function showTooltip(owner, text) {}
        function switchPanelFrom(owner, direction) { return false }
    }
    // A parentless Item under ShellRoot is effectively invisible even after
    // visible=true. Give the real widget a QQuickWindow scene, but never show
    // this test host on a desktop platform if invoked outside the runner.
    Window {
        id: widgetWindow
        width: 320
        height: 40
        visible: Quickshell.env("QT_QPA_PLATFORM") === "offscreen"
        Plugin.BarWidget { id: widget; bar: mockBar; visible: false }
    }
    Plugin.Panel { id: panel; bar: mockBar; service: service; anchorItem: widget }
    Plugin.FolderPicker { id: picker }
    QtObject {
        id: applyingService
        property bool online: true
        property bool compatible: true
        property bool busy: false
        property var status: ({applying: true, paused: false})
        property var settings: ({interval_seconds: 300, selection: "sequential", folders: [Quickshell.env("TEST_EXTRA_FOLDER")]})
        property string revision: "fake:1"
        property string error: ""
        property int pauses: 0
        property int resumes: 0
        property double countdownDeadline: Date.now() + 300000
        function resume() { resumes++ }
        function requestStatus() {}
        signal snapshotReceived(var snapshot)
        signal requestFinished(string id, string op, var reply)
        function pause() { pauses++ }
    }

    property int index: 0
    property int elapsed: 0
    property var replies: ({})
    property string pending: ""
    property string oldInstance: ""
    property int idleSerial: 0
    property int idleTicks: 0
    property double countdownStarted: 0
    property int countdownStartSeconds: 0
    property int countdownSerial: -1
    property var confirmation: null
    Connections {
        target: service
        function onRequestFinished(id, op, reply) { root.replies[id] = reply }
    }
    function check(condition, message) { if (!condition) throw new Error(message) }
    function response() { return replies[pending] }
    function ok() { if (!response()) return false; check(response().ok, JSON.stringify(response())); return true }
    function clickSave() {
        var button = find(panel, function(o) { return o.text === "Save" }, [])
        check(button !== null && button.enabled, "Save enabled")
        button.clicked()
        check(panel.saveRequestId !== "" && panel.draft.dirty, "save dispatched, not optimistic")
    }
    function selectMode(mode) {
        var group = find(panel, function(o) { return o.options && o.options.length === 2 && o.options[0].value === "shuffle" }, [])
        check(group !== null && group.enabled, "real ButtonGroup enabled")
        group._focusedIndex = mode === "shuffle" ? 0 : 1
        group.activateFocused()
        check(panel.draft.selection === mode, "ButtonGroup updates draft")
    }
    function savedRoots(count, eligible, mode) {
        if (panel.saveRequestId !== "" || service.busy || service.status.scanning) return false
        check(panel.inlineError === "", panel.inlineError)
        if (!service.settings || service.settings.folders.length !== count || service.status.eligible_images !== eligible) return false
        check(!panel.draft.dirty && panel.draft.folders.length === count, "authoritative draft roots")
        check(service.settings.selection === mode && panel.draft.selection === mode, "authoritative selection")
        check(panel.draft.baseRevision === service.revision, "authoritative revision")
        return true
    }
    function find(object, predicate, seen) {
        if (!object || seen.indexOf(object) >= 0) return null
        seen.push(object)
        if (predicate(object)) return object
        var lists = [object.children, object.data, object.contentItem]
        for (var n = 0; n < lists.length; ++n) {
            var list = lists[n]
            if (!list) continue
            if (list.length === undefined) list = [list]
            for (var i = 0; i < list.length; ++i) {
                var found = find(list[i], predicate, seen)
                if (found) return found
            }
        }
        return null
    }
    property var steps: [
        function() {
            if (!service.online || service.busy || service.status.scanning || service.status.persistence_pending
                    || !service.status.next_change_in_seconds) return false
            check(service.compatible && service.revision !== "" && service.seq >= 0, "hello/subscribe snapshot")
            check(service.eventsSocket !== null, "native subscription socket")
            check(panel.draft.baseRevision === service.revision, "initial panel revision")
            oldInstance = service.instance
            return true
        },
        function() {
            check(widgetWindow.visible && widget.Window.window === widgetWindow, "widget has an offscreen QQuickWindow scene")
            widget.settings = { showCountdown: true }
            widget.visible = true
            countdownStarted = Date.now()
            countdownStartSeconds = widget.secondsRemaining
            check(widget.ticking && countdownStartSeconds > 0, "visible countdown timer enabled")
            return true
        },
        function() {
            if (service.busy) return false
            if (countdownSerial < 0) countdownSerial = service.serial
            if (Date.now() - countdownStarted < 2300) return false
            var delta = countdownStartSeconds - widget.secondsRemaining
            check(delta >= 1 && delta <= 3, "local countdown tracks elapsed seconds: " + delta)
            check(service.serial === countdownSerial, "countdown does not poll the daemon")
            widget.settings = { showCountdown: false }
            widget.visible = false
            check(!widget.ticking, "hidden countdown timer disabled")
            return true
        },
        function() {
            panel.service = applyingService
            var pauseButton = find(panel, function(o) { return o.text === "Pause" }, [])
            var nextButton = find(panel, function(o) { return o.text === "Next" }, [])
            check(pauseButton !== null && pauseButton.enabled, "Pause stays enabled while applying")
            check(nextButton !== null && !nextButton.enabled, "Next blocked while applying")
            pauseButton.clicked(); check(applyingService.pauses === 1, "Pause dispatched to fake service only")
            panel.service = service
            check(panel.draft.baseRevision === service.revision, "real service restored")
            return true
        },
        function() {
            panel.service = applyingService
            widget.service = applyingService
            widget.settings = { showCountdown: true }
            widget.visible = true
            applyingService.status = {applying: false, paused: false, protective_suspension: "setter failed"}
            check(panel.resumeOffered && panel.stateText.indexOf("Protectively suspended") >= 0, "protective false-paused status")
            check(widget.stateText.indexOf("Protectively suspended") >= 0 && !widget.ticking && widget.countdownText === "—", "bar protective suspension suppresses stale countdown")
            panel.open(); panel.close(); widget.open(); widget.close()
            return true
        },
        function() {
            check(applyingService.resumes === 0, "status and opening never auto resume")
            var resumeButton = find(panel, function(o) { return o.text === "Resume" }, [])
            check(resumeButton !== null && resumeButton.enabled, "protective paused=false deliberate Resume enabled")
            resumeButton.clicked()
            check(applyingService.resumes === 1 && applyingService.pauses === 1, "protective paused=false dispatches Resume, not Pause")
            check(panel.resumeOffered, "Resume not optimistic")
            applyingService.status = {applying: false, paused: true, protective_suspension: "setter failed"}
            check(panel.stateText.indexOf("Explicitly paused") >= 0 && widget.stateText.indexOf("Explicitly paused") >= 0, "combined protective and explicit pause distinguished")
            panel.open(); panel.close()
            return true
        },
        function() {
            check(applyingService.resumes === 1, "paused=true protective status never auto resumes")
            var resumeButton = find(panel, function(o) { return o.text === "Resume" }, [])
            check(resumeButton !== null && resumeButton.enabled, "protective paused=true Resume enabled")
            resumeButton.clicked()
            check(applyingService.resumes === 2, "protective paused=true deliberate recovery")
            applyingService.status = {applying: false, paused: true, protective_suspension: ""}
            check(panel.resumeOffered && panel.stateText === "Explicitly paused" && widget.stateText === "Explicitly paused", "explicit-only pause distinguished")
            applyingService.status = {applying: false, paused: false, protective_suspension: ""}
            check(!panel.resumeOffered && widget.ticking, "authoritative recovery restores rotation UI")
            return true
        },
        function() {
            check(applyingService.resumes === 2, "recovery snapshot never auto resumes")
            panel.service = service
            widget.service = service
            widget.settings = { showCountdown: false }
            widget.visible = false
            return true
        },
        function() {
            for (var i = 0; i < 5; ++i) { widget.open(); check(widget.opened, "widget panel loaded/open"); widget.close(); check(!widget.opened, "widget close") }
            panel.open(); check(panel.opened, "panel open"); panel.close()
            picker.cancel(); check(!picker.active, "unopened external picker cancellation lifecycle")
            return true
        },
        function() { if (service.busy) return false; pending = service.next(); return true },
        function() { return ok() },
        function() { pending = service.next(); return true },
        function() { return ok() },
        function() { pending = service.previous(); return true },
        function() { return ok() },
        function() { pending = service.pause(); return true },
        function() { if (!ok() || !service.status.paused) return false; pending = service.resume(); return true },
        function() { return ok() && !service.status.paused },
        function() {
            var interval = find(panel, function(o) { return o.label === "Interval (seconds)" }, [])
            check(interval !== null, "real NumberField instantiated")
            interval.modified(0); panel.save(); check(panel.saveRequestId === "", "invalid draft must not save")
            panel.revert(); interval.modified(301)
            var saveButton = find(panel, function(o) { return o.text === "Save" }, [])
            check(saveButton !== null && saveButton.enabled, "Save control enabled for valid dirty draft")
            saveButton.clicked(); check(panel.saveRequestId !== "", "valid save dispatched")
            check(panel.draft.dirty, "save is not optimistic")
            return true
        },
        function() {
            if (panel.saveRequestId !== "" || service.busy) return false
            check(panel.inlineError === "", panel.inlineError)
            check(!panel.draft.dirty && service.settings.interval_seconds === 301, "authoritative save")
            pending = service.updateSettings({interval_seconds: 302, selection: "shuffle", folders: service.settings.folders}, "stale-revision")
            return true
        },
        function() {
            if (!response()) return false
            check(!response().ok && response().code === "revision_conflict", "stale revision must fail")
            panel.addFolderUrl(Quickshell.env("TEST_FOLDER_URL"))
            check(panel.validationRequestId !== "", "folder URL validation request")
            return true
        },
        function() {
            if (panel.validationRequestId !== "" || service.busy) return false
            check(panel.inlineError === "", panel.inlineError)
            check(panel.draft.folders.length === 2, "validated URL added to draft")
            var addedIndex = panel.draft.folders.indexOf(Quickshell.env("TEST_EXTRA_FOLDER"))
            check(addedIndex >= 0, "strict URL decoded path: " + JSON.stringify(panel.draft.folders))
            selectMode("shuffle"); clickSave()
            return true
        },
        function() {
            if (!savedRoots(2, 4, "shuffle")) return false
            check(service.settings.folders.indexOf(Quickshell.env("TEST_EXTRA_FOLDER")) >= 0, "snapshot Unicode path matches exactly")
            selectMode("sequential"); clickSave()
            return true
        },
        function() {
            if (!savedRoots(2, 4, "sequential")) return false
            var addedIndex = panel.draft.folders.indexOf(Quickshell.env("TEST_EXTRA_FOLDER"))
            panel.requestRemove(addedIndex)
            confirmation = find(panel, function(o) { return o.confirmText === "Remove folder" }, [])
            check(confirmation !== null && confirmation.opened, "remove confirmation exists")
            confirmation.canceled(); check(panel.draft.folders.length === 2, "cancel preserves draft")
            panel.requestRemove(addedIndex); confirmation.confirmed()
            check(panel.draft.folders.length === 1, "confirmed remove")
            panel.requestRemove(0); check(!confirmation.opened, "last folder protected")
            clickSave()
            return true
        },
        function() {
            if (!savedRoots(1, 3, "sequential")) return false
            panel.draft.setInterval(999); panel.revert()
            check(!panel.draft.dirty && panel.draft.intervalSeconds === 301, "Revert restores saved state")
            panel.validateFolders(["file://remote/tmp/not-local"], false)
            return true
        },
        function() {
            if (panel.validationRequestId !== "" || service.busy) return false
            check(panel.inlineError !== "", "invalid folder URL surfaced")
            panel.revert(); widget.close(); panel.close()
            idleSerial = service.serial; return true
        },
        function() {
            if (++idleTicks < 25) return false
            check(service.serial === idleSerial, "idle must not poll or query")
            console.log("SWIFTWALL_CONTROL stop")
            return true
        },
        function() {
            if (service.online) return false
            pending = service.next(); return true
        },
        function() {
            if (!response()) return false
            check(!response().ok, "offline command rejected")
            console.log("SWIFTWALL_CONTROL restart")
            return true
        },
        function() {
            if (!service.online || service.busy || service.instance === oldInstance) return false
            check(service.settings.interval_seconds === 301, "restart persisted settings")
            service.enabled = false
            service.socketPath = Quickshell.env("TEST_LEGACY_SOCKET")
            service.commandTimeout.interval = 3000
            service.enabled = true
            return true
        },
        function() {
            if (service.connectionState !== "unsupported") return false
            check(!service.online && !service.compatible, "v0.1 fails closed")
            pending = service.next()
            idleSerial = service.serial; idleTicks = 0
            return true
        },
        function() {
            if (!response()) return false
            check(!response().ok && response().code === "unavailable", "v0.1 mutation blocked locally")
            if (++idleTicks < 10) return false
            check(service.serial === idleSerial, "unsupported protocol must not reconnect or poll")
            service.enabled = false
            service.socketPath = Quickshell.env("TEST_FRAGMENT_SOCKET")
            service.enabled = true
            return true
        },
        function() {
            if (!service.online || service.busy) return false
            check(service.instance === "fragment-café 雪 😀", "fragmented Unicode instance")
            check(service.settings.folders[0] === Quickshell.env("TEST_EXTRA_FOLDER"), "fragmented ASCII JSON decodes Unicode path")
            check(service.status.observed_wallpaper === Quickshell.env("TEST_UNICODE_IMAGE"), "fragmented Unicode wallpaper")
            check(service.status.warnings[0] === 'café 雪 😀 # % "quoted" \\ slash\nline', "fragmented complex Unicode warning")
            service.enabled = false
            service.socketPath = Quickshell.env("TEST_STALL_SOCKET")
            service.commandTimeout.interval = 200
            service.enabled = true
            return true
        },
        function() {
            if (service.error.indexOf("timed out") < 0) return false
            check(!service.online, "timeout fails closed")
            service.enabled = false
            console.log("SWIFTWALL_RUNTIME_PASS")
            Qt.quit()
            return true
        }
    ]
    Timer {
        interval: 100; running: true; repeat: true
        onTriggered: {
            try {
                root.elapsed += interval
                root.check(root.elapsed < 15000, "step " + root.index + " timed out: " + service.error)
                if (root.steps[root.index]()) { console.log("SWIFTWALL_STEP " + root.index); root.index++; root.elapsed = 0 }
            } catch (error) {
                console.error("SWIFTWALL_RUNTIME_FAIL step " + root.index + ": " + error)
                stop(); Qt.quit()
            }
        }
    }
}
