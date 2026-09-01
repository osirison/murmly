"""Every plain-C Carbon/CoreFoundation/ApplicationServices call in
`mac_hotkey.py`/`mac_clipboard.py` declares `restype`/`argtypes`, and does so
on a module-private `CDLL` rather than any shared, process-wide cache --
extending `test_win_ctypes_signatures.py`'s source-scanning pattern to this
change's macOS modules, which otherwise escape that scan entirely.

`ctypes` defaults an undeclared function's `restype` to `c_int` -- 32 bits.
`RegisterEventHotKey`'s `outRef`, `GetApplicationEventTarget`,
`CGEventCreateKeyboardEvent` and `CFDictionaryCreate` all return a
pointer-sized value on 64-bit Darwin; an undeclared `restype` truncates any
of them before the module ever uses it, the same defect class
`win_clipboard.py`'s `_KERNEL32_SIGNATURES` docstring explains for
`GlobalAlloc`. This module can never call the real functions from Linux
(none of the three frameworks it loads exist here), so instead it proves the
*declarations* are complete and well-typed by reading each module's own
source, following `test_win_ctypes_signatures.py`'s own approach exactly.

The Objective-C half of these modules (`mac_clipboard.py`'s, `mac_focus.py`'s
and `overlay_renderer_qt.py`'s own `_send` helper, wrapping `objc_msgSend`,
plus `platform.py`'s own inline cast for the microphone-authorization call)
is out of the plain-C scan's scope: each declares a fresh `restype`/
`argtypes` pair at every call site through `ctypes.CFUNCTYPE`, rather than
through a shared signature table `_configure` reads, so there is no table to
scan. `ObjcSendCallSitesDeclareARealRestypeTests` and
`PlatformMicrophoneObjcCallTests` below check the weaker, but still
meaningful, invariant that every one of those call sites names a real
`ctypes` type, not a literal this module's own author forgot to give one.
"""

from __future__ import annotations

import ctypes
import re
import unittest
from pathlib import Path

from murmly import daemon, mac_clipboard, mac_focus, mac_hotkey, overlay_renderer_qt
from murmly import platform as murmly_platform


_CALL_PATTERN = re.compile(r"\b(hitoolbox|applicationservices|corefoundation)\.([A-Za-z_]\w*)\(")

#: `ctypes._CFuncPtr` is what `ctypes.CFUNCTYPE(...)` produces -- the type
#: `mac_hotkey.py`'s `InstallApplicationEventHandler` signature declares its
#: `EventHandlerUPP` argument as, exactly the way `focus.py`'s
#: `XSetErrorHandler.argtypes` declares a real callback type rather than a
#: bare `c_void_p` for the same kind of parameter. Absent from
#: `test_win_ctypes_signatures.py`'s own version of this check because none
#: of that scan's four Windows modules declare a callback in a signature
#: table the same way (`win_hotkey.py`'s callback is a Python function run on
#: its own thread, never passed to Win32 as a UPP-style pointer).
_REAL_CTYPES_BASES = (ctypes._SimpleCData, ctypes.Structure, ctypes.Union, ctypes.Array, ctypes._CFuncPtr)


def _is_real_ctypes_type(value: object) -> bool:
    if value is None:
        return True
    return isinstance(value, type) and (
        issubclass(value, _REAL_CTYPES_BASES) or issubclass(value, ctypes._Pointer)
    )


class _ModuleSignatureCoverageMixin:
    module = None
    signature_tables: tuple[dict[str, tuple[object, tuple[object, ...]]], ...] = ()

    def _source(self) -> str:
        return Path(self.module.__file__).read_text(encoding="utf-8")

    def test_every_call_site_has_a_declared_signature(self) -> None:
        declared = set()
        for table in self.signature_tables:
            declared.update(table)

        called = {name for _handle, name in _CALL_PATTERN.findall(self._source())}

        self.assertTrue(called, "the call-site scan itself found nothing -- check the pattern")
        undeclared = called - declared
        self.assertEqual(
            set(),
            undeclared,
            f"{self.module.__name__} calls {sorted(undeclared)} with no declared "
            "restype/argtypes -- ctypes would default restype to a truncating "
            "32-bit c_int for any of these that return a pointer.",
        )

    def test_every_declared_signature_is_well_typed(self) -> None:
        for table in self.signature_tables:
            for name, (restype, argtypes) in table.items():
                with self.subTest(name=name):
                    self.assertTrue(
                        _is_real_ctypes_type(restype),
                        f"{name}'s restype {restype!r} is not a real ctypes type",
                    )
                    self.assertIsInstance(
                        argtypes, tuple, f"{name}'s argtypes must be a tuple, not {argtypes!r}"
                    )
                    for argtype in argtypes:
                        self.assertTrue(
                            _is_real_ctypes_type(argtype),
                            f"{name}'s argtypes includes {argtype!r}, not a real ctypes type",
                        )


