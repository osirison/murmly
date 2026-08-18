from __future__ import annotations

import unittest

from murmly.silence import (
    VAD_FRAME_MS,
    VAD_FRAME_SAMPLES,
    VAD_SAMPLE_RATE_HZ,
    SilenceDetector,
)


class FakeVadModel:
    """Returns a scripted probability per 512-sample frame."""

    def __init__(self, probability_for_frame) -> None:
        self._probability_for_frame = probability_for_frame
        self.calls: list[int] = []

    def __call__(self, audio):
        frame_count = len(audio) // VAD_FRAME_SAMPLES
        self.calls.append(frame_count)
        return [self._probability_for_frame(index, frame_count) for index in range(frame_count)]


def pcm16(sample_count: int, value: int = 0, *, channels: int = 1) -> bytes:
    return (int(value).to_bytes(2, "little", signed=True) * channels) * sample_count


class SilenceDetectorTests(unittest.TestCase):
    def _detector(self, model, *, sample_rate_hz: int = VAD_SAMPLE_RATE_HZ, **kwargs):
        options = {"silence_ms": 2_000, "min_speech_ms": 300, "channels": 1}
        options.update(kwargs)
        return SilenceDetector(sample_rate_hz, vad_model=model, **options)

    def test_trailing_silence_is_measured_from_the_last_speech_frame(self) -> None:
        # 100 frames: the first 20 are speech, the trailing 80 are silence.
        model = FakeVadModel(lambda index, _total: 0.9 if index < 20 else 0.0)
        detector = self._detector(model)

        reading = detector.observe(pcm16(100 * VAD_FRAME_SAMPLES))

        self.assertTrue(reading.speech_detected)
        self.assertEqual(80 * VAD_FRAME_MS, reading.silence_ms)
        self.assertTrue(reading.triggered)

    def test_silence_shorter_than_the_configured_duration_does_not_trigger(self) -> None:
        # 30 trailing silent frames is 960 ms, below the 2000 ms threshold.
        model = FakeVadModel(lambda index, _total: 0.9 if index < 20 else 0.0)
        detector = self._detector(model)

        reading = detector.observe(pcm16(50 * VAD_FRAME_SAMPLES))

        self.assertTrue(reading.speech_detected)
        self.assertEqual(30 * VAD_FRAME_MS, reading.silence_ms)
        self.assertFalse(reading.triggered)

    def test_silence_before_any_speech_never_triggers(self) -> None:
        model = FakeVadModel(lambda _index, _total: 0.0)
        detector = self._detector(model)

        for _ in range(5):
            reading = detector.observe(pcm16(200 * VAD_FRAME_SAMPLES))

        self.assertFalse(reading.speech_detected)
        self.assertFalse(reading.triggered)
        self.assertGreater(reading.silence_ms, 2_000)

    def test_speech_shorter_than_min_speech_does_not_arm_the_trigger(self) -> None:
        # Two speech frames is 64 ms, below the 300 ms minimum.
        model = FakeVadModel(lambda index, _total: 0.9 if index < 2 else 0.0)
        detector = self._detector(model)

        reading = detector.observe(pcm16(100 * VAD_FRAME_SAMPLES))

        self.assertFalse(reading.speech_detected)
        self.assertFalse(reading.triggered)

    def test_reset_requires_speech_again_for_the_next_segment(self) -> None:
        model = FakeVadModel(lambda index, _total: 0.9 if index < 20 else 0.0)
        detector = self._detector(model)
        self.assertTrue(detector.observe(pcm16(100 * VAD_FRAME_SAMPLES)).triggered)

        detector.reset()
        self.assertFalse(detector.speech_detected)

        silent = FakeVadModel(lambda _index, _total: 0.0)
        detector._model = silent
        self.assertFalse(detector.observe(pcm16(100 * VAD_FRAME_SAMPLES)).triggered)

    def test_forty_eight_kilohertz_audio_is_decimated_to_vad_frames(self) -> None:
        model = FakeVadModel(lambda _index, _total: 0.0)
        detector = self._detector(model, sample_rate_hz=48_000)

        self.assertTrue(detector.available)
        # Three input samples collapse to one, so 3 * 512 * 10 samples is 10 frames.
        detector.observe(pcm16(3 * VAD_FRAME_SAMPLES * 10))

        self.assertEqual([10], model.calls)

    def test_multichannel_capture_uses_the_first_channel(self) -> None:
        model = FakeVadModel(lambda _index, _total: 0.0)
        detector = self._detector(model, channels=2)

        detector.observe(pcm16(VAD_FRAME_SAMPLES * 4, channels=2))

        self.assertEqual([4], model.calls)

    def test_unsupported_capture_rate_reports_unavailable(self) -> None:
        model = FakeVadModel(lambda _index, _total: 0.9)
        detector = self._detector(model, sample_rate_hz=44_100)

        self.assertFalse(detector.available)
        self.assertIn("44100", detector.unavailable_reason)
        reading = detector.observe(pcm16(100 * VAD_FRAME_SAMPLES))
        self.assertFalse(reading.triggered)
        self.assertEqual([], model.calls)

    def test_capture_rate_below_the_vad_rate_reports_unavailable(self) -> None:
        detector = self._detector(FakeVadModel(lambda *_: 0.0), sample_rate_hz=8_000)

        self.assertFalse(detector.available)

    def test_audio_shorter_than_one_frame_leaves_the_reading_unchanged(self) -> None:
        model = FakeVadModel(lambda _index, _total: 0.0)
        detector = self._detector(model)

        reading = detector.observe(pcm16(VAD_FRAME_SAMPLES - 1))

        self.assertEqual([], model.calls)
        self.assertFalse(reading.triggered)

    def test_model_failure_disables_detection_without_raising(self) -> None:
        class ExplodingModel:
            def __call__(self, _audio):
                raise RuntimeError("onnx session died")

        detector = self._detector(ExplodingModel())

        reading = detector.observe(pcm16(100 * VAD_FRAME_SAMPLES))

        self.assertFalse(reading.triggered)
        self.assertFalse(detector.available)
        self.assertIn("onnx session died", detector.unavailable_reason)

    def test_window_seconds_covers_the_configured_silence_duration(self) -> None:
        detector = self._detector(FakeVadModel(lambda *_: 0.0), silence_ms=5_000)

        self.assertGreater(detector.window_seconds, 5.0)

    def test_bundled_vad_model_loads_and_scores_silence(self) -> None:
        detector = SilenceDetector(
            VAD_SAMPLE_RATE_HZ,
            1,
            silence_ms=2_000,
            min_speech_ms=300,
        )
        if not detector.available:
            self.skipTest(f"bundled VAD unavailable: {detector.unavailable_reason}")

        reading = detector.observe(pcm16(100 * VAD_FRAME_SAMPLES))

        self.assertFalse(reading.speech_detected)
        self.assertEqual(100 * VAD_FRAME_MS, reading.silence_ms)


if __name__ == "__main__":
    unittest.main()
