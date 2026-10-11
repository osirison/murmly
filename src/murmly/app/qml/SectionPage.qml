import QtQuick
import QtQuick.Layouts

// An empty page for one section; the screens that fill it come later.
Item {
    property string title: ""

    ColumnLayout {
        anchors.fill: parent
        anchors.leftMargin: theme.space.space32
        anchors.rightMargin: theme.space.space32
        anchors.topMargin: theme.space.space34

        AppText {
            role: "title26"
            text: title
            color: theme.colors.text
            Layout.fillWidth: true
            Accessible.role: Accessible.Heading
        }

        Item { Layout.fillHeight: true }
    }
}
