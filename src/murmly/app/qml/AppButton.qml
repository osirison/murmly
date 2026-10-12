import QtQuick
import QtQuick.Controls

// A button in the design's three weights: "primary" (solid), "secondary"
// (outlined) and "quiet". Tab reaches it; Space and Enter operate it; the
// focus ring is drawn only for keyboard focus.
AbstractButton {
    id: button

    property string kind: "secondary"

    implicitWidth: label.implicitWidth + theme.space.space16 * 2
    implicitHeight: label.implicitHeight + theme.space.space8 * 2 + 2
    focusPolicy: Qt.StrongFocus
    Accessible.role: Accessible.Button
    Accessible.name: text
    Accessible.focusable: true

    Keys.onReturnPressed: click()
    Keys.onEnterPressed: click()

    background: Rectangle {
        radius: theme.radii.r9
        opacity: button.enabled ? 1 : 0.55
        color: button.kind === "primary" ? theme.colors.btnPrimaryBg
            : button.kind === "quiet" ? (button.hovered ? theme.colors.btnSecondaryBg : theme.colors.btnQuietBg)
            : (button.hovered ? theme.colors.btnSecondaryBgAlt : theme.colors.btnSecondaryBg)
        border.width: button.kind === "primary" ? 0 : 1
        border.color: button.kind === "quiet" ? theme.colors.btnQuietBorder : theme.colors.btnSecondaryBorder

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
        font.weight: button.kind === "primary" ? 600 : 500
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        wrapMode: Text.NoWrap
        color: button.kind === "primary" ? theme.colors.btnPrimaryText
            : button.kind === "quiet" ? theme.colors.btnQuietText
            : theme.colors.btnSecondaryText
    }
}
