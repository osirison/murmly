import QtQuick
import QtQuick.Shapes

// The mark: a speech bubble holding three voice bars.
Shape {
    implicitWidth: 20
    implicitHeight: 20
    opacity: theme.scheme === "light" ? 0.65 : 0.75
    Accessible.ignored: true

    ShapePath {
        fillColor: theme.colors.textBright
        strokeColor: "transparent"
        fillRule: ShapePath.OddEvenFill
        scale: Qt.size(20 / 32, 20 / 32)
        PathSvg {
            path: "M8,2H24A6,6 0 0 1 30,8V17A6,6 0 0 1 24,23H14.6C14.6,26.1 13.1,28.4 9.6,29.8C10.9,27.9 11.2,25.6 11.2,23H8A6,6 0 0 1 2,17V8A6,6 0 0 1 8,2ZM9.3,7.2H9.3A2,2 0 0 1 11.3,9.2V15.8A2,2 0 0 1 9.3,17.8H9.3A2,2 0 0 1 7.3,15.8V9.2A2,2 0 0 1 9.3,7.2ZM16.9,5.2H16.9A2,2 0 0 1 18.9,7.2V17.8A2,2 0 0 1 16.9,19.8H16.9A2,2 0 0 1 14.9,17.8V7.2A2,2 0 0 1 16.9,5.2ZM23.6,4.2H23.6A1.1,1.1 0 0 1 24.7,5.3V19.7A1.1,1.1 0 0 1 23.6,20.8H23.6A1.1,1.1 0 0 1 22.5,19.7V5.3A1.1,1.1 0 0 1 23.6,4.2Z"
        }
    }
}
