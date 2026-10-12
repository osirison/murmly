import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// One choice among several, as a card with a round mark. `checked` is bound by
// the page to the file's value; a click reports the choice and changes nothing.
AbstractButton {
    id: card

    property bool selected: false
    property string title: ""
    property string detail: ""
    property string body: ""

    implicitHeight: content.implicitHeight
    focusPolicy: Qt.StrongFocus
    Accessible.role: Accessible.RadioButton
    Accessible.name: title
    Accessible.description: detail + (body !== "" ? ". " + body : "")
    Accessible.checkable: true
    Accessible.checked: selected
    Accessible.focusable: true

    Keys.onReturnPressed: click()
    Keys.onEnterPressed: click()

    background: Rectangle {
        radius: theme.radii.r12
        opacity: card.enabled ? 1 : 0.55
        color: card.selected ? theme.colors.panelRaised : theme.colors.panel
        border.width: 1
        border.color: card.selected ? theme.colors.borderStrong : theme.colors.borderSubtle

        Rectangle {
            visible: card.visualFocus
            anchors.fill: parent
            anchors.margins: -4
            radius: parent.radius + 2
            color: "transparent"
            border.width: 2
            border.color: theme.colors.focusRing
        }
    }

    contentItem: ColumnLayout {
        id: content
        spacing: 0
        opacity: card.enabled ? 1 : 0.55

        RowLayout {
            Layout.fillWidth: true
            Layout.leftMargin: theme.space.space16
            Layout.rightMargin: theme.space.space16
            Layout.topMargin: theme.space.space14
            Layout.bottomMargin: card.body === "" ? theme.space.space14 : 4
            spacing: theme.space.space10

            Rectangle {
                Layout.preferredWidth: 16
                Layout.preferredHeight: 16
                radius: 8
                color: "transparent"
                border.width: card.selected ? 5 : 1
                border.color: card.selected ? theme.colors.text : theme.colors.lineInert
            }

            AppText {
                role: "title14"
                text: card.title
                color: card.selected ? theme.colors.text : theme.colors.text2
                wrapMode: Text.NoWrap
            }

            AppText {
                role: "monoCaption"
                text: card.detail
                color: theme.colors.text5
                wrapMode: Text.NoWrap
                visible: text !== ""
                Layout.fillWidth: true
            }
        }

        AppText {
            role: "body"
            visible: card.body !== ""
            text: card.body
            color: card.selected ? theme.colors.text3 : theme.colors.text4
            Layout.fillWidth: true
            Layout.leftMargin: theme.space.space16 + 26
            Layout.rightMargin: theme.space.space16
            Layout.bottomMargin: theme.space.space14
        }

    }
}