class MacHotkeySignatureTests(_ModuleSignatureCoverageMixin, unittest.TestCase):
    module = mac_hotkey
    signature_tables = (mac_hotkey._HITOOLBOX_SIGNATURES,)


class MacClipboardSignatureTests(_ModuleSignatureCoverageMixin, unittest.TestCase):
    module = mac_clipboard
    signature_tables = (
        mac_clipboard._APPLICATION_SERVICES_SIGNATURES,
        mac_clipboard._COREFOUNDATION_SIGNATURES,
    )


class PrivateLibraryHandleTests(unittest.TestCase):
    """None of these modules may configure a shared, process-wide handle --
    each holds its own lazily-loaded `ctypes.CDLL` instance, exactly as
    `win_clipboard.py`/`win_focus.py`/`win_hotkey.py` each hold their own
    `ctypes.WinDLL` rather than reaching for `ctypes.windll`'s shared cache
    (`test_win_ctypes_signatures.py`'s own `PrivateLibraryHandleTests`).
    `overlay_renderer_qt.py` is included here even though its own
    `_libobjc()`/`_send()` pair is a third, deliberate copy of
    `mac_clipboard.py`'s (that module's own docstring explains why it is
    duplicated rather than imported) -- the same private-handle rule applies
    to every copy, not just the first one written."""

    def test_no_module_touches_a_shared_cdll_cache(self) -> None:
        """`ctypes.cdll` (lowercase) is the shared, process-wide cache --
        `ctypes.cdll.LoadLibrary(...)` or attribute access on it -- distinct
        from `ctypes.CDLL(...)`, the constructor every one of these modules
        correctly calls to build its own private instance. Case-sensitive on
        purpose: matching case-insensitively would flag the very pattern this
        test exists to require."""
        for module in (mac_hotkey, mac_clipboard, mac_focus, overlay_renderer_qt):
            with self.subTest(module=module.__name__):
                source = Path(module.__file__).read_text(encoding="utf-8")
                self.assertIsNone(
                    re.search(r"\bctypes\.cdll\b", source),
                    f"{module.__name__} must configure its own ctypes.CDLL instance, "
                    "never ctypes.cdll's shared, process-wide cache",
                )


class ObjcSendCallSitesDeclareARealRestypeTests(unittest.TestCase):
    """`mac_clipboard.py`'s, `mac_focus.py`'s and `overlay_renderer_qt.py`'s
    own `_send` helper is this module's equivalent of a declared signature
    for every `objc_msgSend` call: its first argument is the cast's
    `restype`. This checks that every call site actually gives one, rather
    than a bare literal a future edit might otherwise slip past unnoticed."""

    _SEND_CALL_PATTERN = re.compile(r"_send\(\s*(ctypes\.\w+)")

    def test_every_send_call_names_a_real_ctypes_restype(self) -> None:
        for module in (mac_clipboard, mac_focus, overlay_renderer_qt):
            source = Path(module.__file__).read_text(encoding="utf-8")
            calls = self._SEND_CALL_PATTERN.findall(source)
            with self.subTest(module=module.__name__):
                self.assertTrue(calls, "the call-site scan itself found nothing -- check the pattern")
                for token in calls:
                    restype = getattr(ctypes, token.removeprefix("ctypes."))
                    self.assertTrue(
                        _is_real_ctypes_type(restype),
                        f"{module.__name__} calls _send(restype={token!r}, ...), not a real ctypes type",
                    )


