import QtQuick
import QtQuick.Controls

// A slider that writes when the person lets go or pauses on a key, never on every
// step of a drag. `bound` is the value in the file; the handle follows it again
// whenever the page's view changes, so a refused value snaps back. A value
// outside the slider's span stays as the file has it until the handle is moved.
Slider {
    id: slider

    property real bound: 0
    property string label: ""
    property var format: function (value) { return value }
    readonly property string valueText: String(format(pressed || settle.running ? value : bound))
    signal committed(real committedValue)

    implicitWidth: 220
    implicitHeight: 28
    snapMode: Slider.SnapAlways
    value: bound
    focusPolicy: Qt.StrongFocus
    Accessible.role: Accessible.Slider
    Accessible.name: label
    Accessible.focusable: true

    onMoved: settle.restart()
    onPressedChanged: {
        if (!pressed && settle.running) {
            settle.stop()
            committed(value)
        }
    }

    Timer {
        id: settle
        interval: 500
        onTriggered: slider.committed(slider.value)
    }

    Connections {
        target: settings
        function onViewChanged() {
            if (!settle.running && !slider.pressed)
                slider.value = Qt.binding(function () { return slider.bound })
        }
    }

    background: Item {
        x: slider.leftPadding
        y: slider.topPadding + slider.availableHeight / 2 - height / 2
        width: slider.availableWidth
        height: 28

        Rectangle {
            anchors.verticalCenter: parent.verticalCenter
            width: parent.width
            height: 4
            radius: 2
            color: theme.colors.trackInert
            opacity: slider.enabled ? 1 : 0.55

            Rectangle {
                width: slider.visualPosition * parent.width
                height: parent.height
                radius: 2
                color: theme.colors.text3
            }
        }
    }

    handle: Rectangle {
        x: slider.leftPadding + slider.visualPosition * (slider.availableWidth - width)
        y: slider.topPadding + slider.availableHeight / 2 - height / 2
        width: 18
        height: 18
        radius: 9
        opacity: slider.enabled ? 1 : 0.55
        color: theme.colors.btnPrimaryBg
        border.width: slider.visualFocus ? 2 : 0
        border.color: theme.colors.focusRing
    }
}
