import QtQuick
import QtQuick.Layouts

// The panel a group of settings sits in. Children are stacked inside it.
Rectangle {
    id: card

    default property alias content: column.data

    Layout.fillWidth: true
    implicitHeight: column.implicitHeight + 2
    radius: theme.radii.r13
    color: theme.colors.panel
    border.width: 1
    border.color: theme.colors.borderSubtle
    clip: true

    ColumnLayout {
        id: column
        anchors.fill: parent
        anchors.margins: 1
        spacing: 0
    }
}
