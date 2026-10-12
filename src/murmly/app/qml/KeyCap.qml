import QtQuick

// One key of a combination, in mono, with the raised bottom edge of a key.
// `large` is the size first run draws the chosen keys at.
Rectangle {
    property string text: ""
    property bool large: false

    implicitWidth: Math.max(large ? 64 : text.length > 1 ? 52 : 40,
        label.implicitWidth + theme.space.space14 * (large ? 1.3 : 2))
    implicitHeight: large ? 58 : 40
    radius: large ? theme.radii.r10 : theme.radii.r9
    color: theme.colors.panelRaised
    border.width: 1
    border.color: theme.colors.borderStrong

    Rectangle {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.leftMargin: parent.radius * 0.6
        anchors.rightMargin: parent.radius * 0.6
        height: large ? 3 : 2
        color: theme.colors.borderStrong
    }

    AppText {
        id: label
        anchors.centerIn: parent
        role: "monoFile"
        text: parent.text
        font.pixelSize: large ? 18 : 13
        wrapMode: Text.NoWrap
        color: theme.colors.textBright
    }
}
