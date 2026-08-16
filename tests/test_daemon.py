from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from murmly.config import MurmlyConfig
from murmly.daemon import MAX_COMMAND_WORKERS, MurmlyDaemon, ProcessingResult, SpeechSession, send_command
from murmly.overlay import OverlayHealth, OverlayState


class DummySession:
    def __init__(
        self,
        *,
        start_error: Exception | None = None,
        stop_error: Exception | None = None,
        process_error: Exception | None = None,
    ) -> None:
        self.started = 0
        self.stopped = 0
        self.processed = 0
        self.start_error = start_error
        self.stop_error = stop_error
        self.process_error = process_error

    def start_recording(self) -> None:
        self.started += 1
        if self.start_error is not None:
            raise self.start_error

    def stop_recording(self) -> bytes:
        self.stopped += 1
        if self.stop_error is not None:
            raise self.stop_error
        return b"recording"

    def process_recording(self, pcm_audio: bytes) -> ProcessingResult:
        self.processed += 1
        if pcm_audio != b"recording":
            raise RuntimeError("unexpected recording")
        if self.process_error is not None:
            raise self.process_error
        return ProcessingResult(text="hello world", state="DONE")


class BlockingSession(DummySession):
    def __init__(self) -> None:
        super().__init__()
        self.processing = threading.Event()
        self.release = threading.Event()

    def process_recording(self, pcm_audio: bytes) -> ProcessingResult:
        self.processing.set()
        self.release.wait(timeout=5)
        return super().process_recording(pcm_audio)


class FakeOverlay:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.events: list[tuple[str, object]] = []
        self.closed = False

    @property
    def health(self) -> OverlayHealth:
        return OverlayHealth(True)

    def publish_state(self, state: OverlayState) -> None:
        if self.fail:
            raise RuntimeError("overlay unavailable")
        self.events.append(("state", state))

    def publish_level(self, level: float) -> None:
        if self.fail:
            raise RuntimeError("overlay unavailable")
        self.events.append(("level", level))

    def publish_error(self, duration_ms: int = 2_000) -> None:
        if self.fail:
            raise RuntimeError("overlay unavailable")
        self.events.append(("error", duration_ms))

    def close(self) -> None:
        if self.fail:
            raise RuntimeError("overlay unavailable")
        self.closed = True


