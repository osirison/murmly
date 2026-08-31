"""macOS clipboard and paste injection: `NSPasteboard` and `CGEventPost`,
never a `pbcopy` subprocess (task 14.1, 14.2).

`pbcopy`/`pbpaste` would work, but everything else in this change goes
straight to the native API rather than shelling out to a CLI wrapper around
it -- `win_clipboard.py` does the same for the same reason: one less process
to spawn per transcript, and one less place a `$PATH` surprise could bite.

The paste itself goes through `CGEventPost` of Cmd+V, and task 14.3 registers
it under the exact rule `win_clipboard.py`'s own docstring states for
`SendInput`: a method that returns success whether or not the keystroke
reached the focused window must never be trusted to overwrite the transcript
it is supposed to have delivered. Without the Accessibility grant,
`CGEventPost` does not fail and raises no dialog on its own (design.md's
"Clipboard and paste injection") -- the event is silently dropped -- so
`MacosClipboardPaster` never restores the previous clipboard contents,
exactly as `WindowsClipboardPaster` never does for `SendInput`.

Every native call in this module goes through raw `ctypes`, never PyObjC:
`platform.py`'s `_real_macos_microphone_authorization_status` already
established that pattern for this codebase (`objc_msgSend`, cast fresh per
call rather than assigned to the shared cached function object, exactly as
that function's own docstring explains), and design.md scopes macOS support
to Apple Silicon only -- arm64 has no `objc_msgSend_stret`/`objc_msgSend_fpret`
split the way the old x86_64 ABI did, so every message send in this module,
returning a pointer, a `BOOL`, or nothing, goes through plain `objc_msgSend`
with a per-call cast. Nothing here needs the struct- or float-returning
variants a future Intel port would.

Structured like `win_clipboard.py`: every native call lives behind a small
first-class seam (`write_clipboard`, `read_clipboard`, `send_cmd_v`,
`is_process_trusted`), each defaulted to a real implementation that only
loads a framework from inside its own function body, so this module stays
importable on Linux. Only the seams and `MacosClipboardPaster`'s policy
around them are exercised by the test suite; the real Objective-C and Carbon
calls -- `NSPasteboard` actually holding what was written, `CGEventPost`
actually reaching a window -- can only be confirmed on macOS.
"""

from __future__ import annotations

import ctypes
import ctypes.util
from collections.abc import Callable
import logging
import time

from murmly.integrations import DeliveryOutcome, PasteInjection, select_paste_injection


logger = logging.getLogger(__name__)

#: `integrations.select_paste_injection`'s method name for this backend --
#: read by `platform_diagnostics` and by `MacosClipboardPaster.injection`,
#: never re-derived from `PasteInjection.method`'s string elsewhere, matching
#: `win_clipboard.SEND_INPUT_METHOD`'s own role.
CGEVENT_POST_METHOD = "cgevent-post"

#: Frameworks are bundles, not flat `.dylib`s on the loader path, exactly as
#: `platform.py`'s `_AVFOUNDATION_FRAMEWORK_PATH` docstring explains --
#: `ctypes.util.find_library` cannot find any of the three below, so each is
#: loaded by its full path instead. `ApplicationServices` is the umbrella that
#: re-exports both HIServices (`AX*`) and Quartz's Core Graphics event
#: services (`CGEvent*`) at the top level -- the same umbrella-re-export
#: mechanism widely relied on by ctypes-based macOS automation tools that
#: predate this change, not something this module invented.
_APPKIT_FRAMEWORK_PATH = "/System/Library/Frameworks/AppKit.framework/AppKit"
_COREFOUNDATION_FRAMEWORK_PATH = (
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)
_APPLICATION_SERVICES_FRAMEWORK_PATH = (
    "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
)

#: `HIToolbox/Events.h`'s `kVK_ANSI_V` and Carbon's `EventModifiers` command
#: bit, reused here rather than re-declared: the paste chord is Cmd+V, the
#: same physical key `hotkey.py`'s `_MACOS_LETTER_DIGIT_KEYS["V"]` names.
_KVK_ANSI_V = 0x09
_CG_EVENT_FLAG_MASK_COMMAND = 1 << 20

#: `CoreGraphics/CGEventTypes.h`'s `kCGHIDEventTap` -- inject as if from the
#: hardware, the same event-source point of entry `CGEventPost` is documented
#: to expect for synthetic input.
_K_CG_HID_EVENT_TAP = 0


