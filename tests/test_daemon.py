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
from murmly.focus import NullFocusObserver, WindowIdentity
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
        self.targets_captured = 0
        self.received_targets: list[WindowIdentity | None] = []
        self.target = WindowIdentity(window_id=1, pid=10, window_class="editor")
        self.start_error = start_error
        self.stop_error = stop_error
        self.process_error = process_error

    def capture_delivery_target(self) -> WindowIdentity | None:
        self.targets_captured += 1
        return self.target

    def start_recording(self) -> None:
        self.started += 1
        if self.start_error is not None:
            raise self.start_error

    def stop_recording(self) -> bytes:
        self.stopped += 1
        if self.stop_error is not None:
            raise self.stop_error
        return b"recording"

    def process_recording(
        self,
        pcm_audio: bytes,
        target: WindowIdentity | None = None,
    ) -> ProcessingResult:
        self.processed += 1
        self.received_targets.append(target)
        if pcm_audio != b"recording":
            raise RuntimeError("unexpected recording")
        if self.process_error is not None:
            raise self.process_error
        return ProcessingResult(text="hello world", state="DONE", delivered=True)


class BlockingSession(DummySession):
    def __init__(self) -> None:
        super().__init__()
        self.processing = threading.Event()
        self.release = threading.Event()

    def process_recording(
        self,
        pcm_audio: bytes,
        target: WindowIdentity | None = None,
    ) -> ProcessingResult:
        self.processing.set()
        self.release.wait(timeout=5)
        return super().process_recording(pcm_audio, target)


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


