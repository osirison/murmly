"""The speaker core: features, the embedding session, attribution and formatting.

Attribution runs against a fake embedder that returns the vector a test chose,
because embeddings of synthetic audio mean nothing (CI has no real voices).
Vectors are written so their cosine similarities are exact in floating point:
(3, 4) against (1, 0) is 0.6 to the last bit, which is what makes the threshold
boundary testable without a tolerance.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from fakes import GATE_TIMEOUT_SECONDS
from murmly import speakers
from murmly.config import default_data_dir
from murmly.speakers import (
    CPU_PROVIDER,
    EMBEDDING_DIMENSION,
    InvalidSpeakerName,
    SpeakerEmbedder,
    SpeakerState,
    TimedText,
    Voice,
    attribute,
    fbank_features,
    normalised_features,
    same_name,
    speaker_text,
    validate_name,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fbank_reference.npz"

#: Four orthogonal directions, so any two differ by exactly zero similarity.
E0 = (1.0, 0.0, 0.0, 0.0)
E1 = (0.0, 1.0, 0.0, 0.0)
E2 = (0.0, 0.0, 1.0, 0.0)
E3 = (0.0, 0.0, 0.0, 1.0)

OWNER = Voice("Ana", E0)
MILO = Voice("Milo", E1)


class FilterbankTests(unittest.TestCase):
    """The numpy features against the reference implementation's output.

    The fixture holds 0.5 s of a 200 Hz to 3 kHz chirp with a little noise,
    stored as int16 so no random generator is involved, and the features
    `kaldi-native-fbank` 1.22.3 produced for it. It was generated once with
    `window_type="hamming"`, `dither=0`, `snip_edges=True`, `preemph_coeff=0.97`,
    `remove_dc_offset=True`, `round_to_power_of_two=True`, `low_freq=20`,
    `high_freq=0`, `use_energy=False`, `use_power=True`, `use_log_fbank=True`,
    `num_bins=80`, from the int16-valued samples as floats, which is how the
    model's publisher feeds it.

    The tolerance is 1e-3 on the log-mel values, which range from 7.6 to 29.
    The reference computes in float32 and this in float64; the measured worst
    difference was 2.4e-4, in the quietest bin of the quietest frame where the
    float32 reference loses the most to cancellation, and the median was 4e-6.
    A wrong window, scale, mel scale or frame count moves values by 0.01 or more.
    """

    TOLERANCE = 1e-3

    @classmethod
    def setUpClass(cls) -> None:
        with np.load(FIXTURE) as data:
            cls.samples = data["pcm"].astype(np.float32) / 32768.0
            cls.fbank = data["fbank"]
            cls.fbank_cmn = data["fbank_cmn"]

    def test_features_match_the_reference_before_normalisation(self) -> None:
        features = fbank_features(self.samples)

        self.assertEqual(self.fbank.shape, features.shape)
        self.assertEqual(np.float32, features.dtype)
        self.assertLess(float(np.abs(features - self.fbank).max()), self.TOLERANCE)

    def test_features_match_the_reference_after_mean_normalisation(self) -> None:
        features = normalised_features(self.samples)

        self.assertEqual(self.fbank_cmn.shape, features.shape)
        self.assertLess(float(np.abs(features - self.fbank_cmn).max()), self.TOLERANCE)
        self.assertLess(float(np.abs(features.mean(axis=0)).max()), 1e-5)

    def test_frame_count_follows_snip_edges(self) -> None:
        for samples, frames in ((399, 0), (400, 1), (559, 1), (560, 2), (8000, 48)):
            with self.subTest(samples=samples):
                self.assertEqual(frames, fbank_features(np.zeros(samples)).shape[0])

    def test_silence_is_floored_not_minus_infinity(self) -> None:
        features = fbank_features(np.zeros(1600))

        self.assertTrue(np.isfinite(features).all())
        self.assertAlmostEqual(float(np.log(np.finfo(np.float32).eps)), float(features[0, 0]), 5)


class RecordingSession:
    """A session stand-in: returns a fixed raw embedding and records its options."""

    def __init__(self, providers=(CPU_PROVIDER,), raw=None) -> None:
        self._providers = list(providers)
        self.raw = np.array([[3.0, 4.0] + [0.0] * (EMBEDDING_DIMENSION - 2)], dtype=np.float32)
        if raw is not None:
            self.raw = raw
        self.runs = 0

    def get_providers(self):
        return list(self._providers)

    def run(self, output_names, feeds):
        self.runs += 1
        self.feeds = feeds
        return [self.raw]


class GatedSession(RecordingSession):
    """Parks inside `run` until a test opens the gate."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.gate = threading.Event()

    def run(self, output_names, feeds):
        self.entered.set()
        self.gate.wait(GATE_TIMEOUT_SECONDS)
        return super().run(output_names, feeds)


