---
title: A Qt test must create a QApplication, never a bare QGuiApplication
description: A QGuiApplication made by one test aborts the whole Windows test run when a later test makes a QWidget
trigger: test_overlay_renderer_qt, test_app_, QGuiApplication, QApplication, "Process completed with exit code 1", "QWidget: Cannot create a QWidget without QApplication"
depends_on: tests/qt_app.py, tests/test_overlay_renderer_qt.py, tests/test_app_*.py, .github/workflows/tests.yml
recorded: 2026-10-12
---

## What happens

One test builds a `QGuiApplication`. A later test, in the same process, builds a
`QWidget` (the overlay renderer's tests do). Qt cannot make a widget without a
`QApplication`, and it does not raise: it prints

```
QWidget: Cannot create a QWidget without QApplication
```

and aborts the whole Python process. CI then shows the job failing with
`Process completed with exit code 1`, no traceback and no unittest summary, so
nothing names the test that was running or the test that caused it.

Only the job that installs PySide6 can hit this, which is the Windows Python 3.12
job. The other jobs skip every Qt test, so a green run anywhere else proves
nothing about it.

## What to do

Every test that needs Qt gets its application from `tests/qt_app.py`:

```python
from qt_app import qt_application

cls.application = qt_application()
```

which is `QApplication.instance() or QApplication(...)`. A `QApplication` is a
`QGuiApplication`, so tests that only need the GUI one are served by it too.
Never write `QGuiApplication(...)` in a test, and never create the application
in a `setUp` that runs before `qt_application`.

The app itself (`murmly.app.run`) does create a bare `QGuiApplication`, which is
right for a process that never shows a widget. Tests share a process, so they
cannot make the same choice.

## Checking

Run the Qt tests together in one process, app tests first, then the overlay's:

```bash
PYTHONPATH=src:tests python -m unittest test_app_shell test_overlay_renderer_qt
```

If the process ends with no summary and no traceback, look for a bare
`QGuiApplication` first.
