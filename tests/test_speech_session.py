from __future__ import annotations

import json
import re
import socket
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path
from types import ModuleType

from channel_helpers import command_channel_address, connect_command_channel
from fakes import FakeSynthesizer, fake_amplitude
from module_stubs import injected_module
from murmly.audio import SoundDeviceRecorder, SoundDevicePlayer, pcm16_from_float32
from murmly.config import MurmlyConfig
from murmly.daemon import (
    MAX_SPEECH_FRAME_BYTES,
    CommandCode,
    MurmlyDaemon,
    ProcessingResult,
    SpeechSessionConnection,
    send_command,
)
from murmly.focus import WindowIdentity
from murmly.overlay import NullOverlayController
from murmly.speech import (
    EVENT_FAILED,
    EVENT_HEARD_ALL,
    EVENT_INTERRUPTED,
    EVENT_SHUTTING_DOWN,
    EVENT_STARTED,
    EVENT_TRANSCRIPT,
    SpeechEngine,
)
from murmly.win_pipe import PIPE_BUFFER_BYTES
from test_audio import FakeOutputStream, FakePortAudioError, FakeStream
from test_speech import RecordingPlayer


class DummyCaptureSession:
    """The capture session the daemon drives, with no microphone behind it."""

    def __init__(self, text: str = "hello world") -> None:
        self.started = 0
        self.stopped = 0
        self.targets_captured = 0
        self.received_targets: list[WindowIdentity | None] = []
        self.delivered_to_session: list[str] = []
        self.copied: list[str] = []
        self.text = text
        self.target = WindowIdentity(window_id=1, pid=10, window_class="editor")

    def capture_delivery_target(self) -> WindowIdentity | None:
        self.targets_captured += 1
        return self.target

    def start_recording(self) -> None:
        self.started += 1

    def stop_recording(self) -> bytes:
        self.stopped += 1
        return b"recording"

    def take_segment(self) -> bytes:
        return b"recording"

    def process_recording(self, pcm_audio: bytes, target: WindowIdentity | None = None):
        self.received_targets.append(target)
        return ProcessingResult(text=self.text, state="DONE", delivered=True)

    def process_for_session(self, pcm_audio: bytes, deliver):
        if not self.text:
            return ProcessingResult(text="", state="DONE")
        if deliver(self.text):
            self.delivered_to_session.append(self.text)
            return ProcessingResult(text=self.text, state="DONE", delivered=True)
        self.copied.append(self.text)
        return ProcessingResult(
            text=self.text,
            state="DONE",
            delivered=False,
            detail="No speech session to deliver to. Transcript copied to the clipboard.",
        )


class SessionClient:
    """A sender: declares a speech session and reads what it is told.

    Waits for the acknowledgement before sending text, which is also how a real
    sender learns the session was accepted rather than refused.
    """

    def __init__(self, socket_path: Path, timeout: float = 5.0) -> None:
        self._timeout = timeout
        self._socket = connect_command_channel(socket_path, timeout)
        self._payload = b""

    def declare(self) -> dict:
        self.send({"command": "speech_session"})
        return self.read()

    def send(self, frame: dict) -> None:
        self._socket.sendall((json.dumps(frame) + "\n").encode("utf-8"))

    def send_raw(self, payload: bytes) -> None:
        self._socket.sendall(payload)

    def read(self) -> dict | None:
        while b"\n" not in self._payload:
            try:
                chunk = self._socket.recv(4096)
            except socket.timeout:
                return None
            if not chunk:
                return None
            self._payload += chunk
        line, _, rest = self._payload.partition(b"\n")
        self._payload = rest
        return json.loads(line.decode("utf-8"))

    def read_until(self, event: str, timeout: float = 5.0) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self.read()
            if frame is None:
                return None
            if frame.get("event") == event:
                return frame
        return None

    def drain(self, timeout: float = 0.3) -> list[dict]:
        """Everything already sent, without waiting for anything more.

        `read_until` cannot answer "did this event NOT arrive": it blocks until
        the event shows up or the timeout expires, so a test written with it
        asserts only that the event eventually comes, never that it was withheld
        until it should have.
        """
        self._socket.settimeout(timeout)
        frames: list[dict] = []
        try:
            while True:
                frame = self.read()
                if frame is None:
                    return frames
                frames.append(frame)
        finally:
            self._socket.settimeout(self._timeout)

    def close(self) -> None:
        try:
            self._socket.close()
        except OSError:
            pass


