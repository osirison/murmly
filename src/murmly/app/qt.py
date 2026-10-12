"""The Qt side of the app: fonts, the theme object and the window.

Imports PySide6 at the top, so only `run` (or a test that has skipped itself
when PySide6 is absent) imports this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Property, QObject, Qt, QUrl, Signal
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from murmly.app import controller, fonts, shell, theme

if TYPE_CHECKING:
    from murmly.app import tray_qt

QML_DIR = Path(__file__).resolve().parent / "qml"
MAIN_QML = QML_DIR / "Main.qml"
TRAY_QML = QML_DIR / "TrayPanel.qml"

# The text the window shows when nothing else sizes it; also the application
# font's pixel size for anything that does not name a type step.
DEFAULT_PIXEL_SIZE = 13

# The Quick Controls style every control here is restyled from.
STYLE = "Basic"


def scheme_name(color_scheme: Qt.ColorScheme) -> str:
    """The theme's name for a Qt colour scheme; unknown gets the default."""
    if color_scheme == Qt.ColorScheme.Light:
        return "light"
    if color_scheme == Qt.ColorScheme.Dark:
        return "dark"
    return theme.DEFAULT_SCHEME


class Theme(QObject):
    """The one object QML reads every token from, as `theme`.

    `colors` is a map of every colour token for the current scheme and changes,
    with `scheme`, when the system's appearance does. Type, spacing, radii and
    layout never change.
    """

    schemeChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        hints = QGuiApplication.styleHints()
        self._scheme = scheme_name(hints.colorScheme())
        self._colors = theme.palette(self._scheme)
        self._typography = theme.typography()
        self._spacing = theme.spacing()
        self._radii = theme.radii()
        self._layout = {theme.camel(k): v for k, v in theme.LAYOUT.items()}
        hints.colorSchemeChanged.connect(self._on_color_scheme_changed)

    def _on_color_scheme_changed(self, color_scheme: Qt.ColorScheme) -> None:
        name = scheme_name(color_scheme)
        if name == self._scheme:
            return
        self._scheme = name
        self._colors = theme.palette(name)
        self.schemeChanged.emit()

    @Property(str, notify=schemeChanged)
    def scheme(self) -> str:
        return self._scheme

    @Property("QVariantMap", notify=schemeChanged)
    def colors(self) -> dict:
        return self._colors

    @Property("QVariantMap", constant=True)
    def typography(self) -> dict:
        return self._typography

    @Property("QVariantMap", constant=True)
    def space(self) -> dict:
        return self._spacing

    @Property("QVariantMap", constant=True)
    def radii(self) -> dict:
        return self._radii

    @Property("QVariantMap", constant=True)
    def layout(self) -> dict:
        return self._layout

    @Property(str, constant=True)
    def fontSans(self) -> str:
        return theme.FONT_SANS

    @Property(str, constant=True)
    def fontMono(self) -> str:
        return theme.FONT_MONO

    # Qt 6.11 has no setting for the system's reduced-motion preference, so
    # this is False on every platform until Qt (or a per-platform lookup)
    # supplies one. Nothing animates yet; the property is where animations ask.
    # Any animation added later must first read the platform setting.
    @Property(bool, constant=True)
    def reducedMotion(self) -> bool:
        return False


def prepare_application() -> list[Path]:
    """Register the bundled fonts and make the sans the default; return the files Qt refused.

    Needs the Qt application to exist. Also picks the Basic Quick Controls style,
    the one that can be restyled without the platform's own look showing through;
    it must be chosen before the first engine loads.
    """
    # Qt refuses a second call once any QML has loaded, so only the first one counts.
    if QQuickStyle.name() != STYLE:
        QQuickStyle.setStyle(STYLE)
    refused = fonts.load_fonts()
    font = QFont(theme.FONT_SANS)
    font.setPixelSize(DEFAULT_PIXEL_SIZE)
    QGuiApplication.setFont(font)
    return refused


def create_engine(
    home: controller.HomeController | None = None,
    health: controller.HealthController | None = None,
    settings: controller.SettingsController | None = None,
    keys: controller.KeysController | None = None,
    first_run: controller.FirstRunController | None = None,
    tray: "tray_qt.TrayController | None" = None,
) -> tuple[QQmlApplicationEngine, Theme]:
    """Load the window. The caller keeps both returned objects alive.

    `home`, `health`, `settings` and `keys` (QML's `hotkeys`) are the controllers QML reads under
    those names; by default ones that poll the real daemon, run the real
    `murmly doctor`, read the real configuration file and read the real hotkey
    record. A test passes ones with a fake behind them, so no test asks the
    machine's own daemon anything, runs doctor, touches the person's settings or
    binds a key. `first_run` is `firstRun`; by default first run is not showing,
    because whether it shows depends on the machine, and `run` decides that.
    """
    engine = QQmlApplicationEngine()
    theme_object = Theme(engine)
    if home is None:
        home = controller.HomeController(controller.default_model())
    if health is None:
        health = controller.HealthController(controller.default_health_model())
    if settings is None:
        settings = controller.SettingsController(controller.default_settings_model())
    if keys is None:
        keys = controller.KeysController(controller.default_keys_model(), settings.executor)
    if first_run is None:
        first_run = controller.FirstRunController()
    # Home reads the keys again once one is saved.
    keys.keysChanged.connect(home.setupChanged)
    # The engine owns them from here, so QML never outlives what it reads.
    home.setParent(engine)
    health.setParent(engine)
    settings.setParent(engine)
    keys.setParent(engine)
    first_run.setParent(engine)
    context = engine.rootContext()
    context.setContextProperty("theme", theme_object)
    context.setContextProperty("home", home)
    context.setContextProperty("health", health)
    context.setContextProperty("settings", settings)
    context.setContextProperty("hotkeys", keys)
    context.setContextProperty("firstRun", first_run)
    context.setContextProperty("sections", list(shell.SECTIONS))
    if tray is not None:
        tray.setParent(engine)
        context.setContextProperty("tray", tray)
    engine.load(QUrl.fromLocalFile(str(MAIN_QML)))
    if tray is not None and engine.rootObjects():
        # The panel is a second window, hidden until the person clicks the icon.
        engine.load(QUrl.fromLocalFile(str(TRAY_QML)))
        roots = engine.rootObjects()
        if len(roots) == 2:
            tray.attach(roots[0], roots[1])
    return engine, theme_object
