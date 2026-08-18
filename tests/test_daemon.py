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
