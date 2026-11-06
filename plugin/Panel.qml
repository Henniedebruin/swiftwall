pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as QQC
import qs.Commons
import qs.Ui as Ui

Ui.Panel {
    id: root
    moduleName: "io.github.henniedebruin.swiftwall"
    manageIpc: false

    property var anchorItem: null
    property var hostWidget: null
    property var service: null
    property alias draft: draftModel
    property string inlineError: ""
    property string validationSummary: ""
    property string saveRequestId: ""
    property double saveSnapshotGeneration: -1
    property string validationRequestId: ""
    property bool validationAddsFolder: false
    property bool pickerActive: false
    property string removePath: ""
    readonly property bool editable: service !== null && service.online && service.compatible
                                      && service.settings !== null && !service.busy && !pickerActive
                                      && !service.status.config_pending && !service.status.config_update_pending
    readonly property bool commandEnabled: editable && !service.status.applying
    readonly property bool protectivelySuspended: service !== null && !!service.status.protective_suspension
    readonly property bool explicitlyPaused: service !== null && service.status.paused === true
    readonly property bool resumeOffered: explicitlyPaused || protectivelySuspended
    readonly property string stateText: !service ? "Service unavailable" : !service.online
        ? (service.error || "Swiftwall is offline") : protectivelySuspended
        ? "Protectively suspended" + (explicitlyPaused ? " · Explicitly paused" : "")
          + ": " + String(service.status.protective_suspension) + " · Resume manually when ready"
        : explicitlyPaused ? "Explicitly paused" : String(service.status.state || "Connected")

    function open() {
        if (pickerActive) return
        root.controller.show()
        if (service && service.online) service.requestStatus()
    }
    function close() { root.controller.hide() }
    function toggle() { if (opened) close(); else open() }
    function switchPanel(direction) {
        return bar && typeof bar.switchPanelFrom === "function"
            ? bar.switchPanelFrom(hostWidget || root, direction) : false
    }
    function syncDraft() {
        if (service && service.settings) draftModel.ingest(service.settings, service.revision)
    }
    function save() {
        if (!editable || !draftModel.dirty || !draftModel.valid || saveRequestId !== "") return
        inlineError = ""
        saveSnapshotGeneration = draftModel.snapshotGeneration
        saveRequestId = service.updateSettings(draftModel.value(), draftModel.baseRevision)
    }
    function revert() {
        if (saveRequestId !== "" || validationRequestId !== "") return
        syncDraft()
        draftModel.revert()
        inlineError = ""
        validationSummary = ""
    }
    function validateFolders(folders, adding) {
        if (!editable || validationRequestId !== "") {
            if (adding) inlineError = "Swiftwall is busy or unavailable; folder was not added. Try again."
            return
        }
        inlineError = ""
        validationSummary = ""
        validationAddsFolder = adding
        if (adding) draftModel.beginEdit()
        validationRequestId = service.validateFolders(folders)
    }
    function chooseFolder() {
        if (!editable) return
        inlineError = ""
        pickerActive = true
        close()
        // Closing releases KeyboardPanel's input ownership immediately. Do not
        // unload/recreate the picker or draft when the panel surface disappears.
        Qt.callLater(function() { if (root.pickerActive) folderPicker.open() })
    }
    function addFolderUrl(url) {
        if (!service || !service.online) { inlineError = "Swiftwall disconnected; folder was not added"; return }
        validateFolders(draftModel.folders.concat([url]), true)
    }
    function requestRemove(index) {
        if (!editable || draftModel.folders.length <= 1) return
        removePath = draftModel.folders[index]
        confirmation.selectedIndex = 0
        confirmation.opened = true
        keyCatcher.forceActiveFocus()
    }
    function restoreFocus() { Qt.callLater(function() { if (root.opened) keyCatcher.forceActiveFocus() }) }
    onServiceChanged: syncDraft()

    SettingsDraft { id: draftModel }
    FolderPicker {
        id: folderPicker
        onFolderChosen: url => {
            // The URL goes unchanged to daemon validation (strict file:// decoding).
            root.pickerActive = false
            root.addFolderUrl(url)
        }
        onFailed: message => { root.inlineError = message }
        onDismissed: {
            root.pickerActive = false
            // Let the external portal window unmap before reacquiring focus.
            Qt.callLater(function() { root.open(); root.restoreFocus() })
        }
    }
    Connections {
        target: root.service
        function onSnapshotReceived(snapshot) { draftModel.ingest(snapshot.settings, snapshot.revision) }
        function onRequestFinished(id, op, reply) {
            if (id === root.saveRequestId) {
                root.saveRequestId = ""
                if (reply.ok) {
                    draftModel.committed(reply, root.saveSnapshotGeneration)
                    root.inlineError = reply.config_durability
                        ? "Saved, but durability is uncertain: " + reply.config_durability : ""
                } else root.inlineError = (reply.code ? reply.code + ": " : "") + (reply.error || "Save failed")
                root.saveSnapshotGeneration = -1
            } else if (id === root.validationRequestId) {
                root.validationRequestId = ""
                if (!reply.ok) {
                    root.inlineError = (reply.code ? reply.code + ": " : "") + (reply.error || "Folder validation failed")
                    return
                }
                let entries = reply.folders || []
                let paths = []
                let messages = []
                for (let i = 0; i < entries.length; ++i) {
                    let folder = entries[i]
                    paths.push(folder.path)
                    messages.push(folder.path + ": " + folder.eligible_images + " eligible / "
                                  + folder.candidate_images + " candidates" + (folder.available ? "" : " (unavailable)"))
                    if (folder.warnings) for (let j = 0; j < folder.warnings.length; ++j) messages.push(String(folder.warnings[j]))
                }
                if (root.validationAddsFolder && paths.length > 0) draftModel.setFolders(paths)
                root.validationSummary = messages.join("\n")
            } else if (!reply.ok && op !== "hello") root.inlineError = reply.error || "Request failed"
        }
    }

    Ui.KeyboardPanel {
        id: popup
        anchorItem: root.anchorItem
        bar: root.bar
        owner: root.hostWidget || root
        open: root.opened
        focusTarget: keyCatcher
        contentWidth: fittedContentWidth(Style.space(470))
        contentHeight: fittedContentHeight(panelContent.implicitHeight, Style.space(720))

        Ui.PanelKeyCatcher {
            id: keyCatcher
            anchors.fill: parent
            // Replace the cursor dispatcher's key policy for this form: native
            // controls retain arrows, text entry and Tab; Escape bubbles here.
            // Confirmation temporarily takes every key from the focused form.
            Keys.onPressed: event => {
                if (confirmation.opened) {
                    if (confirmation.handleKey(event)) event.accepted = true
                } else if (event.key === Qt.Key_Escape) {
                    root.close()
                    event.accepted = true
                }
            }

            Flickable {
                id: scroll
                anchors.fill: parent
                contentWidth: width
                contentHeight: panelContent.implicitHeight
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                QQC.ScrollBar.vertical: QQC.ScrollBar {}

                Column {
                    id: panelContent
                    width: scroll.width
                    spacing: Style.spacing.md
                    enabled: !confirmation.opened

                    Text {
                        text: "SwiftWall"
                        textFormat: Text.PlainText
                        color: Color.foreground
                        font.family: Style.font.family
                        font.pixelSize: Style.font.body * 1.3
                    }
                    Text {
                        width: parent.width
                        text: root.stateText + (root.hostWidget && root.service && root.service.online
                                               ? " · " + root.hostWidget.countdownText : "")
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                        color: Color.foreground
                        font.family: Style.font.family
                        font.pixelSize: Style.font.body
                    }
                    Text {
                        width: parent.width
                        visible: root.service !== null && root.service.online
                        text: root.service ? String(root.service.status.observed_wallpaper || "No observed wallpaper") : ""
                        textFormat: Text.PlainText
                        elide: Text.ElideMiddle
                        color: Color.foreground
                        font.family: Style.font.family
                        font.pixelSize: Style.font.bodySmall
                    }
                    Row {
                        spacing: Style.spacing.md
                        Ui.Button {
                                                    text: "Previous"
                                                    focusable: true
                                                    enabled: root.commandEnabled
                                                    Accessible.role: Accessible.Button
                                                    Accessible.name: "Previous wallpaper"
                                                    Accessible.onPressAction: if (enabled) clicked()
                                                    onClicked: root.service.previous()
                                                }
                        Ui.Button {
                            text: root.resumeOffered ? "Resume" : "Pause"
                            focusable: true
                            enabled: root.editable
                            Accessible.role: Accessible.Button
                            Accessible.name: text + " wallpaper rotation"
                            Accessible.onPressAction: if (enabled) clicked()
                            onClicked: { if (root.resumeOffered) root.service.resume(); else root.service.pause() }
                        }
                        Ui.Button {
                                                    text: "Next"
                                                    focusable: true
                                                    enabled: root.commandEnabled
                                                    Accessible.role: Accessible.Button
                                                    Accessible.name: "Next wallpaper"
                                                    Accessible.onPressAction: if (enabled) clicked()
                                                    onClicked: root.service.next()
                                                }
                    }
                    Text {
                        width: parent.width
                        visible: text !== ""
                        text: root.service ? [root.service.status.error, root.service.status.no_schedule_reason,
                            root.service.status.last_persistence_error,
                            root.service.status.config_worker_error,
                            root.service.status.config_durability,
                            (root.service.status.warnings || []).join("\n")].filter(function(v) { return !!v }).join("\n") : ""
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                        color: Color.foreground
                        font.family: Style.font.family
                        font.pixelSize: Style.font.bodySmall
                    }
                    Ui.NumberField {
                        id: intervalField
                        label: "Interval (seconds)"
                        value: draftModel.intervalSeconds
                        from: 1
                        to: 31536000
                        enabled: root.editable
                        onModified: value => draftModel.setInterval(value)
                    }
                    Ui.ButtonGroup {
                        options: [{ value: "shuffle", label: "Shuffle" }, { value: "sequential", label: "Sequential" }]
                        value: draftModel.selection
                        enabled: root.editable
                        onChanged: value => draftModel.setSelection(value)
                    }
                    Text {
                        text: "Wallpaper folders"
                        textFormat: Text.PlainText
                        color: Color.foreground
                        font.family: Style.font.family
                        font.pixelSize: Style.font.body
                    }
                    Repeater {
                        model: draftModel.folders
                        delegate: Row {
                            id: folderRow
                            required property string modelData
                            required property int index
                            width: panelContent.width
                            spacing: Style.spacing.md
                            Text {
                                width: Math.max(0, folderRow.width - removeButton.implicitWidth - folderRow.spacing)
                                anchors.verticalCenter: parent.verticalCenter
                                text: folderRow.modelData
                                textFormat: Text.PlainText
                                elide: Text.ElideMiddle
                                color: Color.foreground
                                font.family: Style.font.family
                                font.pixelSize: Style.font.bodySmall
                                MouseArea {
                                    id: folderHover
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    acceptedButtons: Qt.NoButton
                                    Ui.PanelToolTip { visible: folderHover.containsMouse; text: folderRow.modelData }
                                }
                            }
                            Ui.Button {
                                id: removeButton
                                text: "Remove"
                                tooltipText: draftModel.folders.length <= 1 ? "Keep at least one folder" : "Remove from settings; never deletes images"
                                focusable: true
                                enabled: root.editable && draftModel.folders.length > 1
                                onClicked: root.requestRemove(folderRow.index)
                            }
                        }
                    }
                    Row {
                        spacing: Style.spacing.md
                        Ui.Button { text: "Add folder…"; focusable: true; enabled: root.editable; onClicked: root.chooseFolder() }
                        Ui.Button {
                            text: "Validate"
                            focusable: true
                            enabled: root.editable && draftModel.folders.length > 0
                            onClicked: root.validateFolders(draftModel.folders, false)
                        }
                    }
                    Text {
                        width: parent.width
                        text: root.validationSummary
                        visible: text !== ""
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                        color: Color.foreground
                        font.family: Style.font.family
                        font.pixelSize: Style.font.bodySmall
                    }
                    Text {
                        width: parent.width
                        text: draftModel.stale ? "Settings changed elsewhere. Saving uses the original revision and may conflict. Revert to load the latest snapshot." : ""
                        visible: text !== ""
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                        color: Color.accent
                        font.family: Style.font.family
                        font.pixelSize: Style.font.bodySmall
                    }
                    Text {
                        width: parent.width
                        text: root.inlineError || (root.service ? root.service.error : "")
                        visible: text !== ""
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                        color: Color.accent
                        font.family: Style.font.family
                        font.pixelSize: Style.font.bodySmall
                    }
                    Row {
                        spacing: Style.spacing.md
                        Ui.Button {
                            text: root.saveRequestId !== "" ? "Saving…" : "Save"
                            focusable: true
                            enabled: root.editable && draftModel.dirty && draftModel.valid && root.saveRequestId === ""
                            onClicked: root.save()
                        }
                        Ui.Button {
                            text: "Revert"
                            focusable: true
                            enabled: draftModel.snapshotSettings !== null && root.saveRequestId === "" && root.validationRequestId === ""
                            onClicked: root.revert()
                        }
                        Ui.Button {
                            text: "Reconnect"
                            focusable: true
                            visible: root.service !== null && !root.service.online
                            enabled: root.service !== null && !root.service.busy
                            onClicked: root.service.reconnect()
                        }
                    }
                    Ui.Button {
                        text: "Bar countdown"
                        selected: root.hostWidget ? root.hostWidget.showCountdown : false
                        focusable: true
                        enabled: root.hostWidget !== null
                        onClicked: root.hostWidget.setShowCountdown(!root.hostWidget.showCountdown)
                    }
                }
            }
            Ui.ConfirmDialog {
                id: confirmation
                anchors.fill: parent
                message: "Remove this folder from the draft? No images will be deleted."
                confirmText: "Remove folder"
                onCanceled: { opened = false; root.removePath = ""; root.restoreFocus() }
                onConfirmed: {
                    if (root.editable) draftModel.removeFolder(draftModel.folders.indexOf(root.removePath))
                    opened = false
                    root.removePath = ""
                    root.restoreFocus()
                }
            }
        }
    }
}
