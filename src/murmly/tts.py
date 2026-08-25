"""Local speech synthesis.

Produces audio one sentence at a time rather than one passage at a time. The
model produces a whole utterance in one pass, so its cost tracks the length of
what it is given and nothing is audible until all of it exists; the only lever
on how soon speech starts is how small the first unit of work is. See
`openspec/changes/add-speech-output/design.md`.
"""

from __future__ import annotations

from collections.abc import Iterator
import ctypes.util
import logging
import os
from pathlib import Path
import re
import subprocess
import threading

from murmly.config import MurmlyConfig
from murmly.stt import (
    CTRANSLATE2_CUDA_LIBRARIES,
    cuda_device_count_available,
    load_cuda_libraries,
)


logger = logging.getLogger(__name__)

MODEL_FILE_NAME = "kokoro-v1.0.onnx"
VOICES_FILE_NAME = "voices-v1.0.bin"
SYNTHESIS_PACKAGE = "kokoro-onnx"
SYNTHESIS_LANGUAGE = "en-us"
ESPEAK_LIBRARY_NAME = "espeak-ng"
ESPEAK_COMMAND = "espeak-ng"
PROBE_TIMEOUT_SECONDS = 10.0

# The ONNX CUDA provider links four libraries CTranslate2 does not, and reports
# a missing one as a warning before running on the CPU. Measured, not assumed:
# see design.md under Risks.
ONNX_CUDA_LIBRARIES = CTRANSLATE2_CUDA_LIBRARIES + (
    ("nvidia-cuda-runtime-cu12", "nvidia/cuda_runtime/lib/libcudart.so.12"),
    ("nvidia-cufft-cu12", "nvidia/cufft/lib/libcufft.so.11"),
    ("nvidia-curand-cu12", "nvidia/curand/lib/libcurand.so.10"),
    ("nvidia-nvjitlink-cu12", "nvidia/nvjitlink/lib/libnvJitLink.so.12"),
)
CUDA_PROVIDER = "CUDAExecutionProvider"
CPU_PROVIDER = "CPUExecutionProvider"

# A swap rather than an addition: onnxruntime-gpu installs into the same package
# namespace as the CPU build faster-whisper and kokoro-onnx depend on, and an
# environment holding both leaves the survivor of any later uninstall broken.
GPU_RUNTIME_REMEDY = (
    "Speech output on CUDA needs the GPU build of ONNX Runtime, which replaces "
    "the CPU one rather than joining it. Run `uv pip uninstall onnxruntime` "
    "then `uv pip install --reinstall \"onnxruntime-gpu==1.24.4\"`. The "
    "`--reinstall` matters: the uninstall deletes files both distributions own, "
    "and a plain install sees its own metadata intact and restores nothing."
)
# States the rule rather than a literal command, because the literal command is
# destructive half the time: `uv sync` makes the environment match exactly the
# extras it is given, so `--extra cuda` alone removes kokoro-onnx and the very
# feature this message is about. A remedy that undoes the thing it is fixing is
# worse than no remedy, because it is followed.
CUDA_EXTRA_REMEDY = (
    "synthesis on CUDA needs the Murmly CUDA extra. Run `uv sync --extra cuda "
    "--extra tts`, naming every extra you already have on the same line, then "
    "reapply the ONNX Runtime swap -- the sync restores the CPU build."
)
RUNTIME_UNUSABLE_REMEDY = (
    "the ONNX Runtime is missing or unusable. Reinstall it with `uv sync "
    "--reinstall --extra tts`, naming every extra you already have on the same "
    "line, and reapply the GPU runtime swap if you want GPU synthesis. A plain "
    "sync does not repair a half-installed runtime: the metadata still says it "
    "is there, so there is nothing for the sync to do."
)

# Anything below this counts as silence when the pause between sentences is
# measured, and a run shorter than the floor is not a pause at all.
SILENCE_FLOOR = 0.005
MIN_SILENT_RUN_SECONDS = 0.04

# Three sentences of ordinary length. Two one-word sentences do not make the
# model produce the pause it produces in real text -- measured: the run between
# "One." and "Two." falls under the floor above and reads as no pause at all.
CALIBRATION_TEXT = "This is the first sentence. Here is the second one. And this ends it."

