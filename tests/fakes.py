"""Fakes shared by the tests that exercise speech output.

Kept out of any `test_*` module because the daemon, session and audio suites all
need the same synthesizer stand-in, and none of them may load a model: the real
one is 326 MB of weights and half a second of construction per test.
"""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np


FAKE_SAMPLE_RATE_HZ = 24_000
SAMPLES_PER_CHARACTER = 24  # 1 ms of audio per character, so durations stay small


def fake_amplitude(text: str) -> float:
    """A distinct, deterministic amplitude for a piece of text.

    Deliberately far from the amplitudes the capture fakes produce, so a test
    asserting that a recording contains none of Murmly's own speech can tell the
    two apart by value rather than by timing.
    """
    return 0.25 + (sum(text.encode("utf-8")) % 64) / 256.0


class FakeSynthesizer:
    """Known audio for known text, with the surface `KokoroSynthesizer` exposes.

    One sentence in, one chunk out, at a fixed rate. `spoken` records every text
    it was asked for, so a test can assert what was produced without inspecting
    audio.
    """

    def __init__(
        self,
        *,
        available: bool = True,
        unavailable_reason: str | None = None,
        voice: str = "af_heart",
        rate_percent: int = 100,
        sample_rate_hz: int = FAKE_SAMPLE_RATE_HZ,
        error: Exception | None = None,
    ) -> None:
        self._available = available
        self._unavailable_reason = unavailable_reason
        self.voice = voice
        self.rejected_voice: str | None = None
        self.rate_percent = rate_percent
        self.sample_rate_hz = sample_rate_hz
        self.provider = "FakeExecutionProvider"
        self.spoken: list[str] = []
        self._error = error

    @property
    def available(self) -> bool:
        return self._available

    @property
    def unavailable_reason(self) -> str | None:
        return self._unavailable_reason

    def synthesize(self, text: str) -> Iterator[tuple[np.ndarray, int]]:
        self.spoken.append(text)
        if self._error is not None:
            raise self._error
        for sentence in split_for_fake(text):
            yield audio_for(sentence), self.sample_rate_hz


def split_for_fake(text: str) -> list[str]:
    """Sentence split that matches what the fake's callers expect of it."""
    sentences = [part.strip() for part in text.replace("!", ".").replace("?", ".").split(".")]
    return [sentence for sentence in sentences if sentence]


def audio_for(sentence: str) -> np.ndarray:
    """The chunk `FakeSynthesizer` produces for one sentence."""
    length = max(len(sentence) * SAMPLES_PER_CHARACTER, SAMPLES_PER_CHARACTER)
    return np.full(length, fake_amplitude(sentence), dtype=np.float32)


def expected_audio(text: str) -> np.ndarray:
    """Everything `FakeSynthesizer` would produce for a piece of text, joined."""
    chunks = [audio_for(sentence) for sentence in split_for_fake(text)]
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks)


def expected_pcm16(text: str) -> bytes:
    """The same audio in the little-endian int16 the output path writes."""
    return to_pcm16(expected_audio(text))


def to_pcm16(samples: np.ndarray) -> bytes:
    scaled = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32_767.0
    return np.round(scaled).astype("<i2").tobytes()


class FakeSession:
    """The ONNX session stand-in, whose provider list is what gets read back."""

    def __init__(self, providers: tuple[str, ...] = ("CPUExecutionProvider",)) -> None:
        self._providers = list(providers)

    def get_providers(self) -> list[str]:
        return list(self._providers)


class FakeKokoroModel:
    """A synthesis model with the behaviour the real one was measured to have.

    One sentence in gives leading silence, speech and trailing silence. A whole
    passage in gives the same, with `boundary_pause` of silence between one
    sentence's speech and the next -- which is the silence that independent
    production drops and that the synthesizer has to put back.
    """

    def __init__(
        self,
        *,
        boundary_pause: float = 0.30,
        lead: float = 0.0,
        tail: float = 0.10,
        sample_rate_hz: int = FAKE_SAMPLE_RATE_HZ,
        voices: tuple[str, ...] = ("af_heart", "bf_emma"),
        providers: tuple[str, ...] = ("CPUExecutionProvider",),
        error: Exception | None = None,
    ) -> None:
        self.boundary_pause = boundary_pause
        self.lead = lead
        self.tail = tail
        self.sample_rate_hz = sample_rate_hz
        self.voices = voices
        self.sess = FakeSession(providers)
        self.calls: list[tuple[str, str, float]] = []
        self._error = error

    def create(self, text, voice="af_heart", speed=1.0, lang="en-us"):
        from murmly.tts import split_sentences

        self.calls.append((text, voice, speed))
        if self._error is not None:
            raise self._error
        if voice not in self.voices:
            raise ValueError(f"Voice {voice} not found in available voices")

        parts: list[np.ndarray] = []
        for index, sentence in enumerate(split_sentences(text)):
            if index:
                # What the whole-passage rendering puts between two sentences,
                # less the silence the two chunks carry at their own edges.
                parts.append(self._silence(self.boundary_pause - self.tail - self.lead))
            parts.append(self._silence(self.lead))
            parts.append(np.full(self._speech_samples(sentence, speed), 0.5, dtype=np.float32))
            parts.append(self._silence(self.tail))
        if not parts:
            return np.zeros(0, dtype=np.float32), self.sample_rate_hz
        return np.concatenate(parts), self.sample_rate_hz

    def _speech_samples(self, sentence: str, speed: float) -> int:
        return max(int(len(sentence) * SAMPLES_PER_CHARACTER / speed), 1)

    def _silence(self, seconds: float) -> np.ndarray:
        return np.zeros(max(int(seconds * self.sample_rate_hz), 0), dtype=np.float32)
