from __future__ import annotations

import ctypes
from importlib.metadata import PackageNotFoundError, distribution
import logging
from pathlib import Path
import sys
import tempfile
import threading
import wave

from murmly.config import MurmlyConfig
from murmly.idle import return_free_heap


logger = logging.getLogger(__name__)
WHISPER_SAMPLE_RATE_HZ = 16_000

# What CTranslate2 needs to run on CUDA. The ONNX synthesis runtime needs these
# and four more, so `tts.py` extends this tuple rather than restating it: the
# provenance checks below are the point, and two copies of them would drift.
CTRANSLATE2_CUDA_LIBRARIES = (
    ("nvidia-cublas-cu12", "nvidia/cublas/lib/libcublasLt.so.12"),
    ("nvidia-cublas-cu12", "nvidia/cublas/lib/libcublas.so.12"),
    ("nvidia-cudnn-cu12", "nvidia/cudnn/lib/libcudnn.so.9"),
)

# What CTranslate2 4.8.1 puts on the `Whisper` object behind `WhisperModel.model`.
# All three are needed to run a release and put the weights back, so a runtime
# that is missing any one of them is treated as one whose model cannot be
# released rather than one to call half a cycle on.
RESIDENCY_ATTRIBUTES = ("model_is_loaded", "load_model", "unload_model")


def cuda_device_count_available() -> int | None:
    """How many CUDA devices this machine has, or None when nothing can say.

    Shared with speech output, which needs the same answer for the same reason:
    on a machine with no NVIDIA device the CPU is the resolution rather than a
    fault, and a remedy printed there sends someone to fix an absence.
    """
    try:
        import ctranslate2
    except ModuleNotFoundError:
        return None
    try:
        return ctranslate2.get_cuda_device_count()
    except RuntimeError as error:
        logger.warning("CUDA device probe failed; falling back to CPU: %s", error)
        return 0


def trusted_library_path(distribution_name: str, relative_path: str) -> Path | None:
    """The path of a CUDA library shipped by a wheel, or None when absent.

    Refuses anything the active environment does not own outright: a symlink, a
    path outside `sys.prefix`, or a file another account can write.
    """
    try:
        package = distribution(distribution_name)
    except PackageNotFoundError:
        return None

    package_file = next(
        (file for file in package.files or () if str(file) == relative_path),
        None,
    )
    if package_file is None:
        return None

    unresolved_path = Path(package.locate_file(package_file))
    if unresolved_path.is_symlink():
        raise RuntimeError(f"Refusing symlinked CUDA runtime library: {unresolved_path}")

    try:
        library_path = unresolved_path.resolve(strict=True)
        library_path.relative_to(Path(sys.prefix).resolve(strict=True))
    except (OSError, ValueError) as error:
        raise RuntimeError(
            f"CUDA runtime library is outside the active environment: {unresolved_path}"
        ) from error

    if not library_path.is_file():
        return None
    if library_path.stat().st_mode & 0o022:
        raise RuntimeError(
            f"Refusing writable CUDA runtime library: {library_path}"
        )
    return library_path


def load_cuda_libraries(libraries: tuple[tuple[str, str], ...]) -> bool:
    """Load every named CUDA library globally, or report that one is missing."""
    for distribution_name, relative_path in libraries:
        library_path = trusted_library_path(distribution_name, relative_path)
        if library_path is None:
            return False
        try:
            ctypes.CDLL(str(library_path), mode=ctypes.RTLD_GLOBAL)
        except OSError as error:
            raise RuntimeError(
                f"Unable to load trusted CUDA runtime library {library_path}: {error}"
            ) from error
    return True


