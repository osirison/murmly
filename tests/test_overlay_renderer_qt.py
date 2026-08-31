from __future__ import annotations

import argparse
import ast
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

from murmly.overlay import OverlayBackend, OverlayController
from murmly.overlay_renderer_qt import (
    NATIVE_EXSTYLE_BITS,
    WS_EX_LAYERED,
    WS_EX_NOACTIVATE,
    WS_EX_TOOLWINDOW,
    WS_EX_TOPMOST,
    WS_EX_TRANSPARENT,
    MacosWindowProperties,
    OverlayApplication,
    apply_and_verify_exstyle,
    build_parser,
    check_visual_runtime,
    main,
    missing_property_for_exstyle,
    missing_property_for_macos_window,
)
from murmly.overlay_shared import MonitorGeometry, RendererViewState, RendererVisualState


class MissingPropertyTests(unittest.TestCase):
    """Task 10.5, in its most literal form: given a resulting `GWL_EXSTYLE`
    value, which spec-required property is missing, if any."""

    def test_every_property_missing(self) -> None:
        self.assertEqual("staying above ordinary application windows", missing_property_for_exstyle(0))

    def test_only_the_focus_property_missing(self) -> None:
        exstyle = WS_EX_TOPMOST | WS_EX_TRANSPARENT
        self.assertEqual("not taking keyboard focus", missing_property_for_exstyle(exstyle))

    def test_only_the_pointer_property_missing(self) -> None:
        exstyle = WS_EX_TOPMOST | WS_EX_NOACTIVATE
        self.assertEqual("not intercepting pointer input", missing_property_for_exstyle(exstyle))

    def test_every_required_property_present_reports_nothing_missing(self) -> None:
        exstyle = WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_TOOLWINDOW
        self.assertIsNone(missing_property_for_exstyle(exstyle))

    def test_extra_bits_do_not_hide_a_missing_property(self) -> None:
        # Some other application-specific bit set, but none of the three this
        # module cares about -- must still report the first one missing, not
        # be fooled by an unrelated bit being on.
        exstyle = 0x1
        self.assertEqual("staying above ordinary application windows", missing_property_for_exstyle(exstyle))


class MissingPropertyForMacosWindowTests(unittest.TestCase):
    """Task 15.1/15.3, in its most literal form -- the macOS twin of
    `MissingPropertyTests` above: given the three `NSWindow` readings a real
    `objc_msgSend` round-trip would produce, which spec-required property is
    missing, if any. Pure, and exercised on every platform CI runs on, unlike
    the real read-back itself."""

    def test_every_property_missing(self) -> None:
        properties = MacosWindowProperties(level=0, ignores_mouse_events=False, can_become_key_window=True)
        self.assertEqual("staying above ordinary application windows", missing_property_for_macos_window(properties))

    def test_only_the_focus_property_missing(self) -> None:
        properties = MacosWindowProperties(level=3, ignores_mouse_events=True, can_become_key_window=True)
        self.assertEqual("not taking keyboard focus", missing_property_for_macos_window(properties))

    def test_only_the_pointer_property_missing(self) -> None:
        properties = MacosWindowProperties(level=3, ignores_mouse_events=False, can_become_key_window=False)
        self.assertEqual("not intercepting pointer input", missing_property_for_macos_window(properties))

    def test_every_required_property_present_reports_nothing_missing(self) -> None:
        properties = MacosWindowProperties(level=3, ignores_mouse_events=True, can_become_key_window=False)
        self.assertIsNone(missing_property_for_macos_window(properties))


