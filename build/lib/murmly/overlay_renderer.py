from __future__ import annotations

import argparse
import ctypes
import ctypes.util
from dataclasses import dataclass
import json
import math
import os
import socket
import sys
import threading
import time
from typing import Any
import warnings


MAX_MESSAGE_BYTES = 1_024
MIN_ERROR_DURATION_MS = 100
MAX_ERROR_DURATION_MS = 10_000
MAX_PARTIAL_CHARS = 200
WINDOW_WIDTH = 156
WINDOW_HEIGHT = 48
MIN_TEXT_SIZE_PX = 8
MAX_TEXT_SIZE_PX = 48
PANEL_MAX_DISPLAY_FRACTION = 0.75
PANEL_HORIZONTAL_PADDING = 12
PANEL_VERTICAL_PADDING = 6
PANEL_GAP_PX = 6
SUPPORTED_BACKENDS = {"x11", "wayland"}
LAYER_SHELL_LIBRARY = "libgtk4-layer-shell.so.0"
XA_ATOM = 4
XA_CARDINAL = 6
PROP_MODE_REPLACE = 0
SHAPE_SET = 0
SHAPE_INPUT = 2
UNSORTED = 0
CLIENT_MESSAGE = 33
NET_WM_STATE_ADD = 1
SUBSTRUCTURE_NOTIFY_MASK = 1 << 19
SUBSTRUCTURE_REDIRECT_MASK = 1 << 20


class _XClientMessageData(ctypes.Union):
    _fields_ = [
        ("bytes", ctypes.c_char * 20),
        ("shorts", ctypes.c_short * 10),
        ("longs", ctypes.c_long * 5),
    ]


class _XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", _XClientMessageData),
    ]


class _XEvent(ctypes.Union):
    _fields_ = [
        ("type", ctypes.c_int),
        ("xclient", _XClientMessageEvent),
        ("padding", ctypes.c_long * 24),
    ]


@dataclass(frozen=True, slots=True)
class MonitorGeometry:
    connector: str
    x: int
    y: int
    width: int
    height: int
    scale: int = 1


def select_monitor_index(monitors: list[MonitorGeometry]) -> int | None:
    for index, monitor in enumerate(monitors):
        if monitor.x <= 0 < monitor.x + monitor.width and monitor.y <= 0 < monitor.y + monitor.height:
            return index
    if not monitors:
        return None
    return min(range(len(monitors)), key=lambda index: monitors[index].connector)


