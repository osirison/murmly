import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// First run: choose the dictation keys and try them, then choose how careful the
// listening is. A layer over the window with a header of its own, as the canvas
// draws it. The steps are `firstRun.view.steps`; the optional voice step goes
// between these two when voices are built.
Rectangle {
    id: page

    readonly property var view: firstRun.view
    readonly property string step: view.step !== undefined ? view.step : "keys"
    readonly property var keyView: hotkeys.view
    readonly property var windowRow: keyView.rows !== undefined && keyView.rows["window"] !== undefined
        ? keyView.rows["window"] : ({ parts: [], display: "", set: false })
    readonly property var note: keyView.note !== undefined ? keyView.note : ({})
    readonly property var profiles: settings.view.options !== undefined && settings.view.options["profile"] !== undefined
        ? settings.view.options["profile"] : []

    color: theme.colors.surface

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // ---- header: the mark and the steps ---------------------------------
        Rectangle {
            Layout.fillWidth: true
            implicitHeight: theme.layout.hHeader
            color: theme.colors.surface

            Rectangle {
                anchors.bottom: parent.bottom
                width: parent.width
                height: 1
                color: theme.colors.divider
            }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: theme.space.space20
                anchors.rightMargin: theme.space.space20
                spacing: theme.space.space20

                RowLayout {
                    spacing: 9

                    Mark {}

                    AppText {
                        role: "title14"
                        text: "murmly"
                        color: theme.colors.textBright
                        wrapMode: Text.NoWrap
                    }
                }

                Item { Layout.fillWidth: true }

                Row {
                    objectName: "first-run-steps"
                    spacing: theme.space.space18
                    Accessible.role: Accessible.List
                    Accessible.name: "Setting up"

                    Repeater {
                        model: page.view.steps !== undefined ? page.view.steps : []

                        Row {
                            required property var modelData
                            objectName: "first-run-step-" + modelData.id
                            spacing: 8
                            Accessible.role: Accessible.ListItem
                            Accessible.name: "Step " + modelData.number + ": " + modelData.title
                                + (modelData.state === "current" ? ", the current step" : modelData.state === "done" ? ", done" : "")

                            Rectangle {
                                anchors.verticalCenter: parent.verticalCenter
                                width: 18
                                height: 18
                                radius: 9
                                color: modelData.state === "current" ? theme.colors.text : "transparent"
                                border.width: modelData.state === "current" ? 0 : 1
                                border.color: theme.colors.borderStrong

                                AppText {
                                    anchors.centerIn: parent
                                    role: "monoCaption"
                                    text: modelData.number
                                    font.pixelSize: 11
                                    lineHeight: 1
                                    font.weight: 600
                                    wrapMode: Text.NoWrap
                                    color: modelData.state === "current" ? theme.colors.surface : theme.colors.text4
                                }
                            }

                            AppText {
                                anchors.verticalCenter: parent.verticalCenter
                                role: "helper"
                                text: modelData.title
                                font.weight: modelData.state === "current" ? 500 : 400
                                color: modelData.state === "current" ? theme.colors.text : theme.colors.text4
                                wrapMode: Text.NoWrap
                            }
                        }
                    }
                }
            }
        }

        // ---- the step ---------------------------------------------------------
        Flickable {
            id: scroller
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentWidth: width
            contentHeight: body.implicitHeight + theme.space.space40 + theme.space.space34
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar {}

            ColumnLayout {
                id: body
                x: Math.max(theme.space.space32, (scroller.width - width) / 2)
                y: theme.space.space40
                width: Math.min(scroller.width - theme.space.space32 * 2, 760)
                spacing: theme.space.space26

                // ---- Your keys ---------------------------------------------------
                ColumnLayout {
                    objectName: "first-run-keys-step"
                    visible: page.step === "keys"
                    spacing: theme.space.space26
                    Layout.fillWidth: true

                    ColumnLayout {
                        spacing: theme.space.space12
                        Layout.fillWidth: true

                        AppText {
                            role: "headline30"
                            text: "Choose the keys you'll press to dictate"
                            color: theme.colors.text
                            Layout.fillWidth: true
                            Accessible.role: Accessible.Heading
                        }

                        AppText {
                            role: "body"
                            text: "Press them once to start and again to finish, from any window. Pick something no other app uses."
                            color: theme.colors.text3
                            Layout.fillWidth: true
                            Layout.maximumWidth: 580
                        }
                    }

                    ColumnLayout {
                        spacing: theme.space.space14
                        Layout.fillWidth: true

                        KeyField {
                            objectName: "first-run-key-field"
                            purpose: "window"
                            large: true
                            Layout.fillWidth: true
                            Layout.preferredHeight: 168
                        }

                        // What binding the keys came to. A refusal is an outline, never a fill.
                        Banner {
                            objectName: "first-run-note"
                            visible: page.note.purpose === "window" && page.note.tone !== undefined && page.note.tone !== ""
                            tone: page.note.tone === "amber" ? "amber" : "crimson"
                            title: page.note.status === "held" ? "Those keys are taken"
                                : page.note.status === "unconfirmed" ? "Saved for your next sign-in"
                                : "The keys were not set"
                            text: page.note.text !== undefined ? page.note.text : ""
                            textItem.objectName: "first-run-note-text"
                        }

                        AppText {
                            objectName: "first-run-detail"
                            visible: page.note.purpose === "window" && page.note.tone !== undefined
                                && page.note.tone !== "" && page.note.detail !== undefined && page.note.detail !== ""
                            role: "monoMeta"
                            text: page.note.detail !== undefined ? page.note.detail : ""
                            color: theme.colors.text5
                            Layout.fillWidth: true
                        }

                        Flow {
                            Layout.fillWidth: true
                            spacing: theme.space.space8

                            AppText {
                                role: "helper"
                                text: "Or pick one:"
                                height: 32
                                verticalAlignment: Text.AlignVCenter
                                rightPadding: 4
                                color: theme.colors.text4
                                wrapMode: Text.NoWrap
                            }

                            Repeater {
                                model: keyView.suggestions !== undefined ? keyView.suggestions : []

                                AbstractButton {
                                    id: pill
                                    required property var modelData
                                    objectName: "first-run-pick-" + modelData.text
                                    text: modelData.label
                                    enabled: keyView.busy !== true
                                    focusPolicy: Qt.StrongFocus
                                    implicitWidth: pillLabel.implicitWidth + 30
                                    implicitHeight: 32
                                    Accessible.role: Accessible.Button
                                    Accessible.name: "Use " + text
                                    Accessible.focusable: true
                                    Keys.onReturnPressed: click()
                                    Keys.onEnterPressed: click()
                                    onClicked: hotkeys.pick("window", modelData.text)

                                    background: Rectangle {
                                        radius: theme.radii.rPill
                                        opacity: pill.enabled ? 1 : 0.55
                                        color: pill.hovered ? theme.colors.panelRaised : "transparent"
                                        border.width: 1
                                        border.color: pill.hovered ? theme.colors.borderStrong : theme.colors.border

                                        Rectangle {
                                            visible: pill.visualFocus
                                            anchors.fill: parent
                                            anchors.margins: -4
                                            radius: parent.radius + 2
                                            color: "transparent"
                                            border.width: 2
                                            border.color: theme.colors.focusRing
                                        }
                                    }

                                    contentItem: AppText {
                                        id: pillLabel
                                        role: "monoCaption"
                                        text: pill.text
                                        wrapMode: Text.NoWrap
                                        horizontalAlignment: Text.AlignHCenter
                                        verticalAlignment: Text.AlignVCenter
                                        color: theme.colors.text2
                                    }
                                }
                            }
                        }

                        AppText {
                            role: "helper"
                            text: "Meta is the Windows key on Windows, and the Super key on Linux."
                            color: theme.colors.text4
                            Layout.fillWidth: true
                        }
                    }

                    // Try it: an ordinary text field. Murmly's own delivery types into
                    // it, as it would into any window.
                    ColumnLayout {
                        spacing: theme.space.space10
                        Layout.fillWidth: true

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 1
                            Layout.bottomMargin: theme.space.space12
                            color: theme.colors.divider
                        }

                        AppText {
                            role: "title14"
                            text: "Try it here"
                            color: theme.colors.textBright
                            Accessible.role: Accessible.StaticText
                        }

                        TextArea {
                            id: tryIt
                            objectName: "first-run-try"
                            Layout.fillWidth: true
                            Layout.preferredHeight: 92
                            wrapMode: TextEdit.Wrap
                            selectByMouse: true
                            placeholderText: page.windowRow.set
                                ? "Press " + page.windowRow.display + ", say a sentence, then press it again. Your words appear here."
                                : "Choose your keys above, then press them and say a sentence. Your words appear here."
                            placeholderTextColor: theme.colors.text5
                            color: theme.colors.text2
                            font.family: theme.fontSans
                            font.pixelSize: 14
                            leftPadding: 14
                            rightPadding: 14
                            topPadding: 12
                            bottomPadding: 12
                            Accessible.name: "Try it here"
                            Accessible.description: "A text box to check that dictation types into a window."
                            // Tab leaves the box instead of typing a tab.
                            KeyNavigation.priority: KeyNavigation.BeforeItem
                            KeyNavigation.tab: next
                            KeyNavigation.backtab: later

                            background: Rectangle {
                                radius: theme.radii.r9
                                color: theme.colors.panelAlt
                                border.width: 1
                                border.color: tryIt.activeFocus ? theme.colors.borderStrong : theme.colors.border

                                Rectangle {
                                    visible: tryIt.activeFocus
                                    anchors.fill: parent
                                    anchors.margins: -4
                                    radius: parent.radius + 2
                                    color: "transparent"
                                    border.width: 2
                                    border.color: theme.colors.focusRing
                                }
                            }
                        }
                    }
                }

                // ---- How careful ---------------------------------------------------
                ColumnLayout {
                    objectName: "first-run-accuracy-step"
                    visible: page.step === "accuracy"
                    spacing: theme.space.space26
                    Layout.fillWidth: true

                    ColumnLayout {
                        spacing: theme.space.space12
                        Layout.fillWidth: true

                        AppText {
                            role: "headline30"
                            text: "How careful should it listen?"
                            color: theme.colors.text
                            Layout.fillWidth: true
                            Accessible.role: Accessible.Heading
                        }

                        AppText {
                            role: "body"
                            text: "A more careful listener gets more words right and takes longer. You can change this at any time in Settings."
                            color: theme.colors.text3
                            Layout.fillWidth: true
                            Layout.maximumWidth: 580
                        }
                    }

                    Banner {
                        objectName: "first-run-profile-message"
                        visible: settings.view.message !== undefined && settings.view.message !== ""
                        tone: "crimson"
                        title: "That was not saved"
                        text: settings.view.message !== undefined ? settings.view.message : ""
                    }

                    AppText {
                        visible: settings.view.loaded !== true
                        text: "Reading the settings file…"
                        role: "body"
                        color: theme.colors.text4
                    }

                    ColumnLayout {
                        enabled: settings.view.disabled !== true
                        spacing: theme.space.space10
                        Layout.fillWidth: true

                        Repeater {
                            model: page.profiles

                            ChoiceCard {
                                required property var modelData
                                objectName: "first-run-profile-" + modelData.id
                                Layout.fillWidth: true
                                title: modelData.name
                                detail: modelData.detail
                                body: modelData.text
                                selected: modelData.checked === true
                                onClicked: settings.change("profile", modelData.id)
                            }
                        }
                    }

                    AppText {
                        objectName: "first-run-profile-status"
                        visible: text !== ""
                        text: settings.view.status !== undefined ? settings.view.status : ""
                        role: "monoCaption"
                        color: theme.colors.text5
                        Layout.fillWidth: true
                    }
                }
            }
        }

        // ---- footer ----------------------------------------------------------------
        Rectangle {
            Layout.fillWidth: true
            implicitHeight: footer.implicitHeight + theme.space.space14 * 2
            color: theme.colors.surface

            Rectangle {
                anchors.top: parent.top
                width: parent.width
                height: 1
                color: theme.colors.divider
            }

            RowLayout {
                id: footer
                anchors.fill: parent
                anchors.leftMargin: theme.space.space32
                anchors.rightMargin: theme.space.space32
                spacing: theme.space.space10

                AppButton {
                    id: later
                    objectName: "first-run-later"
                    kind: "quiet"
                    text: "Set up later"
                    onClicked: firstRun.later()
                    KeyNavigation.tab: next
                }

                AppButton {
                    objectName: "first-run-back"
                    kind: "quiet"
                    visible: page.view.index > 0
                    text: "Back"
                    onClicked: firstRun.back()
                }

                Item { Layout.fillWidth: true }

                AppText {
                    objectName: "first-run-status"
                    role: "monoCaption"
                    text: page.step === "keys" && page.windowRow.set ? page.windowRow.display + " · set" : ""
                    color: theme.colors.text5
                    wrapMode: Text.NoWrap
                }

                AppButton {
                    id: next
                    objectName: "first-run-next"
                    kind: "primary"
                    text: page.view.last === true ? "Finish" : "Continue"
                    enabled: page.step !== "keys" || keyView.canContinue === true
                    onClicked: firstRun.next()
                }
            }
        }
    }
}
