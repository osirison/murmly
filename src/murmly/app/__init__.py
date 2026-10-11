"""The desktop app: a Qt Quick client of the daemon's command socket.

A separate process from the daemon, so a broken or absent GUI cannot affect
dictation, and the daemon never imports Qt. PySide6 comes from the optional
`gui` extra and is imported only inside `run`, so importing this package -- and
everything else in Murmly -- works without it.
"""

from __future__ import annotations

APP_TITLE = "Murmly"


def run() -> int:
    """Show the app window and return its exit status.

    Raises ModuleNotFoundError naming PySide6 when the `gui` extra is not
    installed; the caller turns that into the message a person can act on.
    """
    import sys

    from PySide6.QtGui import QGuiApplication

    from murmly.app import qt

    application = QGuiApplication(sys.argv[:1])
    application.setApplicationName(APP_TITLE)
    for path in qt.prepare_application():
        print(f"murmly: the bundled font {path.name} could not be loaded.", file=sys.stderr)
    # Both stay referenced until the event loop ends; QML holds only weak ties.
    engine, theme = qt.create_engine()
    if not engine.rootObjects():
        print("murmly: the app window could not be created.", file=sys.stderr)
        return 1
    return application.exec()
