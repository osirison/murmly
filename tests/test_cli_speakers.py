"""`murmly enrol` and `murmly speakers`: refusals, the sample, and the reports."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import numpy as np

from murmly.cli import (
    ENROL_MIN_SPEECH_SECONDS,
    ENROL_PASSAGE,
    ENROL_SECONDS,
    _capture_as_speaker_audio,
    _run_enrol,
    _run_speakers_list,
    _run_speakers_remove,
    build_parser,
    main,
)
from murmly.config import MurmlyConfig
from murmly.daemon import DaemonNotRespondingError
from murmly.speakers import EMBEDDING_DIMENSION, SPEAKER_MODEL_FILENAME, l2_normalised
from murmly.voiceprints import VOICEPRINTS_FILENAME, VoiceprintStore

RATE = 16_000
OTHER_MODEL = "0" * 64


def vector(seed: int) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(EMBEDDING_DIMENSION)


class FakeRecorder:
    """Stands in for `SoundDeviceRecorder`: hands back a clip, opens nothing."""

    instances: list["FakeRecorder"] = []

    def __init__(self, config: MurmlyConfig) -> None:
        self.config = config
        self.sample_rate_hz = RATE
        self.requested: list[float] = []
        self.failure: Exception | None = None
        self.stopped = 0
        FakeRecorder.instances.append(self)

    def record_for_seconds(self, seconds: float) -> bytes:
        self.requested.append(seconds)
        if self.failure is not None:
            raise self.failure
        return b"\x01\x00" * int(RATE * seconds * self.config.channels)

    def stop(self) -> bytes:
        self.stopped += 1
        return b""


class FakeEmbedder:
    """Returns a different unit vector per window, and counts the windows."""

    instances: list["FakeEmbedder"] = []
    fail_to_load: Exception | None = None

    def __init__(self, model_path: Path) -> None:
        self.model_path = model_path
        self.windows: list[int] = []
        self.released = 0
        FakeEmbedder.instances.append(self)

    def embed(self, samples):
        if self.fail_to_load is not None:
            raise self.fail_to_load
        if len(samples) == RATE and np.allclose(samples, 1e-3):
            return l2_normalised(vector(0))  # the load probe, not a window
        self.windows.append(len(samples))
        return l2_normalised(vector(len(self.windows)))

    def release(self) -> bool:
        self.released += 1
        return True


class EnrolTestCase(unittest.TestCase):
    def setUp(self) -> None:
        FakeRecorder.instances = []
        FakeEmbedder.instances = []
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.data_dir = Path(self._temporary.name) / "data"
        self.data_dir.mkdir()
        self.model = self.data_dir / SPEAKER_MODEL_FILENAME
        self.model.write_bytes(b"model")
        self.store_path = self.data_dir / VOICEPRINTS_FILENAME
        self.config = MurmlyConfig(
            socket_path=Path("/tmp/murmly-test.sock"),
            config_path=Path("/tmp/murmly-test/config.toml"),
        )
        self.speech: list[tuple[int, int]] = [(0, 12 * RATE)]
        self.audio_seen: list[int] = []
        self.status = {"ok": True, "state": "IDLE"}
        self.asked: list[str] = []
        patch("murmly.cli.default_data_dir", return_value=self.data_dir).start()
        patch("murmly.cli.SoundDeviceRecorder", FakeRecorder).start()
        self.addCleanup(patch.stopall)

    def find_speech(self, audio):
        self.audio_seen.append(len(audio))
        return self.speech

    def send(self, _socket_path: str, command: str) -> dict:
        self.asked.append(command)
        if isinstance(self.status, Exception):
            raise self.status
        return self.status

    def enrol(self, name: str = "Milo", seconds: float = ENROL_SECONDS, **overrides):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = _run_enrol(
                overrides.pop("config", self.config),
                name,
                seconds,
                embedder_factory=FakeEmbedder,
                speech_regions=self.find_speech,
                send=self.send,
            )
        return code, out.getvalue(), err.getvalue()

    def stored_names(self) -> list[str]:
        store = VoiceprintStore(self.data_dir)
        store.refresh()
        return store.names

    def assert_refused_before_recording(self, code: int) -> None:
        self.assertEqual(1, code)
        self.assertEqual([], FakeRecorder.instances)
        self.assertFalse(self.store_path.exists())


class RefusalOrderTests(EnrolTestCase):
    def test_bad_names_are_refused_before_anything_else(self) -> None:
        self.model.unlink()  # a later check would also fail; the name is named first
        for bad in ("", "   ", "Mi\nlo", "Mi\x07lo", "a:b", "you", "YOU", "Speaker 2", "x" * 33):
            with self.subTest(name=bad):
                code, out, err = self.enrol(bad)

                self.assert_refused_before_recording(code)
                self.assertEqual("", out)
                self.assertIn("Cannot enrol that name", err)
                self.assertNotIn("speaker model", err)
                self.assertEqual([], self.asked)

    def test_a_length_that_cannot_hold_enough_speech_is_refused(self) -> None:
        for seconds in (ENROL_MIN_SPEECH_SECONDS, 3.0, 0.0, -5.0, 10_000.0, float("nan"), float("inf")):
            with self.subTest(seconds=seconds):
                code, _out, err = self.enrol(seconds=seconds)

                self.assert_refused_before_recording(code)
                self.assertIn("--seconds", err)

    def test_a_missing_model_is_refused_before_recording_and_before_asking_the_daemon(self) -> None:
        self.model.unlink()

        code, out, err = self.enrol()

        self.assert_refused_before_recording(code)
        self.assertEqual("", out)
        self.assertIn(str(self.model), err)
        self.assertIn("setup.sh upgrade", err)
        self.assertEqual([], self.asked)
        self.assertEqual([], FakeEmbedder.instances)

    def test_a_model_that_will_not_load_is_refused_before_recording(self) -> None:
        with patch.object(FakeEmbedder, "fail_to_load", RuntimeError("bad protobuf")):
            code, _out, err = self.enrol()

        self.assert_refused_before_recording(code)
        self.assertIn("could not be loaded", err)
        self.assertIn("bad protobuf", err)
        self.assertEqual(1, FakeEmbedder.instances[0].released)

    def test_a_busy_daemon_is_refused_for_each_busy_state(self) -> None:
        for state, doing in (("LISTENING", "listening"), ("THINKING", "transcribing"), ("SPEAKING", "speaking")):
            with self.subTest(state=state):
                FakeRecorder.instances = []
                self.status = {"ok": True, "state": state}

                code, out, err = self.enrol()

                self.assert_refused_before_recording(code)
                self.assertEqual("", out)
                self.assertIn(f"The Murmly daemon is {doing}", err)
                self.assertEqual(["status"], self.asked[-1:])

    def test_only_status_is_asked_of_the_daemon(self) -> None:
        self.enrol()

        self.assertEqual(["status"], self.asked)

    def test_no_daemon_answering_proceeds(self) -> None:
        for failure in (
            FileNotFoundError("no socket"),
            ConnectionRefusedError("refused"),
            DaemonNotRespondingError("no reply"),
        ):
            with self.subTest(failure=type(failure).__name__):
                self.status = failure
                self.store_path.unlink(missing_ok=True)

                code, _out, err = self.enrol()

                self.assertEqual(0, code, err)
                self.assertEqual(["Milo"], self.stored_names())

    def test_an_idle_daemon_proceeds(self) -> None:
        self.status = {"ok": True, "state": "IDLE"}

        code, _out, err = self.enrol()

        self.assertEqual(0, code, err)
        self.assertEqual(["Milo"], self.stored_names())
        self.assertEqual([float(ENROL_SECONDS)], FakeRecorder.instances[0].requested)

    def test_the_passage_is_shorter_than_the_recording_so_the_reader_is_told_to_keep_going(self) -> None:
        code, out, err = self.enrol()

        self.assertEqual(0, code, err)
        self.assertIn(ENROL_PASSAGE, out)
        instruction = "Keep reading until the recording stops. If you reach the end, start again from the top."
        self.assertIn(instruction, out)
        # Said before the recording begins, not after.
        self.assertLess(out.index(instruction), out.index("Recording now."))


class SampleTests(EnrolTestCase):
    def test_a_silent_sample_is_refused_and_nothing_is_stored(self) -> None:
        self.speech = []

        code, out, err = self.enrol()

        self.assertEqual(1, code)
        self.assertFalse(self.store_path.exists())
        self.assertIn("Too little speech was heard: 0.0 seconds", err)
        self.assertNotIn("Enrolled", out)
        self.assertEqual([], FakeEmbedder.instances[0].windows)
        self.assertEqual(1, FakeEmbedder.instances[0].released)

    def test_a_short_sample_is_refused_and_leaves_an_existing_voiceprint_alone(self) -> None:
        self.enrol()
        before = self.store_path.read_bytes()
        self.speech = [(0, 4 * RATE), (6 * RATE, 9 * RATE)]  # 7 s in two regions

        code, _out, err = self.enrol()

        self.assertEqual(1, code)
        self.assertIn("Too little speech was heard: 7.0 seconds", err)
        self.assertEqual(before, self.store_path.read_bytes())

    def test_an_accepted_sample_is_averaged_in_windows_and_stored_as_a_unit_vector(self) -> None:
        self.speech = [(0, 7 * RATE), (8 * RATE, 14 * RATE)]  # 13 s: four windows and a 1 s tail

        code, _out, err = self.enrol()

        self.assertEqual(0, code, err)
        embedder = FakeEmbedder.instances[0]
        # 3 s windows over 13 s of speech; the 1 s tail is long enough to keep.
        self.assertEqual([3 * RATE] * 4 + [RATE], embedder.windows)
        expected = l2_normalised(np.mean([l2_normalised(vector(i)) for i in range(1, 6)], axis=0))
        store = VoiceprintStore(self.data_dir)
        store.refresh()
        stored = np.asarray(store.voices[0].embedding)
        self.assertAlmostEqual(1.0, float(np.linalg.norm(stored)), 9)
        np.testing.assert_allclose(stored, expected, atol=1e-9)
        self.assertEqual(1, embedder.released)

    def test_a_tail_shorter_than_the_least_the_daemon_attributes_is_left_out(self) -> None:
        self.speech = [(0, int(12.5 * RATE))]  # four windows and a 0.5 s tail

        code, _out, err = self.enrol()

        self.assertEqual(0, code, err)
        self.assertEqual([3 * RATE] * 4, FakeEmbedder.instances[0].windows)

    def test_the_sample_reaches_the_voice_detector_as_16_khz_mono(self) -> None:
        config = replace(self.config, channels=2, sample_rate_hz=48_000)

        def recorder_at_48k(config: MurmlyConfig) -> FakeRecorder:
            recorder = FakeRecorder(config)
            recorder.sample_rate_hz = 48_000
            recorder.record_for_seconds = lambda seconds: b"\x01\x00" * int(48_000 * seconds * 2)
            return recorder

        with patch("murmly.cli.SoundDeviceRecorder", recorder_at_48k):
            code, _out, err = self.enrol(config=config)

        self.assertEqual(0, code, err)
        self.assertEqual([int(ENROL_SECONDS * RATE)], self.audio_seen)

    def test_stereo_is_averaged_and_resampled_to_16_khz(self) -> None:
        left = np.full(48_000, 3_000, dtype="<i2")
        right = np.full(48_000, 1_000, dtype="<i2")
        clip = np.column_stack([left, right]).astype("<i2").tobytes()

        audio = _capture_as_speaker_audio(clip, 48_000, 2)

        self.assertEqual(np.float32, audio.dtype)
        self.assertEqual(RATE, len(audio))
        np.testing.assert_allclose(audio, 2_000 / 32_768.0, atol=1e-6)

    def test_a_microphone_that_cannot_be_opened_is_reported_and_stores_nothing(self) -> None:
        def failing(config: MurmlyConfig) -> FakeRecorder:
            recorder = FakeRecorder(config)
            recorder.failure = RuntimeError("Unable to open a microphone input.")
            return recorder

        with patch("murmly.cli.SoundDeviceRecorder", failing):
            code, _out, err = self.enrol()

        self.assertEqual(1, code)
        self.assertIn("Could not record: Unable to open a microphone input.", err)
        self.assertFalse(self.store_path.exists())
        self.assertEqual(1, FakeEmbedder.instances[0].released)

    def test_an_interrupt_during_recording_stores_nothing_and_exits_130(self) -> None:
        def interrupted(config: MurmlyConfig) -> FakeRecorder:
            recorder = FakeRecorder(config)
            recorder.failure = KeyboardInterrupt()
            return recorder

        with patch("murmly.cli.SoundDeviceRecorder", interrupted):
            code, _out, err = self.enrol()

        self.assertEqual(130, code)
        self.assertIn("Nothing was stored", err)
        self.assertFalse(self.store_path.exists())
        self.assertEqual(1, FakeRecorder.instances[0].stopped)

    def test_no_file_but_the_voiceprint_store_is_created(self) -> None:
        scratch = Path(self._temporary.name) / "tmp"
        scratch.mkdir()
        before = sorted(p.name for p in self.data_dir.iterdir())

        def refuse(*_args, **_kwargs):
            raise AssertionError("enrolment must not write audio to a file")

        with (
            patch("tempfile.tempdir", str(scratch)),
            patch("wave.open", refuse),
            patch("tempfile.NamedTemporaryFile", refuse),
            patch("tempfile.mkstemp", refuse),
        ):
            code, _out, err = self.enrol()

        self.assertEqual(0, code, err)
        self.assertEqual([], list(scratch.iterdir()))
        after = sorted(p.name for p in self.data_dir.iterdir())
        self.assertEqual(sorted(before + [VOICEPRINTS_FILENAME]), after)


class ReportTests(EnrolTestCase):
    def test_a_new_voice_reports_where_it_is_stored_and_how_to_remove_it(self) -> None:
        code, out, _err = self.enrol("Milo")

        self.assertEqual(0, code)
        self.assertIn('Enrolled "Milo".', out)
        self.assertIn(f"Voiceprint stored in: {self.store_path}", out)
        self.assertIn("To remove this voice: murmly speakers remove Milo", out)
        self.assertNotIn("Replaced", out)

    def test_a_name_with_a_space_is_quoted_in_the_remove_command(self) -> None:
        _code, out, _err = self.enrol("Mary Ann")

        self.assertIn("murmly speakers remove 'Mary Ann'", out)

    def test_enrolling_a_name_again_in_another_case_reports_the_replacement(self) -> None:
        self.enrol("Milo")

        code, out, _err = self.enrol("milo")

        self.assertEqual(0, code)
        self.assertIn('Replaced the voiceprint already enrolled as "milo".', out)
        self.assertEqual(1, len(self.stored_names()))

    def test_without_an_owner_the_config_line_to_add_is_printed(self) -> None:
        _code, out, _err = self.enrol("Milo")

        self.assertIn(str(self.config.config_path), out)
        self.assertIn('[speakers]\nowner = "Milo"', out)

    def test_the_owner_line_escapes_the_name(self) -> None:
        _code, out, _err = self.enrol('Mi"lo')

        self.assertIn('owner = "Mi\\"lo"', out)

    def test_with_an_owner_configured_the_owner_line_is_not_printed(self) -> None:
        config = replace(self.config, speaker_owner="Ana")

        _code, out, _err = self.enrol("Milo", config=config)

        self.assertNotIn("[speakers]", out)
        self.assertNotIn("No owner is set", out)

    def test_voiceprints_from_another_model_are_reported_as_discarded(self) -> None:
        VoiceprintStore(self.data_dir, model_sha256=OTHER_MODEL).add_or_replace("Ana", vector(9))

        code, out, _err = self.enrol("Milo")

        self.assertEqual(0, code)
        self.assertIn("made with a different speaker model and have been discarded", out)
        self.assertEqual(["Milo"], self.stored_names())

    def test_an_unreadable_voiceprint_file_is_reported_as_replaced(self) -> None:
        self.store_path.write_text("not json")

        code, out, _err = self.enrol("Milo")

        self.assertEqual(0, code)
        self.assertIn("could not be read and has been replaced", out)
        self.assertNotIn("different speaker model", out)

    def test_nothing_printed_carries_a_vector(self) -> None:
        _code, out, err = self.enrol("Milo")
        store = VoiceprintStore(self.data_dir)
        store.refresh()

        for value in store.voices[0].embedding[:5]:
            self.assertNotIn(repr(value)[:8], out + err)


class SpeakersCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.data_dir = Path(self._temporary.name) / "data"
        self.store = VoiceprintStore(self.data_dir)
        self.config = MurmlyConfig(
            socket_path=Path("/tmp/murmly-test.sock"),
            config_path=Path("/tmp/murmly-test/config.toml"),
        )
        patch("murmly.cli.default_data_dir", return_value=self.data_dir).start()
        patch("murmly.voiceprints.logger").start()
        self.addCleanup(patch.stopall)

    def run_command(self, function, *arguments):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = function(*arguments)
        return code, out.getvalue(), err.getvalue()

    def test_list_says_when_none_are_enrolled(self) -> None:
        code, out, err = self.run_command(_run_speakers_list, self.config)

        self.assertEqual(0, code)
        self.assertIn("No voices are enrolled", out)
        self.assertEqual("", err)

    def test_list_names_the_voices_and_marks_the_owner(self) -> None:
        self.store.add_or_replace("Milo", vector(1))
        self.store.add_or_replace("Ana", vector(2))
        config = replace(self.config, speaker_owner="milo")

        code, out, _err = self.run_command(_run_speakers_list, config)

        self.assertEqual(0, code)
        self.assertIn("Enrolled voices (2):", out)
        self.assertIn("  Milo (owner)\n", out)
        self.assertIn("  Ana\n", out)
        self.assertNotIn("Ana (owner)", out)
        self.assertNotIn("is not enrolled", out)

    def test_list_says_when_the_configured_owner_is_not_enrolled(self) -> None:
        self.store.add_or_replace("Milo", vector(1))
        config = replace(self.config, speaker_owner="Ana")

        _code, out, _err = self.run_command(_run_speakers_list, config)

        self.assertIn('The configured owner "Ana" is not enrolled.', out)
        self.assertNotIn("(owner)", out)

    def test_list_reports_voices_that_need_enrolling_again(self) -> None:
        VoiceprintStore(self.data_dir, model_sha256=OTHER_MODEL).add_or_replace("Ana", vector(1))

        code, out, _err = self.run_command(_run_speakers_list, self.config)

        self.assertEqual(0, code)
        self.assertIn("No voices are enrolled", out)
        self.assertIn("different speaker model are ignored", out)

    def test_list_reports_an_unreadable_file(self) -> None:
        self.data_dir.mkdir(parents=True)
        (self.data_dir / VOICEPRINTS_FILENAME).write_text("{")

        code, out, _err = self.run_command(_run_speakers_list, self.config)

        self.assertEqual(0, code)
        self.assertIn("Voiceprint file: the voiceprint file cannot be used", out)

    def test_list_never_prints_a_vector(self) -> None:
        self.store.add_or_replace("Milo", vector(1))
        self.store.refresh()
        first = repr(self.store.voices[0].embedding[0])[:8]

        _code, out, err = self.run_command(_run_speakers_list, self.config)

        self.assertNotIn(first, out + err)

    def test_remove_deletes_one_voice_and_keeps_the_others(self) -> None:
        self.store.add_or_replace("Milo", vector(1))
        self.store.add_or_replace("Ana", vector(2))

        code, out, _err = self.run_command(_run_speakers_remove, "milo", False)

        self.assertEqual(0, code)
        self.assertIn('Removed "milo".', out)
        fresh = VoiceprintStore(self.data_dir)
        fresh.refresh()
        self.assertEqual(["Ana"], fresh.names)

    def test_remove_all_leaves_no_voiceprint_file(self) -> None:
        self.store.add_or_replace("Milo", vector(1))
        self.store.add_or_replace("Ana", vector(2))

        code, out, _err = self.run_command(_run_speakers_remove, None, True)

        self.assertEqual(0, code)
        self.assertIn("Removed every enrolled voice.", out)
        self.assertFalse((self.data_dir / VOICEPRINTS_FILENAME).exists())

    def test_remove_all_with_nothing_enrolled_says_so(self) -> None:
        code, out, _err = self.run_command(_run_speakers_remove, None, True)

        self.assertEqual(0, code)
        self.assertIn("No voices were enrolled.", out)

    def test_removing_a_name_that_is_not_enrolled_exits_non_zero(self) -> None:
        self.store.add_or_replace("Milo", vector(1))

        code, out, err = self.run_command(_run_speakers_remove, "Ana", False)

        self.assertEqual(1, code)
        self.assertEqual("", out)
        self.assertIn('"Ana" is not enrolled.', err)
        fresh = VoiceprintStore(self.data_dir)
        fresh.refresh()
        self.assertEqual(["Milo"], fresh.names)


class ParserTests(unittest.TestCase):
    def test_remove_takes_a_name_or_all_but_not_neither_or_both(self) -> None:
        parser = build_parser()

        self.assertEqual("Milo", parser.parse_args(["speakers", "remove", "Milo"]).name)
        self.assertTrue(parser.parse_args(["speakers", "remove", "--all"]).all)
        for arguments in (["speakers", "remove"], ["speakers", "remove", "Milo", "--all"], ["speakers"]):
            with self.subTest(arguments=arguments), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    parser.parse_args(arguments)
                self.assertEqual(2, raised.exception.code)

    def test_enrol_defaults_to_the_placeholder_length(self) -> None:
        args = build_parser().parse_args(["enrol", "Milo"])

        self.assertEqual(ENROL_SECONDS, args.seconds)

    def test_the_speakers_commands_run_without_a_daemon_or_a_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            with (
                patch("murmly.cli.default_data_dir", return_value=Path(directory) / "data"),
                patch("murmly.cli.send_command") as sent,
                patch("murmly.cli.SoundDeviceRecorder") as recorder,
                redirect_stdout(StringIO()) as out,
            ):
                code = main(["--config", str(config_path), "speakers", "list"])

        self.assertEqual(0, code)
        self.assertIn("No voices are enrolled", out.getvalue())
        sent.assert_not_called()
        recorder.assert_not_called()


if __name__ == "__main__":
    unittest.main()