#: Every `ApplicationServices`/`CoreFoundation` entry point this module calls
#: with a plain C calling convention (as opposed to the Objective-C ones,
#: which go through `objc_msgSend` and are declared per-call below), with the
#: `restype`/`argtypes` ctypes needs so it stops defaulting an undeclared
#: function's `restype` to `c_int` -- 32 bits. `CGEventCreateKeyboardEvent`
#: and `CFDictionaryCreate` both return a pointer-sized `CFTypeRef`-family
#: value; an undeclared `restype` truncates either on 64-bit Darwin, the same
#: defect class `win_clipboard.py`'s `_KERNEL32_SIGNATURES` docstring
#: describes for `GlobalAlloc`. `test_mac_ctypes_signatures.py` scans this
#: module's source for every attribute call made on the `applicationservices`/
#: `corefoundation` handles below and asserts each callee has an entry here.
_APPLICATION_SERVICES_SIGNATURES: dict[str, tuple[object, tuple[object, ...]]] = {
    "AXIsProcessTrusted": (ctypes.c_bool, ()),
    "AXIsProcessTrustedWithOptions": (ctypes.c_bool, (ctypes.c_void_p,)),
    "CGEventCreateKeyboardEvent": (
        ctypes.c_void_p,
        (ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool),
    ),
    "CGEventSetFlags": (None, (ctypes.c_void_p, ctypes.c_uint64)),
    "CGEventPost": (None, (ctypes.c_uint32, ctypes.c_void_p)),
}

_COREFOUNDATION_SIGNATURES: dict[str, tuple[object, tuple[object, ...]]] = {
    "CFRelease": (None, (ctypes.c_void_p,)),
    "CFDictionaryCreate": (
        ctypes.c_void_p,
        (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ),
    ),
}

#: Lazily-loaded, module-private library handles -- never ctypes' own shared,
#: process-wide loader cache, following `win_clipboard.py`'s precedent.
_appkit_dll: ctypes.CDLL | None = None
_corefoundation_dll: ctypes.CDLL | None = None
_applicationservices_dll: ctypes.CDLL | None = None
_libobjc_dll: ctypes.CDLL | None = None


def _configure(dll: ctypes.CDLL, signatures: dict[str, tuple[object, tuple[object, ...]]]) -> None:
    for name, (restype, argtypes) in signatures.items():
        function = getattr(dll, name)
        function.restype = restype
        function.argtypes = argtypes


def _appkit() -> ctypes.CDLL:
    global _appkit_dll
    if _appkit_dll is None:
        _appkit_dll = ctypes.CDLL(_APPKIT_FRAMEWORK_PATH)
    return _appkit_dll


def _corefoundation() -> ctypes.CDLL:
    global _corefoundation_dll
    if _corefoundation_dll is None:
        _corefoundation_dll = ctypes.CDLL(_COREFOUNDATION_FRAMEWORK_PATH)
        _configure(_corefoundation_dll, _COREFOUNDATION_SIGNATURES)
    return _corefoundation_dll


def _applicationservices() -> ctypes.CDLL:
    global _applicationservices_dll
    if _applicationservices_dll is None:
        _applicationservices_dll = ctypes.CDLL(_APPLICATION_SERVICES_FRAMEWORK_PATH)
        _configure(_applicationservices_dll, _APPLICATION_SERVICES_SIGNATURES)
    return _applicationservices_dll


def _libobjc() -> ctypes.CDLL:
    global _libobjc_dll
    if _libobjc_dll is None:
        path = ctypes.util.find_library("objc")
        if path is None:
            raise OSError("libobjc is required for NSPasteboard access.")
        _libobjc_dll = ctypes.CDLL(path)
        _libobjc_dll.objc_getClass.restype = ctypes.c_void_p
        _libobjc_dll.objc_getClass.argtypes = [ctypes.c_char_p]
        _libobjc_dll.sel_registerName.restype = ctypes.c_void_p
        _libobjc_dll.sel_registerName.argtypes = [ctypes.c_char_p]
    return _libobjc_dll


def _send(restype: object, argtypes: tuple[object, ...], receiver: int, selector: int, *args: object) -> object:
    """One `objc_msgSend` call, cast fresh to `restype`/`argtypes` for this
    call alone -- never assigned to the shared `libobjc.objc_msgSend`
    attribute, matching `platform.py`'s `_real_macos_microphone_authorization_
    status` docstring on exactly why: that attribute is one cached function
    object, and assigning a signature to it would clobber any other call
    through the same object elsewhere in the process, which is load-bearing
    on arm64 rather than merely tidy."""
    libobjc = _libobjc()
    function = ctypes.cast(
        libobjc.objc_msgSend, ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)
    )
    return function(receiver, selector, *args)


def _general_pasteboard() -> int:
    libobjc = _libobjc()
    _appkit()  # Loads AppKit, which registers NSPasteboard with the runtime.
    pasteboard_class = libobjc.objc_getClass(b"NSPasteboard")
    selector = libobjc.sel_registerName(b"generalPasteboard")
    return _send(ctypes.c_void_p, (), pasteboard_class, selector)


