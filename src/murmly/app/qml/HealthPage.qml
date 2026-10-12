import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Shapes

// Health: what `murmly doctor` found, one check per row. Each row is a plain
// sentence with the raw key and value it came from beside it, in mono. Anything
// doctor wrote itself is shown exactly as written. The controller's `view`
// already holds every string; this only lays them out.
Item {
    id: page

    property string title: ""
    readonly property var view: health.view
    readonly property var rows: view.rows !== undefined ? view.rows : []

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
            spacing: theme.space.space26

            // The verdict in a heading and a sentence, and the two actions.
            Flow {
                Layout.fillWidth: true
                spacing: theme.space.space16

                ColumnLayout {
                    width: Math.max(300, parent.width - actions.width - theme.space.space16)
                    spacing: theme.space.space10

                    AppText {
                        objectName: "health-headline"
                        role: "title26"
                        text: view.headline !== undefined ? view.headline : ""
                        color: theme.colors.text
                        Layout.fillWidth: true
                        Accessible.role: Accessible.Heading
                    }

                    AppText {
                        objectName: "health-body"
                        role: "bodyLead"
                        text: view.body !== undefined ? view.body : ""
                        color: theme.colors.text3
                        Layout.fillWidth: true
                        Layout.maximumWidth: 580
                    }
                }

                Row {
                    id: actions
                    spacing: theme.space.space8

                    AppButton {
                        objectName: "copy-report"
                        kind: "quiet"
                        text: "Copy the full report"
                        enabled: view.canCopy === true
                        onClicked: health.copyReport()
                    }

                    AppButton {
                        objectName: "check-again"
                        text: view.checking === true ? "Checking…" : "Check again"
                        enabled: view.checking !== true
                        onClicked: health.check()
                    }
                }
            }

            // A run that produced no report: what it said, exactly.
            Rectangle {
                objectName: "health-error-card"
                visible: view.state === "failed"
                Layout.fillWidth: true
                implicitHeight: errorText.implicitHeight + theme.space.space18 * 2
                radius: theme.radii.r14
                color: theme.colors.needsYouBandBg
                border.width: 1
                border.color: theme.colors.needsYouBandBorder

                AppText {
                    id: errorText
                    objectName: "health-error"
                    anchors.fill: parent
                    anchors.margins: theme.space.space18
                    role: "monoMeta"
                    text: view.error !== undefined ? view.error : ""
                    color: theme.colors.needsYouText
                    wrapMode: Text.Wrap
                }
            }

            // The checks.
            Rectangle {
                objectName: "health-checks"
                visible: page.rows.length > 0
                Layout.fillWidth: true
                implicitHeight: checks.implicitHeight + 2
                radius: theme.radii.r13
                color: theme.colors.panel
                border.width: 1
                border.color: theme.colors.borderSubtle
                clip: true

                ColumnLayout {
                    id: checks
                    anchors.fill: parent
                    anchors.margins: 1
                    spacing: 0

                    Repeater {
                        model: page.rows

                        Rectangle {
                            id: row
                            objectName: "health-row-" + index
                            required property var modelData
                            required property int index
                            property bool expanded: false
                            readonly property string tone: modelData.tone

                            Layout.fillWidth: true
                            implicitHeight: body.implicitHeight + theme.space.space14 * 2
                            color: tone === "amber" ? theme.colors.noteBandBg
                                : tone === "crimson" ? theme.colors.needsYouBandBg
                                : "transparent"
                            Accessible.role: Accessible.StaticText
                            Accessible.name: modelData.title + ". " + modelData.sentence
                            Accessible.description: tone === "crimson" ? "Needs you"
                                : tone === "amber" ? "Note" : "Fine"

                            Rectangle {
                                anchors.bottom: parent.bottom
                                width: parent.width
                                height: 1
                                color: theme.colors.divider
                                visible: row.index < page.rows.length - 1
                            }

                            RowLayout {
                                id: body
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.top: parent.top
                                anchors.leftMargin: theme.space.space20
                                anchors.rightMargin: theme.space.space20
                                anchors.topMargin: theme.space.space14
                                spacing: theme.space.space14

                                // The mark: a check when settled, an amber triangle for
                                // a note, a crimson ring (never filled) for what needs you.
                                Item {
                                    objectName: "row-marker-" + row.index
                                    Layout.preferredWidth: 16
                                    Layout.preferredHeight: 16
                                    Layout.alignment: Qt.AlignTop
                                    Layout.topMargin: 2
                                    Accessible.ignored: true

                                    Shape {
                                        anchors.fill: parent
                                        visible: row.tone === "none"
                                        ShapePath {
                                            strokeColor: theme.colors.text3
                                            strokeWidth: 1.6
                                            fillColor: "transparent"
                                            capStyle: ShapePath.RoundCap
                                            joinStyle: ShapePath.RoundJoin
                                            startX: 3.5
                                            startY: 8.4
                                            PathLine { x: 6.6; y: 11.3 }
                                            PathLine { x: 12.5; y: 4.9 }
                                        }
                                    }

                                    AppText {
                                        anchors.centerIn: parent
                                        visible: row.tone === "amber"
                                        role: "helper"
                                        text: "▲"
                                        color: theme.colors.note
                                        wrapMode: Text.NoWrap
                                    }

                                    Rectangle {
                                        anchors.centerIn: parent
                                        visible: row.tone === "crimson"
                                        width: 11
                                        height: 11
                                        radius: 5.5
                                        color: "transparent"
                                        border.width: 2
                                        border.color: theme.colors.needsYou
                                    }
                                }

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 4

                                    Flow {
                                        id: line
                                        Layout.fillWidth: true
                                        spacing: 10

                                        AppText {
                                            id: rowTitle
                                            objectName: "row-title-" + row.index
                                            role: "row"
                                            text: row.modelData.title
                                            color: theme.colors.textBright
                                            wrapMode: Text.NoWrap
                                        }

                                        AppText {
                                            objectName: "row-sentence-" + row.index
                                            role: "body"
                                            text: row.modelData.sentence
                                            color: row.tone === "none" ? theme.colors.text4 : theme.colors.text3
                                            width: line.width - rowTitle.implicitWidth - line.spacing >= 240
                                                ? line.width - rowTitle.implicitWidth - line.spacing
                                                : line.width
                                        }
                                    }

                                    AppText {
                                        objectName: "row-raw-" + row.index
                                        role: "monoFootnote"
                                        text: row.modelData.raw
                                        color: theme.colors.text5
                                        wrapMode: Text.WrapAtWordBoundaryOrAnywhere
                                        Layout.fillWidth: true
                                    }

                                    ColumnLayout {
                                        objectName: "row-values-" + row.index
                                        visible: row.expanded
                                        Layout.fillWidth: true
                                        Layout.topMargin: theme.space.space6
                                        spacing: 2

                                        Repeater {
                                            model: row.expanded ? row.modelData.every : []

                                            AppText {
                                                required property var modelData
                                                role: "monoFootnote"
                                                text: modelData.key + ": " + modelData.value
                                                color: theme.colors.text5
                                                wrapMode: Text.WrapAtWordBoundaryOrAnywhere
                                                Layout.fillWidth: true
                                            }
                                        }
                                    }
                                }

                                AppButton {
                                    objectName: "row-all-" + row.index
                                    visible: row.modelData.every.length > row.modelData.pairs.length
                                    kind: "quiet"
                                    text: row.expanded ? "Hide values" : "All values"
                                    Layout.alignment: Qt.AlignTop
                                    onClicked: row.expanded = !row.expanded
                                }
                            }
                        }
                    }
                }
            }

            // What to do when something is wrong.
            Rectangle {
                objectName: "health-help"
                Layout.fillWidth: true
                implicitHeight: helpColumn.implicitHeight + theme.space.space18 * 2
                radius: theme.radii.r13
                color: theme.colors.panelAlt
                border.width: 1
                border.color: theme.colors.borderSubtle

                ColumnLayout {
                    id: helpColumn
                    anchors.fill: parent
                    anchors.margins: theme.space.space20
                    anchors.topMargin: theme.space.space18
                    anchors.bottomMargin: theme.space.space18
                    spacing: theme.space.space12

                    AppText {
                        role: "label"
                        text: "If something stops working"
                        color: theme.colors.textLabel
                        Accessible.role: Accessible.Heading
                    }

                    AppText {
                        role: "body"
                        text: "Copy the full report and keep it with your question. It names your desktop and the tools murmly uses, and never includes anything you said. The manual\u2019s troubleshooting page lists the usual causes."
                        color: theme.colors.text3
                        Layout.fillWidth: true
                    }
                }
            }
        }
    }
}
