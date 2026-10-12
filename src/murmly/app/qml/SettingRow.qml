import QtQuick
import QtQuick.Layouts

// One setting in a card: what it is, a sentence about it and, in mono, the key
// it writes. The control sits at the right, or below when the window is narrow.
Item {
    id: row

    property string title: ""
    property string text: ""
    property string keyLine: ""
    property bool divider: false
    default property alias control: slot.data

    Layout.fillWidth: true
    implicitHeight: flow.implicitHeight + theme.space.space16 * 2

    Rectangle {
        visible: row.divider
        anchors.bottom: parent.bottom
        width: parent.width
        height: 1
        color: theme.colors.divider
    }

    Flow {
        id: flow
        x: theme.space.space20
        y: theme.space.space16
        width: row.width - theme.space.space20 * 2
        spacing: theme.space.space12

        Column {
            width: Math.max(240, flow.width - slot.implicitWidth - flow.spacing)
            spacing: 3

            AppText {
                role: "title14"
                width: parent.width
                text: row.title
                color: theme.colors.textBright
                Accessible.role: Accessible.StaticText
            }

            AppText {
                role: "body"
                width: Math.min(parent.width, 540)
                visible: row.text !== ""
                text: row.text
                color: theme.colors.text4
            }

            AppText {
                role: "monoFootnote"
                width: parent.width
                topPadding: 3
                visible: row.keyLine !== ""
                text: row.keyLine
                color: theme.colors.text5
            }
        }

        Row {
            id: slot
            spacing: theme.space.space8
        }
    }
}
