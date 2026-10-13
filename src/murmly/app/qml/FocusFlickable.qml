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

    Connections {
        target: view.Window.window

        function onActiveFocusItemChanged() {
            view.reveal(view.Window.window.activeFocusItem)
        }
    }
}
