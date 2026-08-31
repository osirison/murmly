"""Every Win32 call in `win_clipboard.py`/`win_focus.py`/`win_hotkey.py`/
`overlay_renderer_qt.py` declares `restype`/`argtypes`, and does so on a
module-private `WinDLL` rather than `ctypes.windll`'s process-wide cache.

`ctypes` defaults an undeclared function's `restype` to `c_int` -- 32 bits.
On 64-bit Windows, `GlobalAlloc`/`GlobalLock`/`GlobalFree` return an
`HGLOBAL`, and `GetForegroundWindow`/`OpenProcess` return an `HWND`/`HANDLE`
-- all 64-bit pointers -- so an undeclared `restype` truncates the value
before the module ever sees it. That is exactly the bug the first real
Windows CI run found: `GlobalAlloc` succeeded, but the handle reaching
`GlobalLock` was not the one it returned, because the missing `restype`
truncated it in between. This module can never call the real Win32
functions from Linux (`ctypes.windll`/`ctypes.WinDLL` do not exist here at
all -- see `ImportsCleanlyOnLinuxTests` in `test_win_clipboard.py`), so
instead it proves the *declarations* are complete and well-typed by reading
each module's own source: every attribute access of the form
`user32.Name(`/`kernel32.Name(` must name a function present in that
module's own signature table, and every table entry must carry a `restype`
and an `argtypes` tuple built only from real ctypes types. A call added to
either module with no declared signature fails here, on Linux, rather than
surfacing as a corrupted handle on a Windows machine -- following the
source-scanning pattern already used by `test_overlay_shared.py`,
`test_overlay_renderer_qt.py`, and `test_tts.py`.
"""

from __future__ import annotations

import ctypes
import re
import unittest
from pathlib import Path

from murmly import overlay_renderer_qt, win_clipboard, win_focus, win_hotkey


#: Matches every place one of these modules' source calls a function off the
#: local variable holding its `user32`/`kernel32` handle -- `user32.Foo(...)`
#: -- which is the sole calling convention all four modules use (each
#: `_real_*` function binds `user32 = _user32()` / `kernel32 = _kernel32()`
#: before calling through it, rather than chaining `_user32().Foo(...)`
#: directly, precisely so this one pattern catches every call site).
_CALL_PATTERN = re.compile(r"\b(user32|kernel32)\.([A-Za-z_]\w*)\(")

#: The real ctypes simple/pointer/struct types a signature is allowed to be
#: built from -- anything else (a bare `int`, a forgotten `None`) is not a
#: declaration ctypes can actually enforce.
_REAL_CTYPES_BASES = (ctypes._SimpleCData, ctypes.Structure, ctypes.Union, ctypes.Array)


def _is_real_ctypes_type(value: object) -> bool:
    if value is None:
        # A legitimate restype for a function whose return value is never
        # read -- none of the four modules currently declare one this way,
        # but a void-returning declaration is not itself a defect.
        return True
    return isinstance(value, type) and (
        issubclass(value, _REAL_CTYPES_BASES) or issubclass(value, ctypes._Pointer)
    )


class _ModuleSignatureCoverageMixin:
    """Shared by one `TestCase` per module below -- see each subclass for
    which module, its signature tables, and which handles (`user32` only,
    or both) it declares."""

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
            "32-bit c_int for any of these that return a pointer or handle.",
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


class WinClipboardSignatureTests(_ModuleSignatureCoverageMixin, unittest.TestCase):
    module = win_clipboard
    signature_tables = (win_clipboard._KERNEL32_SIGNATURES, win_clipboard._USER32_SIGNATURES)


class WinFocusSignatureTests(_ModuleSignatureCoverageMixin, unittest.TestCase):
    module = win_focus
    signature_tables = (win_focus._KERNEL32_SIGNATURES, win_focus._USER32_SIGNATURES)


class WinHotkeySignatureTests(_ModuleSignatureCoverageMixin, unittest.TestCase):
    module = win_hotkey
    signature_tables = (win_hotkey._KERNEL32_SIGNATURES, win_hotkey._USER32_SIGNATURES)


class OverlayRendererQtSignatureTests(_ModuleSignatureCoverageMixin, unittest.TestCase):
    """`GetWindowLongPtrW`/`SetWindowLongPtrW` return `LONG_PTR` -- pointer
    sized -- the same defect class as `win_clipboard.py`'s `GlobalAlloc`
    family, found in this module only by this sweep: it used
    `ctypes.windll.user32.GetWindowLongPtrW(...)`/`SetWindowLongPtrW(...)`
    directly, with no `restype`/`argtypes` declared at all, so an undeclared
    `restype` truncated the readback `apply_and_verify_exstyle` depends on to
    a 32-bit `c_int`."""

    module = overlay_renderer_qt
    signature_tables = (overlay_renderer_qt._USER32_SIGNATURES,)


