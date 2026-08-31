"""macOS's in-process hotkey backend: Carbon `RegisterEventHotKey`, on an
`RunApplicationEventLoop` thread inside the daemon (task 13.5).

Carbon rather than a `CGEventTap` specifically to stay permission-free
(design.md's "Four hotkey backends"): a default-mode tap needs Accessibility
and a listen-only tap needs Input Monitoring, while `RegisterEventHotKey`
needs neither, because the process only ever learns that one registered
combination fired and never sees any other input. Carbon Event Manager is
deprecated and still functional, and is what Electron, VS Code and Slack use
for exactly this. The cost is real and is reported in diagnostics rather than
hidden (task 13.7): `RegisterEventHotKey` does not fire when the frontmost
application consumes the combination itself, and it cannot express a
modifier-only chord (there is no key code for "no key", only for a modifier
combined with one).

Like `win_hotkey.py`'s `RegisterHotKey`, Carbon has no desktop-held state for a
global hotkey -- the binding exists only while this thread's event loop is
alive to have registered it, so the daemon creates it at startup, rebuilds it
whole on every live rebind (`hotkey_record.py`'s `rebind_from_record`, which
`MacosHotkeyRegistrar.rebind` satisfies the same contract
`WindowsHotkeyRegistrar.rebind` does), and releases it when the daemon stops
(task 13.6).

Collisions are detected the same way task 8.4 established for Windows and
task 13.5 asks for here: `RegisterEventHotKey`'s own refusal *is* the
collision signal, queried by trying the registration rather than by asking an
owner first, because there is no such query on this platform either and a
query-then-register window would be exactly the race a query can never close.

Unlike `win_hotkey.py`'s manually pumped `GetMessageW` loop, Carbon's own
`RunApplicationEventLoop` already *is* the pump -- it is a single blocking
call that dispatches every event class an installed handler is registered
for, including `kEventClassKeyboard`/`kEventHotKeyPressed`, to that handler's
callback synchronously, on the calling thread. `QuitApplicationEventLoop` is
documented as callable from any thread (unlike `win_hotkey.py`'s
`PostThreadMessageW`, which needs the specific target thread id), which is
what lets `stop()` interrupt a loop running on the dedicated thread this
class owns without tracking that thread's id at all.

Two things this module cannot answer from Linux, and does not pretend to:

1. **Whether `RegisterEventHotKey`/`RunApplicationEventLoop` need the calling
   thread to be the process's main thread.** Every public reference to this
   API assumes a Carbon or Cocoa application's main thread; nothing in
   Apple's documentation says a background thread cannot host it, and running
   the loop off the daemon's main thread (mirroring `win_hotkey.py`'s own
   dedicated-thread design, which Win32 permits without qualification) is
   what every architectural symmetry in this codebase points at. The runtime
   integration test below is what answers this for real, on the macOS CI
   runner -- see its own docstring for exactly what it can and cannot prove
   from there.
2. **Whether the calling process needs a connection to the window server at
   all**, i.e. whether this works from a `launchd` agent with no logged-in
   GUI session as cleanly as it works from a Terminal-launched process. A
   `LaunchAgent` (as opposed to a `LaunchDaemon`) always runs inside a logged-in
   user's GUI session (`gui/$UID`, task 13.3/13.4's own domain target), which
   is believed sufficient, but "believed" is doing real work in that sentence
   until a real run confirms it.

As with `win_hotkey.py`, every Carbon call lives behind a small first-class
seam (`register`, `unregister`, `install_handler`, `remove_handler`,
`run_loop`, `request_stop`), each defaulted to a real implementation that
only touches `ctypes.CDLL` from inside its own function body (via the
module-private `_hitoolbox()` below) so this module stays importable on
Linux. `MacosHotkeyRegistrar`'s policy -- rebuilding the whole binding set on
one fresh thread per `rebind()`, refusing on the platform's own signal,
unwinding a partial batch, releasing on `stop()` -- is exercised by the test
suite against fakes for all six seams, mirroring `WindowsHotkeyRegistrar`
test-for-test; the loop's real behaviour (`RegisterEventHotKey` actually
claiming a key, the handler actually firing) can only be confirmed on macOS.
"""

from __future__ import annotations

import ctypes
from collections.abc import Callable
from dataclasses import dataclass
import logging
import threading

from murmly.hotkey import HotkeyError, macos_hotkey_for_portable


logger = logging.getLogger(__name__)

