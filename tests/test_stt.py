from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

from murmly.config import MurmlyConfig
from murmly.stt import FasterWhisperTranscriber


class FasterWhisperTranscriberTests(unittest.TestCase):
    def test_transcribe_uses_balanced_decoding_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            transcriber = FasterWhisperTranscriber(config)
            model = Mock()
            model.transcribe.return_value = ([SimpleNamespace(text=" hello ")], object())

            with patch.object(transcriber, "_load_model", return_value=model):
                text = transcriber.transcribe_pcm16(b"\x01\x00" * 16_000)

        self.assertEqual("hello", text)
        model.transcribe.assert_called_once()
        call = model.transcribe.call_args
        self.assertEqual("en", call.kwargs["language"])
        self.assertEqual(5, call.kwargs["beam_size"])
        self.assertTrue(call.kwargs["vad_filter"])

    def test_auto_runtime_prefers_cuda_float16(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            transcriber = FasterWhisperTranscriber(config)

        with (
            patch("ctranslate2.get_cuda_device_count", return_value=1),
            patch.object(transcriber, "_load_cuda_runtime", return_value=True),
        ):
            self.assertEqual(("cuda", "float16"), transcriber._resolve_runtime())

    def test_auto_runtime_falls_back_to_cpu_int8(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            transcriber = FasterWhisperTranscriber(config)

        with patch("ctranslate2.get_cuda_device_count", return_value=0):
            self.assertEqual(("cpu", "int8"), transcriber._resolve_runtime())

    def test_auto_runtime_falls_back_when_cuda_probe_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            transcriber = FasterWhisperTranscriber(config)

        with patch(
            "ctranslate2.get_cuda_device_count",
            side_effect=RuntimeError("incompatible driver"),
        ):
            self.assertEqual(("cpu", "int8"), transcriber._resolve_runtime())

    def test_auto_runtime_falls_back_when_cuda_libraries_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            transcriber = FasterWhisperTranscriber(config)

        with (
            patch("ctranslate2.get_cuda_device_count", return_value=1),
            patch.object(transcriber, "_load_cuda_runtime", return_value=False),
        ):
            self.assertEqual(("cpu", "int8"), transcriber._resolve_runtime())

    def test_explicit_cuda_requires_runtime_extra(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
                device="cuda",
            )
            transcriber = FasterWhisperTranscriber(config)

        with patch.object(transcriber, "_load_cuda_runtime", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "uv sync --extra cuda"):
                transcriber._resolve_runtime()

    def test_cuda_runtime_loads_namespace_package_libraries(self) -> None:
        package_spec = SimpleNamespace(submodule_search_locations=["/cuda/lib"])

        with (
            patch("murmly.stt.find_spec", return_value=package_spec),
            patch("murmly.stt.ctypes.CDLL") as load_library,
        ):
            load_library.side_effect = [OSError(), None, OSError(), None, OSError(), None]
            loaded = FasterWhisperTranscriber._load_cuda_runtime()

        self.assertTrue(loaded)
        self.assertEqual(
            [
                "libcublasLt.so.12",
                "/cuda/lib/libcublasLt.so.12",
                "libcublas.so.12",
                "/cuda/lib/libcublas.so.12",
                "libcudnn.so.9",
                "/cuda/lib/libcudnn.so.9",
            ],
            [call.args[0] for call in load_library.call_args_list],
        )

    def test_cuda_runtime_handles_missing_nvidia_namespace(self) -> None:
        with (
            patch("murmly.stt.find_spec", side_effect=ModuleNotFoundError),
            patch("murmly.stt.ctypes.CDLL", side_effect=OSError),
        ):
            self.assertFalse(FasterWhisperTranscriber._load_cuda_runtime())

    def test_transcribe_skips_digital_silence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            transcriber = FasterWhisperTranscriber(config)

            with patch.object(transcriber, "_load_model") as load_model:
                text = transcriber.transcribe_pcm16(b"\x00\x00" * 16_000)

        self.assertEqual("", text)
        load_model.assert_not_called()

    def test_write_wav_uses_supplied_capture_rate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "clip.wav"
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            transcriber = FasterWhisperTranscriber(config)
            transcriber._write_wav(wav_path, b"\x00\x00" * 480, 48_000)

            with wave.open(str(wav_path), "rb") as wav_handle:
                self.assertEqual(48_000, wav_handle.getframerate())
                self.assertEqual(1, wav_handle.getnchannels())
                self.assertEqual(2, wav_handle.getsampwidth())