"""Speaker recognition inside the daemon: attribution, failing open, residency.

A real `SpeechSession` runs behind a real `MurmlyDaemon`, with the recorder, the
transcriber, the voiceprint store and the speaker model replaced by stand-ins.
Audio is built from constant-amplitude blocks, one amplitude per voice, laid out
to line up with the transcriber's timed parts. The fake embedder reads a slice's
mean amplitude back as a voice number, so a slice cut at the wrong place, at the
wrong rate or with the wrong channel count gives the wrong speaker.
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from murmly.config import MurmlyConfig
from murmly.daemon import MurmlyDaemon, SpeechSession
from murmly.focus import NullFocusObserver
from murmly.integrations import DeliveryOutcome
from murmly.overlay import OverlayHealth
from murmly.platform import OperatingSystem, PlatformProfile
from murmly.speakers import (
    CPU_PROVIDER,
    SPEAKER_MODEL_FILENAME,
    SpeakerEmbedder,
    TimedText,
    Voice,
)


GATE_TIMEOUT_SECONDS = 5.0
# Long enough that a thread which is really blocked is still blocked.
BLOCKED_SECONDS = 0.1
AMPLITUDE_STEP = 1000

ME, MILO, STRANGER_A, STRANGER_B = 1, 2, 3, 4
VECTORS = {
    ME: (1.0, 0.0, 0.0, 0.0),
    MILO: (0.0, 1.0, 0.0, 0.0),
    STRANGER_A: (0.0, 0.0, 1.0, 0.0),
    STRANGER_B: (0.0, 0.0, 0.0, 1.0),
}
ENROLLED_ME = Voice("Me", VECTORS[ME])
ENROLLED_MILO = Voice("Milo", VECTORS[MILO])
LINUX = PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64")


class FakeEmbedder:
    """The speaker model: a voice number out of a slice's mean amplitude."""

    def __init__(self) -> None:
        self.resident = False
        self.resident_detail: str | None = None
        self.loads = 0
        self.embeds = 0
        self.releases = 0
        self.load_error: Exception | None = None
        self.embed_error: Exception | None = None
        self.load_gate: threading.Event | None = None
        self.embed_gate: threading.Event | None = None
        self.load_started = threading.Event()
        self.embedding = threading.Event()
        self.release_ran = threading.Event()

    def load(self) -> None:
        self.load_started.set()
        if self.load_gate is not None:
            self.load_gate.wait(GATE_TIMEOUT_SECONDS)
        if self.load_error is not None:
            raise self.load_error
        self.loads += 1
        self.resident = True

    def embed(self, samples):
        self.embedding.set()
        if self.embed_gate is not None:
            self.embed_gate.wait(GATE_TIMEOUT_SECONDS)
        if self.embed_error is not None:
            raise self.embed_error
        if not self.resident:
            self.load()
        self.embeds += 1
        voice = round(float(np.mean(samples)) * 32_768 / AMPLITUDE_STEP)
        return np.asarray(VECTORS[voice], dtype=np.float32)

    def embed_many(self, audios):
        return [self.embed(samples) for samples in audios]

    def release(self) -> bool:
        was_resident = self.resident
        self.resident = False
        if was_resident:
            self.releases += 1
        self.release_ran.set()
        return was_resident


class FakeStore:
    def __init__(self, voices: list[Voice]) -> None:
        self.voices = voices
        self.needs_reenrolment = False
        self.detail = None
        self.refreshes = 0

    def refresh(self) -> bool:
        self.refreshes += 1
        return True


class FakeRecorder:
    def __init__(self, sample_rate_hz: int = 16_000) -> None:
        self.sample_rate_hz = sample_rate_hz
        self.finals: list[bytes] = []
        self.segments: list[bytes] = []
        self.started = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> bytes:
        return self.finals.pop(0)

    def take_segment(self) -> bytes:
        return self.segments.pop(0)

    def snapshot(self, _seconds: float) -> bytes:
        return b""


class FakeTranscriber:
    """Scripted timed parts, keyed by the audio they belong to."""

    def __init__(self) -> None:
        self.scripts: dict[bytes, list[TimedText]] = {}
        self.plain_calls = 0
        self.timed_calls = 0
        self.released = 0
        self.resident = True
        self.partials_available = False

    def begin_capture(self) -> None:
        pass

    def stop_partials(self) -> None:
        pass

    def release(self) -> bool:
        self.released += 1
        return True

    def transcribe_pcm16(self, pcm_audio: bytes, sample_rate_hz: int | None = None) -> str:
        self.plain_calls += 1
        return " ".join(part.text for part in self.scripts[pcm_audio]).strip()

    def transcribe_segments_pcm16(self, pcm_audio: bytes, sample_rate_hz: int | None = None):
        self.timed_calls += 1
        return list(self.scripts[pcm_audio])


class RecordingPaster:
    def __init__(self) -> None:
        self.pasted: list[str] = []
        self.copied: list[str] = []

    def copy(self, text: str) -> None:
        self.copied.append(text)

    def copy_and_paste(self, text: str) -> DeliveryOutcome:
        self.pasted.append(text)
        return DeliveryOutcome(True)


