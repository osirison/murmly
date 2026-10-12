---
title: A QML Popup is not an Item, so it carries no Accessible and a test cannot find it in the item tree
description: Accessible on a Popup logs a QML warning that fails the test's message log, and the dialog's own object is missing from a walk of childItems
trigger: RemoveDialog.qml, Popup, "Accessible attached property must be attached to an object deriving from Item or Action", findChild, remove-dialog
depends_on: src/murmly/app/qml/RemoveDialog.qml, tests/test_app_removal_qt.py
recorded: 2026-10-12
---

## What happens

`Accessible.role` or `Accessible.name` written on a `Popup` logs

```
QML RemoveDialog: Accessible attached property must be attached to an object deriving from Item or Action
```

Nothing visible breaks, but every Qt test builds a `MessageLog` that fails on any
warning, so every test that loads the window fails at cleanup with that line.

The popup's own object is also not in the window's item tree. A helper that walks
`window.contentItem().childItems()` finds the popup's contents (they are items,
parented to the overlay) but never the popup, so `child("remove-dialog")` fails.

## What to do

Put `Accessible.role: Accessible.Dialog` and `Accessible.name` on the popup's
`contentItem`, which is an item. Find the popup itself as an object:

```python
from PySide6.QtCore import QObject
dialog = window.findChild(QObject, "remove-dialog")
dialog.property("opened")
```

## A dialog taller than the window

A modal popup sized by its content overflows a window at the app's 480 px minimum
height and cuts the buttons off. Bound its height to the window's and put the list
in a `Flickable`, with the buttons outside it, as `RemoveDialog.qml` does.
The Settings group list needed the same: a `ColumnLayout` of entries has no scroll,
so the last entry was off screen at that height until it was wrapped in a `Flickable`.
A mouse click on an item clipped out of view lands on nothing; use the item's
`click()` in a script that has to reach it.
