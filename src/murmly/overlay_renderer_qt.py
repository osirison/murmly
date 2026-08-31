"""Windows' overlay renderer: the same protocol and states, drawn with Qt.

Task 10.1: a second renderer process, speaking the same newline-delimited
JSON protocol over the inherited socket that `overlay_renderer.py` (GTK4)
already speaks, launched the same way `OverlayController` launches that one
-- so the daemon never learns which renderer it started (see
`platform/__init__.py`'s `_load_qt_overlay`, which loads the very same
`OverlayController` class the GTK4 candidate loads). Task 10.4's "single
enumeration both read" is `overlay_shared.py`: the visual states, the
dimensions, and the drawing constants below are read from there, not
retyped -- see that module's docstring for the full reasoning, including why
both this file and `overlay_renderer.py` carry the same `sys.path` bootstrap
just below.

PySide6 is imported nowhere at module scope, on purpose: this file has to
stay importable -- for `--check` to run, and for the test suite's 18.14
coverage of the shared state machine and this module's own pure functions --
on a machine (any Linux CI runner) where PySide6 is not installed at all,
which is the whole point of keeping it an optional, `sys_platform == 'win32'`
dependency (task 10.2). Every `PySide6` name is imported from inside a
function or method body instead, the same discipline `win_hotkey.py` and
`win_clipboard.py` already hold for `ctypes.windll`.

Task 10.3: `Qt.WindowTransparentForInput` and `Qt.WindowDoesNotAcceptFocus`
are the documented, cross-platform Qt flags; `WS_EX_LAYERED |
WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW` are additionally
applied to the native `HWND` through `SetWindowLongPtr`, because Qt's own
flags are not guaranteed to reach every one of those bits on their own.

Task 10.5: the result is read back with `GetWindowLongPtr`, not assumed --
`SetWindowLongPtr` returns 0 on failure, which is indistinguishable here from
"the previous value already was 0" -- and `missing_property_for_exstyle`
decides from that readback whether the window actually has every property
the `recording-overlay` spec's "Non-disruptive placement on every platform"
requirement demands. Where it does not, the overlay is not shown at all and
the missing property is what gets reported, the same choice
`OverlayApplication._fail_visual` already makes in the GTK4 renderer.

Unverified on real Windows -- everything past argument parsing and the pure
functions below needs a real `HWND` and a real Qt installation to confirm.
See task 10.1/10.3 in `openspec/changes/all-os-distributions/tasks.md`.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
import json
import socket
import sys
from typing import Any


if __package__ in (None, ""):
    # Launched as a bare script by `OverlayController` -- see
    # `overlay_renderer.py`'s identical bootstrap and `overlay_shared.py`'s
    # module docstring for why this is necessary and safe.
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

from murmly.overlay_shared import (
    BACKGROUND_RGBA,
    BAR_AMPLITUDE_HEIGHT,
    BAR_CORNER_RADIUS,
    BAR_MIN_HEIGHT,
    BAR_MULTIPLIERS,
    BAR_SPACING_X,
    BAR_START_X,
    BAR_WIDTH,
    CORNER_RADIUS_PX,
    ERROR_CIRCLE_CENTER_Y,
    ERROR_CIRCLE_RADIUS,
    ERROR_DOT_RADIUS,
    ERROR_DOT_Y,
    ERROR_LINE_WIDTH,
    ERROR_RGB,
    ERROR_STEM_BOTTOM_Y,
    ERROR_STEM_TOP_Y,
    FOREGROUND_RGB,
    MICROPHONE_ACTIVE_RGB,
    MICROPHONE_BODY_CENTER_Y,
    MICROPHONE_BODY_LEFT_X,
    MICROPHONE_BODY_RADIUS_INNER,
    MICROPHONE_BODY_RADIUS_OUTER,
    MICROPHONE_BODY_RIGHT_X,
    MICROPHONE_CENTER_X,
    MICROPHONE_HEAD_CENTER_Y,
    MICROPHONE_HEAD_RADIUS,
    MICROPHONE_INACTIVE_RGB,
    MICROPHONE_LINE_WIDTH,
    MICROPHONE_STEM_BOTTOM_Y,
    MICROPHONE_STEM_TOP_Y,
    MIN_TEXT_SIZE_PX,
    MAX_TEXT_SIZE_PX,
    PANEL_HORIZONTAL_PADDING,
    REDUCED_MOTION_LEVEL_INTERVAL_S,
    REDUCED_MOTION_QUANTUM_STEPS,
    STATIC_PROCESSING_DOT_COUNT,
    STATIC_PROCESSING_DOT_RADIUS,
    STATIC_PROCESSING_DOT_SPACING_X,
    STATIC_PROCESSING_DOT_START_X,
    STATIC_PROCESSING_DOT_Y,
    THINKING_FRAME_INTERVAL_MS,
    THINKING_PHASE_PER_BAR,
    THINKING_PHASE_STEP,
    WINDOW_HEIGHT,
    WINDOW_WIDTH,
    MessageParser,
    MonitorGeometry,
    RendererViewState,
    RendererVisualState,
    bottom_center_position,
    panel_height,
    panel_max_width,
    panel_position,
    panel_width,
    select_monitor_index,
    truncate_to_width,
)


SUPPORTED_BACKENDS = {"windows"}

# --------------------------------------------------------------------------
# Task 10.3/10.5: the native window-style bits, and whether they took.
# --------------------------------------------------------------------------

#: `winuser.h`'s `GWL_EXSTYLE`.
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000

#: Exactly the four bits task 10.3 names. `WS_EX_TOPMOST` is not one of
#: them -- Qt's own `Qt.WindowStaysOnTopHint` is what asks for it -- but the
#: readback below checks it anyway, because "above ordinary windows" is a
#: property the spec requires regardless of which layer (Qt's or this
#: module's `SetWindowLongPtr` call) is responsible for the bit that grants
#: it.
NATIVE_EXSTYLE_BITS = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW

#: In the order the `recording-overlay` spec's "Non-disruptive placement on
#: every platform" requirement states them, so the first missing property
#: named is also the first one that requirement lists.
REQUIRED_EXSTYLE_PROPERTIES: tuple[tuple[int, str], ...] = (
    (WS_EX_TOPMOST, "staying above ordinary application windows"),
    (WS_EX_NOACTIVATE, "not taking keyboard focus"),
    (WS_EX_TRANSPARENT, "not intercepting pointer input"),
)


def missing_property_for_exstyle(exstyle: int) -> str | None:
    """Which spec-required property `exstyle` lacks, or `None` if it has all three.

    Pure, and the whole reason task 10.5 is checkable without a real
    `HWND`: given the bitmask a real `GetWindowLongPtr` call would return,
    this is the entire decision of whether the overlay may be shown.
    """
    for bit, name in REQUIRED_EXSTYLE_PROPERTIES:
        if not exstyle & bit:
            return name
    return None


def _real_get_window_long(hwnd: int) -> int:
    from ctypes import windll

    return int(windll.user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))


def _real_set_window_long(hwnd: int, exstyle: int) -> int:
    from ctypes import windll

    return int(windll.user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, exstyle))


def apply_and_verify_exstyle(
    hwnd: int,
    get_window_long: Callable[[int], int] = _real_get_window_long,
    set_window_long: Callable[[int, int], int] = _real_set_window_long,
) -> str | None:
    """Set the four bits task 10.3 names, then read back what actually took.

    Returns the first spec-required property still missing afterwards, or
    `None` if the window has all of them. Read back rather than assumed:
    `SetWindowLongPtr` returns 0 both on failure and when the previous value
    already was 0, so the only way to know what a window ended up with is to
    ask it -- and `WS_EX_TOPMOST` is never set by this call at all (Qt's own
    `Qt.WindowStaysOnTopHint` is responsible for it), so a readback is the
    only way this function can see it either.
    """
    current = get_window_long(hwnd)
    set_window_long(hwnd, current | NATIVE_EXSTYLE_BITS)
    return missing_property_for_exstyle(get_window_long(hwnd))


# --------------------------------------------------------------------------
# The `--check` runtime probe, mirroring `overlay_renderer.check_visual_runtime`.
# --------------------------------------------------------------------------


def check_visual_runtime(backend: str) -> dict[str, object]:
    result: dict[str, object] = {
        "backend": backend,
        "system_python": sys.executable,
        "pyside6": False,
        "qt_version": None,
        "available": False,
    }
    if backend not in SUPPORTED_BACKENDS:
        result["error"] = f"Unsupported overlay backend: {backend}"
        return result
    try:
        from PySide6 import __version__ as pyside6_version
    except ImportError:
        # Named distinctly from the `try` below's failures: this is "the
        # mechanism exists but could not be used" (`platform-support`'s own
        # wording), and that scenario requires naming what to install --
        # `doctor`'s equivalent GTK4 message ("kokoro-onnx is not installed.
        # Run `uv sync`...") already sets this bar, and a bare
        # "No module named 'PySide6'" would not meet it.
        result["error"] = "PySide6 is not installed. Run `uv sync --extra overlay`."
        return result
    try:
        from PySide6.QtWidgets import QApplication

        result["pyside6"] = True
        result["qt_version"] = pyside6_version
        # An offscreen `QApplication` confirms PySide6 itself initializes in
        # this environment. It says nothing about whether a real window here
        # can actually get `WS_EX_TOPMOST`/`NOACTIVATE`/`TRANSPARENT` -- that
        # needs a real `HWND`, which `--check` deliberately never creates,
        # the same dependency-check-not-placement-rehearsal split
        # `check_visual_runtime` already keeps in the GTK4 renderer.
        application = QApplication.instance() or QApplication([sys.argv[0], "-platform", "offscreen"])
        del application
        result["available"] = True
    except (ImportError, OSError, ValueError) as error:
        result["error"] = str(error)
    return result


# --------------------------------------------------------------------------
# The renderer itself.
# --------------------------------------------------------------------------


class OverlayApplication:
    """Owns the Qt event loop, the socket, and every window this renderer shows.

    Mirrors `overlay_renderer.OverlayApplication`'s shape and lifecycle --
    same constructor arguments (a socket rather than a bare file descriptor,
    since Windows hands this one over already reconstructed by
    `socket.fromshare`, not by an inheritable fd number), same
    `RendererViewState`-driven message handling, same monitor selection and
    panel placement math -- because task 10.4 is exactly this: the same
    states, dimensions and transitions, read from `overlay_shared` rather
    than reimplemented here.
    """

    def __init__(
        self,
        sock: socket.socket,
        bottom_margin_px: int,
        reduced_motion: bool,
        backend: str,
        text_size_px: int = 13,
        transcript_panel: bool = False,
    ) -> None:
        from PySide6.QtCore import QSocketNotifier, QTimer
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtWidgets import QApplication

        self._QSocketNotifier = QSocketNotifier
        self._QGuiApplication = QGuiApplication
        self._QTimer = QTimer
        self._backend = backend
        self._socket = sock
        self._bottom_margin_px = bottom_margin_px
        self._reduced_motion = reduced_motion
        self._text_size_px = text_size_px
        self._transcript_panel = transcript_panel
        self._view = RendererViewState()
        self._phase = 0.0
        self._last_reduced_level_at = float("-inf")
        self._selected_monitor: MonitorGeometry | None = None
        self._panel_geometry: MonitorGeometry | None = None
        self._panel_width = 0

        self._application = QApplication.instance() or QApplication([sys.argv[0]])
        self._window = _IndicatorWindow(self)
        self._panel_window = _PanelWindow(self) if transcript_panel else None

        self._reader = self._QSocketNotifier(self._socket.fileno(), self._QSocketNotifier.Type.Read)
        self._reader.activated.connect(self._on_socket_readable)
        self._parser = MessageParser()

        self._animation_timer = QTimer()
        self._animation_timer.setInterval(THINKING_FRAME_INTERVAL_MS)
        self._animation_timer.timeout.connect(self._animate)
        self._animation_timer.start()

    def run(self) -> int:
        return int(self._application.exec())

    # -- socket handling ---------------------------------------------------

    def _on_socket_readable(self, _descriptor: int) -> None:
        try:
            data = self._socket.recv(4_096)
        except OSError:
            data = b""
        if not data:
            self._reader.setEnabled(False)
            self._application.quit()
            return
        for message in self._parser.feed(data):
            self._handle_message(message)

    def _handle_message(self, message: dict[str, object]) -> None:
        if message["type"] == "level" and self._reduced_motion:
            import time as _time

            now = _time.monotonic()
            if now - self._last_reduced_level_at < REDUCED_MOTION_LEVEL_INTERVAL_S:
                return
            self._last_reduced_level_at = now

        stop = self._view.apply(message)
        if stop:
            self._application.quit()
            return
        if message["type"] == "error":
            generation = self._view.error_generation
            self._QTimer.singleShot(
                int(message["duration_ms"]),
                lambda: self._hide_error(generation),
            )
        self._sync_visibility()
        self._window.update()
        if message["type"] != "level" and self._panel_window is not None:
            self._sync_panel()

    def _hide_error(self, generation: int) -> None:
        if self._view.state == RendererVisualState.ERROR and self._view.error_generation == generation:
            self._view.state = RendererVisualState.IDLE
            self._sync_visibility()
            self._window.update()

    # -- monitor and placement ----------------------------------------------

    def _monitors(self) -> list[MonitorGeometry]:
        geometries = []
        for screen in self._QGuiApplication.screens():
            rectangle = screen.geometry()
            geometries.append(
                MonitorGeometry(
                    connector=screen.name() or "",
                    x=rectangle.x(),
                    y=rectangle.y(),
                    width=rectangle.width(),
                    height=rectangle.height(),
                    scale=max(1, round(screen.devicePixelRatio())),
                )
            )
        return geometries

    def _sync_visibility(self) -> None:
        if self._view.visible:
            if self._selected_monitor is None:
                monitors = self._monitors()
                index = select_monitor_index(monitors)
                if index is not None:
                    self._selected_monitor = monitors[index]
                    self._place_window(self._window, self._selected_monitor)
            if not self._window.isVisible():
                self._window.show()
                self._verify_and_show(self._window)
        else:
            self._window.hide()
            if self._panel_window is not None:
                self._panel_window.hide()
            self._panel_width = 0
            self._panel_geometry = None
            self._selected_monitor = None

    def _place_window(self, window: Any, monitor: MonitorGeometry) -> None:
        x, y = bottom_center_position(monitor, self._bottom_margin_px)
        window.setGeometry(x, y, WINDOW_WIDTH * monitor.scale, WINDOW_HEIGHT * monitor.scale)

    def _verify_and_show(self, window: Any) -> None:
        """Task 10.5: refuse to keep presenting a window missing a required property.

        Runs once the window has a native handle (`winId()` forces creation
        of one), which is why this happens after `show()` rather than in
        `__init__` -- the same "only once realized" timing
        `overlay_renderer.OverlayApplication._on_realize`/`_on_map` already
        use for the X11 adapter calls.
        """
        try:
            hwnd = int(window.winId())
            missing = apply_and_verify_exstyle(hwnd)
        except Exception as error:  # noqa: BLE001 - any failure here means "cannot verify, refuse"
            self._fail_visual("placement verification", error)
            return
        if missing is not None:
            self._fail_visual("placement", OSError(f"The platform did not grant: {missing}."))

    def _fail_visual(self, operation: str, error: Exception) -> None:
        print(f"Error: Windows overlay {operation} failed: {error}", file=sys.stderr)
        self._window.hide()
        if self._panel_window is not None:
            self._panel_window.hide()
        self._application.quit()

    def _sync_panel(self) -> None:
        if self._panel_window is None:
            return
        showing = bool(self._view.partial) and self._view.state == RendererVisualState.LISTENING
        if not showing:
            self._panel_window.hide()
            return
        if self._selected_monitor is None:
            return
        if not self._panel_window.isVisible():
            width = panel_max_width(self._selected_monitor)
            height = panel_height(self._text_size_px)
            if width != self._panel_width or self._panel_geometry != self._selected_monitor:
                self._panel_width = width
                self._panel_geometry = self._selected_monitor
                x, y = panel_position(self._selected_monitor, self._bottom_margin_px, width, height)
                self._panel_window.setGeometry(
                    x, y, width * self._selected_monitor.scale, height * self._selected_monitor.scale
                )
            self._panel_window.show()
            self._verify_and_show(self._panel_window)
        self._panel_window.update()

    def _panel_monitor(self, width: int) -> MonitorGeometry:
        if self._panel_geometry is not None:
            return self._panel_geometry
        return MonitorGeometry(connector="", x=0, y=0, width=width, height=0)

    def _animate(self) -> None:
        if self._view.state == RendererVisualState.THINKING and not self._reduced_motion:
            import math

            self._phase = (self._phase + THINKING_PHASE_STEP) % (2 * math.pi)
            self._window.update()


class _IndicatorWindow:
    """The recording indicator: a `QWidget` presenting `owner._view`.

    A thin adapter rather than a `QWidget` subclass defined at module scope,
    so this module still imports without `PySide6` present -- `QWidget`
    itself cannot be subclassed at class-definition time without importing
    it, which task 10.2/18.14 require this module not to do at module scope.
    Constructed lazily from inside `OverlayApplication.__init__`, once
    `PySide6` is already known to be importable.
    """

    def __new__(cls, owner: OverlayApplication) -> Any:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QPainter, QPainterPath
        from PySide6.QtWidgets import QWidget

        class _Widget(QWidget):
            def __init__(self) -> None:
                super().__init__()
                self.setWindowFlags(
                    Qt.WindowType.FramelessWindowHint
                    | Qt.WindowType.Tool
                    | Qt.WindowType.WindowStaysOnTopHint
                    | Qt.WindowType.WindowDoesNotAcceptFocus
                    | Qt.WindowType.WindowTransparentForInput
                )
                self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
                self.setFixedSize(WINDOW_WIDTH, WINDOW_HEIGHT)

            def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt's own name
                painter = QPainter(self)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                _paint_indicator(painter, QPainterPath, owner._view, owner._phase, owner._reduced_motion)
                painter.end()

        return _Widget()


class _PanelWindow:
    """The transcript panel, the `QWidget` counterpart of `_IndicatorWindow`."""

    def __new__(cls, owner: OverlayApplication) -> Any:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QFontMetrics, QPainter, QPainterPath
        from PySide6.QtWidgets import QWidget

        class _Widget(QWidget):
            def __init__(self) -> None:
                super().__init__()
                self.setWindowFlags(
                    Qt.WindowType.FramelessWindowHint
                    | Qt.WindowType.Tool
                    | Qt.WindowType.WindowStaysOnTopHint
                    | Qt.WindowType.WindowDoesNotAcceptFocus
                    | Qt.WindowType.WindowTransparentForInput
                )
                self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

            def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt's own name
                painter = QPainter(self)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                _paint_panel(painter, QPainterPath, QFontMetrics, owner)
                painter.end()

        return _Widget()


def _set_brush(painter: Any, rgba: tuple[float, ...]) -> None:
    from PySide6.QtGui import QColor

    channels = [round(component * 255) for component in rgba]
    if len(channels) == 3:
        channels.append(255)
    painter.setBrush(QColor(*channels))
    painter.setPen(QColor(*channels))


def _rounded_rect_path(painter_path_cls: Any, x: float, y: float, width: float, height: float, radius: float) -> Any:
    path = painter_path_cls()
    path.addRoundedRect(x, y, width, height, radius, radius)
    return path


def _paint_indicator(painter: Any, painter_path_cls: Any, view: RendererViewState, phase: float, reduced_motion: bool) -> None:
    _set_brush(painter, BACKGROUND_RGBA)
    painter.drawPath(
        _rounded_rect_path(painter_path_cls, 0.5, 0.5, WINDOW_WIDTH - 1.0, WINDOW_HEIGHT - 1.0, CORNER_RADIUS_PX)
    )

    if view.state == RendererVisualState.ERROR:
        _paint_error(painter)
        return
    _paint_microphone(painter, active=view.state == RendererVisualState.LISTENING)
    if view.state == RendererVisualState.THINKING and reduced_motion:
        _paint_static_processing(painter)
    else:
        _paint_bars(painter, painter_path_cls, view, phase, reduced_motion)


def _paint_microphone(painter: Any, *, active: bool) -> None:
    from PySide6.QtCore import QPointF, QRectF
    from PySide6.QtGui import QPainterPath, QPen

    color = MICROPHONE_ACTIVE_RGB if active else MICROPHONE_INACTIVE_RGB
    channels = [round(component * 255) for component in color]
    from PySide6.QtGui import QColor

    pen = QPen(QColor(*channels))
    pen.setWidthF(MICROPHONE_LINE_WIDTH)
    painter.setPen(pen)
    painter.setBrush(Qt_NoBrush())

    head = QRectF(
        MICROPHONE_CENTER_X - MICROPHONE_HEAD_RADIUS,
        MICROPHONE_HEAD_CENTER_Y - MICROPHONE_HEAD_RADIUS,
        MICROPHONE_HEAD_RADIUS * 2,
        MICROPHONE_HEAD_RADIUS * 2,
    )
    body = QRectF(
        MICROPHONE_CENTER_X - MICROPHONE_BODY_RADIUS_INNER,
        MICROPHONE_BODY_CENTER_Y - MICROPHONE_BODY_RADIUS_INNER,
        MICROPHONE_BODY_RADIUS_INNER * 2,
        MICROPHONE_BODY_RADIUS_INNER * 2,
    )
    outer = QRectF(
        MICROPHONE_CENTER_X - MICROPHONE_BODY_RADIUS_OUTER,
        MICROPHONE_BODY_CENTER_Y - MICROPHONE_BODY_RADIUS_OUTER,
        MICROPHONE_BODY_RADIUS_OUTER * 2,
        MICROPHONE_BODY_RADIUS_OUTER * 2,
    )
    capsule = QPainterPath()
    capsule.arcMoveTo(head, 0.0)
    capsule.arcTo(head, 0.0, 180.0)
    capsule.arcTo(body, 180.0, 180.0)
    capsule.closeSubpath()
    painter.drawPath(capsule)
    painter.drawArc(outer, 0, 180 * 16)
    painter.drawLine(
        QPointF(MICROPHONE_CENTER_X, MICROPHONE_STEM_TOP_Y), QPointF(MICROPHONE_CENTER_X, MICROPHONE_STEM_BOTTOM_Y)
    )
    painter.drawLine(
        QPointF(MICROPHONE_BODY_LEFT_X, MICROPHONE_STEM_BOTTOM_Y),
        QPointF(MICROPHONE_BODY_RIGHT_X, MICROPHONE_STEM_BOTTOM_Y),
    )


def Qt_NoBrush() -> Any:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QBrush

    return QBrush(Qt.BrushStyle.NoBrush)


def _paint_bars(painter: Any, painter_path_cls: Any, view: RendererViewState, phase: float, reduced_motion: bool) -> None:
    import math

    _set_brush(painter, FOREGROUND_RGB)
    for index, multiplier in enumerate(BAR_MULTIPLIERS):
        if view.state == RendererVisualState.THINKING:
            amplitude = (math.sin(phase + index * THINKING_PHASE_PER_BAR) + 1.0) / 2.0
        elif view.state == RendererVisualState.LISTENING:
            amplitude = view.level * multiplier
        else:
            amplitude = 0.0
        if reduced_motion and view.state == RendererVisualState.LISTENING:
            amplitude = round(amplitude * REDUCED_MOTION_QUANTUM_STEPS) / REDUCED_MOTION_QUANTUM_STEPS
        bar_height = BAR_MIN_HEIGHT + amplitude * BAR_AMPLITUDE_HEIGHT
        x = BAR_START_X + index * BAR_SPACING_X
        y = (WINDOW_HEIGHT - bar_height) / 2.0
        painter.drawPath(_rounded_rect_path(painter_path_cls, x, y, BAR_WIDTH, bar_height, BAR_CORNER_RADIUS))


def _paint_static_processing(painter: Any) -> None:
    from PySide6.QtCore import QRectF

    _set_brush(painter, FOREGROUND_RGB)
    for index in range(STATIC_PROCESSING_DOT_COUNT):
        center_x = STATIC_PROCESSING_DOT_START_X + index * STATIC_PROCESSING_DOT_SPACING_X
        painter.drawEllipse(
            QRectF(
                center_x - STATIC_PROCESSING_DOT_RADIUS,
                STATIC_PROCESSING_DOT_Y - STATIC_PROCESSING_DOT_RADIUS,
                STATIC_PROCESSING_DOT_RADIUS * 2,
                STATIC_PROCESSING_DOT_RADIUS * 2,
            )
        )


def _paint_error(painter: Any) -> None:
    from PySide6.QtCore import QPointF, QRectF
    from PySide6.QtGui import QColor, QPen

    channels = [round(component * 255) for component in ERROR_RGB]
    pen = QPen(QColor(*channels))
    pen.setWidthF(ERROR_LINE_WIDTH)
    painter.setPen(pen)
    painter.setBrush(Qt_NoBrush())
    center_x = WINDOW_WIDTH / 2.0
    painter.drawEllipse(
        QRectF(
            center_x - ERROR_CIRCLE_RADIUS,
            ERROR_CIRCLE_CENTER_Y - ERROR_CIRCLE_RADIUS,
            ERROR_CIRCLE_RADIUS * 2,
            ERROR_CIRCLE_RADIUS * 2,
        )
    )
    painter.drawLine(QPointF(center_x, ERROR_STEM_TOP_Y), QPointF(center_x, ERROR_STEM_BOTTOM_Y))
    painter.setBrush(QColor(*channels))
    painter.drawEllipse(
        QRectF(center_x - ERROR_DOT_RADIUS, ERROR_DOT_Y - ERROR_DOT_RADIUS, ERROR_DOT_RADIUS * 2, ERROR_DOT_RADIUS * 2)
    )


def _paint_panel(painter: Any, painter_path_cls: Any, font_metrics_cls: Any, owner: OverlayApplication) -> None:
    if not owner._view.partial:
        return
    metrics = font_metrics_cls(painter.font())

    def measure(candidate: str) -> float:
        return float(metrics.horizontalAdvance(candidate))

    width = owner._panel_window.width()
    height = owner._panel_window.height()
    text = truncate_to_width(owner._view.partial, measure, width - 2 * PANEL_HORIZONTAL_PADDING)
    if not text:
        return

    background_width = float(min(panel_width(measure(text), owner._panel_monitor(width)), width))
    left = (width - background_width) / 2.0
    _set_brush(painter, BACKGROUND_RGBA)
    painter.drawPath(_rounded_rect_path(painter_path_cls, left + 0.5, 0.5, background_width - 1.0, height - 1.0, CORNER_RADIUS_PX))

    _set_brush(painter, FOREGROUND_RGB)
    from PySide6.QtCore import QRectF

    painter.drawText(
        QRectF(left + PANEL_HORIZONTAL_PADDING, 0, background_width - 2 * PANEL_HORIZONTAL_PADDING, height),
        text,
    )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render the Murmly recording overlay (Qt/Windows).")
    parser.add_argument("--check", action="store_true", help="Check visual runtime dependencies.")
    parser.add_argument("--fd", type=int, default=None, help="Inherited overlay protocol socket (POSIX-style).")
    parser.add_argument(
        "--fd-share-stdin",
        action="store_true",
        help="Read a socket.share() blob from stdin and reconstruct it with socket.fromshare().",
    )
    parser.add_argument("--bottom-margin-px", type=int, default=32)
    parser.add_argument("--text-size-px", type=int, default=13)
    parser.add_argument("--transcript-panel", action="store_true")
    parser.add_argument("--reduced-motion", action="store_true")
    parser.add_argument("--backend", choices=sorted(SUPPORTED_BACKENDS), default=None)
    return parser


def _socket_from_arguments(args: argparse.Namespace) -> socket.socket:
    if args.fd_share_stdin:
        # `socket.share`/`socket.fromshare` are Windows-only -- `socket` does
        # not even define `fromshare` on other platforms, hence the
        # `AttributeError` guard alongside the platform's own `OSError` -- see
        # `OverlayController._spawn_windows_renderer`'s docstring for why the
        # blob arrives this way rather than as an inherited fd number.
        share_data = sys.stdin.buffer.read()
        try:
            return socket.fromshare(share_data)
        except AttributeError as error:
            raise OSError("socket.fromshare() is only available on Windows.") from error
    if args.fd is not None:
        return socket.socket(fileno=args.fd)
    raise ValueError("Either --fd or --fd-share-stdin is required unless --check is used.")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.check:
        result = check_visual_runtime(args.backend or "windows")
        print(json.dumps(result, sort_keys=True))
        return 0 if result["available"] else 1
    if args.backend not in SUPPORTED_BACKENDS:
        print("Error: --backend must select windows.", file=sys.stderr)
        return 2
    if not 0 <= args.bottom_margin_px <= 512:
        print("Error: --bottom-margin-px must be between 0 and 512.", file=sys.stderr)
        return 2
    if not MIN_TEXT_SIZE_PX <= args.text_size_px <= MAX_TEXT_SIZE_PX:
        print(
            f"Error: --text-size-px must be between {MIN_TEXT_SIZE_PX} and {MAX_TEXT_SIZE_PX}.",
            file=sys.stderr,
        )
        return 2
    try:
        sock = _socket_from_arguments(args)
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    try:
        return OverlayApplication(
            sock,
            args.bottom_margin_px,
            args.reduced_motion,
            args.backend,
            text_size_px=args.text_size_px,
            transcript_panel=args.transcript_panel,
        ).run()
    except (ImportError, OSError, ValueError) as error:
        print(f"Error: overlay runtime unavailable: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
