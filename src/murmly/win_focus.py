"""Windows focus observation: the owning application, never the window title.

`GetForegroundWindow`, `GetWindowThreadProcessId` and `QueryFullProcessImageName`
answer task 9.4 without needing a single permission -- none of the three asks
anything about a process the same user owns (design.md's "Focus observation by
owning application, never by window title"). `PROCESS_QUERY_LIMITED_INFORMATION`
is the access right `OpenProcess` is opened with here specifically because it is
the smallest one `QueryFullProcessImageName` accepts; anything broader would be
asking for more than this needs and would be the first thing to fail on a
process this user does not own, which the design note above says is not a case
worth guarding against for a process it already does.

`WindowIdentity.window_class` is a Linux/X11 concept (`WM_CLASS`) with no direct
Windows equivalent, so this fills it with the executable's own basename instead
-- the closest thing Windows has to "which application", and paired with `pid`
exactly as `WindowIdentity.matches` already expects (see `focus.py`).

As with `win_pipe.py`, `win_hotkey.py` and `win_clipboard.py`, every Win32
call lives behind a first-class seam (`foreground_window`, `window_pid`,
`process_image_path`), each defaulted to a real implementation that only
loads `user32`/`kernel32` (via the module-private `_user32()`/`_kernel32()`
below) from inside its own function body, so this module stays importable
on Linux. Only `WindowsFocusObserver`'s assembly of a
`WindowIdentity` from those three answers is exercised by the test suite; the
real Win32 calls can only be confirmed on Windows.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from collections.abc import Callable

from murmly.focus import WindowIdentity


#: `OpenProcess`'s smallest access right that still lets `QueryFullProcessImageNameW`
#: answer -- see the module docstring for why nothing broader is asked for.
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

#: `QueryFullProcessImageNameW`'s `dwFlags`: 0 asks for the Win32 path form
#: (`C:\...`), not the native NT device path `dwFlags=1` would give.
_WIN32_PATH_FORMAT = 0

#: Every `kernel32`/`user32` entry point this module calls, with the
#: `restype`/`argtypes` ctypes needs so it stops defaulting an undeclared
#: function's `restype` to `c_int` -- 32 bits. `GetForegroundWindow`
#: returns an `HWND` and `OpenProcess` returns a `HANDLE`, both 64-bit
#: pointers on 64-bit Windows: an undeclared `restype` truncates either
#: before this module ever uses it, the same defect class `win_clipboard.py`
#: shipped with (see that module's `_KERNEL32_SIGNATURES` docstring).
#: `test_win_ctypes_signatures.py` scans this module's source for every
#: attribute call made on the `user32`/`kernel32` handles below and asserts
#: each callee has an entry here, so a future call added with no declared
#: signature fails on Linux.
_USER32_SIGNATURES: dict[str, tuple[object, tuple[object, ...]]] = {
    "GetForegroundWindow": (wintypes.HWND, ()),
    "GetWindowThreadProcessId": (
        wintypes.DWORD,
        (wintypes.HWND, ctypes.POINTER(wintypes.DWORD)),
    ),
}

_KERNEL32_SIGNATURES: dict[str, tuple[object, tuple[object, ...]]] = {
    "OpenProcess": (
        wintypes.HANDLE,
        (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD),
    ),
    "QueryFullProcessImageNameW": (
        wintypes.BOOL,
        (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)),
    ),
    "CloseHandle": (wintypes.BOOL, (wintypes.HANDLE,)),
}

#: Lazily-loaded, module-private library handles -- never ctypes' own
#: shared, process-wide loader cache, following `win_clipboard.py`'s
#: precedent so declaring a signature here can never change behaviour for
#: some other, unrelated caller of the same DLL.
_user32_dll: ctypes.WinDLL | None = None
_kernel32_dll: ctypes.WinDLL | None = None


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


def _kernel32() -> ctypes.WinDLL:
    global _kernel32_dll
    if _kernel32_dll is None:
        _kernel32_dll = ctypes.WinDLL("kernel32")
        _configure(_kernel32_dll, _KERNEL32_SIGNATURES)
    return _kernel32_dll


def _real_foreground_window() -> int | None:
    hwnd = _user32().GetForegroundWindow()
    return int(hwnd) if hwnd else None


def _real_window_pid(hwnd: int) -> int | None:
    pid = wintypes.DWORD()
    _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value) or None


def _real_process_image_path(pid: int) -> str | None:
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # No handle at all: the process exited between `GetWindowThreadProcessId`
        # answering and this call, or belongs to another user session. Either
        # way there is no path to read, not an error worth raising.
        return None
    try:
        buffer_size = wintypes.DWORD(260)
        buffer = ctypes.create_unicode_buffer(buffer_size.value)
        ok = kernel32.QueryFullProcessImageNameW(
            handle, _WIN32_PATH_FORMAT, buffer, ctypes.byref(buffer_size)
        )
        if not ok:
            return None
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def _basename(path: str) -> str:
    """`os.path.basename`, but for a Windows path even when running on Linux."""
    return path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]


class WindowsFocusObserver:
    """Reads the foreground window's owning process, needing no permission."""

    def __init__(
        self,
        foreground_window: Callable[[], int | None] = _real_foreground_window,
        window_pid: Callable[[int], int | None] = _real_window_pid,
        process_image_path: Callable[[int], str | None] = _real_process_image_path,
    ) -> None:
        self._foreground_window = foreground_window
        self._window_pid = window_pid
        self._process_image_path = process_image_path

    @property
    def supported(self) -> bool:
        return True

    @property
    def detail(self) -> str | None:
        return None

    def active_window(self) -> WindowIdentity | None:
        hwnd = self._foreground_window()
        if hwnd is None:
            return None
        pid = self._window_pid(hwnd)
        image_path = self._process_image_path(pid) if pid is not None else None
        return WindowIdentity(
            window_id=hwnd,
            pid=pid,
            window_class=_basename(image_path) if image_path else None,
        )
