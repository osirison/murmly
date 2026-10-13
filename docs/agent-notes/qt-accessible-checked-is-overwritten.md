---
title: QML Accessible.checked is overwritten by the button's own checked on accessibility activation
description: Qt 6.11's QQuickAbstractButton overwrites the attached Accessible.checked/checkable when accessibility activates or a screen reader turns on
trigger: QML Accessible.checked, Accessible.checkable, AbstractButton, ToggleSwitch, ChoiceCard, Segmented, NavButton, SubNavButton, screen readers
depends_on: src/murmly/app/qml/ChoiceCard.qml, src/murmly/app/qml/Segmented.qml, .github/workflows/tests.yml, "#98"
recorded: 2026-10-13
---

## What happens

Qt 6.11's `QQuickAbstractButton::accessibilityActiveChanged(true)` runs when
accessibility is active at build time and again when a screen reader turns it on
later. It overwrites the attached `Accessible.checked` and `Accessible.checkable`
from the button's own `checked` and `checkable` properties.

On Windows, UIA offers the Toggle pattern only when checkable is set, so a control
that is checkable to the screen reader but not checked on the button loses the
Toggle pattern.

## What to do

Drive the button's own `checkable: true` and `checked: <state>`, and restore the
binding in `onToggled`:

```qml
AbstractButton {
    checkable: true
    checked: myState
    onToggled: checked = Qt.binding(() => myState)
}
```

Side effect: a click flips checked and restores it within the event, which may
send a screen reader a wrong-then-right pair. Test both activation orders:
`QAccessible.setActive(true)` before the window is built, and after.

See #98 for the pattern in context.
