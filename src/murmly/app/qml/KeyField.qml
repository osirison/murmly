import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Shapes

// The field a dictation key is chosen in, for first run (`large`: the dashed
// box the canvas draws) and for Settings' key rows (a row of keys and a button).
//
// It reads keys only while it records: a person clicks it (or presses Enter or
// Space on it) or its Change button, and `hotkeys` then reads key presses made
// while this field holds the focus. Every key press is reported to `hotkeys`,
// which decides; nothing else in the window or on the desktop is listened to.
Item {
    id: field

    // "window" (Dictate) or "session" (Reply to your assistant).
    property string purpose: "window"
    property bool large: false
    readonly property var view: hotkeys.view
    readonly property var row: view.rows !== undefined && view.rows[purpose] !== undefined
        ? view.rows[purpose]
        : ({ parts: [], recording: false, busy: false, hint: "", set: false, display: "", label: "" })
    readonly property bool recording: row.recording === true
    readonly property bool busy: row.busy === true
    // The field holds the focus: the one place key presses are read from.
    readonly property bool focused: target.activeFocus

    function begin() {
        hotkeys.startRecording(purpose)
        target.focus = true
    }

    // Settings: the most width the row can give. The strip wraps its button below the
    // keys, and the keys themselves onto a second line, rather than run past it.
    property real room: 1e6
    // What the strip would take on one line.
    readonly property real onOneLine: {
        var width = 0
        var shown = 0
        for (var i = 0; i < strip.children.length; i++) {
            var part = strip.children[i]
            if (part.visible) {
                width += part.implicitWidth
                shown++
            }
        }
        return width + Math.max(0, shown - 1) * strip.spacing
    }

    implicitWidth: large ? 400 : Math.min(onOneLine, room)
    implicitHeight: large ? 168 : strip.implicitHeight

    // Once a combination is taken or cancelled, the focus goes back to what was asked from.
    onRecordingChanged: {
        if (!recording && field.focused && !large) {
            Qt.callLater(function () { changeButton.focus = true })
        }
    }

    // What takes the focus and the key presses. A plain item, not a button: a
    // button would take Space and Enter for itself.
    Item {
        id: target
        objectName: field.objectName + "-focus"
        anchors.fill: parent
        activeFocusOnTab: field.large
        Accessible.role: Accessible.Button
        Accessible.name: field.recording
            ? "Recording. Press your new keys, or Escape to cancel."
            : field.row.set
                ? "Your " + field.row.label + " keys are " + field.row.display + ". Click to record different keys."
                : "Your " + field.row.label + " keys are not set. Click to record them."
        Accessible.focusable: field.large

        onActiveFocusChanged: hotkeys.fieldFocusChanged(field.purpose, activeFocus)

        Keys.onPressed: function (event) {
            // A key held down repeats; only the first press counts, and a held Space
            // that ended a combination must not start recording again.
            if (event.isAutoRepeat) {
                event.accepted = field.recording
                return
            }
            if (hotkeys.keyPressed(field.purpose, event.key, event.modifiers)) {
                event.accepted = true
                return
            }
            if (field.large && !field.recording && !field.busy && event.modifiers === Qt.NoModifier
                && (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space)) {
                field.begin()
                event.accepted = true
            }
        }
    }

    // ---- first run: the box --------------------------------------------------
    Item {
        visible: field.large
        anchors.fill: parent

        Rectangle {
            anchors.fill: parent
            radius: theme.radii.r14
            color: theme.colors.panel
            border.width: field.recording ? 1 : 0
            border.color: theme.colors.textBright
        }

        Shape {
            visible: !field.recording
            anchors.fill: parent
            preferredRendererType: Shape.CurveRenderer

            ShapePath {
                strokeWidth: 1
                strokeColor: mouse.containsMouse || field.focused ? theme.colors.lineInert : theme.colors.borderStrong
                strokeStyle: ShapePath.DashLine
                dashPattern: [4, 3]
                fillColor: "transparent"

                PathRectangle {
                    x: 0.5
                    y: 0.5
                    width: field.width - 1
                    height: field.height - 1
                    radius: theme.radii.r14
                }
            }
        }

        MouseArea {
            id: mouse
            objectName: field.objectName + "-click"
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            enabled: !field.recording && !field.busy
            onClicked: field.begin()
        }

        Rectangle {
            visible: field.focused
            anchors.fill: parent
            anchors.margins: -4
            radius: theme.radii.r14 + 2
            color: "transparent"
            border.width: 2
            border.color: theme.colors.focusRing
        }

        ColumnLayout {
            anchors.centerIn: parent
            width: parent.width - theme.space.space24 * 2
            spacing: theme.space.space16

            RowLayout {
                Layout.alignment: Qt.AlignHCenter
                spacing: theme.space.space10

                Repeater {
                    model: field.row.parts

                    RowLayout {
                        required property var modelData
                        required property int index
                        spacing: theme.space.space10

                        AppText {
                            visible: index > 0
                            role: "monoFile"
                            font.pixelSize: 18
                            text: "+"
                            color: theme.colors.text5
                            wrapMode: Text.NoWrap
                        }
                        KeyCap {
                            objectName: "keycap"
                            large: true
                            text: modelData
                            opacity: field.recording ? 0.5 : 1
                        }
                    }
                }

                AppText {
                    visible: field.row.parts.length === 0
                    role: "monoFile"
                    font.pixelSize: 18
                    text: "No keys yet"
                    color: theme.colors.text5
                    wrapMode: Text.NoWrap
                }
            }

            RowLayout {
                Layout.alignment: Qt.AlignHCenter
                spacing: theme.space.space8

                Rectangle {
                    visible: field.recording
                    Layout.preferredWidth: 7
                    Layout.preferredHeight: 7
                    radius: 4
                    color: theme.colors.text
                }

                AppText {
                    objectName: field.objectName + "-hint"
                    role: "helper"
                    text: field.row.hint
                    color: theme.colors.text3
                    horizontalAlignment: Text.AlignHCenter
                    Layout.maximumWidth: field.width - theme.space.space24 * 2
                }
            }
        }
    }

    // ---- Settings: a row of keys and a button -------------------------------
    Flow {
        id: strip
        visible: !field.large
        width: field.width
        spacing: theme.space.space10

        // The keys now bound.
        RowLayout {
            visible: !field.recording && field.row.parts.length > 0
            spacing: 8

            Repeater {
                model: field.row.parts

                RowLayout {
                    required property var modelData
                    required property int index
                    spacing: 8

                    AppText {
                        visible: index > 0
                        role: "monoFile"
                        font.pixelSize: 13
                        text: "+"
                        color: theme.colors.text5
                    }
                    KeyCap {
                        objectName: "keycap"
                        text: modelData
                    }
                }
            }
        }

        AppText {
            visible: !field.recording && field.row.parts.length === 0
            role: "monoFile"
            font.pixelSize: 13
            text: "not set"
            color: theme.colors.text5
            wrapMode: Text.NoWrap
        }

        // While recording: the field waits for the keys.
        Rectangle {
            visible: field.recording
            implicitWidth: waiting.implicitWidth + theme.space.space14 * 2
            implicitHeight: 40
            radius: theme.radii.r9
            color: theme.colors.panel
            border.width: 1
            border.color: theme.colors.textBright

            Rectangle {
                visible: field.focused
                anchors.fill: parent
                anchors.margins: -4
                radius: parent.radius + 2
                color: "transparent"
                border.width: 2
                border.color: theme.colors.focusRing
            }

            AppText {
                id: waiting
                objectName: field.objectName + "-hint"
                anchors.centerIn: parent
                role: "helper"
                text: field.row.hint
                color: theme.colors.text2
                wrapMode: Text.NoWrap
            }
        }

        AppButton {
            id: changeButton
            objectName: field.objectName + "-change"
            visible: !field.recording
            enabled: !field.busy && hotkeys.view.busy !== true
            text: field.busy ? "Setting it…" : field.row.set ? "Change" : "Choose a key"
            onClicked: field.begin()
        }

        AppButton {
            objectName: field.objectName + "-cancel"
            visible: field.recording
            kind: "quiet"
            text: "Cancel"
            onClicked: hotkeys.stopRecording()
        }
    }
}
