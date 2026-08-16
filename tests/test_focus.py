from __future__ import annotations

import os
import unittest

from murmly.focus import (
    NullFocusObserver,
    WindowIdentity,
    X11FocusObserver,
    create_focus_observer,
    should_deliver,
)


class StubObserver:
    def __init__(self, supported: bool, window: WindowIdentity | None, error: Exception | None = None) -> None:
        self._supported = supported
        self._window = window
        self._error = error

    @property
    def supported(self) -> bool:
        return self._supported

    @property
    def detail(self) -> str | None:
        return None

    def active_window(self) -> WindowIdentity | None:
        if self._error is not None:
            raise self._error
        return self._window


def _fake_x11(display: int = 1, atom: int = 0) -> object:
    """A libX11 stand-in that classifies without ever reaching XGetWindowProperty."""

    class FakeX11:
        pass

    library = FakeX11()
    library.XOpenDisplay = lambda _name: display
    library.XCloseDisplay = lambda _display: 0
    library.XDefaultRootWindow = lambda _display: 0
    library.XInternAtom = lambda _display, _name, _only_if_exists: atom
    library.XGetWindowProperty = lambda *_args: 1
    library.XFree = lambda _data: 0
    library.XSetErrorHandler = lambda _handler: None
    return library


class SessionClassificationTests(unittest.TestCase):
    def test_wayland_session_is_unverified_without_touching_x11(self) -> None:
        observer = create_focus_observer(
            env={"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0"},
            x11_loader=lambda: self.fail("X11 must not be loaded on a Wayland session"),
        )

        self.assertFalse(observer.supported)
        self.assertIsNone(observer.active_window())
        self.assertIn("X11 session", observer.detail)

    def test_missing_x11_library_is_unverified(self) -> None:
        def missing() -> object:
            raise OSError("libX11 is required to verify the transcript delivery target.")

        observer = create_focus_observer(env={"XDG_SESSION_TYPE": "x11"}, x11_loader=missing)

        self.assertFalse(observer.supported)
        self.assertIn("libX11", observer.detail)

    def test_unopenable_display_is_unverified(self) -> None:
        observer = create_focus_observer(
            env={"XDG_SESSION_TYPE": "x11"},
            x11_loader=lambda: _fake_x11(display=0),
        )

        self.assertFalse(observer.supported)
        self.assertIsNone(observer.active_window())

    def test_window_manager_without_net_active_window_is_unverified(self) -> None:
        observer = create_focus_observer(
            env={"XDG_SESSION_TYPE": "x11"},
            x11_loader=lambda: _fake_x11(display=1, atom=0),
        )

        self.assertFalse(observer.supported)
        self.assertIn("_NET_ACTIVE_WINDOW", observer.detail)


class DeliveryDecisionTests(unittest.TestCase):
    TARGET = WindowIdentity(window_id=42, pid=100, window_class="editor")

    def test_unchanged_target_delivers(self) -> None:
        observer = StubObserver(True, self.TARGET)

        self.assertEqual((True, None), should_deliver(observer, self.TARGET, True))

    def test_changed_target_refuses(self) -> None:
        observer = StubObserver(True, WindowIdentity(window_id=99, pid=200, window_class="browser"))

        allowed, reason = should_deliver(observer, self.TARGET, True)

        self.assertFalse(allowed)
        self.assertIn("focus moved", reason)

    def test_recycled_window_id_with_different_process_refuses(self) -> None:
        recycled = WindowIdentity(window_id=42, pid=777, window_class="editor")
        observer = StubObserver(True, recycled)

        allowed, _reason = should_deliver(observer, self.TARGET, True)

        self.assertFalse(allowed)

    def test_recycled_window_id_with_different_class_refuses(self) -> None:
        recycled = WindowIdentity(window_id=42, pid=100, window_class="browser")

        self.assertFalse(self.TARGET.matches(recycled))

    def test_closed_target_refuses(self) -> None:
        observer = StubObserver(True, None)

        allowed, reason = should_deliver(observer, self.TARGET, True)

        self.assertFalse(allowed)
        self.assertIn("could not be read", reason)

    def test_read_failure_refuses_instead_of_raising(self) -> None:
        observer = StubObserver(True, None, error=OSError("display went away"))

        allowed, reason = should_deliver(observer, self.TARGET, True)

        self.assertFalse(allowed)
        self.assertIn("could not be read", reason)

    def test_unrecorded_target_refuses(self) -> None:
        observer = StubObserver(True, self.TARGET)

        allowed, reason = should_deliver(observer, None, True)

        self.assertFalse(allowed)
        self.assertIn("no delivery target", reason)

    def test_unverified_session_delivers_without_comparison(self) -> None:
        observer = NullFocusObserver("unsupported")

        self.assertEqual((True, None), should_deliver(observer, None, True))

    def test_disabled_verification_delivers_despite_focus_change(self) -> None:
        observer = StubObserver(True, WindowIdentity(window_id=99, pid=200, window_class="browser"))

        self.assertEqual((True, None), should_deliver(observer, self.TARGET, False))

    def test_identity_requires_every_paired_property(self) -> None:
        self.assertFalse(self.TARGET.matches(None))
        self.assertFalse(self.TARGET.matches(WindowIdentity(window_id=42, pid=None, window_class="editor")))
        self.assertTrue(self.TARGET.matches(WindowIdentity(window_id=42, pid=100, window_class="editor")))


class X11RuntimeIntegrationTests(unittest.TestCase):
    def test_live_x11_session_reports_a_stable_active_window(self) -> None:
        if os.environ.get("XDG_SESSION_TYPE", "").casefold() != "x11" or not os.environ.get("DISPLAY"):
            self.skipTest("An X11 session is required to read the active window")
        try:
            observer = X11FocusObserver()
        except OSError as error:
            self.skipTest(f"libX11 is unavailable: {error}")
        if not observer.probe():
            self.skipTest("The active window manager does not publish _NET_ACTIVE_WINDOW")

        first = observer.active_window()
        second = observer.active_window()

        self.assertIsNotNone(first)
        self.assertGreater(first.window_id, 0)
        self.assertTrue(first.matches(second))

    def test_live_x11_property_read_survives_a_closed_window(self) -> None:
        if os.environ.get("XDG_SESSION_TYPE", "").casefold() != "x11" or not os.environ.get("DISPLAY"):
            self.skipTest("An X11 session is required to read the active window")
        try:
            observer = X11FocusObserver()
        except OSError as error:
            self.skipTest(f"libX11 is unavailable: {error}")

        display = observer._x11.XOpenDisplay(None)
        try:
            self.assertIsNone(observer._property(display, 0x1, "_NET_WM_PID"))
        finally:
            observer._x11.XCloseDisplay(display)