#: How long `rebind()` waits for the new thread to finish its initial
#: registration pass before giving up and reporting the rebind itself as
#: failed, distinctly from a registration the thread reported as refused --
#: the same distinction, and the same default, `win_hotkey.py`'s
#: `READY_TIMEOUT_SECONDS` draws.
READY_TIMEOUT_SECONDS = 5.0

#: How long `stop()` waits for the event-loop thread to unwind after
#: `QuitApplicationEventLoop` is called, releasing every key it holds and
#: removing its event handler on its own way out.
STOP_TIMEOUT_SECONDS = 5.0

#: `HIToolbox/Events.h`'s `kEventClassKeyboard` (`FOUR_CHAR_CODE('keyb')`) and
#: `kEventHotKeyPressed`. Not confirmed on a real Mac -- the same caveat every
#: transcribed Apple constant in this change carries (see `hotkey.py`'s
#: `_MACOS_LETTER_DIGIT_KEYS` and `platform.py`'s `_AV_AUTHORIZATION_STATUS_*`).
_K_EVENT_CLASS_KEYBOARD = 0x6B657962
_K_EVENT_HOTKEY_PRESSED = 5

#: `kEventParamDirectObject` (`FOUR_CHAR_CODE('----')`) and
#: `typeEventHotKeyID` (`FOUR_CHAR_CODE('hkid')`) -- the parameter name and
#: type `GetEventParameter` needs to read which registered hotkey fired out
#: of a `kEventHotKeyPressed` event.
_K_EVENT_PARAM_DIRECT_OBJECT = 0x2D2D2D2D
_TYPE_EVENT_HOTKEY_ID = 0x686B6964

#: `RegisterEventHotKey`'s `EventHotKeyID.signature` -- an arbitrary
#: four-character code naming Murmly as the owner of every id it registers,
#: the same role a window class name plays on Windows. Not read back by
#: anything; only `id` (this module's own per-purpose registration index)
#: distinguishes one binding from another.
_HOTKEY_SIGNATURE = 0x6D726D6C  # FOUR_CHAR_CODE('mrml')

_OS_STATUS = ctypes.c_int32
_EVENT_HANDLER_UPP_TYPE = ctypes.CFUNCTYPE(
    _OS_STATUS, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
)


class _EventHotKeyID(ctypes.Structure):
    """`Events.h`'s `EventHotKeyID`: an owner signature and a per-registration id."""

    _fields_ = (("signature", ctypes.c_uint32), ("id", ctypes.c_uint32))