def one_second():
    return np.linspace(-0.5, 0.5, speakers.SAMPLE_RATE_HZ, dtype=np.float32)


class EmbedderTests(unittest.TestCase):
    def test_the_embedding_is_unit_length_though_the_model_output_is_not(self) -> None:
        embedder = SpeakerEmbedder(Path("model.onnx"), session=RecordingSession())

        vector = embedder.embed(one_second())

        self.assertEqual((EMBEDDING_DIMENSION,), vector.shape)
        self.assertAlmostEqual(1.0, float(np.linalg.norm(vector)), 6)
        self.assertAlmostEqual(0.6, float(vector[0]), 6)

    def test_the_model_is_fed_batched_normalised_features(self) -> None:
        session = RecordingSession()
        SpeakerEmbedder(Path("model.onnx"), session=session).embed(one_second())

        feats = session.feeds[speakers.MODEL_INPUT_NAME]
        self.assertEqual((1, 98, 80), feats.shape)
        self.assertLess(float(np.abs(feats[0].mean(axis=0)).max()), 1e-5)

    def test_audio_shorter_than_a_frame_is_refused(self) -> None:
        embedder = SpeakerEmbedder(Path("model.onnx"), session=RecordingSession())

        with self.assertRaises(ValueError):
            embedder.embed(np.zeros(100, dtype=np.float32))

    def test_the_session_is_built_lazily_on_the_cpu_with_the_arena_disabled(self) -> None:
        session = RecordingSession()
        with tempfile_model() as model_path, patch(
            "onnxruntime.InferenceSession", return_value=session
        ) as constructor:
            embedder = SpeakerEmbedder(model_path)
            self.assertFalse(embedder.resident)
            constructor.assert_not_called()

            embedder.embed(one_second())

        self.assertTrue(embedder.resident)
        self.assertEqual([CPU_PROVIDER], constructor.call_args.kwargs["providers"])
        options = constructor.call_args.kwargs["sess_options"]
        self.assertFalse(options.enable_cpu_mem_arena)
        self.assertEqual(0, options.intra_op_num_threads)
        self.assertEqual(CPU_PROVIDER, embedder.provider)

    def test_the_provider_is_read_back_from_the_session(self) -> None:
        session = RecordingSession(providers=("SomethingElseProvider", CPU_PROVIDER))
        with tempfile_model() as model_path, patch(
            "onnxruntime.InferenceSession", return_value=session
        ):
            embedder = SpeakerEmbedder(model_path)
            embedder.embed(one_second())

        self.assertEqual("SomethingElseProvider", embedder.provider)

    def test_a_missing_model_is_reported_and_retried(self) -> None:
        embedder = SpeakerEmbedder(Path("/nonexistent/model.onnx"))

        with self.assertRaises(FileNotFoundError):
            embedder.embed(one_second())

        self.assertFalse(embedder.resident)
        self.assertIn("missing", embedder.resident_detail)

        session = RecordingSession()
        with tempfile_model() as model_path, patch(
            "onnxruntime.InferenceSession", return_value=session
        ):
            embedder._model_path = model_path
            embedder.embed(one_second())
        self.assertIsNone(embedder.resident_detail)

    def test_resident_is_readable_while_both_locks_are_held(self) -> None:
        embedder = SpeakerEmbedder(Path("model.onnx"), session=RecordingSession())
        answers: list[bool] = []

        with embedder._load_lock, embedder._use_lock:
            reader = threading.Thread(target=lambda: answers.append(embedder.resident))
            reader.start()
            reader.join(GATE_TIMEOUT_SECONDS)

        self.assertEqual([True], answers)

    def test_release_drops_the_session_and_returns_the_heap_outside_the_locks(self) -> None:
        embedder = SpeakerEmbedder(Path("model.onnx"), session=RecordingSession())
        held: list[bool] = []

        with patch(
            "murmly.speakers.return_free_heap",
            side_effect=lambda: held.append(
                embedder._load_lock.locked() or embedder._use_lock.locked()
            ),
        ) as trim:
            self.assertTrue(embedder.release())
            self.assertFalse(embedder.release())

        trim.assert_called_once_with()
        self.assertEqual([False], held)
        self.assertFalse(embedder.resident)
        self.assertIsNone(embedder.provider)

    def test_a_release_during_an_inference_waits_rather_than_interrupting(self) -> None:
        session = GatedSession()
        embedder = SpeakerEmbedder(Path("model.onnx"), session=session)
        order: list[str] = []
        produced: list[object] = []

        inference = threading.Thread(target=lambda: produced.append(embedder.embed(one_second())))
        inference.start()
        self.assertTrue(session.entered.wait(GATE_TIMEOUT_SECONDS))

        def release() -> None:
            embedder.release()
            order.append("released")

        releaser = threading.Thread(target=release)
        releaser.start()
        releaser.join(0.05)

        self.assertTrue(releaser.is_alive(), "the release did not wait for the inference")
        self.assertTrue(embedder.resident)
        order.append("gate opened")
        session.gate.set()
        inference.join(GATE_TIMEOUT_SECONDS)
        releaser.join(GATE_TIMEOUT_SECONDS)

        self.assertEqual(["gate opened", "released"], order)
        self.assertEqual(1, len(produced))
        self.assertFalse(embedder.resident)

    def test_a_released_embedder_builds_another_session_on_the_next_embedding(self) -> None:
        sessions = [RecordingSession(), RecordingSession()]
        with tempfile_model() as model_path, patch(
            "onnxruntime.InferenceSession", side_effect=sessions
        ) as constructor:
            embedder = SpeakerEmbedder(model_path)
            embedder.embed(one_second())
            embedder.release()
            embedder.embed(one_second())

        self.assertEqual(2, constructor.call_count)


