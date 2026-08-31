from __future__ import annotations

import dataclasses
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

from murmly.overlay import (
    GTK4_RENDERER_PYTHON,
    GTK4_RENDERER_SCRIPT_NAME,
    MAX_MESSAGE_BYTES,
    MAX_PARTIAL_CHARS,
    NullOverlayController,
    OverlayBackend,
    OverlayController,
    OverlayState,
    QT_RENDERER_SCRIPT_NAME,
    WINDOWS_RENDERER_ENVIRONMENT_KEYS,
    bound_partial_text,
    detect_overlay_backend,
    encode_overlay_message,
    overlay_backend_for_profile,
    renderer_environment,
    renderer_python,
    renderer_script,
)
from murmly.platform import Desktop, OperatingSystem, PlatformProfile, resolve_platform


def _resolve_platform_pinned_to(operating_system: OperatingSystem):
    """A `murmly.overlay.resolve_platform` stand-in that answers for a given
    `env` exactly as the real resolver does -- same desktop/session_type/
    wayland_display/x11_display -- except with `operating_system` pinned to
    `operating_system` regardless of the host's real `sys.platform`.

    `detect_overlay_backend` always resolves against the real platform
    (`overlay_backend_for_profile`'s own docstring: `operating_system` "is
    not one of the keys a caller can steer through `detect_overlay_backend`'s
    `environment` parameter"), so a test that wants to assert one OS's
    behaviour on every CI host -- Linux behaviour when CI happens to run on
    Windows, or vice versa -- has to patch the resolver itself, the same
    shape `test_cli.py` uses for `murmly.cli.resolve_platform`.
    """

    def _resolve(env: dict[str, str] | None = None) -> PlatformProfile:
        return dataclasses.replace(resolve_platform(env), operating_system=operating_system)

    return _resolve


class RendererPythonTests(unittest.TestCase):
    """Task 3.3: the interpreter turns on the renderer, not a bare constant."""

    def test_every_backend_today_needs_the_gtk4_renderers_interpreter(self) -> None:
        # Both backends launch the same GTK4-based renderer; only the display
        # protocol underneath it differs, so both answer the same interpreter.
        self.assertEqual(GTK4_RENDERER_PYTHON, renderer_python(OverlayBackend.X11))
        self.assertEqual(GTK4_RENDERER_PYTHON, renderer_python(OverlayBackend.WAYLAND))

    def test_the_gtk4_interpreter_is_the_system_ones_not_the_projects_own(self) -> None:
        # PyGObject and GTK4 are distribution packages, not wheels: they exist
        # only inside the system interpreter, and `sys.executable` -- Murmly's
        # own, `uv`-managed -- has never had them.
        self.assertEqual(Path("/usr/bin/python3"), GTK4_RENDERER_PYTHON)

    def test_windows_needs_the_projects_own_interpreter(self) -> None:
        # Task 10: PySide6 is a wheel, so its renderer runs under the same
        # interpreter Murmly's own daemon is already running under, never the
        # system one the GTK4 renderer needs.
        self.assertEqual(Path(sys.executable), renderer_python(OverlayBackend.WINDOWS))


class RendererScriptTests(unittest.TestCase):
    """Task 10.1: `OverlayController` launches a different script per backend."""

    def test_gtk4_backends_launch_the_gtk4_script(self) -> None:
        self.assertEqual(GTK4_RENDERER_SCRIPT_NAME, renderer_script(OverlayBackend.X11).name)
        self.assertEqual(GTK4_RENDERER_SCRIPT_NAME, renderer_script(OverlayBackend.WAYLAND).name)

    def test_windows_launches_the_qt_script(self) -> None:
        self.assertEqual(QT_RENDERER_SCRIPT_NAME, renderer_script(OverlayBackend.WINDOWS).name)

    def test_both_scripts_live_beside_this_module(self) -> None:
        from murmly import overlay as overlay_module

        directory = Path(overlay_module.__file__).parent
        self.assertEqual(directory, renderer_script(OverlayBackend.X11).parent)
        self.assertEqual(directory, renderer_script(OverlayBackend.WINDOWS).parent)