class RecordingOverlay:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    @property
    def health(self) -> OverlayHealth:
        return OverlayHealth(True)

    def publish_state(self, state) -> None:
        self.events.append(("state", state))

    def publish_level(self, level: float) -> None:
        pass

    def publish_partial(self, text: str) -> None:
        self.events.append(("partial", text))

    def publish_error(self, duration_ms: int = 2_000) -> None:
        self.events.append(("error", duration_ms))

    def close(self) -> None:
        pass


class SpeakerDaemonCase(unittest.TestCase):
    channels = 1
    sample_rate_hz = 16_000

    def setUp(self) -> None:
        self.recordings = 0
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.temp = Path(directory.name)
        self.data_dir = self.temp / "data"
        self.data_dir.mkdir()
        (self.data_dir / SPEAKER_MODEL_FILENAME).write_bytes(b"")
        self.store = FakeStore([ENROLLED_ME, ENROLLED_MILO])
        self.embedder = FakeEmbedder()
        self.make_store = Mock(side_effect=lambda _directory: self.store)
        self.make_embedder = Mock(side_effect=lambda _path: self.embedder)
        for target, replacement in (
            ("murmly.daemon.default_data_dir", lambda: self.data_dir),
            ("murmly.daemon.VoiceprintStore", self.make_store),
            ("murmly.daemon.SpeakerEmbedder", self.make_embedder),
            ("murmly.daemon.SoundDeviceRecorder", Mock()),
            ("murmly.daemon.FasterWhisperTranscriber", Mock()),
        ):
            patcher = patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(SpeechSession, "_create_silence_detector", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, mode: str, **overrides: object):
        config = MurmlyConfig(
            socket_path=self.temp / "murmly.sock",
            config_path=self.temp / "config.toml",
            overlay_enabled=False,
            speaker_mode=mode,
            speaker_owner="Me",
            channels=self.channels,
            sample_rate_hz=self.sample_rate_hz,
            **overrides,
        )
        session = SpeechSession(config, focus_observer=NullFocusObserver("unsupported"))
        self.recorder = FakeRecorder(self.sample_rate_hz)
        self.transcriber = FakeTranscriber()
        self.paster = RecordingPaster()
        session._recorder = self.recorder
        session._transcriber = self.transcriber
        session._paster = self.paster
        self.overlay = RecordingOverlay()
        daemon = MurmlyDaemon(config, session=session, overlay=self.overlay, profile=LINUX)
        self.addCleanup(daemon._transcription_idle.cancel)
        self.session = session
        self.daemon = daemon
        return daemon

    def audio(self, *blocks: tuple[int, str, float]) -> bytes:
        """PCM and its scripted parts for (voice, words, seconds) blocks, in order."""
        rate = self.sample_rate_hz
        pcm = b""
        parts: list[TimedText] = []
        elapsed = 0.0
        for voice, words, seconds in blocks:
            frames = int(round(seconds * rate))
            sample = np.full(frames * self.channels, voice * AMPLITUDE_STEP, dtype="<i2")
            pcm += sample.tobytes()
            parts.append(TimedText(round(elapsed, 2), round(elapsed + seconds, 2), words))
            elapsed += seconds
        # One frame past the last part, different for every recording: two calls
        # with the same blocks would otherwise be the same bytes and share a script.
        self.recordings += 1
        pcm += np.full(self.channels, self.recordings, dtype="<i2").tobytes()
        self.transcriber.scripts[pcm] = parts
        return pcm

    def settle(self, daemon: MurmlyDaemon) -> None:
        thread = daemon._segment_thread
        if thread is not None:
            thread.join(timeout=5)

    def wait_for_state(self, daemon: MurmlyDaemon, state: str) -> None:
        deadline = time.time() + 3
        while time.time() < deadline:
            if daemon.state == state:
                return
            time.sleep(0.01)
        self.fail(f"daemon never reached {state}; it is {daemon.state}")


class ModeOffTests(SpeakerDaemonCase):
    def test_off_is_the_plain_decode_with_no_store_read_and_no_embedder(self) -> None:
        daemon = self.build("off")
        pcm = self.audio((ME, "hello there", 2.0), (STRANGER_A, "good morning", 2.0))
        self.recorder.finals.append(pcm)

        daemon.handle_command("toggle")
        response = daemon.handle_command("toggle")
        status = daemon.handle_command("status")

        self.assertEqual("hello there good morning", response["text"])
        self.assertEqual(["hello there good morning"], self.paster.pasted)
        self.assertEqual((1, 0), (self.transcriber.plain_calls, self.transcriber.timed_calls))
        self.make_store.assert_not_called()
        self.make_embedder.assert_not_called()
        self.assertIs(False, status["speaker_model_resident"])

    def test_nothing_is_read_or_built_when_the_daemon_starts_in_a_speaker_mode(self) -> None:
        for mode in ("mine-only", "label-everyone"):
            with self.subTest(mode=mode):
                daemon = self.build(mode)

                status = daemon.handle_command("status")

                self.make_store.assert_not_called()
                self.make_embedder.assert_not_called()
                self.assertIs(False, status["speaker_model_resident"])
                self.assertEqual(0, self.embedder.loads)