class PlatformMicrophoneObjcCallTests(unittest.TestCase):
    """`platform/__init__.py`'s `_real_macos_microphone_authorization_status`
    (task 12.5) is a fourth Objective-C call site this change adds, outside
    `mac_clipboard.py`/`mac_focus.py`/`mac_hotkey.py` and so otherwise
    invisible to every scan above. It declares `objc_getClass`/
    `sel_registerName`'s `restype`/`argtypes` inline rather than through a
    shared table, and casts `objc_msgSend` fresh for its one call exactly as
    `mac_clipboard._send`/`mac_focus._send` do (its own docstring says so
    explicitly) -- this checks both declarations are present and well-typed
    by reading the module's own source, the same approach
    `ObjcSendCallSitesDeclareARealRestypeTests` takes for those two modules'
    `_send` call sites. `NSInteger` (`AVAuthorizationStatus`'s underlying
    type) is 8 bytes on 64-bit Darwin, so this specifically must not regress
    to the 32-bit-truncating `ctypes.c_int` default -- exactly the Windows
    `restype` defect class this module's docstring names in full.
    """

    _CAST_PATTERN = re.compile(
        r"ctypes\.cast\(\s*libobjc\.objc_msgSend,\s*ctypes\.CFUNCTYPE\(\s*(ctypes\.\w+)"
    )

    def _source(self) -> str:
        return Path(murmly_platform.__file__).read_text(encoding="utf-8")

    def test_the_objc_msgsend_cast_names_a_real_restype(self) -> None:
        source = self._source()
        matches = self._CAST_PATTERN.findall(source)
        self.assertTrue(matches, "the call-site scan itself found nothing -- check the pattern")
        for token in matches:
            with self.subTest(token=token):
                restype = getattr(ctypes, token.removeprefix("ctypes."))
                self.assertTrue(
                    _is_real_ctypes_type(restype),
                    f"platform.py casts objc_msgSend with restype={token!r}, not a real ctypes type",
                )

    def test_objc_getclass_and_sel_registername_declare_restype_and_argtypes(self) -> None:
        source = self._source()
        for name in ("objc_getClass", "sel_registerName"):
            with self.subTest(name=name):
                self.assertIn(
                    f"libobjc.{name}.restype = ctypes.c_void_p",
                    source,
                    f"{name}'s restype is not declared as ctypes.c_void_p -- left undeclared, "
                    "ctypes defaults restype to a truncating 32-bit c_int",
                )
                self.assertIn(
                    f"libobjc.{name}.argtypes = [ctypes.c_char_p]",
                    source,
                    f"{name}'s argtypes is not declared",
                )


class LibobjcHelperDeclarationTests(unittest.TestCase):
    """`mac_clipboard.py` and `mac_focus.py` each hold their own copy of the
    `_libobjc()` helper that declares `objc_getClass`/`sel_registerName`'s
    `restype`/`argtypes` inline (`overlay_renderer_qt.py`'s own copy declares
    only `sel_registerName` -- it never looks a class up by name, only ever
    receiving an `NSView` pointer from Qt). `platform.py`'s microphone check
    duplicates the same two lines a fourth time, in its own local variable
    rather than a module-level `_libobjc_dll` (`PlatformMicrophoneObjcCallTests`
    above). Four independent copies of the same declaration is exactly the
    shape a Windows struct redeclared once per file, and once in its test,
    drifted apart in without anything catching it (`ab1f794`'s INPUT-struct
    fix) -- so every copy is checked here, not only the one
    `PlatformMicrophoneObjcCallTests` already covers, using a variable-name-
    agnostic pattern so this does not care whether the handle is called
    `libobjc` or `_libobjc_dll`.
    """

    _RESTYPE_ARGTYPES_PATTERN = staticmethod(
        lambda symbol: re.compile(
            rf"\.{symbol}\.restype\s*=\s*(ctypes\.\w+)\s*\n\s*[\w.]*\.{symbol}\.argtypes\s*=\s*\[([^\]]*)\]"
        )
    )

    #: `objc_getClass(const char *)`/`sel_registerName(const char *)` both
    #: return a plain Objective-C pointer and take one C string -- the exact
    #: types every one of these four independent copies must declare, not
    #: merely *some* real ctypes type: `PlatformMicrophoneObjcCallTests`
    #: above already holds `platform.py`'s own copy to this exact pair via
    #: `assertIn`, and a copy that declared, say, `ctypes.c_int` instead of
    #: `ctypes.c_void_p` would still pass a weaker "is this a real type at
    #: all" check while truncating the returned pointer on 64-bit Darwin.
    _EXPECTED_RESTYPE = "ctypes.c_void_p"
    _EXPECTED_ARGTYPES = ("ctypes.c_char_p",)

    def _assert_declared(self, module: object, symbol: str) -> None:
        source = Path(module.__file__).read_text(encoding="utf-8")
        matches = self._RESTYPE_ARGTYPES_PATTERN(symbol).findall(source)
        self.assertTrue(
            matches, f"{module.__name__} never declares {symbol}'s restype/argtypes together"
        )
        for restype_token, argtypes_body in matches:
            with self.subTest(module=module.__name__, symbol=symbol):
                self.assertEqual(
                    self._EXPECTED_RESTYPE,
                    restype_token,
                    f"{module.__name__}'s {symbol}.restype is {restype_token!r}, not "
                    f"{self._EXPECTED_RESTYPE} -- ctypes would otherwise default it to a "
                    "truncating 32-bit c_int, or accept a wrong-but-real type here",
                )
                argtype_tokens = tuple(token.strip() for token in argtypes_body.split(",") if token.strip())
                self.assertEqual(
                    self._EXPECTED_ARGTYPES,
                    argtype_tokens,
                    f"{module.__name__}'s {symbol}.argtypes is {argtype_tokens!r}, not "
                    f"{self._EXPECTED_ARGTYPES}",
                )

    def test_sel_registername_is_declared_in_every_module_that_calls_it(self) -> None:
        for module in (mac_clipboard, mac_focus, overlay_renderer_qt, murmly_platform):
            with self.subTest(module=module.__name__):
                self._assert_declared(module, "sel_registerName")

    def test_objc_getclass_is_declared_in_every_module_that_calls_it(self) -> None:
        # `overlay_renderer_qt.py` never calls `objc_getClass` at all (its own
        # docstring says so -- it only ever receives an `NSView` pointer),
        # so it is deliberately excluded here, unlike the `sel_registerName`
        # check above which every one of these four modules needs.
        for module in (mac_clipboard, mac_focus, murmly_platform):
            with self.subTest(module=module.__name__):
                self._assert_declared(module, "objc_getClass")