@contextlib.contextmanager
def tempfile_model():
    """A real file for the existence check; the session itself is a stand-in."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "model.onnx"
        path.write_bytes(b"")
        yield path


class RealModelTests(unittest.TestCase):
    """Runs the actual model when it is on this machine; skips otherwise.

    Looks at `MURMLY_SPEAKER_MODEL` first, then at the data directory where
    `setup.sh` puts it. CI has neither, so it skips there.
    """

    def setUp(self) -> None:
        override = os.environ.get("MURMLY_SPEAKER_MODEL")
        path = Path(override) if override else default_data_dir() / speakers.SPEAKER_MODEL_FILENAME
        if not path.is_file():
            self.skipTest(f"the speaker model is not at {path}")
        self.embedder = SpeakerEmbedder(path)

    def test_the_real_model_gives_a_unit_vector_of_the_documented_size(self) -> None:
        rng = np.random.default_rng(7)
        clip = (0.1 * rng.standard_normal(speakers.SAMPLE_RATE_HZ * 2)).astype(np.float32)

        first = self.embedder.embed(clip)
        second = self.embedder.embed(clip)

        self.assertEqual((EMBEDDING_DIMENSION,), first.shape)
        self.assertAlmostEqual(1.0, float(np.linalg.norm(first)), 5)
        self.assertAlmostEqual(1.0, float(first @ second), 5)
        self.assertEqual(CPU_PROVIDER, self.embedder.provider)
        self.embedder.release()


class FakeEmbedder:
    """Returns the audio it is handed: a test's `audio_for` supplies the vector."""

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, samples):
        self.calls += 1
        return np.asarray(samples, dtype=np.float64)


