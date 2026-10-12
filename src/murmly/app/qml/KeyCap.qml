import QtQuick

// One key of a combination, in mono, with the raised bottom edge of a key.
Rectangle {
    property string text: ""

    implicitWidth: Math.max(text.length > 1 ? 52 : 40, label.implicitWidth + theme.space.space14 * 2)
    implicitHeight: 40
    radius: theme.radii.r9
    color: theme.colors.panelRaised
    border.width: 1
    border.color: theme.colors.borderStrong

    Rectangle {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.leftMargin: parent.radius * 0.6
        anchors.rightMargin: parent.radius * 0.6
        height: 2
        color: theme.colors.borderStrong
    }

    AppText {
        id: label
        anchors.centerIn: parent
        role: "monoFile"
        text: parent.text
        font.pixelSize: 13
        wrapMode: Text.NoWrap
        color: theme.colors.textBright
    }
}
