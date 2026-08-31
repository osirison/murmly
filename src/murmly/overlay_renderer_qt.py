"""Windows' and macOS's overlay renderer: the same protocol and states, drawn with Qt.

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
`win_clipboard.py` already hold for their own Win32 loading.

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

Task 15.1/15.2/15.3: macOS is attempted through this exact same renderer
first, per design.md's own spike order ("against Qt's own flags first"). The
window-flags code above is already backend-neutral -- every window this
module creates requests `Qt.WindowTransparentForInput` and
`Qt.WindowDoesNotAcceptFocus` regardless of platform -- so nothing about
*asking* differs for macOS. What differs is verification:
`missing_property_for_macos_window` reads the real `NSWindow`'s `level`,
`ignoresMouseEvents` and `canBecomeKeyWindow` back through raw `ctypes`
`objc_msgSend` calls (the same convention `mac_clipboard.py` and
`mac_focus.py` already established for this codebase, and never PyObjC --
see either module's docstring for the arm64-only reasoning) rather than
assuming Qt's flags reached AppKit. It only *reads*: design.md's Risks
section calls reflecting AppKit calls onto Qt's own `NSWindow` "a community
technique with no citable confirmation", and that caution is about writing
through the handle, which this never does, not about observing what is
already there. Where the read-back finds a property missing, the overlay is
refused exactly as `_verify_and_show`'s Windows branch already refuses one,
which is task 15.3's honest outcome on a machine that cannot provide the
missing property -- and if the property is not actually missing, section
15's own spike (15.1) is answered without needing a second renderer (task
15.2's `NSPanel` fallback) at all.

Unverified on real macOS, the same as the Windows half above -- and for an
additional reason there too: this module's real window (unlike `--check`'s
`-platform offscreen` probe) needs an actual Cocoa window server, which a
headless CI runner does not have. Whether the macOS CI runner this project
uses has one at all is itself unconfirmed from here; either way, "a synthetic
click genuinely passes through the live window underneath this one" is not
something any CI run -- headless or not -- can show without a person present
to click. `scripts/macos_overlay_spike.py` is the one-step check for someone
with a Mac and a screen: it prints the three read-back values below plus
what to click to confirm click-through and non-activation directly. See task
15.1 in `openspec/changes/all-os-distributions/tasks.md`.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
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


SUPPORTED_BACKENDS = {"windows", "macos"}

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


#: `GetWindowLongPtrW`/`SetWindowLongPtrW` both return `LONG_PTR` --
#: pointer-sized, 64 bits on 64-bit Windows -- and `SetWindowLongPtrW`'s
#: third argument is the same width. Left undeclared, ctypes defaults
#: `restype` to a truncating 32-bit `c_int` and the second argument's
#: `int` to a 32-bit `c_int` as well: the exact defect class
#: `win_clipboard.py`'s `_KERNEL32_SIGNATURES` docstring explains in full,
#: for `GlobalAlloc`/`GlobalLock`/`GlobalFree`. `test_win_ctypes_signatures.py`
#: scans this module's source for every call made through the `user32`
#: handle and asserts each callee has a declared, well-typed signature here.
_USER32_SIGNATURES: dict[str, tuple[object, tuple[object, ...]]] = {
    "GetWindowLongPtrW": (
        ctypes.c_ssize_t,
        (wintypes.HWND, ctypes.c_int),
    ),
    "SetWindowLongPtrW": (
        ctypes.c_ssize_t,
        (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t),
    ),
}

#: Lazily-loaded, module-private library handle -- never ctypes' own
#: shared, process-wide loader cache, following `win_clipboard.py`'s
#: precedent so declaring a signature here can never change behaviour for
#: some other, unrelated caller of `user32`.
_user32_dll: ctypes.WinDLL | None = None


def _configure(dll: ctypes.WinDLL, signatures: dict[str, tuple[object, tuple[object, ...]]]) -> None:
    for name, (restype, argtypes) in signatures.items():
        function = getattr(dll, name)
        function.restype = restype
        function.argtypes = argtypes


def _user32() -> ctypes.WinDLL:
    global _user32_dll
    if _user32_dll is None:
        _user32_dll = ctypes.WinDLL("user32")
        _configure(_user32_dll, _USER32_SIGNATURES)
    return _user32_dll


def _real_get_window_long(hwnd: int) -> int:
    user32 = _user32()
    return int(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))


def _real_set_window_long(hwnd: int, exstyle: int) -> int:
    user32 = _user32()
    return int(user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, exstyle))


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
# Task 15.1/15.3: the native `NSWindow` state, read back and never mutated.
# --------------------------------------------------------------------------

#: `NSNormalWindowLevel` (`AppKit/NSWindow.h`). Any window Qt's own
#: `Qt.WindowStaysOnTopHint` actually raised above ordinary application
#: windows reads back a `level` higher than this -- `NSFloatingWindowLevel`
#: is `3`, and Qt requests at least that much for the hint -- so the
#: comparison below never needs the higher levels' own names.
NS_NORMAL_WINDOW_LEVEL = 0

#: In the same order `REQUIRED_EXSTYLE_PROPERTIES` states them, so the first
#: missing property named here means the same thing it means there: the
#: first one the `recording-overlay` spec's "Non-disruptive placement on
#: every platform" requirement lists.
REQUIRED_MACOS_WINDOW_PROPERTIES: tuple[tuple[str, Callable[[MacosWindowProperties], bool]], ...] = (
    ("staying above ordinary application windows", lambda properties: properties.level > NS_NORMAL_WINDOW_LEVEL),
    ("not taking keyboard focus", lambda properties: not properties.can_become_key_window),
    ("not intercepting pointer input", lambda properties: properties.ignores_mouse_events),
)


class MacosWindowProperties:
    """The three `NSWindow` readings task 15.1's spike is actually about.

    A plain container rather than a `dataclass`: this file has no top-level
    `dataclasses` import today and adding one for three fields used in
    exactly one place is not worth it. Constructed by `_real_macos_window_properties`
    from a real read-back, and directly by `missing_property_for_macos_window`'s
    own tests with no `ctypes` or macOS involved at all -- the same
    real-call/pure-function split `apply_and_verify_exstyle`/
    `missing_property_for_exstyle` keeps for Windows.
    """

    __slots__ = ("level", "ignores_mouse_events", "can_become_key_window")

    def __init__(self, level: int, ignores_mouse_events: bool, can_become_key_window: bool) -> None:
        self.level = level
        self.ignores_mouse_events = ignores_mouse_events
        self.can_become_key_window = can_become_key_window


def missing_property_for_macos_window(properties: MacosWindowProperties) -> str | None:
    """Which spec-required property `properties` lacks, or `None` if it has all three.

    Pure, and the whole reason task 15.1's spike is checkable without a real
    `NSWindow`: given the three readings a real `objc_msgSend` round-trip
    would produce, this is the entire decision of whether the overlay may be
    shown -- the macOS twin of `missing_property_for_exstyle`.
    """
    for name, holds in REQUIRED_MACOS_WINDOW_PROPERTIES:
        if not holds(properties):
            return name
    return None


#: Lazily-loaded, module-private `libobjc` handle -- the same
#: `_libobjc()`/`_send()` pair `mac_clipboard.py` and `mac_focus.py` each
#: keep privately for the same reason their own docstrings give: never
#: ctypes' shared, process-wide loader cache, and never PyObjC. Duplicated
#: here rather than imported from either of those modules because each one
#: keeps its own copy already -- `mac_focus.py`'s is a byte-for-byte repeat
#: of `mac_clipboard.py`'s, credited in its own docstring rather than
#: imported -- and this module has to stay importable with neither of those
#: two modules present, on any platform, at that.
_libobjc_dll: ctypes.CDLL | None = None


def _libobjc() -> ctypes.CDLL:
    global _libobjc_dll
    if _libobjc_dll is None:
        import ctypes.util

        path = ctypes.util.find_library("objc")
        if path is None:
            raise OSError("libobjc is required to read the overlay window's native state.")
        _libobjc_dll = ctypes.CDLL(path)
        _libobjc_dll.sel_registerName.restype = ctypes.c_void_p
        _libobjc_dll.sel_registerName.argtypes = [ctypes.c_char_p]
    return _libobjc_dll


def _send(restype: object, argtypes: tuple[object, ...], receiver: int, selector: int) -> object:
    """One `objc_msgSend` call, cast fresh for this call alone -- see
    `mac_clipboard._send`'s docstring for exactly why, unchanged here."""
    libobjc = _libobjc()
    function = ctypes.cast(
        libobjc.objc_msgSend, ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)
    )
    return function(receiver, selector)


