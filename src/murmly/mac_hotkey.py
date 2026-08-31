"""macOS's in-process hotkey backend: Carbon `RegisterEventHotKey`, on a
dedicated event-loop thread inside the daemon (task 13.5).

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

Unlike `win_hotkey.py`'s manually pumped `GetMessageW` loop, the pump here
(`_real_run_loop`) is a poll: `ReceiveNextEvent` for up to `_LOOP_POLL_SECONDS`
at a time, dispatched to `GetEventDispatcherTarget` when one arrives --
including `kEventClassKeyboard`/`kEventHotKeyPressed`, to the installed
handler's callback, synchronously, on the calling thread -- and re-checking a
`threading.Event` `request_stop()` sets when nothing arrives. Not a single
blocking `RunApplicationEventLoop()` call, interrupted from any thread by
`QuitApplicationEventLoop()` as Apple documents that function: an earlier
version of this module worked exactly that way, and a macOS CI run showed it
not unwinding within `stop()`'s own bounded join, followed by a same-process
collision on the very next registration of the same key -- the loop still
running, so the key still held. The poll cannot fail that way: every
iteration bounds the delay between `request_stop()` and the loop returning to
`_LOOP_POLL_SECONDS`, regardless of which thread calls it from.

Two things this module cannot answer from Linux, and does not pretend to:

1. **Whether `RegisterEventHotKey`/`ReceiveNextEvent` need the calling thread
   to be the process's main thread.** Every public reference to this API
   assumes a Carbon or Cocoa application's main thread; nothing in Apple's
   documentation says a background thread cannot host it, and running the
   loop off the daemon's main thread (mirroring `win_hotkey.py`'s own
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
import functools
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

#: How long `stop()` waits for the event-loop thread to unwind after being
#: asked to, releasing every key it holds and removing its event handler on
#: its own way out.
STOP_TIMEOUT_SECONDS = 5.0

#: How long each `_real_run_loop` poll of `ReceiveNextEvent` blocks before
#: giving up and checking whether `request_stop()` has been called -- see
#: that function's own docstring. Bounds the delay between `request_stop()`
#: and the loop actually returning to at most this many seconds, regardless
#: of which thread calls it from.
_LOOP_POLL_SECONDS = 0.25

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

#: `CarbonEvents.h`'s `eventHotKeyExistsErr`. The one `RegisterEventHotKey`
#: status that means what task 13.5 calls a collision -- another application
#: already holds this combination. Every other failure means the registration
#: did not happen for some other reason, which is a different thing to tell
#: someone.
_EVENT_HOT_KEY_EXISTS_ERR = -9878


class HotkeyRegistrationRefused(HotkeyError):
    """`RegisterEventHotKey` failed for a reason that is not a collision.

    Carries the raw `OSStatus`, because the number is the only thing that
    distinguishes "this Mac cannot register hotkeys at all" -- a CI runner or
    an SSH session with no window server for Carbon to reach -- from a real
    fault, and neither is "another application holds this key".
    """

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(
            f"RegisterEventHotKey refused the registration with OSStatus {status}. "
            "This is not a collision: no other application holds the key. Carbon "
            "needs a window server session, which a headless login does not have."
        )

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
    # `ReceiveNextEvent`/`SendEventToEventTarget`/`GetEventDispatcherTarget`/
    # `ReleaseEvent`: the four calls `_real_run_loop` polls with, in place of
    # one blocking `RunApplicationEventLoop()` call interrupted by
    # `QuitApplicationEventLoop()` -- see that function's own docstring for
    # why. `inList` (`ReceiveNextEvent`'s second argument) is always passed
    # `None` here (every event class, `inNumTypes=0`), but the pointer type is
    # still declared so a future caller that does pass a list is checked.
    "ReceiveNextEvent": (
        _OS_STATUS,
        (
            ctypes.c_uint32,
            ctypes.POINTER(_EventTypeSpec),
            ctypes.c_double,
            ctypes.c_ubyte,
            ctypes.POINTER(ctypes.c_void_p),
        ),
    ),
    "SendEventToEventTarget": (_OS_STATUS, (ctypes.c_void_p, ctypes.c_void_p)),
    "GetEventDispatcherTarget": (ctypes.c_void_p, ()),
    "ReleaseEvent": (None, (ctypes.c_void_p,)),
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
    for `noErr`).

    Returns `None` for `eventHotKeyExistsErr` alone, which is task 13.5's
    collision signal: the platform's own refusal of a key another application
    holds, arrived at without querying an owner first.

    Every other non-`noErr` status raises, carrying the number. Collapsing
    them all into `None` said "another application holds this key" for
    failures that mean nothing of the sort -- the macOS CI runner refuses
    every registration, having no window server session for Carbon to reach,
    and reported it as a collision with an application that does not exist.
    A person on a Mac reading that would look for the wrong thing.
    """
    hitoolbox = _hitoolbox()
    ref = ctypes.c_void_p()
    hotkey_id = _EventHotKeyID(signature=_HOTKEY_SIGNATURE, id=id_)
    status = hitoolbox.RegisterEventHotKey(
        key_code, modifiers, hotkey_id, hitoolbox.GetApplicationEventTarget(), 0, ctypes.byref(ref)
    )
    if status == _EVENT_HOT_KEY_EXISTS_ERR:
        return None
    if status != 0:
        raise HotkeyRegistrationRefused(status)
    if not ref.value:
        return None
    return ref.value


