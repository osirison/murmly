"""Task 9.1-9.3, 18.11: `WindowsClipboardPaster`'s policy layer, exercised
against fakes standing in for the three Win32 seams (`write_clipboard`,
`read_clipboard`, `send_ctrl_v`).

None of this touches `ctypes.windll` -- that half (`_real_write_clipboard`,
`_real_read_clipboard`, `_real_send_ctrl_v`) can only be confirmed on Windows,
and cannot even be imported here: `ctypes.windll` does not exist on Linux.
What is proven here is everything the module's own docstring claims for the
fakeable half: `send-input` is always available and never confirms delivery,
so a paste never reads or restores the previous clipboard contents (task 9.3,
the `SendInput` half of 18.11 -- `CGEventPost` is section 14), and a failed
injection still leaves the transcript on the clipboard.
"""

from __future__ import annotations

import sys
import unittest

from murmly.integrations import DeliveryOutcome, PasteInjection
from murmly.win_clipboard import SEND_INPUT_METHOD, WindowsClipboardPaster


class RecordingSeams:
    """A single-object stand-in for all three Win32 seams, recording order."""

    def __init__(self, send_ctrl_v_error: Exception | None = None) -> None:
        self.written: list[str] = []
        self.read_calls = 0
        self.sent = 0
        self._send_ctrl_v_error = send_ctrl_v_error
        self.clipboard: str | None = "PREVIOUS-CLIPBOARD"

    def write_clipboard(self, text: str) -> None:
        self.written.append(text)
        self.clipboard = text

    def read_clipboard(self) -> str | None:
        self.read_calls += 1
        return self.clipboard

    def send_ctrl_v(self) -> None:
        self.sent += 1
        if self._send_ctrl_v_error is not None:
            raise self._send_ctrl_v_error


def _windows_select_injection(excluded=frozenset()) -> PasteInjection:
    """Stands in for `integrations.select_paste_injection` resolved against a
    real Windows machine -- this Linux test machine's own `sys.platform`
    would otherwise answer with whatever Wayland/X11 tool happens to be
    installed here, which is exactly the wrong shape for these tests."""
    if SEND_INPUT_METHOD in set(excluded):
        return PasteInjection(
            None, None, reason=f"{SEND_INPUT_METHOD} failed to inject a paste earlier in this session"
        )
    return PasteInjection(SEND_INPUT_METHOD, (), confirms_delivery=False)


def _paster(seams: RecordingSeams, sleeps: list[float], **kwargs) -> WindowsClipboardPaster:
    kwargs.setdefault("select_injection", _windows_select_injection)
    return WindowsClipboardPaster(
        write_clipboard=seams.write_clipboard,
        read_clipboard=seams.read_clipboard,
        send_ctrl_v=seams.send_ctrl_v,
        sleep=sleeps.append,
        **kwargs,
    )


class SendInputIsAlwaysSelectedTests(unittest.TestCase):
    def test_injection_is_send_input_and_does_not_confirm(self) -> None:
        seams = RecordingSeams()
        paster = _paster(seams, [])

        self.assertEqual(SEND_INPUT_METHOD, paster.injection.method)
        self.assertTrue(paster.injection.available)
        self.assertFalse(paster.injection.confirms_delivery)


class UnconfirmableDeliveryTests(unittest.TestCase):
    """Task 9.3 / 18.11: the rule `integrations.ClipboardPaster` already
    applies to an unconfirmable method, proved here for `SendInput`."""

    def test_a_successful_paste_never_reads_or_restores_the_clipboard(self) -> None:
        seams = RecordingSeams()
        sleeps: list[float] = []
        paster = _paster(seams, sleeps, restore_clipboard=True, restore_delay_ms=500)

        outcome = paster.copy_and_paste("transcript")

        self.assertEqual(DeliveryOutcome(True), outcome)
        self.assertEqual(1, seams.sent)
        # The transcript is what remains on the fake clipboard: no restore
        # ever ran, so nothing overwrote it after `copy` wrote it once.
        self.assertEqual(["transcript"], seams.written)
        self.assertEqual("transcript", seams.clipboard)
        # The sharpest assertion: the previous-contents read never happens at
        # all for an unconfirmable method, not merely "is not used".
        self.assertEqual(0, seams.read_calls)
        self.assertEqual([], sleeps)

    def test_restore_disabled_behaves_identically(self) -> None:
        """`confirms_delivery=False` already forces this; disabling restore
        too must not change anything observable."""
        seams = RecordingSeams()
        sleeps: list[float] = []
        paster = _paster(seams, sleeps, restore_clipboard=False, restore_delay_ms=500)

        paster.copy_and_paste("transcript")

        self.assertEqual(0, seams.read_calls)
        self.assertEqual([], sleeps)

    def test_a_failed_send_input_leaves_the_transcript_on_the_clipboard(self) -> None:
        seams = RecordingSeams(send_ctrl_v_error=OSError("SendInput accepted 0 of 4 events"))
        sleeps: list[float] = []
        paster = _paster(seams, sleeps, restore_clipboard=True, restore_delay_ms=500)

        outcome = paster.copy_and_paste("transcript")

        self.assertFalse(outcome.injected)
        self.assertIn("send-input failed to inject the paste", outcome.reason)
        self.assertEqual(["transcript"], seams.written)
        self.assertEqual("transcript", seams.clipboard)
        self.assertEqual(0, seams.read_calls)
        self.assertEqual([], sleeps)

    def test_a_failed_send_input_is_not_used_again_this_session(self) -> None:
        seams = RecordingSeams(send_ctrl_v_error=OSError("boom"))
        paster = _paster(seams, [])

        paster.copy_and_paste("transcript")

        self.assertFalse(paster.injection.available)
        self.assertIn("send-input", paster.injection.reason)

        # And a second attempt copies without trying to inject again -- there
        # is no second Windows method to fall back to, so `send_ctrl_v` is
        # never called a second time.
        outcome = paster.copy_and_paste("another transcript")
        self.assertFalse(outcome.injected)
        self.assertEqual(1, seams.sent)
        self.assertEqual(["transcript", "another transcript"], seams.written)


