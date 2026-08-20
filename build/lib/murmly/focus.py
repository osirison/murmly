from __future__ import annotations

from collections.abc import Callable
import ctypes
import ctypes.util
from dataclasses import dataclass
import logging
from typing import Protocol

from murmly.integrations import is_wayland_session


logger = logging.getLogger(__name__)


ANY_PROPERTY_TYPE = 0
MAX_PROPERTY_ITEMS = 1_024


@dataclass(frozen=True, slots=True)
class WindowIdentity:
    """Identity of a toplevel window, paired so a recycled window id cannot masquerade."""

    window_id: int
    pid: int | None = None
    window_class: str | None = None

    def matches(self, other: "WindowIdentity | None") -> bool:
        if other is None:
            return False
        return (
            self.window_id == other.window_id
            and self.pid == other.pid
            and self.window_class == other.window_class
        )


class FocusObserver(Protocol):
    @property
    def supported(self) -> bool: ...

    @property
    def detail(self) -> str | None: ...

    def active_window(self) -> WindowIdentity | None: ...


class NullFocusObserver:
    """Used when the session cannot expose the focused window at all."""

    def __init__(self, detail: str | None = None) -> None:
        self._detail = detail

    @property
    def supported(self) -> bool:
        return False

    @property
    def detail(self) -> str | None:
        return self._detail

    def active_window(self) -> WindowIdentity | None:
        return None


def _load_x11() -> ctypes.CDLL:
    path = ctypes.util.find_library("X11")
    if not path:
        raise OSError("libX11 is required to verify the transcript delivery target.")
    return ctypes.CDLL(path)


_ERROR_HANDLER_TYPE = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)


def _ignore_x_error(_display: object, _event: object) -> int:
    return 0


# Held at module scope so ctypes does not collect the trampoline while X11 holds it.
_IGNORE_X_ERROR = _ERROR_HANDLER_TYPE(_ignore_x_error)


class X11FocusObserver:
    """Reads the active toplevel through EWMH, opening a display per read.

    Xlib's default error handler exits the process on BadWindow, which a target that
    closed mid-transcription would trigger, so the handler is replaced on construction.
    """

    def __init__(self, x11: ctypes.CDLL | None = None) -> None:
        self._x11 = x11 if x11 is not None else _load_x11()
        self._configure_signatures()
        self._x11.XSetErrorHandler(_IGNORE_X_ERROR)

    @property
    def supported(self) -> bool:
        return True

    @property
    def detail(self) -> str | None:
        return None

    def probe(self) -> bool:
        """True when the window manager publishes _NET_ACTIVE_WINDOW at all."""
        display = self._x11.XOpenDisplay(None)
        if not display:
            return False
        try:
            root = self._x11.XDefaultRootWindow(display)
            return self._property(display, root, "_NET_ACTIVE_WINDOW") is not None
        finally:
            self._x11.XCloseDisplay(display)

    def active_window(self) -> WindowIdentity | None:
        display = self._x11.XOpenDisplay(None)
        if not display:
            return None
        try:
            root = self._x11.XDefaultRootWindow(display)
            active = self._property(display, root, "_NET_ACTIVE_WINDOW")
            if not isinstance(active, list) or not active:
                return None
            window_id = active[0]
            if window_id == 0:
                return None
            pid = self._property(display, window_id, "_NET_WM_PID")
            window_class = self._property(display, window_id, "WM_CLASS")
            return WindowIdentity(
                window_id=window_id,
                pid=pid[0] if isinstance(pid, list) and pid else None,
                window_class=_decode_window_class(window_class),
            )
        finally:
            self._x11.XCloseDisplay(display)

    def _property(self, display: int, window: int, name: str) -> list[int] | bytes | None:
        atom = self._x11.XInternAtom(display, name.encode("ascii"), True)
        if atom == 0:
            return None
        actual_type = ctypes.c_ulong()
        actual_format = ctypes.c_int()
        item_count = ctypes.c_ulong()
        bytes_after = ctypes.c_ulong()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        status = self._x11.XGetWindowProperty(
            display,
            window,
            atom,
            0,
            MAX_PROPERTY_ITEMS,
            False,
            ANY_PROPERTY_TYPE,
            ctypes.byref(actual_type),
            ctypes.byref(actual_format),
            ctypes.byref(item_count),
            ctypes.byref(bytes_after),
            ctypes.byref(data),
        )
        if status != 0 or not data:
            return None
        try:
            if item_count.value == 0:
                return None
            if actual_format.value == 32:
                values = ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))
                return [int(values[index]) for index in range(item_count.value)]
            return bytes(bytearray(data[index] for index in range(item_count.value)))
        finally:
            self._x11.XFree(data)

    def _configure_signatures(self) -> None:
        self._x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self._x11.XOpenDisplay.restype = ctypes.c_void_p
        self._x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self._x11.XCloseDisplay.restype = ctypes.c_int
        self._x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self._x11.XDefaultRootWindow.restype = ctypes.c_ulong
        self._x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        self._x11.XInternAtom.restype = ctypes.c_ulong
        self._x11.XGetWindowProperty.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_long,
            ctypes.c_long,
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
        ]
        self._x11.XGetWindowProperty.restype = ctypes.c_int
        self._x11.XFree.argtypes = [ctypes.c_void_p]
        self._x11.XFree.restype = ctypes.c_int
        self._x11.XSetErrorHandler.argtypes = [_ERROR_HANDLER_TYPE]
        self._x11.XSetErrorHandler.restype = ctypes.c_void_p


def _decode_window_class(value: object) -> str | None:
    if not isinstance(value, bytes):
        return None
    parts = [part for part in value.split(b"\x00") if part]
    if not parts:
        return None
    return parts[-1].decode("utf-8", "replace")


def create_focus_observer(
    env: dict[str, str] | None = None,
    x11_loader: Callable[[], ctypes.CDLL] = _load_x11,
) -> FocusObserver:
    """Classify the session once: verifying, or unverified and delivering as before."""
    if is_wayland_session(env):
        return NullFocusObserver("Delivery target verification requires an X11 session.")
    try:
        observer = X11FocusObserver(x11_loader())
    except OSError as error:
        return NullFocusObserver(str(error))
    if not observer.probe():
        return NullFocusObserver("The active window manager does not publish _NET_ACTIVE_WINDOW.")
    return observer


def record_target(observer: FocusObserver) -> WindowIdentity | None:
    """Read the intended target while capture stops. Never raises into the lifecycle."""
    try:
        return observer.active_window()
    except Exception as error:
        logger.warning("Delivery target could not be recorded: %s", error)
        return None


def should_deliver(
    observer: FocusObserver,
    target: WindowIdentity | None,
    verify_enabled: bool,
) -> tuple[bool, str | None]:
    """Decide whether the paste may be injected. Fails closed on a verifying session."""
    if not verify_enabled:
        return True, None
    if not observer.supported:
        return True, None
    if target is None:
        return False, "no delivery target was recorded"
    try:
        current = observer.active_window()
    except Exception:
        return False, "the focused window could not be read"
    if current is None:
        return False, "the focused window could not be read"
    if not target.matches(current):
        return False, "focus moved away from the delivery target"
    return True, None
