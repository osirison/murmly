"""macOS focus observation: the owning application, never the window title
(task 14.6, 14.7).

`NSWorkspace.sharedWorkspace().frontmostApplication()` answers this without
needing a single permission -- it returns bundle identifier, process id and
localized name as unprotected metadata (design.md's "Focus observation by
owning application, never by window title"), the same no-permission property
`win_focus.py`'s `GetForegroundWindow`/`GetWindowThreadProcessId`/
`QueryFullProcessImageName` trio has on Windows.

`CGWindowListCopyWindowInfo` is deliberately not used here at all. Since
macOS 10.15 it omits `kCGWindowName` -- the window title -- unless the
process holds Screen Recording permission, while the owning process id and
name remain available regardless. Murmly's delivery target is the
application and process that held focus, exactly what the X11 observer
already records (`_NET_WM_PID` and `WM_CLASS`) and what
`NSRunningApplication` already gives for free, so nothing here needs a
window title and nothing here needs that grant.

`WindowIdentity.window_id` is an X11 concept (a toplevel window's XID) with no
NSWorkspace equivalent -- this fills it with the process id, matching
`win_focus.py`'s own choice to fill `WindowIdentity.window_class` with
something Windows actually has (there, the executable's basename; here, the
bundle identifier) rather than inventing a value neither platform's API
reports. `WindowIdentity.matches` only ever compares this observer's own
readings against each other, never against an X11 or Windows one, so filling
`window_id` with the pid costs nothing: two readings of the *same* frontmost
application still compare equal to each other, which is everything
`should_deliver` (`focus.py`) needs from either field.

Like `mac_clipboard.py`, every native call goes through raw `ctypes`
`objc_msgSend`, never PyObjC, for the reasons that module's own docstring
gives in full -- including the arm64-only, plain-`objc_msgSend`-suffices
simplification design.md's Apple-Silicon-only scope allows. Structured like
`win_focus.py`: the three answers (`frontmost_application`,
`application_pid`, `application_bundle_identifier`) are separate seams, each
defaulted to a real implementation loading `AppKit` from inside its own
function body, so this module stays importable on Linux. Only
`MacosFocusObserver`'s assembly of a `WindowIdentity` from those three
answers is exercised by the test suite; the real Objective-C calls can only
be confirmed on macOS.
"""

from __future__ import annotations

import ctypes
import ctypes.util
from collections.abc import Callable

from murmly.focus import WindowIdentity


_APPKIT_FRAMEWORK_PATH = "/System/Library/Frameworks/AppKit.framework/AppKit"

_appkit_dll: ctypes.CDLL | None = None
_libobjc_dll: ctypes.CDLL | None = None


def _appkit() -> ctypes.CDLL:
    global _appkit_dll
    if _appkit_dll is None:
        _appkit_dll = ctypes.CDLL(_APPKIT_FRAMEWORK_PATH)
    return _appkit_dll


def _libobjc() -> ctypes.CDLL:
    global _libobjc_dll
    if _libobjc_dll is None:
        path = ctypes.util.find_library("objc")
        if path is None:
            raise OSError("libobjc is required to read the frontmost application.")
        _libobjc_dll = ctypes.CDLL(path)
        _libobjc_dll.objc_getClass.restype = ctypes.c_void_p
        _libobjc_dll.objc_getClass.argtypes = [ctypes.c_char_p]
        _libobjc_dll.sel_registerName.restype = ctypes.c_void_p
        _libobjc_dll.sel_registerName.argtypes = [ctypes.c_char_p]
    return _libobjc_dll


def _send(restype: object, argtypes: tuple[object, ...], receiver: int, selector: int, *args: object) -> object:
    """One `objc_msgSend` call, cast fresh for this call alone -- see
    `mac_clipboard._send`'s docstring for exactly why, unchanged here."""
    libobjc = _libobjc()
    function = ctypes.cast(
        libobjc.objc_msgSend, ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)
    )
    return function(receiver, selector, *args)


def _real_frontmost_application() -> int | None:
    """`[[NSWorkspace sharedWorkspace] frontmostApplication]` -- an
    `NSRunningApplication *`, or `nil` when nothing is frontmost (Apple's
    documentation names a brief window during a full-screen space transition
    as the one case this can happen)."""
    libobjc = _libobjc()
    _appkit()  # Loads AppKit, which registers NSWorkspace with the runtime.
    workspace_class = libobjc.objc_getClass(b"NSWorkspace")
    shared_selector = libobjc.sel_registerName(b"sharedWorkspace")
    workspace = _send(ctypes.c_void_p, (), workspace_class, shared_selector)
    if not workspace:
        return None
    frontmost_selector = libobjc.sel_registerName(b"frontmostApplication")
    application = _send(ctypes.c_void_p, (), workspace, frontmost_selector)
    return application if application else None


def _real_application_pid(application: int) -> int | None:
    """`[application processIdentifier]` -- a `pid_t` (`int32_t`)."""
    libobjc = _libobjc()
    selector = libobjc.sel_registerName(b"processIdentifier")
    pid = _send(ctypes.c_int32, (), application, selector)
    return int(pid) if pid else None


def _real_application_bundle_identifier(application: int) -> str | None:
    """`[application bundleIdentifier]`, decoded through `UTF8String` -- `nil`
    for a process with no bundle, which Apple's documentation says can
    happen for a command-line tool that became the frontmost application."""
    libobjc = _libobjc()
    selector = libobjc.sel_registerName(b"bundleIdentifier")
    ns_string = _send(ctypes.c_void_p, (), application, selector)
    if not ns_string:
        return None
    utf8_selector = libobjc.sel_registerName(b"UTF8String")
    char_pointer = _send(ctypes.c_char_p, (), ns_string, utf8_selector)
    if char_pointer is None:
        return None
    return char_pointer.decode("utf-8", "replace")


class MacosFocusObserver:
    """Reads the frontmost application, needing no permission (task 14.6)."""

    def __init__(
        self,
        frontmost_application: Callable[[], int | None] = _real_frontmost_application,
        application_pid: Callable[[int], int | None] = _real_application_pid,
        application_bundle_identifier: Callable[[int], str | None] = _real_application_bundle_identifier,
    ) -> None:
        self._frontmost_application = frontmost_application
        self._application_pid = application_pid
        self._application_bundle_identifier = application_bundle_identifier

    @property
    def supported(self) -> bool:
        return True

    @property
    def detail(self) -> str | None:
        return None

    def active_window(self) -> WindowIdentity | None:
        application = self._frontmost_application()
        if application is None:
            return None
        pid = self._application_pid(application)
        if pid is None:
            return None
        bundle_identifier = self._application_bundle_identifier(application)
        return WindowIdentity(window_id=pid, pid=pid, window_class=bundle_identifier)
