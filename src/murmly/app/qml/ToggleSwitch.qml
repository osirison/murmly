import QtQuick
import QtQuick.Controls

// An on/off switch. Its state is only `on`, which the page binds to the value in
// the file: a click reports the wish (`clicked`) and changes nothing itself, so a
// refused change leaves the switch where the file says it is.
AbstractButton {
    id: control

    property bool on: false

    implicitWidth: 44
    implicitHeight: 26
    focusPolicy: Qt.StrongFocus
    Accessible.role: Accessible.CheckBox
    Accessible.name: text
    Accessible.checkable: true
    Accessible.checked: on
    Accessible.focusable: true

    Keys.onReturnPressed: click()
    Keys.onEnterPressed: click()

    background: Rectangle {
        radius: theme.radii.rPill
        opacity: control.enabled ? 1 : 0.55
        color: control.on ? theme.colors.btnPrimaryBg : theme.colors.panelAlt
        border.width: control.on ? 0 : 1
        border.color: theme.colors.borderStrong

        Rectangle {
            visible: control.visualFocus
            anchors.fill: parent
            anchors.margins: -4
            radius: parent.radius + 2
            color: "transparent"
            border.width: 2
            border.color: theme.colors.focusRing
        }
    }

    contentItem: Item {
        Rectangle {
            width: 20
            height: 20
            radius: 10
            y: 3
            x: control.on ? 21 : 3
            opacity: control.enabled ? 1 : 0.55
            color: control.on ? theme.colors.btnPrimaryText : theme.colors.text4
        }
    }
}
