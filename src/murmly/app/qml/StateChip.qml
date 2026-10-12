import QtQuick
import QtQuick.Layouts

// The daemon's state in the header: a dot and a few mono words. `tone` is
// "neutral", "warm", "cool" or "crimson"; crimson is a ring, never a fill.
Rectangle {
    id: chip

    property string text: ""
    property string tone: "neutral"
    readonly property bool alert: tone === "crimson"

    implicitWidth: row.implicitWidth + theme.space.space12 * 2
    implicitHeight: row.implicitHeight + 10
    radius: theme.radii.rPill
    color: "transparent"
    border.width: 1
    border.color: alert ? theme.colors.needsYouChipBorder : theme.colors.border
    Accessible.role: Accessible.StaticText
    Accessible.name: text

    RowLayout {
        id: row
        anchors.centerIn: parent
        spacing: 7

        Rectangle {
            Layout.preferredWidth: chip.alert ? 8 : 7
            Layout.preferredHeight: chip.alert ? 8 : 7
            radius: width / 2
            color: chip.alert ? "transparent"
                : chip.tone === "warm" ? theme.colors.listeningLabel
                : chip.tone === "cool" ? theme.colors.writingLabel
                : theme.colors.dotInert
            border.width: chip.alert ? 2 : 0
            border.color: theme.colors.needsYou
        }

        AppText {
            objectName: "chip-text"
            role: "monoCaption"
            text: chip.text
            wrapMode: Text.NoWrap
            color: chip.alert ? theme.colors.needsYouText : theme.colors.text4
        }
    }
}
