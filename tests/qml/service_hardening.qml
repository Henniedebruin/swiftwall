import QtQuick
import QtTest
import Quickshell

// Loaded only with disposable Quickshell/Io stubs. No bus or sockets are used.
TestCase {
    id: host
    name: "ServiceHardening"
    property int checks: 0
    property bool inSocketCallback: false
    property bool synchronousTeardown: false
    property int disconnectEvents: 0
    SharedService { id: service; enabled: false }
    Component {
        id: fakeSocket
        QtObject {
            property bool eventStream: false
            property string path: ""
            property bool connected: false
            property bool retired: false
            onConnectedChanged: if (!connected) {
                host.disconnectEvents++
                if (host.inSocketCallback) host.synchronousTeardown = true
            }
        }
    }
    function check(value, message) {
        if (!value) throw new Error(message)
        checks++
    }
    function snapshot(instance, seq, seconds) {
        service.receive({retired: false, eventStream: true, buffer: "", bufferBytes: 0}, JSON.stringify({
            v: 1, protocol_version: 1, daemon_version: "0.2.0-dev", capabilities: ["settings", "subscribe", "sequential"],
            event: "snapshot", instance: instance, seq: seq, revision: instance + ":1",
            settings: {interval_seconds: 300, selection: "shuffle", folders: ["/private/walls"]},
            status: {next_change_in_seconds: seconds}
        }) + "\n")
    }
    function dispatch() {
        service.requestStatus()
        check(service.activeRequest && service.activeRequest.countdownEpoch === service.snapshotEpoch,
              "status must capture authority at dispatch")
    }
    function reply(seconds) { service.finish({ok: true, status: {next_change_in_seconds: seconds}}) }
    function test_hardening() {
        check(service.socketPath === Quickshell.env("TEST_EXPECTED_SOCKET"), "environment precedence")
        check(service.runtimeSocket("", "") === "", "missing runtime must not produce /control.sock")
        check(service.runtimeSocket("/private/explicit", "/private/xdg") === "/private/explicit/control.sock", "explicit precedence")
        check(service.runtimeSocket("", "/private/xdg") === "/private/xdg/swiftwall/control.sock", "XDG fallback")
        check(service.runtimeSocket("relative", "/valid") === "relative/control.sock", "invalid override must not fall back")
        service.ready = false
        service.enabled = true
        for (const path of ["", "relative/control.sock", "/control.sock", "/swiftwall/control.sock", "/a/../b/control.sock",
                           "/a/./control.sock", "/bad\n/control.sock", "/bad\u0000/control.sock", "/" + "x".repeat(107)]) {
            check(service.socketPathError(path) !== "", "reject invalid runtime path")
            service.socketPath = path
            service.reconnect()
            check(service.commandSocket === null && service.eventsSocket === null && service.serial === 0,
                  "invalid runtime must never create a socket or request")
            check(service.error.indexOf("runtime") >= 0 || service.error.indexOf("socket") >= 0, "actionable runtime diagnostic")
        }
        check(service.socketPathError("/private/run/control.sock") === "", "valid runtime")
        service.ready = false
        service.enabled = true
        service.socketPath = "/private/unused/control.sock"
        service.socketComponent = fakeSocket
        service.online = true
        service.compatible = true
        snapshot("first", 1, 300)
        dispatch()
        snapshot("first", 2, 0)
        const newer = service.countdownDeadline
        reply(900)
        check(service.countdownDeadline === newer, "late status must not overwrite newer seq")
        dispatch()
        snapshot("restarted", 0, null)
        reply(900)
        check(service.instance === "restarted" && service.seq === 0 && service.countdownDeadline === 0,
              "old instance response must not replace restart snapshot")
        dispatch()
        reply(120)
        check(service.countdownDeadline >= Date.now() + 119000 && service.countdownDeadline <= Date.now() + 121000,
              "unchanged snapshot permits countdown refresh")
        dispatch()
        snapshot("restarted", 1, null)
        reply(500)
        check(service.countdownDeadline === 0, "new suspension clears stale countdown")
        service.offline("test disconnect", false, false)
        check(service.countdownDeadline === 0 && !service.online, "disconnect clears countdown")
        wait(0)
        const disconnectBaseline = disconnectEvents

        const retiring = fakeSocket.createObject(service)
        retiring.connected = true
        service.commandSocket = retiring
        service.error = "retirement sentinel"
        inSocketCallback = true
        service.disposeCommand()
        check(service.commandSocket === null && retiring.retired, "retirement detaches command socket immediately")
        check(retiring.connected && !synchronousTeardown,
              "retirement must not disconnect or destroy inside the socket callback stack")
        service.receive(retiring, "not json\n")
        check(service.error === "retirement sentinel", "retired socket must ignore stale data immediately")
        inSocketCallback = false
        tryCompare(host, "disconnectEvents", disconnectBaseline + 1)
        check(!synchronousTeardown, "deferred command teardown must stay outside the callback stack")

        const eventBaseline = disconnectEvents
        const retiringEvents = fakeSocket.createObject(service)
        retiringEvents.connected = true
        service.eventsSocket = retiringEvents
        service.online = true
        inSocketCallback = true
        service.offline("event retirement sentinel", false, false)
        check(service.eventsSocket === null && retiringEvents.retired,
              "offline must detach event socket immediately")
        check(retiringEvents.connected && !synchronousTeardown,
              "offline must not disconnect or destroy inside the socket callback stack")
        service.receive(retiringEvents, "not json\n")
        check(service.error === "event retirement sentinel", "retired event socket must ignore stale data immediately")
        inSocketCallback = false
        tryCompare(host, "disconnectEvents", eventBaseline + 1)
        check(!synchronousTeardown, "deferred event teardown must stay outside the callback stack")

        // Deletion may win the race with the deferred teardown; the callback
        // must tolerate the captured QML object becoming null.
        const alreadyDeleting = fakeSocket.createObject(service)
        alreadyDeleting.connected = true
        alreadyDeleting.destroy()
        service.retireSocket(alreadyDeleting)
        wait(0)
        console.log("HARDENING_PASS " + checks)
    }
}