class OverlayBackendForProfileTests(unittest.TestCase):
    """Task 10.1: Windows always gets the Qt backend, independent of the
    Linux-only desktop/session fields `detect_overlay_backend` otherwise
    gates on -- and independent of `env=`, which cannot steer
    `PlatformProfile.operating_system` (that comes from `sys.platform`), so
    the Windows branch is exercised by constructing a profile directly.
    """

    def test_windows_selects_the_windows_backend_regardless_of_desktop_fields(self) -> None:
        profile = PlatformProfile(operating_system=OperatingSystem.WINDOWS, architecture="x86_64")
        self.assertEqual(OverlayBackend.WINDOWS, overlay_backend_for_profile(profile))

        # Even a profile carrying Linux-shaped fields (which a real Windows
        # profile never would) still answers Windows: the OS check comes
        # first, before the desktop/session gate GTK4 uses.
        odd_profile = PlatformProfile(
            operating_system=OperatingSystem.WINDOWS,
            architecture="x86_64",
            desktop=Desktop.OTHER,
            session_type="x11",
        )
        self.assertEqual(OverlayBackend.WINDOWS, overlay_backend_for_profile(odd_profile))

    def test_macos_and_unsupported_platforms_have_no_backend(self) -> None:
        self.assertIsNone(overlay_backend_for_profile(PlatformProfile(operating_system=OperatingSystem.MACOS, architecture="arm64")))
        self.assertIsNone(overlay_backend_for_profile(PlatformProfile(operating_system=OperatingSystem.OTHER, architecture="x86_64")))


class RendererEnvironmentWindowsTests(unittest.TestCase):
    """Task 10: the Qt renderer's environment is built from Windows
    vocabulary, not the GTK4/XDG one -- and carries `SYSTEMROOT`, without
    which Winsock (and Qt through it) fails to initialize."""

    def test_only_windows_keys_pass_through(self) -> None:
        source = {
            "SYSTEMROOT": r"C:\Windows",
            "TEMP": r"C:\Users\person\AppData\Local\Temp",
            "USERPROFILE": r"C:\Users\person",
            "PATH": r"C:\Windows\System32",
            # Linux-only vocabulary the Qt renderer has no use for; must not
            # leak through, and must not gain the GTK-specific keys either.
            "WAYLAND_DISPLAY": "wayland-0",
            "DISPLAY": ":0",
            "XDG_RUNTIME_DIR": "/run/user/1000",
        }

        environment = renderer_environment(OverlayBackend.WINDOWS, source)

        self.assertEqual(set(WINDOWS_RENDERER_ENVIRONMENT_KEYS) & set(source), set(environment))
        self.assertNotIn("WAYLAND_DISPLAY", environment)
        self.assertNotIn("DISPLAY", environment)
        self.assertNotIn("XDG_RUNTIME_DIR", environment)
        self.assertNotIn("GDK_BACKEND", environment)
        self.assertNotIn("PYTHONNOUSERSITE", environment)
        self.assertEqual(r"C:\Windows", environment["SYSTEMROOT"])

    def test_missing_keys_are_simply_absent_not_empty(self) -> None:
        self.assertEqual({}, renderer_environment(OverlayBackend.WINDOWS, {}))


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


class FakeShareableSocket(FakeSocket):
    """`FakeSocket` plus `socket.socket.share`'s Windows-only shape."""

    def __init__(self, descriptor: int) -> None:
        super().__init__(descriptor)
        self.shared_for_pid: int | None = None

    def share(self, process_id: int) -> bytes:
        self.shared_for_pid = process_id
        return f"share-data-for-{process_id}".encode()


class FakeStdin:
    def __init__(self) -> None:
        self.written = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    def close(self) -> None:
        self.closed = True


