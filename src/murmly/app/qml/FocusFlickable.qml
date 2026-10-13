import QtQuick

// A Flickable that scrolls to keep the control holding the keyboard focus in
// view, so Tab never moves the focus off screen. `margin` is left around the
// control, enough for its focus ring. It jumps rather than glides, which needs no
// reduced-motion variant.
Flickable {
    id: view

    property real margin: 12

    clip: true
    boundsBehavior: Flickable.StopAtBounds

    function reveal(item) {
        if (!item || !visible)
            return
        var inside = false
        for (var parent = item.parent; parent; parent = parent.parent) {
            if (parent === contentItem) {
                inside = true
                break
            }
        }
        if (!inside)
            return
        var top = item.mapToItem(contentItem, 0, 0).y - margin
        var bottom = top + item.height + margin * 2
        var y = contentY
        if (bottom > y + height)
            y = bottom - height
        // A control taller than the view shows its top.
        if (top < y)
            y = top
        contentY = Math.max(0, Math.min(y, contentHeight - height))
    }

    // The control just revealed, for a moment, while the layout around it may still
    // be moving (a pane laid out late, a banner appearing): it is revealed again
    // after each change. Any scrolling by the person ends this at once.
    property Item following: null

    function follow(item) {
        following = item
        settling.restart()
    }

    function stay() {
        if (following && following.activeFocus)
            reveal(following)
        else
            following = null
    }

    onContentHeightChanged: stay()
    onMovementStarted: {
        following = null
        settling.stop()
    }

    Timer {
        id: settling
        interval: 400
        onTriggered: view.following = null
    }

    Connections {
        target: view.Window.window

        function onActiveFocusItemChanged() {
            var item = view.Window.window.activeFocusItem
            view.reveal(item)
            view.follow(item)
        }
    }

    // After the layout has been redone for the frame, which is when a control's
    // place can be trusted.
    Connections {
        target: view.following ? view.Window.window : null

        function onAfterAnimating() {
            view.stay()
        }
    }
}
