from __future__ import annotations

import argparse
import ast
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

from murmly.overlay_renderer_qt import (
    NATIVE_EXSTYLE_BITS,
    WS_EX_LAYERED,
    WS_EX_NOACTIVATE,
    WS_EX_TOOLWINDOW,
    WS_EX_TOPMOST,
    WS_EX_TRANSPARENT,
    OverlayApplication,
    apply_and_verify_exstyle,
    build_parser,
    check_visual_runtime,
    main,
    missing_property_for_exstyle,
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
        source = Path(sys.modules["murmly.overlay_renderer_qt"].__file__).read_text()
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
    def test_backend_choices_are_windows_only(self) -> None:
        parser = build_parser()
        backend_action = next(action for action in parser._actions if action.dest == "backend")
        self.assertEqual(["windows"], backend_action.choices)


if __name__ == "__main__":
    unittest.main()