def part(text: str, start: float, end: float, vector=None) -> tuple[TimedText, object]:
    return TimedText(start, end, text), vector


def run(
    parts,
    *,
    mode="label-everyone",
    voices=(OWNER, MILO),
    owner="Ana",
    threshold=50,
    state=None,
    embedder=None,
):
    """`speaker_text` over `(TimedText, vector)` pairs, plus the embedder used."""
    vectors = {id(timed): vector for timed, vector in parts}
    embedder = embedder or FakeEmbedder()
    text = speaker_text(
        [timed for timed, _ in parts],
        lambda timed: vectors[id(timed)],
        mode=mode,
        voices=list(voices),
        owner=owner,
        embedder=embedder,
        state=state if state is not None else SpeakerState(),
        threshold_percent=threshold,
    )
    return text, embedder


class ThresholdTests(unittest.TestCase):
    def test_a_similarity_equal_to_the_threshold_matches(self) -> None:
        text, _ = run([part("Hello.", 0, 2, (3.0, 4.0, 0.0, 0.0))], voices=(OWNER,), threshold=60)

        self.assertEqual("You: Hello.", text)

    def test_a_similarity_just_below_the_threshold_does_not(self) -> None:
        text, _ = run([part("Hello.", 0, 2, (3.0, 4.0, 0.0, 0.0))], voices=(OWNER,), threshold=61)

        self.assertEqual("Speaker 1: Hello.", text)

    def test_an_unknown_voice_joins_a_cluster_only_at_or_above_the_threshold(self) -> None:
        stranger = (0.0, 0.0, 1.0, 0.0)
        near = (0.0, 0.0, 3.0, 4.0)  # similarity 0.6 to the first stranger
        expected = {
            60: "Speaker 1: A. B.",
            61: "Speaker 1: A. Speaker 2: B.",
        }
        for threshold, text in expected.items():
            with self.subTest(threshold=threshold):
                result, _ = run(
                    [part("A.", 0, 2, stranger), part("B.", 2, 4, near)],
                    voices=(),
                    owner="",
                    threshold=threshold,
                )
                self.assertEqual(text, result)