# Two alternatives so a terminator wrapped in a closing quote or bracket stays
# with the sentence it ends, rather than being eaten as part of the separator.
_SENTENCE_BOUNDARY = re.compile(r"""(?<=[.!?]["')\]])\s+|(?<=[.!?])\s+""")


def split_sentences(text: str) -> list[str]:
    """Split text into units no larger than a sentence.

    Nothing is dropped: text with no terminator at all comes back as one unit,
    because a sender that streams a clause at a time still has to be spoken.
    """
    return [part.strip() for part in _SENTENCE_BOUNDARY.split(text.strip()) if part.strip()]


def resolve_espeak() -> tuple[str, str]:
    """The phoneme library and its data directory, or why they cannot be found.

    Neither path is written down here. The wheel that bundles espeak-ng compiles
    its data directory in as the machine that built it and ignores every attempt
    to override it, so the system install is the one that works -- but naming
    `/usr/lib64` would only move the problem to the next distribution.
    """
    soname = ctypes.util.find_library(ESPEAK_LIBRARY_NAME)
    if soname is None:
        raise RuntimeError(
            "espeak-ng is required for speech output and was not found. Install "
            "your distribution's espeak-ng package."
        )
    try:
        handle = ctypes.CDLL(soname)
    except OSError as error:
        raise RuntimeError(f"espeak-ng ({soname}) could not be loaded: {error}") from error

    library_path = _loaded_library_path(soname) or soname
    data_path = _espeak_data_path()
    if data_path is None:
        raise RuntimeError(
            f"espeak-ng was found at {library_path} but its data directory could "
            f"not be determined. Check that '{ESPEAK_COMMAND} --version' runs."
        )
    del handle
    return library_path, data_path


def _loaded_library_path(soname: str) -> str | None:
    """Where the loader actually found a library it has already opened."""
    stem = soname.split(".so")[0]
    try:
        maps = Path("/proc/self/maps").read_text()
    except OSError:
        return None
    for line in maps.splitlines():
        path = line.rsplit(" ", 1)[-1]
        if path.startswith("/") and os.path.basename(path).startswith(stem):
            return path
    return None


