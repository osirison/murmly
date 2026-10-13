import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

// The tray panel: Home at a smaller scale. The state, the last words and the four
// quick switches, then Open murmly, Health and Quit. It is shown only by a click
// on the tray icon, and it hides when the person clicks elsewhere.
Window {
    id: panel

    objectName: "tray-panel"
    readonly property var view: home.view
    readonly property string state: view.state !== undefined ? view.state : ""
    readonly property var words: view.lastWords ? view.lastWords : null
    readonly property var switchView: tray.view
    readonly property var dictateKey: view.keys && view.keys.length > 0 && view.keys[0].keys.length > 0
        ? view.keys[0].keys.join(" + ") : ""

    flags: Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
    title: "Murmly"
    visible: false
    width: theme.layout.wCompact
    height: content.implicitHeight
    color: theme.colors.panel

    // Polling and the settings file are looked at only while the panel is showing.
    onVisibleChanged: {
        home.setSurfaceVisible("tray", visible)
        settings.setSurfaceActive("tray", visible)
    }
    // Clicking another window takes the focus away, and the panel goes with it.
    onActiveChanged: if (!active && visible) tray.panelLostFocus()

    Item {
        id: content
        anchors.fill: parent
        implicitHeight: column.implicitHeight + 2
        Keys.onEscapePressed: tray.hidePanel()

        Rectangle {
            anchors.fill: parent
            color: "transparent"
            border.width: 1
            border.color: theme.colors.borderChrome
            z: 1
        }

        ColumnLayout {
            id: column
            x: 1
            y: 1
            width: parent.width - 2
            spacing: 0

            // The mark, the name and the state.
            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: theme.space.space18
                Layout.rightMargin: theme.space.space18
                Layout.topMargin: theme.space.space14
                Layout.bottomMargin: theme.space.space14
                spacing: 9

                Mark {}

                AppText {
                    role: "row"
                    text: "murmly"
                    color: theme.colors.textBright
                    wrapMode: Text.NoWrap
                }

                Item { Layout.fillWidth: true }

                StateChip {
                    objectName: "tray-state-chip"
                    visible: text !== ""
                    text: panel.view.chip !== undefined ? panel.view.chip : ""
                    tone: panel.view.tone !== undefined ? panel.view.tone : "neutral"
                }
            }

            Rectangle { Layout.fillWidth: true; height: 1; color: theme.colors.divider }

            ColumnLayout {
                Layout.fillWidth: true
                Layout.margins: theme.space.space18
                spacing: theme.space.space18

                ColumnLayout {
                    Layout.fillWidth: true
                    visible: panel.state !== ""
                    spacing: 6

                    AppText {
                        objectName: "tray-headline"
                        Layout.fillWidth: true
                        role: "headline17"
                        text: panel.view.headline !== undefined ? panel.view.headline : ""
                        color: theme.colors.text
                    }

                    AppText {
                        objectName: "tray-body"
                        Layout.fillWidth: true
                        role: "helper"
                        maximumLineCount: 3
                        elide: Text.ElideRight
                        text: panel.state === "ready" && panel.dictateKey !== ""
                            ? "Press " + panel.dictateKey + " in any window to dictate."
                            : (panel.view.body !== undefined ? panel.view.body : "")
                        color: theme.colors.text4
                    }
                }

                // The last words, clamped to two lines; the whole text is on Home.
                Rectangle {
                    objectName: "tray-last-words"
                    Layout.fillWidth: true
                    visible: panel.words !== null
                    implicitHeight: lastColumn.implicitHeight + theme.space.space12 * 2
                    radius: theme.radii.r11
                    color: theme.colors.panelAlt
                    border.width: 1
                    border.color: theme.colors.border

                    ColumnLayout {
                        id: lastColumn
                        anchors.fill: parent
                        anchors.margins: theme.space.space12
                        anchors.leftMargin: theme.space.space14
                        anchors.rightMargin: theme.space.space14
                        spacing: 8

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: theme.space.space10

                            AppText {
                                role: "labelCompact"
                                text: "Last words"
                                color: theme.colors.textLabel
                                wrapMode: Text.NoWrap
                            }

                            AppText {
                                objectName: "tray-last-when"
                                role: "monoFootnote"
                                text: panel.words ? panel.words.when : ""
                                color: theme.colors.text5
                                wrapMode: Text.NoWrap
                            }

                            Item { Layout.fillWidth: true }

                            AppButton {
                                objectName: "tray-copy-last"
                                text: "Copy"
                                onClicked: home.copyAgain()
                            }
                        }

                        AppText {
                            objectName: "tray-last-text"
                            Layout.fillWidth: true
                            role: "body"
                            maximumLineCount: 2
                            elide: Text.ElideRight
                            text: panel.words ? panel.words.text : ""
                            color: theme.colors.textBright
                        }

                        AppText {
                            objectName: "tray-last-outcome"
                            Layout.fillWidth: true
                            role: "monoFootnote"
                            text: panel.words ? panel.words.outcome : ""
                            color: panel.words && !panel.words.delivered ? theme.colors.needsYouText : theme.colors.text5
                        }
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 0

                    AppText {
                        role: "labelCompact"
                        Layout.bottomMargin: 6
                        text: "Quick switches"
                        color: theme.colors.textLabel
                        Accessible.role: Accessible.Heading
                    }

                    Repeater {
                        // A count, not the list: the list is a new one on every change,
                        // which would rebuild the rows and drop the focus from a switch
                        // that was just toggled with the keyboard.
                        model: panel.switchView.switches ? panel.switchView.switches.length : 0

                        Item {
                            id: row
                            required property int index
                            readonly property var modelData: panel.switchView.switches[index]

                            Layout.fillWidth: true
                            implicitHeight: rowLayout.implicitHeight + theme.space.space10 * 2

                            Rectangle {
                                anchors.bottom: parent.bottom
                                width: parent.width
                                height: 1
                                color: theme.colors.divider
                                visible: row.index < 3
                            }

                            RowLayout {
                                id: rowLayout
                                x: 0
                                y: theme.space.space10
                                width: parent.width
                                spacing: theme.space.space12

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 0

                                    AppText {
                                        Layout.fillWidth: true
                                        role: "row"
                                        text: row.modelData.name
                                        color: theme.colors.textBright
                                    }

                                    AppText {
                                        objectName: "tray-note-" + row.modelData.id
                                        Layout.fillWidth: true
                                        role: "monoFootnote"
                                        text: row.modelData.note
                                        color: theme.colors.text5
                                    }
                                }

                                ToggleSwitch {
                                    objectName: "tray-switch-" + row.modelData.id
                                    text: row.modelData.name
                                    on: row.modelData.on === true
                                    enabled: panel.switchView.disabled !== true
                                    onClicked: tray.toggle(row.modelData.id)
                                }
                            }
                        }
                    }

                    AppText {
                        objectName: "tray-message"
                        Layout.fillWidth: true
                        Layout.topMargin: 8
                        visible: text !== ""
                        role: "monoFootnote"
                        text: panel.switchView.message ? panel.switchView.message
                            : (panel.switchView.status ? panel.switchView.status : "")
                        color: panel.switchView.message ? theme.colors.needsYouText : theme.colors.text5
                    }
                }
            }

            Rectangle { Layout.fillWidth: true; height: 1; color: theme.colors.divider }

            RowLayout {
                Layout.fillWidth: true
                Layout.margins: theme.space.space12
                Layout.leftMargin: theme.space.space18
                Layout.rightMargin: theme.space.space18
                spacing: theme.space.space8

                AppButton {
                    objectName: "tray-open"
                    text: "Open murmly"
                    onClicked: tray.openWindow("")
                }

                AppButton {
                    objectName: "tray-health"
                    kind: "quiet"
                    text: "Health"
                    onClicked: tray.openWindow("health")
                }

                Item { Layout.fillWidth: true }

                AppButton {
                    objectName: "tray-quit"
                    kind: "quiet"
                    text: "Quit"
                    onClicked: tray.quit()
                }
            }
        }
    }
}