def _ns_string(text: str) -> int:
    libobjc = _libobjc()
    string_class = libobjc.objc_getClass(b"NSString")
    selector = libobjc.sel_registerName(b"stringWithUTF8String:")
    return _send(ctypes.c_void_p, (ctypes.c_char_p,), string_class, selector, text.encode("utf-8"))


def _pasteboard_type_string() -> int:
    """`AppKit`'s `NSPasteboardTypeString` constant -- an `NSString *` exported
    as a plain symbol, read the same way `platform.py` reads `AVMediaTypeAudio`
    (`ctypes.c_void_p.in_dll`) rather than built from a hardcoded UTI string,
    so nothing here depends on remembering `"public.utf8-plain-text"` correctly."""
    return ctypes.c_void_p.in_dll(_appkit(), "NSPasteboardTypeString").value


def _real_write_clipboard(text: str) -> None:
    """`[[NSPasteboard generalPasteboard] clearContents]` then
    `setString:forType:NSPasteboardTypeString]` (task 14.1)."""
    pasteboard = _general_pasteboard()
    if not pasteboard:
        raise OSError("NSPasteboard generalPasteboard returned nil.")
    libobjc = _libobjc()
    clear_selector = libobjc.sel_registerName(b"clearContents")
    _send(ctypes.c_long, (), pasteboard, clear_selector)

    set_selector = libobjc.sel_registerName(b"setString:forType:")
    ns_string = _ns_string(text)
    ok = _send(
        ctypes.c_bool,
        (ctypes.c_void_p, ctypes.c_void_p),
        pasteboard,
        set_selector,
        ns_string,
        _pasteboard_type_string(),
    )
    if not ok:
        raise OSError("NSPasteboard setString:forType: failed.")


def _real_read_clipboard() -> str | None:
    """`[[NSPasteboard generalPasteboard] stringForType:NSPasteboardTypeString]`,
    decoded through `UTF8String` (task 14.1)."""
    pasteboard = _general_pasteboard()
    if not pasteboard:
        raise OSError("NSPasteboard generalPasteboard returned nil.")
    libobjc = _libobjc()
    get_selector = libobjc.sel_registerName(b"stringForType:")
    ns_result = _send(
        ctypes.c_void_p, (ctypes.c_void_p,), pasteboard, get_selector, _pasteboard_type_string()
    )
    if not ns_result:
        return None
    utf8_selector = libobjc.sel_registerName(b"UTF8String")
    char_pointer = _send(ctypes.c_char_p, (), ns_result, utf8_selector)
    if char_pointer is None:
        return None
    return char_pointer.decode("utf-8", "replace")


def _real_send_cmd_v() -> None:
    """`CGEventPost` for Cmd-V-down then Cmd-V-up (task 14.2).

    Whatever `CGEventPost` returns -- nothing; it is declared `void` -- this
    can only detect a failure to *create* the event (a null `CGEventRef` from
    `CGEventCreateKeyboardEvent`, which Apple's documentation attributes to
    an invalid event source or keyboard layout, not to a missing permission).
    It says nothing about whether the target window received the chord: task
    14.3 forbids trusting this call's success to mean the paste arrived,
    because without the Accessibility grant the event is silently dropped
    with no error and no dialog (design.md's "Clipboard and paste injection").
    """
    applicationservices = _applicationservices()

    def _key_event(key_down: bool) -> int:
        event = applicationservices.CGEventCreateKeyboardEvent(None, _KVK_ANSI_V, key_down)
        if not event:
            raise OSError("CGEventCreateKeyboardEvent returned NULL.")
        applicationservices.CGEventSetFlags(event, _CG_EVENT_FLAG_MASK_COMMAND)
        return event

    down = _key_event(True)
    try:
        applicationservices.CGEventPost(_K_CG_HID_EVENT_TAP, down)
    finally:
        _corefoundation().CFRelease(down)

    up = _key_event(False)
    try:
        applicationservices.CGEventPost(_K_CG_HID_EVENT_TAP, up)
    finally:
        _corefoundation().CFRelease(up)


def _real_is_process_trusted() -> bool:
    """`AXIsProcessTrusted()` (task 14.4): reports the current Accessibility
    grant without prompting, ever. Read by `platform.py`'s permission-registry
    entry for diagnostics and status; never called from the daemon (task 14.5's
    other half -- see `request_accessibility_permission` below for the one
    call site that is allowed to prompt)."""
    return bool(_applicationservices().AXIsProcessTrusted())


