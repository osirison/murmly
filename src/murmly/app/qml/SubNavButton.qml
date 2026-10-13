import QtQuick
import QtQuick.Controls

// One entry in Settings' own list of groups. Left-aligned, drawn raised when it
// is the group being shown. Tab reaches it; Space and Enter operate it.
AbstractButton {
    id: button

    property bool selected: false

    implicitWidth: label.implicitWidth + theme.space.space10 * 2
    implicitHeight: label.implicitHeight + 14
    focusPolicy: Qt.StrongFocus
    Accessible.role: Accessible.Button
    Accessible.name: text
    Accessible.checkable: true
    Accessible.checked: selected
    Accessible.focusable: true

    Keys.onReturnPressed: click()
    Keys.onEnterPressed: click()

    background: Rectangle {
        radius: theme.radii.r7
        color: button.selected || button.hovered ? theme.colors.panelRaised : "transparent"

        Rectangle {
            visible: button.visualFocus
            anchors.fill: parent
            anchors.margins: -2
            radius: parent.radius + 2
            color: "transparent"
            border.width: 2
            border.color: theme.colors.focusRing
        }
    }

    contentItem: AppText {
        id: label
        role: "button"
        text: button.text
        leftPadding: theme.space.space10
        verticalAlignment: Text.AlignVCenter
        wrapMode: Text.NoWrap
        color: button.selected ? theme.colors.text : theme.colors.text3
    }
}
