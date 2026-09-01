"""Task 14.1-14.5, 18.11, 18.12: `MacosClipboardPaster`'s policy layer,
exercised against fakes standing in for its native seams
(`write_clipboard`, `read_clipboard`, `send_cmd_v`), mirroring
`test_win_clipboard.py` test-for-test where the two backends share a rule.

None of this touches a real framework -- that half (`_real_write_clipboard`,
`_real_read_clipboard`, `_real_send_cmd_v`, `_real_is_process_trusted`,
`request_accessibility_permission`) can only be confirmed on macOS. What is
proven here is everything the module's own docstring claims for the fakeable
half: `cgevent-post` never confirms delivery, so a paste never reads or
restores the previous clipboard contents (task 14.3, the `CGEventPost` half
of 18.11), a failed injection still leaves the transcript on the clipboard,
and an ungranted Accessibility permission is reported distinctly from an
absent method (task 18.12).
"""

from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from murmly.integrations import DeliveryOutcome, PasteInjection, select_paste_injection
from murmly.mac_clipboard import CGEVENT_POST_METHOD, MacosClipboardPaster, request_accessibility_permission
from murmly.platform import MACOS_ACCESSIBILITY_PERMISSION, OperatingSystem, PERMISSIONS, PlatformProfile


def _macos_profile() -> PlatformProfile:
    return PlatformProfile(operating_system=OperatingSystem.MACOS, architecture="arm64")


class RecordingSeams:
    """A single-object stand-in for all three native seams, recording order."""

    def __init__(self, send_cmd_v_error: Exception | None = None) -> None:
        self.written: list[str] = []
        self.read_calls = 0
        self.sent = 0
        self._send_cmd_v_error = send_cmd_v_error
        self.clipboard: str | None = "PREVIOUS-CLIPBOARD"

    def write_clipboard(self, text: str) -> None:
        self.written.append(text)
        self.clipboard = text

    def read_clipboard(self) -> str | None:
        self.read_calls += 1
        return self.clipboard

    def send_cmd_v(self) -> None:
        self.sent += 1
        if self._send_cmd_v_error is not None:
            raise self._send_cmd_v_error


def _macos_select_injection(excluded=frozenset()) -> PasteInjection:
    """Stands in for `integrations.select_paste_injection` resolved against a
    trusted macOS process -- this Linux test machine's own `sys.platform`
    would otherwise never reach the macOS branch at all."""
    if CGEVENT_POST_METHOD in set(excluded):
        return PasteInjection(
            None, None, reason=f"{CGEVENT_POST_METHOD} failed to inject a paste earlier in this session"
        )
    return PasteInjection(CGEVENT_POST_METHOD, (), confirms_delivery=False)


def _paster(seams: RecordingSeams, sleeps: list[float], **kwargs) -> MacosClipboardPaster:
    kwargs.setdefault("select_injection", _macos_select_injection)
    return MacosClipboardPaster(
        write_clipboard=seams.write_clipboard,
        read_clipboard=seams.read_clipboard,
        send_cmd_v=seams.send_cmd_v,
        sleep=sleeps.append,
        **kwargs,
    )


class CgeventPostIsAlwaysSelectedWhenTrustedTests(unittest.TestCase):
    def test_injection_is_cgevent_post_and_does_not_confirm(self) -> None:
        seams = RecordingSeams()
        paster = _paster(seams, [])

        self.assertEqual(CGEVENT_POST_METHOD, paster.injection.method)
        self.assertTrue(paster.injection.available)
        self.assertFalse(paster.injection.confirms_delivery)


class UnconfirmableDeliveryTests(unittest.TestCase):
    """Task 14.3 / 18.11: the rule `integrations.ClipboardPaster` already
    applies to an unconfirmable method, proved here for `CGEventPost`."""

    def test_a_successful_paste_never_reads_or_restores_the_clipboard(self) -> None:
        seams = RecordingSeams()
        sleeps: list[float] = []
        paster = _paster(seams, sleeps, restore_clipboard=True, restore_delay_ms=500)

        outcome = paster.copy_and_paste("transcript")

        self.assertEqual(DeliveryOutcome(True), outcome)
        self.assertEqual(1, seams.sent)
        self.assertEqual(["transcript"], seams.written)
        self.assertEqual("transcript", seams.clipboard)
        self.assertEqual(0, seams.read_calls)
        self.assertEqual([], sleeps)

    def test_restore_disabled_behaves_identically(self) -> None:
        seams = RecordingSeams()
        sleeps: list[float] = []
        paster = _paster(seams, sleeps, restore_clipboard=False, restore_delay_ms=500)

        paster.copy_and_paste("transcript")

        self.assertEqual(0, seams.read_calls)
        self.assertEqual([], sleeps)

    def test_a_failed_send_cmd_v_leaves_the_transcript_on_the_clipboard(self) -> None:
        seams = RecordingSeams(send_cmd_v_error=OSError("CGEventCreateKeyboardEvent returned NULL"))
        sleeps: list[float] = []
        paster = _paster(seams, sleeps, restore_clipboard=True, restore_delay_ms=500)

        outcome = paster.copy_and_paste("transcript")

        self.assertFalse(outcome.injected)
        self.assertIn("cgevent-post failed to inject the paste", outcome.reason)
        self.assertEqual(["transcript"], seams.written)
        self.assertEqual("transcript", seams.clipboard)
        self.assertEqual(0, seams.read_calls)
        self.assertEqual([], sleeps)

    def test_a_failed_send_cmd_v_is_not_used_again_this_session(self) -> None:
        seams = RecordingSeams(send_cmd_v_error=OSError("boom"))
        paster = _paster(seams, [])

        paster.copy_and_paste("transcript")

        self.assertFalse(paster.injection.available)
        self.assertIn("cgevent-post", paster.injection.reason)

        outcome = paster.copy_and_paste("another transcript")
        self.assertFalse(outcome.injected)
        self.assertEqual(1, seams.sent)
        self.assertEqual(["transcript", "another transcript"], seams.written)