def _real_unregister(ref: int) -> None:
    """`UnregisterEventHotKey(ref)`.

    Raises on a non-`noErr` status rather than discarding it the way this
    function used to: a discarded failure here is indistinguishable from a
    successful release everywhere that matters -- `_run`'s own `finally`
    reports a purpose as still held only when `_unregister` raises, so a
    silently-ignored status would report every purpose released regardless of
    whether Carbon actually did. This is exactly the third explanation this
    module's own docstring credits for the same symptom a stuck event loop
    produces: a same-process collision on the very next registration of the
    same key.
    """
    hitoolbox = _hitoolbox()
    status = hitoolbox.UnregisterEventHotKey(ctypes.c_void_p(ref))
    if status != 0:
        raise HotkeyError(f"UnregisterEventHotKey failed with OSStatus {status}.")


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


def _real_run_loop(loop_stop: threading.Event) -> None:
    """Poll `ReceiveNextEvent` for up to `_LOOP_POLL_SECONDS` at a time,
    dispatching whatever it returns to `GetEventDispatcherTarget` -- the
    documented way to build a custom Carbon event loop by hand, and the same
    target a real `RunApplicationEventLoop` dispatches every event through,
    including the installed handler's own `kEventHotKeyPressed` -- until
    `loop_stop` is set.

    Not a single blocking `RunApplicationEventLoop()` call, interrupted by
    `QuitApplicationEventLoop()`: this module's own docstring (point 1)
    already flagged running that pair from a background thread as unconfirmed
    on a real Mac, and a macOS CI run showed exactly that doubt landing --
    `stop()`'s `thread.join()` timed out, and the very next
    `RegisterEventHotKey` for the same key collided with this same process,
    which only a loop still running (and so a key still registered) explains.
    A bounded poll cannot fail that way: every iteration either dispatches
    one event or gives up after one poll interval and re-checks `loop_stop`,
    so asking it to stop is honoured within `_LOOP_POLL_SECONDS` regardless
    of which thread `request_stop()` is called from.
    """
    hitoolbox = _hitoolbox()
    dispatcher = hitoolbox.GetEventDispatcherTarget()
    event = ctypes.c_void_p()
    while not loop_stop.is_set():
        status = hitoolbox.ReceiveNextEvent(0, None, _LOOP_POLL_SECONDS, 1, ctypes.byref(event))
        if status != 0 or not event.value:
            # Almost always `eventLoopTimedOutErr` -- nothing arrived within
            # this poll -- but nothing below needs to tell that apart from any
            # other failure to hand back an event: either way there is
            # nothing to dispatch this iteration.
            continue
        hitoolbox.SendEventToEventTarget(event, dispatcher)
        hitoolbox.ReleaseEvent(event)


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
        run_loop: Callable[[], None] | None = None,
        request_stop: Callable[[], None] | None = None,
        ready_timeout: float = READY_TIMEOUT_SECONDS,
        stop_timeout: float = STOP_TIMEOUT_SECONDS,
    ) -> None:
        self._on_hotkey = on_hotkey
        self._register = register
        self._unregister = unregister
        self._install_handler = install_handler
        self._remove_handler = remove_handler
        # `run_loop`/`request_stop` default to `None`, not `_real_run_loop`/
        # a bare function, because the real pair needs a `threading.Event`
        # shared between them and reset before every thread this registrar
        # starts (`_start_locked` below) -- a plain default parameter cannot
        # carry that reset. Every test fake supplies both explicitly, with
        # its own cooperating pair (`FakeCarbon`'s own `_stop_event`,
        # recreated by its `install_handler` each run), so `_loop_stop` stays
        # `None` and untouched whenever a fake is in use.
        self._loop_stop: threading.Event | None = None
        if run_loop is None and request_stop is None:
            loop_stop = threading.Event()
            self._loop_stop = loop_stop
            run_loop = functools.partial(_real_run_loop, loop_stop)
            request_stop = loop_stop.set
        elif run_loop is None or request_stop is None:
            raise TypeError("run_loop and request_stop must be supplied together, or not at all.")
        self._run_loop = run_loop
        self._request_stop = request_stop
        self._ready_timeout = ready_timeout
        self._stop_timeout = stop_timeout
        # Guards every field below and serializes `rebind()`/`stop()` against
        # each other, exactly as `WindowsHotkeyRegistrar._operation_lock` does.
        self._operation_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._ready_exception: HotkeyError | None = None
        self._held: dict[str, int] = {}
        self._by_id: dict[int, str] = {}

    def held_purposes(self) -> frozenset[str]:
        """Which purposes are registered right now, for a `status` response.

        Empty once a `stop()` has *confirmed* every registration released,
        matching `WindowsHotkeyRegistrar.held_purposes`'s own contract exactly
        (task 13.6, the `desktop-integration` spec's "daemon holding the
        hotkey stops" scenario applied to this platform). If the event-loop
        thread has not confirmed that -- still running past `stop()`'s own
        timeout, or its own `UnregisterEventHotKey` call failing -- the
        purposes it may still hold stay reported here, deliberately: this is
        what a `doctor`/`status` response actually holding the record is more
        useful for than a report of "released" that has not been confirmed.
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
            if self._thread is not None:
                # `_stop_locked` could not confirm the previous thread
                # released its registrations (see its own docstring). Layering
                # a new thread on top would either collide with keys this
                # same process may still hold -- reported as "claimed by
                # another application", which would be a lie -- or, if the
                # old thread does finish a moment later, leave its own
                # `_held`/`_by_id` write racing the new thread's. Neither is
                # acceptable, so the rebind itself fails instead.
                raise HotkeyError(
                    "Could not confirm the previous hotkey registrations were "
                    "released; refusing to register a new set while they may "
                    "still be held."
                )
            if not encoded:
                return
            self._start_locked(encoded)

    def stop(self) -> None:
        """Stop the event-loop thread, releasing every hotkey it holds.

        Safe to call with no thread running, and never raises -- the same
        rules `WindowsHotkeyRegistrar.stop` documents for the same reasons.
        Not guaranteed to finish releasing every hotkey by the time it
        returns: see `_stop_locked`.
        """
        with self._operation_lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        """Ask the event-loop thread to stop and wait up to `_stop_timeout`
        for it to.

        If the thread has not finished by then, this returns anyway --
        `stop()` must complete in bounded time, since a daemon that cannot
        shut down is worse than one whose hotkeys briefly outlive it -- but
        `_thread`, `_held` and `_by_id` are all left exactly as they were:
        still pointing at the (possibly still running) thread and its
        (possibly still held) registrations. Clearing them here would report
        a release that has not been confirmed, which is the defect this
        method exists to not have. `_run`'s own `finally` is the only thing
        that ever writes `_held`/`_by_id` to reflect a thread that has
        actually finished -- by the time `thread.join()` returns `True`
        below, that write has already happened, so nothing here needs to
        repeat it.

        Leaving `_thread` set on an unconfirmed stop also means the next
        `stop()` or `rebind()` gets another bounded chance to join the same
        thread and pick up whatever it left behind, rather than losing track
        of it.
        """
        thread = self._thread
        if thread is None:
            return
        # Wait for the thread's first pass to finish (registration, handler
        # installation, and the loop starting or a refusal) before asking it
        # to stop -- a `request_stop()` landing in the true race window
        # between `_ready.set()` and the loop actually starting could leave
        # the loop that starts a moment later with nothing left to tell it to
        # stop, if `_run_loop` had no way to notice.
        self._ready.wait(self._ready_timeout)
        try:
            self._request_stop()
        except Exception:  # noqa: BLE001 - stop must still join
            logger.warning("Could not signal the hotkey event loop to stop.", exc_info=True)
        thread.join(self._stop_timeout)
        if thread.is_alive():
            logger.warning(
                "The hotkey event-loop thread did not stop within %.3g seconds; "
                "its registrations could not be confirmed released.",
                self._stop_timeout,
            )
            return
        self._thread = None

    def _start_locked(self, encoded: dict[str, object]) -> None:
        self._ready.clear()
        self._ready_exception = None
        if self._loop_stop is not None:
            self._loop_stop.clear()
        thread = threading.Thread(
            target=self._run, args=(encoded,), name="murmly-macos-hotkey-loop", daemon=True
        )
        self._thread = thread
        thread.start()
        if not self._ready.wait(self._ready_timeout):
            # The thread itself is not touched here -- it may yet finish
            # starting and go on to register keys `held_purposes()` needs to
            # keep reporting, and a later `stop()`/`rebind()` needs a live
            # reference to be able to join. Losing that reference (by setting
            # `self._thread = None`) would be the same "reported a release
            # that never happened" defect `_stop_locked` exists to avoid,
            # applied to a thread that never even confirmed starting.
            raise HotkeyError(
                "The hotkey registration thread did not start within "
                f"{self._ready_timeout:g} seconds."
            )
        if self._ready_exception is not None:
            raise self._ready_exception

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

        `self._held`/`self._by_id` are this thread's own responsibility for
        its entire life, written twice: once after registration (what this
        thread holds while its loop runs) and once more in `finally` (what it
        *still* holds on the way out, which is empty unless `_unregister`
        itself failed for some purpose -- in which case that purpose stays
        reported as held, honestly, rather than as released because this
        thread merely stopped trying). `_stop_locked` never has to repeat
        either write: by the time `thread.join()` sees this thread has
        finished, `finally` has already run.
        """
        registered: dict[int, int] = {}
        held: dict[str, int] = {}
        by_id: dict[int, str] = {}
        exception: HotkeyError | None = None
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
                exception = HotkeyError(
                    f"{hotkey.portable} ({purpose}) is already claimed by another "
                    "application."
                )
                break

            self._held = held
            self._by_id = by_id
            self._ready_exception = exception
            self._ready.set()

            if exception is not None:
                return

            self._run_loop()
        except HotkeyError as install_error:
            # Preserves the exception's own type -- `HotkeyRegistrationRefused`
            # included -- rather than collapsing it to a string here and
            # rebuilding a plain `HotkeyError` in `_start_locked`, which used
            # to erase exactly the distinction task 13.5 introduced that type
            # for: a caller catching `HotkeyRegistrationRefused` specifically
            # (this runtime test's own real-Mac skip, among others) never saw
            # it, because every refusal surfaced as the same generic type.
            self._ready_exception = install_error
            self._ready.set()
        finally:
            unreleased: dict[str, int] = {}
            for index, ref in registered.items():
                try:
                    self._unregister(ref)
                except Exception:  # noqa: BLE001 - every other claim still needs releasing
                    logger.warning("Could not release hotkey ref %r.", ref, exc_info=True)
                    purpose = by_id.get(index)
                    if purpose is not None:
                        unreleased[purpose] = index
            self._held = unreleased
            self._by_id = {index: purpose for purpose, index in unreleased.items()}
            if handler is not None:
                try:
                    self._remove_handler(handler)
                except Exception:  # noqa: BLE001 - shutdown must not raise
                    logger.warning("Could not remove the hotkey event handler.", exc_info=True)
