---
title: Three ways a QML control loses or skips the keyboard focus
description: KeyNavigation also sets the reverse link, a Repeater over a list rebuilds the focused row, and a disabled control drops the focus without a trace
trigger: KeyNavigation.tab, Shift+Tab, Backtab, Repeater model, activeFocusItem, visualFocus, focusReason, ChoiceCard, Segmented, FocusFlickable
depends_on: src/murmly/app/qml/FirstRunPage.qml, src/murmly/app/qml/Segmented.qml, src/murmly/app/qml/FocusFlickable.qml, src/murmly/app/qml/SettingsPage.qml, tests/test_app_keyboard_qt.py
recorded: 2026-10-13
---

## KeyNavigation.tab also sets the target's backtab

`KeyNavigation.tab: later` on one item makes `later` go back to that item on
Shift+Tab, even when the item is hidden. First run's "Try it here" box did this, so
on the second step Shift+Tab from "Set up later" went to a box that was not showing
and stayed where it was. Leave the order to the window (items are visited in the
order they are laid out) and use `KeyNavigation` only where it has to be, and then
only for a target that is always shown. A text box that must not type a tab uses
`Keys.priority: Keys.BeforeItem` and `Keys.onTabPressed` instead.

## A Repeater over a list rebuilds the row that has the focus

The controller hands QML a new list on every change, so a `Repeater` whose model is
that list destroys and recreates every row, and the row that was just chosen with
Space takes the focus with it. Give the Repeater a count and let each row read its
entry by index (`TrayPanel.qml`, `Segmented.qml`, the choice cards):

```qml
Repeater {
    model: list.length
    Row { required property int index; readonly property var modelData: list[index] }
}
```

## The focus ring needs a Tab reason, and a disabled control drops the focus first

`item.focus = true` moves the focus but leaves `focusReason` at "other", so
`visualFocus` is false and no ring is drawn. Set `item.focusReason =
Qt.TabFocusReason` after it. When a button disables itself (the text size's "+" at
its limit), `onEnabledChanged` sees `activeFocus` already false; hand the focus on in
`onActiveFocusChanged` with `!activeFocus && !enabled`.

Test it with `window.activeFocusItem().objectName()` after the key, as
`tests/test_app_keyboard_qt.py` does, and check `visualFocus` as well.