class DeliveryTargetTests(unittest.TestCase):
    def _config(self, temp_dir: str, **overrides: object) -> MurmlyConfig:
        return MurmlyConfig(
            socket_path=Path(temp_dir) / "murmly.sock",
            config_path=Path(temp_dir) / "config.toml",
            overlay_enabled=False,
            **overrides,
        )

    def test_target_is_recorded_before_transcription_and_not_re_read(self) -> None:
        order: list[str] = []

        class OrderedSession(DummySession):
            def capture_delivery_target(inner) -> WindowIdentity | None:
                order.append("capture_target")
                return super().capture_delivery_target()

            def process_recording(inner, pcm_audio, target=None):
                order.append("process")
                return super().process_recording(pcm_audio, target)

        with tempfile.TemporaryDirectory() as temp_dir:
            session = OrderedSession()
            daemon = MurmlyDaemon(self._config(temp_dir), session=session, overlay=FakeOverlay())

            daemon.handle_command("toggle")
            daemon.handle_command("toggle")

        self.assertEqual(["capture_target", "process"], order)
        self.assertEqual(1, session.targets_captured)
        self.assertEqual([session.target], session.received_targets)

    def test_target_capture_failure_does_not_abort_the_lifecycle(self) -> None:
        class FailingObserver:
            supported = True
            detail = None

            def active_window(self):
                raise OSError("display went away")

        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir)
            with patch("murmly.daemon.SoundDeviceRecorder"), patch(
                "murmly.daemon.FasterWhisperTranscriber"
            ):
                session = SpeechSession(config, focus_observer=FailingObserver())

            self.assertIsNone(session.capture_delivery_target())

    def test_refused_delivery_copies_without_pasting_and_publishes_error(self) -> None:
        class RefusingSession(DummySession):
            def process_recording(inner, pcm_audio, target=None):
                inner.processed += 1
                return ProcessingResult(
                    text="hello world",
                    state="DONE",
                    delivered=False,
                    detail="Transcript copied to the clipboard but not pasted.",
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            overlay = FakeOverlay()
            daemon = MurmlyDaemon(self._config(temp_dir), session=RefusingSession(), overlay=overlay)

            daemon.handle_command("toggle")
            response = daemon.handle_command("toggle")

        self.assertTrue(response["ok"])
        self.assertFalse(response["delivered"])
        self.assertIn("not pasted", response["detail"])
        self.assertEqual("hello world", response["text"])
        self.assertIn(("error", 2_000), overlay.events)
        self.assertNotIn(("state", OverlayState.IDLE), overlay.events)

    def test_delivered_transcript_reports_delivery_and_returns_to_idle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            overlay = FakeOverlay()
            daemon = MurmlyDaemon(self._config(temp_dir), session=DummySession(), overlay=overlay)

            daemon.handle_command("toggle")
            response = daemon.handle_command("toggle")

        self.assertTrue(response["delivered"])
        self.assertNotIn("detail", response)
        self.assertIn(("state", OverlayState.IDLE), overlay.events)


class TranscriptDeliveryTests(unittest.TestCase):
    def _session(self, temp_dir: str, observer, verify_target: bool = True) -> SpeechSession:
        config = MurmlyConfig(
            socket_path=Path(temp_dir) / "murmly.sock",
            config_path=Path(temp_dir) / "config.toml",
            overlay_enabled=False,
            verify_target=verify_target,
        )
        with patch("murmly.daemon.SoundDeviceRecorder"), patch("murmly.daemon.FasterWhisperTranscriber"):
            session = SpeechSession(config, focus_observer=observer)
        return session

    class RecordingPaster:
        def __init__(self) -> None:
            self.copied: list[str] = []
            self.pasted: list[str] = []

        def copy(self, text: str) -> None:
            self.copied.append(text)

        def copy_and_paste(self, text: str) -> None:
            self.pasted.append(text)

    class Observer:
        def __init__(self, window, supported: bool = True) -> None:
            self._window = window
            self._supported = supported

        @property
        def supported(self) -> bool:
            return self._supported

        @property
        def detail(self) -> str | None:
            return None

        def active_window(self):
            return self._window

    def _run(self, session: SpeechSession, target, text: str = "hello world"):
        paster = self.RecordingPaster()
        session._paster = paster
        session._transcriber.transcribe_pcm16.return_value = text
        session._recorder.sample_rate_hz = 16_000
        return session.process_recording(b"pcm", target), paster

    def test_unchanged_focus_pastes(self) -> None:
        target = WindowIdentity(1, 10, "editor")
        with tempfile.TemporaryDirectory() as temp_dir:
            session = self._session(temp_dir, self.Observer(target))
            result, paster = self._run(session, target)

        self.assertTrue(result.delivered)
        self.assertEqual(["hello world"], paster.pasted)
        self.assertEqual([], paster.copied)

    def test_changed_focus_copies_without_pasting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = self._session(temp_dir, self.Observer(WindowIdentity(2, 20, "browser")))
            result, paster = self._run(session, WindowIdentity(1, 10, "editor"))

        self.assertFalse(result.delivered)
        self.assertEqual(["hello world"], paster.copied)
        self.assertEqual([], paster.pasted)
        self.assertIsNotNone(result.detail)

    def test_unreadable_focus_copies_without_pasting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = self._session(temp_dir, self.Observer(None))
            result, paster = self._run(session, WindowIdentity(1, 10, "editor"))

        self.assertFalse(result.delivered)
        self.assertEqual(["hello world"], paster.copied)

    def test_disabled_verification_pastes_despite_focus_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = self._session(
                temp_dir,
                self.Observer(WindowIdentity(2, 20, "browser")),
                verify_target=False,
            )
            result, paster = self._run(session, WindowIdentity(1, 10, "editor"))

        self.assertTrue(result.delivered)
        self.assertEqual(["hello world"], paster.pasted)

    def test_unverified_session_pastes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = self._session(temp_dir, NullFocusObserver("unsupported"))
            result, paster = self._run(session, None)

        self.assertTrue(result.delivered)
        self.assertEqual(["hello world"], paster.pasted)

    def test_empty_transcript_touches_the_clipboard_at_all(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = self._session(temp_dir, self.Observer(WindowIdentity(2, 20, "browser")))
            result, paster = self._run(session, WindowIdentity(1, 10, "editor"), text="")

        self.assertFalse(result.delivered)
        self.assertIsNone(result.detail)
        self.assertEqual([], paster.copied)
        self.assertEqual([], paster.pasted)

    def test_refusal_log_excludes_transcript_and_window_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = self._session(temp_dir, self.Observer(WindowIdentity(2, 20, "browser")))
            with self.assertLogs("murmly.daemon", level="WARNING") as logs:
                self._run(session, WindowIdentity(1, 10, "editor"), text="my private words")

        recorded = "\n".join(logs.output)
        self.assertIn("delivery refused", recorded)
        self.assertNotIn("my private words", recorded)
        self.assertNotIn("browser", recorded)
        self.assertNotIn("editor", recorded)
