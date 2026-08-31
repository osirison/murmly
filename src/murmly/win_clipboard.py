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

As with `win_pipe.py` and `win_hotkey.py`, every `ctypes.windll` call lives
behind a small first-class seam (`write_clipboard`, `read_clipboard`,
`send_ctrl_v`), each defaulted to a real Win32 implementation imported from
inside its own function body so this module stays importable on Linux. Only
the seams and `WindowsClipboardPaster`'s policy around them (never reading the
clipboard before an unconfirmable paste, never restoring over one) are
exercised by the test suite; the real Win32 calls -- `GlobalAlloc` actually
allocating movable memory, `SendInput` actually reaching a window -- can only
be confirmed on Windows.
"""

from __future__ import annotations

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


def _real_write_clipboard(text: str) -> None:
    """`OpenClipboard` / `EmptyClipboard` / `SetClipboardData(CF_UNICODETEXT)`.

    The buffer is UTF-16LE with a terminating null, allocated `GMEM_MOVEABLE`
    because `SetClipboardData` requires a movable, unlocked handle. Ownership
    of that handle passes to the system the moment `SetClipboardData` succeeds
    -- freeing it afterwards would free memory the clipboard still holds, so
    it is freed only on the failure path, where the system never took it.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32

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
    import ctypes

    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32

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


def _keybd_input(vk: int, key_up: bool):
    """One `INPUT` struct of type `INPUT_KEYBOARD` for `vk`."""
    import ctypes
    from ctypes import wintypes

    # `ULONG_PTR`: pointer-sized, which `wintypes.WPARAM` already is on every
    # architecture ctypes targets -- the same substitution `win_hotkey.py`'s
    # struct-free calls do not need but the `SendInput` array does, since
    # `KEYBDINPUT.dwExtraInfo` is defined as `ULONG_PTR` in `winuser.h`.
    ulong_ptr = wintypes.WPARAM

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = (
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ulong_ptr),
        )

    class _InputUnion(ctypes.Union):
        _fields_ = (("ki", KEYBDINPUT),)

    class INPUT(ctypes.Structure):
        _anonymous_ = ("_input",)
        _fields_ = (("type", wintypes.DWORD), ("_input", _InputUnion))

    entry = INPUT()
    entry.type = INPUT_KEYBOARD
    entry.ki = KEYBDINPUT(
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
    import ctypes
    from ctypes import wintypes

    inputs = (
        _keybd_input(VK_CONTROL, key_up=False),
        _keybd_input(VK_V, key_up=False),
        _keybd_input(VK_V, key_up=True),
        _keybd_input(VK_CONTROL, key_up=True),
    )
    array_type = type(inputs[0]) * len(inputs)
    array = array_type(*inputs)
    sent = ctypes.windll.user32.SendInput(
        len(inputs), array, ctypes.sizeof(type(inputs[0]))
    )
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