def _real_macos_window_properties(nsview: int) -> MacosWindowProperties:
    """Read a Qt window's real `NSWindow` state through `nsview = winId()`.

    `QWidget.winId()` on macOS returns the view's `NSView*`, not its
    `NSWindow*` -- `[nsview window]` is what reaches the window itself.
    Everything from here on only *reads*: `level`, `ignoresMouseEvents` and
    `canBecomeKeyWindow` are all read-only-in-spirit queries this module
    never calls the paired setter for (`setLevel:`, `setIgnoresMouseEvents:`,
    overriding `canBecomeKeyWindow` all require subclassing or a private
    `NSWindow` category this module does not create) -- see this file's own
    module docstring for why mutating Qt's `NSWindow` is deliberately never
    attempted here.
    """
    libobjc = _libobjc()
    window = _send(ctypes.c_void_p, (), nsview, libobjc.sel_registerName(b"window"))
    if not window:
        raise RuntimeError("The Qt view has no native NSWindow yet.")
    level = _send(ctypes.c_long, (), window, libobjc.sel_registerName(b"level"))
    ignores_mouse_events = _send(ctypes.c_bool, (), window, libobjc.sel_registerName(b"ignoresMouseEvents"))
    can_become_key_window = _send(ctypes.c_bool, (), window, libobjc.sel_registerName(b"canBecomeKeyWindow"))
    return MacosWindowProperties(
        level=int(level),
        ignores_mouse_events=bool(ignores_mouse_events),
        can_become_key_window=bool(can_become_key_window),
    )


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
        """Task 10.5/15.1/15.3: refuse to keep presenting a window missing a
        required property.

        Runs once the window has a native handle (`winId()` forces creation
        of one), which is why this happens after `show()` rather than in
        `__init__` -- the same "only once realized" timing
        `overlay_renderer.OverlayApplication._on_realize`/`_on_map` already
        use for the X11 adapter calls. Windows *mutates* the native handle
        before reading it back (`apply_and_verify_exstyle`); macOS only
        reads (`_real_macos_window_properties`) -- see this module's own
        docstring for why the two backends differ there. Neither branch runs
        for any other backend, which reaches here with `missing` staying
        `None` -- Qt's own flags are all the GTK4/X11/Wayland renderer's
        equivalent (`overlay_renderer.py`'s adapter calls) ever had to rely
        on either, so this is not a regression for them.
        """
        try:
            native_id = int(window.winId())
            if self._backend == "windows":
                missing = apply_and_verify_exstyle(native_id)
            elif self._backend == "macos":
                missing = missing_property_for_macos_window(_real_macos_window_properties(native_id))
            else:
                missing = None
        except Exception as error:  # noqa: BLE001 - any failure here means "cannot verify, refuse"
            self._fail_visual("placement verification", error)
            return
        if missing is not None:
            self._fail_visual("placement", OSError(f"The platform did not grant: {missing}."))

    def _fail_visual(self, operation: str, error: Exception) -> None:
        label = {"windows": "Windows", "macos": "macOS"}.get(self._backend, self._backend)
        print(f"Error: {label} overlay {operation} failed: {error}", file=sys.stderr)
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
