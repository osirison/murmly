from __future__ import annotations

import json
from pathlib import Path
import threading
import time
import unittest

from murmly.overlay import (
    OverlayBackend,
    OverlayController,
    OverlayState,
    detect_overlay_backend,
    encode_overlay_message,
    renderer_environment,
)


class FakeSocket:
    def __init__(self, descriptor: int, block: threading.Event | None = None) -> None:
        self.descriptor = descriptor
        self.block = block
        self.entered = threading.Event()
        self.closed = False
        self.messages: list[bytes] = []
        self.timestamps: list[float] = []

    def fileno(self) -> int:
        return self.descriptor

    def sendall(self, message: bytes) -> None:
        self.entered.set()
        if self.block is not None:
            self.block.wait(timeout=2)
        if self.closed:
            raise BrokenPipeError("closed")
        self.messages.append(message)
        self.timestamps.append(time.monotonic())

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self) -> None:
        self.return_code: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True
        self.return_code = 0

    def wait(self, timeout: float) -> int:
        del timeout
        return self.return_code or 0

    def kill(self) -> None:
        self.return_code = -9


class OverlayTests(unittest.TestCase):
    def test_protocol_rejects_unknown_extra_and_out_of_range_values(self) -> None:
        valid = encode_overlay_message({"type": "level", "value": 0.5})
        self.assertEqual({"type": "level", "value": 0.5}, json.loads(valid))

        invalid_messages = [
            {"type": "unknown"},
            {"type": "shutdown", "extra": True},
            {"type": "level", "value": -0.1},
            {"type": "level", "value": float("nan")},
            {"type": "error", "duration_ms": 99},
            {"type": "state", "value": "ERROR"},
        ]
        for message in invalid_messages:
            with self.subTest(message=message), self.assertRaises(ValueError):
                encode_overlay_message(message)

    def test_renderer_environment_excludes_code_injection_variables(self) -> None:
        environment = renderer_environment(
            OverlayBackend.WAYLAND,
            {
                "HOME": "/home/user",
                "LANG": "en_US.UTF-8",
                "WAYLAND_DISPLAY": "wayland-0",
                "PYTHONPATH": "/tmp/injected",
                "PYTHONHOME": "/tmp/python",
                "LD_PRELOAD": "/tmp/injected.so",
                "GTK_PATH": "/tmp/gtk",
                "SECRET": "value",
            }
        )

        self.assertEqual("/home/user", environment["HOME"])
        self.assertEqual("wayland", environment["GDK_BACKEND"])
        self.assertEqual("1", environment["PYTHONNOUSERSITE"])
        self.assertNotIn("PYTHONPATH", environment)
        self.assertNotIn("PYTHONHOME", environment)
        self.assertNotIn("LD_PRELOAD", environment)
        self.assertNotIn("GTK_PATH", environment)
        self.assertNotIn("SECRET", environment)

        x11_environment = renderer_environment(
            OverlayBackend.X11,
            {
                "DISPLAY": ":0",
                "XAUTHORITY": "/run/user/1000/xauth",
                "WAYLAND_DISPLAY": "wayland-0",
            },
        )
        self.assertEqual(":0", x11_environment["DISPLAY"])
        self.assertEqual("/run/user/1000/xauth", x11_environment["XAUTHORITY"])
        self.assertNotIn("WAYLAND_DISPLAY", x11_environment)
        self.assertEqual("x11", x11_environment["GDK_BACKEND"])

    def test_backend_detection_requires_plasma_and_selects_display_protocol(self) -> None:
        self.assertEqual(
            OverlayBackend.X11,
            detect_overlay_backend(
                {"XDG_CURRENT_DESKTOP": "KDE", "XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}
            ),
        )
        self.assertEqual(
            OverlayBackend.WAYLAND,
            detect_overlay_backend(
                {
                    "XDG_CURRENT_DESKTOP": "KDE",
                    "XDG_SESSION_TYPE": "wayland",
                    "WAYLAND_DISPLAY": "wayland-0",
                }
            ),
        )
        self.assertIsNone(
            detect_overlay_backend(
                {"XDG_CURRENT_DESKTOP": "GNOME", "XDG_SESSION_TYPE": "wayland"}
            )
        )
        self.assertEqual(
            OverlayBackend.X11,
            detect_overlay_backend(
                {
                    "XDG_CURRENT_DESKTOP": "KDE",
                    "XDG_SESSION_TYPE": "x11",
                    "DISPLAY": ":0",
                    "WAYLAND_DISPLAY": "wayland-stale",
                }
            ),
        )
        self.assertIsNone(
            detect_overlay_backend(
                {"XDG_CURRENT_DESKTOP": "KDE", "XDG_SESSION_TYPE": "x11"}
            )
        )

    def test_controller_prioritizes_states_and_coalesces_levels(self) -> None:
        parent = FakeSocket(10)
        child = FakeSocket(11)
        process = FakeProcess()
        launches: list[tuple[list[str], dict[str, object]]] = []

        def popen(command: list[str], **kwargs: object) -> FakeProcess:
            launches.append((command, kwargs))
            return process

        controller = OverlayController(
            bottom_margin_px=48,
            reduced_motion=True,
            backend=OverlayBackend.WAYLAND,
            helper_path=Path("/tmp/renderer.py"),
            popen_factory=popen,
            socket_pair_factory=lambda: (parent, child),
            autostart=False,
        )
        controller.publish_level(0.1)
        controller.publish_level(0.2)
        controller.publish_level(0.3)
        controller.publish_state(OverlayState.LISTENING)
        controller.publish_state(OverlayState.THINKING)
        controller.start()
        self.addCleanup(controller.close)

        self._wait_for(lambda: len(parent.messages) >= 3)
        decoded = [json.loads(message) for message in parent.messages[:3]]

        self.assertEqual(["LISTENING", "THINKING"], [decoded[0]["value"], decoded[1]["value"]])
        self.assertEqual({"type": "level", "value": 0.3}, decoded[2])
        self.assertEqual("/usr/bin/python3", launches[0][0][0])
        self.assertEqual("/tmp/renderer.py", launches[0][0][1])
        self.assertIn("--reduced-motion", launches[0][0])
        self.assertIn("wayland", launches[0][0])
        self.assertFalse(launches[0][1]["shell"])
        self.assertEqual((11,), launches[0][1]["pass_fds"])
        self.assertTrue(controller.health.available)

    def test_backpressure_does_not_block_publishers(self) -> None:
        release = threading.Event()
        parent = FakeSocket(20, block=release)
        child = FakeSocket(21)
        controller = OverlayController(
            bottom_margin_px=32,
            reduced_motion=False,
            backend=OverlayBackend.WAYLAND,
            helper_path=Path("/tmp/renderer.py"),
            popen_factory=lambda *_args, **_kwargs: FakeProcess(),
            socket_pair_factory=lambda: (parent, child),
            autostart=False,
        )
        controller.publish_state(OverlayState.LISTENING)
        controller.start()
        self.assertTrue(parent.entered.wait(timeout=1))

        started_at = time.monotonic()
        for index in range(1_000):
            controller.publish_level((index % 100) / 100)
        elapsed = time.monotonic() - started_at
        release.set()
        controller.close()

        self.assertLess(elapsed, 0.1)

    def test_level_updates_are_capped_at_thirty_per_second(self) -> None:
        parent = FakeSocket(25)
        child = FakeSocket(26)
        controller = OverlayController(
            bottom_margin_px=32,
            reduced_motion=False,
            backend=OverlayBackend.X11,
            helper_path=Path("/tmp/renderer.py"),
            popen_factory=lambda *_args, **_kwargs: FakeProcess(),
            socket_pair_factory=lambda: (parent, child),
        )
        controller.publish_state(OverlayState.LISTENING)
        deadline = time.monotonic() + 0.2
        update = 0
        while time.monotonic() < deadline:
            controller.publish_level((update % 100) / 100)
            update += 1
        controller.publish_state(OverlayState.THINKING)
        self._wait_for(
            lambda: any(json.loads(message).get("value") == "THINKING" for message in parent.messages)
        )
        controller.close()

        level_timestamps = [
            timestamp
            for message, timestamp in zip(parent.messages, parent.timestamps, strict=True)
            if json.loads(message).get("type") == "level"
        ]
        self.assertGreaterEqual(len(level_timestamps), 2)
        for previous, current in zip(level_timestamps, level_timestamps[1:]):
            self.assertGreaterEqual(current - previous, (1.0 / 30.0) - 0.003)

    def test_controller_limits_restart_attempts_and_shuts_down(self) -> None:
        sockets: list[tuple[FakeSocket, FakeSocket]] = []
        processes: list[FakeProcess] = []

        def socket_pair() -> tuple[FakeSocket, FakeSocket]:
            pair = (FakeSocket(30 + len(sockets) * 2), FakeSocket(31 + len(sockets) * 2))
            sockets.append(pair)
            return pair

        def popen(*_args: object, **_kwargs: object) -> FakeProcess:
            process = FakeProcess()
            process.return_code = 1
            processes.append(process)
            return process

        controller = OverlayController(
            bottom_margin_px=32,
            reduced_motion=False,
            backend=OverlayBackend.WAYLAND,
            helper_path=Path("/tmp/renderer.py"),
            popen_factory=popen,
            socket_pair_factory=socket_pair,
            restart_delays=(0.0, 0.0, 0.0),
        )
        for _attempt in range(5):
            controller.publish_state(OverlayState.LISTENING)
            time.sleep(0.02)
        controller.close()

        self.assertEqual(3, len(processes))
        self.assertFalse(controller.health.available)

    def test_failed_listening_is_replayed_after_restart_delay(self) -> None:
        sockets: list[tuple[FakeSocket, FakeSocket]] = []
        processes: list[FakeProcess] = []

        def socket_pair() -> tuple[FakeSocket, FakeSocket]:
            pair = (FakeSocket(50 + len(sockets) * 2), FakeSocket(51 + len(sockets) * 2))
            sockets.append(pair)
            return pair

        def popen(*_args: object, **_kwargs: object) -> FakeProcess:
            process = FakeProcess()
            if not processes:
                process.return_code = 1
            processes.append(process)
            return process

        controller = OverlayController(
            bottom_margin_px=32,
            reduced_motion=False,
            backend=OverlayBackend.X11,
            helper_path=Path("/tmp/renderer.py"),
            popen_factory=popen,
            socket_pair_factory=socket_pair,
            restart_delays=(0.0, 0.05, 0.1),
        )
        started_at = time.monotonic()
        controller.publish_state(OverlayState.LISTENING)
        self._wait_for(lambda: len(processes) == 2 and bool(sockets[1][0].messages))
        controller.close()

        self.assertGreaterEqual(time.monotonic() - started_at, 0.045)
        self.assertEqual(
            {"type": "state", "value": "LISTENING"},
            json.loads(sockets[1][0].messages[0]),
        )

    def test_health_failure_is_logged_once_per_transition(self) -> None:
        controller = OverlayController(
            bottom_margin_px=32,
            reduced_motion=False,
            backend=OverlayBackend.X11,
            helper_path=Path("/tmp/renderer.py"),
            popen_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("failed")),
            socket_pair_factory=lambda: (FakeSocket(60), FakeSocket(61)),
            restart_delays=(0.0,),
            autostart=False,
        )

        with self.assertLogs("murmly.overlay", level="WARNING") as logs:
            controller.start()
            controller.publish_state(OverlayState.LISTENING)
            self._wait_for(lambda: "failed" in (controller.health.detail or ""))
            controller.publish_state(OverlayState.LISTENING)
            time.sleep(0.02)
            controller.close()

        matching = [message for message in logs.output if "Unable to launch overlay renderer" in message]
        self.assertEqual(1, len(matching))

    def test_clean_shutdown_sends_shutdown_message(self) -> None:
        parent = FakeSocket(40)
        child = FakeSocket(41)
        process = FakeProcess()
        controller = OverlayController(
            bottom_margin_px=32,
            reduced_motion=False,
            backend=OverlayBackend.WAYLAND,
            helper_path=Path("/tmp/renderer.py"),
            popen_factory=lambda *_args, **_kwargs: process,
            socket_pair_factory=lambda: (parent, child),
        )
        controller.close()

        self.assertIn({"type": "shutdown"}, [json.loads(message) for message in parent.messages])
        self.assertTrue(parent.closed)

    def _wait_for(self, condition: callable, timeout: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() >= deadline:
                self.fail("Timed out waiting for controller output")
            time.sleep(0.005)