def x11_position(monitor: MonitorGeometry, bottom_margin_px: int) -> tuple[int, int]:
    return (
        (monitor.x + (monitor.width - WINDOW_WIDTH) // 2) * monitor.scale,
        (monitor.y + monitor.height - WINDOW_HEIGHT - bottom_margin_px) * monitor.scale,
    )


def panel_height(text_size_px: int) -> int:
    return text_size_px + 2 * PANEL_VERTICAL_PADDING


def panel_max_width(monitor: MonitorGeometry) -> int:
    return max(int(monitor.width * PANEL_MAX_DISPLAY_FRACTION), WINDOW_WIDTH)


def panel_width(text_width_px: float, monitor: MonitorGeometry) -> int:
    """Size the transcript panel to its text, bounded by the display fraction."""
    requested = int(math.ceil(text_width_px)) + 2 * PANEL_HORIZONTAL_PADDING
    return max(min(requested, panel_max_width(monitor)), WINDOW_WIDTH)


def panel_position(
    monitor: MonitorGeometry,
    bottom_margin_px: int,
    width: int,
    height: int,
) -> tuple[int, int]:
    """Place the panel below the recording indicator without moving it.

    The indicator keeps its own margin, so the panel occupies the space between
    the indicator's bottom edge and the display edge, clamped so it never leaves
    the screen.
    """
    indicator_top = monitor.y + monitor.height - WINDOW_HEIGHT - bottom_margin_px
    indicator_bottom = indicator_top + WINDOW_HEIGHT
    below_top = indicator_bottom + PANEL_GAP_PX
    if below_top + height <= monitor.y + monitor.height:
        top = below_top
    else:
        # No room in the margin. Sitting above the indicator keeps the panel
        # visible and adjacent; clamping it downward instead would draw it on top
        # of the indicator, and leaving it below would push it off the display.
        top = max(indicator_top - PANEL_GAP_PX - height, monitor.y)
    return (
        (monitor.x + (monitor.width - width) // 2) * monitor.scale,
        top * monitor.scale,
    )


def truncate_to_width(text: str, measure, available_px: float) -> str:
    """Drop leading characters until the tail fits, matching the encoder's bias.

    Binary searched: measuring one candidate per starting offset is quadratic
    glyph shaping, and this runs on every draw.
    """
    if not text or measure(text) <= available_px:
        return text
    low, high = 1, len(text)
    while low < high:
        middle = (low + high) // 2
        if measure("…" + text[middle:]) <= available_px:
            high = middle
        else:
            low = middle + 1
    candidate = "…" + text[low:]
    return candidate if low < len(text) and measure(candidate) <= available_px else ""


def x11_surface_id(gdk_x11: Any, surface: Any) -> int:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="GdkX11.X11Surface.get_xid is deprecated",
            category=DeprecationWarning,
        )
        return int(gdk_x11.X11Surface.get_xid(surface))


def validate_message(message: object) -> dict[str, object] | None:
    if not isinstance(message, dict):
        return None
    message_type = message.get("type")
    if message_type == "state":
        if set(message) != {"type", "value"} or message.get("value") not in {
            "IDLE",
            "LISTENING",
            "THINKING",
        }:
            return None
    elif message_type == "partial":
        value = message.get("value")
        if set(message) != {"type", "value"} or not isinstance(value, str):
            return None
        if len(value) > MAX_PARTIAL_CHARS:
            return None
    elif message_type == "level":
        value = message.get("value")
        if (
            set(message) != {"type", "value"}
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0.0 <= value <= 1.0
        ):
            return None
    elif message_type == "error":
        duration_ms = message.get("duration_ms")
        if (
            set(message) != {"type", "duration_ms"}
            or isinstance(duration_ms, bool)
            or not isinstance(duration_ms, int)
            or not MIN_ERROR_DURATION_MS <= duration_ms <= MAX_ERROR_DURATION_MS
        ):
            return None
    elif message_type == "shutdown":
        if set(message) != {"type"}:
            return None
    else:
        return None
    return message


class MessageParser:
    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[dict[str, object]]:
        self._buffer.extend(data)
        messages: list[dict[str, object]] = []
        while b"\n" in self._buffer:
            line, _, remainder = self._buffer.partition(b"\n")
            self._buffer = bytearray(remainder)
            if not line or len(line) + 1 > MAX_MESSAGE_BYTES:
                continue
            try:
                decoded = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            validated = validate_message(decoded)
            if validated is not None:
                messages.append(validated)
        if len(self._buffer) >= MAX_MESSAGE_BYTES:
            self._buffer.clear()
        return messages


@dataclass(slots=True)
class RendererViewState:
    state: str = "IDLE"
    level: float = 0.0
    partial: str = ""
    error_generation: int = 0

    @property
    def visible(self) -> bool:
        return self.state != "IDLE"

    def apply(self, message: dict[str, object]) -> bool:
        message_type = message["type"]
        if message_type == "state":
            self.state = str(message["value"])
            if self.state != "LISTENING":
                self.level = 0.0
                # Partial text describes audio still being captured. Dropping it
                # here, rather than at each call site, is what keeps it out of the
                # processing and error presentations.
                self.partial = ""
            return False
        if message_type == "level":
            if self.state == "LISTENING":
                self.level = float(message["value"])
            return False
        if message_type == "partial":
            if self.state == "LISTENING":
                self.partial = str(message["value"])
            return False
        if message_type == "error":
            self.state = "ERROR"
            self.level = 0.0
            self.partial = ""
            self.error_generation += 1
            return False
        return message_type == "shutdown"


COMPOSITOR_LACKS_LAYER_SHELL = "The active Wayland compositor does not support Layer Shell."


def load_layer_shell(modules: dict[str, object] | None = None) -> str | None:
    """Put gtk4-layer-shell in the global symbol scope, before GTK loads libwayland.

    The library works by interposing on libwayland-client's symbols, so it has to be
    in the global scope first. PyGObject pulls libwayland in when it imports
    `gi.repository.Gtk`, and once that has happened the layer-shell calls silently do
    nothing rather than failing, so this runs before any gi import in the process.
    Upstream's own Python example loads the library this way for the same reason.

    Returns the reason it could not be done, or None. The two failures are told
    apart because they have different remedies: a library that is not installed is
    the user's to install, and a `gi` already imported is a bug in this file.
    """
    loaded = sys.modules if modules is None else modules
    if "gi" in loaded:
        # Conservative: it is `from gi.repository import Gtk` that pulls in
        # libwayland-client and closes the window, not `import gi` on its own. The
        # cheap check is the safe one, because the failure it prevents is silent.
        return (
            "gtk4-layer-shell has to be loaded before gi.repository imports "
            "libwayland-client, and gi is already imported."
        )
    try:
        ctypes.CDLL(LAYER_SHELL_LIBRARY, mode=ctypes.RTLD_GLOBAL)
    except OSError as error:
        return f"{LAYER_SHELL_LIBRARY} could not be loaded: {error}"
    return None


def check_visual_runtime(backend: str) -> dict[str, object]:
    result: dict[str, object] = {
        "backend": backend,
        "system_python": sys.executable,
        "pygobject": False,
        "gtk4": None,
        "gdk_x11": False,
        "native_x11": False,
        "gtk4_layer_shell": False,
        "available": False,
    }
    if backend not in SUPPORTED_BACKENDS:
        result["error"] = f"Unsupported overlay backend: {backend}"
        return result
    if backend == "wayland":
        # Before the gi import below, which is what makes the ordering work.
        unloadable = load_layer_shell()
        if unloadable is not None:
            result["error"] = unloadable
            return result
    try:
        import cairo
        import gi

        del cairo

        result["pygobject"] = True
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        result["gtk4"] = f"{Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}"
        if backend == "wayland":
            gi.require_version("Gtk4LayerShell", "1.0")
            from gi.repository import Gtk4LayerShell

            Gtk.init()
            if not Gtk4LayerShell.is_supported():
                raise OSError(COMPOSITOR_LACKS_LAYER_SHELL)
            result["gtk4_layer_shell"] = True
        else:
            gi.require_version("GdkX11", "4.0")
            from gi.repository import GdkX11

            del GdkX11
            result["gdk_x11"] = True
            X11WindowAdapter().check_runtime()
            result["native_x11"] = True
        result["available"] = True
    except (AttributeError, ImportError, OSError, ValueError) as error:
        result["error"] = str(error)
    return result


def _load_x11_libraries() -> tuple[ctypes.CDLL, ctypes.CDLL]:
    x11_path = ctypes.util.find_library("X11")
    xext_path = ctypes.util.find_library("Xext")
    if not x11_path or not xext_path:
        raise OSError("libX11 and libXext are required for the X11 overlay backend.")
    return ctypes.CDLL(x11_path), ctypes.CDLL(xext_path)


class X11WindowAdapter:
    def __init__(self, x11: ctypes.CDLL | None = None, xext: ctypes.CDLL | None = None) -> None:
        if x11 is None or xext is None:
            x11, xext = _load_x11_libraries()
        self._x11 = x11
        self._xext = xext
        self._configure_signatures()

    def check_runtime(self) -> None:
        display = self._x11.XOpenDisplay(None)
        if not display:
            raise OSError("Unable to open the active X11 display.")
        try:
            event_base = ctypes.c_int()
            error_base = ctypes.c_int()
            if self._xext.XShapeQueryExtension(
                display,
                ctypes.byref(event_base),
                ctypes.byref(error_base),
            ) == 0:
                raise OSError("The active X11 display does not support the Shape extension.")
        finally:
            self._x11.XCloseDisplay(display)

    def prepare(
        self,
        xid: int,
        monitor: MonitorGeometry,
        bottom_margin_px: int,
        geometry: tuple[int, int, int, int] | None = None,
    ) -> None:
        display = self._x11.XOpenDisplay(None)
        if not display:
            raise OSError("Unable to open the X11 display for overlay placement.")
        try:
            window_type_property = self._atom(display, "_NET_WM_WINDOW_TYPE")
            notification_type = self._atom(display, "_NET_WM_WINDOW_TYPE_NOTIFICATION")
            self._replace_atoms(display, xid, window_type_property, [notification_type])

            state_property = self._atom(display, "_NET_WM_STATE")
            states = [
                self._atom(display, "_NET_WM_STATE_ABOVE"),
                self._atom(display, "_NET_WM_STATE_STICKY"),
                self._atom(display, "_NET_WM_STATE_SKIP_TASKBAR"),
                self._atom(display, "_NET_WM_STATE_SKIP_PAGER"),
            ]
            self._replace_atoms(display, xid, state_property, states)
            desktop_property = self._atom(display, "_NET_WM_DESKTOP")
            self._replace_cardinal(display, xid, desktop_property, 0xFFFFFFFF)

            x, y, width, height = geometry or (
                *x11_position(monitor, bottom_margin_px),
                WINDOW_WIDTH * monitor.scale,
                WINDOW_HEIGHT * monitor.scale,
            )
            self._x11.XMoveResizeWindow(display, xid, x, y, width, height)
            self._xext.XShapeCombineRectangles(
                display,
                xid,
                SHAPE_INPUT,
                0,
                0,
                None,
                0,
                SHAPE_SET,
                UNSORTED,
            )
            self._x11.XFlush(display)
        finally:
            self._x11.XCloseDisplay(display)

    def activate(
        self,
        xid: int,
        monitor: MonitorGeometry,
        bottom_margin_px: int,
        geometry: tuple[int, int, int, int] | None = None,
    ) -> None:
        display = self._x11.XOpenDisplay(None)
        if not display:
            raise OSError("Unable to open the X11 display for overlay activation.")
        try:
            x, y, width, height = geometry or (
                *x11_position(monitor, bottom_margin_px),
                WINDOW_WIDTH * monitor.scale,
                WINDOW_HEIGHT * monitor.scale,
            )
            self._x11.XMoveResizeWindow(display, xid, x, y, width, height)
            root = self._x11.XDefaultRootWindow(display)
            state_property = self._atom(display, "_NET_WM_STATE")
            for state_name in (
                "_NET_WM_STATE_ABOVE",
                "_NET_WM_STATE_STICKY",
                "_NET_WM_STATE_SKIP_TASKBAR",
                "_NET_WM_STATE_SKIP_PAGER",
            ):
                self._send_state_add(display, root, xid, state_property, self._atom(display, state_name))
            self._send_desktop_all(display, root, xid)
            self._x11.XRaiseWindow(display, xid)
            self._x11.XFlush(display)
        finally:
            self._x11.XCloseDisplay(display)

    def _send_state_add(
        self,
        display: int,
        root: int,
        xid: int,
        state_property: int,
        state_atom: int,
    ) -> None:
        event = _XEvent()
        event.xclient.type = CLIENT_MESSAGE
        event.xclient.send_event = True
        event.xclient.display = display
        event.xclient.window = xid
        event.xclient.message_type = state_property
        event.xclient.format = 32
        event.xclient.data.longs[0] = NET_WM_STATE_ADD
        event.xclient.data.longs[1] = state_atom
        event.xclient.data.longs[2] = 0
        event.xclient.data.longs[3] = 1
        status = self._x11.XSendEvent(
            display,
            root,
            False,
            SUBSTRUCTURE_NOTIFY_MASK | SUBSTRUCTURE_REDIRECT_MASK,
            ctypes.byref(event),
        )
        if status == 0:
            raise OSError("X11 window manager rejected an overlay state request.")

    def _send_desktop_all(self, display: int, root: int, xid: int) -> None:
        event = _XEvent()
        event.xclient.type = CLIENT_MESSAGE
        event.xclient.send_event = True
        event.xclient.display = display
        event.xclient.window = xid
        event.xclient.message_type = self._atom(display, "_NET_WM_DESKTOP")
        event.xclient.format = 32
        event.xclient.data.longs[0] = 0xFFFFFFFF
        event.xclient.data.longs[1] = 1
        status = self._x11.XSendEvent(
            display,
            root,
            False,
            SUBSTRUCTURE_NOTIFY_MASK | SUBSTRUCTURE_REDIRECT_MASK,
            ctypes.byref(event),
        )
        if status == 0:
            raise OSError("X11 window manager rejected the all-desktops request.")

    def _replace_atoms(self, display: int, xid: int, property_atom: int, values: list[int]) -> None:
        atom_values = (ctypes.c_ulong * len(values))(*values)
        status = self._x11.XChangeProperty(
            display,
            xid,
            property_atom,
            XA_ATOM,
            32,
            PROP_MODE_REPLACE,
            ctypes.cast(atom_values, ctypes.POINTER(ctypes.c_ubyte)),
            len(values),
        )
        if status == 0:
            raise OSError("X11 rejected an overlay window property.")

    def _replace_cardinal(self, display: int, xid: int, property_atom: int, value: int) -> None:
        cardinal = ctypes.c_ulong(value)
        status = self._x11.XChangeProperty(
            display,
            xid,
            property_atom,
            XA_CARDINAL,
            32,
            PROP_MODE_REPLACE,
            ctypes.cast(ctypes.pointer(cardinal), ctypes.POINTER(ctypes.c_ubyte)),
            1,
        )
        if status == 0:
            raise OSError("X11 rejected the overlay desktop property.")

    def _atom(self, display: int, name: str) -> int:
        atom = self._x11.XInternAtom(display, name.encode("ascii"), False)
        if atom == 0:
            raise OSError(f"X11 atom is unavailable: {name}")
        return int(atom)

    def _configure_signatures(self) -> None:
        self._x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self._x11.XOpenDisplay.restype = ctypes.c_void_p
        self._x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self._x11.XCloseDisplay.restype = ctypes.c_int
        self._x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        self._x11.XInternAtom.restype = ctypes.c_ulong
        self._x11.XChangeProperty.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int,
        ]
        self._x11.XChangeProperty.restype = ctypes.c_int
        self._x11.XMoveResizeWindow.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint,
        ]
        self._x11.XMoveResizeWindow.restype = ctypes.c_int
        self._x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self._x11.XDefaultRootWindow.restype = ctypes.c_ulong
        self._x11.XSendEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_long,
            ctypes.POINTER(_XEvent),
        ]
        self._x11.XSendEvent.restype = ctypes.c_int
        self._x11.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self._x11.XRaiseWindow.restype = ctypes.c_int
        self._x11.XFlush.argtypes = [ctypes.c_void_p]
        self._x11.XFlush.restype = ctypes.c_int
        self._xext.XShapeCombineRectangles.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
        ]
        self._xext.XShapeCombineRectangles.restype = None
        self._xext.XShapeQueryExtension.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        self._xext.XShapeQueryExtension.restype = ctypes.c_int