class DaemonGetpeereidSignatureTests(unittest.TestCase):
    """`daemon.py`'s `read_peer_identity_macos` (task 13.2) is a plain-C
    libSystem call outside `mac_hotkey.py`/`mac_clipboard.py`'s scan above and
    otherwise invisible to every check in this module. `uid_t`/`gid_t` are
    4-byte, but `getpeereid` itself returns `c_int` and takes two pointers --
    an undeclared `restype` would still default to the truncating 32-bit
    `c_int` `read_peer_identity_macos`'s own docstring says this call happens
    to be safe from by accident; declaring it explicitly is the rule
    regardless of whether a given call happens to be safe from the actual
    defect.
    """

    _PATTERN = re.compile(
        r"libc\.getpeereid\.restype\s*=\s*(ctypes\.\w+)\s*\n\s*"
        r"libc\.getpeereid\.argtypes\s*=\s*\[([^\]]*)\]"
    )

    def _source(self) -> str:
        return Path(daemon.__file__).read_text(encoding="utf-8")

    def test_getpeereid_declares_a_real_restype_and_argtypes(self) -> None:
        source = self._source()
        matches = self._PATTERN.findall(source)
        self.assertTrue(matches, "the call-site scan itself found nothing -- check the pattern")
        restype_token, argtypes_body = matches[0]
        restype = getattr(ctypes, restype_token.removeprefix("ctypes."))
        self.assertTrue(
            _is_real_ctypes_type(restype), f"getpeereid's restype {restype_token!r} is not a real ctypes type"
        )
        argtype_tokens = [token.strip() for token in argtypes_body.split(",") if token.strip()]
        self.assertEqual(3, len(argtype_tokens), "getpeereid takes exactly (int, uid_t*, gid_t*)")
        for argtype_token in argtype_tokens:
            with self.subTest(argtype=argtype_token):
                # `ctypes.POINTER(_DARWIN_UID_T)` is not a bare `ctypes.*`
                # attribute -- evaluated against the real module's own
                # namespace so `_DARWIN_UID_T`/`_DARWIN_GID_T` resolve to
                # what `daemon.py` actually declared them as, rather than
                # this test guessing their spelling.
                argtype = eval(argtype_token, {"ctypes": ctypes}, vars(daemon))  # noqa: S307
                self.assertTrue(
                    _is_real_ctypes_type(argtype),
                    f"getpeereid's argtypes includes {argtype_token!r}, not a real ctypes type",
                )


if __name__ == "__main__":
    unittest.main()
