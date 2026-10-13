---
title: QML Accessible.checked is overwritten by the button's own checked on accessibility activation
description: Qt 6.11's QQuickAbstractButton overwrites the attached Accessible.checked/checkable when accessibility activates or a screen reader turns on
trigger: QML Accessible.checked, Accessible.checkable, AbstractButton, ToggleSwitch, ChoiceCard, Segmented, NavButton, SubNavButton, screen readers
depends_on: src/murmly/app/qml/ToggleSwitch.qml, src/murmly/app/qml/ChoiceCard.qml, src/murmly/app/qml/Segmented.qml, src/murmly/app/qml/NavButton.qml, src/murmly/app/qml/SubNavButton.qml, tests/test_app_accessibility_qt.py, .github/workflows/tests.yml, "#98"
recorded: 2026-10-13
---

## What happens

Qt 6.11's `QQuickAbstractButton::accessibilityActiveChanged(true)` runs when
accessibility is active at build time and again when a screen reader turns it on
later. It overwrites the attached `Accessible.checked` and `Accessible.checkable`
from the button's own `checked` and `checkable` properties.

Per Qt's Windows UIA source (`qwindowsuiamainprovider.cpp`), UIA offers the Toggle
pattern only when checkable is set, so a control whose button is not checkable
loses the Toggle pattern.

## What to do

Drive the button's own `checkable: true` and `checked: <state>`, and restore the
binding in `onToggled`. `ChoiceCard.qml`, `NavButton.qml` and `Segmented.qml` do it
this way:

```qml
onToggled: checked = Qt.binding(function () { return selected })
```

Side effect: each click flips checked and restores it within the event, so a screen
reader can receive a wrong-then-right pair. There is no clean fix in Qt 6.11.

Test both activation orders: `QAccessible.setActive(true)` before the window is
built, and after. `tests/test_app_accessibility_qt.py` covers the controls.

See #98 for the pattern in context.