class OverlayApplication:
    def __init__(
        self,
        file_descriptor: int,
        bottom_margin_px: int,
        reduced_motion: bool,
        backend: str,
        text_size_px: int = 13,
        transcript_panel: bool = False,
    ) -> None:
        if backend == "wayland":
            # Ahead of the gi import: gtk4-layer-shell has to reach the global symbol
            # scope before PyGObject loads libwayland-client, or its placement calls
            # quietly do nothing and the compositor puts the overlay where it likes.
            unloadable = load_layer_shell()
            if unloadable is not None:
                raise OSError(unloadable)

        import cairo
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import Gio, GLib, Gtk

        self._cairo = cairo
        self._Gio = Gio
        self._GLib = GLib
        self._Gtk = Gtk
        self._backend = backend
        self._layer_shell = None
        self._GdkX11 = None
        self._x11_adapter = None
        if backend == "wayland":
            gi.require_version("Gtk4LayerShell", "1.0")
            from gi.repository import Gtk4LayerShell

            # Checked before any window exists: init_for_window does not raise when
            # Layer Shell is unavailable, it quietly leaves an ordinary toplevel that
            # the compositor places itself. Refusing here keeps a mis-placed overlay
            # off the screen and reports through the existing unavailable path.
            if not Gtk4LayerShell.is_supported():
                raise OSError(COMPOSITOR_LACKS_LAYER_SHELL)
            self._layer_shell = Gtk4LayerShell
        else:
            gi.require_version("GdkX11", "4.0")
            from gi.repository import GdkX11

            self._GdkX11 = GdkX11
            self._x11_adapter = X11WindowAdapter()
        self._socket = socket.socket(fileno=file_descriptor)
        self._bottom_margin_px = bottom_margin_px
        self._reduced_motion = reduced_motion
        self._text_size_px = text_size_px
        self._transcript_panel = transcript_panel
        self._view = RendererViewState()
        self._window = None
        self._drawing_area = None
        self._panel_window = None
        self._panel_area = None
        self._panel_width = 0
        self._panel_geometry = None
        self._selected_monitor = None
        self._selected_geometry = None
        self._phase = 0.0
        self._last_reduced_level_at = float("-inf")
        self._application = Gtk.Application(
            application_id="io.murmly.RecordingOverlay",
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self._application.connect("activate", self._activate)

    def run(self) -> int:
        return int(self._application.run([]))

    def _activate(self, application: Any) -> None:
        window = self._Gtk.ApplicationWindow(application=application)
        window.set_decorated(False)
        window.set_resizable(False)
        window.set_focusable(False)
        window.set_default_size(WINDOW_WIDTH, WINDOW_HEIGHT)
        window.add_css_class("murmly-overlay")
        if self._layer_shell is not None:
            self._layer_shell.init_for_window(window)
            self._layer_shell.set_layer(window, self._layer_shell.Layer.OVERLAY)
            self._layer_shell.set_anchor(window, self._layer_shell.Edge.BOTTOM, True)
            self._layer_shell.set_margin(window, self._layer_shell.Edge.BOTTOM, self._bottom_margin_px)
            self._layer_shell.set_keyboard_mode(window, self._layer_shell.KeyboardMode.NONE)

        css = self._Gtk.CssProvider()
        css.load_from_string("window.murmly-overlay { background: transparent; box-shadow: none; }")
        self._Gtk.StyleContext.add_provider_for_display(
            window.get_display(),
            css,
            self._Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        drawing_area = self._Gtk.DrawingArea()
        drawing_area.set_size_request(WINDOW_WIDTH, WINDOW_HEIGHT)
        drawing_area.set_can_target(False)
        drawing_area.set_draw_func(self._draw)
        window.set_child(drawing_area)
        window.connect("realize", self._on_realize)
        window.connect("map", self._on_map)
        self._window = window
        self._drawing_area = drawing_area

        if self._transcript_panel:
            self._build_panel(application, css)

        threading.Thread(target=self._read_messages, name="murmly-overlay-reader", daemon=True).start()
        self._GLib.timeout_add(33, self._animate)

    def _build_panel(self, application: Any, css: Any) -> None:
        height = panel_height(self._text_size_px)
        window = self._Gtk.ApplicationWindow(application=application)
        window.set_decorated(False)
        window.set_resizable(False)
        window.set_focusable(False)
        window.set_default_size(WINDOW_WIDTH, height)
        window.add_css_class("murmly-overlay")
        if self._layer_shell is not None:
            self._layer_shell.init_for_window(window)
            self._layer_shell.set_layer(window, self._layer_shell.Layer.OVERLAY)
            self._layer_shell.set_anchor(window, self._layer_shell.Edge.BOTTOM, True)
            self._layer_shell.set_margin(
                window,
                self._layer_shell.Edge.BOTTOM,
                max(self._bottom_margin_px - height - PANEL_GAP_PX, 0),
            )
            self._layer_shell.set_keyboard_mode(window, self._layer_shell.KeyboardMode.NONE)
        self._Gtk.StyleContext.add_provider_for_display(
            window.get_display(),
            css,
            self._Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        area = self._Gtk.DrawingArea()
        area.set_size_request(WINDOW_WIDTH, height)
        area.set_can_target(False)
        area.set_draw_func(self._draw_panel)
        window.set_child(area)
        window.connect("realize", self._on_panel_realize)
        window.connect("map", self._on_panel_map)
        self._panel_window = window
        self._panel_area = area

    def _read_messages(self) -> None:
        parser = MessageParser()
        try:
            while data := self._socket.recv(4_096):
                for message in parser.feed(data):
                    self._GLib.idle_add(self._handle_message, message)
        except OSError:
            pass
        self._GLib.idle_add(self._application.quit)

    def _handle_message(self, message: dict[str, object]) -> bool:
        if message["type"] == "level" and self._reduced_motion:
            now = time.monotonic()
            if now - self._last_reduced_level_at < 0.25:
                return False
            self._last_reduced_level_at = now

        stop = self._view.apply(message)
        if stop:
            self._application.quit()
            return False
        if message["type"] == "error":
            generation = self._view.error_generation
            self._GLib.timeout_add(
                int(message["duration_ms"]),
                self._hide_error,
                generation,
            )
        self._sync_visibility()
        if self._drawing_area is not None:
            self._drawing_area.queue_draw()
        # Levels arrive at 30 Hz and never change the panel's content, so
        # redrawing on them would re-run the truncation search 30 times a second.
        if message["type"] != "level":
            self._sync_panel()
        return False

    def _sync_panel(self) -> None:
        if self._panel_window is None:
            return
        showing = bool(self._view.partial) and self._view.state == "LISTENING"
        if not showing:
            self._panel_window.set_visible(False)
            return
        if not self._panel_window.get_visible():
            # Size and place only while hidden. Resizing a mapped window means
            # racing the compositor for its geometry: the width applies but the
            # old left edge survives, which pushes text off the display.
            self._resize_panel_while_hidden()
            if self._backend == "x11":
                self._panel_window.set_visible(True)
            else:
                self._panel_window.present()
        if self._panel_area is not None:
            self._panel_area.queue_draw()

    def _resize_panel_while_hidden(self) -> None:
        if self._selected_geometry is None:
            return
        width = panel_max_width(self._selected_geometry)
        height = panel_height(self._text_size_px)
        if width == self._panel_width and self._panel_geometry == self._selected_geometry:
            return
        self._panel_width = width
        self._panel_geometry = self._selected_geometry
        self._panel_window.set_default_size(width, height)
        self._panel_window.set_size_request(width, height)
        if self._panel_area is not None:
            self._panel_area.set_size_request(width, height)
        self._place_panel(activate=False)

    def _panel_monitor(self, width: int) -> MonitorGeometry:
        """The geometry `panel_width` should bound against.

        Falls back to the drawing width so a draw that arrives before a monitor
        is selected still sizes sensibly.
        """
        if self._panel_geometry is not None:
            return self._panel_geometry
        return MonitorGeometry(connector="", x=0, y=0, width=width, height=0)

    def _select_panel_font(self, context: Any) -> None:
        context.select_font_face("sans-serif", self._cairo.FONT_SLANT_NORMAL, self._cairo.FONT_WEIGHT_NORMAL)
        context.set_font_size(self._text_size_px)

    def _sync_visibility(self) -> None:
        if self._window is None:
            return
        if self._view.visible:
            if self._selected_monitor is None:
                selection = self._select_monitor()
                if selection is not None:
                    self._selected_monitor, self._selected_geometry = selection
                if self._selected_monitor is not None and self._layer_shell is not None:
                    self._layer_shell.set_monitor(self._window, self._selected_monitor)
                    if self._panel_window is not None:
                        # Without this the panel follows the compositor's default
                        # output and can land on a different display than the
                        # indicator it belongs under.
                        self._layer_shell.set_monitor(self._panel_window, self._selected_monitor)
            if self._backend == "x11":
                self._window.set_visible(True)
            else:
                self._window.present()
        else:
            self._window.set_visible(False)
            if self._panel_window is not None:
                self._panel_window.set_visible(False)
            # Forget the panel geometry too: the next recording may select a
            # different monitor, and a width-only check would skip re-placing it.
            self._panel_width = 0
            self._panel_geometry = None
            self._selected_monitor = None
            self._selected_geometry = None

    def _select_monitor(self) -> tuple[object, MonitorGeometry] | None:
        display = self._window.get_display()
        model = display.get_monitors()
        monitors = [model.get_item(index) for index in range(model.get_n_items())]
        geometries = []
        for monitor in monitors:
            geometry = monitor.get_geometry()
            geometries.append(
                MonitorGeometry(
                    connector=monitor.get_connector() or "",
                    x=geometry.x,
                    y=geometry.y,
                    width=geometry.width,
                    height=geometry.height,
                    scale=monitor.get_scale_factor(),
                )
            )
        selected_index = select_monitor_index(geometries)
        if selected_index is None:
            return None
        return monitors[selected_index], geometries[selected_index]

    def _on_realize(self, window: Any) -> None:
        try:
            surface = window.get_surface()
            if surface is not None:
                surface.set_input_region(self._cairo.Region())
            if (
                self._x11_adapter is not None
                and self._GdkX11 is not None
                and surface is not None
                and self._selected_geometry is not None
            ):
                xid = x11_surface_id(self._GdkX11, surface)
                self._x11_adapter.prepare(xid, self._selected_geometry, self._bottom_margin_px)
        except Exception as error:
            self._fail_visual("setup", error)

    def _on_map(self, window: Any) -> None:
        try:
            surface = window.get_surface()
            if (
                self._x11_adapter is not None
                and self._GdkX11 is not None
                and surface is not None
                and self._selected_geometry is not None
            ):
                xid = x11_surface_id(self._GdkX11, surface)
                self._x11_adapter.activate(xid, self._selected_geometry, self._bottom_margin_px)
        except Exception as error:
            self._fail_visual("activation", error)

    def _on_panel_realize(self, window: Any) -> None:
        try:
            surface = window.get_surface()
            if surface is not None:
                surface.set_input_region(self._cairo.Region())
            self._place_panel(activate=False)
        except Exception as error:
            self._fail_visual("transcript panel setup", error)

    def _on_panel_map(self, window: Any) -> None:
        del window
        try:
            self._place_panel(activate=True)
        except Exception as error:
            self._fail_visual("transcript panel activation", error)

    def _place_panel(self, *, activate: bool) -> None:
        if (
            self._x11_adapter is None
            or self._GdkX11 is None
            or self._panel_window is None
            or self._selected_geometry is None
        ):
            return
        surface = self._panel_window.get_surface()
        if surface is None:
            return
        monitor = self._selected_geometry
        height = panel_height(self._text_size_px)
        x, y = panel_position(monitor, self._bottom_margin_px, self._panel_width, height)
        geometry = (x, y, self._panel_width * monitor.scale, height * monitor.scale)
        xid = x11_surface_id(self._GdkX11, surface)
        if activate:
            self._x11_adapter.activate(xid, monitor, self._bottom_margin_px, geometry)
        else:
            self._x11_adapter.prepare(xid, monitor, self._bottom_margin_px, geometry)

    def _draw_panel(self, _area: Any, context: Any, width: int, height: int) -> None:
        """Draw a content-sized panel inside a window whose size never changes.

        The window is created at the maximum width and stays there; what the user
        sees as the panel is this background, sized to the text it holds. That is
        what keeps the visible panel content-sized without ever moving a mapped
        window.
        """
        context.set_operator(self._cairo.OPERATOR_SOURCE)
        context.set_source_rgba(0.0, 0.0, 0.0, 0.0)
        context.paint()
        context.set_operator(self._cairo.OPERATOR_OVER)

        if not self._view.partial:
            return
        self._select_panel_font(context)

        def measure(candidate: str) -> float:
            return float(context.text_extents(candidate).x_advance)

        text = truncate_to_width(
            self._view.partial,
            measure,
            width - 2 * PANEL_HORIZONTAL_PADDING,
        )
        if not text:
            return

        background_width = float(min(panel_width(measure(text), self._panel_monitor(width)), width))
        left = (width - background_width) / 2.0
        self._rounded_rectangle(context, left + 0.5, 0.5, background_width - 1.0, height - 1.0, 8.0)
        context.set_source_rgba(0.07, 0.08, 0.09, 0.92)
        context.fill()

        extents = context.font_extents()
        baseline = (height + extents[0] - extents[1]) / 2.0
        context.set_source_rgb(0.9, 0.92, 0.94)
        context.move_to(left + PANEL_HORIZONTAL_PADDING, baseline)
        context.show_text(text)

    def _fail_visual(self, operation: str, error: Exception) -> None:
        print(f"Error: X11 overlay {operation} failed: {error}", file=sys.stderr)
        if self._window is not None:
            self._window.set_visible(False)
        self._application.quit()

    def _hide_error(self, generation: int) -> bool:
        if self._view.state == "ERROR" and self._view.error_generation == generation:
            self._view.state = "IDLE"
            self._sync_visibility()
        return False

    def _animate(self) -> bool:
        if self._view.state == "THINKING" and not self._reduced_motion:
            self._phase = (self._phase + 0.16) % (2 * math.pi)
            if self._drawing_area is not None:
                self._drawing_area.queue_draw()
        return True

    def _draw(self, _area: Any, context: Any, width: int, height: int) -> None:
        context.set_operator(self._cairo.OPERATOR_SOURCE)
        context.set_source_rgba(0.0, 0.0, 0.0, 0.0)
        context.paint()
        context.set_operator(self._cairo.OPERATOR_OVER)
        self._rounded_rectangle(context, 0.5, 0.5, width - 1.0, height - 1.0, 8.0)
        context.set_source_rgba(0.07, 0.08, 0.09, 0.92)
        context.fill()

        if self._view.state == "ERROR":
            self._draw_error(context)
            return
        self._draw_microphone(context, active=self._view.state == "LISTENING")
        if self._view.state == "THINKING" and self._reduced_motion:
            self._draw_static_processing(context)
        else:
            self._draw_bars(context)

    def _draw_microphone(self, context: Any, *, active: bool) -> None:
        if active:
            context.set_source_rgb(1.0, 0.27, 0.29)
        else:
            context.set_source_rgb(0.88, 0.9, 0.92)
        context.set_line_width(2.2)
        context.arc(25.0, 20.0, 5.0, math.pi, 0.0)
        context.line_to(30.0, 23.0)
        context.arc(25.0, 23.0, 5.0, 0.0, math.pi)
        context.close_path()
        context.stroke()
        context.arc(25.0, 23.0, 9.0, 0.0, math.pi)
        context.stroke()
        context.move_to(25.0, 32.0)
        context.line_to(25.0, 36.0)
        context.move_to(20.0, 36.0)
        context.line_to(30.0, 36.0)
        context.stroke()

    def _draw_bars(self, context: Any) -> None:
        context.set_source_rgb(0.9, 0.92, 0.94)
        multipliers = (0.5, 0.72, 0.9, 1.0, 0.82, 0.65, 0.45)
        for index, multiplier in enumerate(multipliers):
            if self._view.state == "THINKING":
                amplitude = (math.sin(self._phase + index * 0.75) + 1.0) / 2.0
            elif self._view.state == "LISTENING":
                amplitude = self._view.level * multiplier
            else:
                amplitude = 0.0
            if self._reduced_motion and self._view.state == "LISTENING":
                amplitude = round(amplitude * 3.0) / 3.0
            bar_height = 4.0 + amplitude * 22.0
            x = 53.0 + index * 13.0
            y = (WINDOW_HEIGHT - bar_height) / 2.0
            self._rounded_rectangle(context, x, y, 6.0, bar_height, 3.0)
            context.fill()

    def _draw_static_processing(self, context: Any) -> None:
        context.set_source_rgb(0.9, 0.92, 0.94)
        for index in range(3):
            context.arc(76.0 + index * 18.0, 24.0, 3.0, 0.0, 2 * math.pi)
            context.fill()

    def _draw_error(self, context: Any) -> None:
        context.set_source_rgb(1.0, 0.3, 0.3)
        context.set_line_width(3.0)
        context.arc(WINDOW_WIDTH / 2.0, 24.0, 12.0, 0.0, 2 * math.pi)
        context.stroke()
        context.move_to(WINDOW_WIDTH / 2.0, 16.0)
        context.line_to(WINDOW_WIDTH / 2.0, 26.0)
        context.stroke()
        context.arc(WINDOW_WIDTH / 2.0, 31.0, 1.5, 0.0, 2 * math.pi)
        context.fill()

    @staticmethod
    def _rounded_rectangle(context: Any, x: float, y: float, width: float, height: float, radius: float) -> None:
        radius = min(radius, width / 2.0, height / 2.0)
        context.new_sub_path()
        context.arc(x + width - radius, y + radius, radius, -math.pi / 2.0, 0.0)
        context.arc(x + width - radius, y + height - radius, radius, 0.0, math.pi / 2.0)
        context.arc(x + radius, y + height - radius, radius, math.pi / 2.0, math.pi)
        context.arc(x + radius, y + radius, radius, math.pi, 3.0 * math.pi / 2.0)
        context.close_path()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render the Murmly recording overlay.")
    parser.add_argument("--check", action="store_true", help="Check visual runtime dependencies.")
    parser.add_argument("--fd", type=int, default=None, help="Inherited overlay protocol socket.")
    parser.add_argument("--bottom-margin-px", type=int, default=32)
    parser.add_argument("--text-size-px", type=int, default=13)
    parser.add_argument("--transcript-panel", action="store_true")
    parser.add_argument("--reduced-motion", action="store_true")
    parser.add_argument("--backend", choices=sorted(SUPPORTED_BACKENDS), default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    backend = args.backend or os.environ.get("XDG_SESSION_TYPE", "").casefold()
    if args.check:
        result = check_visual_runtime(backend)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["available"] else 1
    if args.fd is None:
        print("Error: --fd is required unless --check is used.", file=sys.stderr)
        return 2
    if backend not in SUPPORTED_BACKENDS:
        print("Error: --backend must select x11 or wayland.", file=sys.stderr)
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
        return OverlayApplication(
            args.fd,
            args.bottom_margin_px,
            args.reduced_motion,
            backend,
            text_size_px=args.text_size_px,
            transcript_panel=args.transcript_panel,
        ).run()
    except (ImportError, OSError, ValueError) as error:
        print(f"Error: overlay runtime unavailable: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())