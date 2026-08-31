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

from murmly import mac_clipboard, mac_focus, mac_hotkey, overlay_renderer_qt
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


if __name__ == "__main__":
    unittest.main()
