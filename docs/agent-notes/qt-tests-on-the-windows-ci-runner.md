---
title: Qt tests on the Windows CI runner have a ~1028 px screen and timing quirks
description: window.resize(1040, ...) warns and fails the suite; the settings UI shows an asked value before the worker writes it; imports must be lazy
trigger: test_app_*_qt.py, window.resize, QWindowsWindow::setGeometry, unittest (Windows, Python 3.12), PySide6
depends_on: tests/test_app_keyboard_qt.py, tests/test_app_tray_qt.py, .github/workflows/tests.yml
recorded: 2026-10-13
---

## Screen size and warnings

The Windows CI runner's screen is about 1028 px wide. A call to `window.resize(1040, ...)`
makes Qt print

```
QWindowsWindow::setGeometry: Unable to set geometry
```

A shared message log turns that warning into a failure. Use the `resize_window`
helper in `tests/test_app_keyboard_qt.py`, which clamps resize values to the screen
dimensions.

## Settings UI shows asked value before the worker writes it

Do not use the visible UI state as proof that a save reached the file. The settings
controller shows the asked value in the view before the worker thread writes it
to disk (#96). Instead, wait on a write count — use the `CountingWriter` pattern
in `tests/test_app_tray_qt.py` — or read the view's status line after waiting for
the worker.

## Qt test modules must import PySide6 lazily

Only the Windows Python 3.12 job installs PySide6. Qt test modules must wrap the
PySide6 import at module level so the module skips cleanly on other jobs:

```python
try:
    from PySide6 import ...
except ImportError:
    def test_something(self):
        self.skipTest("PySide6 not available")
```

Linux passing proves little about timing and rendering. Always check the Windows
job before shipping a Qt change.
