import QtQuick
import QtQuick.Controls

// A row of exclusive choices. `options` is a list of {id, label, checked}; the
// one marked checked is drawn raised. A click reports the choice (`picked`) and
// changes nothing itself, so the page's binding to the file stays the truth.
Rectangle {
    id: control

    property var options: []
    property string label: ""
    signal picked(string id)

    implicitWidth: row.implicitWidth + 6
    implicitHeight: row.implicitHeight + 6
    radius: theme.radii.r9
    color: theme.colors.panelAlt
    border.width: 1
    border.color: theme.colors.border
    Accessible.role: Accessible.Grouping
    Accessible.name: label

    Row {
        id: row
        x: 3
        y: 3
        spacing: 2

        Repeater {
            // A count, not the list: the list is a new one on every change, which
            // would rebuild the options and drop the focus from the one just chosen
            // with Space.
            model: control.options.length

            AbstractButton {
                id: option
                required property int index
                readonly property var modelData: control.options[index]

                objectName: control.objectName + "-" + modelData.id
                property bool selected: modelData.checked === true

                implicitWidth: caption.implicitWidth + theme.space.space12 * 2
                implicitHeight: caption.implicitHeight + 16
                text: modelData.label
                focusPolicy: Qt.StrongFocus
                Accessible.role: Accessible.RadioButton
                Accessible.name: text
                Accessible.checkable: true
                Accessible.checked: selected
                Accessible.focusable: true
                onClicked: control.picked(modelData.id)
                Keys.onReturnPressed: click()
                Keys.onEnterPressed: click()

                background: Rectangle {
                    radius: theme.radii.r7
                    opacity: option.enabled ? 1 : 0.55
                    color: option.selected ? theme.colors.btnSecondaryBgAlt
                        : option.hovered ? theme.colors.panelRaised : "transparent"
                    border.width: 1
                    border.color: option.selected ? theme.colors.borderStrong : "transparent"

                    Rectangle {
                        visible: option.visualFocus
                        anchors.fill: parent
                        anchors.margins: -2
                        radius: parent.radius + 2
                        color: "transparent"
                        border.width: 2
                        border.color: theme.colors.focusRing
                    }
                }

                contentItem: AppText {
                    id: caption
                    role: "button"
                    text: option.text
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    wrapMode: Text.NoWrap
                    color: option.selected ? theme.colors.text : theme.colors.text4
                }
            }
        }
    }
}