class MineOnlyTests(SpeakerDaemonCase):
    def test_toggle_delivers_only_the_owner_and_reports_what_was_delivered(self) -> None:
        daemon = self.build("mine-only")
        self.recorder.finals.append(
            self.audio(
                (ME, "shall we start", 2.0),
                (STRANGER_A, "the television says hello", 2.0),
                (MILO, "I agree", 2.0),
                (ME, "good plan", 2.0),
            )
        )

        daemon.handle_command("toggle")
        response = daemon.handle_command("toggle")

        self.assertEqual(["shall we start good plan"], self.paster.pasted)
        self.assertEqual("shall we start good plan", response["text"])
        self.assertTrue(response["delivered"])
        self.assertEqual((0, 1), (self.transcriber.plain_calls, self.transcriber.timed_calls))

    def test_stop_mode_delivers_only_the_owner(self) -> None:
        daemon = self.build("mine-only", auto_transcribe="stop")
        self.recorder.finals.append(
            self.audio((STRANGER_A, "noise from the room", 2.0), (ME, "my words", 2.0))
        )

        daemon.handle_command("toggle")
        daemon._on_silence()
        self.wait_for_state(daemon, "IDLE")

        self.assertEqual(["my words"], self.paster.pasted)

    def test_continuous_mode_delivers_only_the_owner_per_segment(self) -> None:
        daemon = self.build("mine-only", auto_transcribe="continuous")
        self.recorder.segments.append(
            self.audio((ME, "first thing", 2.0), (STRANGER_A, "ignore me", 2.0))
        )
        self.recorder.finals.append(self.audio((ME, "last thing", 2.0)))

        daemon.handle_command("toggle")
        daemon._on_silence()
        self.settle(daemon)
        response = daemon.handle_command("toggle")

        self.assertEqual(["first thing", "last thing"], self.paster.pasted)
        self.assertEqual("first thing last thing", response["text"])
        self.assertEqual(2, response["segments"])

    def test_a_dropped_continuous_segment_keeps_listening_and_is_not_counted(self) -> None:
        daemon = self.build("mine-only", auto_transcribe="continuous")
        self.recorder.segments.append(self.audio((ME, "kept segment", 2.0)))
        self.recorder.segments.append(self.audio((STRANGER_A, "only others here", 2.0)))
        self.recorder.finals.append(self.audio((ME, "closing words", 2.0)))

        daemon.handle_command("toggle")
        daemon._on_silence()
        self.settle(daemon)
        daemon._on_silence()
        self.settle(daemon)

        self.assertEqual("LISTENING", daemon.state)
        self.assertEqual(["kept segment"], self.paster.pasted)
        self.assertEqual(["kept segment"], daemon._segments)
        self.assertNotIn(("error", 2_000), self.overlay.events)

        response = daemon.handle_command("toggle")

        self.assertEqual("kept segment closing words", response["text"])
        self.assertEqual(2, response["segments"])
        self.assertTrue(response["delivered"])
        self.assertEqual([], self.paster.copied)

    def test_a_recording_of_only_other_voices_delivers_nothing_and_signals_no_failure(self) -> None:
        daemon = self.build("mine-only")
        self.recorder.finals.append(self.audio((STRANGER_A, "not you", 2.0), (MILO, "nor me", 2.0)))

        daemon.handle_command("toggle")
        response = daemon.handle_command("toggle")

        self.assertEqual("", response["text"])
        self.assertFalse(response["delivered"])
        self.assertNotIn("detail", response)
        self.assertEqual([], self.paster.pasted)
        self.assertEqual([], self.paster.copied)
        self.assertNotIn("error", [event[0] for event in self.overlay.events])

    def test_a_dropped_final_segment_does_not_make_the_session_undelivered(self) -> None:
        daemon = self.build("mine-only", auto_transcribe="continuous")
        self.recorder.segments.append(self.audio((ME, "kept segment", 2.0)))
        self.recorder.finals.append(self.audio((STRANGER_B, "someone else", 2.0)))

        daemon.handle_command("toggle")
        daemon._on_silence()
        self.settle(daemon)
        response = daemon.handle_command("toggle")

        self.assertEqual("kept segment", response["text"])
        self.assertTrue(response["delivered"])
        self.assertNotIn("segments", response)


