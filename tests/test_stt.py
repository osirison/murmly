from __future__ import annotations

import tempfile
import unittest
import wave
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

from murmly.config import MurmlyConfig
from murmly.stt import FasterWhisperTranscriber


class FasterWhisperTranscriberTests(unittest.TestCase):
    def test_model_load_pins_balanced_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            transcriber = FasterWhisperTranscriber(config)

        with (
            patch.object(
                FasterWhisperTranscriber,
                "resolve_runtime",
                return_value=("cuda", "float16"),
            ),
            patch("faster_whisper.WhisperModel") as model_class,
        ):
            transcriber._load_model()

        model_class.assert_called_once_with(
            "large-v3-turbo",
            device="cuda",
            compute_type="float16",
            revision="0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
        )

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
            patch.object(FasterWhisperTranscriber, "_load_cuda_runtime", return_value=True),
        ):
            self.assertEqual(
                ("cuda", "float16"),
                FasterWhisperTranscriber.resolve_runtime(config),
            )

    def test_auto_runtime_falls_back_to_cpu_int8(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            transcriber = FasterWhisperTranscriber(config)

        with patch("ctranslate2.get_cuda_device_count", return_value=0):
            self.assertEqual(
                ("cpu", "int8"),
                FasterWhisperTranscriber.resolve_runtime(config),
            )

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
            self.assertEqual(
                ("cpu", "int8"),
                FasterWhisperTranscriber.resolve_runtime(config),
            )

    def test_auto_runtime_falls_back_when_cuda_libraries_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            transcriber = FasterWhisperTranscriber(config)

        with (
            patch("ctranslate2.get_cuda_device_count", return_value=1),
            patch.object(FasterWhisperTranscriber, "_load_cuda_runtime", return_value=False),
        ):
            self.assertEqual(
                ("cpu", "int8"),
                FasterWhisperTranscriber.resolve_runtime(config),
            )

    def test_explicit_cuda_requires_runtime_extra(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
                device="cuda",
            )
            transcriber = FasterWhisperTranscriber(config)

        with patch.object(FasterWhisperTranscriber, "_load_cuda_runtime", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "uv sync --extra cuda"):
                FasterWhisperTranscriber.resolve_runtime(config)

    def test_cuda_runtime_loads_libraries_from_installed_distributions(self) -> None:
        relative_paths = [
            "nvidia/cublas/lib/libcublasLt.so.12",
            "nvidia/cublas/lib/libcublas.so.12",
            "nvidia/cudnn/lib/libcudnn.so.9",
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            environment_path = Path(temp_dir)
            for relative_path in relative_paths:
                library_path = environment_path / relative_path
                library_path.parent.mkdir(parents=True, exist_ok=True)
                library_path.touch(mode=0o644)

            package = SimpleNamespace(
                files=[Path(path) for path in relative_paths],
                locate_file=lambda path: environment_path / path,
            )
            with (
                patch("murmly.stt.distribution", return_value=package),
                patch("murmly.stt.sys.prefix", temp_dir),
                patch("murmly.stt.ctypes.CDLL") as load_library,
            ):
                loaded = FasterWhisperTranscriber._load_cuda_runtime()

        self.assertTrue(loaded)
        self.assertEqual(
            [str(environment_path / path) for path in relative_paths],
            [call.args[0] for call in load_library.call_args_list],
        )

    def test_cuda_runtime_handles_missing_nvidia_distribution(self) -> None:
        with patch(
            "murmly.stt.distribution",
            side_effect=PackageNotFoundError,
        ):
            self.assertFalse(FasterWhisperTranscriber._load_cuda_runtime())

    def test_cuda_runtime_rejects_library_outside_environment(self) -> None:
        relative_path = Path("nvidia/cublas/lib/libcublasLt.so.12")
        with (
            tempfile.TemporaryDirectory() as environment_dir,
            tempfile.TemporaryDirectory() as external_dir,
        ):
            library_path = Path(external_dir) / relative_path
            library_path.parent.mkdir(parents=True)
            library_path.touch(mode=0o644)
            package = SimpleNamespace(
                files=[relative_path],
                locate_file=lambda path: Path(external_dir) / path,
            )

            with (
                patch("murmly.stt.distribution", return_value=package),
                patch("murmly.stt.sys.prefix", environment_dir),
            ):
                with self.assertRaisesRegex(RuntimeError, "outside the active environment"):
                    FasterWhisperTranscriber._trusted_library_path(
                        "nvidia-cublas-cu12",
                        str(relative_path),
                    )

    def test_cuda_runtime_rejects_writable_library(self) -> None:
        relative_path = Path("nvidia/cublas/lib/libcublasLt.so.12")
        with tempfile.TemporaryDirectory() as environment_dir:
            library_path = Path(environment_dir) / relative_path
            library_path.parent.mkdir(parents=True)
            library_path.touch()
            library_path.chmod(0o666)
            package = SimpleNamespace(
                files=[relative_path],
                locate_file=lambda path: Path(environment_dir) / path,
            )

            with (
                patch("murmly.stt.distribution", return_value=package),
                patch("murmly.stt.sys.prefix", environment_dir),
            ):
                with self.assertRaisesRegex(RuntimeError, "writable CUDA runtime"):
                    FasterWhisperTranscriber._trusted_library_path(
                        "nvidia-cublas-cu12",
                        str(relative_path),
                    )

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