class AttributionTests(unittest.TestCase):
    def test_the_owner_and_another_enrolled_voice_are_told_apart(self) -> None:
        text, _ = run(
            [
                part("Shall we start?", 0, 3, (0.9, 0.1, 0.0, 0.0)),
                part("Yes.", 3, 5, (0.1, 0.9, 0.0, 0.0)),
            ]
        )

        self.assertEqual("You: Shall we start? Milo: Yes.", text)

    def test_the_best_match_wins_not_the_first_above_the_threshold(self) -> None:
        both = Voice("Ben", (0.8, 0.6, 0.0, 0.0))
        text, _ = run(
            [part("Hi.", 0, 2, (0.6, 0.8, 0.0, 0.0))], voices=(OWNER, both), threshold=50
        )

        self.assertEqual("Ben: Hi.", text)

    def test_mine_only_keeps_the_owner_and_drops_everyone_else(self) -> None:
        text, _ = run(
            [
                part("Owner one.", 0, 2, E0),
                part("Television.", 2, 4, E2),
                part("Milo speaks.", 4, 6, E1),
                part("Owner two.", 6, 8, E0),
            ],
            mode="mine-only",
        )

        self.assertEqual("Owner one. Owner two.", text)

    def test_mine_only_dropping_everything_is_the_empty_string_not_none(self) -> None:
        text, _ = run(
            [part("Television.", 0, 2, E2), part("Milo.", 2, 4, E1)], mode="mine-only"
        )

        self.assertEqual("", text)
        self.assertIsNotNone(text)

    def test_mine_only_with_only_the_owner_equals_the_joined_text(self) -> None:
        text, _ = run(
            [part("One two.", 0, 2, E0), part("Three.", 2, 4, E0)], mode="mine-only"
        )

        self.assertEqual("One two. Three.", text)

    def test_mine_only_without_an_enrolled_owner_is_delivered_as_off(self) -> None:
        for owner in ("", "Nobody"):
            with self.subTest(owner=owner):
                text, embedder = run(
                    [part("Hello.", 0, 2, E0)], mode="mine-only", owner=owner
                )
                self.assertIsNone(text)
                self.assertEqual(0, embedder.calls)

    def test_off_is_delivered_as_off(self) -> None:
        text, embedder = run([part("Hello.", 0, 2, E0)], mode="off")

        self.assertIsNone(text)
        self.assertEqual(0, embedder.calls)

    def test_the_owner_is_found_without_regard_to_letter_case(self) -> None:
        text, _ = run([part("Hello.", 0, 2, E0)], owner="ANA", mode="mine-only")

        self.assertEqual("Hello.", text)

    def test_label_everyone_with_no_owner_labels_enrolled_people_by_name(self) -> None:
        text, _ = run(
            [part("From Ana.", 0, 2, E0), part("From Milo.", 2, 4, E1)], owner=""
        )

        self.assertEqual("Ana: From Ana. Milo: From Milo.", text)
        self.assertNotIn("You:", text)

    def test_label_everyone_with_no_voices_enrolled_numbers_everyone(self) -> None:
        text, _ = run(
            [part("A.", 0, 2, E2), part("B.", 2, 4, E3)], voices=(), owner=""
        )

        self.assertEqual("Speaker 1: A. Speaker 2: B.", text)

    def test_unknown_voices_are_numbered_by_first_appearance(self) -> None:
        text, _ = run(
            [
                part("First.", 0, 2, E3),
                part("Second.", 2, 4, E2),
                part("First again.", 4, 6, E3),
            ]
        )

        self.assertEqual("Speaker 1: First. Speaker 2: Second. Speaker 1: First again.", text)

    def test_unknown_numbers_hold_across_calls_in_one_session(self) -> None:
        state = SpeakerState()
        first, _ = run([part("One.", 0, 2, E2)], state=state)
        second, _ = run([part("Two.", 0, 2, E3), part("Again.", 2, 4, E2)], state=state)

        self.assertEqual("Speaker 1: One.", first)
        self.assertEqual("Speaker 2: Two. Speaker 1: Again.", second)

    def test_a_new_session_starts_numbering_from_one(self) -> None:
        state = SpeakerState()
        run([part("One.", 0, 2, E2)], state=state)
        run([part("Two.", 0, 2, E3)], state=state)

        state.reset()
        fresh, _ = run([part("Three.", 0, 2, E3)], state=state)
        other, _ = run([part("Four.", 0, 2, E2)], state=SpeakerState())

        self.assertEqual("Speaker 1: Three.", fresh)
        self.assertEqual("Speaker 1: Four.", other)

    def test_the_cluster_centroid_is_a_normalised_running_mean(self) -> None:
        """Two members either side of the x axis make the x axis the centroid.

        Each member alone is 0.8 from a part on the axis; their mean is 1.0 from
        it. At threshold 90 the part joins only if the centroid is the mean.
        """
        state = SpeakerState()
        up = (0.8, 0.6, 0.0, 0.0)
        down = (0.8, -0.6, 0.0, 0.0)
        axis = (1.0, 0.0, 0.0, 0.0)
        # Their similarity to each other is 0.28, so 25 lets them join.
        run([part("A.", 0, 2, up)], voices=(), owner="", threshold=25, state=state)
        run([part("B.", 0, 2, down)], voices=(), owner="", threshold=25, state=state)
        self.assertEqual(1, state.unknown_count)

        text, _ = run([part("C.", 0, 2, axis)], voices=(), owner="", threshold=90, state=state)

        self.assertEqual("Speaker 1: C.", text)

    def test_an_error_from_the_embedder_propagates_to_the_caller(self) -> None:
        class Failing:
            def embed(self, samples):
                raise RuntimeError("session failed")

        with self.assertRaises(RuntimeError):
            run([part("Hello.", 0, 2, E0)], embedder=Failing())

    def test_parts_without_words_are_ignored(self) -> None:
        text, embedder = run([part("  ", 0, 2, E0), part("Hello.", 2, 4, E0)])

        self.assertEqual("You: Hello.", text)
        self.assertEqual(1, embedder.calls)