def request_accessibility_permission(
    is_trusted: Callable[[], bool] = _real_is_process_trusted,
) -> bool:
    """Task 14.5: raise the Accessibility consent dialog, only when not
    already trusted, through `AXIsProcessTrustedWithOptions(prompt: true)`.

    Called from exactly one place in the whole codebase --
    `installer.Installer._request_macos_accessibility_permission`, itself
    reached only from `install()`, on every path (the desktop-launcher body
    and `_install_in_process`), never from `doctor` and never from the daemon:
    design.md is explicit that "a permission dialog raised by a background
    process the person did not just invoke is one they cannot connect to
    anything." `AXIsProcessTrusted()` is checked first so an already-trusted
    process never raises the dialog it would otherwise be entitled to skip.

    The dictionary `AXIsProcessTrustedWithOptions` takes is built directly
    from three CoreFoundation constant symbols
    (`kAXTrustedCheckOptionPrompt`, `kCFBooleanTrue`,
    `kCFTypeDictionaryKeyCallBacks`/`kCFTypeDictionaryValueCallBacks`), read
    the same way every other Apple-exported constant in this change is:
    `ctypes.c_void_p.in_dll` for the two pointer-valued symbols, and
    `ctypes.addressof(ctypes.c_byte.in_dll(...))` for the two callback-table
    symbols, which are themselves struct *values* at that address rather than
    pointers stored there -- `c_void_p.in_dll` would misread the first eight
    bytes of the struct as if it were a pointer if used for these instead.

    Every failure here -- a missing symbol, a call that raises -- is caught
    and logged rather than propagated: a failed *request* must never be
    treated as a denial the way a failed *check* is elsewhere in this
    codebase, because this function's only job is to raise a dialog, and
    there is nothing to report back from the attempt itself.
    """
    try:
        if is_trusted():
            return True
        corefoundation = _corefoundation()
        applicationservices = _applicationservices()
        prompt_key = ctypes.c_void_p.in_dll(applicationservices, "kAXTrustedCheckOptionPrompt").value
        true_value = ctypes.c_void_p.in_dll(corefoundation, "kCFBooleanTrue").value
        key_callbacks = ctypes.addressof(ctypes.c_byte.in_dll(corefoundation, "kCFTypeDictionaryKeyCallBacks"))
        value_callbacks = ctypes.addressof(
            ctypes.c_byte.in_dll(corefoundation, "kCFTypeDictionaryValueCallBacks")
        )
        keys = (ctypes.c_void_p * 1)(prompt_key)
        values = (ctypes.c_void_p * 1)(true_value)
        options = corefoundation.CFDictionaryCreate(
            None, keys, values, 1, ctypes.c_void_p(key_callbacks), ctypes.c_void_p(value_callbacks)
        )
        if not options:
            return False
        try:
            return bool(applicationservices.AXIsProcessTrustedWithOptions(options))
        finally:
            corefoundation.CFRelease(options)
    except (OSError, ValueError, AttributeError) as error:
        logger.debug("Could not request the macOS Accessibility permission: %s", error)
        return False


class MacosClipboardPaster:
    """Mirrors `ClipboardPaster`'s public surface -- `injection`, `copy`,
    `copy_and_paste` -- over `NSPasteboard` and `CGEventPost` instead of
    `xclip`/`wl-copy` and a subprocess injector, the same role
    `WindowsClipboardPaster` plays for Win32.

    `copy_and_paste` applies task 14.3's rule exactly as `WindowsClipboardPaster`
    does: the previous clipboard contents are read *only* when the chosen
    method confirms delivery, which for `cgevent-post` is never, so the read
    never happens and the transcript this call just wrote is what a person
    finds on the clipboard afterwards, whether or not the paste actually
    reached the focused window.
    """

    def __init__(
        self,
        restore_clipboard: bool = True,
        restore_delay_ms: int = 200,
        write_clipboard: Callable[[str], None] = _real_write_clipboard,
        read_clipboard: Callable[[], str | None] = _real_read_clipboard,
        send_cmd_v: Callable[[], None] = _real_send_cmd_v,
        sleep: Callable[[float], None] = time.sleep,
        select_injection: Callable[..., PasteInjection] = select_paste_injection,
    ) -> None:
        self._write_clipboard = write_clipboard
        self._read_clipboard = read_clipboard
        self._send_cmd_v = send_cmd_v
        self._sleep = sleep
        self._restore_clipboard = restore_clipboard
        self._restore_delay_ms = restore_delay_ms
        self._select_injection = select_injection
        self._failed_methods: set[str] = set()
        # `select_paste_injection` with no `env`/`profile` resolves against
        # this process's own environment -- correct here because this class
        # is only ever constructed once the caller already knows it is on
        # macOS (`integrations.create_clipboard_paster`'s dispatch).
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
        # Not read at all when the method cannot confirm delivery (task 14.3):
        # restoring over a transcript that may never have arrived would
        # destroy the only copy of it. `cgevent-post` never confirms, so this
        # branch is always skipped today -- kept as a condition, not dropped,
        # matching `WindowsClipboardPaster.copy_and_paste`'s own reasoning.
        restoring = self._restore_clipboard and injection.confirms_delivery
        previous = self._read_clipboard() if restoring else None
        self.copy(text)
        try:
            self._send_cmd_v()
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