class LabelEveryoneTests(SpeakerDaemonCase):
    def test_the_toggle_response_is_the_labelled_text_that_was_delivered(self) -> None:
        daemon = self.build("label-everyone")
        self.recorder.finals.append(
            self.audio(
                (ME, "Shall we start?", 2.0),
                (STRANGER_A, "Yes, go ahead.", 2.0),
                (MILO, "Sounds good.", 2.0),
            )
        )

        daemon.handle_command("toggle")
        response = daemon.handle_command("toggle")

        expected = "You: Shall we start? Speaker 1: Yes, go ahead. Milo: Sounds good."
        self.assertEqual([expected], self.paster.pasted)
        self.assertEqual(expected, response["text"])
        self.assertNotIn("\n", response["text"])

    def test_unknown_voices_keep_their_numbers_across_segments_of_one_session(self) -> None:
        daemon = self.build("label-everyone", auto_transcribe="continuous")
        self.recorder.segments.append(
            self.audio((STRANGER_A, "first stranger", 2.0), (ME, "owner here", 2.0))
        )
        self.recorder.segments.append(
            self.audio((STRANGER_B, "second stranger", 2.0), (STRANGER_A, "first again", 2.0))
        )
        self.recorder.finals.append(self.audio((STRANGER_A, "still first", 2.0)))

        daemon.handle_command("toggle")
        daemon._on_silence()
        self.settle(daemon)
        daemon._on_silence()
        self.settle(daemon)
        daemon.handle_command("toggle")

        self.assertEqual(
            [
                "Speaker 1: first stranger You: owner here",
                "Speaker 2: second stranger Speaker 1: first again",
                "Speaker 1: still first",
            ],
            self.paster.pasted,
        )

    def test_numbering_starts_again_in_the_next_capture_session(self) -> None:
        daemon = self.build("label-everyone")
        self.recorder.finals.append(self.audio((STRANGER_A, "one", 2.0), (STRANGER_B, "two", 2.0)))
        self.recorder.finals.append(self.audio((STRANGER_B, "fresh", 2.0)))

        daemon.handle_command("toggle")
        daemon.handle_command("toggle")
        daemon.handle_command("toggle")
        daemon.handle_command("toggle")

        self.assertEqual(["Speaker 1: one Speaker 2: two", "Speaker 1: fresh"], self.paster.pasted)

    def test_with_no_owner_enrolled_voices_are_labelled_by_name_and_never_you(self) -> None:
        self.store.voices = [ENROLLED_MILO]
        daemon = self.build("label-everyone")
        self.recorder.finals.append(self.audio((MILO, "Milo speaking", 2.0), (ME, "unknown now", 2.0)))

        with self.assertNoLogs("murmly.daemon", level=logging.WARNING):
            daemon.handle_command("toggle")
            daemon.handle_command("toggle")

        self.assertEqual(["Milo: Milo speaking Speaker 1: unknown now"], self.paster.pasted)

    def test_a_recording_too_short_to_identify_is_delivered_unlabelled_and_unfiltered(self) -> None:
        daemon = self.build("label-everyone")
        self.recorder.finals.append(self.audio((ME, "yes", 0.5)))

        daemon.handle_command("toggle")
        response = daemon.handle_command("toggle")

        self.assertEqual(["yes"], self.paster.pasted)
        self.assertEqual("yes", response["text"])


class SpeechSessionBoundTests(SpeakerDaemonCase):
    def test_a_session_bound_reply_is_filtered(self) -> None:
        self.build("mine-only")
        pcm = self.audio((ME, "tell me the time", 2.0), (STRANGER_A, "background chatter", 2.0))
        delivered: list[str] = []

        self.session.start_recording()
        result = self.session.process_for_session(pcm, lambda text: delivered.append(text) or True)

        self.assertEqual(["tell me the time"], delivered)
        self.assertTrue(result.delivered)

    def test_a_session_bound_reply_that_mine_only_empties_is_not_delivered(self) -> None:
        self.build("mine-only")
        pcm = self.audio((STRANGER_A, "background chatter", 2.0))
        delivered: list[str] = []

        self.session.start_recording()
        result = self.session.process_for_session(pcm, lambda text: delivered.append(text) or True)

        self.assertEqual([], delivered)
        self.assertEqual("", result.text)
        self.assertFalse(result.delivered)
        self.assertIsNone(result.detail)

    def test_a_session_bound_reply_is_labelled_in_label_everyone(self) -> None:
        self.build("label-everyone")
        pcm = self.audio((ME, "hello agent", 2.0))
        delivered: list[str] = []

        self.session.start_recording()
        self.session.process_for_session(pcm, lambda text: delivered.append(text) or True)

        self.assertEqual(["You: hello agent"], delivered)


class CaptureAudioTests(SpeakerDaemonCase):
    channels = 2
    sample_rate_hz = 48_000

    def test_parts_are_cut_from_interleaved_stereo_at_the_capture_rate(self) -> None:
        daemon = self.build("mine-only")
        self.recorder.finals.append(
            self.audio((STRANGER_A, "other voice", 1.5), (ME, "owner voice", 1.5))
        )

        daemon.handle_command("toggle")
        daemon.handle_command("toggle")

        self.assertEqual(["owner voice"], self.paster.pasted)


