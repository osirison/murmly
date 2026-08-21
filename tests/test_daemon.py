from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from murmly.config import MurmlyConfig, default_socket_path
from murmly.daemon import (
    MAX_COMMAND_BYTES,
    MAX_COMMAND_WORKERS,
    CommandCode,
    DaemonNotRespondingError,
    DaemonStartupError,
    MurmlyDaemon,
    ProcessingResult,
    RequestError,
    SpeechSession,
    read_peer_identity,
    send_command,
    socket_path_detail,
)
from murmly.focus import NullFocusObserver, WindowIdentity
from murmly.integrations import DeliveryOutcome
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

    def publish_partial(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("overlay unavailable")
        self.events.append(("partial", text))

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

            responses: list[dict[str, object]] = []

            def blocked_request() -> None:
                # What this receives is the point of the test: a command shutdown
                # interrupts is answered rather than closed on.
                responses.append(send_command(str(socket_path), "toggle"))

            processing_thread = threading.Thread(
                target=blocked_request,
                daemon=True,
            )
            processing_thread.start()
            self.assertTrue(session.processing.wait(timeout=1))

            daemon.shutdown()
            # Not 0.5: SHUTDOWN_DRAIN_SECONDS is itself 0.5, so a bound equal to
            # the drain fails under load for a shutdown that is working
            # perfectly. What is asserted is that the thread exits, not how fast.
            server_thread.join(timeout=3.0)

            self.assertFalse(server_thread.is_alive())
            self.assertFalse(socket_path.exists())
            self.assertTrue(overlay.closed)
            session.release.set()
            processing_thread.join(timeout=1)

            self.assertEqual(1, len(responses))
            self.assertEqual(CommandCode.SHUTTING_DOWN, responses[0]["code"])

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
                received = client_side.recv(4_096)
            finally:
                client_side.close()

        response = json.loads(received)
        self.assertFalse(dispatched)
        self.assertFalse(semaphore.assert_nonblocking)
        self.assertEqual(1, semaphore.released)
        self.assertFalse(response["ok"])
        self.assertEqual(CommandCode.SHUTTING_DOWN, response["code"])
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
        def __init__(self, injection_reason: str | None = None) -> None:
            self.copied: list[str] = []
            self.pasted: list[str] = []
            self.injection_reason = injection_reason

        def copy(self, text: str) -> None:
            self.copied.append(text)

        def copy_and_paste(self, text: str) -> DeliveryOutcome:
            self.pasted.append(text)
            if self.injection_reason is not None:
                self.copied.append(text)
                return DeliveryOutcome(False, self.injection_reason)
            return DeliveryOutcome(True)

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

    def test_a_session_without_an_injector_copies_and_reports_it(self) -> None:
        target = WindowIdentity(1, 10, "editor")
        with tempfile.TemporaryDirectory() as temp_dir:
            session = self._session(temp_dir, self.Observer(target))
            paster = self.RecordingPaster(injection_reason="No Wayland paste injector is installed.")
            session._paster = paster
            session._transcriber.transcribe_pcm16.return_value = "hello world"
            session._recorder.sample_rate_hz = 16_000
            result = session.process_recording(b"pcm", target)

        # The same outcome as a refused delivery, so every path that already handles
        # a refusal - the response, the overlay, ending a continuous session - applies.
        self.assertFalse(result.delivered)
        self.assertEqual("Transcript copied to the clipboard but not pasted.", result.detail)
        self.assertEqual(["hello world"], paster.copied)

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


class SegmentSession(DummySession):
    """A session double that can close segments while capture continues."""

    def __init__(self, *, texts: list[str] | None = None, delivered: list[bool] | None = None) -> None:
        super().__init__()
        self.texts = texts or ["hello world"]
        self.delivered = delivered or []
        self.segments_taken = 0
        self.processed_audio: list[bytes] = []

    def take_segment(self) -> bytes:
        self.segments_taken += 1
        return b"recording"

    def process_recording(
        self,
        pcm_audio: bytes,
        target: WindowIdentity | None = None,
    ) -> ProcessingResult:
        self.processed += 1
        self.processed_audio.append(pcm_audio)
        self.received_targets.append(target)
        index = self.processed - 1
        text = self.texts[index] if index < len(self.texts) else ""
        delivered = self.delivered[index] if index < len(self.delivered) else True
        if not text:
            return ProcessingResult(text="", state="DONE")
        if delivered:
            return ProcessingResult(text=text, state="DONE", delivered=True)
        return ProcessingResult(
            text=text,
            state="DONE",
            delivered=False,
            detail="Transcript copied to the clipboard but not pasted.",
        )


class AutoTranscribeTests(unittest.TestCase):
    def _daemon(self, temp_dir: str, session, **overrides: object) -> MurmlyDaemon:
        config = MurmlyConfig(
            socket_path=Path(temp_dir) / "murmly.sock",
            config_path=Path(temp_dir) / "config.toml",
            overlay_enabled=False,
            **overrides,
        )
        return MurmlyDaemon(config, session=session)

    def _settle(self, daemon: MurmlyDaemon) -> None:
        """Wait for an off-thread segment delivery to finish.

        Segment delivery runs on its own thread so it cannot stall silence
        polling, so the tests have to join it rather than assume it is done.
        """
        thread = daemon._segment_thread
        if thread is not None:
            thread.join(timeout=5)

    def _wait_for_state(self, daemon: MurmlyDaemon, state: str) -> None:
        deadline = time.time() + 3
        while time.time() < deadline:
            if daemon.state == state:
                return
            time.sleep(0.01)
        self.fail(f"daemon never reached {state}; it is {daemon.state}")

    def test_silence_ends_the_recording_in_stop_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = DummySession()
            daemon = self._daemon(temp_dir, session, auto_transcribe="stop")
            daemon.handle_command("toggle")
            self.assertEqual("LISTENING", daemon.state)

            daemon._on_silence()

            self._settle(daemon)
            self._wait_for_state(daemon, "IDLE")

        self.assertEqual(1, session.stopped)
        self.assertEqual(1, session.processed)
        self.assertEqual(1, session.targets_captured)

    def test_silence_is_ignored_when_auto_transcribe_is_off(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = DummySession()
            daemon = self._daemon(temp_dir, session)
            daemon.handle_command("toggle")

            daemon._on_silence()

            self._settle(daemon)

            self.assertEqual("LISTENING", daemon.state)
            self.assertEqual(0, session.stopped)
            self.assertEqual(0, session.processed)

    def test_a_toggle_before_the_silence_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = DummySession()
            daemon = self._daemon(temp_dir, session, auto_transcribe="stop")
            daemon.handle_command("toggle")
            daemon.handle_command("toggle")
            self.assertEqual("IDLE", daemon.state)

            daemon._on_silence()

            self._settle(daemon)

            self.assertEqual(1, session.stopped)
            self.assertEqual(1, session.processed)

    def test_toggle_during_auto_stopped_processing_reports_busy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = BlockingSession()
            daemon = self._daemon(temp_dir, session, auto_transcribe="stop")
            daemon.handle_command("toggle")

            daemon._on_silence()

            self._settle(daemon)
            self.assertTrue(session.processing.wait(timeout=3))

            response = daemon.handle_command("toggle")
            session.release.set()
            self._wait_for_state(daemon, "IDLE")

        self.assertFalse(response["ok"])
        self.assertEqual("THINKING", response["state"])
        self.assertEqual("Daemon is busy.", response["error"])
        self.assertEqual(1, session.started)

    def test_continuous_mode_delivers_segments_and_keeps_listening(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = SegmentSession(texts=["first segment", "second segment", "final segment"])
            daemon = self._daemon(temp_dir, session, auto_transcribe="continuous")
            daemon.handle_command("toggle")

            daemon._on_silence()

            self._settle(daemon)
            self.assertEqual("LISTENING", daemon.state)
            daemon._on_silence()
            self._settle(daemon)
            self.assertEqual("LISTENING", daemon.state)

            response = daemon.handle_command("toggle")

        self.assertEqual(2, session.segments_taken)
        self.assertEqual(1, session.stopped)
        self.assertEqual(3, session.processed)
        self.assertEqual("first segment second segment final segment", response["text"])
        self.assertTrue(response["delivered"])
        self.assertEqual(3, response["segments"])
        self.assertEqual("IDLE", daemon.state)

    def test_each_segment_records_its_own_delivery_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = SegmentSession(texts=["one", "two"])
            daemon = self._daemon(temp_dir, session, auto_transcribe="continuous")
            daemon.handle_command("toggle")

            daemon._on_silence()

            self._settle(daemon)
            daemon.handle_command("toggle")

        self.assertEqual(2, session.targets_captured)
        self.assertEqual(2, len(session.received_targets))

    def test_a_refused_segment_ends_the_continuous_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = SegmentSession(texts=["refused text"], delivered=[False])
            daemon = self._daemon(temp_dir, session, auto_transcribe="continuous")
            daemon.handle_command("toggle")

            daemon._on_silence()

            self._settle(daemon)
            self._wait_for_state(daemon, "IDLE")

        self.assertEqual(1, session.segments_taken)
        self.assertEqual(1, session.stopped)
        # The trailing audio is not delivered after a refusal.
        self.assertEqual(1, session.processed)

    def test_continuous_session_with_no_trailing_speech_delivers_nothing_more(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = SegmentSession(texts=["only segment", ""])
            daemon = self._daemon(temp_dir, session, auto_transcribe="continuous")
            daemon.handle_command("toggle")

            daemon._on_silence()

            self._settle(daemon)
            response = daemon.handle_command("toggle")

        self.assertEqual("only segment", response["text"])
        self.assertTrue(response["delivered"])
        self.assertNotIn("segments", response)

    def test_single_transcript_response_is_unchanged_by_this_capability(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = DummySession()
            daemon = self._daemon(temp_dir, session, auto_transcribe="continuous", live_transcribe=True)
            daemon.handle_command("toggle")

            response = daemon.handle_command("toggle")

        self.assertEqual(
            {"ok": True, "state": "DONE", "text": "hello world", "delivered": True},
            response,
        )

    def test_public_states_are_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = SegmentSession(texts=["a", "b"])
            daemon = self._daemon(temp_dir, session, auto_transcribe="continuous", live_transcribe=True)

            self.assertEqual("IDLE", daemon.state)
            self.assertEqual({"ok": True, "state": "IDLE"}, daemon.handle_command("status"))
            daemon.handle_command("toggle")
            self.assertEqual("LISTENING", daemon.state)
            daemon._on_silence()
            self._settle(daemon)
            self.assertEqual("LISTENING", daemon.state)
            daemon.handle_command("toggle")
            self.assertEqual("IDLE", daemon.state)


class LiveTranscriptionSessionTests(unittest.TestCase):
    def _session(self, temp_dir: str, **overrides: object) -> SpeechSession:
        config = MurmlyConfig(
            socket_path=Path(temp_dir) / "murmly.sock",
            config_path=Path(temp_dir) / "config.toml",
            overlay_enabled=False,
            **overrides,
        )
        with patch("murmly.daemon.SoundDeviceRecorder"), patch("murmly.daemon.FasterWhisperTranscriber"):
            return SpeechSession(config, focus_observer=NullFocusObserver("unsupported"))

    def test_delivered_transcript_ignores_partials_entirely(self) -> None:
        transcripts = []
        for live in (False, True):
            with tempfile.TemporaryDirectory() as temp_dir:
                session = self._session(temp_dir, live_transcribe=live)
                session._paster = TranscriptDeliveryTests.RecordingPaster()
                session._recorder.sample_rate_hz = 16_000
                session._transcriber.transcribe_pcm16.return_value = "the delivered transcript"
                session._transcriber.transcribe_partial.return_value = "a partial guess"

                result = session.process_recording(b"pcm", None)
                transcripts.append(result.text)

                # Delivery reads the complete recording, never a partial.
                session._transcriber.transcribe_pcm16.assert_called_once_with(b"pcm", 16_000)
                session._transcriber.transcribe_partial.assert_not_called()

        self.assertEqual(["the delivered transcript", "the delivered transcript"], transcripts)

    def test_no_live_worker_starts_when_both_features_are_off(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = self._session(temp_dir)
            session.start_recording()
            self.addCleanup(session.stop_recording)

        self.assertEqual([], session._live_threads)
        self.assertIsNone(session._silence)

    def test_live_worker_starts_for_partials_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = self._session(temp_dir, live_transcribe=True, live_interval_ms=250)
            session._recorder.sample_rate_hz = 16_000
            session._recorder.snapshot.return_value = b"\x01\x00" * 8_000
            session._transcriber.transcribe_partial.return_value = "partial words"
            published: list[str] = []
            session._partial_sink = published.append

            session.start_recording()
            deadline = time.time() + 3
            while time.time() < deadline and not published:
                time.sleep(0.01)
            session.stop_recording()

        self.assertEqual(["partial words"], published[:1])
        self.assertIsNone(session._silence)

    def test_stop_recording_stops_partials_before_returning_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = self._session(temp_dir, live_transcribe=True)
            session.start_recording()
            session.stop_recording()

        session._transcriber.stop_partials.assert_called_once()
        self.assertTrue(session._live_stop.is_set())


class LiveWorkerCadenceTests(unittest.TestCase):
    """Silence detection must not be starved by the partial transcription pass.

    A pause only slightly longer than the threshold is wiped by the next
    utterance, so a partial pass that blocks silence polling loses it.
    """

    def _session(self, temp_dir: str, **overrides: object) -> SpeechSession:
        config = MurmlyConfig(
            socket_path=Path(temp_dir) / "murmly.sock",
            config_path=Path(temp_dir) / "config.toml",
            overlay_enabled=False,
            **overrides,
        )
        with patch("murmly.daemon.SoundDeviceRecorder"), patch("murmly.daemon.FasterWhisperTranscriber"):
            return SpeechSession(config, focus_observer=NullFocusObserver("unsupported"))

    def test_a_slow_partial_pass_does_not_starve_silence_polling(self) -> None:
        """The regression guard: a partial slower than the silence cadence.

        Stubbing the partial with an instant no-op only measures scheduling
        arithmetic and would pass even with both on one thread.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            session = self._session(
                temp_dir,
                live_transcribe=True,
                live_interval_ms=250,
                auto_transcribe="continuous",
            )
            silence_ticks = 0
            partials_started = threading.Event()

            def slow_partial() -> None:
                partials_started.set()
                time.sleep(1.5)

            def count_silence() -> None:
                nonlocal silence_ticks
                # Only ticks observed while a partial pass is in flight count:
                # that is exactly the window a shared thread would black out.
                if partials_started.is_set():
                    silence_ticks += 1

            session._partial_tick = slow_partial
            session._silence_tick = count_silence

            stop = threading.Event()
            session._live_stop = stop
            session._live_threads = []
            session._spawn_live_thread(session._run_silence_loop, "murmly-silence", 0.25)
            session._spawn_live_thread(session._run_partial_loop, "murmly-partial", 0.25)
            self.assertTrue(partials_started.wait(timeout=2))
            time.sleep(1.0)
            stop.set()
            for thread in list(session._live_threads):
                thread.join(timeout=3)

        # The observation window sits entirely inside one 1.5 s partial pass. A
        # shared thread would record zero ticks here; separate loops give ~4.
        self.assertGreaterEqual(silence_ticks, 3)

    def test_a_blocking_segment_delivery_does_not_stall_silence_polling(self) -> None:
        """Drives the real silence loop against a delivery that blocks.

        Every other auto-transcribe test calls `_on_silence()` from the test
        thread, so none of them can see a delivery running on the polling thread.
        """
        class BlockingSegmentSession(SegmentSession):
            def __init__(self) -> None:
                super().__init__(texts=["one", "two"])
                self.delivering = threading.Event()
                self.release = threading.Event()

            def process_recording(self, pcm_audio, target=None):
                self.delivering.set()
                self.release.wait(timeout=5)
                return super().process_recording(pcm_audio, target)

        with tempfile.TemporaryDirectory() as temp_dir:
            session = BlockingSegmentSession()
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
                overlay_enabled=False,
                auto_transcribe="continuous",
            )
            daemon = MurmlyDaemon(config, session=session)
            daemon.handle_command("toggle")

            ticks = 0
            triggered = threading.Event()

            def tick() -> None:
                nonlocal ticks
                if not triggered.is_set():
                    triggered.set()
                    daemon._on_silence()
                    return
                if session.delivering.is_set():
                    ticks += 1

            loop_session = SpeechSession.__new__(SpeechSession)
            loop_session._clock = time.monotonic
            loop_session._silence_tick = tick

            stop = threading.Event()
            worker = threading.Thread(
                target=SpeechSession._run_tick_loop,
                args=(loop_session, stop, 0.25, tick, "silence"),
                daemon=True,
            )
            worker.start()
            self.assertTrue(session.delivering.wait(timeout=3))
            time.sleep(1.0)
            session.release.set()
            stop.set()
            worker.join(timeout=3)
            if daemon._segment_thread is not None:
                daemon._segment_thread.join(timeout=5)
            daemon.handle_command("toggle")

        # The delivery blocks on its own thread, so the loop keeps ticking.
        self.assertGreaterEqual(ticks, 3)

    def test_segment_delivery_runs_off_the_polling_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = SegmentSession(texts=["one"])
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
                overlay_enabled=False,
                auto_transcribe="continuous",
            )
            daemon = MurmlyDaemon(config, session=session)
            daemon.handle_command("toggle")
            caller = threading.current_thread()

            daemon._on_silence()
            thread = daemon._segment_thread
            self.assertIsNotNone(thread)
            self.assertIsNot(thread, caller)
            thread.join(timeout=5)

            self.assertEqual(1, session.segments_taken)
            daemon.handle_command("toggle")

    def test_only_the_configured_loops_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = self._session(temp_dir, live_transcribe=True)
            session.start_recording()
            self.addCleanup(session.stop_recording)
            names = sorted(thread.name for thread in session._live_threads)

        # Auto-transcribe is off, so no silence loop should exist.
        self.assertEqual(["murmly-partial"], names)

    def test_silence_loop_runs_without_partials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = self._session(temp_dir, auto_transcribe="stop")
            silence_ticks = 0

            def count_silence() -> None:
                nonlocal silence_ticks
                silence_ticks += 1

            session._silence_tick = count_silence
            stop = threading.Event()
            session._live_stop = stop
            session._live_threads = []
            session._spawn_live_thread(session._run_silence_loop, "murmly-silence", 0.25)
            time.sleep(0.9)
            stop.set()
            for thread in list(session._live_threads):
                thread.join(timeout=3)

        self.assertGreaterEqual(silence_ticks, 2)


class SegmentToggleConcurrencyTests(unittest.TestCase):
    """A toggle can arrive while a segment is mid-flight on the live worker.

    Every other continuous-mode test calls `_on_silence()` synchronously on the
    test thread, so none of them can observe the two paths overlapping.
    """

    def _daemon(self, temp_dir: str, session, **overrides: object) -> MurmlyDaemon:
        config = MurmlyConfig(
            socket_path=Path(temp_dir) / "murmly.sock",
            config_path=Path(temp_dir) / "config.toml",
            overlay_enabled=False,
            **overrides,
        )
        return MurmlyDaemon(config, session=session)

    def test_a_toggle_waits_for_an_in_flight_segment(self) -> None:
        class SlowSegmentSession(SegmentSession):
            def __init__(self) -> None:
                super().__init__(texts=["segment one", "final text"])
                self.entered = threading.Event()
                self.release = threading.Event()
                self.order: list[str] = []

            def process_recording(self, pcm_audio, target=None):
                self.order.append("enter")
                if not self.entered.is_set():
                    self.entered.set()
                    self.release.wait(timeout=5)
                result = super().process_recording(pcm_audio, target)
                self.order.append("exit")
                return result

        with tempfile.TemporaryDirectory() as temp_dir:
            session = SlowSegmentSession()
            daemon = self._daemon(temp_dir, session, auto_transcribe="continuous")
            daemon.handle_command("toggle")

            worker = threading.Thread(target=daemon._on_silence, daemon=True)
            worker.start()
            self.assertTrue(session.entered.wait(timeout=3))

            response: dict[str, object] = {}

            def toggle_off() -> None:
                response.update(daemon.handle_command("toggle"))

            toggler = threading.Thread(target=toggle_off, daemon=True)
            toggler.start()
            # The toggle must not begin its own stop-and-deliver yet.
            time.sleep(0.3)
            self.assertEqual(["enter"], session.order)

            session.release.set()
            worker.join(timeout=5)
            toggler.join(timeout=5)

        # Never interleaved: each delivery completed before the next began.
        self.assertEqual(["enter", "exit", "enter", "exit"], session.order)
        self.assertEqual("segment one final text", response["text"])
        self.assertEqual(2, response["segments"])
        self.assertTrue(response["delivered"])

    def test_a_refused_segment_is_reported_even_if_a_toggle_races_it(self) -> None:
        class RefusingSlowSession(SegmentSession):
            def __init__(self) -> None:
                super().__init__(texts=["refused text", "trailing"], delivered=[False, True])
                self.entered = threading.Event()
                self.release = threading.Event()

            def process_recording(self, pcm_audio, target=None):
                if not self.entered.is_set():
                    self.entered.set()
                    self.release.wait(timeout=5)
                return super().process_recording(pcm_audio, target)

        with tempfile.TemporaryDirectory() as temp_dir:
            session = RefusingSlowSession()
            daemon = self._daemon(temp_dir, session, auto_transcribe="continuous")
            daemon.handle_command("toggle")

            worker = threading.Thread(target=daemon._on_silence, daemon=True)
            worker.start()
            self.assertTrue(session.entered.wait(timeout=3))

            response: dict[str, object] = {}
            toggler = threading.Thread(
                target=lambda: response.update(daemon.handle_command("toggle")),
                daemon=True,
            )
            toggler.start()
            session.release.set()
            worker.join(timeout=5)
            toggler.join(timeout=5)

        # Both interleavings must be asserted: guarding on response["ok"] would
        # let the branch where the session-end wins pass vacuously.
        if response.get("ok"):
            self.assertFalse(response["delivered"])
            self.assertIn("refused text", response["text"])
        else:
            # The session ended itself first. The toggle is told the daemon is
            # busy, so the refusal has to be observable from the daemon state.
            self.assertEqual("Daemon is busy.", response["error"])
            deadline = time.time() + 3
            while time.time() < deadline and daemon.state != "IDLE":
                time.sleep(0.01)
            self.assertEqual("IDLE", daemon.state)
            self.assertFalse(daemon._session_delivered)
            self.assertEqual(["refused text"], daemon._segments)

    def test_a_failed_transition_thread_does_not_wedge_the_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = DummySession()
            daemon = self._daemon(temp_dir, session, auto_transcribe="stop")
            daemon.handle_command("toggle")
            self.assertEqual("LISTENING", daemon.state)

            with patch("murmly.daemon.threading.Thread.start", side_effect=RuntimeError("no thread")):
                daemon._on_silence()

            # Without the guard the state stays THINKING and every later toggle
            # is answered "busy" until the daemon is restarted.
            self.assertEqual("IDLE", daemon.state)
            self.assertEqual(1, session.stopped)
            self.assertEqual({"ok": True, "state": "LISTENING"}, daemon.handle_command("toggle"))
            daemon.handle_command("toggle")


def read_frame(client: socket.socket) -> bytes:
    payload = b""
    while not payload.endswith(b"\n"):
        chunk = client.recv(4_096)
        if not chunk:
            break
        payload += chunk
    return payload


def send_payload(socket_path: Path, payload: bytes, timeout: float = 5.0) -> bytes:
    """Send one raw request and read the frame that answers it."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(socket_path))
        if payload:
            client.sendall(payload)
        return read_frame(client)


def send_and_read_to_close(socket_path: Path, payload: bytes, timeout: float = 10.0) -> bytes:
    """Send one request and read until the daemon closes, not just to the first frame.

    Reading past the first newline is the point: it is what can observe a second
    response on a connection that should carry exactly one.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(socket_path))
        client.sendall(payload)
        received = b""
        while True:
            try:
                chunk = client.recv(4_096)
            except (ConnectionResetError, socket.timeout):
                break
            if not chunk:
                break
            received += chunk
    return received


class ServedDaemonTests(unittest.TestCase):
    """A daemon serving on a real socket, for behavior only the transport shows."""

    def serve(self, session: object | None = None, **overrides: object) -> tuple[MurmlyDaemon, Path]:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        socket_path = Path(temp_dir.name) / "murmly.sock"
        config = MurmlyConfig(
            socket_path=socket_path,
            config_path=Path(temp_dir.name) / "config.toml",
            overlay_enabled=False,
        )
        return self.serve_config(config, session, **overrides)

    def serve_config(
        self,
        config: MurmlyConfig,
        session: object | None = None,
        **overrides: object,
    ) -> tuple[MurmlyDaemon, Path]:
        daemon = MurmlyDaemon(config, session=session or DummySession(), **overrides)
        thread = threading.Thread(target=daemon.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(daemon.shutdown)
        deadline = time.time() + 3
        while not config.socket_path.exists():
            if time.time() >= deadline:
                self.fail("daemon socket was not created")
            time.sleep(0.01)
        return daemon, config.socket_path

    def wait_for_connections(self, daemon: MurmlyDaemon, count: int) -> int:
        deadline = time.time() + 3
        while True:
            with daemon._connections_lock:
                observed = len(daemon._connections)
            if observed == count or time.time() >= deadline:
                return observed
            time.sleep(0.01)


class FailureCodeTests(unittest.TestCase):
    def _daemon(self, temp_dir: str, session: object | None = None) -> MurmlyDaemon:
        config = MurmlyConfig(
            socket_path=Path(temp_dir) / "murmly.sock",
            config_path=Path(temp_dir) / "config.toml",
            overlay_enabled=False,
        )
        return MurmlyDaemon(config, session=session or DummySession())

    def test_an_unsupported_command_keeps_its_wording_and_gains_a_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            response = self._daemon(temp_dir).handle_command("wobble")

        self.assertEqual(
            {"ok": False, "error": "Unsupported command: wobble", "code": "unsupported_command"},
            response,
        )

    def test_a_busy_daemon_keeps_its_wording_state_and_gains_a_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = BlockingSession()
            daemon = self._daemon(temp_dir, session)
            daemon.handle_command("toggle")
            stopper = threading.Thread(target=daemon.handle_command, args=("toggle",), daemon=True)
            stopper.start()
            self.assertTrue(session.processing.wait(timeout=2))
            response = daemon.handle_command("toggle")
            session.release.set()
            stopper.join(timeout=3)

        self.assertEqual(
            {"ok": False, "state": "THINKING", "error": "Daemon is busy.", "code": "busy"},
            response,
        )

    def test_a_failed_start_keeps_its_wording_state_and_gains_a_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = DummySession(start_error=RuntimeError("no microphone"))
            response = self._daemon(temp_dir, session).handle_command("toggle")

        self.assertEqual(
            {"ok": False, "state": "IDLE", "error": "no microphone", "code": "command_failed"},
            response,
        )

    def test_a_failed_stop_keeps_its_wording_state_and_gains_a_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = DummySession(stop_error=RuntimeError("capture died"))
            daemon = self._daemon(temp_dir, session)
            daemon.handle_command("toggle")
            response = daemon.handle_command("toggle")

        self.assertEqual(
            {"ok": False, "state": "IDLE", "error": "capture died", "code": "command_failed"},
            response,
        )

    def test_a_failed_transcription_keeps_its_wording_state_and_gains_a_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = DummySession(process_error=RuntimeError("decode exploded"))
            daemon = self._daemon(temp_dir, session)
            daemon.handle_command("toggle")
            response = daemon.handle_command("toggle")

        self.assertEqual(
            {"ok": False, "state": "IDLE", "error": "decode exploded", "code": "command_failed"},
            response,
        )

    def test_every_category_maps_to_its_own_distinct_code(self) -> None:
        codes = [code.value for code in CommandCode]

        self.assertEqual(11, len(codes))
        self.assertEqual(
            {
                "busy",
                "unsupported_command",
                "malformed_request",
                "over_capacity",
                "not_permitted",
                "shutting_down",
                "command_failed",
                # Speech output. Three refusals a caller has to tell apart --
                # turn it on, install what is missing, wait for the other
                # session -- and the category an interruption reports itself as.
                "speech_disabled",
                "speech_unavailable",
                "speech_session_in_use",
                "speech_interrupted",
            },
            set(codes),
        )

    def test_the_original_seven_categories_keep_their_codes(self) -> None:
        """Every existing caller branches on these, so none of them may move."""
        self.assertEqual("busy", CommandCode.BUSY)
        self.assertEqual("unsupported_command", CommandCode.UNSUPPORTED_COMMAND)
        self.assertEqual("malformed_request", CommandCode.MALFORMED_REQUEST)
        self.assertEqual("over_capacity", CommandCode.OVER_CAPACITY)
        self.assertEqual("not_permitted", CommandCode.NOT_PERMITTED)
        self.assertEqual("shutting_down", CommandCode.SHUTTING_DOWN)
        self.assertEqual("command_failed", CommandCode.COMMAND_FAILED)

    def test_successful_responses_carry_no_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            daemon = self._daemon(temp_dir)
            status = daemon.handle_command("status")
            started = daemon.handle_command("toggle")
            finished = daemon.handle_command("toggle")

        for response in (status, started, finished):
            self.assertTrue(response["ok"])
            self.assertNotIn("code", response)


class RequestShapeTests(ServedDaemonTests):
    def test_a_payload_that_is_not_an_object_is_answered(self) -> None:
        _daemon, socket_path = self.serve()

        responses = [
            json.loads(send_payload(socket_path, payload))
            for payload in (b"[1, 2]\n", b'"hi"\n', b"5\n", b"true\n")
        ]

        for response in responses:
            self.assertFalse(response["ok"])
            self.assertEqual(CommandCode.MALFORMED_REQUEST, response["code"])
            self.assertEqual("Request is not a JSON object.", response["error"])
        self.assertEqual({"ok": True, "state": "IDLE"}, send_command(str(socket_path), "status"))

    def test_a_command_name_that_is_not_text_is_answered(self) -> None:
        _daemon, socket_path = self.serve()

        response = json.loads(send_payload(socket_path, b'{"command": 5}\n'))

        self.assertFalse(response["ok"])
        self.assertEqual(CommandCode.UNSUPPORTED_COMMAND, response["code"])
        self.assertEqual({"ok": True, "state": "IDLE"}, send_command(str(socket_path), "status"))

    def test_a_request_carrying_extra_fields_runs_its_command(self) -> None:
        _daemon, socket_path = self.serve()

        response = json.loads(send_payload(socket_path, b'{"command": "status", "extra": 1}\n'))

        self.assertEqual({"ok": True, "state": "IDLE"}, response)


class AnsweredConnectionTests(ServedDaemonTests):
    def test_invalid_json_is_answered(self) -> None:
        _daemon, socket_path = self.serve()

        response = json.loads(send_payload(socket_path, b"not json\n"))

        self.assertFalse(response["ok"])
        self.assertEqual(CommandCode.MALFORMED_REQUEST, response["code"])
        self.assertIn("not valid JSON", response["error"])
        self.assertEqual({"ok": True, "state": "IDLE"}, send_command(str(socket_path), "status"))

    def test_invalid_text_is_answered(self) -> None:
        _daemon, socket_path = self.serve()

        response = json.loads(send_payload(socket_path, b"\xff\xfe\n"))

        self.assertFalse(response["ok"])
        self.assertEqual(CommandCode.MALFORMED_REQUEST, response["code"])
        self.assertEqual("Request is not valid UTF-8 text.", response["error"])
        self.assertEqual({"ok": True, "state": "IDLE"}, send_command(str(socket_path), "status"))

    def test_a_request_that_never_arrives_is_answered(self) -> None:
        with patch("murmly.daemon.COMMAND_TIMEOUT_SECONDS", 0.2):
            _daemon, socket_path = self.serve()

            response = json.loads(send_payload(socket_path, b""))

            self.assertFalse(response["ok"])
            self.assertEqual(CommandCode.MALFORMED_REQUEST, response["code"])
            self.assertIn("No request arrived within", response["error"])
            self.assertEqual({"ok": True, "state": "IDLE"}, send_command(str(socket_path), "status"))

    def test_an_unexpected_failure_in_command_handling_is_answered(self) -> None:
        daemon, socket_path = self.serve()

        def explode(command: str) -> dict[str, object]:
            raise ZeroDivisionError("nothing divides")

        daemon.handle_command = explode
        with self.assertLogs("murmly.daemon", level="WARNING"):
            response = json.loads(send_payload(socket_path, b'{"command": "status"}\n'))
        del daemon.handle_command

        self.assertFalse(response["ok"])
        self.assertEqual(CommandCode.COMMAND_FAILED, response["code"])
        self.assertIn("nothing divides", response["error"])
        self.assertEqual({"ok": True, "state": "IDLE"}, send_command(str(socket_path), "status"))

    def test_a_connection_over_capacity_is_answered(self) -> None:
        daemon, socket_path = self.serve()
        for _index in range(MAX_COMMAND_WORKERS):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(client.close)
            client.connect(str(socket_path))
        self.assertEqual(MAX_COMMAND_WORKERS, self.wait_for_connections(daemon, MAX_COMMAND_WORKERS))

        response = json.loads(send_payload(socket_path, b'{"command": "status"}\n'))

        self.assertFalse(response["ok"])
        self.assertEqual(CommandCode.OVER_CAPACITY, response["code"])
        self.assertIn(str(MAX_COMMAND_WORKERS), response["error"])

    def test_shutdown_answers_a_command_whose_request_was_read(self) -> None:
        daemon, socket_path = self.serve()
        dispatched = threading.Event()

        def wait_for_shutdown(command: str) -> dict[str, object]:
            dispatched.set()
            daemon._shutdown.wait(timeout=5)
            raise RuntimeError("capture interrupted")

        daemon.handle_command = wait_for_shutdown
        received: list[bytes] = []
        caller = threading.Thread(
            target=lambda: received.append(send_payload(socket_path, b'{"command": "toggle"}\n')),
            daemon=True,
        )
        caller.start()
        self.assertTrue(dispatched.wait(timeout=3))

        daemon.shutdown()
        caller.join(timeout=5)

        self.assertEqual(1, len(received))
        response = json.loads(received[0])
        self.assertFalse(response["ok"])
        self.assertEqual(CommandCode.SHUTTING_DOWN, response["code"])

    def test_shutdown_answers_a_command_that_outlives_the_drain(self) -> None:
        # The case the drain alone cannot cover, and the one that actually
        # happens: a transcription runs for seconds, so a service restart lands
        # while the command is still running and the drain expires under it.
        with patch("murmly.daemon.SHUTDOWN_DRAIN_SECONDS", 0.05):
            daemon, socket_path = self.serve()
            dispatched = threading.Event()
            finish = threading.Event()

            def slow_command(command: str) -> dict[str, object]:
                dispatched.set()
                finish.wait(timeout=5)
                return {"ok": True, "state": "DONE", "text": "too late", "delivered": True}

            daemon.handle_command = slow_command
            frames: list[bytes] = []
            caller = threading.Thread(
                target=lambda: frames.append(
                    send_and_read_to_close(socket_path, b'{"command": "toggle"}\n')
                ),
                daemon=True,
            )
            caller.start()
            self.assertTrue(dispatched.wait(timeout=3))

            daemon.shutdown()
            caller.join(timeout=5)
            # Released only after shutdown, so the command really did outlive the
            # drain rather than finishing inside it.
            finish.set()

        self.assertEqual(1, len(frames))
        received = frames[0]
        self.assertEqual(1, received.count(b"\n"), f"expected exactly one response, got {received!r}")
        response = json.loads(received)
        self.assertFalse(response["ok"])
        self.assertEqual(CommandCode.SHUTTING_DOWN, response["code"])
        self.assertEqual("Murmly is shutting down.", response["error"])

    def test_a_command_that_finishes_inside_the_drain_keeps_its_own_response(self) -> None:
        daemon, socket_path = self.serve()
        dispatched = threading.Event()

        def prompt_command(command: str) -> dict[str, object]:
            dispatched.set()
            return {"ok": True, "state": "DONE", "text": "in time", "delivered": True}

        daemon.handle_command = prompt_command
        frames: list[bytes] = []
        caller = threading.Thread(
            target=lambda: frames.append(
                send_and_read_to_close(socket_path, b'{"command": "toggle"}\n')
            ),
            daemon=True,
        )
        caller.start()
        self.assertTrue(dispatched.wait(timeout=3))
        caller.join(timeout=5)

        daemon.shutdown()

        self.assertEqual(1, len(frames))
        response = json.loads(frames[0])
        self.assertTrue(response["ok"])
        self.assertEqual("in time", response["text"])
        self.assertNotIn("code", response)

    def test_a_request_is_owed_an_answer_from_the_moment_its_bytes_arrive(self) -> None:
        # Closes the window between a request arriving and its command being
        # dispatched. Registering only after the decode would let shutdown land
        # in that gap and close on a connection it already owes a response.
        daemon, _socket_path = self.serve()
        for payload, failure in (
            (b"not json\n", json.JSONDecodeError),
            (b"x" * (MAX_COMMAND_BYTES + 1), RequestError),
        ):
            with self.subTest(payload=payload[:12]):
                server_side, client_side = socket.socketpair()
                self.addCleanup(client_side.close)
                self.addCleanup(server_side.close)
                client_side.sendall(payload)

                with self.assertRaises(failure):
                    daemon._read_request(server_side)

                with daemon._connections_lock:
                    self.assertIn(server_side, daemon._answering)
                    # Registered by hand, so discharged by hand: left owed, this
                    # connection makes every later shutdown wait out its drain on
                    # a socket this test has already closed.
                    daemon._answering.discard(server_side)

    def test_a_peer_that_never_reads_does_not_stop_the_daemon(self) -> None:
        _daemon, socket_path = self.serve()
        silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(silent.close)
        silent.connect(str(socket_path))
        silent.sendall(b'{"command": "status"}\n')

        self.assertEqual({"ok": True, "state": "IDLE"}, send_command(str(socket_path), "status"))

    def test_a_client_that_receives_nothing_raises_a_named_type(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            socket_path = Path(temp_dir) / "murmly.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(server.close)
            server.bind(str(socket_path))
            server.listen()
            stop = threading.Event()

            def accept_and_close() -> None:
                server.settimeout(0.2)
                while not stop.is_set():
                    try:
                        connection, _address = server.accept()
                    except socket.timeout:
                        continue
                    # Read first: closing on an unread request resets the
                    # connection, which is a different failure from the one a
                    # daemon that dies after reading produces.
                    with connection:
                        connection.settimeout(1)
                        try:
                            connection.recv(4_096)
                        except OSError:
                            pass

            thread = threading.Thread(target=accept_and_close, daemon=True)
            thread.start()
            try:
                with self.assertRaises(DaemonNotRespondingError):
                    send_command(str(socket_path), "status")
            finally:
                stop.set()
                thread.join(timeout=3)


    def test_shutdown_waits_for_a_response_that_is_claimed_but_not_yet_written(self) -> None:
        # The window between taking the claim and the bytes reaching the kernel.
        # A drain that treated the claim as the answer would find nothing left to
        # wait for and close on a response already on its way.
        daemon, socket_path = self.serve()
        claimed = threading.Event()
        take_claim = daemon._claim_response

        def stall_after_claiming(connection: socket.socket) -> bool:
            taken = take_claim(connection)
            if taken:
                claimed.set()
                time.sleep(0.15)
            return taken

        daemon._claim_response = stall_after_claiming
        frames: list[bytes] = []
        caller = threading.Thread(
            target=lambda: frames.append(
                send_and_read_to_close(socket_path, b'{"command": "status"}\n')
            ),
            daemon=True,
        )
        caller.start()
        self.assertTrue(claimed.wait(timeout=3))

        daemon.shutdown()
        caller.join(timeout=5)

        self.assertEqual(1, len(frames))
        received = frames[0]
        self.assertEqual(1, received.count(b"\n"), f"expected one response, got {received!r}")
        self.assertEqual({"ok": True, "state": "IDLE"}, json.loads(received))

    def test_shutdown_waits_for_a_write_that_outlives_the_drain(self) -> None:
        # The drain expires while the worker holds the claim. Shutdown cannot
        # answer for that connection -- the worker is about to -- and must not
        # close on it either, so the wait after the answer is the only thing
        # keeping the response whole.
        daemon, socket_path = self.serve()
        claimed = threading.Event()
        answered = threading.Event()
        release = threading.Event()
        take_claim = daemon._claim_response
        answer_the_rest = daemon._answer_the_rest

        def stall_after_claiming(connection: socket.socket) -> bool:
            taken = take_claim(connection)
            if taken:
                claimed.set()
                release.wait(timeout=5)
            return taken

        def note_the_answer() -> None:
            answer_the_rest()
            answered.set()

        daemon._claim_response = stall_after_claiming
        daemon._answer_the_rest = note_the_answer
        frames: list[bytes] = []
        caller = threading.Thread(
            target=lambda: frames.append(
                send_and_read_to_close(socket_path, b'{"command": "status"}\n')
            ),
            daemon=True,
        )
        caller.start()
        self.assertTrue(claimed.wait(timeout=3))

        shutting_down = threading.Thread(target=daemon.shutdown, daemon=True)
        shutting_down.start()
        # Released only once shutdown has answered everything it could, so the
        # write lands after that point -- the whole window the second wait is for.
        self.assertTrue(answered.wait(timeout=5))
        release.set()
        shutting_down.join(timeout=5)
        caller.join(timeout=5)

        self.assertEqual(1, len(frames))
        received = frames[0]
        self.assertEqual(1, received.count(b"\n"), f"expected one response, got {received!r}")
        self.assertEqual({"ok": True, "state": "IDLE"}, json.loads(received))

    def test_a_peer_that_closes_before_the_response_does_not_stop_the_daemon(self) -> None:
        # One of the two connections that cannot be answered. Murmly discards the
        # response it produced and goes on serving.
        _daemon, socket_path = self.serve()
        early = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        early.connect(str(socket_path))
        early.sendall(b'{"command": "status"}\n')
        early.close()

        self.assertEqual({"ok": True, "state": "IDLE"}, send_command(str(socket_path), "status"))

    def test_a_peer_that_never_reads_is_given_up_on_within_the_bound(self) -> None:
        # A response small enough to fit the socket buffer never waits on anyone,
        # so the bound is only exercised by one too large to fit.
        with patch("murmly.daemon.COMMAND_TIMEOUT_SECONDS", 0.2):
            daemon, socket_path = self.serve()
            answer_command = daemon.handle_command

            def oversized(command: str) -> dict[str, object]:
                if command != "toggle":
                    return answer_command(command)
                return {"ok": True, "state": "IDLE", "text": "x" * (2 << 20), "delivered": True}

            daemon.handle_command = oversized
            silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(silent.close)
            silent.connect(str(socket_path))
            silent.sendall(b'{"command": "toggle"}\n')
            started = time.monotonic()

            self.assertEqual(0, self.wait_for_connections(daemon, 0))
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 2.0)
            self.assertEqual(
                {"ok": True, "state": "IDLE"}, send_command(str(socket_path), "status")
            )

    def test_a_daemon_that_holds_the_connection_open_is_reported_not_waited_on(self) -> None:
        # The other half of "no command terminates with an unhandled error": a
        # daemon that answers nothing at all is as unhelpful as one that closes,
        # and a hotkey press has nowhere to show a caller that never returns.
        with tempfile.TemporaryDirectory() as temp_dir:
            socket_path = Path(temp_dir) / "murmly.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(server.close)
            server.bind(str(socket_path))
            server.listen()
            stop = threading.Event()
            held: list[socket.socket] = []

            def accept_and_hold() -> None:
                server.settimeout(0.2)
                while not stop.is_set():
                    try:
                        connection, _address = server.accept()
                    except socket.timeout:
                        continue
                    # Kept open and unanswered, which is the case a close cannot
                    # produce: the caller has no empty read to notice.
                    held.append(connection)
                    connection.settimeout(1)
                    try:
                        connection.recv(4_096)
                    except OSError:
                        pass

            thread = threading.Thread(target=accept_and_hold, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 3)
            self.addCleanup(stop.set)
            started = time.monotonic()

            with self.assertRaises(DaemonNotRespondingError) as failure:
                send_command(str(socket_path), "status", response_timeout=0.2)

            elapsed = time.monotonic() - started
            for connection in held:
                connection.close()

        # Bounded by the response timeout specifically, not by the connect
        # timeout leaking into the read: those are different waits.
        self.assertLess(elapsed, 1.0)
        self.assertIn("did not respond", str(failure.exception))


class SocketAccessTests(ServedDaemonTests):
    def test_the_socket_and_every_directory_murmly_creates_are_owner_only(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        directory = Path(temp_dir.name) / "created" / "nested"
        config = MurmlyConfig(
            socket_path=directory / "murmly.sock",
            config_path=Path(temp_dir.name) / "config.toml",
            overlay_enabled=False,
        )

        _daemon, socket_path = self.serve_config(config)

        self.assertEqual(0o600, stat.S_IMODE(socket_path.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(directory.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(directory.parent.stat().st_mode))

    def test_peer_identity_reports_the_connecting_account(self) -> None:
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)

        self.assertEqual(os.getuid(), read_peer_identity(left))

    def test_a_peer_from_the_same_account_is_served(self) -> None:
        _daemon, socket_path = self.serve()

        self.assertEqual({"ok": True, "state": "IDLE"}, send_command(str(socket_path), "status"))

    def test_a_peer_from_another_account_is_refused_and_takes_no_capacity(self) -> None:
        # A genuine cross-account connection needs a second account and root,
        # which the suite does not have and must not need, so the comparison is
        # substituted. More refusals than there are worker slots are sent: if one
        # consumed a slot, the permitted command that follows could not be served.
        refusals = MAX_COMMAND_WORKERS + 4
        seen: list[socket.socket] = []

        def foreign_until_permitted(connection: socket.socket) -> int:
            seen.append(connection)
            return os.getuid() + 1 if len(seen) <= refusals else os.getuid()

        _daemon, socket_path = self.serve(peer_identity=foreign_until_permitted)

        for _index in range(refusals):
            response = json.loads(send_payload(socket_path, b'{"command": "toggle"}\n'))
            self.assertFalse(response["ok"])
            self.assertEqual(CommandCode.NOT_PERMITTED, response["code"])

        self.assertEqual({"ok": True, "state": "IDLE"}, send_command(str(socket_path), "status"))

    def test_a_refused_peer_runs_no_command(self) -> None:
        session = DummySession()
        _daemon, socket_path = self.serve(session, peer_identity=lambda connection: os.getuid() + 1)

        json.loads(send_payload(socket_path, b'{"command": "toggle"}\n'))

        self.assertEqual(0, session.started)

    def test_the_daemon_refuses_a_socket_path_other_accounts_can_write(self) -> None:
        for mode in (0o777, 0o770):
            with self.subTest(mode=oct(mode)), tempfile.TemporaryDirectory() as temp_dir:
                directory = Path(temp_dir) / "shared"
                directory.mkdir()
                directory.chmod(mode)
                socket_path = directory / "murmly.sock"
                config = MurmlyConfig(
                    socket_path=socket_path,
                    config_path=Path(temp_dir) / "config.toml",
                    overlay_enabled=False,
                )

                with self.assertRaises(DaemonStartupError) as refusal:
                    MurmlyDaemon(config, session=DummySession())

                message = str(refusal.exception)
                self.assertIn(str(socket_path), message)
                self.assertIn("XDG_RUNTIME_DIR", message)
                self.assertIn(str(directory), message)
                self.assertFalse(socket_path.exists())

    def test_a_refused_socket_path_is_not_unlinked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "shared"
            directory.mkdir()
            occupant = directory / "murmly.sock"
            occupant.write_text("not ours", encoding="utf-8")
            directory.chmod(0o777)
            config = MurmlyConfig(
                socket_path=occupant,
                config_path=Path(temp_dir) / "config.toml",
                overlay_enabled=False,
            )

            with self.assertRaises(DaemonStartupError):
                MurmlyDaemon(config, session=DummySession())

            self.assertEqual("not ours", occupant.read_text(encoding="utf-8"))

    def test_the_daemon_serves_a_directory_others_can_read_but_not_write(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        directory = Path(temp_dir.name) / "readable"
        directory.mkdir()
        directory.chmod(0o755)
        config = MurmlyConfig(
            socket_path=directory / "murmly.sock",
            config_path=Path(temp_dir.name) / "config.toml",
            overlay_enabled=False,
        )

        _daemon, socket_path = self.serve_config(config)

        self.assertEqual({"ok": True, "state": "IDLE"}, send_command(str(socket_path), "status"))

    def test_the_default_socket_path_is_served(self) -> None:
        runtime_dir = tempfile.TemporaryDirectory()
        self.addCleanup(runtime_dir.cleanup)
        socket_path = default_socket_path({"XDG_RUNTIME_DIR": runtime_dir.name})
        config = MurmlyConfig(
            socket_path=socket_path,
            config_path=Path(runtime_dir.name) / "config.toml",
            overlay_enabled=False,
        )

        _daemon, served_path = self.serve_config(config)

        self.assertEqual(Path(runtime_dir.name) / "murmly.sock", served_path)
        self.assertEqual({"ok": True, "state": "IDLE"}, send_command(str(served_path), "status"))

    def test_the_daemon_refuses_a_private_directory_under_one_others_can_write(self) -> None:
        # The exposure is above the directory that holds the socket. Renaming
        # `wide/private` away and putting another one in its place substitutes
        # every path under it, so checking the holder alone misses this.
        with tempfile.TemporaryDirectory() as temp_dir:
            wide = Path(temp_dir) / "wide"
            wide.mkdir()
            private = wide / "private"
            private.mkdir()
            private.chmod(0o700)
            wide.chmod(0o777)
            socket_path = private / "murmly.sock"
            config = MurmlyConfig(
                socket_path=socket_path,
                config_path=Path(temp_dir) / "config.toml",
                overlay_enabled=False,
            )

            with self.assertRaises(DaemonStartupError) as refusal:
                MurmlyDaemon(config, session=DummySession())

            message = str(refusal.exception)
            self.assertIn(str(wide), message)
            self.assertIn(str(socket_path), message)
            self.assertIn("XDG_RUNTIME_DIR", message)
            self.assertFalse(socket_path.exists())

    def test_a_directory_missing_under_one_others_can_write_is_refused(self) -> None:
        # Nothing exists below `wide` yet, so `wide` is where another account
        # would create the directory first and own everything under it.
        with tempfile.TemporaryDirectory() as temp_dir:
            wide = Path(temp_dir) / "wide"
            wide.mkdir()
            wide.chmod(0o777)

            detail = socket_path_detail(wide / "murmly" / "murmly.sock")

        self.assertIsNotNone(detail)
        self.assertIn(str(wide), detail)

    @unittest.skipUnless(
        Path(tempfile.gettempdir()).stat().st_mode & stat.S_ISVTX,
        "the shared temporary directory is not sticky here",
    )
    def test_a_sticky_shared_ancestor_leaves_the_path_private(self) -> None:
        # /tmp is world-writable and sticky, and every temporary directory in
        # this suite sits under it. The sticky bit is exactly what stops another
        # account renaming ours away, so it is accepted above the holder.
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertIsNone(socket_path_detail(Path(temp_dir) / "murmly.sock"))

    def test_the_sticky_bit_does_not_excuse_the_directory_holding_the_socket(self) -> None:
        # Sticky stops another account removing our entry; it does not stop one
        # creating the entry first, which is all it takes when the node is the
        # thing being created.
        with tempfile.TemporaryDirectory() as temp_dir:
            shared = Path(temp_dir) / "shared"
            shared.mkdir()
            shared.chmod(0o1777)

            detail = socket_path_detail(shared / "murmly.sock")

        self.assertIsNotNone(detail)
        self.assertIn(str(shared), detail)

    def test_a_directory_owned_by_another_account_is_not_private(self) -> None:
        # A directory this account does not own can be opened up by its owner at
        # any time, so its mode right now says nothing. The comparison is
        # substituted because the suite has one account.
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("murmly.daemon.os.getuid", return_value=os.getuid() + 1):
                detail = socket_path_detail(Path(temp_dir) / "murmly.sock")

        self.assertIsNotNone(detail)
        self.assertIn(str(temp_dir), detail)
        self.assertIn("owned by uid", detail)

    @unittest.skipIf(os.getuid() == 0, "root is not bound by directory permissions")
    def test_a_startup_failure_closes_the_overlay_the_constructor_started(self) -> None:
        # The refusal happens before the socket exists, so the unwinding that
        # closes the overlay has to sit outside the socket's own.
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        parent = Path(temp_dir.name) / "murmly"
        parent.mkdir()
        parent.chmod(0o500)
        self.addCleanup(parent.chmod, 0o700)
        socket_path = parent / "nested" / "murmly.sock"
        config = MurmlyConfig(
            socket_path=socket_path,
            config_path=Path(temp_dir.name) / "config.toml",
        )
        overlay = FakeOverlay()
        daemon = MurmlyDaemon(config, session=DummySession(), overlay=overlay)

        with self.assertRaises(DaemonStartupError) as refusal:
            daemon.serve_forever()

        self.assertIn(str(socket_path), str(refusal.exception))
        self.assertTrue(overlay.closed)

    def test_a_symlink_does_not_hide_the_directory_it_is_reached_through(self) -> None:
        # A caller opens the configured path, so the link is what it reaches
        # through. Judging only what the path resolves to leaves the directory
        # holding the link unexamined, and an account that can write there
        # replaces the link and takes the commands, whatever it pointed at.
        with tempfile.TemporaryDirectory() as temp_dir:
            wide = Path(temp_dir) / "wide"
            wide.mkdir()
            private = Path(temp_dir) / "private"
            private.mkdir(mode=0o700)
            link = wide / "link"
            link.symlink_to(private)
            wide.chmod(0o777)
            hop = Path(temp_dir) / "hop"
            hop.symlink_to(link)

            detail = socket_path_detail(link / "murmly.sock")
            chained = socket_path_detail(hop / "murmly.sock")

        self.assertIsNotNone(detail)
        self.assertIn(str(wide), detail)
        # The second link reaches the first, so the exposure is two hops away and
        # is still found.
        self.assertIsNotNone(chained)
        self.assertIn(str(wide), chained)

    def test_a_socket_path_that_cannot_be_created_is_reported_as_a_refusal(self) -> None:
        # The daemon detected this itself, so it is a refusal rather than the
        # unexpected failure the caller's backstop would otherwise report.
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        occupant = Path(temp_dir.name) / "notadir"
        occupant.write_text("", encoding="utf-8")
        config = MurmlyConfig(
            socket_path=occupant / "murmly.sock",
            config_path=Path(temp_dir.name) / "config.toml",
            overlay_enabled=False,
        )
        daemon = MurmlyDaemon(config, session=DummySession())

        with self.assertRaises(DaemonStartupError) as refusal:
            daemon.serve_forever()

        self.assertIn(str(config.socket_path), str(refusal.exception))

    def test_a_platform_without_peer_identity_is_reported_and_keeps_serving(self) -> None:
        # Refusing here would make the daemon unusable on a platform whose only
        # fault is not offering the check, so it serves and says so.
        with patch("murmly.daemon.peer_identity_supported", return_value=False):
            with self.assertLogs("murmly.daemon", level="WARNING") as logs:
                _daemon, socket_path = self.serve(peer_identity=lambda _connection: None)
                response = send_command(str(socket_path), "status")

        self.assertEqual({"ok": True, "state": "IDLE"}, response)
        self.assertTrue(
            any("cannot report the account" in line for line in logs.output),
            f"expected the startup warning, got {logs.output!r}",
        )