class FakeWindowsProcess(FakeProcess):
    def __init__(self, pid: int) -> None:
        super().__init__()
        self.pid = pid
        self.stdin = FakeStdin()


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

    def test_partial_messages_are_validated_like_every_other_type(self) -> None:
        encoded = encode_overlay_message({"type": "partial", "value": "hello there"})
        self.assertEqual({"type": "partial", "value": "hello there"}, json.loads(encoded))

        for message in (
            {"type": "partial"},
            {"type": "partial", "value": 5},
            {"type": "partial", "value": None},
            {"type": "partial", "value": "text", "extra": True},
        ):
            with self.subTest(message=message), self.assertRaises(ValueError):
                encode_overlay_message(message)

    def test_partial_text_is_truncated_to_the_tail_at_encode_time(self) -> None:
        long_text = "".join(str(index % 10) for index in range(MAX_PARTIAL_CHARS * 3))

        encoded = encode_overlay_message({"type": "partial", "value": long_text})
        value = json.loads(encoded)["value"]

        self.assertEqual(MAX_PARTIAL_CHARS, len(value))
        self.assertTrue(long_text.endswith(value))
        self.assertLessEqual(len(encoded), MAX_MESSAGE_BYTES)

    def test_partial_text_never_exceeds_the_message_budget(self) -> None:
        for text in ("😀" * MAX_PARTIAL_CHARS, '"\\' * MAX_PARTIAL_CHARS, "\n" * 400):
            with self.subTest(text=text[:8]):
                encoded = encode_overlay_message({"type": "partial", "value": text})
                self.assertLessEqual(len(encoded), MAX_MESSAGE_BYTES)

    def test_partial_text_collapses_whitespace_to_stay_on_one_line(self) -> None:
        self.assertEqual("one two three", bound_partial_text("  one\ttwo\n\nthree  "))
        self.assertEqual("", bound_partial_text("   \n\t "))

    def test_empty_partial_is_a_valid_clear(self) -> None:
        encoded = encode_overlay_message({"type": "partial", "value": ""})

        self.assertEqual({"type": "partial", "value": ""}, json.loads(encoded))

    def test_null_controller_accepts_partials(self) -> None:
        controller = NullOverlayController()

        controller.publish_partial("anything at all")

        self.assertFalse(controller.health.available)

    def test_controller_queues_partials_in_order_with_state_changes(self) -> None:
        socket_holder: list[FakeSocket] = []

        def socket_pair_factory() -> tuple[object, object]:
            parent = FakeSocket(11)
            socket_holder.append(parent)
            return parent, FakeSocket(12)

        controller = OverlayController(
            bottom_margin_px=32,
            reduced_motion=False,
            backend=OverlayBackend.X11,
            popen_factory=lambda *_args, **_kwargs: FakeProcess(),
            socket_pair_factory=socket_pair_factory,
            restart_delays=(0.0,),
        )
        try:
            controller.publish_state(OverlayState.LISTENING)
            controller.publish_partial("first")
            controller.publish_partial("second")
            controller.publish_state(OverlayState.THINKING)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if socket_holder and len(socket_holder[0].messages) >= 4:
                    break
                time.sleep(0.01)
        finally:
            controller.close()

        sent = [json.loads(message) for message in socket_holder[0].messages]
        ordered = [
            (message["type"], message.get("value"))
            for message in sent
            if message["type"] in {"state", "partial"}
        ]

        # A superseded partial is dropped rather than replayed, but the survivor
        # stays between the state changes that bracket it.
        self.assertEqual(
            [
                ("state", "LISTENING"),
                ("partial", "second"),
                ("state", "THINKING"),
            ],
            ordered,
        )

    def test_a_partial_is_never_reordered_across_a_state_change(self) -> None:
        """Coalescing must not let a partial jump a transition.

        A partial emitted after the state that clears it would be shown for audio
        that is no longer being captured.
        """
        controller = OverlayController(
            bottom_margin_px=32,
            reduced_motion=False,
            backend=OverlayBackend.X11,
            popen_factory=lambda *_args, **_kwargs: FakeProcess(),
            socket_pair_factory=lambda: (FakeSocket(21), FakeSocket(22)),
            restart_delays=(0.0,),
            autostart=False,
        )
        controller.publish_state(OverlayState.LISTENING)
        controller.publish_partial("older")
        controller.publish_state(OverlayState.THINKING)
        controller.publish_partial("newer")

        queued = [json.loads(message) for message in controller._control_messages]
        ordered = [(message["type"], message.get("value")) for message in queued]

        self.assertEqual(
            [
                ("state", "LISTENING"),
                ("state", "THINKING"),
                ("partial", "newer"),
            ],
            ordered,
        )

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
        self.assertNotIn("LD_PRELOAD", x11_environment)

    def test_backend_detection_requires_plasma_and_selects_display_protocol(self) -> None:
        # Pinned to Linux: this asserts the Plasma/session-type gate
        # `overlay_backend_for_profile` applies below its `WINDOWS` check
        # (see that function's docstring), which is real on every host, not
        # only one where `sys.platform` happens to already say Linux.
        with patch(
            "murmly.overlay.resolve_platform",
            side_effect=_resolve_platform_pinned_to(OperatingSystem.LINUX),
        ):
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

    def test_backend_detection_on_windows_ignores_desktop_fields(self) -> None:
        # The Windows counterpart: `detect_overlay_backend` must answer
        # `WINDOWS` unconditionally there, the same Linux-shaped-fields-don't-
        # matter guarantee `OverlayBackendForProfileTests` already proves for
        # the pure `overlay_backend_for_profile`, but exercised through the
        # `environment`-driven wrapper `cli.overlay_diagnostics` actually calls.
        with patch(
            "murmly.overlay.resolve_platform",
            side_effect=_resolve_platform_pinned_to(OperatingSystem.WINDOWS),
        ):
            self.assertEqual(
                OverlayBackend.WINDOWS,
                detect_overlay_backend(
                    {"XDG_CURRENT_DESKTOP": "KDE", "XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}
                ),
            )
            self.assertEqual(OverlayBackend.WINDOWS, detect_overlay_backend({}))

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
        # `OverlayController` stores `helper_path.resolve()`, not the literal
        # value passed in: on macOS `/tmp` is itself a symlink to
        # `/private/tmp`, so the launched command names the resolved path
        # (`/private/tmp/renderer.py` there), not the string this test
        # constructed the controller with. Comparing against the same
        # `.resolve()` is a no-op on Linux, where `/tmp` is not a symlink.
        # `.as_posix()`, not `str(...)`: the command line itself is built with
        # `.as_posix()` (see `OverlayController.start`'s own comment), since
        # this renderer is launched only on Linux regardless of which host's
        # `pathlib` flavour runs the suite, and `str()` on a real Windows
        # host would render backslashes no launch of this ever produces.
        self.assertEqual(Path("/tmp/renderer.py").resolve().as_posix(), launches[0][0][1])
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

    def test_windows_backend_shares_the_socket_over_stdin_instead_of_pass_fds(self) -> None:
        """Task 10.1: the Windows launch seam. `pass_fds` is POSIX-only and a
        `socket.socketpair()` socket has no fd a Windows child could inherit
        that way regardless, so the child gets a `str()` command line, no
        `--fd`, and its half of the pair over a piped stdin instead --
        `OverlayController._spawn_windows_renderer`'s own docstring records
        what only a real Windows machine can still confirm about this."""
        parent = FakeSocket(10)
        child = FakeShareableSocket(11)
        process = FakeWindowsProcess(pid=4_242)
        launches: list[tuple[list[str], dict[str, object]]] = []

        def popen(command: list[str], **kwargs: object) -> FakeWindowsProcess:
            launches.append((command, kwargs))
            return process

        controller = OverlayController(
            bottom_margin_px=32,
            reduced_motion=False,
            backend=OverlayBackend.WINDOWS,
            helper_path=Path("/tmp/renderer_qt.py"),
            popen_factory=popen,
            socket_pair_factory=lambda: (parent, child),
            autostart=False,
        )
        controller.start()
        self.addCleanup(controller.close)
        self._wait_for(lambda: bool(launches))

        command, kwargs = launches[0]
        # `str()`, not `.as_posix()`: on a real Windows host this command
        # line is rendered by a `WindowsPath`, and `.as_posix()` would force
        # forward slashes a real launch never produces there.
        self.assertEqual(str(Path(sys.executable)), command[0])
        self.assertEqual(str(Path("/tmp/renderer_qt.py").resolve()), command[1])
        self.assertIn("--fd-share-stdin", command)
        self.assertNotIn("--fd", command)
        self.assertNotIn("pass_fds", kwargs)

        self._wait_for(lambda: process.stdin.closed)
        self.assertEqual(4_242, child.shared_for_pid)
        self.assertEqual(b"share-data-for-4242", process.stdin.written)
        self.assertTrue(controller.health.available)

    def test_windows_launch_environment_has_no_gtk_vocabulary(self) -> None:
        parent = FakeSocket(12)
        child = FakeShareableSocket(13)
        process = FakeWindowsProcess(pid=7)
        launches: list[dict[str, object]] = []

        def popen(command: list[str], **kwargs: object) -> FakeWindowsProcess:
            del command
            launches.append(kwargs)
            return process

        controller = OverlayController(
            bottom_margin_px=32,
            reduced_motion=False,
            backend=OverlayBackend.WINDOWS,
            helper_path=Path("/tmp/renderer_qt.py"),
            popen_factory=popen,
            socket_pair_factory=lambda: (parent, child),
        )
        self.addCleanup(controller.close)
        self._wait_for(lambda: bool(launches))

        environment = launches[0]["env"]
        self.assertNotIn("GDK_BACKEND", environment)
        self.assertNotIn("PYTHONNOUSERSITE", environment)

    def _wait_for(self, condition: callable, timeout: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() >= deadline:
                self.fail("Timed out waiting for controller output")
            time.sleep(0.005)