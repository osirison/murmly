"""Windows clipboard and paste injection: the Win32 API, never `clip.exe`.

`clip.exe` reads stdin through the console's OEM/ANSI codepage and mangles
anything outside it (design.md's "Clipboard and paste injection"), which for a
transcription tool is a defect in the product's whole purpose. So this module
goes straight to `user32`/`kernel32` with `CF_UNICODETEXT` (task 9.1): the
clipboard always carries UTF-16LE, with no codepage in between to lose
anything to.

The paste itself goes through `SendInput` (task 9.2), and task 9.3 registers
it under the rule `integrations.py` already wrote for KDE's input-consent
dialog: a method that returns success whether or not the keystroke reached the
focused window must never be trusted to overwrite the transcript it is
supposed to have delivered. UIPI (User Interface Privilege Isolation) is why
`SendInput` qualifies -- it silently discards synthetic input aimed at a
window belonging to a higher-integrity process, and an unelevated Murmly
pasting into an elevated window gets no error back at all. So
`WindowsClipboardPaster` reads the previous clipboard contents, and restores
them afterwards, only when `select_paste_injection`'s answer says the method
running can confirm delivery -- which, for Windows today, is never `True`.

As with `win_pipe.py` and `win_hotkey.py`, every Win32 call lives behind a
small first-class seam (`write_clipboard`, `read_clipboard`, `send_ctrl_v`),
each defaulted to a real implementation that only loads `user32`/`kernel32`
(via the module-private `_user32()`/`_kernel32()` below) from inside its own
function body, so this module stays importable on Linux. Only
the seams and `WindowsClipboardPaster`'s policy around them (never reading the
clipboard before an unconfirmable paste, never restoring over one) are
exercised by the test suite; the real Win32 calls -- `GlobalAlloc` actually
allocating movable memory, `SendInput` actually reaching a window -- can only
be confirmed on Windows.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from collections.abc import Callable
import logging
import time

from murmly.integrations import DeliveryOutcome, PasteInjection, select_paste_injection


logger = logging.getLogger(__name__)

#: `winuser.h`'s `CF_UNICODETEXT`: UTF-16LE text, null-terminated, in a
#: `GMEM_MOVEABLE` global -- the format that carries every code point without
#: passing through a console codepage (task 9.1).
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

#: `winuser.h`'s virtual-key codes for the paste chord, and `INPUT_KEYBOARD` /
#: `KEYEVENTF_KEYUP` for the `SendInput` array (task 9.2).
VK_CONTROL = 0x11
VK_V = 0x56
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

#: `integrations.select_paste_injection`'s method name for this backend --
#: read by `platform_diagnostics` and by `WindowsClipboardPaster.injection`,
#: never re-derived from `PasteInjection.method`'s string elsewhere.
SEND_INPUT_METHOD = "send-input"


class _KEYBDINPUT(ctypes.Structure):
    """`winuser.h`'s `KEYBDINPUT`, defined once at module scope.

    Defining this (and `_InputUnion`/`_INPUT` below) inside `_keybd_input`,
    as a fresh class built on every call, was the module's second real bug:
    each of the four calls `_real_send_ctrl_v` makes to build one chord
    produced its *own* `INPUT` class -- structurally identical but a
    different Python type every time -- and `ctypes.Array` construction
    rejects mixing instances of "the same" structure from different class
    objects with `TypeError: incompatible types`. That defect was masked by
    the `GlobalLock` bug (`_real_write_clipboard` always raised first, so
    `_real_send_ctrl_v` was never reached) and would have become the next CI
    failure the moment that one was fixed. One shared class per process
    fixes both problems at once.

    `dwExtraInfo` is `ULONG_PTR` in `winuser.h` -- pointer-sized. `wintypes`
    resolves `WPARAM` to whichever of `c_ulong`/`c_ulonglong` matches
    `sizeof(c_void_p)` on the platform ctypes is running against (see
    `Lib/ctypes/wintypes.py`), so it is already the correct substitute here
    on every architecture ctypes targets, unlike a hand-picked `c_ulong`.
    """

    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    )


class _InputUnion(ctypes.Union):
    _fields_ = (("ki", _KEYBDINPUT),)


class _INPUT(ctypes.Structure):
    _anonymous_ = ("_input",)
    _fields_ = (("type", wintypes.DWORD), ("_input", _InputUnion))


#: Every `kernel32`/`user32` entry point this module calls, with the
#: `restype`/`argtypes` ctypes needs to stop defaulting an undeclared
#: function's `restype` to `c_int` -- 32 bits. That default is the module's
#: first real bug: `GlobalAlloc`, `GlobalLock` and `GlobalFree` all return an
#: `HGLOBAL`, a 64-bit pointer on 64-bit Windows, so an undeclared `restype`
#: truncated the value `GlobalAlloc` returned before `GlobalLock` ever saw
#: it -- the allocation succeeded; the handle reaching the next call was not
#: the one it returned. `test_win_ctypes_signatures.py` scans this module's
#: source for every attribute call made on the `kernel32`/`user32` handles
#: below and asserts each callee has an entry here, so a future call added
#: with no declared signature fails on Linux instead of on a Windows machine.
_KERNEL32_SIGNATURES: dict[str, tuple[object, tuple[object, ...]]] = {
    "GlobalAlloc": (wintypes.HGLOBAL, (wintypes.UINT, ctypes.c_size_t)),
    "GlobalLock": (wintypes.LPVOID, (wintypes.HGLOBAL,)),
    "GlobalUnlock": (wintypes.BOOL, (wintypes.HGLOBAL,)),
    "GlobalFree": (wintypes.HGLOBAL, (wintypes.HGLOBAL,)),
}

_USER32_SIGNATURES: dict[str, tuple[object, tuple[object, ...]]] = {
    "OpenClipboard": (wintypes.BOOL, (wintypes.HWND,)),
    "EmptyClipboard": (wintypes.BOOL, ()),
    "SetClipboardData": (wintypes.HANDLE, (wintypes.UINT, wintypes.HANDLE)),
    "CloseClipboard": (wintypes.BOOL, ()),
    "IsClipboardFormatAvailable": (wintypes.BOOL, (wintypes.UINT,)),
    "GetClipboardData": (wintypes.HANDLE, (wintypes.UINT,)),
    "SendInput": (wintypes.UINT, (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)),
}

#: Lazily-loaded, module-private library handles -- never ctypes' own
#: shared, process-wide loader cache, so declaring a signature here can
#: never change behaviour for some other, unrelated caller of the same DLL
#: (`test_win_hotkey.py`'s own runtime-integration test builds its own
#: `INPUT` array and calls `SendInput` through that shared cache; it must
#: not see this module's `argtypes`). Loaded and configured once per
#: process, the first time either is needed.
_kernel32_dll: ctypes.WinDLL | None = None
_user32_dll: ctypes.WinDLL | None = None


def _configure(dll: ctypes.WinDLL, signatures: dict[str, tuple[object, tuple[object, ...]]]) -> None:
    for name, (restype, argtypes) in signatures.items():
        function = getattr(dll, name)
        function.restype = restype
        function.argtypes = argtypes


def _kernel32() -> ctypes.WinDLL:
    global _kernel32_dll
    if _kernel32_dll is None:
        _kernel32_dll = ctypes.WinDLL("kernel32")
        _configure(_kernel32_dll, _KERNEL32_SIGNATURES)
    return _kernel32_dll


def _user32() -> ctypes.WinDLL:
    global _user32_dll
    if _user32_dll is None:
        _user32_dll = ctypes.WinDLL("user32")
        _configure(_user32_dll, _USER32_SIGNATURES)
    return _user32_dll


def _real_write_clipboard(text: str) -> None:
    """`OpenClipboard` / `EmptyClipboard` / `SetClipboardData(CF_UNICODETEXT)`.

    The buffer is UTF-16LE with a terminating null, allocated `GMEM_MOVEABLE`
    because `SetClipboardData` requires a movable, unlocked handle. Ownership
    of that handle passes to the system the moment `SetClipboardData` succeeds
    -- freeing it afterwards would free memory the clipboard still holds, so
    it is freed only on the failure path, where the system never took it.
    """
    kernel32 = _kernel32()
    user32 = _user32()

    encoded = text.encode("utf-16-le") + b"\x00\x00"
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
    if not handle:
        raise OSError("GlobalAlloc failed while preparing the clipboard text.")
    locked = kernel32.GlobalLock(handle)
    if not locked:
        kernel32.GlobalFree(handle)
        raise OSError("GlobalLock failed while preparing the clipboard text.")
    try:
        ctypes.memmove(locked, encoded, len(encoded))
    finally:
        kernel32.GlobalUnlock(handle)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        raise OSError("OpenClipboard failed; another process may hold it.")
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, wintypes.HANDLE(handle)):
            # The system took no ownership on failure -- this is the one path
            # that frees the handle after a successful GlobalAlloc.
            kernel32.GlobalFree(handle)
            raise OSError("SetClipboardData failed.")
    finally:
        user32.CloseClipboard()


def _real_read_clipboard() -> str | None:
    """The current `CF_UNICODETEXT` clipboard contents, or `None` if it holds none.

    Reads through `GlobalLock` on the handle `GetClipboardData` returns
    without taking ownership of it -- that handle belongs to the clipboard for
    as long as the clipboard is open, and is never freed here.
    """
    kernel32 = _kernel32()
    user32 = _user32()

    if not user32.OpenClipboard(None):
        raise OSError("OpenClipboard failed; another process may hold it.")
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        locked = kernel32.GlobalLock(handle)
        if not locked:
            return None
        try:
            return ctypes.wstring_at(locked)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _keybd_input(vk: int, key_up: bool) -> _INPUT:
    """One `INPUT` struct of type `INPUT_KEYBOARD` for `vk`.

    Built from the module-level `_INPUT`/`_KEYBDINPUT` classes -- the same
    class object on every call, which is what lets `_real_send_ctrl_v` pack
    four of these into one `ctypes.Array` (see `_KEYBDINPUT`'s docstring for
    the bug that shipped when each call minted its own class instead).
    """
    entry = _INPUT()
    entry.type = INPUT_KEYBOARD
    entry.ki = _KEYBDINPUT(
        wVk=vk,
        wScan=0,
        dwFlags=KEYEVENTF_KEYUP if key_up else 0,
        time=0,
        dwExtraInfo=0,
    )
    return entry


def _real_send_ctrl_v() -> None:
    """`SendInput` for Ctrl-down, V-down, V-up, Ctrl-up, in that order.

    Whatever `SendInput` returns, this raises only when it reports fewer
    events accepted than were sent -- that is the one signal Win32 documents
    at all. It says nothing about whether the target window received them:
    UIPI drops synthetic input aimed at a higher-integrity process without
    telling the sender, which is exactly why task 9.3 forbids trusting this
    call's success to mean the paste arrived.
    """
    user32 = _user32()

    inputs = (
        _keybd_input(VK_CONTROL, key_up=False),
        _keybd_input(VK_V, key_up=False),
        _keybd_input(VK_V, key_up=True),
        _keybd_input(VK_CONTROL, key_up=True),
    )
    array = (_INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(_INPUT))
    if sent != len(inputs):
        raise OSError(
            f"SendInput accepted {sent} of {len(inputs)} events; "
            f"GetLastError={wintypes.DWORD(ctypes.GetLastError()).value}"
        )


class WindowsClipboardPaster:
    """Mirrors `ClipboardPaster`'s public surface -- `injection`, `copy`,
    `copy_and_paste` -- over the Win32 clipboard and `SendInput` instead of
    `xclip`/`wl-copy` and a subprocess injector.

    `copy_and_paste` applies task 9.3's rule exactly as `ClipboardPaster` does
    (see that class's docstring and `docs/agent-notes/clipboard-consumption-signal.md`):
    the previous clipboard contents are read *only* when the chosen method
    confirms delivery, which for `send-input` is never, so the read never
    happens and the transcript this call just wrote is what a person finds on
    the clipboard afterwards, whether or not the paste actually reached the
    focused window.
    """

    def __init__(
        self,
        restore_clipboard: bool = True,
        restore_delay_ms: int = 200,
        write_clipboard: Callable[[str], None] = _real_write_clipboard,
        read_clipboard: Callable[[], str | None] = _real_read_clipboard,
        send_ctrl_v: Callable[[], None] = _real_send_ctrl_v,
        sleep: Callable[[float], None] = time.sleep,
        select_injection: Callable[..., PasteInjection] = select_paste_injection,
    ) -> None:
        self._write_clipboard = write_clipboard
        self._read_clipboard = read_clipboard
        self._send_ctrl_v = send_ctrl_v
        self._sleep = sleep
        self._restore_clipboard = restore_clipboard
        self._restore_delay_ms = restore_delay_ms
        self._select_injection = select_injection
        self._failed_methods: set[str] = set()
        # `select_paste_injection` with no `env`/`profile` resolves against
        # this process's own environment -- correct here because this class
        # is only ever constructed once the caller already knows it is on
        # Windows (`integrations.create_clipboard_paster`'s dispatch).
        self._injection = self._select_injection()

    @property
    def injection(self) -> PasteInjection:
        return self._injection

    def copy(self, text: str) -> None:
        self._write_clipboard(text)

    def copy_and_paste(self, text: str) -> DeliveryOutcome:
        injection = self._injection
        if not injection.available:
            self.copy(text)
            return DeliveryOutcome(False, injection.reason)
        # Not read at all when the method cannot confirm delivery (task 9.3):
        # restoring over a transcript that may never have arrived would
        # destroy the only copy of it. `send-input` never confirms, so this
        # branch is always skipped today -- kept as a condition, not dropped,
        # so a future confirming method does not have to re-derive the rule.
        restoring = self._restore_clipboard and injection.confirms_delivery
        previous = self._read_clipboard() if restoring else None
        self.copy(text)
        try:
            self._send_ctrl_v()
        except OSError as error:
            self._demote(injection.method)
            return DeliveryOutcome(False, f"{injection.method} failed to inject the paste: {error}")
        if previous is not None:
            self._sleep(self._restore_delay_ms / 1000)
            self.copy(previous)
        return DeliveryOutcome(True)

    def _demote(self, method: str | None) -> None:
        if method is None:
            return
        logger.warning("Paste injector %s failed; it will not be used again this session.", method)
        self._failed_methods.add(method)
        self._injection = self._select_injection(excluded=frozenset(self._failed_methods))