class DaemonTests(unittest.TestCase):
    def test_daemon_initializes_without_clipboard_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
                overlay_enabled=False,
            )
            daemon = MurmlyDaemon(config)

        self.assertEqual("IDLE", daemon.state)

    def test_socket_protocol_toggles_between_idle_and_done(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            socket_path = Path(temp_dir) / "murmly.sock"
            config = MurmlyConfig(
                socket_path=socket_path,
                config_path=Path(temp_dir) / "config.toml",
                overlay_enabled=False,
            )
            session = DummySession()
            daemon = MurmlyDaemon(config, session=session)
            thread = threading.Thread(target=daemon.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(daemon.shutdown)
            self.addCleanup(thread.join, 2)

            deadline = time.time() + 2
            while not socket_path.exists():
                if time.time() >= deadline:
                    self.fail("daemon socket was not created")
                time.sleep(0.05)

            status = send_command(str(socket_path), "status")
            first = send_command(str(socket_path), "toggle")
            second = send_command(str(socket_path), "toggle")

        self.assertEqual({"ok": True, "state": "IDLE"}, status)
        self.assertEqual("LISTENING", first["state"])
        self.assertEqual("DONE", second["state"])
        self.assertEqual("hello world", second["text"])
        self.assertEqual(1, session.started)
        self.assertEqual(1, session.stopped)
        self.assertEqual(1, session.processed)

    def test_speech_session_injects_level_sink_into_recorder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
                overlay_enabled=False,
            )
            sink = lambda _level: None
            with patch("murmly.daemon.SoundDeviceRecorder") as recorder:
                SpeechSession(config, level_sink=sink)

        recorder.assert_called_once_with(config, level_sink=sink)

    def test_daemon_publishes_successful_overlay_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            overlay = FakeOverlay()
            daemon = MurmlyDaemon(config, session=DummySession(), overlay=overlay)

            first = daemon.handle_command("toggle")
            second = daemon.handle_command("toggle")

        self.assertEqual("LISTENING", first["state"])
        self.assertEqual("DONE", second["state"])
        self.assertEqual(
            [
                ("state", OverlayState.LISTENING),
                ("state", OverlayState.THINKING),
                ("state", OverlayState.IDLE),
            ],
            overlay.events,
        )

    def test_recorder_stops_before_thinking_is_published(self) -> None:
        events: list[str] = []

        class OrderedSession(DummySession):
            def stop_recording(self) -> bytes:
                events.append("stop")
                return super().stop_recording()

        class OrderedOverlay(FakeOverlay):
            def publish_state(self, state: OverlayState) -> None:
                if state is OverlayState.THINKING:
                    events.append("thinking")
                super().publish_state(state)

        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            daemon = MurmlyDaemon(config, session=OrderedSession(), overlay=OrderedOverlay())
            daemon.handle_command("toggle")
            daemon.handle_command("toggle")

        self.assertEqual(["stop", "thinking"], events)

    def test_startup_and_processing_failures_publish_error(self) -> None:
        cases = [
            (DummySession(start_error=RuntimeError("microphone failed")), 1),
            (DummySession(stop_error=RuntimeError("microphone stop failed")), 2),
            (DummySession(process_error=RuntimeError("transcription failed")), 2),
        ]
        for session, toggle_count in cases:
            with self.subTest(toggle_count=toggle_count), tempfile.TemporaryDirectory() as temp_dir:
                config = MurmlyConfig(
                    socket_path=Path(temp_dir) / "murmly.sock",
                    config_path=Path(temp_dir) / "config.toml",
                )
                overlay = FakeOverlay()
                daemon = MurmlyDaemon(config, session=session, overlay=overlay)

                responses = [daemon.handle_command("toggle") for _index in range(toggle_count)]

            self.assertFalse(responses[-1]["ok"])
            self.assertEqual("IDLE", daemon.state)
            self.assertEqual(("error", 2_000), overlay.events[-1])
            self.assertNotIn(("state", OverlayState.IDLE), overlay.events)

    def test_overlay_failure_does_not_change_voice_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            daemon = MurmlyDaemon(config, session=DummySession(), overlay=FakeOverlay(fail=True))

            first = daemon.handle_command("toggle")
            second = daemon.handle_command("toggle")

        self.assertEqual("LISTENING", first["state"])
        self.assertEqual("DONE", second["state"])

    def test_disabled_overlay_does_not_create_renderer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
                overlay_enabled=False,
            )
            with patch("murmly.daemon.OverlayController") as controller:
                MurmlyDaemon(config, session=DummySession())

        controller.assert_not_called()

    def test_server_shutdown_closes_overlay_after_socket_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            socket_path = Path(temp_dir) / "murmly.sock"
            config = MurmlyConfig(socket_path=socket_path, config_path=Path(temp_dir) / "config.toml")
            overlay = FakeOverlay()
            daemon = MurmlyDaemon(config, session=DummySession(), overlay=overlay)
            thread = threading.Thread(target=daemon.serve_forever, daemon=True)
            thread.start()
            deadline = time.time() + 2
            while not socket_path.exists():
                if time.time() >= deadline:
                    self.fail("daemon socket was not created")
                time.sleep(0.05)

            daemon.shutdown()
            thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertFalse(socket_path.exists())
        self.assertTrue(overlay.closed)

    def test_shutdown_unwinds_server_while_processing_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            socket_path = Path(temp_dir) / "murmly.sock"
            config = MurmlyConfig(socket_path=socket_path, config_path=Path(temp_dir) / "config.toml")
            session = BlockingSession()
            overlay = FakeOverlay()
            daemon = MurmlyDaemon(config, session=session, overlay=overlay)
            server_thread = threading.Thread(target=daemon.serve_forever, daemon=True)
            server_thread.start()
            deadline = time.time() + 2
            while not socket_path.exists():
                if time.time() >= deadline:
                    self.fail("daemon socket was not created")
                time.sleep(0.01)
            self.assertEqual("LISTENING", send_command(str(socket_path), "toggle")["state"])

            def blocked_request() -> None:
                try:
                    send_command(str(socket_path), "toggle")
                except RuntimeError:
                    pass

            processing_thread = threading.Thread(
                target=blocked_request,
                daemon=True,
            )
            processing_thread.start()
            self.assertTrue(session.processing.wait(timeout=1))

            daemon.shutdown()
            server_thread.join(timeout=0.5)

            self.assertFalse(server_thread.is_alive())
            self.assertFalse(socket_path.exists())
            self.assertTrue(overlay.closed)
            session.release.set()
            processing_thread.join(timeout=1)

    def test_incomplete_clients_are_bounded_and_closed_on_shutdown(self) -> None:
        clients: list[socket.socket] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            socket_path = Path(temp_dir) / "murmly.sock"
            config = MurmlyConfig(
                socket_path=socket_path,
                config_path=Path(temp_dir) / "config.toml",
                overlay_enabled=False,
            )
            daemon = MurmlyDaemon(config, session=DummySession())
            server_thread = threading.Thread(target=daemon.serve_forever, daemon=True)
            server_thread.start()
            deadline = time.time() + 2
            while not socket_path.exists():
                if time.time() >= deadline:
                    self.fail("daemon socket was not created")
                time.sleep(0.01)

            for _index in range(MAX_COMMAND_WORKERS + 4):
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.connect(str(socket_path))
                clients.append(client)
            deadline = time.time() + 1
            while True:
                with daemon._connections_lock:
                    connection_count = len(daemon._connections)
                if connection_count == MAX_COMMAND_WORKERS or time.time() >= deadline:
                    break
                time.sleep(0.01)

            daemon.shutdown()
            server_thread.join(timeout=0.5)
            deadline = time.time() + 1
            while True:
                with daemon._connections_lock:
                    remaining_connections = len(daemon._connections)
                if remaining_connections == 0 or time.time() >= deadline:
                    break
                time.sleep(0.01)

        for client in clients:
            client.close()
        self.assertEqual(MAX_COMMAND_WORKERS, connection_count)
        self.assertEqual(0, remaining_connections)
        self.assertFalse(server_thread.is_alive())
        self.assertFalse(socket_path.exists())

    def test_oversized_command_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            socket_path = Path(temp_dir) / "murmly.sock"
            config = MurmlyConfig(
                socket_path=socket_path,
                config_path=Path(temp_dir) / "config.toml",
                overlay_enabled=False,
            )
            daemon = MurmlyDaemon(config, session=DummySession())
            server_thread = threading.Thread(target=daemon.serve_forever, daemon=True)
            server_thread.start()
            deadline = time.time() + 2
            while not socket_path.exists():
                if time.time() >= deadline:
                    self.fail("daemon socket was not created")
                time.sleep(0.01)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(socket_path))
                client.sendall(b"x" * 4_097)
                response = json.loads(client.recv(4_096))
            daemon.shutdown()
            server_thread.join(timeout=1)

        self.assertFalse(response["ok"])
        self.assertIn("4096-byte", response["error"])

    def test_shutdown_during_slot_acquisition_rejects_connection_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
                overlay_enabled=False,
            )
            daemon = MurmlyDaemon(config, session=DummySession())

            class ShutdownSemaphore:
                def __init__(self) -> None:
                    self.released = 0

                def acquire(self, blocking: bool) -> bool:
                    self.assert_nonblocking = blocking
                    daemon.shutdown()
                    return True

                def release(self) -> None:
                    self.released += 1

            semaphore = ShutdownSemaphore()
            daemon._worker_slots = semaphore
            server_side, client_side = socket.socketpair()
            try:
                dispatched = daemon._dispatch_connection(server_side)
                client_side.settimeout(1)
                received = client_side.recv(1)
            finally:
                client_side.close()

        self.assertFalse(dispatched)
        self.assertFalse(semaphore.assert_nonblocking)
        self.assertEqual(1, semaphore.released)
        self.assertEqual(b"", received)
        with daemon._connections_lock:
            self.assertEqual(0, len(daemon._connections))
