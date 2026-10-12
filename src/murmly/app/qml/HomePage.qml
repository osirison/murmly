import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Home: what the daemon is doing, the keys bound, the last words and how
// murmly is set up. The five states are one layout with parts shown or hidden;
// everything it shows is already worded by the controller's `view`.
Item {
    id: page

    property string title: ""
    readonly property var view: home.view
    readonly property string state: view.state !== undefined ? view.state : ""
    readonly property var words: view.lastWords ? view.lastWords : null
    readonly property var service: view.service ? view.service : ({ starting: false, error: "", reason: "" })
    readonly property bool refused: state === "needs_you"
    readonly property bool stopped: state === "not_running"

    Flickable {
        id: scroller
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight + theme.space.space34 + theme.space.space40
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar {}

        ColumnLayout {
            id: column
            width: Math.min(scroller.width - theme.space.space32 * 2, 1040 - theme.space.space32 * 2)
            x: (scroller.width - width) / 2
            y: theme.space.space34
            spacing: theme.space.space30
            visible: page.state !== ""

            // The state, in a heading and a sentence. The reassurance for a
            // refused delivery is this heading, so it comes before anything else.
            RowLayout {
                Layout.fillWidth: true
                spacing: theme.space.space30

                HeroMark {
                    objectName: "hero-mark"
                    Layout.alignment: Qt.AlignTop
                    tone: view.tone !== undefined ? view.tone : "neutral"
                    badge: page.refused ? "1" : ""
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: theme.space.space12

                    AppText {
                        objectName: "hero-headline"
                        role: "headline32"
                        text: view.headline !== undefined ? view.headline : ""
                        color: theme.colors.text
                        Layout.fillWidth: true
                        Accessible.role: Accessible.Heading
                    }

                    AppText {
                        objectName: "hero-body"
                        role: "bodyLead"
                        text: view.body !== undefined ? view.body : ""
                        color: theme.colors.text3
                        Layout.fillWidth: true
                        Layout.maximumWidth: 580
                    }
                }
            }

            // Service not running: say so, and offer to start it.
            Rectangle {
                objectName: "service-card"
                visible: page.stopped
                Layout.fillWidth: true
                implicitHeight: stoppedColumn.implicitHeight + theme.space.space18 * 2
                radius: theme.radii.r14
                color: theme.colors.needsYouBandBg
                border.width: 1
                border.color: theme.colors.needsYouBandBorder

                ColumnLayout {
                    id: stoppedColumn
                    anchors.fill: parent
                    anchors.margins: theme.space.space18
                    spacing: theme.space.space12

                    RowLayout {
                        spacing: theme.space.space12

                        AppButton {
                            objectName: "start-service"
                            kind: "primary"
                            text: page.service.starting ? "Starting the service\u2026" : "Start the service"
                            enabled: !page.service.starting
                            onClicked: home.startService()
                        }

                        AppText {
                            role: "helper"
                            text: "This is the service murmly installs to start when you sign in."
                            color: theme.colors.text2
                            Layout.fillWidth: true
                        }
                    }

                    AppText {
                        objectName: "start-error"
                        visible: page.service.error !== ""
                        role: "monoMeta"
                        text: page.service.error
                        color: theme.colors.needsYouText
                        Layout.fillWidth: true
                    }

                    AppText {
                        objectName: "service-reason"
                        visible: page.service.reason !== ""
                        role: "monoCaption"
                        text: page.service.reason
                        color: theme.colors.text5
                        Layout.fillWidth: true
                    }
                }
            }

            // Words not typed: the daemon's own detail, exactly as it reported it.
            Rectangle {
                objectName: "refused-card"
                visible: page.refused
                Layout.fillWidth: true
                implicitHeight: refusedColumn.implicitHeight
                radius: theme.radii.r14
                color: theme.colors.needsYouBandBg
                border.width: 1
                border.color: theme.colors.needsYouBandBorder

                ColumnLayout {
                    id: refusedColumn
                    width: parent.width
                    spacing: 0

                    RowLayout {
                        Layout.fillWidth: true
                        Layout.margins: theme.space.space18
                        spacing: theme.space.space14

                        Rectangle {
                            Layout.preferredWidth: 9
                            Layout.preferredHeight: 9
                            Layout.alignment: Qt.AlignTop
                            Layout.topMargin: 4
                            radius: 4.5
                            color: "transparent"
                            border.width: 2
                            border.color: theme.colors.needsYou
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 3

                            AppText {
                                role: "bandTitle"
                                text: "Your last words weren't typed"
                                color: theme.colors.text
                                Layout.fillWidth: true
                            }

                            AppText {
                                objectName: "refused-detail"
                                visible: text !== ""
                                role: "monoMeta"
                                text: page.words ? page.words.detail : ""
                                color: theme.colors.text4
                                Layout.fillWidth: true
                            }
                        }

                        AppText {
                            objectName: "refused-when"
                            role: "monoCaption"
                            text: page.words ? page.words.when : ""
                            color: theme.colors.text5
                            wrapMode: Text.NoWrap
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        height: 1
                        color: theme.colors.needsYouDivider
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.margins: theme.space.space18
                        Layout.leftMargin: theme.space.space18 + 23
                        spacing: theme.space.space12

                        AppText {
                            role: "label"
                            text: "What you said"
                            color: theme.colors.textLabel
                        }

                        AppText {
                            objectName: "refused-text"
                            role: "bodyLead"
                            font.pixelSize: 15
                            lineHeight: 1.55
                            text: page.words ? page.words.text : ""
                            color: theme.colors.textBright
                            Layout.fillWidth: true
                            Layout.maximumWidth: 760
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        height: 1
                        color: theme.colors.needsYouDivider
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        Layout.margins: theme.space.space14
                        Layout.leftMargin: theme.space.space18 + 23
                        spacing: theme.space.space10

                        AppButton {
                            objectName: "copy-again"
                            kind: "primary"
                            text: "Copy again"
                            onClicked: home.copyAgain()
                        }

                        AppButton {
                            objectName: "dismiss-refusal"
                            text: "Done"
                            onClicked: home.dismissRefusal()
                        }

                        AppText {
                            role: "helper"
                            text: "Then press Ctrl + V where you meant them to go."
                            color: theme.colors.text2
                            Layout.fillWidth: true
                        }
                    }
                }
            }

            // The keys bound.
            GridLayout {
                objectName: "keys"
                visible: !page.refused
                Layout.fillWidth: true
                columns: width >= 640 ? 2 : 1
                columnSpacing: theme.space.space16
                rowSpacing: theme.space.space16

                Repeater {
                    model: view.keys !== undefined ? view.keys : []

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                        implicitHeight: keyColumn.implicitHeight + theme.space.space18 * 2
                        radius: theme.radii.r13
                        color: theme.colors.panel
                        border.width: 1
                        border.color: theme.colors.borderSubtle

                        ColumnLayout {
                            id: keyColumn
                            anchors.fill: parent
                            anchors.leftMargin: theme.space.space20
                            anchors.rightMargin: theme.space.space20
                            anchors.topMargin: theme.space.space18
                            anchors.bottomMargin: theme.space.space18
                            spacing: theme.space.space14

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: theme.space.space12

                                AppText {
                                    role: "label"
                                    text: modelData.label
                                    color: theme.colors.textLabel
                                    Layout.fillWidth: true
                                }

                                AppText {
                                    objectName: "key-note-" + index
                                    role: "monoCaption"
                                    text: modelData.note
                                    color: theme.colors.text5
                                    wrapMode: Text.NoWrap
                                }
                            }

                            RowLayout {
                                spacing: 8

                                Repeater {
                                    model: modelData.keys

                                    RowLayout {
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

                                Rectangle {
                                    visible: modelData.keys.length === 0
                                    implicitWidth: noKey.implicitWidth + theme.space.space14 * 2
                                    implicitHeight: 40
                                    radius: theme.radii.r9
                                    color: "transparent"
                                    border.width: 1
                                    border.color: theme.colors.borderStrong

                                    AppText {
                                        id: noKey
                                        anchors.centerIn: parent
                                        role: "monoFile"
                                        font.pixelSize: 13
                                        text: "No key yet"
                                        color: theme.colors.text5
                                        wrapMode: Text.NoWrap
                                    }
                                }

                                // Setting up again after "Set up later": the person asks for it.
                                AppButton {
                                    objectName: "key-setup-" + index
                                    visible: modelData.keys.length === 0 && index === 0
                                    text: "Set up"
                                    onClicked: firstRun.open()
                                }
                            }

                            AppText {
                                role: "helper"
                                text: modelData.helper
                                color: theme.colors.text4
                                Layout.fillWidth: true
                            }
                        }
                    }
                }
            }

            // The last words, with what happened to them.
            Rectangle {
                objectName: "last-words"
                visible: !page.refused && !page.stopped
                Layout.fillWidth: true
                implicitHeight: lastColumn.implicitHeight + theme.space.space20 * 2
                radius: theme.radii.r13
                color: theme.colors.panel
                border.width: 1
                border.color: theme.colors.borderSubtle

                ColumnLayout {
                    id: lastColumn
                    anchors.fill: parent
                    anchors.margins: theme.space.space20
                    anchors.leftMargin: theme.space.space22
                    anchors.rightMargin: theme.space.space22
                    spacing: theme.space.space14

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: theme.space.space16

                        AppText {
                            role: "label"
                            text: "Last words"
                            color: theme.colors.textLabel
                            Accessible.role: Accessible.Heading
                        }

                        AppText {
                            objectName: "last-words-when"
                            role: "monoCaption"
                            text: page.words ? page.words.when : ""
                            color: theme.colors.text5
                            wrapMode: Text.NoWrap
                        }

                        Item { Layout.fillWidth: true }

                        AppButton {
                            objectName: "copy-last"
                            visible: page.words !== null
                            text: "Copy again"
                            onClicked: home.copyAgain()
                        }

                        AppButton {
                            objectName: "clear-last-words"
                            visible: page.words !== null
                            kind: "quiet"
                            text: "Clear"
                            onClicked: home.clearLastWords()
                        }
                    }

                    AppText {
                        objectName: "last-words-text"
                        visible: page.words !== null
                        role: "bodyLead"
                        font.pixelSize: 15
                        lineHeight: 1.55
                        text: page.words ? page.words.text : ""
                        color: theme.colors.textBright
                        Layout.fillWidth: true
                        Layout.maximumWidth: 760
                    }

                    AppText {
                        objectName: "last-words-outcome"
                        visible: page.words !== null
                        role: "monoCaption"
                        text: page.words ? page.words.outcome : ""
                        color: theme.colors.text5
                        Layout.fillWidth: true
                    }

                    AppText {
                        objectName: "last-words-empty"
                        visible: page.words === null
                        role: "body"
                        text: "Nothing yet. Your next words appear here."
                        color: theme.colors.text4
                        Layout.fillWidth: true
                    }
                }
            }

            // How murmly is set up: a plain value beside the raw setting.
            GridLayout {
                objectName: "setup"
                visible: !page.refused && view.setup !== undefined && view.setup.length > 0
                Layout.fillWidth: true
                columns: Math.max(1, Math.min(5, Math.floor(width / 176)))
                columnSpacing: 1
                rowSpacing: 1

                Repeater {
                    model: view.setup !== undefined ? view.setup : []

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                        implicitHeight: setupColumn.implicitHeight + theme.space.space16 * 2
                        color: theme.colors.panelAlt
                        border.width: 1
                        border.color: theme.colors.divider

                        ColumnLayout {
                            id: setupColumn
                            anchors.fill: parent
                            anchors.margins: theme.space.space16
                            anchors.leftMargin: theme.space.space20
                            anchors.rightMargin: theme.space.space20
                            spacing: 6

                            AppText {
                                role: "label"
                                text: modelData.label
                                color: theme.colors.textLabel
                            }

                            AppText {
                                role: "title14"
                                text: modelData.value
                                color: theme.colors.textBright
                                Layout.fillWidth: true
                            }

                            AppText {
                                role: "monoCaption"
                                text: modelData.note
                                color: theme.colors.text5
                                Layout.fillWidth: true
                            }
                        }
                    }
                }
            }

            AppText {
                objectName: "footer"
                role: "monoCaption"
                text: view.footer !== undefined ? view.footer : ""
                color: theme.colors.text5
                Layout.fillWidth: true
            }
        }
    }
}
