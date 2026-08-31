"""Task 14.6, 14.7: `MacosFocusObserver`, exercised against fakes standing in
for the three native seams (`frontmost_application`, `application_pid`,
`application_bundle_identifier`), mirroring `test_win_focus.py` test-for-test.

None of this touches a real framework -- that half (`_real_frontmost_
application` and friends) can only be confirmed on macOS. What is proven here
is the assembly `MacosFocusObserver.active_window` does from those three
answers: a `WindowIdentity` naming the process id twice (there is no window
handle on this platform -- see the module's own docstring for why the pid
fills both roles) and the bundle identifier in place of X11's `WM_CLASS`
(design.md's "Focus observation by owning application, never by window
title").
"""

from __future__ import annotations

import sys
import unittest

from murmly.focus import WindowIdentity
from murmly.mac_focus import MacosFocusObserver


class MacosFocusObserverTests(unittest.TestCase):
    def test_needs_no_permission_and_is_always_supported(self) -> None:
        observer = MacosFocusObserver(frontmost_application=lambda: None)

        self.assertTrue(observer.supported)
        self.assertIsNone(observer.detail)

    def test_no_frontmost_application_reports_none(self) -> None:
        observer = MacosFocusObserver(frontmost_application=lambda: None)

        self.assertIsNone(observer.active_window())

    def test_assembles_identity_from_the_three_seams(self) -> None:
        observer = MacosFocusObserver(
            frontmost_application=lambda: 4242,
            application_pid=lambda app: 99 if app == 4242 else None,
            application_bundle_identifier=lambda app: "com.example.editor" if app == 4242 else None,
        )

        identity = observer.active_window()

        self.assertEqual(
            WindowIdentity(window_id=99, pid=99, window_class="com.example.editor"), identity
        )

    def test_no_pid_reports_none_rather_than_a_half_built_identity(self) -> None:
        """Apple's own documentation names a command-line tool becoming the
        frontmost application as a case with no bundle identifier; a missing
        pid is treated as more fundamental -- there is nothing to identify at
        all without it, unlike a missing bundle identifier alone."""
        observer = MacosFocusObserver(
            frontmost_application=lambda: 4242,
            application_pid=lambda app: None,
            application_bundle_identifier=lambda app: self.fail("must not be called without a pid"),
        )

        identity = observer.active_window()

        self.assertIsNone(identity)

    def test_no_bundle_identifier_still_returns_the_pid(self) -> None:
        observer = MacosFocusObserver(
            frontmost_application=lambda: 4242,
            application_pid=lambda app: 55,
            application_bundle_identifier=lambda app: None,
        )

        identity = observer.active_window()

        self.assertEqual(WindowIdentity(window_id=55, pid=55, window_class=None), identity)

    def test_a_different_frontmost_application_does_not_match(self) -> None:
        """`WindowIdentity.matches` is what `focus.should_deliver` relies on to
        refuse a paste when focus moved -- proved here for a macOS identity
        the same way `test_win_focus.py` proves it for a Windows one."""
        first = MacosFocusObserver(
            frontmost_application=lambda: 1,
            application_pid=lambda app: 10,
            application_bundle_identifier=lambda app: "com.example.editor",
        ).active_window()
        second = MacosFocusObserver(
            frontmost_application=lambda: 1,
            application_pid=lambda app: 20,
            application_bundle_identifier=lambda app: "com.example.browser",
        ).active_window()

        self.assertFalse(first.matches(second))


class MacosFocusRuntimeIntegrationTests(unittest.TestCase):
    """Task 14.6, against the real `NSWorkspace` calls: everything above this
    class proves `MacosFocusObserver`'s assembly logic against fakes. This
    follows the `WindowsFocusRuntimeIntegrationTests` (`test_win_focus.py`)
    pattern of skipping in `setUp` on every platform but the one with the
    mechanism, and its own split into "runs without raising" versus "reports
    a well-formed identity", for the same reason that class gives: a headless
    CI runner may have no application genuinely frontmost.
    """

    def setUp(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("A macOS kernel is required to call NSWorkspace")

    def test_the_real_calls_compose_without_raising(self) -> None:
        observer = MacosFocusObserver()

        identity = observer.active_window()

        self.assertTrue(identity is None or isinstance(identity, WindowIdentity))

    def test_a_real_session_reports_a_well_formed_frontmost_application(self) -> None:
        observer = MacosFocusObserver()

        identity = observer.active_window()

        if identity is None:
            self.skipTest(
                "NSWorkspace reported no frontmost application on this runner -- "
                "very likely no interactive session is attached."
            )

        self.assertGreater(identity.pid, 0)
        self.assertEqual(identity.window_id, identity.pid)


if __name__ == "__main__":
    unittest.main()
