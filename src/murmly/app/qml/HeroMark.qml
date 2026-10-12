import QtQuick
import QtQuick.Shapes

// The speech bubble holding three voice bars, drawn in the state's colour.
// Static: nothing animates until the platform's reduced-motion setting can be read.
Item {
    id: mark

    property string tone: "neutral"
    property string badge: ""

    implicitWidth: 88
    implicitHeight: 88
    Accessible.ignored: true

    readonly property bool alert: tone === "crimson"
    readonly property color outline: alert ? theme.colors.needsYou
        : tone === "warm" ? theme.colors.listeningLabel
        : tone === "cool" ? theme.colors.writingLabel
        : theme.colors.borderStrong
    readonly property color barColor: tone === "warm" ? theme.colors.listeningLabel
        : tone === "cool" ? theme.colors.writingLabel
        : theme.colors.textBright

    Shape {
        anchors.fill: parent
        preferredRendererType: Shape.CurveRenderer

        ShapePath {
            fillColor: "transparent"
            strokeColor: mark.outline
            strokeWidth: 1.1 * 88 / 32
            joinStyle: ShapePath.RoundJoin
            scale: Qt.size(88 / 32, 88 / 32)
            PathSvg {
                path: "M8,2H24A6,6 0 0 1 30,8V17A6,6 0 0 1 24,23H14.6C14.6,26.1 13.1,28.4 9.6,29.8C10.9,27.9 11.2,25.6 11.2,23H8A6,6 0 0 1 2,17V8A6,6 0 0 1 8,2Z"
            }
        }
    }

    Repeater {
        model: mark.alert ? [] : [
            { x: 8.6, y: 9.5, h: 6 },
            { x: 14.8, y: 6.5, h: 12 },
            { x: 21, y: 8.5, h: 8 }
        ]

        Rectangle {
            x: modelData.x * 88 / 32
            y: modelData.y * 88 / 32
            width: 2.4 * 88 / 32
            height: modelData.h * 88 / 32
            radius: width / 2
            color: mark.barColor
        }
    }

    AppText {
        visible: mark.alert && mark.badge !== ""
        anchors.horizontalCenter: parent.horizontalCenter
        y: 15.6 * 88 / 32 - height * 0.72
        role: "monoNumeral"
        text: mark.badge
        color: theme.colors.needsYouText
        wrapMode: Text.NoWrap
    }
}