def _espeak_data_path() -> str | None:
    """Ask espeak-ng where its data is rather than assuming an install prefix."""
    try:
        result = subprocess.run(
            [ESPEAK_COMMAND, "--version"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"Data at:\s*(\S+)", result.stdout)
    if match is None or not Path(match.group(1)).is_dir():
        return None
    return match.group(1)


def resolve_providers(config: MurmlyConfig) -> list[str]:
    """Which ONNX execution providers synthesis may use, most preferred first.

    Resolved here rather than through `FasterWhisperTranscriber.resolve_runtime`,
    which validates a compute type against CTranslate2's vocabulary -- values
    such as `int8_float16` that an ONNX runtime does not accept. Only the device
    choice is shared, and the library set is this runtime's own.

    TensorRT is never requested. It heads the runtime's default provider list,
    fails on a missing `libnvinfer`, and only then falls back, which turns every
    session construction into a page of error output.

    The setting read is `[tts] device`, which is synthesis's own and defaults
    to the CPU. `[stt] device` decided it until that default landed, so pinning
    transcription to the GPU also put speech there. It is still read as a
    preference and never as a demand: an explicitly configured `cuda` that
    cannot be used falls back and speaks, because refusing every speech session
    over a missing GPU build would leave someone silent to protect a
    preference. What is never allowed is a silent fallback -- the reason is
    logged and `murmly doctor` reports both the providers in use and the remedy.
    """
    # Before the cpu shortcut, not after it. CPU synthesis needs the runtime
    # every bit as much as CUDA does, so skipping the check for the `cpu`
    # setting reported speech output as available on an environment where the
    # first session would fail -- accept-then-fail, which is what the probe
    # exists to prevent. The `cpu` default makes that the ordinary path rather
    # than a corner of one.
    #
    # Whether this runtime build carries the CUDA provider is a separate
    # question, and the module-level list is what answers it. What that list
    # must never be used for is proof that a session ran on it -- that comes off
    # the session.
    try:
        import onnxruntime

        available = onnxruntime.get_available_providers()
    except Exception as error:  # noqa: BLE001 - reported as unavailable, not raised
        # A bare import here reached the daemon's startup path: absent, it
        # raised ModuleNotFoundError, and half-installed -- the state an
        # interrupted swap between the CPU and GPU builds leaves behind -- it
        # imported and then had no `get_available_providers`. Either killed the
        # daemon outright, taking transcription with it, for a feature that is
        # meant to degrade on its own.
        raise RuntimeError(RUNTIME_UNUSABLE_REMEDY) from error

    if config.tts_device == "cpu":
        return [CPU_PROVIDER]

    if CUDA_PROVIDER not in available:
        # Silent only when there is no GPU to use. The default returns at the
        # shortcut above, so what reaches here asked for a GPU by name or by
        # `auto` -- and `auto` on a machine with no NVIDIA device is an absence
        # rather than a fault, so printing the GPU-swap remedy there would tell
        # someone to replace a working runtime to fix nothing.
        if config.tts_device == "cuda" or (cuda_device_count_available() or 0) > 0:
            logger.warning("Speech output falling back to the CPU: %s", GPU_RUNTIME_REMEDY)
        return [CPU_PROVIDER]

    try:
        cuda_ready = load_cuda_libraries(ONNX_CUDA_LIBRARIES)
    except RuntimeError as error:
        logger.warning("Speech output falling back to the CPU: %s", error)
        return [CPU_PROVIDER]
    if not cuda_ready:
        logger.warning("Speech output falling back to the CPU: %s", CUDA_EXTRA_REMEDY)
        return [CPU_PROVIDER]
    return [CUDA_PROVIDER, CPU_PROVIDER]


class KokoroSynthesizer:
    """Turns text into audio, a sentence at a time.

    Availability is a flag and a reason rather than an exception, following
    `SilenceDetector`: speech output is one capability among several and a
    missing model must not stop the daemon transcribing.
    """

    def __init__(self, config: MurmlyConfig, model=None) -> None:
        self._config = config
        self._model = model
        self._model_lock = threading.Lock()
        self._load_lock = threading.Lock()
        self._unavailable_reason: str | None = None
        self._provider: str | None = None
        self._pause_seconds: dict[tuple[str, float], float] = {}
        self._voice = config.tts_voice
        self._rejected_voice = config.tts_voice_rejected_value
        if self._rejected_voice is not None:
            logger.warning(
                "Speech output does not have a voice named %r; using %s.",
                self._rejected_voice,
                self._voice,
            )
        if model is None:
            self._unavailable_reason = self._probe()
            if self._unavailable_reason is not None:
                logger.warning("Speech output is unavailable: %s", self._unavailable_reason)

    @property
    def available(self) -> bool:
        return self._unavailable_reason is None

    @property
    def unavailable_reason(self) -> str | None:
        return self._unavailable_reason

    @property
    def voice(self) -> str:
        return self._voice

    @property
    def rejected_voice(self) -> str | None:
        return self._rejected_voice

    @property
    def rate_percent(self) -> int:
        return self._config.tts_rate_percent

    @property
    def speed(self) -> float:
        return self._config.tts_rate_percent / 100.0

    @property
    def provider(self) -> str | None:
        """The provider the session reported, once one has been constructed.

        None until then, and never taken from `get_available_providers()`, which
        advertises CUDA on a session that fell back to the CPU.
        """
        return self._provider

    @property
    def model_path(self) -> Path:
        return self._config.tts_model_dir / MODEL_FILE_NAME

    @property
    def voices_path(self) -> Path:
        return self._config.tts_model_dir / VOICES_FILE_NAME

    def synthesize(self, text: str) -> Iterator[tuple[object, int]]:
        """Yield `(samples, sample_rate_hz)` for each sentence in `text`.

        A chunk per sentence rather than one buffer for the passage, so a model
        that produces a whole utterance in one pass and a model that streams
        natively are driven the same way.

        The pause the model puts between sentences when it sees them together is
        reinserted here, less whatever silence the two chunks already carry.
        Concatenating independently produced sentences drops that pause and the
        passage runs together.
        """
        import numpy as np

        sentences = split_sentences(text)
        if not sentences:
            return
        model = self._load_model()
        # Measured at the first boundary rather than before the first sentence.
        # Calibration is a whole extra synthesis, and paying for it up front put
        # it in front of the first audible word of every multi-sentence passage
        # -- which is the delay this whole class exists to avoid. Deferred, it
        # runs while sentence one is already playing, and a sender streaming one
        # sentence at a time never pays for it at all.
        pause: float | None = None
        previous_tail = 0.0
        for index, sentence in enumerate(sentences):
            samples, sample_rate_hz = self._create(model, sentence)
            samples = np.asarray(samples, dtype=np.float32)
            if index:
                if pause is None:
                    pause = self._pause_for(model)
                gap = max(0.0, pause - previous_tail - _leading_silence(samples, sample_rate_hz))
                if gap > 0:
                    samples = np.concatenate(
                        (np.zeros(int(gap * sample_rate_hz), dtype=np.float32), samples)
                    )
            previous_tail = _trailing_silence(samples, sample_rate_hz)
            yield samples, sample_rate_hz

    def _create(self, model, sentence: str) -> tuple[object, int]:
        # Serialized: one ONNX session is not safe to drive from two threads, and
        # the barge-in path can arrive while a sentence is still being produced.
        with self._model_lock:
            return model.create(
                sentence,
                voice=self._voice,
                speed=self.speed,
                lang=SYNTHESIS_LANGUAGE,
            )

    def _pause_for(self, model) -> float:
        """The silence this voice and rate carry between sentences, measured.

        Not a constant: across four voices at three speeds the gap the model
        produces ranges from 0.159 s to 0.435 s, so a figure written into the
        code would be right for one voice and wrong for the rest. One
        calibration passage is produced whole and the silence it leaves between
        its own sentences is measured out of it.
        """
        key = (self._voice, self.speed)
        cached = self._pause_seconds.get(key)
        if cached is not None:
            return cached
        try:
            samples, sample_rate_hz = self._create(model, CALIBRATION_TEXT)
            pause = _median_internal_gap(samples, sample_rate_hz)
        except Exception as error:  # noqa: BLE001 - a pause is cosmetic, speech is not
            logger.warning("Unable to measure the pause between sentences: %s", error)
            pause = 0.0
        self._pause_seconds[key] = pause
        return pause

    def _probe(self) -> str | None:
        """Why speech output cannot run, or None when it can.

        Deliberately does not construct the model. This runs at daemon start and
        the weights are 326 MB; what it checks is that every part is present.
        """
        try:
            import importlib.util

            if importlib.util.find_spec("kokoro_onnx") is None:
                raise ModuleNotFoundError(name="kokoro_onnx")
        except ModuleNotFoundError:
            return (
                f"{SYNTHESIS_PACKAGE} is not installed. Run "
                f"`uv sync --extra tts`, naming every extra you already have on "
                f"the same line -- a sync removes whatever it is not given."
            )
        except ImportError as error:
            return f"{SYNTHESIS_PACKAGE} could not be inspected: {error}"

        missing = [path for path in (self.model_path, self.voices_path) if not path.is_file()]
        if missing:
            names = ", ".join(str(path) for path in missing)
            return (
                f"the synthesis model files are missing: {names}. Download "
                f"{MODEL_FILE_NAME} and {VOICES_FILE_NAME} into "
                f"{self._config.tts_model_dir}."
            )

        try:
            resolve_espeak()
            # Checked here rather than at the first speak: a configuration that
            # names a runtime it cannot have must refuse sessions with a reason,
            # not accept one and fail once someone is listening. Under the `cpu`
            # default that costs distribution metadata and nothing else, because
            # resolution returns above the CUDA preload -- 188 MB of RSS and
            # seven mapped libraries that a daemon which will never reach them
            # paid at every start. `[tts] device = "cuda"` still pays it, having
            # asked for it.
            resolve_providers(self._config)
        except RuntimeError as error:
            return str(error)
        except Exception as error:  # noqa: BLE001 - a broken part refuses speech, not the daemon
            # The backstop for the whole probe. Speech output is required to
            # start and refuse with a reason rather than prevent the daemon
            # starting, so anything unanticipated in here becomes a reason too.
            # `murmly doctor` reports it and it is logged where it is caught.
            return f"speech output could not be checked: {error}"
        return None

    def _load_model(self):
        # Serialized for the same reason `FasterWhisperTranscriber` serializes
        # its own load: two threads that both saw None would allocate two copies.
        with self._load_lock:
            if self._model is None:
                self._model = self._construct_model()
            return self._model

    def _construct_model(self):
        try:
            import onnxruntime

            from kokoro_onnx import EspeakConfig, Kokoro
        except ModuleNotFoundError as error:
            raise RuntimeError(
                f"{SYNTHESIS_PACKAGE} is required for speech output. Run "
                f"`uv sync --extra tts` before enabling it, naming every extra you "
                f"already have on the same line."
            ) from error

        library_path, data_path = resolve_espeak()
        providers = resolve_providers(self._config)
        # Nothing was passed here at all, which left every ONNX Runtime default
        # in force -- the only session in this dependency tree that did, since
        # faster-whisper's own bundled VAD sets its options explicitly. Measured
        # over 16 utterances the CPU arena let the working set grow from 452 MiB
        # to 784 MiB; without it the set holds at 510 MiB, for 4 ms on a short
        # sentence and 50 ms on 8.02 s of audio. The option governs the CPU
        # allocator alone, so it is inert under CUDA -- measured rather than
        # assumed, because this graph runs some operators on the CPU either way.
        options = onnxruntime.SessionOptions()
        options.enable_cpu_mem_arena = False
        # `intra_op_num_threads` and `inter_op_num_threads` stay at the runtime
        # defaults on purpose. Capping intra-op to 4 saves a further 41 MiB, but
        # costs +54% on a short sentence (401 ms against 261 ms) and +36% on
        # 8.02 s of audio (2083 ms against 1537 ms). The short sentence is what
        # a listener waits through before the first word, and 41 MiB does not
        # pay for it.
        #
        # The session is built here rather than left to the package, whose own
        # resolution hands the runtime every provider it can see. TensorRT heads
        # that list, fails on a missing libnvinfer, and prints a page of errors
        # before falling back.
        session = onnxruntime.InferenceSession(
            str(self.model_path), sess_options=options, providers=providers
        )
        model = Kokoro.from_session(
            session,
            str(self.voices_path),
            espeak_config=EspeakConfig(lib_path=library_path, data_path=data_path),
        )
        self._provider = self._session_provider(model, providers)
        return model

    def _session_provider(self, model, requested: list[str]) -> str:
        """What the constructed session is actually running on.

        Read back off the session, never from `get_available_providers()`: the
        module-level list advertises CUDA while the session runs on the CPU, and
        a check against it reports a GPU run that never happened.
        """
        session = getattr(model, "sess", None)
        providers = getattr(session, "get_providers", lambda: requested)()
        active = providers[0] if providers else CPU_PROVIDER
        if CUDA_PROVIDER in requested and active != CUDA_PROVIDER:
            logger.warning(
                "Speech output asked for %s and the session reports %s.",
                CUDA_PROVIDER,
                active,
            )
        return active


def _leading_silence(samples, sample_rate_hz: int) -> float:
    import numpy as np

    loud = np.flatnonzero(np.abs(samples) >= SILENCE_FLOOR)
    return 0.0 if loud.size == 0 else float(loud[0]) / sample_rate_hz


def _trailing_silence(samples, sample_rate_hz: int) -> float:
    import numpy as np

    loud = np.flatnonzero(np.abs(samples) >= SILENCE_FLOOR)
    if loud.size == 0:
        return float(len(samples)) / sample_rate_hz
    return float(len(samples) - 1 - loud[-1]) / sample_rate_hz


def _median_internal_gap(samples, sample_rate_hz: int) -> float:
    """The typical silent run inside a passage, ignoring its own edges.

    The median rather than the mean: a comma inside one of the sentences leaves
    a shorter run, and averaging it in would understate the sentence boundary.
    """
    import numpy as np

    samples = np.asarray(samples, dtype=np.float32)
    quiet = np.abs(samples) < SILENCE_FLOOR
    if quiet.size == 0:
        return 0.0
    edges = np.diff(quiet.astype(np.int8))
    starts = list(np.flatnonzero(edges == 1) + 1)
    ends = list(np.flatnonzero(edges == -1) + 1)
    if quiet[0]:
        starts.insert(0, 0)
    if quiet[-1]:
        ends.append(quiet.size)

    minimum = MIN_SILENT_RUN_SECONDS * sample_rate_hz
    runs = [
        (end - start) / sample_rate_hz
        for start, end in zip(starts, ends)
        # Edge runs are this passage's own lead-in and fade-out, not a pause
        # between two of its sentences.
        if start > 0 and end < quiet.size and (end - start) > minimum
    ]
    if not runs:
        return 0.0
    # The widest runs are the sentence boundaries; a calibration passage of
    # three sentences has two of them.
    widest = sorted(runs, reverse=True)[: max(len(split_sentences(CALIBRATION_TEXT)) - 1, 1)]
    return float(np.median(widest))