class UnavailableInjectionTests(unittest.TestCase):
    def test_an_unavailable_injection_still_copies(self) -> None:
        seams = RecordingSeams()

        def unavailable(*_args, **_kwargs) -> PasteInjection:
            return PasteInjection(None, None, reason="cgevent-post failed earlier in this session")

        paster = _paster(seams, [], select_injection=unavailable)

        outcome = paster.copy_and_paste("transcript")

        self.assertFalse(outcome.injected)
        self.assertIn("earlier in this session", outcome.reason)
        self.assertEqual(["transcript"], seams.written)
        self.assertEqual(0, seams.sent)


class UngrantedAccessibilityPermissionTests(unittest.TestCase):
    """Task 18.12: an ungranted permission is reported distinctly from an
    absent method and from an installed-but-unusable one, and never as
    available -- exercised through `select_paste_injection` itself, not a
    fake standing in for it, since this is where that distinction is made."""

    def test_untrusted_is_reported_as_ungranted_not_absent(self) -> None:
        injection = select_paste_injection(
            profile=_macos_profile(), macos_accessibility_trusted=lambda: False
        )

        self.assertFalse(injection.available)
        self.assertIsNone(injection.method)
        self.assertIn("Accessibility", injection.reason)
        self.assertIn("not been granted", injection.reason)
        permission = PERMISSIONS[MACOS_ACCESSIBILITY_PERMISSION]
        self.assertIn(permission.grant_location, injection.remedy[0])

    def test_trusted_selects_cgevent_post(self) -> None:
        injection = select_paste_injection(
            profile=_macos_profile(), macos_accessibility_trusted=lambda: True
        )

        self.assertEqual(CGEVENT_POST_METHOD, injection.method)
        self.assertTrue(injection.available)
        self.assertFalse(injection.confirms_delivery)

    def test_excluded_after_a_prior_failure_reports_that_reason_even_when_trusted(self) -> None:
        injection = select_paste_injection(
            profile=_macos_profile(),
            macos_accessibility_trusted=lambda: True,
            excluded={CGEVENT_POST_METHOD},
        )

        self.assertFalse(injection.available)
        self.assertIn("failed to inject a paste earlier", injection.reason)


class RequestAccessibilityPermissionTests(unittest.TestCase):
    """Task 14.5: `request_accessibility_permission` never raises, even when
    the platform check it starts with fails to run at all -- the failure
    mode this Linux machine actually hits (no `ApplicationServices` framework
    to load), which is exactly why this must be proven here rather than only
    assumed from the docstring."""

    def test_already_trusted_returns_true_without_touching_corefoundation(self) -> None:
        self.assertTrue(request_accessibility_permission(is_trusted=lambda: True))

    def test_a_raising_is_trusted_check_is_caught_not_propagated(self) -> None:
        """The regression this pins: `is_trusted()` used to be called outside
        the function's own `try`, so a real `AXIsProcessTrusted()` failure --
        the framework not loading -- would have propagated straight out of
        an install, instead of being logged the way the docstring promises."""

        def explode() -> bool:
            raise OSError("ApplicationServices.framework not found")

        result = request_accessibility_permission(is_trusted=explode)  # must not raise

        self.assertFalse(result)

    def test_not_trusted_falls_back_to_false_when_applicationservices_will_not_load(self) -> None:
        """A host with no `ApplicationServices` framework at all -- every
        real Linux machine -- reaches `request_accessibility_permission`'s
        own `except (OSError, ValueError, AttributeError)` clause the moment
        it tries to load the library that would make the real request, not
        merely a failing `is_trusted()` (already pinned above). Injected
        through `_applicationservices` directly, the same seam a real Linux
        machine fails at, rather than relied on the machine actually lacking
        the framework: this used to be named `..._on_linux` and pass only
        because this suite happened to run on Linux, where the real load
        already failed for the same reason -- the exact same assertion now
        holds on any host, macOS included, once the load is forced to fail
        here instead of trusted to fail on its own."""
        with patch(
            "murmly.mac_clipboard._applicationservices",
            side_effect=OSError("ApplicationServices.framework not found"),
        ):
            result = request_accessibility_permission(is_trusted=lambda: False)

        self.assertFalse(result)


