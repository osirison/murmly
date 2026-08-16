from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import unittest

from murmly.overlay_renderer import (
    MAX_MESSAGE_BYTES,
    SUPPORTED_BACKENDS,
    MessageParser,
    MonitorGeometry,
    RendererViewState,
    select_monitor_index,
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

    def test_runtime_integration_skips_without_supported_plasma_session(self) -> None:
        backend = os.environ.get("XDG_SESSION_TYPE", "").casefold()
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").casefold()
        if backend not in SUPPORTED_BACKENDS or "kde" not in desktop:
            self.skipTest("GTK4 overlay runtime on KDE Plasma is unavailable")

        renderer_path = Path(sys.modules["murmly.overlay_renderer"].__file__).resolve()
        try:
            check = subprocess.run(
                ["/usr/bin/python3", str(renderer_path), "--check", "--backend", backend],
                capture_output=True,
                text=True,
                check=False,
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