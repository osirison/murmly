---
title: A QML key recorder needs a plain item to hold the focus, and a context name its page does not use
description: Space never reaches a recorder whose focus sits on a button inside it, and a context property is hidden by a same-named property on the page
trigger: KeyField.qml, hotkeys, QTest.keyClick, forceActiveFocus, "Cannot read property 'note' of undefined"
depends_on: src/murmly/app/qml/KeyField.qml, src/murmly/app/qml/SettingsPage.qml, src/murmly/app/qt.py, tests/test_app_keys_qt.py
recorded: 2026-10-12
---

## Space and Enter vanish while recording

The recorder was a `FocusScope` holding both the key handler and the Change
button. Clicking Change gave the button the focus, and the scope then reported
`activeFocus` as true because a child of it had the focus. Every key but Space and
Enter reached the scope's `Keys.onPressed`; those two went to the button, which
took them as a click and accepted them. A combination ending in Space (the
suggested `Meta+Shift+Space`) was silently never recorded, while `Meta+X` worked.

Give the focus to a plain `Item` that carries `Keys.onPressed`, and read the
field's focus from that item, not from the enclosing scope:

```qml
Item { id: target; onActiveFocusChanged: hotkeys.fieldFocusChanged(purpose, activeFocus) }
```

Check it by looking at `window.activeFocusItem().objectName()` in a test: if it
names a button, the keys are going to the button.

## A context property is hidden by a page's own property

`SettingsPage.qml` already had `readonly property var keys: view.keys`, so a
context property also named `keys` read as that map inside the page and every
binding on it failed with "Cannot read property ... of undefined" (the tests'
Qt message log turns each into a failure). The controller is exposed to QML as
`hotkeys` for that reason. Look at a page's own properties before choosing a
context name.

## Moving the focus without `forceActiveFocus`, and the Loader

`test_app_shell` fails on the text `forceActiveFocus` (and `raise(`,
`requestActivate`, `alert(`) in any QML file, so a field takes the focus with
`target.focus = true`. That only works when every focus scope above it already
holds the focus, and a `Loader` is a focus scope of its own: `focus = true` on an
item inside a Loader that does not hold the focus makes no item active. First
run is in a Loader, so the Loader declares `focus: active` and its key field then
takes the focus when clicked. Settings' fields are reached through a button the
person clicked, which already put the Loader's chain in focus. A test that clicks
the field first, with nothing focused, shows the difference.

## How the tests press keys

`QTest.keyClick(window, key, modifiers)` first delivers a press for each modifier
(Shift, then Meta), then the key. The recorder sees those as "a modifier on its
own" and keeps waiting, which is also what a person's real key presses do. Only
`QTest.keyClick` with a `Qt.Key` works on a `QQuickWindow`; `keyClicks` takes a
widget.