class ImportsCleanlyOnLinuxTests(unittest.TestCase):
    def test_default_construction_touches_no_framework(self) -> None:
        """`murmly.mac_clipboard` imports cleanly from this Linux machine, and
        constructing the class -- every other test in this file does exactly
        this -- touches only `select_paste_injection`, never a framework
        path. The real seam bodies can only be confirmed on macOS."""
        MacosClipboardPaster()


class MacosClipboardRuntimeIntegrationTests(unittest.TestCase):
    """Task 14.1, and the acceptance half of 14.2/14.4, against the real
    Objective-C and Carbon calls: everything above this class proves the
    policy layer against fakes, since the frameworks this module loads do not
    exist on Linux. Follows the `WindowsClipboardRuntimeIntegrationTests`
    (`test_win_clipboard.py`) pattern of skipping in `setUp` on every
    platform but the one with the mechanism.

    The clipboard is a single machine-wide slot, so every test here reads
    whatever it held on entry and restores exactly that in `tearDown` --
    never leaving its own fixture text sitting there afterwards, which
    `test_win_clipboard.py`'s own `WindowsClipboardRuntimeIntegrationTests`
    calls out by name as a defect for the same reason on the other platform:
    a `None` reading (nothing on the clipboard, or nothing in the
    `NSPasteboardTypeString` format) is restored by clearing the pasteboard,
    not by leaving whatever this test just wrote to it.
    """

    def setUp(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("A macOS kernel is required to reach NSPasteboard")
        from murmly.mac_clipboard import _real_read_clipboard

        try:
            self._previous = _real_read_clipboard()
        except OSError as error:
            self.skipTest(f"NSPasteboard could not be read: {error}")

    def tearDown(self) -> None:
        if getattr(self, "_previous", None) is not None:
            from murmly.mac_clipboard import _real_write_clipboard

            _real_write_clipboard(self._previous)
        else:
            self._clear_clipboard()

    @staticmethod
    def _clear_clipboard() -> None:
        """`[[NSPasteboard generalPasteboard] clearContents]` alone, with no
        `setString:forType:` call -- there is nothing to put back when the
        pasteboard held no `NSPasteboardTypeString` value on entry, and
        writing an empty string would itself be a value the pasteboard did
        not have before this test ran."""
        from murmly.mac_clipboard import _general_pasteboard, _libobjc, _send
        import ctypes

        pasteboard = _general_pasteboard()
        if not pasteboard:
            return
        libobjc = _libobjc()
        clear_selector = libobjc.sel_registerName(b"clearContents")
        _send(ctypes.c_long, (), pasteboard, clear_selector)

    def test_round_trip_of_text(self) -> None:
        from murmly.mac_clipboard import _real_read_clipboard, _real_write_clipboard

        fixture = "日本語のテスト 😀 Straße Ĺẅṽ"

        _real_write_clipboard(fixture)
        roundtripped = _real_read_clipboard()

        self.assertEqual(fixture, roundtripped)

    def test_is_process_trusted_answers_without_raising(self) -> None:
        """Task 14.4: `AXIsProcessTrusted()` must never prompt and must
        answer a plain `bool` -- whatever this test runner's own grant state
        happens to be, calling it must not raise and must not block."""
        from murmly.mac_clipboard import _real_is_process_trusted

        result = _real_is_process_trusted()

        self.assertIsInstance(result, bool)

    def test_not_trusted_attempts_the_real_request_and_does_not_raise(self) -> None:
        """Task 14.5, against the real `AXIsProcessTrustedWithOptions` call:
        `RequestAccessibilityPermissionTests` above proves the function's own
        catch-and-log-`False` policy against an injected framework-load
        failure -- the only thing provable from Linux. This proves the other
        half: bypassing the `is_trusted` short-circuit (`is_trusted=lambda:
        False`) and letting the real Accessibility request run on a real
        kernel must not raise and must answer a plain `bool`, whatever this
        runner's own grant state and the dialog's own presence or absence
        turn out to be -- both are the runner's, not this test's, to assert.
        A prior version of this test assumed the answer must be `False`,
        which does not hold: the request can genuinely return `True` on a
        runner where Accessibility is already granted."""
        result = request_accessibility_permission(is_trusted=lambda: False)

        self.assertIsInstance(result, bool)

    def test_cgevent_keyboard_event_can_be_created_and_released(self) -> None:
        """The acceptance half of 14.2: proves `CGEventCreateKeyboardEvent`
        and `CGEventPost` run without raising on a real kernel. This cannot
        prove the chord reached any window -- task 14.3 is exactly the rule
        that such proof is unavailable by design -- only that the call itself
        is well-formed."""
        from murmly.mac_clipboard import _real_send_cmd_v

        _real_send_cmd_v()


if __name__ == "__main__":
    unittest.main()
