import QtQuick
import QtQuick.Layouts

// A message above the settings. `tone` is "crimson" (something needs the person:
// drawn as an outline, never a fill) or "amber" (a note). `mono` is for text that
// is not ours to reword, such as a parser's error.
Rectangle {
    id: banner

    property string tone: "amber"
    property bool mono: false
    property string title: ""
    property string text: ""
    property alias textItem: body

    Layout.fillWidth: true
    implicitHeight: column.implicitHeight + theme.space.space14 * 2
    radius: theme.radii.r13
    color: tone === "crimson" ? theme.colors.needsYouBandBg : theme.colors.noteBandBg
    border.width: 1
    border.color: tone === "crimson" ? theme.colors.needsYouBandBorder : theme.colors.noteBandBorder
    Accessible.role: Accessible.StaticText
    Accessible.name: title !== "" ? title + ". " + text : text

    ColumnLayout {
        id: column
        anchors.fill: parent
        anchors.margins: theme.space.space14
        anchors.leftMargin: theme.space.space18
        anchors.rightMargin: theme.space.space18
        spacing: 4

        AppText {
            visible: banner.title !== ""
            role: "bandTitle"
            text: banner.title
            color: theme.colors.text
            Layout.fillWidth: true
        }

        AppText {
            id: body
            role: banner.mono ? "monoMeta" : "body"
            text: banner.text
            color: banner.tone === "crimson" ? theme.colors.needsYouText : theme.colors.text3
            Layout.fillWidth: true
        }
    }
}
