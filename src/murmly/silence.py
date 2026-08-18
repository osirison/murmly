from __future__ import annotations

from dataclasses import dataclass
import logging


logger = logging.getLogger(__name__)

VAD_SAMPLE_RATE_HZ = 16_000
VAD_FRAME_SAMPLES = 512
VAD_FRAME_MS = 1_000 * VAD_FRAME_SAMPLES // VAD_SAMPLE_RATE_HZ
SPEECH_THRESHOLD = 0.5
MIN_WINDOW_MS = 3_000
WINDOW_MARGIN_MS = 1_000


@dataclass(frozen=True, slots=True)
class SilenceReading:
    speech_detected: bool
    silence_ms: int
    triggered: bool


class SilenceDetector:
    """Measures the trailing run of silence in captured audio.

    Each reading re-examines a bounded trailing window rather than accumulating
    per-chunk verdicts, because the voice activity model resets its recurrent
    state on every call: a window wide enough to contain the whole silence run
    gives a consistent measurement, where chunk-at-a-time feeding would not.
    """

    def __init__(
        self,
        sample_rate_hz: int,
        channels: int,
        *,
        silence_ms: int,
        min_speech_ms: int,
        vad_model=None,
        threshold: float = SPEECH_THRESHOLD,
    ) -> None:
        self._sample_rate_hz = sample_rate_hz
        self._channels = max(channels, 1)
        self._silence_ms = silence_ms
        self._min_speech_ms = min_speech_ms
        self._threshold = threshold
        self._speech_detected = False
        self._silence_ms_observed = 0
        self._unavailable_reason: str | None = None
        self._decimation = self._resolve_decimation()
        self._model = vad_model
        if self._model is None and self._unavailable_reason is None:
            self._model = self._load_model()

    @property
    def available(self) -> bool:
        return self._unavailable_reason is None

    @property
    def unavailable_reason(self) -> str | None:
        return self._unavailable_reason

    @property
    def speech_detected(self) -> bool:
        return self._speech_detected

    @property
    def window_seconds(self) -> float:
        return max(self._silence_ms + WINDOW_MARGIN_MS, MIN_WINDOW_MS) / 1_000

    def reset(self) -> None:
        """Forget the current segment, so the next one must observe speech again."""
        self._speech_detected = False
        self._silence_ms_observed = 0

    def observe(self, pcm_audio: bytes) -> SilenceReading:
        if not self.available:
            return SilenceReading(self._speech_detected, 0, False)

        frames = self._to_vad_frames(pcm_audio)
        if frames is None:
            return SilenceReading(self._speech_detected, self._silence_ms_observed, False)

        try:
            probabilities = self._model(frames)
        except Exception as error:
            self._unavailable_reason = f"Voice activity detection failed: {error}"
            logger.warning("Silence detection disabled: %s", self._unavailable_reason)
            return SilenceReading(self._speech_detected, 0, False)

        speech = [float(value) >= self._threshold for value in probabilities]
        if sum(speech) * VAD_FRAME_MS >= self._min_speech_ms and any(speech):
            self._speech_detected = True

        trailing = 0
        for is_speech in reversed(speech):
            if is_speech:
                break
            trailing += 1
        self._silence_ms_observed = trailing * VAD_FRAME_MS

        triggered = self._speech_detected and self._silence_ms_observed >= self._silence_ms
        return SilenceReading(self._speech_detected, self._silence_ms_observed, triggered)

    def _resolve_decimation(self) -> int:
        if self._sample_rate_hz < VAD_SAMPLE_RATE_HZ or self._sample_rate_hz % VAD_SAMPLE_RATE_HZ:
            self._unavailable_reason = (
                f"capture rate {self._sample_rate_hz} Hz is not an integer multiple of "
                f"{VAD_SAMPLE_RATE_HZ} Hz"
            )
            return 0
        return self._sample_rate_hz // VAD_SAMPLE_RATE_HZ

    def _load_model(self):
        try:
            from faster_whisper.vad import get_vad_model

            return get_vad_model()
        except Exception as error:
            self._unavailable_reason = f"voice activity detection is unavailable: {error}"
            return None

    def _to_vad_frames(self, pcm_audio: bytes):
        import numpy as np

        usable_length = len(pcm_audio) - (len(pcm_audio) % 2)
        if usable_length == 0:
            return None
        samples = np.frombuffer(pcm_audio[:usable_length], dtype=np.int16).astype(np.float32)

        if self._channels > 1:
            usable = len(samples) - (len(samples) % self._channels)
            if usable == 0:
                return None
            samples = samples[:usable].reshape(-1, self._channels)[:, 0]

        if self._decimation > 1:
            usable = len(samples) - (len(samples) % self._decimation)
            if usable == 0:
                return None
            # Averaging rather than picking every nth sample: a box filter is a
            # crude anti-alias, and aliased energy reads as speech to the model.
            samples = samples[:usable].reshape(-1, self._decimation).mean(axis=1)

        frame_count = len(samples) // VAD_FRAME_SAMPLES
        if frame_count == 0:
            return None
        # Keep the tail: the trailing silence run is what the caller acts on.
        tail = samples[len(samples) - frame_count * VAD_FRAME_SAMPLES :]
        return np.ascontiguousarray(tail, dtype=np.float32) / 32_768.0
