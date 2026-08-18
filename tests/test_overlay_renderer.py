from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch

from murmly.overlay import detect_overlay_backend, renderer_environment
from murmly.overlay_renderer import (
    LAYER_SHELL_PRELOAD,
    MAX_MESSAGE_BYTES,
    MAX_PARTIAL_CHARS,
    PANEL_GAP_PX,
    PANEL_HORIZONTAL_PADDING,
    PANEL_VERTICAL_PADDING,
    SUPPORTED_BACKENDS,
    WINDOW_HEIGHT,
    WINDOW_WIDTH,
    MessageParser,
    MonitorGeometry,
    RendererViewState,
    layer_shell_unsupported_reason,
    main,
    panel_height,
    panel_max_width,
    panel_position,
    panel_width,
    select_monitor_index,
    truncate_to_width,
    x11_position,
)


class OverlayRendererTests(unittest.TestCase):
    def test_parser_accepts_fragmented_messages_and_recovers_from_invalid_input(self) -> None:
        parser = MessageParser()

        self.assertEqual([], parser.feed(b'{"type":"state","value":"LIST'))
        messages = parser.feed(
            b'ENING"}\nnot-json\n'
            + b"x" * MAX_MESSAGE_BYTES
            + b'\n{"type":"level","value":0.4}\n'
        )

        self.assertEqual(
            [
                {"type": "state", "value": "LISTENING"},
                {"type": "level", "value": 0.4},
            ],
            messages,
        )

    def test_parser_rejects_unknown_extra_and_out_of_range_messages(self) -> None:
        parser = MessageParser()
        payload = b"\n".join(
            json.dumps(message).encode("utf-8")
            for message in [
                {"type": "unknown"},
                {"type": "shutdown", "extra": True},
                {"type": "level", "value": 2},
                {"type": "error", "duration_ms": 50_000},
                {"type": "state", "value": "ERROR"},
            ]
        )

        self.assertEqual([], parser.feed(payload + b"\n"))

    def test_view_state_tracks_listening_processing_error_and_shutdown(self) -> None:
        view = RendererViewState()

        self.assertFalse(view.apply({"type": "state", "value": "LISTENING"}))
        view.apply({"type": "level", "value": 0.7})
        self.assertTrue(view.visible)
        self.assertEqual(0.7, view.level)
        view.apply({"type": "state", "value": "THINKING"})
        self.assertEqual(0.0, view.level)
        view.apply({"type": "error", "duration_ms": 2_000})
        self.assertEqual("ERROR", view.state)
        self.assertEqual(1, view.error_generation)
        self.assertTrue(view.apply({"type": "shutdown"}))

    def test_parser_accepts_partials_and_rejects_oversized_ones(self) -> None:
        parser = MessageParser()

        messages = parser.feed(json.dumps({"type": "partial", "value": "hello"}).encode() + b"\n")
        self.assertEqual([{"type": "partial", "value": "hello"}], messages)

        for invalid in (
            {"type": "partial", "value": 5},
            {"type": "partial"},
            {"type": "partial", "value": "x", "extra": 1},
            {"type": "partial", "value": "x" * (MAX_PARTIAL_CHARS + 1)},
        ):
            with self.subTest(invalid=invalid):
                self.assertEqual([], parser.feed(json.dumps(invalid).encode() + b"\n"))

    def test_partial_is_shown_while_listening_and_cleared_on_every_exit(self) -> None:
        for exit_message in (
            {"type": "state", "value": "THINKING"},
            {"type": "state", "value": "IDLE"},
            {"type": "error", "duration_ms": 2_000},
        ):
            with self.subTest(exit_message=exit_message):
                view = RendererViewState()
                view.apply({"type": "state", "value": "LISTENING"})
                view.apply({"type": "partial", "value": "half a sentence"})
                self.assertEqual("half a sentence", view.partial)

                view.apply(exit_message)

                self.assertEqual("", view.partial)

    def test_partial_arriving_outside_listening_is_ignored(self) -> None:
        view = RendererViewState()
        view.apply({"type": "state", "value": "THINKING"})

        view.apply({"type": "partial", "value": "leaked"})

        self.assertEqual("", view.partial)

    def test_monitor_selection_prefers_origin_then_connector(self) -> None:
        monitors = [
            MonitorGeometry("DP-2", 1920, 0, 1920, 1080),
            MonitorGeometry("eDP-1", 0, 0, 1920, 1080),
        ]
        self.assertEqual(1, select_monitor_index(monitors))

        fallback = [
            MonitorGeometry("DP-2", 100, 100, 100, 100),
            MonitorGeometry("DP-1", 300, 100, 100, 100),
        ]
        self.assertEqual(1, select_monitor_index(fallback))
        self.assertIsNone(select_monitor_index([]))

    def test_x11_position_centers_fixed_surface_above_bottom_margin(self) -> None:
        monitor = MonitorGeometry("eDP-1", -1920, 100, 1920, 1080, scale=2)

        self.assertEqual((-2076, 2200), x11_position(monitor, 32))

    def test_panel_width_grows_with_text_and_stops_at_the_display_fraction(self) -> None:
        monitor = MonitorGeometry(connector="DP-1", x=0, y=0, width=1_920, height=1_080)

        narrow = panel_width(60, monitor)
        wide = panel_width(800, monitor)
        overflowing = panel_width(100_000, monitor)

        self.assertEqual(WINDOW_WIDTH, narrow)
        self.assertEqual(800 + 2 * PANEL_HORIZONTAL_PADDING, wide)
        self.assertEqual(int(1_920 * 0.75), overflowing)
        self.assertEqual(panel_max_width(monitor), overflowing)
        self.assertLess(overflowing, monitor.width)

    def test_panel_is_never_narrower_than_the_recording_indicator(self) -> None:
        monitor = MonitorGeometry(connector="DP-1", x=0, y=0, width=200, height=200)

        self.assertGreaterEqual(panel_width(1, monitor), WINDOW_WIDTH)
        self.assertGreaterEqual(panel_max_width(monitor), WINDOW_WIDTH)

    def test_panel_height_follows_the_configured_text_size(self) -> None:
        self.assertEqual(13 + 2 * PANEL_VERTICAL_PADDING, panel_height(13))
        self.assertEqual(32 + 2 * PANEL_VERTICAL_PADDING, panel_height(32))

    def test_panel_sits_below_the_indicator_without_moving_it(self) -> None:
        monitor = MonitorGeometry(connector="DP-1", x=0, y=0, width=1_920, height=1_080)
        indicator_x, indicator_y = x11_position(monitor, 96)
        height = panel_height(13)

        panel_x, panel_y = panel_position(monitor, 96, 400, height)

        self.assertEqual((0 + (1_920 - 400) // 2), panel_x)
        self.assertEqual(1_080 - 96 + PANEL_GAP_PX, panel_y)
        self.assertGreater(panel_y, indicator_y + WINDOW_HEIGHT)
        self.assertEqual((1_920 - WINDOW_WIDTH) // 2, indicator_x)
        self.assertEqual(1_080 - WINDOW_HEIGHT - 96, indicator_y)

    def test_panel_moves_above_the_indicator_when_the_margin_cannot_hold_it(self) -> None:
        """It must never overlap the indicator, and never leave the display."""
        monitor = MonitorGeometry(connector="DP-1", x=0, y=0, width=1_920, height=1_080)
        height = panel_height(40)

        _x, y = panel_position(monitor, 0, 400, height)

        indicator_top = 1_080 - WINDOW_HEIGHT
        self.assertLessEqual(y + height, indicator_top)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(y + height, 1_080)

    def test_panel_sits_below_the_indicator_when_the_margin_has_room(self) -> None:
        monitor = MonitorGeometry(connector="DP-1", x=0, y=0, width=1_920, height=1_080)
        height = panel_height(13)

        _x, y = panel_position(monitor, 120, 400, height)

        self.assertGreaterEqual(y, 1_080 - 120)
        self.assertLessEqual(y + height, 1_080)

    def test_truncation_keeps_the_tail_behind_an_ellipsis(self) -> None:
        def measure(text: str) -> float:
            return len(text) * 10.0

        self.assertEqual("short", truncate_to_width("short", measure, 100))
        self.assertEqual("…world", truncate_to_width("hello world", measure, 60))
        self.assertEqual("", truncate_to_width("hello", measure, 0))

    def test_layer_shell_reason_separates_a_missing_preload_from_the_compositor(self) -> None:
        without_preload = layer_shell_unsupported_reason({})
        with_preload = layer_shell_unsupported_reason({"LD_PRELOAD": LAYER_SHELL_PRELOAD})

        self.assertIn(LAYER_SHELL_PRELOAD, without_preload)
        self.assertNotIn("compositor", without_preload)
        self.assertIn("compositor", with_preload)
        self.assertNotIn(LAYER_SHELL_PRELOAD, with_preload)

    def test_an_unplaceable_overlay_is_reported_instead_of_presented(self) -> None:
        reason = "The active Wayland compositor does not support Layer Shell."

        def refuse(*arguments: object, **keywords: object) -> None:
            raise OSError(reason)

        errors = StringIO()
        with patch("murmly.overlay_renderer.OverlayApplication", refuse), redirect_stderr(errors):
            status = main(["--fd", "3", "--backend", "wayland"])

        self.assertEqual(1, status)
        self.assertIn(reason, errors.getvalue())

    def test_runtime_integration_skips_without_supported_plasma_session(self) -> None:
        selected = detect_overlay_backend()
        if selected is None or selected.value not in SUPPORTED_BACKENDS:
            self.skipTest("GTK4 overlay runtime on KDE Plasma is unavailable")
        backend = selected.value
        # The renderer's own environment, not the test runner's: on Wayland the
        # difference is the layer-shell preload, without which this exercises a
        # renderer that refuses to start.
        environment = renderer_environment(selected)

        renderer_path = Path(sys.modules["murmly.overlay_renderer"].__file__).resolve()
        try:
            check = subprocess.run(
                ["/usr/bin/python3", str(renderer_path), "--check", "--backend", backend],
                capture_output=True,
                text=True,
                check=False,
                env=environment,
            )
        except OSError:
            self.skipTest("The system interpreter for the overlay renderer is unavailable")
        try:
            result = json.loads(check.stdout)
        except json.JSONDecodeError:
            self.skipTest(f"Overlay runtime check produced no report: {check.stderr.strip()}")
        if not result["available"]:
            self.skipTest("GTK4 overlay runtime on KDE Plasma is unavailable")

        parent, child = socket.socketpair()
        process = subprocess.Popen(
            [
                "/usr/bin/python3",
                str(renderer_path),
                "--fd",
                str(child.fileno()),
                "--bottom-margin-px",
                "32",
                "--backend",
                backend,
                "--reduced-motion",
            ],
            close_fds=True,
            pass_fds=(child.fileno(),),
            env=environment,
        )
        child.close()
        try:
            parent.sendall(b'{"type":"state","value":"LISTENING"}\n')
            parent.sendall(b'{"type":"level","value":0.5}\n')
            parent.sendall(b'{"type":"state","value":"THINKING"}\n')
            parent.sendall(b'{"type":"state","value":"IDLE"}\n')
            parent.sendall(b'{"type":"error","duration_ms":100}\n')
            parent.sendall(b'{"type":"shutdown"}\n')
            self.assertEqual(0, process.wait(timeout=5))
        finally:
            parent.close()
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=2)