class _EventTypeSpec(ctypes.Structure):
    """`Events.h`'s `EventTypeSpec`: one (class, kind) pair an event handler
    is installed for."""

    _fields_ = (("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32))


#: Every HIToolbox entry point this module calls, with the `restype`/
#: `argtypes` ctypes needs so it stops defaulting an undeclared function's
#: `restype` to `c_int` -- 32 bits, the defect class `win_clipboard.py`'s
#: `_KERNEL32_SIGNATURES` docstring explains in full and which applies here to
#: every ref-returning call (`GetApplicationEventTarget`,
#: `RegisterEventHotKey`'s `outRef`), all of them pointer-sized opaque
#: references on 64-bit Darwin. `test_mac_ctypes_signatures.py` scans this
#: module's source for every attribute call made on the `hitoolbox` handle
#: below and asserts each callee has an entry here.
_HITOOLBOX_SIGNATURES: dict[str, tuple[object, tuple[object, ...]]] = {
    # `InstallEventHandler`, not `InstallApplicationEventHandler`. The latter
    # reads like an entry point and is not one: `CarbonEvents.h` defines it as
    # a macro expanding to
    # `InstallEventHandler(GetApplicationEventTarget(), ...)`, so no such
    # symbol is exported and `dlsym` cannot find it. Asking for it raised
    # `AttributeError: dlsym(..., InstallApplicationEventHandler): symbol not
    # found` on the macOS runner -- inside the event-loop thread, where it
    # failed no test and left the hotkey registered but permanently unable to
    # fire, since nothing was listening for the event it raises.
    #
    # `inNumTypes` is an `ItemCount`, which `MacTypes.h` makes `unsigned long`
    # -- 64 bits on every macOS Murmly supports, not the 32 this declared.
    "InstallEventHandler": (
        _OS_STATUS,
        (
            ctypes.c_void_p,
            _EVENT_HANDLER_UPP_TYPE,
            ctypes.c_ulong,
            ctypes.POINTER(_EventTypeSpec),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ),
    ),
    "RemoveEventHandler": (_OS_STATUS, (ctypes.c_void_p,)),
    "RegisterEventHotKey": (
        _OS_STATUS,
        (
            ctypes.c_uint32,
            ctypes.c_uint32,
            _EventHotKeyID,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ),
    ),
    "UnregisterEventHotKey": (_OS_STATUS, (ctypes.c_void_p,)),
    "GetEventParameter": (
        _OS_STATUS,
        (
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ),
    ),
    "GetApplicationEventTarget": (ctypes.c_void_p, ()),
    "RunApplicationEventLoop": (None, ()),
    "QuitApplicationEventLoop": (None, ()),
}

#: Frameworks are bundles, not flat `.dylib`s on the loader path --
#: `ctypes.util.find_library` cannot find one, exactly as `platform.py`'s
#: `_AVFOUNDATION_FRAMEWORK_PATH` docstring explains for `AVFoundation`. Loaded
#: by its full path for the same reason.
_HITOOLBOX_FRAMEWORK_PATH = "/System/Library/Frameworks/Carbon.framework/Versions/A/Frameworks/HIToolbox.framework/HIToolbox"

#: Lazily-loaded, module-private library handle -- never a shared, process-wide
#: cache, following `win_hotkey.py`'s precedent so declaring a signature here
#: can never change behaviour for some other, unrelated caller of the same
#: library.
_hitoolbox_dll: ctypes.CDLL | None = None


def _configure(dll: ctypes.CDLL, signatures: dict[str, tuple[object, tuple[object, ...]]]) -> None:
    for name, (restype, argtypes) in signatures.items():
        function = getattr(dll, name)
        function.restype = restype
        function.argtypes = argtypes


def _hitoolbox() -> ctypes.CDLL:
    global _hitoolbox_dll
    if _hitoolbox_dll is None:
        _hitoolbox_dll = ctypes.CDLL(_HITOOLBOX_FRAMEWORK_PATH)
        _configure(_hitoolbox_dll, _HITOOLBOX_SIGNATURES)
    return _hitoolbox_dll


def _real_register(id_: int, modifiers: int, key_code: int) -> int | None:
    """`RegisterEventHotKey(key_code, modifiers, {signature, id_}, GetApplicationEventTarget(), 0, &ref)`.

    Registered against the application event target rather than any
    particular window -- there is no window here, by design, matching
    `win_hotkey.py`'s `_real_register` registering against the calling thread
    rather than an `HWND`. Returns the opaque `EventHotKeyRef` as a plain
    `int` (never `None` on success -- HIToolbox never gives back a null ref
    for `noErr`), or `None` on any non-`noErr` status, which is task 13.5's
    collision signal: nothing here queries an owner first.
    """
    hitoolbox = _hitoolbox()
    ref = ctypes.c_void_p()
    hotkey_id = _EventHotKeyID(signature=_HOTKEY_SIGNATURE, id=id_)
    status = hitoolbox.RegisterEventHotKey(
        key_code, modifiers, hotkey_id, hitoolbox.GetApplicationEventTarget(), 0, ctypes.byref(ref)
    )
    if status != 0 or not ref.value:
        return None
    return ref.value


def _real_unregister(ref: int) -> None:
    hitoolbox = _hitoolbox()
    hitoolbox.UnregisterEventHotKey(ctypes.c_void_p(ref))


def _real_install_handler(on_fired: Callable[[int], None]) -> tuple[object, object]:
    """Install the callback HIToolbox invokes for every `kEventHotKeyPressed`.

    Returns `(handler_ref, upp)` -- the second element exists only so the
    caller keeps the ctypes trampoline (`upp`) referenced for as long as
    HIToolbox might call it: like `focus.py`'s `_IGNORE_X_ERROR`, a
    `CFUNCTYPE` instance with no surviving Python reference is free to be
    garbage-collected while a C library still holds a pointer to it, which
    would call into freed memory the moment a hotkey fires.
    """
    hitoolbox = _hitoolbox()

    def _callback(_next_handler: object, event: object, _user_data: object) -> int:
        hotkey_id = _EventHotKeyID()
        status = hitoolbox.GetEventParameter(
            event,
            _K_EVENT_PARAM_DIRECT_OBJECT,
            _TYPE_EVENT_HOTKEY_ID,
            None,
            ctypes.sizeof(_EventHotKeyID),
            None,
            ctypes.cast(ctypes.byref(hotkey_id), ctypes.c_void_p),
        )
        if status == 0:
            try:
                on_fired(hotkey_id.id)
            except Exception:  # noqa: BLE001 - the loop must survive a bad handler
                logger.warning("Hotkey handler raised.", exc_info=True)
        return 0  # noErr -- let the event continue past this handler.

    upp = _EVENT_HANDLER_UPP_TYPE(_callback)
    event_types = (_EventTypeSpec * 1)(
        _EventTypeSpec(eventClass=_K_EVENT_CLASS_KEYBOARD, eventKind=_K_EVENT_HOTKEY_PRESSED)
    )
    handler_ref = ctypes.c_void_p()
    # The application event target, passed explicitly: this is what the
    # `InstallApplicationEventHandler` macro exists to supply, and it is the
    # same target `_real_register` registers the hotkey against, which is what
    # makes the handler the one that receives it.
    status = hitoolbox.InstallEventHandler(
        hitoolbox.GetApplicationEventTarget(),
        upp,
        1,
        event_types,
        None,
        ctypes.byref(handler_ref),
    )
    if status != 0:
        raise HotkeyError(f"InstallEventHandler failed with OSStatus {status}.")
    return handler_ref.value, upp


def _real_remove_handler(handler: object) -> None:
    handler_ref, _upp = handler
    hitoolbox = _hitoolbox()
    hitoolbox.RemoveEventHandler(ctypes.c_void_p(handler_ref))


def _real_run_loop() -> None:
    """Block, dispatching events (including fired hotkeys) until
    `QuitApplicationEventLoop` is called -- from any thread, per Apple's own
    documentation for that function, which is what lets `stop()` interrupt
    this without knowing which thread is running it."""
    _hitoolbox().RunApplicationEventLoop()


def _real_request_stop() -> None:
    _hitoolbox().QuitApplicationEventLoop()


class MacosHotkeyRegistrar:
    """Owns the event-loop thread that holds every hotkey Murmly registers on macOS.

    `rebind(bindings)` is `WindowsHotkeyRegistrar.rebind`'s exact contract,
    satisfying the same `hotkey_record.py` interface: `rebind(bindings: dict[str, str])
    -> None`, taking exactly what `HotkeyRecordStore.read()` returns. Every
    call replaces the *entire* registration rather than adding to one already
    running, for the same structural reason Windows' does: rebuilding the
    whole set on one fresh thread every time is simpler and safer than trying
    to add one more key to a loop already in flight, even though Carbon (unlike
    `RegisterHotKey`/`UnregisterHotKey`) does not strictly require the same
    thread that registered a key to be the one that releases it.
    """

    def __init__(
        self,
        on_hotkey: Callable[[str], None],
        register: Callable[[int, int, int], int | None] = _real_register,
        unregister: Callable[[int], None] = _real_unregister,
        install_handler: Callable[[Callable[[int], None]], object] = _real_install_handler,
        remove_handler: Callable[[object], None] = _real_remove_handler,
        run_loop: Callable[[], None] = _real_run_loop,
        request_stop: Callable[[], None] = _real_request_stop,
        ready_timeout: float = READY_TIMEOUT_SECONDS,
        stop_timeout: float = STOP_TIMEOUT_SECONDS,
    ) -> None:
        self._on_hotkey = on_hotkey
        self._register = register
        self._unregister = unregister
        self._install_handler = install_handler
        self._remove_handler = remove_handler
        self._run_loop = run_loop
        self._request_stop = request_stop
        self._ready_timeout = ready_timeout
        self._stop_timeout = stop_timeout
        # Guards every field below and serializes `rebind()`/`stop()` against
        # each other, exactly as `WindowsHotkeyRegistrar._operation_lock` does.
        self._operation_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._ready_error: str | None = None
        self._held: dict[str, int] = {}
        self._by_id: dict[int, str] = {}

    def held_purposes(self) -> frozenset[str]:
        """Which purposes are registered right now, for a `status` response.

        Empty whenever no thread is running, matching
        `WindowsHotkeyRegistrar.held_purposes`'s own contract exactly (task
        13.6, the `desktop-integration` spec's "daemon holding the hotkey
        stops" scenario applied to this platform).
        """
        with self._operation_lock:
            return frozenset(self._held)

    def rebind(self, bindings: dict[str, str]) -> None:
        """Replace the full set of registered hotkeys with `bindings`.

        Encodes every value with `hotkey.macos_hotkey_for_portable` before
        touching the running thread at all, so a record entry macOS cannot
        encode (a key with no Carbon virtual-key code, or `Hyper`) is refused
        with nothing torn down first -- `WindowsHotkeyRegistrar.rebind`'s own
        rule, applied to this platform's encoder.
        """
        encoded: dict[str, object] = {}
        for purpose, portable in sorted(bindings.items()):
            try:
                encoded[purpose] = macos_hotkey_for_portable(portable)
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
        """Stop the event-loop thread, releasing every hotkey it holds.

        Safe to call with no thread running, and never raises -- the same
        rules `WindowsHotkeyRegistrar.stop` documents for the same reasons.
        """
        with self._operation_lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        thread = self._thread
        if thread is None:
            return
        # Wait for the thread's first pass to finish (registration, handler
        # installation, and either the loop starting or a refusal) before
        # asking it to quit -- `QuitApplicationEventLoop` needs a loop already
        # running to interrupt; calling it any earlier is a no-op that this
        # thread's own `finally` would still unwind correctly (a refused
        # registration never calls `run_loop()` at all), but calling it in
        # the true race window between `_ready.set()` and `run_loop()`
        # actually starting could leave the loop that starts a moment later
        # with nothing left to stop it. Not exercised on a real Mac; see this
        # module's own docstring.
        self._ready.wait(self._ready_timeout)
        try:
            self._request_stop()
        except Exception:  # noqa: BLE001 - stop must still join and clear state
            logger.warning("Could not signal the hotkey event loop to stop.", exc_info=True)
        thread.join(self._stop_timeout)
        self._thread = None
        self._held = {}
        self._by_id = {}

    def _start_locked(self, encoded: dict[str, object]) -> None:
        self._ready.clear()
        self._ready_error = None
        thread = threading.Thread(
            target=self._run, args=(encoded,), name="murmly-macos-hotkey-loop", daemon=True
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
            raise HotkeyError(self._ready_error)

    def _dispatch(self, numeric_id: int) -> None:
        purpose = self._by_id.get(numeric_id)
        if purpose is None:
            return
        try:
            self._on_hotkey(purpose)
        except Exception:  # noqa: BLE001 - the loop must survive a bad handler
            logger.warning("Hotkey handler for %r failed.", purpose, exc_info=True)

    def _run(self, encoded: dict[str, object]) -> None:
        """The event-loop thread's whole body: install the handler, register,
        run the loop, unregister and remove the handler on the way out.

        Registration happens here, on this thread, mirroring
        `WindowsHotkeyRegistrar._run`'s own placement even though Carbon does
        not document the same same-thread requirement `RegisterHotKey`/
        `UnregisterHotKey` carry -- keeping every Carbon call on the one
        thread that owns this registrar's whole lifecycle is the simpler
        invariant to reason about regardless of whether it turns out to be
        load-bearing.
        """
        registered: dict[int, int] = {}
        held: dict[str, int] = {}
        by_id: dict[int, str] = {}
        error: str | None = None
        handler = None
        try:
            handler = self._install_handler(self._dispatch)
            for index, (purpose, hotkey) in enumerate(encoded.items(), start=1):
                ref = self._register(index, hotkey.modifiers, hotkey.key_code)
                if ref is not None:
                    registered[index] = ref
                    held[purpose] = index
                    by_id[index] = purpose
                    continue
                # Task 13.5: the platform's own refusal is the collision.
                # Every id this batch already claimed is released before
                # reporting it, matching `WindowsHotkeyRegistrar._run`'s own
                # "a collision on the second must not leave the first bound".
                for claimed_ref in registered.values():
                    self._unregister(claimed_ref)
                registered = {}
                held = {}
                by_id = {}
                error = (
                    f"{hotkey.portable} ({purpose}) is already claimed by another "
                    "application, or macOS refused the registration."
                )
                break

            self._held = held
            self._by_id = by_id
            self._ready_error = error
            self._ready.set()

            if error is not None:
                return

            self._run_loop()
        except HotkeyError as install_error:
            self._ready_error = str(install_error)
            self._ready.set()
        finally:
            for ref in registered.values():
                try:
                    self._unregister(ref)
                except Exception:  # noqa: BLE001 - every other claim still needs releasing
                    logger.warning("Could not release hotkey ref %r.", ref, exc_info=True)
            if handler is not None:
                try:
                    self._remove_handler(handler)
                except Exception:  # noqa: BLE001 - shutdown must not raise
                    logger.warning("Could not remove the hotkey event handler.", exc_info=True)
