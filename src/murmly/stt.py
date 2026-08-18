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


logger = logging.getLogger(__name__)
WHISPER_SAMPLE_RATE_HZ = 16_000


class FasterWhisperTranscriber:
    def __init__(self, config: MurmlyConfig) -> None:
        self._config = config
        self._model = None
        self._model_lock = threading.Lock()
        self._load_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stopping = threading.Event()
        self._partials_disabled = False
        self._capture_generation = 0
        if not self._config.lazy_load_model:
            self._load_model()

    @property
    def partials_available(self) -> bool:
        return not self._partials_disabled

    def begin_capture(self) -> None:
        """Allow partial passes again for a new recording."""
        with self._state_lock:
            self._stopping.clear()
            self._partials_disabled = False
            self._capture_generation += 1

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
        audio = self._as_array(pcm_audio) if allow_array and rate == WHISPER_SAMPLE_RATE_HZ else None
        if audio is not None:
            with self._model_lock:
                return self._decode(model, audio)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            wav_path = Path(handle.name)
        try:
            self._write_wav(wav_path, pcm_audio, rate)
            with self._model_lock:
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

    def _load_model(self):
        # Serialized: two threads that both saw `self._model is None` would each
        # construct a model, allocating two copies of the weights at once.
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

    @classmethod
    def resolve_runtime(cls, config: MurmlyConfig) -> tuple[str, str]:
        device = config.device
        if device == "auto":
            try:
                import ctranslate2
            except ModuleNotFoundError:
                device = "cpu"
            else:
                try:
                    cuda_device_count = ctranslate2.get_cuda_device_count()
                except RuntimeError as error:
                    logger.warning("CUDA device probe failed; falling back to CPU: %s", error)
                    cuda_device_count = 0
                cuda_available = cuda_device_count > 0 and cls._load_cuda_runtime()
                if cuda_device_count > 0 and not cuda_available:
                    logger.warning(
                        "CUDA runtime libraries are unavailable; falling back to CPU."
                    )
                device = "cuda" if cuda_available else "cpu"
        elif device == "cuda" and not cls._load_cuda_runtime():
            raise RuntimeError(
                "CUDA requires the Murmly CUDA extra. Run `uv sync --extra cuda`."
            )

        compute_type = config.compute_type
        if compute_type == "auto":
            compute_type = "float16" if device == "cuda" else "int8"
        return device, compute_type

    @staticmethod
    def _load_cuda_runtime() -> bool:
        libraries = (
            ("nvidia-cublas-cu12", "nvidia/cublas/lib/libcublasLt.so.12"),
            ("nvidia-cublas-cu12", "nvidia/cublas/lib/libcublas.so.12"),
            ("nvidia-cudnn-cu12", "nvidia/cudnn/lib/libcudnn.so.9"),
        )
        for distribution_name, relative_path in libraries:
            library_path = FasterWhisperTranscriber._trusted_library_path(
                distribution_name,
                relative_path,
            )
            if library_path is None:
                return False
            try:
                ctypes.CDLL(str(library_path), mode=ctypes.RTLD_GLOBAL)
            except OSError as error:
                raise RuntimeError(
                    f"Unable to load trusted CUDA runtime library {library_path}: {error}"
                ) from error
        return True

    @staticmethod
    def _trusted_library_path(
        distribution_name: str,
        relative_path: str,
    ) -> Path | None:
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

    def _write_wav(self, path: Path, pcm_audio: bytes, sample_rate_hz: int) -> None:
        with wave.open(str(path), "wb") as wav_handle:
            wav_handle.setnchannels(self._config.channels)
            wav_handle.setsampwidth(2)
            wav_handle.setframerate(sample_rate_hz)
            wav_handle.writeframes(pcm_audio)
