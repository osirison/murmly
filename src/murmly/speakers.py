"""Telling voices apart: the speaker model, its features, and attribution.

The model is WeSpeaker ResNet34-LM trained on VoxCeleb, published by the
WeSpeaker project under CC BY 4.0 (`licenses/wespeaker-resnet34-lm-license.txt`).
`setup.sh` and `bootstrap.ps1` download it from the URL below into the data
directory and check its SHA-256 against the value below;
`tests/test_speakers_model_pin.py` fails when either script drifts from these
three constants. The URL is pinned to one commit of the publisher's repository,
so a later upload cannot change what is fetched, and the checksum would refuse
it if it did.

Everything after the constants is the attribution core:

- `fbank_features` and `normalised_features` turn 16 kHz audio into what the
  model takes, in numpy because `kaldi-native-fbank` has no cp314 Windows wheel.
- `SpeakerEmbedder` holds the ONNX session, built lazily and released on demand.
- `speaker_text` takes the timed parts of one transcript and returns the text
  mine-only or label-everyone delivers.

Attribution is pure apart from the calls it makes on the embedder, so the daemon
decides when to read voiceprints, what audio a part covers, and what a failure
costs. See `openspec/changes/add-speaker-recognition/design.md`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import functools
import logging
from pathlib import Path
import re
import threading
from typing import Protocol

from murmly.idle import return_free_heap


logger = logging.getLogger(__name__)


SPEAKER_MODEL_URL = (
    "https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/"
    "f0c48c298fd835726c27956a5d617bad7115627e/voxceleb_resnet34_LM.onnx"
)
SPEAKER_MODEL_FILENAME = "voxceleb_resnet34_LM.onnx"
SPEAKER_MODEL_SHA256 = "7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068"
EMBEDDING_DIMENSION = 256

#: The one rate the model takes. Callers resample to it before embedding.
SAMPLE_RATE_HZ = 16_000

#: Named here rather than imported from `murmly.tts`: that module imports
#: `murmly.stt`, which will import `TimedText` from this one.
CPU_PROVIDER = "CPUExecutionProvider"

#: The model's input name, from its graph (`feats`, ['B', 'T', 80]).
MODEL_INPUT_NAME = "feats"

#: A part shorter than this is not embedded: the LM model is fine-tuned on about
#: 6 s and similarities fall off steeply below 2 s. A placeholder until task 4.7
#: measures it on real recordings.
MIN_PART_SECONDS = 1.0

#: The longest name an enrolled voice may have, and the labels Murmly reserves.
MAX_NAME_LENGTH = 32
OWNER_LABEL = "You"
UNKNOWN_LABEL_PREFIX = "Speaker"

# --- Features ---------------------------------------------------------------

_FRAME_LENGTH = 400  # 25 ms at 16 kHz
_FRAME_SHIFT = 160  # 10 ms
_FFT_SIZE = 512  # the frame length rounded up to a power of two
_MEL_BINS = 80
_LOW_FREQUENCY_HZ = 20.0
_PREEMPHASIS = 0.97
# The scale the publisher feeds its model: `waveform * (1 << 15)`.
_INT16_SCALE = 32768.0


def _mel(frequency_hz):
    """Kaldi's mel scale. Not the HTK variant that uses base-10 or 2595."""
    import numpy as np

    return 1127.0 * np.log(1.0 + frequency_hz / 700.0)


