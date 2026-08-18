from __future__ import annotations

import shutil
import tempfile
import threading
import time
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

    def test_partial_transcription_returns_text_while_capture_runs(self) -> None:
        transcriber, model = self._transcriber_with_model(" partial text ")

        with patch.object(transcriber, "_load_model", return_value=model):
            transcriber.begin_capture()
            text = transcriber.transcribe_partial(b"\x01\x00" * 16_000)

        self.assertEqual("partial text", text)

    def test_partial_result_is_dropped_once_capture_stops(self) -> None:
        transcriber, model = self._transcriber_with_model(" too late ")

        def transcribe(*_args, **_kwargs):
            transcriber.stop_partials()
            return ([SimpleNamespace(text=" too late ")], object())

        model.transcribe.side_effect = transcribe

        with patch.object(transcriber, "_load_model", return_value=model):
            transcriber.begin_capture()
            text = transcriber.transcribe_partial(b"\x01\x00" * 16_000)

        self.assertIsNone(text)

    def test_no_partial_pass_starts_after_capture_stops(self) -> None:
        transcriber, model = self._transcriber_with_model(" never ")

        with patch.object(transcriber, "_load_model", return_value=model):
            transcriber.begin_capture()
            transcriber.stop_partials()
            text = transcriber.transcribe_partial(b"\x01\x00" * 16_000)

        self.assertIsNone(text)
        model.transcribe.assert_not_called()

    def test_partial_failure_does_not_propagate_and_disables_partials(self) -> None:
        transcriber, model = self._transcriber_with_model(" unused ")
        model.transcribe.side_effect = RuntimeError("decode exploded")

        with patch.object(transcriber, "_load_model", return_value=model):
            transcriber.begin_capture()
            self.assertTrue(transcriber.partials_available)
            text = transcriber.transcribe_partial(b"\x01\x00" * 16_000)

        self.assertIsNone(text)
        self.assertFalse(transcriber.partials_available)

        with patch.object(transcriber, "_load_model", return_value=model):
            self.assertIsNone(transcriber.transcribe_partial(b"\x01\x00" * 16_000))
        self.assertEqual(1, model.transcribe.call_count)

    def test_a_failed_partial_does_not_stop_the_final_transcription(self) -> None:
        transcriber, model = self._transcriber_with_model(" unused ")
        model.transcribe.side_effect = RuntimeError("decode exploded")

        with patch.object(transcriber, "_load_model", return_value=model):
            transcriber.begin_capture()
            transcriber.transcribe_partial(b"\x01\x00" * 16_000)
            model.transcribe.side_effect = None
            model.transcribe.return_value = ([SimpleNamespace(text=" delivered ")], object())
            text = transcriber.transcribe_pcm16(b"\x01\x00" * 16_000)

        self.assertEqual("delivered", text)

    def test_partial_and_final_passes_never_run_concurrently(self) -> None:
        transcriber, model = self._transcriber_with_model(" text ")
        concurrent = []
        active = []
        barrier = threading.Event()

        def transcribe(*_args, **_kwargs):
            active.append(1)
            concurrent.append(len(active))
            barrier.wait(timeout=1)
            active.pop()
            return ([SimpleNamespace(text=" text ")], object())

        model.transcribe.side_effect = transcribe

        with patch.object(transcriber, "_load_model", return_value=model):
            transcriber.begin_capture()
            partial = threading.Thread(
                target=transcriber.transcribe_partial,
                args=(b"\x01\x00" * 16_000,),
            )
            partial.start()
            final = threading.Thread(
                target=transcriber.transcribe_pcm16,
                args=(b"\x01\x00" * 16_000,),
            )
            final.start()
            barrier.set()
            partial.join(timeout=3)
            final.join(timeout=3)

        self.assertEqual(2, len(concurrent))
        self.assertEqual([1, 1], concurrent)

    def test_begin_capture_re_enables_partials_for_the_next_session(self) -> None:
        transcriber, model = self._transcriber_with_model(" text ")
        model.transcribe.side_effect = RuntimeError("decode exploded")

        with patch.object(transcriber, "_load_model", return_value=model):
            transcriber.begin_capture()
            transcriber.transcribe_partial(b"\x01\x00" * 16_000)
            self.assertFalse(transcriber.partials_available)

            transcriber.begin_capture()
            self.assertTrue(transcriber.partials_available)

    def test_partial_transcription_skips_digital_silence(self) -> None:
        transcriber, model = self._transcriber_with_model(" text ")

        with patch.object(transcriber, "_load_model", return_value=model) as load_model:
            transcriber.begin_capture()
            self.assertIsNone(transcriber.transcribe_partial(b"\x00\x00" * 16_000))

        load_model.assert_not_called()

    def _transcriber_with_model(self, text: str):
        temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp_dir, ignore_errors=True)
        config = MurmlyConfig(
            socket_path=Path(temp_dir) / "murmly.sock",
            config_path=Path(temp_dir) / "config.toml",
        )
        transcriber = FasterWhisperTranscriber(config)
        model = Mock()
        model.transcribe.return_value = ([SimpleNamespace(text=text)], object())
        return transcriber, model

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

class TranscriberConcurrencyTests(unittest.TestCase):
    def _transcriber(self) -> FasterWhisperTranscriber:
        temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp_dir, ignore_errors=True)
        config = MurmlyConfig(
            socket_path=Path(temp_dir) / "murmly.sock",
            config_path=Path(temp_dir) / "config.toml",
        )
        return FasterWhisperTranscriber(config)

    def test_only_one_model_is_ever_constructed(self) -> None:
        """Two threads that both see `_model is None` must not each build one.

        Each model is ~1.6 GB, so a second concurrent construction can exhaust
        VRAM and abort the transcription the user is actually waiting on.
        """
        transcriber = self._transcriber()
        constructed = []
        start = threading.Barrier(4)

        def slow_model(*_args, **_kwargs):
            constructed.append(1)
            time.sleep(0.2)
            return Mock()

        with (
            patch.object(FasterWhisperTranscriber, "resolve_runtime", return_value=("cpu", "int8")),
            patch("faster_whisper.WhisperModel", side_effect=slow_model),
        ):
            def load() -> None:
                start.wait(timeout=5)
                transcriber._load_model()

            threads = [threading.Thread(target=load) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertEqual(1, len(constructed))

    def test_a_partial_from_a_finished_recording_is_never_published(self) -> None:
        """A pass can outlive the recording that started it.

        `stop_partials` is only advisory once a pass is inside the engine, so the
        result must be matched to its own recording rather than to a flag the
        next `begin_capture` clears.
        """
        transcriber = self._transcriber()
        model = Mock()
        released = threading.Event()

        def slow_transcribe(*_args, **_kwargs):
            released.wait(timeout=5)
            return ([SimpleNamespace(text=" speech from recording A ")], object())

        model.transcribe.side_effect = slow_transcribe
        results: list[str | None] = []

        with patch.object(transcriber, "_load_model", return_value=model):
            transcriber.begin_capture()

            def stale_pass() -> None:
                results.append(transcriber.transcribe_partial(b"\x01\x00" * 16_000))

            worker = threading.Thread(target=stale_pass)
            worker.start()
            time.sleep(0.1)

            # Recording A ends and recording B begins while the pass is in flight.
            transcriber.stop_partials()
            transcriber.begin_capture()
            released.set()
            worker.join(timeout=5)

        self.assertEqual([None], results)