class SpeechSessionHarness(unittest.TestCase):
    def serve(self, *, enabled: bool = True, session=None, engine=None, **overrides):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        config = MurmlyConfig(
            socket_path=command_channel_address(temp_dir.name),
            config_path=Path(temp_dir.name) / "config.toml",
            overlay_enabled=False,
            tts_enabled=enabled,
            **overrides,
        )
        player = RecordingPlayer()
        engine = engine or SpeechEngine(config, synthesizer=FakeSynthesizer(), player=player)
        capture = session or DummyCaptureSession()
        daemon = MurmlyDaemon(
            config,
            session=capture,
            overlay=NullOverlayController(),
            speech=engine,
        )
        thread = threading.Thread(target=daemon.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(daemon.shutdown)
        # Waiting for the socket file is not enough: it appears at bind() and
        # the daemon does not accept until listen(), several statements later,
        # so a client connecting in between is refused. Waiting for a connection
        # to be accepted is the condition the tests actually depend on.
        deadline = time.time() + 3
        while True:
            try:
                # A whole exchange rather than a bare connect: it proves the
                # daemon is answering, and leaves nothing half-read behind for
                # it to log as a broken connection.
                send_command(str(config.socket_path), "status")
                break
            except Exception:  # noqa: BLE001 - not up yet is the ordinary case here
                if time.time() >= deadline:
                    self.fail("daemon socket was not accepting connections")
                time.sleep(0.01)
        return daemon, config.socket_path, engine, player, capture

    def client(self, socket_path: Path) -> SessionClient:
        client = SessionClient(socket_path)
        self.addCleanup(client.close)
        return client

    def wait_for(self, predicate, timeout: float = 5.0, message: str = "condition") -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail(f"timed out waiting for {message}")


class SessionDeclarationTests(SpeechSessionHarness):
    def test_a_session_is_accepted_when_speech_output_is_enabled(self) -> None:
        _daemon, socket_path, _engine, _player, _capture = self.serve()

        acknowledgement = self.client(socket_path).declare()

        self.assertEqual({"ok": True, "session": "speech"}, acknowledgement)

    def test_a_session_is_refused_with_a_code_when_speech_output_is_disabled(self) -> None:
        _daemon, socket_path, _engine, player, _capture = self.serve(enabled=False)

        refusal = self.client(socket_path).declare()

        self.assertFalse(refusal["ok"])
        self.assertEqual(CommandCode.SPEECH_DISABLED, refusal["code"])
        self.assertEqual(0, player.started, "an audio device was opened for a refused session")

    def test_a_refused_session_receives_exactly_one_response_and_is_closed(self) -> None:
        _daemon, socket_path, _engine, _player, _capture = self.serve(enabled=False)
        client = self.client(socket_path)

        client.declare()

        self.assertIsNone(client.read(), "a refused session was sent a second frame")

    def test_a_second_session_is_refused_while_the_first_is_open(self) -> None:
        _daemon, socket_path, _engine, _player, _capture = self.serve()
        self.client(socket_path).declare()

        refusal = self.client(socket_path).declare()

        self.assertFalse(refusal["ok"])
        self.assertEqual(CommandCode.SPEECH_SESSION_IN_USE, refusal["code"])

    def test_a_session_can_be_opened_again_after_the_first_closes(self) -> None:
        daemon, socket_path, _engine, _player, _capture = self.serve()
        first = SessionClient(socket_path)
        first.declare()
        first.close()
        self.wait_for(lambda: daemon._speech_session is None, message="the first session to end")

        self.assertEqual({"ok": True, "session": "speech"}, self.client(socket_path).declare())

    def test_an_unavailable_engine_is_refused_with_its_reason(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        config = MurmlyConfig(
            socket_path=command_channel_address(temp_dir.name),
            config_path=Path(temp_dir.name) / "config.toml",
            overlay_enabled=False,
            tts_enabled=True,
        )
        engine = SpeechEngine(
            config,
            synthesizer=FakeSynthesizer(available=False, unavailable_reason="espeak-ng is missing"),
            player=RecordingPlayer(),
        )
        _daemon, socket_path, _engine, _player, _capture = self.serve(engine=engine)

        refusal = self.client(socket_path).declare()

        self.assertFalse(refusal["ok"])
        self.assertEqual(CommandCode.SPEECH_UNAVAILABLE, refusal["code"])
        self.assertIn("espeak-ng is missing", refusal["error"])

    def test_a_device_that_will_not_open_refuses_the_session_with_the_reason(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        config = MurmlyConfig(
            socket_path=command_channel_address(temp_dir.name),
            config_path=Path(temp_dir.name) / "config.toml",
            overlay_enabled=False,
            tts_enabled=True,
        )
        engine = SpeechEngine(
            config,
            synthesizer=FakeSynthesizer(),
            player=RecordingPlayer(start_error=RuntimeError("Unable to open an audio output.")),
        )
        _daemon, socket_path, *_ = self.serve(engine=engine)

        refusal = self.client(socket_path).declare()

        self.assertEqual(CommandCode.SPEECH_UNAVAILABLE, refusal["code"])
        self.assertIn("Unable to open an audio output", refusal["error"])


class SessionFrameTests(SpeechSessionHarness):
    def test_text_sent_over_time_is_spoken_in_order(self) -> None:
        _daemon, socket_path, _engine, player, _capture = self.serve()
        client = self.client(socket_path)
        client.declare()

        for name in ("one", "two", "three"):
            client.send({"command": "speak", "name": name, "text": f"Sentence {name}."})
        self.wait_for(lambda: len(player.written) >= 3, message="three units produced")
        player.play()

        started = [client.read_until(EVENT_STARTED)["name"] for _ in range(3)]
        self.assertEqual(["one", "two", "three"], started)

    def test_everything_heard_is_reported_only_after_the_end_marker(self) -> None:
        _daemon, socket_path, _engine, player, _capture = self.serve()
        client = self.client(socket_path)
        client.declare()

        client.send({"command": "speak", "name": "only", "text": "The only thing."})
        self.wait_for(lambda: player.frames_written > 0, message="audio produced")
        player.play()
        client.read_until(EVENT_STARTED)

        # Everything queued has been heard, but the sender has not finished. The
        # assertion below alone passes with the end-of-input check removed
        # entirely, because it only ever waits for the event to arrive.
        early = client.drain()
        self.assertEqual(
            [],
            [f for f in early if f.get("event") == EVENT_HEARD_ALL],
            "heard-all arrived before the sender said it had finished",
        )

        client.send({"command": "end"})

        self.assertIsNotNone(client.read_until(EVENT_HEARD_ALL), "no heard-all event arrived")

    def test_speaking_again_after_the_end_marker_reopens_the_exchange(self) -> None:
        """The marker latched for the life of the session.

        A sender that said `end` and then sent more was told everything was
        heard the moment that new text drained -- for a batch it had never
        ended -- because nothing ever cleared the marker.
        """
        _daemon, socket_path, _engine, player, _capture = self.serve()
        client = self.client(socket_path)
        client.declare()

        client.send({"command": "speak", "name": "first", "text": "The first thing."})
        self.wait_for(lambda: player.frames_written > 0, message="audio produced")
        player.play()
        client.send({"command": "end"})
        self.assertIsNotNone(client.read_until(EVENT_HEARD_ALL), "no heard-all for the first")

        client.send({"command": "speak", "name": "second", "text": "The second thing."})
        self.wait_for(lambda: player.pending_frames > 0, message="the second unit")
        player.play()

        self.assertEqual(
            [],
            [f for f in client.drain() if f.get("event") == EVENT_HEARD_ALL],
            "heard-all for a batch the sender never ended",
        )

    def test_a_speak_frame_without_a_name_is_reported_and_not_fatal(self) -> None:
        _daemon, socket_path, _engine, player, _capture = self.serve()
        client = self.client(socket_path)
        client.declare()

        client.send({"command": "speak", "text": "Nameless."})
        refusal = client.read()

        self.assertFalse(refusal["ok"])
        self.assertEqual(CommandCode.MALFORMED_REQUEST, refusal["code"])

        client.send({"command": "speak", "name": "named", "text": "Named."})
        self.wait_for(lambda: player.frames_written > 0, message="the session to still work")

    def test_an_unreadable_frame_is_reported_and_the_session_continues(self) -> None:
        _daemon, socket_path, _engine, player, _capture = self.serve()
        client = self.client(socket_path)
        client.declare()

        client.send_raw(b"{not json\n")
        refusal = client.read()

        self.assertEqual(CommandCode.MALFORMED_REQUEST, refusal["code"])
        client.send({"command": "speak", "name": "after", "text": "Still working."})
        self.wait_for(lambda: player.frames_written > 0, message="the session to still work")

    def test_a_frame_beyond_the_speech_bound_is_refused(self) -> None:
        _daemon, socket_path, _engine, _player, _capture = self.serve()
        client = self.client(socket_path)
        client.declare()

        client.send_raw(b'{"command": "speak", "name": "big", "text": "')
        client.send_raw(b"x" * (MAX_SPEECH_FRAME_BYTES + 16))
        refusal = client.read()

        self.assertEqual(CommandCode.MALFORMED_REQUEST, refusal["code"])
        self.assertIn(str(MAX_SPEECH_FRAME_BYTES), refusal["error"])

    def test_the_existing_command_bound_is_unchanged_for_a_speech_frame_size(self) -> None:
        """A session's bound is its own; `status` and `toggle` keep 4096."""
        from murmly.daemon import MAX_COMMAND_BYTES

        self.assertEqual(4_096, MAX_COMMAND_BYTES)
        self.assertGreater(MAX_SPEECH_FRAME_BYTES, MAX_COMMAND_BYTES)

    def test_an_unsupported_session_command_is_reported(self) -> None:
        _daemon, socket_path, _engine, _player, _capture = self.serve()
        client = self.client(socket_path)
        client.declare()

        client.send({"command": "sing"})

        self.assertEqual(CommandCode.UNSUPPORTED_COMMAND, client.read()["code"])


class OneShotCommandTests(SpeechSessionHarness):
    def test_status_and_toggle_still_answer_with_a_session_open(self) -> None:
        _daemon, socket_path, _engine, _player, capture = self.serve()
        self.client(socket_path).declare()

        status = send_command(str(socket_path), "status")
        toggle = send_command(str(socket_path), "toggle")

        self.assertTrue(status["ok"])
        self.assertEqual({"ok": True, "state": "LISTENING"}, toggle)
        self.assertEqual(1, capture.started)

    def test_a_session_does_not_consume_a_command_worker_slot(self) -> None:
        daemon, socket_path, _engine, _player, _capture = self.serve()
        self.client(socket_path).declare()
        self.wait_for(
            lambda: daemon._speech_session is not None, message="the session to be registered"
        )

        # Every one of the eight slots must still be free: an exchange between a
        # person and a sender lasts as long as it lasts, and state queries have
        # to keep working throughout.
        taken = [daemon._worker_slots.acquire(blocking=False) for _ in range(8)]
        for _ in taken:
            daemon._worker_slots.release()

        self.assertTrue(all(taken))

    def test_a_one_shot_command_still_gets_exactly_one_response(self) -> None:
        _daemon, socket_path, _engine, _player, _capture = self.serve()
        self.client(socket_path).declare()

        client = connect_command_channel(socket_path, 3)
        try:
            client.sendall(b'{"command": "status"}\n')
            payload = b""
            while not payload.endswith(b"\n"):
                chunk = client.recv(4096)
                if not chunk:
                    break
                payload += chunk
            self.assertTrue(json.loads(payload)["ok"])
            self.assertEqual(b"", client.recv(4096), "a second frame reached a one-shot caller")
        finally:
            client.close()

    def test_status_reports_the_state_that_means_output_is_active(self) -> None:
        _daemon, socket_path, _engine, player, _capture = self.serve()
        client = self.client(socket_path)
        client.declare()

        client.send({"command": "speak", "name": "one", "text": "A sentence being spoken."})
        self.wait_for(
            lambda: send_command(str(socket_path), "status")["state"] == "SPEAKING",
            message="the SPEAKING state",
        )

        # SPEAKING is true while a unit is merely queued, so it does not mean any
        # audio exists yet. Playing on that signal alone consumes an empty device
        # and leaves the real audio unplayed, and heard_all never arrives.
        self.wait_for(lambda: player.frames_written > 0, message="audio produced")
        player.play()
        client.send({"command": "end"})
        client.read_until(EVENT_HEARD_ALL)
        self.wait_for(
            lambda: send_command(str(socket_path), "status")["state"] == "IDLE",
            message="a return to IDLE",
        )


class SessionLifetimeTests(SpeechSessionHarness):
    def test_speech_stops_and_the_queue_is_discarded_when_the_connection_closes(self) -> None:
        daemon, socket_path, engine, player, _capture = self.serve()
        client = SessionClient(socket_path)
        client.declare()
        for index in range(5):
            client.send({"command": "speak", "name": f"n{index}", "text": "A sentence here."})
        self.wait_for(lambda: player.frames_written > 0, message="audio produced")

        client.close()
        self.wait_for(lambda: not engine.active, message="the engine to stop")
        # `active` is cleared at the head of end(), before the playback thread is
        # joined, so a count taken on it alone can still be moved by a chunk
        # already inside the producer. Closing the device is the last thing end()
        # does, after the join, which is what makes the count below stable.
        self.wait_for(lambda: not player.active, message="the output device to close")
        written = player.frames_written
        time.sleep(0.1)

        self.assertEqual(written, player.frames_written, "speech outlived its sender")
        self.assertFalse(player.active, "an audio device was left open")

    def test_a_session_that_stops_reading_is_disconnected_rather_than_stalling_playback(
        self,
    ) -> None:
        daemon, socket_path, engine, player, _capture = self.serve()
        client = SessionClient(socket_path)
        client.declare()

        self.addCleanup(client.close)

        # Never reads. Every started event piles up in its outbox until the
        # backlog is reached, and then the session goes rather than the audio.
        # The broken pipe is the disconnection arriving, not a test failure.
        #
        # The outbox only grows once the writer is actually blocked -- a fixed
        # frame count is a bet on how much data that takes, and it is a bet
        # transport-dependent enough to lose: a `{"event": "started", "name":
        # ...}` frame serializes to ~37 bytes, and a UNIX socket here blocks
        # after roughly 70 of them (~2.6 KB, measured against this same
        # harness -- far below the socket's advertised SO_SNDBUF, because the
        # kernel accounts per-frame overhead against it too), while a named
        # pipe's own fixed `PIPE_BUFFER_BYTES` buffer takes a full 64 KiB
        # (~1,771 frames) before a write can block at all. 400 clears the
        # first easily and never comes close to the second -- nothing ever
        # blocks on a named pipe at that volume, so the backlog is never
        # reached and the session is never dropped. Sending until the session
        # actually goes, rather than a count picked to work on one transport,
        # is what exercises the real backlog on both. A byte budget of four
        # named-pipe buffers is the safety valve, not the expected exit: it
        # covers the ~1,835 frames (~68 KB) the pipe transport needs to both
        # fill its buffer and pile up the backlog on top, generously covers
        # whatever the client -> server direction keeps accepting in the
        # meantime while the daemon's own bounded pipe-flush-and-disconnect
        # runs (see `win_pipe.PIPE_FLUSH_TIMEOUT_SECONDS`), and still fails
        # loudly rather than hanging if neither bound is ever reached.
        byte_budget = 4 * PIPE_BUFFER_BYTES
        sent_bytes = 0
        index = 0
        while daemon._speech_session is not None and sent_bytes < byte_budget:
            frame = {"command": "speak", "name": f"n{index}", "text": "Short."}
            try:
                client.send(frame)
            except OSError:
                break
            sent_bytes += len(json.dumps(frame)) + 1
            player.play()
            index += 1
        if daemon._speech_session is not None and sent_bytes >= byte_budget:
            self.fail(
                f"sent {sent_bytes} bytes ({index} frames) without the "
                "backlogged session being disconnected"
            )

        self.wait_for(
            lambda: daemon._speech_session is None,
            timeout=10,
            message="the session to be disconnected",
        )
        self.assertFalse(engine.active, "playback outlived the session that stopped reading")

    def test_shutdown_stops_speech_and_tells_the_session(self) -> None:
        daemon, socket_path, engine, player, _capture = self.serve()
        client = self.client(socket_path)
        client.declare()
        client.send({"command": "speak", "name": "one", "text": "A sentence."})
        self.wait_for(lambda: player.frames_written > 0, message="audio produced")

        daemon.shutdown()

        self.assertIsNotNone(
            client.read_until(EVENT_SHUTTING_DOWN), "the session was not told about shutdown"
        )
        self.assertFalse(player.active, "the output stream was left open")


class BargeInTests(SpeechSessionHarness):
    def test_the_interruption_names_the_unit_playing_and_the_ones_never_started(self) -> None:
        _daemon, socket_path, engine, player, _capture = self.serve()
        client = self.client(socket_path)
        client.declare()
        for name in ("one", "two", "three", "four"):
            client.send({"command": "speak", "name": name, "text": f"Sentence {name} here."})
        # Every unit known to the engine, produced or still queued. Waiting on
        # two alone leaves the last frame possibly still in the socket, and an
        # interruption then names three units because the fourth had not
        # arrived -- the report is right and the test is early.
        self.wait_for(
            lambda: len(player.written) >= 2
            and len(player.written) + len(engine._queue.waiting) >= 4,
            message="two units produced and all four known to the engine",
        )
        player.play(player.written[0][0] + 1)
        self.wait_for(lambda: player.frames_played > player.written[0][0], message="the second unit")

        send_command(str(socket_path), "toggle")

        interruption = client.read_until(EVENT_INTERRUPTED)
        self.assertEqual("two", interruption["playing"])
        self.assertEqual(["three", "four"], interruption["pending"])
        self.assertEqual(CommandCode.SPEECH_INTERRUPTED, interruption["code"])

    def test_the_output_is_closed_before_the_microphone_opens(self) -> None:
        _daemon, socket_path, _engine, player, capture = self.serve()
        client = self.client(socket_path)
        client.declare()
        client.send({"command": "speak", "name": "one", "text": "A sentence."})
        self.wait_for(lambda: player.frames_written > 0, message="audio produced")

        send_command(str(socket_path), "toggle")

        self.assertFalse(player.active, "the output was still open when capture started")
        self.assertEqual(1, capture.started)

    def test_the_interruption_arrives_before_the_transcript(self) -> None:
        _daemon, socket_path, _engine, player, _capture = self.serve()
        client = self.client(socket_path)
        client.declare()
        client.send({"command": "speak", "name": "one", "text": "A sentence."})
        self.wait_for(lambda: player.frames_written > 0, message="audio produced")

        send_command(str(socket_path), "toggle_session")
        send_command(str(socket_path), "toggle_session")

        order = []
        for _ in range(6):
            frame = client.read()
            if frame is None:
                break
            if frame.get("event") in (EVENT_INTERRUPTED, EVENT_TRANSCRIPT):
                order.append(frame["event"])
            if EVENT_TRANSCRIPT in order:
                break

        self.assertEqual([EVENT_INTERRUPTED, EVENT_TRANSCRIPT], order)

    def test_nothing_left_unheard_is_reported_as_nothing(self) -> None:
        _daemon, socket_path, _engine, player, _capture = self.serve()
        client = self.client(socket_path)
        client.declare()
        client.send({"command": "speak", "name": "only", "text": "The only sentence."})
        self.wait_for(lambda: player.frames_written > 0, message="audio produced")
        player.play()
        client.read_until(EVENT_STARTED)

        send_command(str(socket_path), "toggle")

        interruption = client.read_until(EVENT_INTERRUPTED)
        self.assertIsNone(interruption["playing"])
        self.assertEqual([], interruption["pending"])

    def test_text_arriving_while_capture_runs_is_spoken_after_it(self) -> None:
        _daemon, socket_path, _engine, player, _capture = self.serve()
        client = self.client(socket_path)
        client.declare()
        send_command(str(socket_path), "toggle")

        client.send({"command": "speak", "name": "held", "text": "Held until you finish."})
        time.sleep(0.15)
        self.assertEqual(0, player.frames_written, "spoke over the person")

        send_command(str(socket_path), "toggle")
        self.wait_for(lambda: player.frames_written > 0, message="the held text to be spoken")

    def test_a_continuous_session_that_ends_on_a_refusal_still_releases_the_hold(self) -> None:
        """Every capture-end path owes the held text its turn, not just the toggle.

        A continuous auto-transcribe session ends on its own when delivery is
        refused. Without the release there, text queued during that recording
        waits for some later capture to end instead.
        """
        _daemon, socket_path, engine, player, _capture = self.serve()
        client = self.client(socket_path)
        client.declare()
        engine.suspend()
        client.send({"command": "speak", "name": "held", "text": "Held until you finish."})
        time.sleep(0.1)
        self.assertEqual(0, player.frames_written, "spoke over the person")

        _daemon._finish_continuous_session()

        self.wait_for(lambda: player.frames_written > 0, message="the held text to be spoken")

    def test_a_transition_that_cannot_start_still_releases_the_hold(self) -> None:
        _daemon, socket_path, engine, player, _capture = self.serve()
        client = self.client(socket_path)
        client.declare()
        engine.suspend()
        client.send({"command": "speak", "name": "held", "text": "Held until you finish."})
        time.sleep(0.1)

        from unittest.mock import patch

        with patch("threading.Thread.start", side_effect=RuntimeError("no thread")):
            _daemon._start_transition(lambda: None, "murmly-test")

        self.wait_for(lambda: player.frames_written > 0, message="the held text to be spoken")

    def test_a_hotkey_press_while_silent_starts_capture_as_it_does_today(self) -> None:
        _daemon, socket_path, _engine, _player, capture = self.serve()

        response = send_command(str(socket_path), "toggle")

        self.assertEqual({"ok": True, "state": "LISTENING"}, response)
        self.assertEqual(1, capture.started)


class TranscriptRoutingTests(SpeechSessionHarness):
    def test_the_session_hotkey_delivers_to_the_session_and_records_no_window(self) -> None:
        _daemon, socket_path, _engine, _player, capture = self.serve()
        client = self.client(socket_path)
        client.declare()

        send_command(str(socket_path), "toggle_session")
        response = send_command(str(socket_path), "toggle_session")

        self.assertTrue(response["delivered"])
        self.assertEqual(0, capture.targets_captured, "a window was recorded for a session capture")
        self.assertEqual(["hello world"], capture.delivered_to_session)
        transcript = client.read_until(EVENT_TRANSCRIPT)
        self.assertEqual("hello world", transcript["text"])

    def test_the_window_hotkey_is_unchanged_with_a_session_open(self) -> None:
        _daemon, socket_path, _engine, _player, capture = self.serve()
        client = self.client(socket_path)
        client.declare()

        send_command(str(socket_path), "toggle")
        response = send_command(str(socket_path), "toggle")

        self.assertEqual(
            {"ok": True, "state": "DONE", "text": "hello world", "delivered": True}, response
        )
        self.assertEqual(1, capture.targets_captured)
        self.assertEqual([], capture.delivered_to_session)

    def test_the_session_hotkey_with_no_session_open_copies_and_reports(self) -> None:
        _daemon, socket_path, _engine, _player, capture = self.serve()

        send_command(str(socket_path), "toggle_session")
        response = send_command(str(socket_path), "toggle_session")

        self.assertFalse(response["delivered"])
        self.assertIn("No speech session", response["detail"])
        self.assertEqual(["hello world"], capture.copied)

    def test_a_session_that_closes_mid_transcription_leaves_the_transcript_on_the_clipboard(
        self,
    ) -> None:
        daemon, socket_path, _engine, _player, capture = self.serve()
        client = SessionClient(socket_path)
        client.declare()
        self.wait_for(lambda: daemon._speech_session is not None, message="the session")

        send_command(str(socket_path), "toggle_session")
        client.close()
        self.wait_for(lambda: daemon._speech_session is None, message="the session to close")
        response = send_command(str(socket_path), "toggle_session")

        self.assertFalse(response["delivered"])
        self.assertEqual(["hello world"], capture.copied)
        self.assertEqual(0, capture.targets_captured, "a window was substituted for the session")

    def test_the_session_hotkey_response_carries_no_transcript(self) -> None:
        """A response is not the exception the spec makes for carrying one.

        The transcript goes to the session that asked for it and nowhere else.
        The hotkey process is not the recipient the person chose, and in
        continuous mode the response would accumulate the whole exchange.
        """
        _daemon, socket_path, _engine, _player, capture = self.serve()
        client = self.client(socket_path)
        client.declare()

        send_command(str(socket_path), "toggle_session")
        response = send_command(str(socket_path), "toggle_session")

        self.assertEqual("", response["text"])
        self.assertEqual(["hello world"], capture.delivered_to_session)
        self.assertEqual("hello world", client.read_until(EVENT_TRANSCRIPT)["text"])

    def test_a_transcript_the_session_could_not_take_is_reported_as_undelivered(self) -> None:
        """A session object still registered is not a session still reading.

        `send` drops what it is handed once the connection is closing, and a
        delivery reported on the existence of the object alone reaches neither
        the socket nor the clipboard: the person's words are simply lost.
        """
        daemon, socket_path, _engine, _player, capture = self.serve()
        client = SessionClient(socket_path)
        self.addCleanup(client.close)
        client.declare()
        self.wait_for(lambda: daemon._speech_session is not None, message="the session")

        send_command(str(socket_path), "toggle_session")
        # Closing, and the reader has not noticed yet: the socket is still open,
        # so the session stays registered while every frame it is handed from
        # here on is dropped. Calling close() instead would take the session out
        # of the register and the delivery would be refused for a different
        # reason -- the one this daemon already handled.
        daemon._speech_session._closing.set()
        response = send_command(str(socket_path), "toggle_session")

        self.assertFalse(response["delivered"])
        self.assertEqual(["hello world"], capture.copied)


class EngineTeardownOrderingTests(SpeechSessionHarness):
    """Both paths that release a session must tear the engine down under the lock.

    Released first, a session declaring itself in the window is accepted, given
    the engine, and then has it closed underneath it by the call that is still
    finishing. The window is a few instructions wide and a test that tried to
    land a declaration inside it would pass on any machine that happened not to
    switch there, so what is asserted is the ordering itself.
    """

    def _observing_engine(self, config):
        class LockObservingSpeech(SpeechEngine):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.daemon = None
                self.ended_under_lock: list[bool] = []

            def end(self) -> None:
                lock = self.daemon._speech_session_lock
                # A non-reentrant lock refuses this if anyone holds it,
                # including the thread asking -- which is the case being checked.
                free = lock.acquire(blocking=False)
                if free:
                    lock.release()
                self.ended_under_lock.append(not free)
                super().end()

        return LockObservingSpeech(
            config, synthesizer=FakeSynthesizer(), player=RecordingPlayer()
        )

    def test_a_session_closing_tears_the_engine_down_under_the_lock(self) -> None:
        config = MurmlyConfig(
            socket_path=Path(tempfile.mkdtemp()) / "murmly.sock",
            config_path=Path(tempfile.mkdtemp()) / "config.toml",
            tts_enabled=True,
        )
        engine = self._observing_engine(config)
        daemon, socket_path, _engine, _player, _capture = self.serve(engine=engine)
        engine.daemon = daemon
        client = SessionClient(socket_path)
        client.declare()
        self.wait_for(lambda: daemon._speech_session is not None, message="the session")
        engine.ended_under_lock.clear()

        client.close()

        self.wait_for(lambda: engine.ended_under_lock, message="the engine to be ended")
        self.assertEqual([True], engine.ended_under_lock)

    def test_shutdown_tears_the_engine_down_under_the_lock(self) -> None:
        config = MurmlyConfig(
            socket_path=Path(tempfile.mkdtemp()) / "murmly.sock",
            config_path=Path(tempfile.mkdtemp()) / "config.toml",
            tts_enabled=True,
        )
        engine = self._observing_engine(config)
        daemon, socket_path, _engine, _player, _capture = self.serve(engine=engine)
        engine.daemon = daemon
        client = SessionClient(socket_path)
        self.addCleanup(client.close)
        client.declare()
        self.wait_for(lambda: daemon._speech_session is not None, message="the session")
        engine.ended_under_lock.clear()

        daemon.shutdown()

        self.assertTrue(engine.ended_under_lock, "the engine was never ended")
        self.assertNotIn(
            False, engine.ended_under_lock, "the engine was ended outside the session lock"
        )


class ClosingOnFirstUse:
    """Closes the session the first time the outbox lock is taken.

    The window between `send` testing the closing flag and `send` taking the
    lock is a few instructions wide and cannot be reached by timing. Firing the
    close from inside the acquisition puts it exactly there, and every other
    acquisition -- including the close's own -- passes straight through.
    """

    def __init__(self, real, session) -> None:
        self._real = real
        self._session = session
        self.fired = False

    def __enter__(self):
        if not self.fired:
            self.fired = True
            closer = threading.Thread(target=self._session.close, kwargs={"drain": True})
            closer.start()
            closer.join(timeout=5)
        return self._real.__enter__()

    def __exit__(self, *arguments):
        return self._real.__exit__(*arguments)


class DeclarationDuringCaptureTests(SpeechSessionHarness):
    def test_a_session_cannot_be_declared_while_the_microphone_is_open(self) -> None:
        """Declaring opens the output device and drops the barge-in hold.

        Accepted during capture, that puts speech and the microphone in the room
        together -- the one arrangement the design rules out, and the reason
        there is no echo cancellation to fall back on. The refusal is the
        ordinary busy one, so a sender retries rather than treating it as fatal.
        """
        daemon, socket_path, _engine, player, capture = self.serve()

        self.assertEqual("LISTENING", send_command(str(socket_path), "toggle")["state"])
        self.assertFalse(player.active, "the barge-in was expected to close the device")

        refusal = self.client(socket_path).declare()

        self.assertFalse(refusal["ok"])
        self.assertEqual(CommandCode.BUSY, refusal["code"])
        self.assertFalse(player.active, "the output device was reopened during capture")
        self.assertEqual(0, capture.stopped, "capture was expected to be still running")

    def test_a_session_is_still_accepted_while_speech_is_playing(self) -> None:
        """SPEAKING is IDLE with speech running, and must not refuse a session."""
        daemon, socket_path, _engine, player, _capture = self.serve()
        first = SessionClient(socket_path)
        first.declare()
        first.send({"command": "speak", "name": "one", "text": "A sentence."})
        self.wait_for(lambda: player.frames_written > 0, message="audio produced")
        self.assertEqual("SPEAKING", send_command(str(socket_path), "status")["state"])

        first.close()
        self.wait_for(lambda: daemon._speech_session is None, message="the session to clear")

        self.assertEqual({"ok": True, "session": "speech"}, self.client(socket_path).declare())


class TranscriptBindingTests(SpeechSessionHarness):
    def test_a_transcript_is_not_delivered_to_a_session_that_replaced_the_bound_one(
        self,
    ) -> None:
        """Defence in depth, and deliberately so.

        Refusing declarations during capture already stops a second sender
        appearing between the key press and the transcript, so there is no
        end-to-end route to this any more. The binding is what makes the
        delivery correct on its own terms rather than by relying on that
        refusal, so it is exercised directly: a delivery resolving against
        "whichever session is open now" hands one sender the words another
        person spoke, which no clipboard fallback can undo.
        """
        daemon, socket_path, _engine, _player, capture = self.serve()
        bound = self.client(socket_path)
        bound.declare()
        self.wait_for(lambda: daemon._speech_session is not None, message="the session")
        daemon._capture_session = daemon._speech_session
        daemon._capture_destination = "session"

        # The sender that was bound goes, and a different one takes the slot.
        replacement = object()
        with daemon._speech_session_lock:
            daemon._speech_session = replacement

        delivered = daemon._deliver_to_session("the words the person said")

        self.assertFalse(delivered, "a transcript went to a session it was not for")
        self.assertEqual(
            [],
            [f for f in bound.drain() if f.get("event") == EVENT_TRANSCRIPT],
            "the bound session was sent a transcript after being replaced",
        )
        with daemon._speech_session_lock:
            daemon._speech_session = None


class ProtocolDocumentationTests(unittest.TestCase):
    def test_the_manual_documents_every_event_the_daemon_can_send(self) -> None:
        """A sender writes its handler from this table.

        An event missing from it is one nobody handles: `failed` is the one that
        says a piece of the reply is never coming, so a sender that does not
        know about it waits for audio that will not arrive.
        """
        page = (
            Path(__file__).parents[1] / "manual" / "for-developers.md"
        ).read_text(encoding="utf-8")
        # Anchored on the heading above it: the page carries two
        # "| Frame | Meaning |" tables, and the first lists what a sender may
        # send rather than what it will receive.
        table = re.search(
            r"^## Frames murmly sends\b(.*?)^## ", page, re.DOTALL | re.MULTILINE
        )
        self.assertIsNotNone(
            table, "no session event table found on the developer page"
        )

        for event in (
            EVENT_STARTED,
            EVENT_HEARD_ALL,
            EVENT_INTERRUPTED,
            EVENT_TRANSCRIPT,
            EVENT_FAILED,
            EVENT_SHUTTING_DOWN,
        ):
            self.assertIn(
                f'"{event}"',
                table.group(1),
                f"the session protocol table does not document the {event} event",
            )


class DrainedDeliveryTests(SpeechSessionHarness):
    def test_a_transcript_drained_on_close_is_reported_delivered(self) -> None:
        """Draining writes what is queued, so it must not be reported dropped.

        Waking the waiters undelivered and then sending the frame anyway is the
        clipboard copy and the socket both: the person's words arrive at the
        sender and are also pasted somewhere they did not choose.
        """
        daemon, socket_path, _engine, _player, _capture = self.serve()
        client = SessionClient(socket_path)
        self.addCleanup(client.close)
        client.declare()
        self.wait_for(lambda: daemon._speech_session is not None, message="the session")
        session = daemon._speech_session

        results: list[bool] = []
        sender = threading.Thread(
            target=lambda: results.append(
                session.send({"event": EVENT_TRANSCRIPT, "text": "the words"}, confirm=True)
            )
        )
        sender.start()
        sender.join(5)

        self.assertEqual([True], results)
        self.assertEqual("the words", client.read_until(EVENT_TRANSCRIPT)["text"])


class ResumeOrderingTests(SpeechSessionHarness):
    def test_the_state_lock_is_held_across_the_resume_that_ends_capture(self) -> None:
        """`resume` reopens the device and clears the barge-in hold.

        Run after the lock is released, a capture starting in that window has
        the loudspeaker opened underneath it -- speech and the microphone in the
        room together, with no echo cancellation behind it. Asserting the
        ordering rather than racing it: the window is a handful of bytecodes and
        a test that tried to land inside it would pass on any machine that did
        not happen to switch there.
        """
        observed: list[bool] = []

        class LockObservingSpeech(SpeechEngine):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.daemon = None

            def resume(self) -> None:
                lock = self.daemon._lock
                free = lock.acquire(blocking=False)
                if free:
                    lock.release()
                observed.append(not free)
                super().resume()

        config = MurmlyConfig(
            socket_path=Path(tempfile.mkdtemp()) / "murmly.sock",
            config_path=Path(tempfile.mkdtemp()) / "config.toml",
            tts_enabled=True,
        )
        engine = LockObservingSpeech(
            config, synthesizer=FakeSynthesizer(), player=RecordingPlayer()
        )
        daemon, socket_path, _engine, _player, _capture = self.serve(engine=engine)
        engine.daemon = daemon
        self.client(socket_path).declare()

        send_command(str(socket_path), "toggle_session")
        send_command(str(socket_path), "toggle_session")

        self.assertTrue(observed, "no resume was observed")
        self.assertNotIn(
            False, observed, "capture ended with the speech hold released outside the state lock"
        )


class ConfirmedDeliveryTests(SpeechSessionHarness):
    def test_a_transcript_is_undelivered_when_the_write_never_happens(self) -> None:
        """Queued is not delivered.

        A session that dies between the queue and the write leaves the person's
        words on neither the socket nor the clipboard, because the clipboard
        fallback was skipped on the strength of the queueing alone.
        """
        daemon, socket_path, _engine, _player, capture = self.serve()
        client = SessionClient(socket_path)
        self.addCleanup(client.close)
        client.declare()
        self.wait_for(lambda: daemon._speech_session is not None, message="the session")

        send_command(str(socket_path), "toggle_session")
        # The writer stops taking frames, so anything queued from here on is
        # never written -- but the session is still registered, so the delivery
        # is attempted and must report what actually happened.
        session = daemon._speech_session
        session._outbox_ready.clear()
        original = session._outbox_ready.set
        session._outbox_ready.set = lambda: None
        self.addCleanup(setattr, session._outbox_ready, "set", original)

        response = send_command(str(socket_path), "toggle_session")

        self.assertFalse(response["delivered"])
        self.assertEqual(["hello world"], capture.copied)


class SessionSendTests(SpeechSessionHarness):
    def test_a_frame_queued_as_the_session_closes_is_reported_as_dropped(self) -> None:
        """The flag is set before the sentinel is queued, both under the lock.

        A send that checked the flag outside the lock could append behind a
        sentinel the writer has already stopped at, report the frame as queued,
        and leave the transcript on neither the socket nor the clipboard.
        """
        daemon, socket_path, _engine, _player, _capture = self.serve()
        client = SessionClient(socket_path)
        self.addCleanup(client.close)
        client.declare()
        self.wait_for(lambda: daemon._speech_session is not None, message="the session")
        session = daemon._speech_session

        # The interleaving itself, not the settled state after it. Asserting
        # only that a send AFTER close returns False passes with the check left
        # outside the lock, because by then the flag is set either way. What
        # has to hold is that a send already past an unlocked check still
        # observes the close that lands before it takes the lock.
        closing = ClosingOnFirstUse(session._outbox_lock, session)
        session._outbox_lock = closing

        queued = session.send({"event": EVENT_TRANSCRIPT, "text": "the words"})

        self.assertTrue(closing.fired, "the close never landed inside the window")
        self.assertFalse(queued, "a frame behind the sentinel was reported as queued")

    def test_a_session_whose_device_will_not_close_is_still_released(self) -> None:
        """The reader thread carries this call, so an exception ends the session
        teardown half done: writer thread alive, both handles open, and the
        connection still counted as live for the life of the daemon.
        """

        class FailingStopPlayer(RecordingPlayer):
            """Fails the way a device that has gone away does.

            `end()` closes the device last, so the failure escapes after the
            engine state has already been torn down -- which is what makes it
            reach the reader thread rather than being caught earlier.
            """

            def stop(self) -> None:
                super().stop()
                raise RuntimeError("the output stream would not close")

        config = MurmlyConfig(
            socket_path=Path(tempfile.mkdtemp()) / "murmly.sock",
            config_path=Path(tempfile.mkdtemp()) / "config.toml",
            tts_enabled=True,
        )
        engine = SpeechEngine(
            config, synthesizer=FakeSynthesizer(), player=FailingStopPlayer()
        )
        daemon, socket_path, _engine, _player, _capture = self.serve(engine=engine)
        client = SessionClient(socket_path)
        client.declare()
        self.wait_for(lambda: daemon._speech_session is not None, message="the session")
        session = daemon._speech_session

        client.close()

        self.wait_for(lambda: daemon._speech_session is None, message="the session to clear")
        self.wait_for(
            lambda: session._writer is not None and not session._writer.is_alive(),
            message="the writer thread to exit",
        )
        self.assertNotIn(session.connection, daemon._connections)
        # And the slot is free, so the next sender is not refused forever.
        self.assertEqual({"ok": True, "session": "speech"}, self.client(socket_path).declare())


class CaptureFailureTests(SpeechSessionHarness):
    def test_a_failing_stop_releases_the_speech_hold(self) -> None:
        """Capture has ended, so the hold ends with it.

        Every other path that ends capture releases it. Without this one the
        session is silent until some later capture happens to finish cleanly,
        and `status` reports SPEAKING the whole time with the device closed.
        """

        class FailingStopSession(DummyCaptureSession):
            def stop_recording(self) -> bytes:
                raise RuntimeError("the input stream would not close")

        _daemon, socket_path, engine, player, _capture = self.serve(
            session=FailingStopSession()
        )
        client = self.client(socket_path)
        client.declare()

        send_command(str(socket_path), "toggle_session")
        response = send_command(str(socket_path), "toggle_session")

        self.assertFalse(response["ok"])
        self.wait_for(lambda: player.active, message="the output device to reopen")
        client.send({"command": "speak", "name": "after", "text": "A sentence."})
        self.wait_for(lambda: player.frames_written > 0, message="speech after the failure")

    def test_speech_that_will_not_stop_keeps_the_microphone_shut(self) -> None:
        """Both running at once is the one thing the barge-in exists to prevent.

        So a barge-in that fails refuses the capture rather than opening the
        microphone anyway, and releases the hold so the session is not wedged.
        """

        class UnstoppableSpeech(SpeechEngine):
            def suspend(self):
                # Fails the way the real one does. `suspend` takes the hold and
                # then closes the device, so a device that will not close leaves
                # speech held -- which is what the release below is about.
                self.hold()
                raise RuntimeError("the output stream would not close")

        player = RecordingPlayer()
        engine = UnstoppableSpeech(
            MurmlyConfig(
                socket_path=Path(tempfile.mkdtemp()) / "unused.sock",
                config_path=Path(tempfile.mkdtemp()) / "unused.toml",
                tts_enabled=True,
            ),
            synthesizer=FakeSynthesizer(),
            player=player,
        )
        _daemon, socket_path, _engine, _player, capture = self.serve(engine=engine)
        client = self.client(socket_path)
        client.declare()

        response = send_command(str(socket_path), "toggle_session")

        self.assertFalse(response["ok"])
        self.assertEqual(CommandCode.COMMAND_FAILED, response["code"])
        self.assertEqual(0, capture.started, "the microphone opened while speech was playing")
        self.assertEqual("IDLE", send_command(str(socket_path), "status")["state"])
        # And the hold was released, so the session is not silent until some
        # later capture happens to finish. Letting the failure escape to the
        # daemon's outer handler answers identically and fails here.
        client.send({"command": "speak", "name": "after", "text": "A sentence."})
        self.wait_for(lambda: player.frames_written > 0, message="speech after the refusal")

    def test_a_session_that_could_not_start_does_not_block_the_next_one(self) -> None:
        """A half-started session left registered refuses every later one.

        The device is open and the session recorded by the time the threads are
        started, so a failure there has to undo both or the daemon answers
        `speech_session_in_use` for the rest of its life.
        """
        _daemon, socket_path, _engine, player, _capture = self.serve()

        def refuse_to_start(self, *args, **kwargs):
            raise RuntimeError("no thread available")

        with unittest.mock.patch.object(SpeechSessionConnection, "start", refuse_to_start):
            refusal = self.client(socket_path).declare()

        self.assertFalse(refusal["ok"])
        self.assertEqual(CommandCode.COMMAND_FAILED, refusal["code"])
        self.wait_for(lambda: not player.active, message="the device to be released")

        self.assertEqual({"ok": True, "session": "speech"}, self.client(socket_path).declare())


class CaptureNeverHearsSpeechTests(unittest.TestCase):
    """The recording a barge-in produces, taken through a device that loops back.

    Whatever the output stream is pumped with reaches the open input stream, so
    an implementation that left the output open during capture would put its own
    voice in the recording and fail here.
    """

    def _loopback_module(self, opened: dict) -> ModuleType:
        module = ModuleType("sounddevice")
        module.PortAudioError = FakePortAudioError
        module.check_input_settings = lambda **_kwargs: None
        module.check_output_settings = lambda **_kwargs: None

        def query_devices(device=None, kind=None):
            entry = {
                "name": "Loopback",
                "max_input_channels": 1,
                "max_output_channels": 1,
                "default_samplerate": 24_000,
            }
            if kind in {"input", "output"}:
                return entry
            if device is None:
                return [entry]
            return entry

        module.query_devices = query_devices

        def raw_input_stream(**kwargs):
            stream = FakeStream(sample_rate_hz=kwargs["samplerate"])
            stream.callback = kwargs["callback"]
            opened["input"] = stream
            return stream

        def raw_output_stream(**kwargs):
            stream = FakeOutputStream(
                kwargs["callback"], kwargs["samplerate"], kwargs["channels"]
            )
            opened["output"] = stream
            return stream

        module.RawInputStream = raw_input_stream
        module.RawOutputStream = raw_output_stream
        return module

    def test_a_recording_made_after_a_barge_in_carries_none_of_the_speech(self) -> None:
        import sys
        from unittest.mock import patch

        opened: dict[str, object] = {}
        module = self._loopback_module(opened)
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        config = MurmlyConfig(
            socket_path=command_channel_address(temp_dir.name),
            config_path=Path(temp_dir.name) / "config.toml",
            sample_rate_hz=24_000,
            tts_enabled=True,
        )

        with injected_module("sounddevice", module):
            player = SoundDevicePlayer(config)
            recorder = SoundDeviceRecorder(config)
            engine = SpeechEngine(config, synthesizer=FakeSynthesizer(), player=player)
            engine.begin(lambda _event: None)
            engine.speak("one", "Murmly speaking its own voice.")
            deadline = time.monotonic() + 3
            while player.pending_frames == 0 and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertGreater(player.pending_frames, 0, "no speech was produced")

            output = opened["output"]
            output.pump(256)  # heard before the hotkey, with no microphone open

            engine.suspend()
            recorder.start()

            # The device would go on calling the output callback if the stream
            # were still open. It is not, so this contributes nothing.
            if not output.closed:
                produced = output.pump(4_096)
                opened["input"].callback(produced, len(produced) // 2, object(), None)

            voice = pcm16_from_float32([0.9] * 2_048)
            opened["input"].callback(voice, 2_048, object(), None)
            recording = recorder.stop()
            engine.end()

        self.assertTrue(output.closed, "the output stream was open while the microphone was")
        speech_sample = pcm16_from_float32(
            [fake_amplitude("Murmly speaking its own voice")]
        )
        self.assertNotIn(speech_sample, recording, "the recording carries Murmly's own speech")
        self.assertEqual(voice, recording)


if __name__ == "__main__":
    unittest.main()