class FailingOpenTests(SpeakerDaemonCase):
    def test_a_missing_model_is_delivered_as_off_with_one_warning_naming_it(self) -> None:
        (self.data_dir / SPEAKER_MODEL_FILENAME).unlink()
        daemon = self.build("label-everyone")
        self.recorder.finals.append(self.audio((ME, "private words", 2.0), (STRANGER_A, "more", 2.0)))

        with self.assertLogs("murmly.daemon", level=logging.WARNING) as logs:
            daemon.handle_command("toggle")
            response = daemon.handle_command("toggle")

        self.assertEqual("private words more", response["text"])
        self.assertEqual(1, len(logs.records))
        message = logs.records[0].getMessage()
        self.assertIn("Speaker recognition disabled for this capture", message)
        self.assertIn(SPEAKER_MODEL_FILENAME, message)
        self.assertNotIn("private words", "\n".join(logs.output))
        self.make_embedder.assert_not_called()
        self.assertEqual((1, 0), (self.transcriber.plain_calls, self.transcriber.timed_calls))

    def test_mine_only_without_an_owner_is_delivered_as_off(self) -> None:
        self.store.voices = [ENROLLED_MILO]
        daemon = self.build("mine-only")
        self.recorder.finals.append(self.audio((ME, "private words", 2.0), (MILO, "other words", 2.0)))

        with self.assertLogs("murmly.daemon", level=logging.WARNING) as logs:
            daemon.handle_command("toggle")
            response = daemon.handle_command("toggle")

        self.assertEqual("private words other words", response["text"])
        self.assertEqual(
            ["Speaker recognition disabled for this capture: no owner is enrolled"],
            [record.getMessage() for record in logs.records],
        )
        self.make_embedder.assert_not_called()

    def test_voiceprints_from_another_model_are_named_as_the_reason(self) -> None:
        self.store.voices = []
        self.store.needs_reenrolment = True
        daemon = self.build("mine-only")

        with self.assertLogs("murmly.daemon", level=logging.WARNING) as logs:
            self.session.start_recording()

        self.assertEqual(1, len(logs.records))
        self.assertIn("must be enrolled again", logs.records[0].getMessage())

    def test_the_warning_is_logged_once_per_capture_session_not_per_segment(self) -> None:
        self.store.voices = []
        daemon = self.build("mine-only", auto_transcribe="continuous")
        self.recorder.segments.append(self.audio((ME, "one", 2.0)))
        self.recorder.segments.append(self.audio((ME, "two", 2.0)))
        self.recorder.finals.append(self.audio((ME, "three", 2.0)))

        with self.assertLogs("murmly.daemon", level=logging.WARNING) as logs:
            daemon.handle_command("toggle")
            daemon._on_silence()
            self.settle(daemon)
            daemon._on_silence()
            self.settle(daemon)
            response = daemon.handle_command("toggle")

        self.assertEqual(1, len(logs.records))
        self.assertEqual("one two three", response["text"])

    def test_an_embedder_that_raises_still_delivers_the_transcript_without_text_in_the_log(self) -> None:
        daemon = self.build("label-everyone")
        self.embedder.embed_error = RuntimeError("secret words and Milo")
        self.recorder.finals.append(self.audio((ME, "secret words", 2.0), (MILO, "more words", 2.0)))

        with self.assertLogs("murmly.daemon", level=logging.WARNING) as logs:
            daemon.handle_command("toggle")
            response = daemon.handle_command("toggle")

        self.assertTrue(response["ok"])
        self.assertEqual("secret words more words", response["text"])
        self.assertEqual(["secret words more words"], self.paster.pasted)
        recorded = "\n".join(logs.output)
        self.assertIn("RuntimeError", recorded)
        for private in ("secret words", "more words", "Milo"):
            self.assertNotIn(private, recorded)

    def test_a_raising_embedder_does_not_end_a_continuous_session(self) -> None:
        daemon = self.build("mine-only", auto_transcribe="continuous")
        self.embedder.embed_error = RuntimeError("model fell over")
        self.recorder.segments.append(self.audio((ME, "first words", 2.0)))
        self.recorder.segments.append(self.audio((ME, "second words", 2.0)))
        self.recorder.finals.append(self.audio((ME, "last words", 2.0)))

        daemon.handle_command("toggle")
        daemon._on_silence()
        self.settle(daemon)
        self.assertEqual("LISTENING", daemon.state)
        daemon._on_silence()
        self.settle(daemon)
        self.assertEqual("LISTENING", daemon.state)
        response = daemon.handle_command("toggle")

        self.assertEqual(["first words", "second words", "last words"], self.paster.pasted)
        self.assertTrue(response["delivered"])
        self.assertEqual(3, response["segments"])
        self.assertNotIn("error", [event[0] for event in self.overlay.events])

    def test_a_decode_failure_still_fails_the_recording_as_it_does_with_the_mode_off(self) -> None:
        daemon = self.build("label-everyone")
        pcm = self.audio((ME, "words", 2.0))
        self.recorder.finals.append(pcm)
        self.transcriber.transcribe_segments_pcm16 = Mock(side_effect=RuntimeError("decode failed"))

        daemon.handle_command("toggle")
        response = daemon.handle_command("toggle")

        self.assertFalse(response["ok"])
        self.assertIn("decode failed", response["error"])

    def test_a_build_that_fails_is_delivered_unlabelled_with_one_warning(self) -> None:
        daemon = self.build("label-everyone")
        self.embedder.load_error = RuntimeError("cannot open the model")
        self.recorder.finals.append(self.audio((ME, "all of it", 2.0), (STRANGER_A, "and this", 2.0)))

        with self.assertLogs("murmly.daemon", level=logging.WARNING) as logs:
            daemon.handle_command("toggle")
            response = daemon.handle_command("toggle")

        self.assertEqual("all of it and this", response["text"])
        self.assertEqual(1, len(logs.records))
        self.assertIn("could not be loaded", logs.records[0].getMessage())
        self.assertEqual(0, self.embedder.embeds)

    def test_a_model_with_the_wrong_checksum_is_never_used_and_is_delivered_as_off(self) -> None:
        # The stand-in file is empty, which is not the pinned model.
        self.make_embedder.side_effect = SpeakerEmbedder
        daemon = self.build("label-everyone")
        self.recorder.finals.append(self.audio((ME, "all of it", 2.0), (STRANGER_A, "and this", 2.0)))

        with patch("onnxruntime.InferenceSession") as constructor, self.assertLogs(
            "murmly.daemon", level=logging.WARNING
        ) as logs:
            daemon.handle_command("toggle")
            response = daemon.handle_command("toggle")
            status = daemon.handle_command("status")

        constructor.assert_not_called()
        self.assertEqual("all of it and this", response["text"])
        self.assertEqual(["all of it and this"], self.paster.pasted)
        self.assertEqual(1, len(logs.records))
        message = logs.records[0].getMessage()
        self.assertIn("does not match the expected checksum", message)
        self.assertNotIn("all of it", message)
        self.assertNotIn("and this", message)
        self.assertIs(False, status["speaker_model_resident"])
        self.assertIn("does not match the expected checksum", status["speaker_model_resident_detail"])

    def test_a_failure_while_getting_ready_never_costs_the_capture(self) -> None:
        daemon = self.build("label-everyone")
        self.store.refresh = Mock(side_effect=OSError("disk gone"))
        self.recorder.finals.append(self.audio((ME, "still here", 2.0)))

        with self.assertLogs("murmly.daemon", level=logging.WARNING) as logs:
            started = daemon.handle_command("toggle")
            response = daemon.handle_command("toggle")

        self.assertTrue(started["ok"])
        self.assertEqual("still here", response["text"])
        self.assertIn("OSError", "\n".join(logs.output))


