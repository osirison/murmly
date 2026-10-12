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

    # A QApplication, not a bare QGuiApplication: the tray icon's menu is a widget.
    from PySide6.QtWidgets import QApplication

    from murmly.app import controller, qt, tray_qt

    application = QApplication(sys.argv[:1])
    application.setApplicationName(APP_TITLE)
    for path in qt.prepare_application():
        print(f"murmly: the bundled font {path.name} could not be loaded.", file=sys.stderr)
    # All of these stay referenced until the event loop ends; QML holds only weak ties.
    home = controller.HomeController(controller.default_model())
    health = controller.HealthController(controller.default_health_model())
    application.aboutToQuit.connect(home.shutdown)
    # Stops a `murmly doctor` still running, so quitting never waits for it.
    application.aboutToQuit.connect(health.shutdown)
    settings = controller.SettingsController(controller.default_settings_model())
    application.aboutToQuit.connect(settings.shutdown)
    # Shares the Settings worker, so binding a key and restarting the service never overlap.
    keys_model = controller.default_keys_model()
    keys = controller.KeysController(keys_model, settings.executor)
    application.aboutToQuit.connect(keys.shutdown)
    # First run shows when no dictation key is bound, decided once, here, from the
    # hotkey record, read before the window exists. Nothing later changes it but
    # the person.
    first_run = controller.FirstRunController(
        controller.keys.FirstRun(controller.keys.should_show_first_run(keys_model.read()))
    )
    # The tray, where the desktop has one; otherwise Health says none was found and
    # closing the window ends the app. Never raises: no tray is a normal desktop.
    tray = tray_qt.create_tray(home, settings, health, application)
    engine, theme = qt.create_engine(home, health, settings, keys, first_run, tray)
    if not engine.rootObjects():
        print("murmly: the app window could not be created.", file=sys.stderr)
        return 1
    return application.exec()
