from __future__ import annotations

import ctypes
from importlib.util import find_spec
from pathlib import Path
import tempfile
import wave

from murmly.config import MurmlyConfig


class FasterWhisperTranscriber:
    def __init__(self, config: MurmlyConfig) -> None:
        self._config = config
        self._model = None
        if not self._config.lazy_load_model:
            self._load_model()

    def transcribe_pcm16(self, pcm_audio: bytes, sample_rate_hz: int | None = None) -> str:
        if not pcm_audio or not any(pcm_audio):
            return ""
        model = self._load_model()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            wav_path = Path(handle.name)
        try:
            self._write_wav(wav_path, pcm_audio, sample_rate_hz or self._config.sample_rate_hz)
            segments, _info = model.transcribe(
                str(wav_path),
                language="en",
                beam_size=self._config.beam_size,
                vad_filter=self._config.vad_filter,
            )
            return " ".join(segment.text.strip() for segment in segments).strip()
        finally:
            wav_path.unlink(missing_ok=True)

    def _load_model(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ModuleNotFoundError as error:
                raise RuntimeError(
                    "faster-whisper is required for transcription. Install it before starting murmly."
                ) from error
            device, compute_type = self._resolve_runtime()
            self._model = WhisperModel(
                self._config.model_name,
                device=device,
                compute_type=compute_type,
            )
        return self._model

    def _resolve_runtime(self) -> tuple[str, str]:
        device = self._config.device
        if device == "auto":
            try:
                import ctranslate2
            except ModuleNotFoundError:
                device = "cpu"
            else:
                try:
                    cuda_device_count = ctranslate2.get_cuda_device_count()
                except RuntimeError:
                    cuda_device_count = 0
                cuda_available = cuda_device_count > 0 and self._load_cuda_runtime()
                device = "cuda" if cuda_available else "cpu"
        elif device == "cuda" and not self._load_cuda_runtime():
            raise RuntimeError(
                "CUDA requires the Murmly CUDA extra. Run `uv sync --extra cuda`."
            )

        compute_type = self._config.compute_type
        if compute_type == "auto":
            compute_type = "float16" if device == "cuda" else "int8"
        return device, compute_type

    @staticmethod
    def _load_cuda_runtime() -> bool:
        libraries = (
            ("nvidia.cublas.lib", "libcublasLt.so.12"),
            ("nvidia.cublas.lib", "libcublas.so.12"),
            ("nvidia.cudnn.lib", "libcudnn.so.9"),
        )
        for package_name, library_name in libraries:
            try:
                ctypes.CDLL(library_name, mode=ctypes.RTLD_GLOBAL)
                continue
            except OSError:
                pass

            try:
                package_spec = find_spec(package_name)
            except ModuleNotFoundError:
                return False
            if package_spec is None or package_spec.submodule_search_locations is None:
                return False

            for package_path in package_spec.submodule_search_locations:
                try:
                    ctypes.CDLL(
                        str(Path(package_path) / library_name),
                        mode=ctypes.RTLD_GLOBAL,
                    )
                    break
                except OSError:
                    continue
            else:
                return False
        return True

    def _write_wav(self, path: Path, pcm_audio: bytes, sample_rate_hz: int) -> None:
        with wave.open(str(path), "wb") as wav_handle:
            wav_handle.setnchannels(self._config.channels)
            wav_handle.setsampwidth(2)
            wav_handle.setframerate(sample_rate_hz)
            wav_handle.writeframes(pcm_audio)
