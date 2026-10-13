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

    // Settings reads the file when it is shown, and watches it for hand edits while it is.
    // First run reads it too, for the accuracy choice.
    readonly property bool firstRunShowing: firstRun.view.visible === true
    readonly property bool settingsShowing: onScreen && (firstRunShowing || sections[current].id === "settings")
    onSettingsShowingChanged: settings.setActive(settingsShowing)

    // Closing first run takes the focus with it. The first control of the window
    // is where Tab would have gone next; it is marked as a Tab would mark it.
    onFirstRunShowingChanged: if (!firstRunShowing) Qt.callLater(function () {
        var first = navigation.itemAt(0)
        first.focus = true
        first.focusReason = Qt.TabFocusReason
    })

    // First run has a header of its own, so this one is not shown behind it.
    header: Rectangle {
        visible: !window.firstRunShowing
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

            RowLayout {
                spacing: 2

                Repeater {
                    id: navigation
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
        // Nothing behind first run can be reached by Tab while it covers the window.
        enabled: !window.firstRunShowing

        Repeater {
            model: sections

            // Home, Settings and Health are their own pages.
            Loader {
                objectName: "page-" + modelData.id
                property string title: modelData.title
                source: modelData.id === "home" ? "HomePage.qml"
                    : modelData.id === "settings" ? "SettingsPage.qml"
                    : modelData.id === "health" ? "HealthPage.qml"
                    : "SectionPage.qml"
                onLoaded: item.title = title
            }
        }
    }

    // First run: a layer in this window, shown once from the start and never in
    // response to anything the daemon reports.
    Loader {
        objectName: "first-run"
        anchors.fill: parent
        active: window.firstRunShowing
        source: "FirstRunPage.qml"
        // A Loader is a focus scope of its own. Taking the focus within this window
        // when first run opens lets its key field take it in turn when clicked; it
        // never activates or raises the window.
        focus: active
    }

    // Removing murmly: opens only when the person asks for it in Settings.
    RemoveDialog {}
}
