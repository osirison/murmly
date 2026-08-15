from __future__ import annotations

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
                beam_size=1,
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
            self._model = WhisperModel(
                self._config.model_name,
                device="cpu",
                compute_type=self._config.compute_type,
            )
        return self._model

    def _write_wav(self, path: Path, pcm_audio: bytes, sample_rate_hz: int) -> None:
        with wave.open(str(path), "wb") as wav_handle:
            wav_handle.setnchannels(self._config.channels)
            wav_handle.setsampwidth(2)
            wav_handle.setframerate(sample_rate_hz)
            wav_handle.writeframes(pcm_audio)