class ApplyAndVerifyExstyleTests(unittest.TestCase):
    """Task 10.3/10.5 without a real `HWND`: `SetWindowLongPtr`'s return value
    cannot be trusted (0 means both "failed" and "was already 0"), so this
    always reads back through the injected `get_window_long` -- exactly the
    seam a fake stands in for here."""

    def test_the_requested_bits_are_ored_onto_whatever_was_already_there(self) -> None:
        calls: list[int] = []

        def get(hwnd: int) -> int:
            del hwnd
            return WS_EX_TOPMOST if not calls else (WS_EX_TOPMOST | NATIVE_EXSTYLE_BITS)

        def set_(hwnd: int, exstyle: int) -> int:
            del hwnd
            calls.append(exstyle)
            return exstyle

        missing = apply_and_verify_exstyle(12345, get_window_long=get, set_window_long=set_)

        self.assertIsNone(missing)
        self.assertEqual([WS_EX_TOPMOST | NATIVE_EXSTYLE_BITS], calls)

    def test_a_platform_that_silently_refuses_is_reported_by_what_it_actually_ended_up_with(self) -> None:
        # `set_window_long` is called (task 10.3 still tries), but the
        # platform's real state -- what `get_window_long` reports afterwards
        # -- never gained WS_EX_NOACTIVATE. This is exactly the case a naive
        # "assume SetWindowLongPtr worked" implementation cannot detect.
        final_exstyle = WS_EX_TOPMOST | WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_TOOLWINDOW

        missing = apply_and_verify_exstyle(
            1,
            get_window_long=lambda hwnd: final_exstyle,
            set_window_long=lambda hwnd, exstyle: 0,
        )

        self.assertEqual("not taking keyboard focus", missing)


