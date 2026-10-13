import QtQuick
import QtQuick.Controls

// One section in the header's navigation. Tab reaches it; Space and Enter
// operate it; the keyboard focus ring is drawn only for keyboard focus.
AbstractButton {
    id: button

    property bool selected: false

    implicitWidth: label.implicitWidth + theme.space.space12 * 2
    implicitHeight: label.implicitHeight + theme.space.space6 * 2 + 2
    focusPolicy: Qt.StrongFocus
    Accessible.role: Accessible.Button
    Accessible.name: text
    // Qt reports a button's own `checkable` and `checked` to assistive technology, over
    // the Accessible ones, so these are what a screen reader reads. A click toggles a
    // checkable button, so the binding to the section shown is put back at once.
    checkable: true
    checked: selected
    onToggled: checked = Qt.binding(function () { return selected })
    Accessible.focusable: true

    Keys.onReturnPressed: click()
    Keys.onEnterPressed: click()

    background: Rectangle {
        radius: theme.radii.r7
        color: button.selected ? theme.colors.panelRaised
            : button.hovered ? theme.colors.panelRaised : "transparent"
        border.width: 1
        border.color: button.selected && theme.scheme === "light" ? theme.colors.border : "transparent"

        Rectangle {
            visible: button.visualFocus
            anchors.fill: parent
            anchors.margins: -4
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
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        wrapMode: Text.NoWrap
        color: button.selected ? theme.colors.text : theme.colors.text3
    }
}
