"""Task 9.4: `WindowsFocusObserver`, exercised against fakes standing in for
the three Win32 seams (`foreground_window`, `window_pid`, `process_image_path`).

None of this touches `ctypes.windll` -- that half (`_real_foreground_window`
and friends) can only be confirmed on Windows. What is proven here is the
assembly `WindowsFocusObserver.active_window` does from those three answers:
a `WindowIdentity` naming the HWND, the owning process id, and the
executable's basename in place of X11's `WM_CLASS` (design.md's "Focus
observation by owning application, never by window title").
"""

from __future__ import annotations

import sys
import unittest

from murmly.focus import WindowIdentity
from murmly.win_focus import WindowsFocusObserver


class WindowsFocusObserverTests(unittest.TestCase):
    def test_needs_no_permission_and_is_always_supported(self) -> None:
        observer = WindowsFocusObserver(foreground_window=lambda: None)

        self.assertTrue(observer.supported)
        self.assertIsNone(observer.detail)

    def test_no_foreground_window_reports_none(self) -> None:
        observer = WindowsFocusObserver(foreground_window=lambda: None)

        self.assertIsNone(observer.active_window())

    def test_assembles_identity_from_the_three_seams(self) -> None:
        observer = WindowsFocusObserver(
            foreground_window=lambda: 4242,
            window_pid=lambda hwnd: 99 if hwnd == 4242 else None,
            process_image_path=lambda pid: r"C:\Program Files\Editor\editor.exe" if pid == 99 else None,
        )

        identity = observer.active_window()

        self.assertEqual(WindowIdentity(window_id=4242, pid=99, window_class="editor.exe"), identity)

    def test_no_pid_leaves_window_class_unread_rather_than_guessed(self) -> None:
        """A process this observer cannot open (another user's session, or one
        that exited between the two calls) must not fabricate a name for it."""
        observer = WindowsFocusObserver(
            foreground_window=lambda: 7,
            window_pid=lambda hwnd: None,
            process_image_path=lambda pid: self.fail("must not be called without a pid"),
        )

        identity = observer.active_window()

        self.assertEqual(WindowIdentity(window_id=7, pid=None, window_class=None), identity)

    def test_an_unreadable_image_path_still_returns_the_hwnd_and_pid(self) -> None:
        observer = WindowsFocusObserver(
            foreground_window=lambda: 7,
            window_pid=lambda hwnd: 55,
            process_image_path=lambda pid: None,
        )

        identity = observer.active_window()

        self.assertEqual(WindowIdentity(window_id=7, pid=55, window_class=None), identity)

    def test_a_recycled_window_id_with_a_different_pid_does_not_match(self) -> None:
        """`WindowIdentity.matches` is what `focus.should_deliver` relies on to
        refuse a paste when focus moved -- proved here for a Windows identity
        the same way `test_focus.py` proves it for an X11 one."""
        first = WindowsFocusObserver(
            foreground_window=lambda: 1,
            window_pid=lambda hwnd: 10,
            process_image_path=lambda pid: r"C:\editor.exe",
        ).active_window()
        second = WindowsFocusObserver(
            foreground_window=lambda: 1,
            window_pid=lambda hwnd: 20,
            process_image_path=lambda pid: r"C:\browser.exe",
        ).active_window()

        self.assertFalse(first.matches(second))


class WindowsFocusRuntimeIntegrationTests(unittest.TestCase):
    """Task 9.4, against the real `GetForegroundWindow`,
    `GetWindowThreadProcessId` and `QueryFullProcessImageName`: everything
    above this class proves `WindowsFocusObserver`'s assembly logic against
    fakes. This follows the `WindowsPipeSecurityDescriptorIntegrationTests`
    (`test_platform.py`) pattern of skipping in `setUp` on every platform but
    the one with the mechanism.

    Split into two tests rather than one bifurcated assertion, so the two
    real questions this class can answer land as two independent, readable
    outcomes in the Windows job's own skip list (`tests.yml`'s "List what
    this platform skipped" step) rather than being folded into one test whose
    pass means one of two different things depending on the runner:

    - whether the three real calls compose into a `WindowIdentity` or `None`
      without raising, on *any* runner (always runs), and
    - whether a runner actually has an interactive desktop to report a
      foreground window for at all -- very likely absent on a CI runner with
      no logged-on session, per this task's own text -- which the second test
      below answers by skipping, naming the absence, rather than asserting a
      window that may not exist.
    """

    def setUp(self) -> None:
        if sys.platform != "win32":
            self.skipTest("A Windows kernel is required to call GetForegroundWindow")

    def test_the_real_calls_compose_without_raising(self) -> None:
        """Proves task 9.4's assembly runs end to end against real Win32,
        whatever this runner's answer to "is there a foreground window" is:
        `active_window()` either returns `None` (no window) or a
        `WindowIdentity`, and never raises either way -- true on a runner
        with a desktop and on one without."""
        observer = WindowsFocusObserver()

        identity = observer.active_window()

        self.assertTrue(identity is None or isinstance(identity, WindowIdentity))

    def test_a_real_desktop_reports_a_well_formed_foreground_window(self) -> None:
        """The half task 9.4 asks a real desktop to confirm: a real
        `GetForegroundWindow` answer assembled into a `WindowIdentity` whose
        `window_id` and `pid` are genuine, positive values and whose
        `window_class` -- the owning executable's basename, per this module's
        docstring -- is non-empty. Skips, naming the reason, on a runner with
        no interactive desktop rather than asserting a window that cannot
        exist there; whether it skips here is itself 9.4's open question for
        this runner."""
        observer = WindowsFocusObserver()

        identity = observer.active_window()

        if identity is None:
            self.skipTest(
                "GetForegroundWindow returned no window on this runner -- "
                "very likely no interactive desktop is attached to this session."
            )

        self.assertGreater(identity.window_id, 0)
        self.assertIsNotNone(identity.pid)
        self.assertGreater(identity.pid, 0)
        self.assertTrue(identity.window_class, "the owning executable's basename was empty")


if __name__ == "__main__":
    unittest.main()