@functools.lru_cache(maxsize=1)
def _mel_filterbank():
    """Kaldi's triangular filters, triangular in the mel domain.

    Shape (80, 256): the FFT's 257 bins without Nyquist, as Kaldi drops it.
    """
    import numpy as np

    nyquist = SAMPLE_RATE_HZ / 2.0
    low = _mel(_LOW_FREQUENCY_HZ)
    high = _mel(nyquist)
    delta = (high - low) / (_MEL_BINS + 1)
    bins = np.arange(_MEL_BINS)[:, None]
    left = low + bins * delta
    center = low + (bins + 1) * delta
    right = low + (bins + 2) * delta
    mel = _mel(np.arange(_FFT_SIZE // 2) * (SAMPLE_RATE_HZ / _FFT_SIZE))[None, :]
    up = (mel - left) / (center - left)
    down = (right - mel) / (right - center)
    return np.maximum(0.0, np.minimum(up, down))


@functools.lru_cache(maxsize=1)
def _hamming_window():
    """`torch.hamming_window(400, periodic=False)`: 0.54 and 0.46, not Povey."""
    import numpy as np

    n = np.arange(_FRAME_LENGTH)
    return 0.54 - 0.46 * np.cos(2.0 * np.pi * n / (_FRAME_LENGTH - 1))


def fbank_features(samples):
    """80-bin log-mel filterbank of 16 kHz mono float audio in [-1, 1].

    Kaldi's `fbank` as the model's publisher calls it: 25 ms frames every 10 ms
    with `snip_edges` (no padding, a trailing partial frame is dropped), DC
    offset removed per frame, pre-emphasis 0.97, Hamming window, a 512-point
    FFT, power spectrum, mel bins from 20 Hz to Nyquist, natural log floored at
    the float32 epsilon, no dither. Returns shape (frames, 80), float32, with
    zero frames for audio shorter than one frame.
    """
    import numpy as np

    audio = np.asarray(samples, dtype=np.float64) * _INT16_SCALE
    if audio.size < _FRAME_LENGTH:
        return np.zeros((0, _MEL_BINS), dtype=np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(audio, _FRAME_LENGTH)[::_FRAME_SHIFT]
    frames = frames - frames.mean(axis=1, keepdims=True)
    emphasised = frames.copy()
    emphasised[:, 1:] -= _PREEMPHASIS * frames[:, :-1]
    emphasised[:, 0] -= _PREEMPHASIS * frames[:, 0]
    spectrum = np.fft.rfft(emphasised * _hamming_window(), n=_FFT_SIZE, axis=1)
    power = (spectrum.real**2 + spectrum.imag**2)[:, : _FFT_SIZE // 2]
    energies = power @ _mel_filterbank().T
    floor = np.finfo(np.float32).eps
    return np.log(np.maximum(energies, floor)).astype(np.float32)


def normalised_features(samples):
    """`fbank_features` with each bin's mean over the utterance subtracted.

    Cepstral mean normalisation without variance normalisation, the same at
    enrolment and at recognition.
    """
    features = fbank_features(samples)
    if features.shape[0] == 0:
        return features
    return features - features.mean(axis=0, keepdims=True)


def l2_normalised(vector):
    """The vector scaled to unit length, as float64. A zero vector is refused."""
    import numpy as np

    array = np.asarray(vector, dtype=np.float64)
    norm = float(np.linalg.norm(array))
    if not norm > 0.0 or not np.isfinite(norm):
        raise ValueError("a voiceprint cannot be made from an all-zero or non-finite vector")
    return array / norm


# --- The embedding session ---------------------------------------------------


class Embedder(Protocol):
    """What attribution needs from the model: audio in, a unit vector out."""

    def embed(self, samples):
        """16 kHz mono float32 audio to an L2-normalised embedding."""
        ...


class SpeakerEmbedder:
    """The speaker model's ONNX session, built when first used and droppable.

    Follows the synthesis session in `murmly.tts`: a load lock and a use lock
    taken in that order by `release`, `enable_cpu_mem_arena = False`, intra-op
    threads left at the runtime default (journal 2026-08-25), a residency read
    that takes no lock, and `return_free_heap` after a release. The CPU provider
    is named explicitly: the model is small enough that a GPU gains nothing, and
    the 2026-08-26 measurement had the CUDA provider holding 876 MB against 65 MB
    on the CPU and never returning it.
    """

    def __init__(self, model_path: Path, session=None) -> None:
        self._model_path = Path(model_path)
        self._session = session
        self._provider: str | None = CPU_PROVIDER if session is not None else None
        self._load_error: str | None = None
        self._load_lock = threading.Lock()
        self._use_lock = threading.Lock()

    @property
    def model_path(self) -> Path:
        return self._model_path

    @property
    def resident(self) -> bool:
        """Whether a session exists right now, asked without taking a lock."""
        return self._session is not None

    @property
    def resident_detail(self) -> str | None:
        """Why the last build failed, or None. Cleared by the next build that works."""
        return self._load_error

    @property
    def provider(self) -> str | None:
        """The provider the session reports, or None while none exists."""
        return self._provider

    def embed(self, samples):
        """The L2-normalised embedding of 16 kHz mono float32 audio.

        The raw model output has a norm of about 0.8, so it is normalised here
        and nothing downstream has to remember to.
        """
        import numpy as np

        features = normalised_features(samples)
        if features.shape[0] == 0:
            raise ValueError("audio is shorter than one 25 ms frame")
        session = self._load()
        with self._use_lock:
            outputs = session.run(None, {MODEL_INPUT_NAME: features[None, :, :]})
        return l2_normalised(np.asarray(outputs[0], dtype=np.float32)[0]).astype(np.float32)

    def release(self) -> bool:
        """Drop the session and hand its memory back. Waits for an inference.

        Reports whether anything was released. The two locks are taken in the
        order the use path takes them, for the reason given in
        `KokoroSynthesizer.release`: a release holding only the use lock could
        interleave with a build and leave the new session behind it.
        """
        with self._load_lock, self._use_lock:
            if self._session is None:
                return False
            self._session = None
            self._provider = None
        return_free_heap()
        logger.debug("Released the speaker model.")
        return True

    def _load(self):
        with self._load_lock:
            if self._session is None:
                try:
                    session = self._construct()
                except Exception as error:
                    self._load_error = str(error) or type(error).__name__
                    raise
                self._session = session
                self._load_error = None
            return self._session

    def _construct(self):
        if not self._model_path.is_file():
            raise FileNotFoundError(f"the speaker model is missing: {self._model_path}")
        import onnxruntime

        options = onnxruntime.SessionOptions()
        options.enable_cpu_mem_arena = False
        session = onnxruntime.InferenceSession(
            str(self._model_path), sess_options=options, providers=[CPU_PROVIDER]
        )
        providers = session.get_providers()
        self._provider = providers[0] if providers else CPU_PROVIDER
        return session


# --- Names ---------------------------------------------------------------------


class InvalidSpeakerName(ValueError):
    """An enrolment name that cannot be typed into a document as a label."""


_UNKNOWN_LABEL = re.compile(rf"{UNKNOWN_LABEL_PREFIX}\s+\d+", re.IGNORECASE)


def validate_name(name: str) -> str:
    """The name as it will be stored, or `InvalidSpeakerName` saying why not.

    Names become labels in typed text, so one may not be empty, longer than 32
    characters, hold a line break, a control character or a colon, or read as a
    label Murmly writes itself: "You", or "Speaker" and a number, in any case.
    "Speaker" alone and "Speaker two" are not labels and are allowed.

    The check for control characters runs on the name as given, so "Milo\\n" is
    refused rather than stripped into something valid. Only spaces at either end
    are removed afterwards, which is what the configured owner gets too.
    """
    import unicodedata

    for character in name:
        if unicodedata.category(character) in {"Cc", "Zl", "Zp"}:
            raise InvalidSpeakerName("a name cannot contain a line break or a control character")
    stripped = name.strip()
    if not stripped:
        raise InvalidSpeakerName("a name cannot be empty")
    if len(stripped) > MAX_NAME_LENGTH:
        raise InvalidSpeakerName(f"a name cannot be longer than {MAX_NAME_LENGTH} characters")
    if ":" in stripped:
        raise InvalidSpeakerName("a name cannot contain a colon")
    if same_name(stripped, OWNER_LABEL) or _UNKNOWN_LABEL.fullmatch(stripped):
        raise InvalidSpeakerName(
            f'a name cannot be "{OWNER_LABEL}" or "{UNKNOWN_LABEL_PREFIX}" and a number'
        )
    return stripped


def same_name(first: str, second: str) -> bool:
    """Whether two names are the same voice: ignoring case and outer spaces."""
    return first.strip().casefold() == second.strip().casefold()


# --- Voices, parts and speakers ---------------------------------------------------


@dataclass(frozen=True)
class Voice:
    """An enrolled voice: a name and a unit-length embedding."""

    name: str
    embedding: tuple[float, ...]


@dataclass(frozen=True)
class TimedText:
    """One Whisper segment: its words and its span, in seconds of the recording.

    Defined here so attribution does not import `murmly.stt`; `attribute` and
    `speaker_text` accept any object with these three attributes.
    """

    start_s: float
    end_s: float
    text: str


@dataclass(frozen=True)
class Speaker:
    """Who a part was attributed to. `kind` is owner, voice or unknown."""

    kind: str
    label: str


@dataclass(frozen=True)
class AttributedPart:
    speaker: Speaker
    text: str


@dataclass
class SpeakerState:
    """The unknown voices heard so far in one capture session, in memory only.

    A toggle or stop-mode recording is one session; a continuous session keeps
    one state from segment to segment so a number holds. Start a new one (or
    call `reset`) when capture starts. Each unknown voice is the sum of its
    members' unit embeddings; the centroid is that sum normalised, which is the
    normalised running mean.
    """

    _sums: list = field(default_factory=list)

    def reset(self) -> None:
        self._sums.clear()

    @property
    def unknown_count(self) -> int:
        return len(self._sums)

    def assign(self, embedding, threshold: float) -> int:
        """The number (from 1) of the unknown voice this embedding belongs to."""
        best, best_similarity = None, -2.0
        for index, total in enumerate(self._sums):
            similarity = float(l2_normalised(total) @ embedding)
            if similarity > best_similarity:
                best, best_similarity = index, similarity
        if best is not None and best_similarity >= threshold:
            self._sums[best] = self._sums[best] + embedding
            return best + 1
        self._sums.append(embedding.copy())
        return len(self._sums)


# --- Attribution ---------------------------------------------------------------------


def find_voice(voices: Sequence[Voice], name: str) -> Voice | None:
    """The enrolled voice with this name, compared without regard to case."""
    if not name.strip():
        return None
    for voice in voices:
        if same_name(voice.name, name):
            return voice
    return None


def _cleaned(text: str) -> str:
    """The words with any line break folded into a single space.

    Only line breaks are touched, so text without one is returned as stripped
    Whisper wrote it and mine-only stays identical to the mode that is off.
    """
    return re.sub(r"\s*[\r\n\v\f\x85  ]+\s*", " ", text.strip())


def attribute(
    parts: Sequence[TimedText],
    audio_for: Callable[[TimedText], object],
    *,
    voices: Sequence[Voice],
    owner: str,
    embedder: Embedder,
    state: SpeakerState,
    threshold_percent: int,
    min_part_seconds: float = MIN_PART_SECONDS,
) -> list[AttributedPart] | None:
    """Attribute each part to a speaker, or None when no part can be identified.

    `audio_for(part)` returns that part's 16 kHz mono float32 audio, and is
    called only for parts long enough to embed. An enrolled voice wins when its
    cosine similarity is at least `threshold_percent / 100`; the owner is the
    enrolled voice named `owner`. Otherwise the part joins the nearest unknown
    voice in `state` at or above the same threshold, or starts a new one. A part
    shorter than `min_part_seconds` takes the speaker of the nearest part that
    could be identified, by position, the one before it on a tie. Parts with no
    words are left out of the result.

    None is not an empty list and must not be tested with `not`: it means every
    part was too short, and the caller delivers the transcript as with the mode
    off.
    """
    words = [(part, _cleaned(part.text)) for part in parts]
    words = [(part, text) for part, text in words if text]
    threshold = threshold_percent / 100.0
    owner_voice = find_voice(voices, owner)
    enrolled = [(voice, l2_normalised(voice.embedding)) for voice in voices]

    speakers: list[Speaker | None] = []
    for part, _text in words:
        if round(part.end_s - part.start_s, 2) < min_part_seconds:
            speakers.append(None)
            continue
        embedding = l2_normalised(embedder.embed(audio_for(part)))
        speakers.append(_who(embedding, enrolled, owner_voice, state, threshold))

    identified = [index for index, speaker in enumerate(speakers) if speaker is not None]
    if not identified:
        return None
    for index, speaker in enumerate(speakers):
        if speaker is None:
            before = max((i for i in identified if i < index), default=None)
            after = min((i for i in identified if i > index), default=None)
            if before is not None and (after is None or index - before <= after - index):
                nearest = before
            else:
                nearest = after
            speakers[index] = speakers[nearest]
    return [AttributedPart(speaker, text) for speaker, (_part, text) in zip(speakers, words)]


def _who(embedding, enrolled, owner_voice, state, threshold) -> Speaker:
    best, best_similarity = None, -2.0
    for voice, unit in enrolled:
        similarity = float(unit @ embedding)
        if similarity > best_similarity:
            best, best_similarity = voice, similarity
    if best is not None and best_similarity >= threshold:
        if best is owner_voice:
            return Speaker("owner", OWNER_LABEL)
        return Speaker("voice", best.name)
    number = state.assign(embedding, threshold)
    return Speaker("unknown", f"{UNKNOWN_LABEL_PREFIX} {number}")


def format_mine_only(attributed: Sequence[AttributedPart]) -> str:
    """The owner's parts in order, one space apart, with no labels."""
    return " ".join(part.text for part in attributed if part.speaker.kind == "owner")


def format_label_everyone(attributed: Sequence[AttributedPart]) -> str:
    """`Label: words` per run of one speaker, runs one space apart, on one line.

    Consecutive parts from one speaker share a label, and the result always
    begins with one. Two different voices with the same label cannot occur:
    names that read as labels are refused at enrolment.
    """
    runs: list[tuple[Speaker, list[str]]] = []
    for part in attributed:
        if runs and runs[-1][0] == part.speaker:
            runs[-1][1].append(part.text)
        else:
            runs.append((part.speaker, [part.text]))
    return " ".join(f"{speaker.label}: {' '.join(words)}" for speaker, words in runs)


def speaker_text(
    parts: Sequence[TimedText],
    audio_for: Callable[[TimedText], object],
    *,
    mode: str,
    voices: Sequence[Voice],
    owner: str,
    embedder: Embedder,
    state: SpeakerState,
    threshold_percent: int,
    min_part_seconds: float = MIN_PART_SECONDS,
) -> str | None:
    """The transcript mine-only or label-everyone delivers for these parts.

    Returns "" when mine-only drops every part, which the daemon treats as a
    transcription that yielded no text. Returns None when the transcript is to
    be delivered as with the mode off: no part could be identified, the mode is
    `off`, or mine-only has no enrolled owner. None and "" mean opposite things.
    """
    if mode not in {"mine-only", "label-everyone"}:
        return None
    if mode == "mine-only" and find_voice(voices, owner) is None:
        return None
    attributed = attribute(
        parts,
        audio_for,
        voices=voices,
        owner=owner,
        embedder=embedder,
        state=state,
        threshold_percent=threshold_percent,
        min_part_seconds=min_part_seconds,
    )
    if attributed is None:
        return None
    if mode == "mine-only":
        kept = format_mine_only(attributed)
        # Counts only: a log line never carries a name next to transcript text.
        logger.info(
            "mine-only kept %d of %d parts",
            sum(part.speaker.kind == "owner" for part in attributed),
            len(attributed),
        )
        return kept
    return format_label_everyone(attributed)
