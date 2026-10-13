import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Removing murmly, in one dialog that follows `removal.view`: first the
// confirmation, which names everything that will be removed (the list comes from
// what is installed, not from text here) and offers deleting settings and
// downloaded models as a separate switch that starts off; then "Removing murmly…",
// then what was removed, or why it could not be. Nothing is removed until the
// confirm button is pressed. It opens only because the person asked to remove
// murmly, and takes no focus from anything outside the window.
Popup {
    id: dialog
    objectName: "remove-dialog"

    readonly property var view: removal.view
    readonly property string step: view.step !== undefined ? view.step : "idle"
    readonly property bool wanted: view.open === true

    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(540, (parent ? parent.width : 540) - theme.space.space32)
    // Never taller than the window: the list inside scrolls instead.
    height: Math.min(implicitHeight, (parent ? parent.height : 600) - theme.space.space32)
    padding: theme.space.space24
    modal: true
    focus: true
    closePolicy: step === "confirming" || step === "partial" || step === "failed"
        ? Popup.CloseOnEscape : Popup.NoAutoClose

    // The controller decides when it is open; Escape asks it to cancel.
    onWantedChanged: {
        if (wanted)
            open()
        else
            close()
    }
    Component.onCompleted: {
        if (wanted)
            open()
    }
    onClosed: {
        if (wanted && (step === "confirming" || step === "partial" || step === "failed"))
            removal.cancel()
    }

    Overlay.modal: Rectangle { color: theme.colors.scrim }

    background: Rectangle {
        radius: theme.radii.r14
        color: theme.colors.panel
        border.width: 1
        border.color: theme.colors.borderStrong
    }

    contentItem: ColumnLayout {
        spacing: theme.space.space16
        Accessible.role: Accessible.Dialog
        Accessible.name: dialog.view.title !== undefined ? dialog.view.title : ""

        AppText {
            objectName: "remove-title"
            role: "title16"
            text: dialog.view.title !== undefined ? dialog.view.title : ""
            color: theme.colors.textBright
            Layout.fillWidth: true
            Accessible.role: Accessible.Heading
        }

        // The confirmation. What will be removed scrolls when the window is short;
        // the two buttons below it stay in view.
        FocusFlickable {
            id: body
            visible: dialog.step === "confirming"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredHeight: bodyColumn.implicitHeight
            contentWidth: width
            contentHeight: bodyColumn.implicitHeight
            ScrollBar.vertical: ScrollBar {}

            ColumnLayout {
                id: bodyColumn
                width: body.width
                spacing: theme.space.space16

            AppText {
                objectName: "remove-intro"
                role: "body"
                text: dialog.view.intro !== undefined ? dialog.view.intro : ""
                color: theme.colors.text3
                Layout.fillWidth: true
            }

            Rectangle {
                visible: items.count > 0
                Layout.fillWidth: true
                implicitHeight: itemColumn.implicitHeight + theme.space.space8 * 2
                radius: theme.radii.r11
                color: theme.colors.panelAlt
                border.width: 1
                border.color: theme.colors.borderSubtle

                ColumnLayout {
                    id: itemColumn
                    anchors.fill: parent
                    anchors.margins: theme.space.space8
                    anchors.leftMargin: theme.space.space14
                    anchors.rightMargin: theme.space.space14
                    spacing: 0

                    Repeater {
                        id: items
                        model: dialog.view.items !== undefined ? dialog.view.items : []

                        ColumnLayout {
                            objectName: "remove-item"
                            Layout.fillWidth: true
                            Layout.topMargin: theme.space.space8
                            Layout.bottomMargin: theme.space.space8
                            spacing: 2

                            AppText {
                                objectName: "remove-item-label"
                                role: "row"
                                text: modelData.label
                                color: theme.colors.textBright
                                Layout.fillWidth: true
                            }

                            AppText {
                                objectName: "remove-item-detail"
                                visible: modelData.detail !== ""
                                role: "monoCaption"
                                text: modelData.detail
                                color: theme.colors.text5
                                wrapMode: Text.WrapAnywhere
                                Layout.fillWidth: true
                            }
                        }
                    }
                }
            }

            // Deleting settings and models: its own choice, off until the person turns it on.
            Rectangle {
                Layout.fillWidth: true
                implicitHeight: purgeColumn.implicitHeight + theme.space.space14 * 2
                radius: theme.radii.r11
                color: dialog.view.purge === true ? theme.colors.destructiveCardBg : "transparent"
                border.width: 1
                border.color: dialog.view.purge === true
                    ? theme.colors.destructiveCardBorder : theme.colors.borderSubtle

                ColumnLayout {
                    id: purgeColumn
                    anchors.fill: parent
                    anchors.margins: theme.space.space14
                    spacing: theme.space.space10

                    RowLayout {
                        spacing: theme.space.space12
                        Layout.fillWidth: true

                        ToggleSwitch {
                            objectName: "remove-purge"
                            text: dialog.view.purgeTitle !== undefined ? dialog.view.purgeTitle : ""
                            on: dialog.view.purge === true
                            onClicked: removal.setPurge(!on)
                        }

                        AppText {
                            role: "title14"
                            text: dialog.view.purgeTitle !== undefined ? dialog.view.purgeTitle : ""
                            color: theme.colors.textBright
                            Layout.fillWidth: true
                        }
                    }

                    Repeater {
                        model: dialog.view.purgeTargets !== undefined ? dialog.view.purgeTargets : []

                        ColumnLayout {
                            objectName: "remove-target"
                            Layout.fillWidth: true
                            spacing: 1

                            AppText {
                                role: "helper"
                                text: modelData.label
                                color: theme.colors.text3
                                Layout.fillWidth: true
                            }

                            AppText {
                                objectName: "remove-target-path"
                                role: "monoCaption"
                                text: modelData.path
                                color: theme.colors.text5
                                wrapMode: Text.WrapAnywhere
                                Layout.fillWidth: true
                            }
                        }
                    }

                    AppText {
                        objectName: "remove-purge-footnote"
                        visible: text !== ""
                        role: "helper"
                        text: dialog.view.purgeFootnote !== undefined ? dialog.view.purgeFootnote : ""
                        color: theme.colors.text4
                        Layout.fillWidth: true
                    }

                    AppText {
                        objectName: "remove-purge-note"
                        visible: text !== ""
                        role: "body"
                        text: dialog.view.purgeNote !== undefined ? dialog.view.purgeNote : ""
                        color: theme.colors.destructiveText
                        Layout.fillWidth: true
                    }
                }
            }

            AppText {
                objectName: "remove-kept-note"
                visible: text !== ""
                role: "body"
                text: dialog.view.keptNote !== undefined ? dialog.view.keptNote : ""
                color: theme.colors.text4
                Layout.fillWidth: true
            }
            }   // bodyColumn
        }

        RowLayout {
            visible: dialog.step === "confirming"
            spacing: theme.space.space10
            Layout.fillWidth: true

            Item { Layout.fillWidth: true }

            AppButton {
                objectName: "remove-cancel"
                text: dialog.view.cancelLabel !== undefined ? dialog.view.cancelLabel : ""
                focus: dialog.step === "confirming"
                onClicked: removal.cancel()
            }

            AppButton {
                objectName: "remove-confirm"
                kind: "danger-solid"
                text: dialog.view.confirmLabel !== undefined ? dialog.view.confirmLabel : ""
                onClicked: removal.confirm()
            }
        }

        // Working.
        AppText {
            objectName: "remove-working"
            visible: dialog.step === "removing"
            role: "body"
            text: "This takes a moment. Murmly is stopped first, then what it set up is removed."
            color: theme.colors.text3
            Layout.fillWidth: true
        }

        // Done: what was removed. Partial: the same list, with what went wrong in it;
        // the app stays open, and its button only closes the dialog.
        ColumnLayout {
            visible: dialog.step === "done" || dialog.step === "partial"
            spacing: theme.space.space12
            Layout.fillWidth: true

            Repeater {
                model: dialog.view.report !== undefined ? dialog.view.report : []

                AppText {
                    objectName: "remove-report-line"
                    role: "body"
                    text: modelData
                    color: theme.colors.text
                    wrapMode: Text.WrapAnywhere
                    Layout.fillWidth: true
                }
            }

            AppText {
                objectName: "remove-done-note"
                role: "body"
                text: dialog.view.doneNote !== undefined ? dialog.view.doneNote : ""
                color: theme.colors.text4
                Layout.fillWidth: true
            }

            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }

                AppButton {
                    objectName: "remove-close"
                    kind: "primary"
                    text: dialog.view.doneLabel !== undefined ? dialog.view.doneLabel : ""
                    focus: dialog.step === "done" || dialog.step === "partial"
                    onClicked: dialog.step === "partial" ? removal.cancel() : removal.finish()
                }
            }
        }

        // Failed: its own words.
        ColumnLayout {
            visible: dialog.step === "failed"
            spacing: theme.space.space12
            Layout.fillWidth: true

            AppText {
                objectName: "remove-error"
                role: "monoMeta"
                text: dialog.view.error !== undefined ? dialog.view.error : ""
                color: theme.colors.needsYouText
                wrapMode: Text.WrapAnywhere
                Layout.fillWidth: true
            }

            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }

                AppButton {
                    objectName: "remove-failed-close"
                    text: dialog.view.failedLabel !== undefined ? dialog.view.failedLabel : ""
                    focus: dialog.step === "failed"
                    onClicked: removal.cancel()
                }
            }
        }
    }
}