class OverlayTests(SpeakerDaemonCase):
    def test_overlay_messages_are_the_same_in_every_mode(self) -> None:
        recorded = {}
        for mode in ("off", "mine-only", "label-everyone"):
            daemon = self.build(mode)
            self.recorder.finals.append(
                self.audio((ME, "one two", 2.0), (STRANGER_A, "three four", 2.0))
            )
            daemon.handle_command("toggle")
            daemon.handle_command("toggle")
            recorded[mode] = list(self.overlay.events)

        self.assertEqual(recorded["off"], recorded["mine-only"])
        self.assertEqual(recorded["off"], recorded["label-everyone"])
        for event in recorded["label-everyone"]:
            self.assertEqual("state", event[0])

    def test_partial_passes_are_not_attributed(self) -> None:
        daemon = self.build("label-everyone", live_transcribe=True)
        self.transcriber.partials_available = True
        self.transcriber.transcribe_partial = Mock(return_value="partial words")
        self.recorder.snapshot = lambda _seconds: b"\x01\x00" * 100
        partials: list[str] = []
        self.session._partial_sink = partials.append

        self.session._partial_tick()

        self.assertEqual(["partial words"], partials)
        self.assertEqual(0, self.embedder.embeds)
        self.assertEqual(0, self.transcriber.timed_calls)


