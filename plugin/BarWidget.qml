import QtQuick

import qs.Ui as Ui

Ui.BarWidget {
    id: root
    moduleName: "io.github.henniedebruin.swiftwall"

    property var service: bar && bar.shell ? bar.shell.serviceFor(moduleName) : null
    readonly property bool showCountdown: setting("showCountdown", false) === true
    readonly property bool opened: panelLoader.item ? panelLoader.item.opened : false
    readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing : false
    readonly property bool paused: service ? service.status.paused === true : false
    readonly property bool protectivelySuspended: service ? !!service.status.protective_suspension : false
    readonly property string stateText: !service ? "Service unavailable" : !service.online
        ? (service.error || "Swiftwall is offline") : protectivelySuspended
        ? "Protectively suspended" + (paused ? " · Explicitly paused" : "")
          + ": " + String(service.status.protective_suspension) + " · Resume manually when ready"
        : paused ? "Explicitly paused" : String(service.status.state || "Connected")
    readonly property bool countdownVisible: visible && (showCountdown || opened)
    readonly property bool ticking: countdownVisible && service !== null && service.online
                                   && !paused && !protectivelySuspended && service.countdownDeadline > 0
    property double now: Date.now()
    readonly property int secondsRemaining: service && service.countdownDeadline > 0
        ? Math.max(0, Math.ceil((service.countdownDeadline - now) / 1000)) : 0
    readonly property string countdownText: service && !paused && !protectivelySuspended && service.countdownDeadline > 0
        ? Math.floor(secondsRemaining / 60) + ":" + (secondsRemaining % 60 < 10 ? "0" : "") + secondsRemaining % 60 : "—"

    function open() { if (panelLoader.item) panelLoader.item.open() }
    function close() { if (panelLoader.item) panelLoader.item.close() }
    function toggle() { if (panelLoader.item) panelLoader.item.toggle() }
    function closeForPopoutSwitch() { if (panelLoader.item) panelLoader.item.closeForPopoutSwitch() }
    function resync() {
        now = Date.now()
        if (countdownVisible && service && service.online) service.requestStatus()
    }
    function setShowCountdown(value) {
        var entry = { id: moduleName }
        for (var key in settings) if (key !== "id") entry[key] = settings[key]
        entry.showCountdown = value
        settings = entry
        if (bar && bar.shell && typeof bar.shell.updateEntryInline === "function")
            bar.shell.updateEntryInline(moduleName, entry)
    }
    function injectPanel() {
        var target = panelLoader.item
        if (!target) return
        target.bar = bar
        target.anchorItem = button
        target.hostWidget = root
        target.service = service
        target.settings = settings
    }
    onBarChanged: injectPanel()
    onServiceChanged: injectPanel()
    onSettingsChanged: injectPanel()
    onCountdownVisibleChanged: if (countdownVisible) resync()
    onTickingChanged: now = Date.now()

    implicitWidth: button.implicitWidth
    implicitHeight: button.implicitHeight

    Timer {
        interval: 1000
        repeat: true
        running: root.ticking
        onTriggered: {
            var current = Date.now()
            if (Math.abs(current - root.now - 1000) > 5000 && root.service) root.service.requestStatus()
            root.now = current
        }
    }
    Connections {
        target: root.service
        function onCountdownDeadlineChanged() { root.now = Date.now() }
    }
    Loader {
        id: panelLoader
        active: true
        source: Qt.resolvedUrl("Panel.qml")
        visible: false
        onLoaded: { root.injectPanel(); Qt.callLater(root.injectPanel) }
    }
    Ui.WidgetButton {
        id: button
        anchors.fill: parent
        bar: root.bar
        tooltipText: {
            if (!root.service) return "Swiftwall · Service unavailable"
            if (!root.service.online) return "Swiftwall · " + (root.service.error || root.service.connectionState)
            var lines = ["Swiftwall · " + root.stateText]
            if (!root.paused && !root.protectivelySuspended && root.countdownVisible && root.service.countdownDeadline > 0)
                lines.push("Next wallpaper in " + root.countdownText)
            else if (root.service.status.no_schedule_reason)
                lines.push(String(root.service.status.no_schedule_reason))
            if (root.service.error) lines.push(root.service.error)
            if (root.service.status.error) lines.push(String(root.service.status.error))
            return lines.join("\n")
        }
        Accessible.role: Accessible.Button
        Accessible.name: "Swiftwall wallpaper controls"
        Accessible.description: tooltipText
        Accessible.onPressAction: root.toggle()
        text: "󰸉 " + (!root.service || !root.service.online ? "󰅙" : root.paused || root.protectivelySuspended ? "󰏤" : "󰐊")
              + (root.showCountdown && !root.vertical && root.service && root.service.online ? " " + root.countdownText : "")
        onPressed: b => { if (b === Qt.LeftButton) root.toggle() }
    }
}