class PrivateLibraryHandleTests(unittest.TestCase):
    """None of the four modules may configure `ctypes.windll`'s shared,
    process-wide handle -- that cache is also used directly by
    `test_win_hotkey.py`'s own `WindowsHotkeyRuntimeIntegrationTests` to
    build and send an unrelated `SendInput` call, and a signature declared
    on the shared handle by this module would silently change what that
    other, independent caller receives back for calls it never opted into.
    Each module must instead hold its own `ctypes.WinDLL` instance, exactly
    as `focus.py`'s `X11FocusObserver` holds its own `ctypes.CDLL` rather
    than reaching for a shared loader."""

    def test_no_module_touches_the_shared_windll_cache(self) -> None:
        for module in (win_clipboard, win_focus, win_hotkey, overlay_renderer_qt):
            with self.subTest(module=module.__name__):
                source = Path(module.__file__).read_text(encoding="utf-8")
                self.assertIsNone(
                    re.search(r"\bwindll\b", source),
                    f"{module.__name__} must configure its own ctypes.WinDLL instance, "
                    "never ctypes.windll's shared cache",
                )


class KeybdinputSharesOneClassTests(unittest.TestCase):
    """The bug `WindowsClipboardRuntimeIntegrationTests` never got to run:
    `_keybd_input` used to define `INPUT`/`KEYBDINPUT` as fresh classes on
    every call, so the four structs `_real_send_ctrl_v` packs into one
    `ctypes.Array` were four different, mutually-incompatible types --
    `ctypes.Array` construction rejects mixing instances of "the same"
    struct that come from different class objects. It never surfaced
    because `_real_write_clipboard`'s `GlobalLock` bug always raised first.
    """

    def test_keybd_input_returns_the_same_class_every_call(self) -> None:
        first = win_clipboard._keybd_input(0x11, key_up=False)
        second = win_clipboard._keybd_input(0x56, key_up=True)
        self.assertIs(type(first), type(second))

    def test_four_keybd_inputs_pack_into_one_array(self) -> None:
        entries = [
            win_clipboard._keybd_input(0x11, key_up=False),
            win_clipboard._keybd_input(0x56, key_up=False),
            win_clipboard._keybd_input(0x56, key_up=True),
            win_clipboard._keybd_input(0x11, key_up=True),
        ]
        # Raises TypeError("incompatible types, ... instead of ...") if any
        # entry is not literally the same class as the others.
        array = (win_clipboard._INPUT * len(entries))(*entries)
        self.assertEqual(4, len(array))


class ExtraInfoIsPointerSizedTests(unittest.TestCase):
    """`KEYBDINPUT.dwExtraInfo` is `ULONG_PTR` in `winuser.h` -- pointer
    sized. Asserted as a relationship to `sizeof(c_void_p)` on *this* host,
    never as a literal byte count: `wintypes.DWORD`/`WPARAM` resolve to
    different concrete widths on Linux than on Windows (`ctypes.c_ulong` is
    the host C compiler's `unsigned long`, 8 bytes under glibc's LP64, 4
    under MSVC's LLP64), so a hardcoded `sizeof(_KEYBDINPUT) == 40` would
    pass here for the wrong reason and prove nothing about Windows. The
    exact struct layout Windows expects is what
    `WindowsClipboardRuntimeIntegrationTests.test_send_input_accepts_the_hand_built_input_struct`
    proves empirically, on Windows, by having real `user32` accept it.
    """

    def test_dw_extra_info_field_is_pointer_sized(self) -> None:
        from ctypes import wintypes

        self.assertEqual(
            ctypes.sizeof(ctypes.c_void_p),
            ctypes.sizeof(wintypes.WPARAM),
        )

    def test_keybdinput_field_order_matches_winuser_h(self) -> None:
        self.assertEqual(
            ["wVk", "wScan", "dwFlags", "time", "dwExtraInfo"],
            [name for name, _type in win_clipboard._KEYBDINPUT._fields_],
        )

    def test_input_struct_has_an_anonymous_keyboard_union(self) -> None:
        self.assertEqual(("_input",), win_clipboard._INPUT._anonymous_)
        entry = win_clipboard._INPUT()
        entry.ki = win_clipboard._KEYBDINPUT(wVk=1, wScan=0, dwFlags=0, time=0, dwExtraInfo=0)
        self.assertEqual(1, entry.ki.wVk)

    def test_the_union_carries_every_member_winuser_h_gives_it(self) -> None:
        """A union is as large as its largest member, and only `ki` is written.

        Declaring it with `ki` alone made `sizeof(INPUT)` 32 on Windows where
        the real size is 40, because `MOUSEINPUT` is the largest of the three
        and it was absent. `SendInput` checks the `cbSize` it is passed
        against its own size and rejected the batch outright:
        `SendInput accepted 0 of 4 events; GetLastError=87`.

        Asserted as a relationship rather than a byte count, because
        `wintypes.DWORD` is `c_ulong` -- 4 bytes under MSVC, 8 on this host --
        so every literal size differs between the machine that runs this and
        the machine the struct is for.
        """
        self.assertEqual(
            ["ki", "mi", "hi"],
            [name for name, _type in win_clipboard._InputUnion._fields_],
        )
        largest = max(
            ctypes.sizeof(member)
            for _name, member in win_clipboard._InputUnion._fields_
        )
        self.assertEqual(largest, ctypes.sizeof(win_clipboard._InputUnion))
        self.assertGreater(
            ctypes.sizeof(win_clipboard._MOUSEINPUT),
            ctypes.sizeof(win_clipboard._KEYBDINPUT),
            "MOUSEINPUT is what decides sizeof(INPUT); if it stops being the "
            "largest member this test is no longer guarding what it thinks",
        )


if __name__ == "__main__":
    unittest.main()
