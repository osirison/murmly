"""The Qt side of the app: fonts, the theme object and the window.

Imports PySide6 at the top, so only `run` (or a test that has skipped itself
when PySide6 is absent) imports this module.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Property, QObject, Qt, QUrl, Signal
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from murmly.app import fonts, shell, theme

QML_DIR = Path(__file__).resolve().parent / "qml"
MAIN_QML = QML_DIR / "Main.qml"

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


def create_engine() -> tuple[QQmlApplicationEngine, Theme]:
    """Load the window. The caller keeps both returned objects alive."""
    engine = QQmlApplicationEngine()
    theme_object = Theme(engine)
    context = engine.rootContext()
    context.setContextProperty("theme", theme_object)
    context.setContextProperty("sections", list(shell.SECTIONS))
    engine.load(QUrl.fromLocalFile(str(MAIN_QML)))
    return engine, theme_object