class UnavailableInjectionTests(unittest.TestCase):
    def test_an_unavailable_injection_still_copies(self) -> None:
        seams = RecordingSeams()

        def unavailable(*_args, **_kwargs) -> PasteInjection:
            return PasteInjection(None, None, reason="send-input failed earlier in this session")

        paster = _paster(seams, [], select_injection=unavailable)

        outcome = paster.copy_and_paste("transcript")

        self.assertFalse(outcome.injected)
        self.assertIn("earlier in this session", outcome.reason)
        self.assertEqual(["transcript"], seams.written)
        self.assertEqual(0, seams.sent)


class ImportsCleanlyOnLinuxTests(unittest.TestCase):
    def test_default_construction_touches_no_win32_name(self) -> None:
        """`murmly.win_clipboard` imports cleanly from this Linux machine, and
        constructing the class -- every other test in this file does exactly
        this -- touches only `select_paste_injection`, never `ctypes.windll`.
        The real seam bodies (`_real_write_clipboard`, `_real_read_clipboard`,
        `_real_send_ctrl_v`) can only be confirmed on Windows."""
        WindowsClipboardPaster()


class WindowsClipboardRuntimeIntegrationTests(unittest.TestCase):
    """Task 9.1, and the acceptance half of 9.2, against the real Win32
    calls: everything above this class proves the policy layer against
    fakes, since `ctypes.windll` cannot even be imported from Linux. This
    follows the `WindowsPipeSecurityDescriptorIntegrationTests`
    (`test_platform.py`) pattern of skipping in `setUp` on every platform but
    the one with the mechanism.

    The clipboard is a single machine-wide slot, so every test here reads
    whatever it held on entry and restores exactly that in `tearDown` --
    never leaving its own fixture text sitting there afterwards, which the
    task this class exists for calls out by name as a defect.
    """

    def setUp(self) -> None:
        if sys.platform != "win32":
            self.skipTest("A Windows kernel is required to reach the Win32 clipboard")
        from murmly.win_clipboard import _real_read_clipboard

        try:
            self._previous = _real_read_clipboard()
        except OSError as error:
            self.skipTest(f"OpenClipboard failed reading the prior contents: {error}")

    def tearDown(self) -> None:
        from murmly.win_clipboard import _real_write_clipboard

        if self._previous is not None:
            _real_write_clipboard(self._previous)
        else:
            self._clear_clipboard()

    @staticmethod
    def _clear_clipboard() -> None:
        """`OpenClipboard`/`EmptyClipboard`/`CloseClipboard`, with no
        `SetClipboardData` call -- there is nothing to put back when the
        clipboard held no `CF_UNICODETEXT` value on entry, and writing an
        empty string would itself be a value the clipboard did not have
        before this test ran."""
        import ctypes

        user32 = ctypes.windll.user32
        if not user32.OpenClipboard(None):
            return
        try:
            user32.EmptyClipboard()
        finally:
            user32.CloseClipboard()

    def test_round_trip_of_text_outside_any_console_codepage(self) -> None:
        """The whole reason this module exists rather than shelling out to
        `clip.exe` (module docstring): `clip.exe` reads stdin through the
        console's OEM/ANSI codepage, which loses every one of these
        characters. `CF_UNICODETEXT` is UTF-16LE with no codepage in the
        path, so this fixture -- CJK, a Latin letter no Windows-1252 codepage
        carries, and an astral-plane emoji needing a UTF-16 surrogate pair --
        must survive exactly."""
        from murmly.win_clipboard import _real_read_clipboard, _real_write_clipboard

        fixture = "日本語のテスト 😀 Straße Ĺẅṽ"

        _real_write_clipboard(fixture)
        roundtripped = _real_read_clipboard()

        self.assertEqual(fixture, roundtripped)

    def test_send_input_accepts_the_hand_built_input_struct(self) -> None:
        """Task 9.2's acceptance half of 18.11: `SendInput` receiving the
        `INPUT`/`KEYBDINPUT` structs `_real_send_ctrl_v` builds by hand and
        reporting every event accepted (`sent == 4`) is what proves the
        struct layout -- `cbSize`, the anonymous union, `ULONG_PTR` sized
        correctly for this process's bitness -- matches what real `user32`
        expects; a wrong one fails exactly here with `sent != 4`, distinctly
        from `OSError` on a genuinely refused call.

        This says nothing about *delivery* -- task 9.3 and 18.11's other half
        register that as permanently unconfirmable, since UIPI drops
        synthetic input aimed at a higher-integrity window without telling
        the sender. So the clipboard is seeded with a short, harmless marker
        first: wherever this Ctrl+V actually lands (nothing may have focus at
        all on a CI runner), it must never risk pasting something that
        matters into it.
        """
        from murmly.win_clipboard import _real_send_ctrl_v, _real_write_clipboard

        _real_write_clipboard("murmly-integration-test-marker")

        _real_send_ctrl_v()  # raises OSError on a struct/acceptance mismatch


if __name__ == "__main__":
    unittest.main()
