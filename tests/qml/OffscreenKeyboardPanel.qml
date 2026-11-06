import QtQuick

// Only the staged surface is substituted: Quickshell 0.3 has no offscreen
// PanelWindow backend. This is NOT a compositor/window lifecycle test.
Item {
    required property Item anchorItem
    required property QtObject bar
    property var owner: null
    property bool open: false
    property Item focusTarget: null
    property int contentWidth: 470
    property int contentHeight: 720
    width: contentWidth
    height: contentHeight
    function fittedContentWidth(value) { return value }
    function fittedContentHeight(value, maximum) { return Math.min(value, maximum) }
}
