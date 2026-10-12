"""The Qt side of the tray: the icon, its menu, and the panel's window.

The decisions (which icon, where the panel goes, what a switch writes) are in
`tray.py`. This puts them on a `QSystemTrayIcon` and a small frameless window
that `TrayPanel.qml` draws.

The panel opens only when the person clicks the icon, and nothing the daemon
reports shows, raises or activates anything. Showing the main window
(`openWindow`) is the person asking, from the panel's button or the menu.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Property, QByteArray, QObject, Qt, Signal, Slot
from PySide6.QtGui import QAction, QCursor, QGuiApplication, QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from murmly.app import controller, qt, shell, theme, tray

# The icon is drawn once at this size and scaled by the desktop.
ICON_PIXELS = 64


def tray_available() -> bool:
    return QSystemTrayIcon.isSystemTrayAvailable()


def render_icon(presentation: str, scheme: str) -> QIcon:
    """The icon for a state, in the glyph colour that reads on this scheme's tray."""
    svg = tray.icon_svg(presentation, theme.palette(scheme))
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    image = QImage(ICON_PIXELS, ICON_PIXELS, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    return QIcon(QPixmap.fromImage(image))


class TrayController(QObject):
    """QML's `tray`: the switches' `view`, and what a person can do from the panel.

    Owns the system tray icon. A switch is written through the Settings
    controller, so it is saved, refused and applied (a restart once the daemon is
    idle) exactly as the same change on the Settings page is.
    """

    viewChanged = Signal()

    def __init__(
        self,
        home: controller.HomeController,
        settings: controller.SettingsController,
        application: QGuiApplication,
        icon: QSystemTrayIcon,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._home = home
        self._settings = settings
        self._application = application
        self._icon = icon
        self._window: QObject | None = None
        self._panel: QObject | None = None
        self._toggle = tray.PanelToggle()
        self._shown: tuple[str, str] | None = None
        self._memory = tray.SwitchMemory()
        self._remember()
        self._view: dict = tray.switches_view(settings.view)
        self._menu = QMenu()
        open_action = QAction("Open murmly", self._menu)
        open_action.triggered.connect(lambda: self.openWindow(""))
        quit_action = QAction("Quit", self._menu)
        quit_action.triggered.connect(self.quit)
        self._menu.addAction(open_action)
        self._menu.addSeparator()
        self._menu.addAction(quit_action)
        icon.setContextMenu(self._menu)
        icon.activated.connect(self._activated)
        settings.viewChanged.connect(self._settings_changed)
        home.viewChanged.connect(self._refresh_icon)
        home.pollingChanged.connect(self._refresh_icon)
        self._scheme = qt.scheme_name(QGuiApplication.styleHints().colorScheme())
        QGuiApplication.styleHints().colorSchemeChanged.connect(self._scheme_changed)
        application.aboutToQuit.connect(self.shutdown)
        self._refresh_icon()
        icon.show()

    # -- what QML reads -----------------------------------------------------

    @Property("QVariantMap", notify=viewChanged)
    def view(self) -> dict:
        return self._view

    @property
    def icon(self) -> QSystemTrayIcon:
        return self._icon

    @property
    def panel(self) -> QObject | None:
        return self._panel

    def attach(self, window: QObject, panel: QObject) -> None:
        """The main window and the panel's, once QML has made them."""
        self._window = window
        self._panel = panel

    # -- what a person can do -------------------------------------------------

    @Slot(str)
    def toggle(self, switch_id: str) -> None:
        """A click on a switch: write what it now asks for, as Settings would."""
        values = self._settings.view.get("values")
        if self._settings.view.get("loaded") is not True or self._settings.view.get("disabled") is True:
            return
        control, value = tray.change_for(switch_id, values if isinstance(values, dict) else {}, self._memory)
        self._settings.change(control, value)

    @Slot()
    def togglePanel(self) -> None:
        panel = self._panel
        if panel is None:
            return
        visible = bool(panel.property("visible"))
        if visible:
            panel.setProperty("visible", False)
        elif self._toggle.should_show(visible):
            self._place_panel(panel)
            panel.setProperty("visible", True)

    @Slot()
    def hidePanel(self) -> None:
        if self._panel is not None:
            self._panel.setProperty("visible", False)

    @Slot()
    def panelLostFocus(self) -> None:
        """The person clicked elsewhere: the panel hides, and says so to the icon's next click."""
        if self._panel is not None and bool(self._panel.property("visible")):
            self._toggle.hid_itself()
            self._panel.setProperty("visible", False)

    @Slot(str)
    def openWindow(self, section: str) -> None:
        """Show the main window, on `section` ("home", "settings" or "health") when named.

        The person asked, from the panel or the menu, so this may bring the
        window forward. Nothing the daemon says reaches it.
        """
        self.hidePanel()
        window = self._window
        if window is None:
            return
        for index, entry in enumerate(shell.SECTIONS):
            if entry["id"] == section:
                window.setProperty("current", index)
        window.show()
        window.raise_()
        window.requestActivate()

    @Slot()
    def quit(self) -> None:
        """End the app. The daemon is a separate process and keeps running."""
        self.hidePanel()
        self._application.quit()

    def shutdown(self) -> None:
        """Take the icon away so a quit never leaves a dead one in the tray. Safe to call twice."""
        self._icon.hide()
        self._icon.setContextMenu(None)

    # -- plumbing -------------------------------------------------------------

    def _remember(self) -> None:
        values = self._settings.view.get("values")
        if isinstance(values, dict):
            self._memory.observe(values)

    def _settings_changed(self) -> None:
        self._remember()
        view = tray.switches_view(self._settings.view)
        if view != self._view:
            self._view = view
            self.viewChanged.emit()

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.togglePanel()

    @Slot(Qt.ColorScheme)
    def _scheme_changed(self, color_scheme: Qt.ColorScheme) -> None:
        # From the signal's own argument, as the window's theme reads it, so the
        # icon and the panel never disagree about the scheme.
        self._scheme = qt.scheme_name(color_scheme)
        self._refresh_icon()

    def _refresh_icon(self) -> None:
        scheme = self._scheme
        view = self._home.view
        presentation = tray.icon_presentation(view, self._home.polling)
        self._icon.setToolTip(tray.tooltip(presentation, view))
        if self._shown == (presentation, scheme):
            return
        self._shown = (presentation, scheme)
        self._icon.setIcon(render_icon(presentation, scheme))

    def _place_panel(self, panel: QObject) -> None:
        cursor = QCursor.pos()
        screen = QGuiApplication.screenAt(cursor) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        geometry = self._icon.geometry()
        x, y = tray.place_panel(
            tray.Rect(geometry.x(), geometry.y(), geometry.width(), geometry.height()),
            (cursor.x(), cursor.y()),
            (int(panel.property("width")), int(panel.property("height"))),
            tray.Rect(area.x(), area.y(), area.width(), area.height()),
        )
        panel.setProperty("x", x)
        panel.setProperty("y", y)


def create_tray(
    home: controller.HomeController,
    settings: controller.SettingsController,
    health: controller.HealthController,
    application: QGuiApplication,
    available: Callable[[], bool] = tray_available,
) -> TrayController | None:
    """The tray, when the desktop has one; otherwise None, and Health says so.

    With a tray, closing the window leaves the app running in it. Without one the
    app is reachable only from its window, so closing the window ends it rather
    than leaving something running that cannot be seen.
    """
    found = bool(available())
    health.set_tray_found(found)
    application.setQuitOnLastWindowClosed(tray.quits_when_window_closes(found))
    if not found:
        return None
    return TrayController(home, settings, application, QSystemTrayIcon(application))
