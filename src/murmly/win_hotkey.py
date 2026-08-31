"""Windows' in-process hotkey backend: `RegisterHotKey` on a message-loop
thread inside the daemon.

Unlike KDE's launcher file and GNOME's `custom-keybindings` entry, Windows has
no desktop-held state for a global hotkey: `RegisterHotKey` binds to the
calling thread and the binding exists only for as long as that thread runs a
message loop. That is what `design.md`'s "Four hotkey backends" section means
by "Windows and macOS register in Murmly's own process" -- the daemon itself
is the only thing that ever holds this binding, so it has to create it at
startup and again on every live rebind (`hotkey_record.py`'s
`rebind_from_record`, which this module's `WindowsHotkeyRegistrar.rebind`
satisfies the contract of), and release it when it stops (task 8.5).

Collisions are detected the way task 8.4 requires: `RegisterHotKey` itself
refuses a key another application already holds, and that refusal *is* the
collision -- nothing here queries an owner first, because Windows offers no
such query and the platform's own refusal is a stronger signal anyway (a
query-then-register window is exactly the race a query can never close).

As with `win_pipe.py`, every Win32 call lives behind a small first-class
seam (`register`, `unregister`, `pump`, `post_quit`, `current_thread_id`),
each defaulted to a real implementation that only touches `ctypes.WinDLL`
from inside its own function body (via the module-private `_user32()`/
`_kernel32()` below) so this module stays importable on Linux.
`WindowsHotkeyRegistrar`'s policy -- rebuilding the whole binding set on one
fresh thread per `rebind()`, refusing on the platform's own signal, unwinding
a partial batch, releasing on `stop()` -- is exercised by the test suite
against fakes for all five seams; the message loop's real behaviour
(`GetMessageW` actually blocking, `RegisterHotKey` actually claiming a key)
can only be confirmed on Windows.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from collections.abc import Callable
from dataclasses import dataclass
import logging
import threading

from murmly.hotkey import HotkeyError, WINDOWS_MOD_NOREPEAT, windows_hotkey_for_portable


logger = logging.getLogger(__name__)

#: How long `rebind()` waits for the new thread to finish its initial
#: registration pass before giving up and reporting the rebind itself as
#: failed, distinctly from a registration the thread reported as refused.
READY_TIMEOUT_SECONDS = 5.0

#: How long `stop()` waits for the message-loop thread to unwind after
#: `WM_QUIT` is posted, unregistering every key it holds on its own way out.
STOP_TIMEOUT_SECONDS = 5.0

#: `winuser.h`'s `WM_QUIT` and `WM_HOTKEY`.
_WM_QUIT = 0x0012
_WM_HOTKEY = 0x0312

#: Every `user32`/`kernel32` entry point this module calls, with the
#: `restype`/`argtypes` ctypes needs so it stops defaulting an undeclared
#: function's `restype` to `c_int` -- 32 bits, the defect class
#: `win_clipboard.py`'s `_KERNEL32_SIGNATURES` docstring explains in full.
#: None of these particular calls return a pointer-sized value that an
#: undeclared 32-bit default would silently truncate, but they are declared
#: anyway so a future call added to either seam is checked, not assumed
#: safe by the same reasoning. `test_win_ctypes_signatures.py` scans this
#: module's source for every attribute call made on the `user32`/`kernel32`
#: handles below and asserts each callee has an entry here.
_USER32_SIGNATURES: dict[str, tuple[object, tuple[object, ...]]] = {
    "RegisterHotKey": (
        wintypes.BOOL,
        (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT),
    ),
    "UnregisterHotKey": (wintypes.BOOL, (wintypes.HWND, ctypes.c_int)),
    "GetMessageW": (
        wintypes.BOOL,
        (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT),
    ),
    "TranslateMessage": (wintypes.BOOL, (ctypes.POINTER(wintypes.MSG),)),
    # `DispatchMessageW` actually returns `LRESULT` (`LONG_PTR`, pointer-sized)
    # -- unused here, but `c_ssize_t` is the correct width to declare it at
    # rather than let it default to a truncating `c_int`.
    "DispatchMessageW": (ctypes.c_ssize_t, (ctypes.POINTER(wintypes.MSG),)),
    "PostThreadMessageW": (
        wintypes.BOOL,
        (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM),
    ),
}

_KERNEL32_SIGNATURES: dict[str, tuple[object, tuple[object, ...]]] = {
    "GetCurrentThreadId": (wintypes.DWORD, ()),
}

#: Lazily-loaded, module-private library handles -- never ctypes' own
#: shared, process-wide loader cache, following `win_clipboard.py`'s
#: precedent so declaring a signature here can never change behaviour for
#: some other, unrelated caller of the same DLL (this module's own
#: runtime-integration test builds its own `SendInput` call against that
#: shared cache directly, and must not see these `argtypes`).
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


@dataclass(frozen=True, slots=True)
class _PumpQuit:
    """`GetMessageW` returned the loop's own exit signal, or an error it
    cannot recover from -- both are treated as "stop", never as "retry"."""


@dataclass(frozen=True, slots=True)
class _PumpHotkey:
    id: int


@dataclass(frozen=True, slots=True)
class _PumpOther:
    """A message arrived that is neither `WM_QUIT` nor `WM_HOTKEY`.

    Dispatched like any ordinary message and the loop continues -- most
    threads that only ever register hotkeys will never see one, but nothing
    here assumes that.
    """


PumpResult = _PumpQuit | _PumpHotkey | _PumpOther


def _real_register(id_: int, modifiers: int, vk: int) -> bool:
    """`RegisterHotKey(None, id_, modifiers, vk)`.

    `hwnd=None` registers the hotkey against the *calling thread* rather than
    a window -- there is no window here, by design: a message-only thread is
    all a global hotkey needs, and it is what keeps `WM_HOTKEY` delivered to
    this thread's own queue rather than requiring one more Win32 object
    (`CreateWindowEx`) to own.
    """
    user32 = _user32()
    return bool(user32.RegisterHotKey(None, id_, modifiers, vk))


def _real_unregister(id_: int) -> None:
    user32 = _user32()
    user32.UnregisterHotKey(None, id_)


def _real_pump() -> PumpResult:
    """Block on this thread's message queue for the next message.

    A real `GetMessageW`, as design.md's "message-loop thread ... pumping
    `GetMessageW`" specifies -- blocking, not polled, which is why `stop()`
    wakes it with `PostThreadMessageW(WM_QUIT)` rather than a flag this loop
    would otherwise have no way to notice between hotkey presses that might
    be minutes apart.
    """
    user32 = _user32()
    msg = wintypes.MSG()
    result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
    if result == 0 or result == -1:
        # 0: WM_QUIT. -1: GetMessageW itself failed -- both end the loop; a
        # thread that cannot ask for its next message has nothing left to wait
        # on, and holding a hotkey with no loop pumping it would leave WM_HOTKEY
        # undelivered forever.
        return _PumpQuit()
    if msg.message == _WM_HOTKEY:
        return _PumpHotkey(id=int(msg.wParam))
    user32.TranslateMessage(ctypes.byref(msg))
    user32.DispatchMessageW(ctypes.byref(msg))
    return _PumpOther()


def _real_post_quit(thread_id: int) -> None:
    user32 = _user32()
    user32.PostThreadMessageW(thread_id, _WM_QUIT, 0, 0)


def _real_current_thread_id() -> int:
    kernel32 = _kernel32()
    return int(kernel32.GetCurrentThreadId())


class WindowsHotkeyRegistrar:
    """Owns the message-loop thread that holds every hotkey Murmly registers.

    `rebind(bindings)` is the whole public surface `hotkey_record.py` needs:
    it satisfies the ``rebind(bindings: dict[str, str]) -> None`` contract
    that module's docstring describes for a future in-process backend, taking
    exactly what `HotkeyRecordStore.read()` returns. Every call replaces the
    *entire* registration rather than adding to it, because `RegisterHotKey`
    and `UnregisterHotKey` both require the calling thread to be the one that
    made the original registration -- there is no way to add one more key to
    an already-running loop's thread from here, so instead the whole set is
    rebuilt on one fresh thread every time: the daemon's own startup, and
    every live install that reaches a running daemon over
    `COMMAND_REBIND_HOTKEYS` (task 5.5).
    """

    def __init__(
        self,
        on_hotkey: Callable[[str], None],
        register: Callable[[int, int, int], bool] = _real_register,
        unregister: Callable[[int], None] = _real_unregister,
        pump: Callable[[], PumpResult] = _real_pump,
        post_quit: Callable[[int], None] = _real_post_quit,
        current_thread_id: Callable[[], int] = _real_current_thread_id,
        ready_timeout: float = READY_TIMEOUT_SECONDS,
        stop_timeout: float = STOP_TIMEOUT_SECONDS,
    ) -> None:
        self._on_hotkey = on_hotkey
        self._register = register
        self._unregister = unregister
        self._pump = pump
        self._post_quit = post_quit
        self._current_thread_id = current_thread_id
        self._ready_timeout = ready_timeout
        self._stop_timeout = stop_timeout
        # Guards every field below, and serializes `rebind()`/`stop()` against
        # each other -- a `stop()` racing a `rebind()`'s own internal
        # `self.stop()` call must never post to a thread the other call is in
        # the middle of starting.
        self._operation_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self._ready_error: str | None = None
        self._held: dict[str, int] = {}
        self._by_id: dict[int, str] = {}

    def held_purposes(self) -> frozenset[str]:
        """Which purposes are registered right now, for a `status` response.

        Empty whenever no thread is running -- including the whole window
        between `stop()` being called and a later `rebind()` completing --
        which is exactly the "not currently held" state task 8.7 and the
        `desktop-integration` spec's "daemon holding the hotkey stops"
        scenario require.
        """
        with self._operation_lock:
            return frozenset(self._held)

    def rebind(self, bindings: dict[str, str]) -> None:
        """Replace the full set of registered hotkeys with `bindings`.

        Encodes every value with `hotkey.windows_hotkey_for_portable` before
        touching the running thread at all, so a record entry Windows cannot
        encode (a key with no Windows virtual-key code, `Hyper`) is refused
        with nothing torn down first. Raises `HotkeyError` -- never any other
        exception -- naming the purpose and the reason, for both an encoding
        failure and a collision the platform itself refused; the daemon's own
        `_rebind_hotkeys` already treats any exception from this call as a
        reportable, non-fatal rebind failure.
        """
        encoded: dict[str, object] = {}
        for purpose, portable in sorted(bindings.items()):
            try:
                encoded[purpose] = windows_hotkey_for_portable(portable)
            except HotkeyError as error:
                raise HotkeyError(
                    f"Could not bind {purpose!r} ({portable!r}): {error}"
                ) from error

        with self._operation_lock:
            self._stop_locked()
            if not encoded:
                return
            self._start_locked(encoded)

    def stop(self) -> None:
        """Stop the message-loop thread, releasing every hotkey it holds.

        Safe to call with no thread running: `rebind`'s own first step is
        `stop`, and a daemon that never registered a hotkey still calls this
        during shutdown. Never raises -- a hotkey release must not be the
        reason shutdown reports a failure.
        """
        with self._operation_lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        thread = self._thread
        if thread is None:
            return
        # `PostThreadMessageW` fails with `ERROR_INVALID_THREAD_ID` until the
        # target thread has made its own first user32 call -- `_run` always
        # has one (`RegisterHotKey`) before it sets `_ready`, so waiting here
        # is what keeps a `stop()` that lands moments after `rebind()` starts
        # the thread from posting to a queue that does not exist yet.
        self._ready.wait(self._ready_timeout)
        if self._thread_id is not None:
            try:
                self._post_quit(self._thread_id)
            except Exception:  # noqa: BLE001 - stop must still join and clear state
                logger.warning("Could not signal the hotkey thread to stop.", exc_info=True)
        thread.join(self._stop_timeout)
        self._thread = None
        self._thread_id = None
        self._held = {}
        self._by_id = {}

    def _start_locked(self, encoded: dict[str, object]) -> None:
        self._ready.clear()
        self._ready_error = None
        thread = threading.Thread(
            target=self._run, args=(encoded,), name="murmly-hotkey-loop", daemon=True
        )
        self._thread = thread
        thread.start()
        if not self._ready.wait(self._ready_timeout):
            self._thread = None
            raise HotkeyError(
                "The hotkey registration thread did not start within "
                f"{self._ready_timeout:g} seconds."
            )
        if self._ready_error is not None:
            self._thread = None
            self._thread_id = None
            raise HotkeyError(self._ready_error)

    def _run(self, encoded: dict[str, object]) -> None:
        """The message-loop thread's whole body: register, pump, unregister.

        Registration happens here, on this thread, and nowhere else --
        `RegisterHotKey` and `UnregisterHotKey` both bind to the calling
        thread, so every claim this thread makes must also be the one that
        releases it, in the `finally` below, whichever way the loop ends.
        """
        thread_id = self._current_thread_id()
        registered: list[int] = []
        held: dict[str, int] = {}
        by_id: dict[int, str] = {}
        error: str | None = None
        try:
            for index, (purpose, hotkey) in enumerate(encoded.items(), start=1):
                if self._register(index, hotkey.modifiers | WINDOWS_MOD_NOREPEAT, hotkey.vk):
                    registered.append(index)
                    held[purpose] = index
                    by_id[index] = purpose
                    continue
                # Task 8.4: the platform's own refusal is the collision. Every
                # id this batch already claimed is released before reporting
                # it, so a failure on the second purpose never leaves the
                # first bound -- installer.py's own "a collision on the
                # second must not leave the first bound" rule, applied here.
                for claimed_id in registered:
                    self._unregister(claimed_id)
                registered = []
                held = {}
                by_id = {}
                error = f"{hotkey.portable} ({purpose}) is already claimed by another application."
                break

            self._thread_id = thread_id
            self._held = held
            self._by_id = by_id
            self._ready_error = error
            self._ready.set()

            if error is not None:
                return

            while True:
                result = self._pump()
                if isinstance(result, _PumpQuit):
                    return
                if isinstance(result, _PumpHotkey):
                    purpose = by_id.get(result.id)
                    if purpose is None:
                        continue
                    try:
                        self._on_hotkey(purpose)
                    except Exception:  # noqa: BLE001 - the loop must survive a bad handler
                        logger.warning("Hotkey handler for %r failed.", purpose, exc_info=True)
        finally:
            for claimed_id in registered:
                try:
                    self._unregister(claimed_id)
                except Exception:  # noqa: BLE001 - every other claim still needs releasing
                    logger.warning("Could not release hotkey id %s.", claimed_id, exc_info=True)