class FasterWhisperTranscriber:
    def __init__(self, config: MurmlyConfig) -> None:
        self._config = config
        self._model = None
        # The division between these two locks is what keeps an idle release from
        # racing a transcription, so it is fixed rather than incidental:
        #
        #   `_load_lock` guards CONSTRUCTION of the `WhisperModel` wrapper, and
        #   nothing else.
        #   `_model_lock` guards RESIDENCY TRANSITIONS -- the reload in
        #   `_ensure_resident_locked` and the unload in `release` -- together with
        #   `_decode`, so an evictor waits for a pass in flight rather than taking
        #   the weights out from under it.
        #   `_load_lock` is never acquired while `_model_lock` is held, which is
        #   why `_transcribe` calls `_load_model` before entering its
        #   `_model_lock` block instead of inside it.
        self._model_lock = threading.Lock()
        self._load_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stopping = threading.Event()
        self._partials_disabled = False
        self._capture_generation = 0
        self._warming_up = False
        self._warm_up_thread: threading.Thread | None = None
        if not self._config.lazy_load_model:
            self._load_model()

    @property
    def partials_available(self) -> bool:
        return not self._partials_disabled

    @property
    def vad_filter(self) -> bool:
        return self._config.vad_filter

    @property
    def resident(self) -> bool:
        """Whether the model's weights are on the device right now.

        Answers without loading and without waiting. Diagnostics ask this of a
        daemon that may never have transcribed, so a model that was never
        constructed reports False rather than being built to answer, and the read
        deliberately skips `_model_lock`: taking it would park the report behind
        whatever decode is currently holding it.
        """
        model = self._model
        if model is None:
            return False
        return self._is_resident(model)

    def begin_capture(self) -> None:
        """Allow partial passes again for a new recording, and warm the model.

        The warm-up is started, not awaited: capture must not be delayed by a
        reload, and the reload it starts is then spent against speech that is
        still being recorded.
        """
        with self._state_lock:
            self._stopping.clear()
            self._partials_disabled = False
            self._capture_generation += 1
        self._start_warm_up()

    def release(self) -> bool:
        """Give the model's accelerator memory back, keeping the wrapper.

        Reports whether anything was released, so an idle timer can log the
        transition without asking a second time. A model that was never
        constructed, one already unloaded, and a runtime that cannot be asked all
        answer False and do nothing.

        `to_cpu=False` is a decision rather than a default. `to_cpu=True` returns
        the same device memory but parks the weights in host RAM -- measured here
        at 1541 MiB of added RSS -- so for a daemon that is idle almost all of the
        time it relocates the footprint instead of reducing it.

        Acquiring `_model_lock` is the whole of "a release never interrupts a
        pass": the release queues behind a decode that holds the lock instead of
        needing its own mechanism to notice one.
        """
        with self._model_lock:
            model = self._model
            if model is None:
                return False
            handle = self._residency_handle(model)
            if handle is None or not handle.model_is_loaded:
                return False
            handle.unload_model(to_cpu=False)
        # Outside the lock, because it walks the heap and a transcription
        # starting behind it would wait for that. What it returns here is not the
        # weights -- those went back to the device above -- but the staging the
        # last reload allocated and freed, measured at about 1416 MiB left sitting
        # in glibc's arenas until something asks for it back.
        return_free_heap()
        logger.debug("Released the transcription model's accelerator memory.")
        return True

    def stop_partials(self) -> None:
        """Refuse to start further partial passes.

        A pass already inside the engine cannot be interrupted, so it runs to
        completion and its result is discarded instead.
        """
        self._stopping.set()

    def transcribe_pcm16(self, pcm_audio: bytes, sample_rate_hz: int | None = None) -> str:
        if not pcm_audio or not any(pcm_audio):
            return ""
        return self._transcribe(pcm_audio, sample_rate_hz)

    def transcribe_partial(self, pcm_audio: bytes, sample_rate_hz: int | None = None) -> str | None:
        """Transcribe captured audio for display only.

        Returns None when the result must not be shown: partials are disabled,
        capture has stopped, or the pass failed. A failure never reaches the
        caller, because a partial is feedback and must not disturb the recording.
        """
        with self._state_lock:
            if self._partials_disabled or self._stopping.is_set():
                return None
            generation = self._capture_generation
        if not pcm_audio or not any(pcm_audio):
            return None
        try:
            text = self._transcribe(pcm_audio, sample_rate_hz, allow_array=True)
        except Exception as error:
            with self._state_lock:
                if generation == self._capture_generation:
                    self._partials_disabled = True
            logger.warning("Live transcription disabled after a failed pass: %s", error)
            return None
        with self._state_lock:
            # Compare the generation, not just the stopping flag: a pass that
            # outlived its recording would otherwise see the flag cleared by the
            # next begin_capture and publish the previous recording's speech.
            if self._stopping.is_set() or generation != self._capture_generation:
                return None
        return text

    def _transcribe(
        self,
        pcm_audio: bytes,
        sample_rate_hz: int | None,
        *,
        allow_array: bool = False,
    ) -> str:
        model = self._load_model()
        rate = sample_rate_hz or self._config.sample_rate_hz
        # Mono 16 kHz only: the array path has no de-interleaving, and handing
        # Whisper interleaved stereo would show nonsense in the panel while the
        # delivered transcript (written as a correct WAV) said something else.
        fast_path = (
            allow_array
            and rate == WHISPER_SAMPLE_RATE_HZ
            and self._config.channels == 1
        )
        audio = self._as_array(pcm_audio) if fast_path else None
        if audio is not None:
            with self._model_lock:
                self._ensure_resident_locked(model)
                return self._decode(model, audio)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            wav_path = Path(handle.name)
        try:
            self._write_wav(wav_path, pcm_audio, rate)
            with self._model_lock:
                # Both decode sites re-check, not just the array one above. A site
                # that skipped it would fail inside CTranslate2 on an evicted
                # model, and only for the audio shape that reaches that site.
                self._ensure_resident_locked(model)
                return self._decode(model, str(wav_path))
        finally:
            wav_path.unlink(missing_ok=True)

    def _decode(self, model, audio) -> str:
        segments, _info = model.transcribe(
            audio,
            language="en",
            beam_size=self._config.beam_size,
            vad_filter=self._config.vad_filter,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()

    @staticmethod
    def _as_array(pcm_audio: bytes):
        """Hand 16 kHz partials straight to the model instead of via a temp WAV.

        Only the partial path uses this. The delivered transcript keeps the file
        route unchanged, so it cannot drift from what a non-live session produces.
        """
        try:
            import numpy as np
        except ModuleNotFoundError:
            return None
        usable = len(pcm_audio) - (len(pcm_audio) % 2)
        if usable == 0:
            return None
        samples = np.frombuffer(pcm_audio[:usable], dtype=np.int16).astype(np.float32)
        return samples / 32_768.0

    def _start_warm_up(self) -> None:
        """Begin putting a released model's weights back, without making anyone wait.

        Capture start is on the critical path, so this returns before the reload
        has done anything: the 0.78 s it costs is then spent against speech that
        is still being recorded rather than against the pause after the user
        stops. Nothing depends on the warm-up winning that race -- `_transcribe`
        re-checks residency under the same lock and reloads there if it has to --
        so this is latency work only, and a failure is logged rather than raised
        into the caller that started capture.

        A model that was never constructed is left alone. The warm-up exists to
        undo a release; constructing the first model here would move a cold 1.99 s
        load onto exactly the path `lazy_load_model` keeps it off.
        """
        if not self._claim_warm_up():
            return
        thread = threading.Thread(
            target=self._warm_up, name="murmly-model-warmup", daemon=True
        )
        try:
            thread.start()
        except RuntimeError as error:
            # Giving the claim back matters more than the warm-up that failed:
            # left set, it would silently disable warming for the whole process.
            self._finish_warm_up()
            logger.warning("Unable to warm the transcription model: %s", error)
            return
        self._warm_up_thread = thread

    def _claim_warm_up(self) -> bool:
        """Whether this caller should start a warm-up, claiming it if so.

        Declines when the weights are already on the device and when a warm-up is
        still running. Someone toggling capture quickly would otherwise stack
        threads that all queue on `_model_lock` to redo what the first one is
        already doing.
        """
        model = self._model
        if model is None or self._is_resident(model):
            return False
        with self._state_lock:
            if self._warming_up:
                return False
            self._warming_up = True
            return True

    def _warm_up(self) -> None:
        model = self._model
        try:
            if model is not None:
                with self._model_lock:
                    self._ensure_resident_locked(model)
        except Exception as error:
            logger.warning("Unable to warm the transcription model: %s", error)
        finally:
            self._finish_warm_up()

    def _finish_warm_up(self) -> None:
        with self._state_lock:
            self._warming_up = False

    def _load_model(self):
        # Serialized: two threads that both saw `self._model is None` would each
        # construct a model, allocating two copies of the weights at once. This
        # is construction only -- whether the wrapper it returns still has its
        # weights is a separate question, answered under `_model_lock`.
        with self._load_lock:
            return self._load_model_locked()

    def _load_model_locked(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ModuleNotFoundError as error:
                raise RuntimeError(
                    "faster-whisper is required for transcription. Install it before starting murmly."
                ) from error
            device, compute_type = self.resolve_runtime(self._config)
            self._model = WhisperModel(
                self._config.model_name,
                device=device,
                compute_type=compute_type,
                revision=self._config.model_revision,
            )
        return self._model

    @staticmethod
    def _residency_handle(model):
        """The CTranslate2 object that owns the weights, or None when unreachable.

        Residency lives on the `Whisper` reached through `WhisperModel.model`, not
        on the wrapper: `unload_model()` leaves the wrapper a perfectly valid
        object and takes only the weights off the device, so `self._model is None`
        answers a different question than "is this model loaded". Each step uses
        `getattr` with a default because a CTranslate2 build without these names,
        or a stand-in that does not reach that far, must degrade to today's
        always-resident behaviour rather than raise inside a transcription.
        """
        handle = getattr(model, "model", None)
        if handle is None:
            return None
        if any(getattr(handle, name, None) is None for name in RESIDENCY_ATTRIBUTES):
            return None
        return handle

    def _is_resident(self, model) -> bool:
        """Whether `model` holds its weights, answering yes when it cannot say."""
        handle = self._residency_handle(model)
        if handle is None:
            return True
        return bool(handle.model_is_loaded)

    def _ensure_resident_locked(self, model) -> None:
        """Put the weights back if an idle release took them off the device.

        Called with `_model_lock` held and immediately before `_decode`. Checking
        earlier -- where `_load_model` hands the wrapper back -- leaves a window in
        which a release lands between the check and the decode, and CTranslate2
        then fails inside the decode rather than reloading for us. Reloading is
        `load_model()` on the wrapper already held, never a fresh `WhisperModel`:
        reconstruction would discard the tokenizer and feature extractor along
        with the weights and make every reload a cold load.
        """
        handle = self._residency_handle(model)
        if handle is None or handle.model_is_loaded:
            return
        handle.load_model()

    @classmethod
    def resolve_runtime(cls, config: MurmlyConfig) -> tuple[str, str]:
        device = config.device
        if device == "auto":
            cuda_device_count = cuda_device_count_available()
            if cuda_device_count is None:
                device = "cpu"
            else:
                cuda_available = cuda_device_count > 0 and cls._load_cuda_runtime()
                if cuda_device_count > 0 and not cuda_available:
                    logger.warning(
                        "CUDA runtime libraries are unavailable; falling back to CPU."
                    )
                device = "cuda" if cuda_available else "cpu"
        elif device == "cuda" and not cls._load_cuda_runtime():
            raise RuntimeError(
                "CUDA requires the Murmly CUDA extra. Run `uv sync --extra cuda`, "
                "which is the whole command -- speech output is a default "
                "dependency group and the sync keeps it."
            )

        compute_type = config.compute_type
        if compute_type == "auto":
            compute_type = "float16" if device == "cuda" else "int8"
        return device, compute_type

    @staticmethod
    def _load_cuda_runtime() -> bool:
        return load_cuda_libraries(CTRANSLATE2_CUDA_LIBRARIES)

    @staticmethod
    def _trusted_library_path(
        distribution_name: str,
        relative_path: str,
    ) -> Path | None:
        return trusted_library_path(distribution_name, relative_path)

    def _write_wav(self, path: Path, pcm_audio: bytes, sample_rate_hz: int) -> None:
        with wave.open(str(path), "wb") as wav_handle:
            wav_handle.setnchannels(self._config.channels)
            wav_handle.setsampwidth(2)
            wav_handle.setframerate(sample_rate_hz)
            wav_handle.writeframes(pcm_audio)
