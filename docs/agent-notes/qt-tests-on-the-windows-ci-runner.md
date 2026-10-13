---
title: Qt tests on the Windows CI runner have a ~1028 px screen and timing quirks
description: window.resize(1040, ...) warns and fails the suite; the settings UI shows an asked value before the worker writes it; only one CI job has PySide6
trigger: test_app_*_qt.py, window.resize, QWindowsWindow::setGeometry, unittest (Windows, Python 3.12), PySide6
depends_on: tests/test_app_keyboard_qt.py, tests/test_app_tray_qt.py, tests/qt_app.py, .github/workflows/tests.yml
recorded: 2026-10-13
---

## Screen size and warnings

The Windows CI runner's screen is 1028 px wide. The CI log for #97 showed
`QWindowsWindow::setGeometry: Unable to set geometry 1040x480 ... Resulting geometry: 1028x480`.
A shared message log (`MessageLog` in `tests/qt_app.py`) turns that warning into a
failure. Use the `resize_window` helper in `tests/test_app_keyboard_qt.py`, which
clamps resize values to the screen size. Its docstring assumes 1024 px, a safe
figure; the observed width was 1028.

## Settings UI shows asked value before the worker writes it

Do not use the visible UI state as proof that a save reached the file. The settings
controller shows the asked value in the view before the worker thread writes it
to disk (#96). Instead, wait on a write count — use the `CountingWriter` pattern
in `tests/test_app_tray_qt.py` — or read the view's status line after waiting for
the worker.

## PySide6 is installed in one job only

Only the "unittest (Windows, Python 3.12)" job installs PySide6 (`--extra overlay
--extra gui` in `tests.yml`). Qt tests import PySide6 inside test methods and
helpers, not at module level, and take the application from `qt_application` in
`tests/qt_app.py`, called in `setUpClass`. It skips the test when PySide6 is missing.

Linux passing proves little about timing and rendering. Always check the Windows
job before shipping a Qt change.
