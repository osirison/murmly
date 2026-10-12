"""The Qt application the desktop app's tests share.

Kept out of any `test_*` module so the theme, font and shell tests build the
application the same way. Every test that needs Qt goes through `qt_application`,
which returns the process's one `QApplication`: a bare `QGuiApplication` made
first would leave a later widget test (the overlay's, on Windows) without the
`QApplication` it needs, and abort the whole process.
"""

from __future__ import annotations

import os
import sys
import unittest

# A message Qt prints on its own platforms and none of this code's: the
# offscreen and X11 platform plugins cannot honour a size hint, the offscreen one
# has no system tray signal for a tray icon to connect to and cannot raise a
# window (the tray's "Open murmly" does, on a person's click).
_PLATFORM_NOISE = ("propagateSizeHints", "systemTrayWindowChanged", "does not support raise()")


def read_text_retrying(path, attempts: int = 50) -> str:
    """The file's text, trying again while Windows has it locked for a swap.

    A test that reads the configuration file every few milliseconds while the
    app replaces it can hold it open at the moment of the swap, and then reads,
    or the swap, fails with PermissionError on Windows. Tests read through this.
    """
    import time

    for attempt in range(attempts):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.02)
    raise AssertionError("unreachable")


def qt_application():
    """The process's QApplication, or skip the calling test when Qt is not usable."""
    try:
        import PySide6  # noqa: F401
    except ModuleNotFoundError as error:
        if (error.name or "").partition(".")[0] != "PySide6":
            raise
        raise unittest.SkipTest("PySide6 is not installed. Run `uv sync --extra gui`.")
    if sys.platform != "win32":
        # No display needed. Windows keeps its real platform: the overlay's own
        # tests share this process and check real window styles.
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication(["murmly-test"])


class MessageLog:
    """Everything Qt prints at warning level or above while it is installed."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self._previous = None

    def __enter__(self) -> "MessageLog":
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler

        def handler(kind, context, message) -> None:
            if kind == QtMsgType.QtDebugMsg or kind == QtMsgType.QtInfoMsg:
                return
            if any(noise in message for noise in _PLATFORM_NOISE):
                return
            self.messages.append(message)

        self._previous = qInstallMessageHandler(handler)
        return self

    def __exit__(self, *exc) -> None:
        from PySide6.QtCore import qInstallMessageHandler

        qInstallMessageHandler(self._previous)