class BackgroundBuildTests(SpeakerDaemonCase):
    def test_capture_starts_without_waiting_for_a_slow_build(self) -> None:
        self.build("label-everyone")
        self.embedder.load_gate = threading.Event()
        self.addCleanup(self.embedder.load_gate.set)
        started = threading.Event()

        def start() -> None:
            self.session.start_recording()
            started.set()

        thread = threading.Thread(target=start, daemon=True)
        thread.start()

        self.assertTrue(started.wait(GATE_TIMEOUT_SECONDS), "capture waited for the model build")
        self.assertTrue(self.embedder.load_started.wait(GATE_TIMEOUT_SECONDS))
        self.assertFalse(self.embedder.resident)
        self.assertEqual(1, self.recorder.started)

    def test_attribution_waits_for_the_build_when_it_has_not_finished(self) -> None:
        self.build("label-everyone")
        self.embedder.load_gate = threading.Event()
        self.addCleanup(self.embedder.load_gate.set)
        pcm = self.audio((ME, "waiting for the model", 2.0))
        self.session.start_recording()
        results: list[str] = []
        worker = threading.Thread(
            target=lambda: results.append(self.session.process_recording(pcm).text), daemon=True
        )

        worker.start()
        worker.join(BLOCKED_SECONDS)
        self.assertTrue(worker.is_alive(), "attribution did not wait for the build")
        self.embedder.load_gate.set()
        worker.join(GATE_TIMEOUT_SECONDS)

        self.assertEqual(["You: waiting for the model"], results)

    def test_the_embedder_is_built_once_and_reused_while_resident(self) -> None:
        daemon = self.build("label-everyone")
        for _ in range(2):
            self.recorder.finals.append(self.audio((ME, "again", 2.0)))
            daemon.handle_command("toggle")
            daemon.handle_command("toggle")

        self.assertEqual(1, self.make_embedder.call_count)
        self.assertEqual(1, self.make_store.call_count)
        self.assertEqual(2, self.store.refreshes)
        self.assertEqual(1, self.embedder.loads)

    def test_a_voice_enrolled_since_the_last_capture_is_used_by_the_next(self) -> None:
        self.store.voices = [ENROLLED_ME]
        daemon = self.build("label-everyone")
        first = self.audio((MILO, "who is this", 2.0))
        second = self.audio((MILO, "who is this", 2.0), (ME, "tail", 2.0))
        self.recorder.finals.extend([first, second])

        daemon.handle_command("toggle")
        daemon.handle_command("toggle")
        self.store.voices = [ENROLLED_ME, ENROLLED_MILO]
        daemon.handle_command("toggle")
        daemon.handle_command("toggle")

        self.assertEqual(
            ["Speaker 1: who is this", "Milo: who is this You: tail"], self.paster.pasted
        )

    def test_the_daemon_builds_a_cpu_only_session_for_the_speaker_model(self) -> None:
        # The stand-in model file is empty, so its checksum is the empty one.
        self.make_embedder.side_effect = lambda path: SpeakerEmbedder(
            path, expected_sha256=hashlib.sha256(b"").hexdigest()
        )
        session = Mock()
        session.get_providers.return_value = [CPU_PROVIDER]
        daemon = self.build("label-everyone")

        with patch("onnxruntime.InferenceSession", return_value=session) as constructor:
            self.session.start_recording()
            self.session._speaker_build.succeeded()

        constructor.assert_called_once()
        self.assertEqual([CPU_PROVIDER], constructor.call_args.kwargs["providers"])
        self.assertFalse(constructor.call_args.kwargs["sess_options"].enable_cpu_mem_arena)
        self.assertEqual(CPU_PROVIDER, self.session._speaker_embedder.provider)
        self.assertIs(True, daemon.handle_command("status")["speaker_model_resident"])


class ResidencyTests(SpeakerDaemonCase):
    def test_the_speaker_model_is_released_with_the_transcription_model(self) -> None:
        daemon = self.build("label-everyone", unload_after_idle_s=0.02)
        self.recorder.finals.append(self.audio((ME, "hello", 2.0)))

        daemon.handle_command("toggle")
        daemon.handle_command("toggle")
        self.assertTrue(self.embedder.release_ran.wait(GATE_TIMEOUT_SECONDS), "never released")

        self.assertEqual(1, self.embedder.releases)
        self.assertEqual(1, self.transcriber.released)
        self.assertIs(False, daemon.handle_command("status")["speaker_model_resident"])

    def test_a_period_of_zero_never_releases_the_speaker_model(self) -> None:
        daemon = self.build("label-everyone", unload_after_idle_s=0)
        self.recorder.finals.append(self.audio((ME, "hello", 2.0)))

        daemon.handle_command("toggle")
        daemon.handle_command("toggle")
        time.sleep(BLOCKED_SECONDS)

        self.assertFalse(daemon._transcription_idle.armed)
        self.assertEqual(0, self.embedder.releases)
        self.assertEqual(0, self.transcriber.released)
        self.assertIs(True, daemon.handle_command("status")["speaker_model_resident"])

    def test_the_status_follows_the_model_in_and_out(self) -> None:
        daemon = self.build("label-everyone", unload_after_idle_s=0)
        self.recorder.finals.append(self.audio((ME, "hello", 2.0)))

        before = daemon.handle_command("status")["speaker_model_resident"]
        daemon.handle_command("toggle")
        self.session._speaker_build.succeeded()
        held = daemon.handle_command("status")["speaker_model_resident"]
        daemon._release_transcription()
        released = daemon.handle_command("status")["speaker_model_resident"]

        self.assertEqual((False, True, False), (before, held, released))
        daemon.handle_command("toggle")

    def test_a_release_never_lands_between_two_parts_of_one_attribution(self) -> None:
        self.build("label-everyone")
        self.embedder.embed_gate = threading.Event()
        self.addCleanup(self.embedder.embed_gate.set)
        pcm = self.audio((ME, "first part", 2.0), (STRANGER_A, "second part", 2.0))
        self.session.start_recording()
        self.session._speaker_build.succeeded()
        results: list[str] = []
        worker = threading.Thread(
            target=lambda: results.append(self.session.process_recording(pcm).text), daemon=True
        )
        worker.start()
        self.assertTrue(self.embedder.embedding.wait(GATE_TIMEOUT_SECONDS))
        releaser = threading.Thread(target=self.session.release_model, daemon=True)

        releaser.start()
        releaser.join(BLOCKED_SECONDS)

        self.assertTrue(releaser.is_alive(), "the release did not wait for the attribution")
        self.assertEqual(0, self.embedder.releases)
        self.embedder.embed_gate.set()
        worker.join(GATE_TIMEOUT_SECONDS)
        releaser.join(GATE_TIMEOUT_SECONDS)

        self.assertEqual(["You: first part Speaker 1: second part"], results)
        self.assertEqual(1, self.embedder.releases)

    def test_the_next_capture_after_a_release_builds_the_model_again(self) -> None:
        daemon = self.build("mine-only", unload_after_idle_s=0)
        self.recorder.finals.append(self.audio((ME, "before", 2.0), (STRANGER_A, "noise", 2.0)))
        self.recorder.finals.append(self.audio((ME, "after", 2.0), (STRANGER_A, "noise", 2.0)))

        daemon.handle_command("toggle")
        daemon.handle_command("toggle")
        daemon._release_transcription()
        self.assertFalse(self.embedder.resident)
        daemon.handle_command("toggle")
        daemon.handle_command("toggle")

        self.assertEqual(["before", "after"], self.paster.pasted)
        self.assertEqual(2, self.embedder.loads)
        self.assertEqual(1, self.make_embedder.call_count)

    def test_a_release_with_no_speaker_model_ever_created_is_a_no_op(self) -> None:
        daemon = self.build("off")

        daemon._release_transcription()

        self.assertEqual(1, self.transcriber.released)
        self.make_embedder.assert_not_called()

    def test_the_transcription_model_is_released_even_if_the_speaker_release_fails(self) -> None:
        daemon = self.build("label-everyone")
        self.recorder.finals.append(self.audio((ME, "hello", 2.0)))
        daemon.handle_command("toggle")
        daemon.handle_command("toggle")
        self.embedder.release = Mock(side_effect=RuntimeError("heap trouble"))

        with self.assertRaises(RuntimeError):
            self.session.release_model()

        self.assertEqual(1, self.transcriber.released)


