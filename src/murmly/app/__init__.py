"""The desktop app: a Qt Quick client of the daemon's command socket.

A separate process from the daemon, so a broken or absent GUI cannot affect
dictation, and the daemon never imports Qt. PySide6 comes from the optional
`gui` extra and is imported only inside `run`, so importing this package -- and
everything else in Murmly -- works without it.
"""

from __future__ import annotations

APP_TITLE = "Murmly"

_WINDOW_QML = f"""
import QtQuick
import QtQuick.Window

Window {{
    title: "{APP_TITLE}"
    width: 960
    height: 640
    visible: true
}}
""".encode()


def run() -> int:
    """Show the app window and return its exit status.

    Raises ModuleNotFoundError naming PySide6 when the `gui` extra is not
    installed; the caller turns that into the message a person can act on.
    """
    import sys

    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine

    application = QGuiApplication(sys.argv[:1])
    application.setApplicationName(APP_TITLE)
    engine = QQmlApplicationEngine()
    engine.loadData(_WINDOW_QML)
    if not engine.rootObjects():
        print("murmly: the app window could not be created.", file=sys.stderr)
        return 1
    return application.exec()