class ShortPartTests(unittest.TestCase):
    def test_owner_answers_briefly_after_speaking(self) -> None:
        text, embedder = run(
            [part("I will be there at noon.", 0, 3, E0), part("Yes.", 3, 3.4, E1)],
            mode="mine-only",
        )

        self.assertEqual("I will be there at noon. Yes.", text)
        self.assertEqual(1, embedder.calls, "the short part must not be embedded")

    def test_a_short_word_between_two_sentences_of_one_unknown_voice(self) -> None:
        text, _ = run(
            [
                part("Buy now.", 0, 2, E2),
                part("Right.", 2, 2.3, E0),
                part("Limited offer.", 2.3, 5, E2),
            ],
            mode="mine-only",
        )

        self.assertEqual("", text)

    def test_the_same_short_word_in_label_everyone_joins_the_unknown_voice(self) -> None:
        text, _ = run(
            [
                part("Buy now.", 0, 2, E2),
                part("Right.", 2, 2.3, E0),
                part("Limited offer.", 2.3, 5, E2),
            ]
        )

        self.assertEqual("Speaker 1: Buy now. Right. Limited offer.", text)

    def test_a_recording_of_one_short_word_is_delivered_as_off_in_both_modes(self) -> None:
        for mode in ("mine-only", "label-everyone"):
            with self.subTest(mode=mode):
                text, embedder = run([part("Yes.", 0, 0.4, E0)], mode=mode)
                self.assertIsNone(text)
                self.assertEqual(0, embedder.calls)

    def test_a_tie_goes_to_the_part_before(self) -> None:
        text, _ = run(
            [part("Mine.", 0, 2, E0), part("Hm.", 2, 2.3), part("Theirs.", 2.3, 4.5, E2)],
            mode="mine-only",
        )

        self.assertEqual("Mine. Hm.", text)

    def test_the_nearer_part_wins_when_it_is_the_one_after(self) -> None:
        text, _ = run(
            [
                part("Mine.", 0, 2, E0),
                part("Uh.", 2, 2.2),
                part("Um.", 2.2, 2.4),
                part("Theirs.", 2.4, 5, E2),
            ],
            mode="mine-only",
        )

        self.assertEqual("Mine. Uh.", text)

    def test_a_short_part_at_the_start_takes_the_part_after(self) -> None:
        text, _ = run(
            [part("Ok.", 0, 0.3), part("Then I went home.", 0.3, 3, E0)], mode="mine-only"
        )

        self.assertEqual("Ok. Then I went home.", text)

    def test_a_part_of_exactly_the_minimum_length_is_embedded(self) -> None:
        text, embedder = run([part("Hello.", 0.12, 1.12, E0)], mode="mine-only")

        self.assertEqual("Hello.", text)
        self.assertEqual(1, embedder.calls)

    def test_short_parts_take_no_unknown_number(self) -> None:
        text, _ = run(
            [part("Short.", 0, 0.3), part("Long one.", 0.3, 3, E2)],
            voices=(),
            owner="",
        )

        self.assertEqual("Speaker 1: Short. Long one.", text)