class StatusIsReadOnlyTests(SpeakerDaemonCase):
    def test_status_neither_builds_the_embedder_nor_waits_on_its_locks(self) -> None:
        daemon = self.build("label-everyone")
        real = SpeakerEmbedder(self.data_dir / SPEAKER_MODEL_FILENAME, session=Mock())
        self.session._speaker_embedder = real
        answers: list[dict] = []
        asked = threading.Thread(
            target=lambda: answers.append(daemon.handle_command("status")), daemon=True
        )

        with real._load_lock, real._use_lock, patch("onnxruntime.InferenceSession") as constructor:
            asked.start()
            asked.join(GATE_TIMEOUT_SECONDS)

        self.assertFalse(asked.is_alive(), "status waited on the speaker model's locks")
        self.assertIs(True, answers[0]["speaker_model_resident"])
        constructor.assert_not_called()
        self.make_embedder.assert_not_called()

    def test_a_failed_build_is_reported_with_its_reason_without_locking_or_building(self) -> None:
        daemon = self.build("label-everyone")
        real = SpeakerEmbedder(self.data_dir / "missing.onnx")
        with self.assertRaises(FileNotFoundError):
            real.embed(np.zeros(16_000, dtype=np.float32))
        self.session._speaker_embedder = real
        answers: list[dict] = []
        asked = threading.Thread(
            target=lambda: answers.append(daemon.handle_command("status")), daemon=True
        )

        with real._load_lock, real._use_lock, patch("onnxruntime.InferenceSession") as constructor:
            asked.start()
            asked.join(GATE_TIMEOUT_SECONDS)

        self.assertFalse(asked.is_alive(), "status waited on the speaker model's locks")
        self.assertIs(False, answers[0]["speaker_model_resident"])
        self.assertIn("missing", answers[0]["speaker_model_resident_detail"])
        constructor.assert_not_called()

    def test_no_detail_is_carried_when_the_build_has_not_failed(self) -> None:
        daemon = self.build("label-everyone")
        self.session._speaker_embedder = SpeakerEmbedder(
            self.data_dir / SPEAKER_MODEL_FILENAME, session=Mock()
        )

        response = daemon.handle_command("status")

        self.assertIs(True, response["speaker_model_resident"])
        self.assertNotIn("speaker_model_resident_detail", response)

    def test_a_session_that_cannot_say_costs_the_field_and_nothing_else(self) -> None:
        daemon = self.build("label-everyone")
        with patch.object(
            SpeechSession,
            "speaker_model_resident",
            new_callable=lambda: property(lambda _self: (_ for _ in ()).throw(RuntimeError("no idea"))),
        ):
            response = daemon.handle_command("status")

        self.assertIsNone(response["speaker_model_resident"])
        self.assertIn("no idea", response["speaker_model_resident_detail"])
        self.assertEqual("IDLE", response["state"])
        self.assertTrue(response["ok"])


if __name__ == "__main__":
    unittest.main()