class NoTopLevelPySide6ImportTests(unittest.TestCase):
    """Task 10.2/18.14: this module must import cleanly with no `PySide6`
    installed at all, which is only true if nothing at module scope names
    it -- the same guard `test_overlay_renderer.py` holds `gi`/`cairo` to."""

    def test_pyside6_is_never_imported_at_module_scope(self) -> None:
        source = Path(sys.modules["murmly.overlay_renderer_qt"].__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        module_scope = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
        imported = set()
        for node in module_scope:
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif node.module:
                imported.add(node.module.split(".")[0])

        self.assertNotIn("PySide6", imported)

    def test_the_module_is_already_imported_without_pyside6_installed(self) -> None:
        # If the module-scope import guard above ever regressed, this
        # test file's own collection would already have failed with an
        # ImportError before reaching a single test method -- this assertion
        # just makes that property explicit and named.
        self.assertIn("murmly.overlay_renderer_qt", sys.modules)


class CheckVisualRuntimeTests(unittest.TestCase):
    def test_an_unsupported_backend_is_named_without_needing_pyside6(self) -> None:
        result = check_visual_runtime("wayland")

        self.assertFalse(result["available"])
        self.assertIn("wayland", result["error"])


class MainArgumentValidationTests(unittest.TestCase):
    """These reject before ever touching PySide6 or a socket, mirroring
    `overlay_renderer.py`'s own `main()` validation order."""

    def test_an_explicit_unsupported_backend_is_refused_by_argparse_itself(self) -> None:
        # `choices=` on `--backend` means argparse itself refuses this before
        # `main()`'s own body ever runs, exactly as it does for
        # `overlay_renderer.py`'s `--backend`.
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as context:
            main(["--fd", "3", "--backend", "linux-only"])
        self.assertEqual(2, context.exception.code)

    def test_an_omitted_backend_is_refused_by_mains_own_check(self) -> None:
        # `--backend` has no default fallback the way `overlay_renderer.py`'s
        # does from `XDG_SESSION_TYPE` -- Windows has no environment
        # equivalent worth guessing from -- so leaving it out is what reaches
        # `main()`'s own `SUPPORTED_BACKENDS` check instead of argparse's.
        errors = StringIO()
        with redirect_stderr(errors):
            status = main(["--fd", "3"])
        self.assertEqual(2, status)
        self.assertIn("--backend", errors.getvalue())

    def test_bottom_margin_out_of_range_is_refused(self) -> None:
        errors = StringIO()
        with redirect_stderr(errors):
            status = main(["--fd", "3", "--backend", "windows", "--bottom-margin-px", "9999"])
        self.assertEqual(2, status)
        self.assertIn("bottom-margin-px", errors.getvalue())

    def test_text_size_out_of_range_is_refused(self) -> None:
        errors = StringIO()
        with redirect_stderr(errors):
            status = main(["--fd", "3", "--backend", "windows", "--text-size-px", "1"])
        self.assertEqual(2, status)
        self.assertIn("text-size-px", errors.getvalue())

    def test_neither_fd_form_is_refused_before_touching_pyside6(self) -> None:
        errors = StringIO()
        with redirect_stderr(errors):
            status = main(["--backend", "windows"])
        self.assertEqual(2, status)
        self.assertIn("--fd", errors.getvalue())


class SocketFromArgumentsTests(unittest.TestCase):
    def test_fd_share_stdin_on_a_non_windows_interpreter_is_reported_by_name(self) -> None:
        """`socket.fromshare` does not exist off Windows at all; this proves
        that failure surfaces as a named, catchable `OSError` -- exactly what
        `main()`'s own exception handling expects -- rather than an
        `AttributeError` main() does not catch."""
        args = argparse.Namespace(fd=None, fd_share_stdin=True)
        with patch("sys.stdin") as fake_stdin:
            fake_stdin.buffer.read.return_value = b"whatever"
            from murmly.overlay_renderer_qt import _socket_from_arguments

            if hasattr(socket, "fromshare"):
                self.skipTest("socket.fromshare exists on this interpreter (Windows).")
            with self.assertRaises(OSError) as context:
                _socket_from_arguments(args)
        self.assertIn("fromshare", str(context.exception))

    def test_fd_builds_an_ordinary_socket(self) -> None:
        from murmly.overlay_renderer_qt import _socket_from_arguments

        parent, child = socket.socketpair()
        parent_fd = parent.fileno()
        parent.detach()  # `built` below becomes the sole owner of this fd.
        try:
            args = argparse.Namespace(fd=parent_fd, fd_share_stdin=False)
            built = _socket_from_arguments(args)
            self.assertEqual(parent_fd, built.fileno())
        finally:
            built.close()
            child.close()


class RefusalWithoutPresentingTests(unittest.TestCase):
    """Task 10.5's scenario, exercised the same way
    `test_an_unplaceable_overlay_is_reported_instead_of_presented` exercises
    it for the GTK4 renderer: a construction failure is reported on stderr
    and `main()` returns non-zero, without a window ever having been shown."""

    def test_a_construction_failure_is_reported_instead_of_presented(self) -> None:
        reason = "PySide6 is not installed."

        def refuse(*arguments: object, **keywords: object) -> None:
            raise ImportError(reason)

        parent, child = socket.socketpair()
        # `main()` builds its own `socket.socket(fileno=...)` wrapper around
        # this same fd number (`_socket_from_arguments`); detaching `child`'s
        # Python-level ownership first avoids two objects each thinking they
        # own -- and closing -- the one underlying fd.
        child_fd = child.fileno()
        child.detach()
        try:
            errors = StringIO()
            with patch("murmly.overlay_renderer_qt.OverlayApplication", refuse), redirect_stderr(errors):
                status = main(["--fd", str(child_fd), "--backend", "windows"])

            self.assertEqual(1, status)
            self.assertIn(reason, errors.getvalue())
        finally:
            parent.close()
            import os as _os

            try:
                _os.close(child_fd)
            except OSError:
                pass

    def test_a_missing_property_is_reported_by_name_and_the_window_is_hidden(self) -> None:
        """Task 10.5's actual join point: `apply_and_verify_exstyle` read back
        a real style and found one property missing (no exception involved --
        distinct from the construction-failure path above, which never
        reaches `_verify_and_show` at all). No PySide6 object is needed:
        `_verify_and_show`/`_fail_visual` only call `winId()`, `hide()`, and
        `quit()` on whatever they are handed, so fakes stand in for the real
        `QWidget`/`QApplication`."""

        class FakeWindow:
            def __init__(self) -> None:
                self.hidden = False

            def winId(self) -> int:
                return 12345

            def hide(self) -> None:
                self.hidden = True

        class FakeApplication:
            def __init__(self) -> None:
                self.quit_called = False

            def quit(self) -> None:
                self.quit_called = True

        window = FakeWindow()
        application = OverlayApplication.__new__(OverlayApplication)
        application._backend = "windows"
        application._window = window
        application._panel_window = None
        application._application = FakeApplication()

        errors = StringIO()
        with (
            patch(
                "murmly.overlay_renderer_qt.apply_and_verify_exstyle",
                return_value="not taking keyboard focus",
            ),
            redirect_stderr(errors),
        ):
            application._verify_and_show(window)

        self.assertTrue(window.hidden)
        self.assertTrue(application._application.quit_called)
        self.assertIn("not taking keyboard focus", errors.getvalue())
        self.assertIn("Windows overlay", errors.getvalue())

    def test_a_missing_property_is_reported_on_macos_through_the_native_readback(self) -> None:
        """The macOS twin of the test above: `_verify_and_show` dispatches on
        `self._backend` rather than assuming Windows, and macOS's own
        verification (`missing_property_for_macos_window`) is what gets
        patched here instead of `apply_and_verify_exstyle` (task 15.1/15.3).
        """

        class FakeWindow:
            def __init__(self) -> None:
                self.hidden = False

            def winId(self) -> int:
                return 67890

            def hide(self) -> None:
                self.hidden = True

        class FakeApplication:
            def __init__(self) -> None:
                self.quit_called = False

            def quit(self) -> None:
                self.quit_called = True

        window = FakeWindow()
        application = OverlayApplication.__new__(OverlayApplication)
        application._backend = "macos"
        application._window = window
        application._panel_window = None
        application._application = FakeApplication()

        errors = StringIO()
        with (
            patch(
                "murmly.overlay_renderer_qt._real_macos_window_properties",
                return_value=MacosWindowProperties(
                    level=3, ignores_mouse_events=True, can_become_key_window=True
                ),
            ),
            redirect_stderr(errors),
        ):
            application._verify_and_show(window)

        self.assertTrue(window.hidden)
        self.assertTrue(application._application.quit_called)
        self.assertIn("not taking keyboard focus", errors.getvalue())
        self.assertIn("macOS overlay", errors.getvalue())

    def test_a_window_with_every_macos_property_is_left_showing(self) -> None:
        """The other half of the branch: nothing is hidden when the real
        `NSWindow` read-back reports every spec-required property present."""

        class FakeWindow:
            def __init__(self) -> None:
                self.hidden = False

            def winId(self) -> int:
                return 13579

            def hide(self) -> None:
                self.hidden = True

        application = OverlayApplication.__new__(OverlayApplication)
        application._backend = "macos"
        application._window = FakeWindow()
        application._panel_window = None
        application._application = Mock()

        with patch(
            "murmly.overlay_renderer_qt._real_macos_window_properties",
            return_value=MacosWindowProperties(
                level=3, ignores_mouse_events=True, can_become_key_window=False
            ),
        ):
            application._verify_and_show(application._window)

        self.assertFalse(application._window.hidden)
        application._application.quit.assert_not_called()


class ErrorRevertTests(unittest.TestCase):
    """Task 10.4: the two renderers share `overlay_shared.RendererViewState`
    so they cannot disagree on *what* ERROR is, but nothing stopped them
    disagreeing on *when it ends*. The GTK4 renderer schedules a revert to
    IDLE off `message["duration_ms"]`, guarded by `error_generation` so a
    stale timer from a superseded error is a no-op
    (`overlay_renderer.OverlayApplication._hide_error`,
    `test_overlay_renderer.py`'s `RendererViewStateTests`). The Qt renderer
    painted ERROR but never scheduled anything to leave it -- on a real
    Windows run the error glyph would stay up forever. This exercises
    `_handle_message`/`_hide_error` the same way
    `RefusalWithoutPresentingTests` exercises `_verify_and_show`: through
    `OverlayApplication.__new__` with fake collaborators, no PySide6
    needed."""

    @staticmethod
    def _make_application() -> tuple[OverlayApplication, list[tuple[int, object]]]:
        class FakeTimer:
            def __init__(self, scheduled: list[tuple[int, object]]) -> None:
                self._scheduled = scheduled

            def singleShot(self, milliseconds: int, callback: object) -> None:
                self._scheduled.append((milliseconds, callback))

        class FakeWindow:
            def __init__(self) -> None:
                self._visible = True

            def isVisible(self) -> bool:
                return self._visible

            def show(self) -> None:
                self._visible = True

            def hide(self) -> None:
                self._visible = False

            def update(self) -> None:
                pass

        scheduled: list[tuple[int, object]] = []
        application = OverlayApplication.__new__(OverlayApplication)
        application._view = RendererViewState(state=RendererVisualState.LISTENING)
        application._window = FakeWindow()
        application._panel_window = None
        application._selected_monitor = MonitorGeometry(
            connector="fake", x=0, y=0, width=1920, height=1080
        )
        application._reduced_motion = False
        application._last_reduced_level_at = float("-inf")
        application._QTimer = FakeTimer(scheduled)
        return application, scheduled

    def test_an_error_schedules_a_revert_to_idle_after_its_duration(self) -> None:
        application, scheduled = self._make_application()

        application._handle_message({"type": "error", "duration_ms": 2_000})

        self.assertEqual(RendererVisualState.ERROR, application._view.state)
        self.assertEqual(1, len(scheduled))
        milliseconds, callback = scheduled[0]
        self.assertEqual(2_000, milliseconds)

        callback()

        self.assertEqual(RendererVisualState.IDLE, application._view.state)
        self.assertFalse(application._window.isVisible())

    def test_a_stale_revert_does_not_clobber_a_newer_error(self) -> None:
        application, scheduled = self._make_application()

        application._handle_message({"type": "error", "duration_ms": 2_000})
        _, first_callback = scheduled[0]
        application._handle_message({"type": "error", "duration_ms": 5_000})

        first_callback()

        self.assertEqual(RendererVisualState.ERROR, application._view.state)
        self.assertTrue(application._window.isVisible())


class BuildParserTests(unittest.TestCase):
    def test_backend_choices_are_windows_and_macos(self) -> None:
        """Task 15.1/15.2: this renderer is what macOS attempts first too."""
        parser = build_parser()
        backend_action = next(action for action in parser._actions if action.dest == "backend")
        self.assertEqual(["macos", "windows"], backend_action.choices)


class MacosWindowReadbackRuntimeIntegrationTests(unittest.TestCase):
    """Task 15.1's spike, as far as an automated run can take it, against a
    real `NSWindow` -- everything above this class proves the policy layer
    against fakes, since AppKit cannot be reached from Linux or Windows.
    Follows `MacosHotkeyRuntimeIntegrationTests`
    (`test_mac_hotkey.py`)'s pattern of skipping in `setUp` on every platform
    but the one with the mechanism, extended with a second skip for the
    capability `uv sync --locked` alone never installs (PySide6 is behind
    the `overlay` extra; see `pyproject.toml`'s own comment for why).

    What this proves: the real `objc_msgSend` round-trip against a real,
    Qt-created `NSWindow` returns well-typed values without raising, end to
    end through `winId()` -> `[nsview window]` -> the three selectors. What
    it deliberately does not assert is which way task 15.1's own question
    comes out -- whether Qt's flags actually grant all three spec-required
    properties on this runner is exactly the open question `design.md`
    records as unconfirmed, and a headless CI runner (this one may or may
    not be -- see this module's own docstring) cannot settle it either way:
    even where it can create a real window, "a synthetic click genuinely
    passes through the live window underneath this one" needs a person
    present to click, which is what `scripts/macos_overlay_spike.py` is for.
    So the actual reading is printed to the test log for a human to read,
    not asserted -- turning it into a hard pass/fail here would either mask
    a real "Qt is not enough" finding behind a green run, or fail a test for
    a fact about Qt's own implementation that no amount of fixing this
    codebase could change.
    """

    def setUp(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("A macOS window and libobjc are required for this")
        try:
            import PySide6  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed. Run `uv sync --extra overlay`.")

    def test_the_real_readback_round_trips_without_raising(self) -> None:
        from murmly.overlay_renderer_qt import _real_macos_window_properties
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication, QWidget

        application = QApplication.instance() or QApplication([sys.argv[0]])
        window = QWidget()
        window.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput
        )
        window.resize(1, 1)
        window.show()
        try:
            native_id = int(window.winId())
            self.assertNotEqual(0, native_id, "Qt did not realize a native NSView")
            properties = _real_macos_window_properties(native_id)
        finally:
            window.hide()
            del application

        self.assertIsInstance(properties.level, int)
        self.assertIsInstance(properties.ignores_mouse_events, bool)
        self.assertIsInstance(properties.can_become_key_window, bool)

        missing = missing_property_for_macos_window(properties)
        print(
            f"\ntask 15.1 spike reading on this runner: level={properties.level} "
            f"ignores_mouse_events={properties.ignores_mouse_events} "
            f"can_become_key_window={properties.can_become_key_window} "
            f"-> missing={missing!r}",
            file=sys.stderr,
        )


def _skip_without_pyside6(test: unittest.TestCase) -> None:
    if sys.platform != "win32":
        test.skipTest("A Windows kernel and window station are required for this")
    try:
        import PySide6  # noqa: F401
    except ModuleNotFoundError:
        test.skipTest("PySide6 is not installed. Run `uv sync --extra overlay`.")


class WindowsWindowReadbackRuntimeIntegrationTests(unittest.TestCase):
    """Task 10.3's spike, against a real `HWND` -- everything above this class
    proves the window-flags/verification policy against fakes, since
    `ctypes.WinDLL("user32")` cannot be reached from Linux or macOS.

    Follows `MacosWindowReadbackRuntimeIntegrationTests`'s pattern of
    skipping in `setUp` on every platform but the one with the mechanism,
    extended with the same second skip for the `overlay` extra `uv sync
    --locked` alone never installs (task 10.2's own comment: it is behind
    `uv sync --extra overlay`, and CI installs it on exactly one job -- see
    `.github/workflows/tests.yml`).

    Unlike the macOS twin, this asserts a hard pass/fail rather than only
    printing the reading for a human: task 15's own open question is whether
    a synthetic click genuinely passes through the live window underneath
    Qt's `NSWindow`, which needs a person present to click and which no CI
    run -- headless or not -- can show. `GetWindowLongPtr`'s four bits carry
    no such ambiguity; they are a local, synchronous, objectively-checkable
    property of the real `HWND` this test creates, needing nobody present to
    confirm. `WindowsHotkeyRuntimeIntegrationTests`
    (`test_win_hotkey.py`) already proved this project's Windows CI runner
    has a real, interactive window station -- `SendInput` delivered a
    `WM_HOTKEY` there -- so a missing property found here would be a real
    regression in `apply_and_verify_exstyle`/the window flags above it, not
    an artifact of a headless runner.
    """

    def setUp(self) -> None:
        _skip_without_pyside6(self)

    def test_the_real_readback_finds_every_required_property_present(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication, QWidget

        try:
            application = QApplication.instance() or QApplication([sys.argv[0]])
            window = QWidget()
            window.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.Tool
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.WindowDoesNotAcceptFocus
                | Qt.WindowType.WindowTransparentForInput
            )
            window.resize(1, 1)
            window.show()
            native_id = int(window.winId())
        except Exception as error:  # noqa: BLE001 - task 10.5's own degrade path
            self.skipTest(f"This runner could not realize a real HWND: {error}")

        try:
            self.assertNotEqual(0, native_id, "Qt did not realize a native HWND")

            # Task 10.3's first half: the documented, cross-platform Qt flags
            # were actually requested, not merely intended.
            flags = window.windowFlags()
            self.assertTrue(bool(flags & Qt.WindowType.WindowTransparentForInput))
            self.assertTrue(bool(flags & Qt.WindowType.WindowDoesNotAcceptFocus))

            # Task 10.3's second half, and task 10.5's actual join point: the
            # real `SetWindowLongPtr`/`GetWindowLongPtr` round trip, on the
            # real `HWND` Qt just created, finds every spec-required property
            # this codebase's own `apply_and_verify_exstyle` is trusted to
            # decide from a fake readback everywhere else in this file.
            missing = apply_and_verify_exstyle(native_id)
            self.assertIsNone(
                missing,
                f"A real Windows HWND did not end up with every spec-required "
                f"property; missing: {missing!r}",
            )
        finally:
            window.hide()
            del application


class WindowsQtOverlayRuntimeIntegrationTests(unittest.TestCase):
    """Task 10.1, against the real seams: a real PySide6 process, launched by
    the real `OverlayController._spawn_windows_renderer` -- the Windows-only
    `socket.share()`/`socket.fromshare()` handoff this module's own docstring
    records as unconfirmed -- speaking the real newline-delimited JSON
    protocol back over a real socket pair.

    `_spawn_windows_renderer` is exercised directly, rather than driving the
    whole `OverlayController` through `start()`/`publish_state()`/`close()`,
    for the same reason `test_overlay_renderer.py`'s own
    `test_runtime_integration_skips_without_supported_plasma_session` drives
    its renderer subprocess directly with raw `socket.sendall` rather than
    through `OverlayController`: `close()`'s shutdown path races its own
    bounded `thread.join(0.5)` against the child actually finishing `quit()`
    and exiting, and terminates the process outright if that race is lost --
    a real property of `OverlayController.close()`, not of this task, and not
    what task 10.1 is about. Calling the real spawn method directly still
    proves the one thing task 10.1 requires: the exact production code path
    that launches a Qt renderer on Windows -- the same `OverlayController`
    class, and the same private method, `_load_qt_overlay`/`platform/__init__.py`
    hands the daemon (see `test_platform.py`'s
    `test_overlay_selects_qt_on_windows` for the structural half of "the
    daemon does not learn which renderer it started": both the `qt` and
    `gtk4` candidates load the identical `OverlayController` class object).

    An exit code alone cannot prove presentation succeeded:
    `OverlayApplication._fail_visual` calls `self._application.quit()`,
    after which `run()`'s `self._application.exec()` returns and `main()`
    returns 0 -- a refused window exits exactly as cleanly as a window that
    shut down after presenting correctly. What actually discriminates the two
    is whether the process is *still running*, mid-`LISTENING`, well past the
    time a real refusal would have already quit it, and whether stderr ever
    printed the `"Error:"` line `_fail_visual` writes.
    """

    def setUp(self) -> None:
        _skip_without_pyside6(self)

    def test_a_real_renderer_process_speaks_the_protocol_and_presents_without_error(self) -> None:
        def popen_factory(command: list[str], **kwargs: object) -> subprocess.Popen:
            kwargs.setdefault("stderr", subprocess.PIPE)
            return subprocess.Popen(command, **kwargs)

        controller = OverlayController(
            bottom_margin_px=32,
            reduced_motion=True,
            backend=OverlayBackend.WINDOWS,
            popen_factory=popen_factory,
            autostart=False,
        )
        parent_transport, child_transport = socket.socketpair()
        process = controller._spawn_windows_renderer(child_transport)
        child_transport.close()

        try:
            parent_transport.sendall(b'{"type":"state","value":"LISTENING"}\n')
            parent_transport.sendall(b'{"type":"level","value":0.5}\n')

            # PySide6's cold import (tens of megabytes of `PySide6-Addons`)
            # is what this margin is for, not the socket round trip itself --
            # the message is already sitting in the kernel's socket buffer by
            # the time the renderer's own `QSocketNotifier` starts polling
            # it, the same reason `test_overlay_renderer.py`'s own runtime
            # test needs no readiness handshake either.
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline and process.poll() is None:
                time.sleep(0.2)
            self.assertIsNone(
                process.poll(),
                "The Qt renderer exited instead of presenting while LISTENING "
                "-- a real placement/verification refusal, or a crash.",
            )

            parent_transport.sendall(b'{"type":"state","value":"IDLE"}\n')
            parent_transport.sendall(b'{"type":"shutdown"}\n')
            returncode = process.wait(timeout=10.0)
            # `stdout` was never piped (`_spawn_windows_renderer` pipes only
            # `stdin`, to hand over the shared socket, and this test's own
            # `popen_factory` adds `stderr` alone) -- reading `stderr`
            # directly avoids `communicate()`'s own stdin bookkeeping against
            # a pipe `_spawn_windows_renderer` already wrote to and closed.
            stderr = process.stderr.read() if process.stderr is not None else b""
        finally:
            parent_transport.close()
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5.0)

        self.assertEqual(0, returncode)
        self.assertNotIn(b"Error:", stderr or b"")


if __name__ == "__main__":
    unittest.main()