class FormattingTests(unittest.TestCase):
    def test_the_specs_example_string(self) -> None:
        text, _ = run(
            [part("Shall we start?", 0, 2, E0), part("Yes, go ahead.", 2, 4, E2)]
        )

        self.assertEqual("You: Shall we start? Speaker 1: Yes, go ahead.", text)

    def test_consecutive_parts_from_one_speaker_share_a_label(self) -> None:
        text, _ = run(
            [
                part("First sentence.", 0, 2, E0),
                part("Second sentence.", 2, 4, E0),
                part("Reply.", 4, 6, E2),
            ]
        )

        self.assertEqual(
            "You: First sentence. Second sentence. Speaker 1: Reply.", text
        )

    def test_every_segment_of_a_session_begins_with_a_label(self) -> None:
        state = SpeakerState()
        first, _ = run([part("One.", 0, 2, E0)], state=state)
        second, _ = run([part("Two.", 0, 2, E0)], state=state)

        self.assertEqual("You: One.", first)
        self.assertEqual("You: Two.", second)

    def test_a_line_break_in_whisper_text_never_reaches_the_output(self) -> None:
        for mode in ("mine-only", "label-everyone"):
            with self.subTest(mode=mode):
                text, _ = run(
                    [
                        part("Line one.\nLine two.\r\n", 0, 2, E0),
                        part(" Other\x0bvoice.\x85", 2, 4, E2),
                        part("Owner again.\n\n", 4, 6, E0),
                    ],
                    mode=mode,
                )
                for character in "\n\r\x0b\x0c\x85  ":
                    self.assertNotIn(character, text)
                self.assertIn("Line one. Line two.", text)

    def test_text_without_a_line_break_is_untouched(self) -> None:
        text, _ = run([part("  Two  spaces inside.  ", 0, 2, E0)], mode="mine-only")

        self.assertEqual("Two  spaces inside.", text)

    def test_a_name_that_is_a_label_in_disguise_cannot_exist_so_labels_are_unambiguous(
        self,
    ) -> None:
        text, _ = run(
            [part("A.", 0, 2, E1), part("B.", 2, 4, E2)],
            voices=(MILO,),
            owner="",
        )

        self.assertEqual("Milo: A. Speaker 1: B.", text)


class NameTests(unittest.TestCase):
    def test_a_good_name_is_returned_with_outer_spaces_removed(self) -> None:
        self.assertEqual("Milo", validate_name("Milo"))
        self.assertEqual("Milo", validate_name("  Milo "))
        self.assertEqual("Mary Ann", validate_name("Mary Ann"))
        self.assertEqual("Zoë", validate_name("Zoë"))

    def test_each_rejected_form(self) -> None:
        rejected = {
            "empty": "",
            "only spaces": "   ",
            "too long": "x" * 33,
            "line feed": "Mi\nlo",
            "trailing line feed": "Milo\n",
            "carriage return": "Mi\rlo",
            "tab": "Mi\tlo",
            "control character": "Mi\x00lo",
            "escape": "Milo\x1b",
            "next line": "Mi\x85lo",
            "line separator": "Mi lo",
            "paragraph separator": "Mi lo",
            "colon": "Milo: Jr",
            "lone colon": ":",
            "you": "You",
            "you lower": "you",
            "you upper": "YOU",
            "you with spaces": "  you ",
            "speaker 2": "speaker 2",
            "Speaker 2": "Speaker 2",
            "SPEAKER 12": "SPEAKER 12",
            "speaker with leading zero": "Speaker 01",
            "speaker with two spaces": "Speaker  3",
        }
        for label, name in rejected.items():
            with self.subTest(label):
                with self.assertRaises(InvalidSpeakerName):
                    validate_name(name)

    def test_the_edges_that_are_accepted(self) -> None:
        for name in ("x" * 32, "Speaker", "Speaker two", "Speaker 2b", "Yourself", "You Too"):
            with self.subTest(name):
                self.assertEqual(name, validate_name(name))

    def test_names_compare_without_regard_to_case_or_outer_spaces(self) -> None:
        self.assertTrue(same_name("Milo", "milo"))
        self.assertTrue(same_name(" Milo", "MILO "))
        self.assertTrue(same_name("Straße", "STRASSE"))
        self.assertFalse(same_name("Milo", "Mila"))


class StaysPureTests(unittest.TestCase):
    def test_attribute_accepts_any_object_with_start_end_and_text(self) -> None:
        class Segment:
            start_s, end_s, text = 0.0, 2.0, "Hello."

        result = attribute(
            [Segment()],
            lambda part: E0,
            voices=[OWNER],
            owner="Ana",
            embedder=FakeEmbedder(),
            state=SpeakerState(),
            threshold_percent=50,
        )

        self.assertEqual("You", result[0].speaker.label)


if __name__ == "__main__":
    unittest.main()
