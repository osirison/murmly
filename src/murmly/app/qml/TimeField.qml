import QtQuick
import QtQuick.Controls

// A time of day written as HH:MM. It writes when the person leaves the field or
// presses Enter, and shows the file's value again afterwards, so a refused time
// does not stay on screen as though it had been saved.
TextField {
    id: field

    property string bound: ""
    property string label: ""
    signal committed(string committedText)

    implicitWidth: 76
    implicitHeight: 34
    text: bound
    maximumLength: 5
    selectByMouse: true
    font.family: theme.fontMono
    font.pixelSize: 12
    color: theme.colors.textBright
    placeholderTextColor: theme.colors.text5
    selectionColor: theme.colors.focusRing
    horizontalAlignment: Text.AlignHCenter
    validator: RegularExpressionValidator { regularExpression: /^[0-9:]{0,5}$/ }
    Accessible.role: Accessible.EditableText
    Accessible.name: label
    Accessible.focusable: true

    // Typed into and not yet committed: the file's value must not replace it.
    property bool dirty: false
    onTextEdited: dirty = true
    onEditingFinished: {
        dirty = false
        if (text !== bound)
            committed(text)
    }

    Connections {
        target: settings
        function onViewChanged() {
            if (!(field.activeFocus && field.dirty))
                field.text = Qt.binding(function () { return field.bound })
        }
    }

    background: Rectangle {
        radius: theme.radii.r8
        opacity: field.enabled ? 1 : 0.55
        color: theme.colors.panelAlt
        border.width: field.activeFocus ? 2 : 1
        border.color: field.activeFocus ? theme.colors.focusRing : theme.colors.border
    }
}
