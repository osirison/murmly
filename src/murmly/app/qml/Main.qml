import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Shapes

ApplicationWindow {
    id: window

    property int current: 0

    objectName: "shell"
    title: "Murmly"
    // The canvas's window size, unless the screen is smaller: a window asked to
    // be larger than its screen makes some platforms warn and resize it anyway.
    width: Math.min(theme.layout.wWindow, Screen.desktopAvailableWidth)
    height: Math.min(theme.layout.hWindow, Screen.desktopAvailableHeight)
    minimumWidth: 640
    minimumHeight: 480
    visible: true
    color: theme.colors.surface

    // Polling the daemon runs only while a person can see the window. The tray
    // panel reports itself the same way, as the surface "tray".
    readonly property bool onScreen: visible && visibility !== Window.Minimized
        && visibility !== Window.Hidden
    onOnScreenChanged: home.setSurfaceVisible("window", onScreen)
    Component.onCompleted: home.setSurfaceVisible("window", onScreen)

    // The checks run when Health is the page a person is looking at, and not
    // before: running them opens the audio devices.
    readonly property bool healthShowing: onScreen && sections[current].id === "health"
    onHealthShowingChanged: health.setActive(healthShowing)

    header: Rectangle {
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

                Shape {
                    // The mark: a speech bubble holding three voice bars.
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

                AppText {
                    role: "title14"
                    text: "murmly"
                    color: theme.colors.textBright
                    wrapMode: Text.NoWrap
                }
            }

            RowLayout {
                spacing: 2

                Repeater {
                    model: sections

                    NavButton {
                        objectName: "nav-" + modelData.id
                        text: modelData.title
                        selected: window.current === index
                        onClicked: window.current = index
                    }
                }
            }

            Item { Layout.fillWidth: true }

            StateChip {
                objectName: "state-chip"
                visible: text !== ""
                text: home.view.chip !== undefined ? home.view.chip : ""
                tone: home.view.tone !== undefined ? home.view.tone : "neutral"
            }
        }
    }

    StackLayout {
        anchors.fill: parent
        currentIndex: window.current

        Repeater {
            model: sections

            // Home and Health are their own pages; the other sections are empty for now.
            Loader {
                objectName: "page-" + modelData.id
                property string title: modelData.title
                source: modelData.id === "home" ? "HomePage.qml"
                    : modelData.id === "health" ? "HealthPage.qml"
                    : "SectionPage.qml"
                onLoaded: item.title = title
            }
        }
    }
}
