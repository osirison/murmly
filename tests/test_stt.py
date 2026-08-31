from __future__ import annotations

import ctypes
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


# A gate a test forgot to open fails that test rather than hanging the suite.
GATE_TIMEOUT_SECONDS = 5.0
# Long enough that a thread which is genuinely blocked is still blocked when it
# expires, short enough that asserting so costs nothing.
BLOCKED_SECONDS = 0.05


class FakeCTranslate2Model:
    """The `Whisper` object behind `WhisperModel.model`, with real residency.

    A bare `Mock()` answers every attribute truthily, so `model.model_is_loaded`
    on one reads True whether or not the code under test ever consults it, and a
    missing residency re-check would pass. This tracks the flag for real and
    records the `to_cpu` argument of every unload, which is the only place in the
    suite that can catch `to_cpu=True` -- the mode that frees the device by moving
    1541 MiB into host RSS -- being reintroduced.
    """

    def __init__(
        self,
        *,
        loaded: bool = True,
        events: list[str] | None = None,
        load_gate: threading.Event | None = None,
        load_error: Exception | None = None,
    ) -> None:
        self.model_is_loaded = loaded
        self.loads = 0
        self.unloads: list[bool] = []
        self.events = events if events is not None else []
        self._load_gate = load_gate
        self._load_error = load_error

    def load_model(self) -> None:
        if self._load_gate is not None:
            self._load_gate.wait(GATE_TIMEOUT_SECONDS)
        self.loads += 1
        if self._load_error is not None:
            # One failure only, so the same fake can show that the next request
            # tries again rather than giving up on the model for good.
            error, self._load_error = self._load_error, None
            raise error
        self.model_is_loaded = True
        self.events.append("loaded")

    def unload_model(self, to_cpu: bool = False) -> None:
        self.unloads.append(to_cpu)
        self.model_is_loaded = False
        self.events.append("unloaded")


class FakeWhisperModel:
    """`WhisperModel` as far as murmly uses it: a decode and an inner model.

    The wrapper outlives an unload -- that is the whole reason identity cannot be
    the residency test -- so this stays a usable object while its inner model
    reports the weights gone, and decoding in that state raises the way
    CTranslate2 does rather than quietly returning text.
    """

    def __init__(self, text: str = " the transcript ", **model_arguments) -> None:
        self.events: list[str] = []
        self.model = FakeCTranslate2Model(events=self.events, **model_arguments)
        self.text = text
        self.decoded: list[object] = []
        self.decode_started = threading.Event()
        self.decode_gate: threading.Event | None = None

    def transcribe(self, audio, **_kwargs):
        if not self.model.model_is_loaded:
            raise RuntimeError("Requested a decode from a model whose weights are gone")
        self.decoded.append(audio)
        self.decode_started.set()
        if self.decode_gate is not None:
            self.decode_gate.wait(GATE_TIMEOUT_SECONDS)
        self.events.append("decoded")
        return ([SimpleNamespace(text=self.text)], object())


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
        if not hasattr(ctypes, "RTLD_GLOBAL"):
            # `load_cuda_libraries` calls `ctypes.CDLL(..., mode=ctypes.
            # RTLD_GLOBAL)` unconditionally -- a POSIX dlopen flag Windows'
            # `ctypes` does not define at all, since Windows DLLs have no
            # comparable flat, process-wide symbol namespace to load
            # globally into. `os.add_dll_directory` is task 11.1's own,
            # not-yet-built Windows answer to this same loading problem
            # (still unchecked in tasks.md); this test is the current,
            # POSIX-only implementation specifically, with no Windows branch
            # to redirect it to yet.
            self.skipTest("needs ctypes.RTLD_GLOBAL, which Windows does not define")
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
        # `trusted_library_path` loads `unresolved_path.resolve(strict=True)`,
        # not the literal joined path: on macOS, `tempfile.TemporaryDirectory`
        # lands under `/var/folders/...`, itself a symlink to
        # `/private/var/folders/...`, so the path actually handed to
        # `ctypes.CDLL` is the resolved one. Resolving the expected paths the
        # same way is a no-op on Linux, where no such symlink sits in the way.
        self.assertEqual(
            [str((environment_path / path).resolve()) for path in relative_paths],
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


class TranscriberResidencyTests(unittest.TestCase):
    """The release, reload and warm-up cycle around a model that can vanish.

    These use `FakeWhisperModel` rather than a `Mock`, because a `Mock` reports
    itself loaded whatever the code does and every one of these guarantees would
    then pass without being implemented.
    """

    def _transcriber(self, model) -> FasterWhisperTranscriber:
        """A transcriber already holding `model`, with no construction involved.

        Assigning `_model` is what a warm daemon looks like from here:
        `_load_model` finds a wrapper already there and hands it straight back,
        which is the state an idle release acts on.
        """
        temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp_dir, ignore_errors=True)
        config = MurmlyConfig(
            socket_path=Path(temp_dir) / "murmly.sock",
            config_path=Path(temp_dir) / "config.toml",
        )
        transcriber = FasterWhisperTranscriber(config)
        transcriber._model = model
        return transcriber

    def test_a_delivered_transcript_after_an_eviction_is_unchanged(self) -> None:
        model = FakeWhisperModel()
        transcriber = self._transcriber(model)

        before = transcriber.transcribe_pcm16(b"\x01\x00" * 16_000)
        self.assertTrue(transcriber.release())
        self.assertFalse(transcriber.resident)

        after = transcriber.transcribe_pcm16(b"\x01\x00" * 16_000)

        self.assertEqual("the transcript", before)
        self.assertEqual(before, after)
        self.assertTrue(transcriber.resident)
        # Reloaded in place. A reconstructed wrapper would have counted no load
        # here and paid a cold build instead.
        self.assertEqual(1, model.model.loads)
        # The delivered transcript takes the WAV route, which is the decode site
        # that goes unchecked if only the array path re-checks residency.
        self.assertTrue(all(isinstance(audio, str) for audio in model.decoded))

    def test_a_partial_after_an_eviction_is_unchanged(self) -> None:
        model = FakeWhisperModel()
        transcriber = self._transcriber(model)
        transcriber.begin_capture()

        before = transcriber.transcribe_partial(b"\x01\x00" * 16_000)
        self.assertTrue(transcriber.release())

        after = transcriber.transcribe_partial(b"\x01\x00" * 16_000)

        self.assertEqual("the transcript", before)
        self.assertEqual(before, after)
        self.assertTrue(transcriber.partials_available)
        self.assertEqual(1, model.model.loads)
        # The other decode site: 16 kHz mono partials hand the model an array and
        # never touch a temporary WAV.
        self.assertFalse(any(isinstance(audio, str) for audio in model.decoded))

    def test_eviction_during_a_pass_waits_rather_than_interrupting(self) -> None:
        model = FakeWhisperModel()
        model.decode_gate = threading.Event()
        transcriber = self._transcriber(model)

        decoding = threading.Thread(
            target=transcriber.transcribe_pcm16, args=(b"\x01\x00" * 16_000,)
        )
        decoding.start()
        self.assertTrue(model.decode_started.wait(GATE_TIMEOUT_SECONDS))

        released: list[bool] = []
        evictor = threading.Thread(target=lambda: released.append(transcriber.release()))
        evictor.start()
        evictor.join(BLOCKED_SECONDS)

        # The evictor is parked on `_model_lock`, which is the whole mechanism:
        # the weights are still under the decode that is using them.
        self.assertTrue(evictor.is_alive())
        self.assertTrue(model.model.model_is_loaded)

        model.decode_gate.set()
        decoding.join(GATE_TIMEOUT_SECONDS)
        evictor.join(GATE_TIMEOUT_SECONDS)

        self.assertEqual(["decoded", "unloaded"], model.events)
        self.assertEqual([True], released)

    def test_a_release_frees_the_device_rather_than_moving_the_weights_to_the_host(self) -> None:
        """`to_cpu` must stay False.

        `to_cpu=True` returns the same accelerator memory but parks the weights in
        host RAM, measured at 1541 MiB of added RSS, which for a daemon that is
        idle almost all of the time relocates the footprint instead of reducing
        it. Nothing else in the suite would notice that argument changing.
        """
        model = FakeWhisperModel()
        transcriber = self._transcriber(model)

        self.assertTrue(transcriber.release())

        self.assertEqual([False], model.model.unloads)
        self.assertFalse(model.model.model_is_loaded)

    def test_releasing_a_model_that_is_absent_or_already_released_does_nothing(self) -> None:
        transcriber = self._transcriber(None)
        self.assertFalse(transcriber.release())

        model = FakeWhisperModel()
        transcriber._model = model
        self.assertTrue(transcriber.release())
        self.assertFalse(transcriber.release())

        self.assertEqual([False], model.model.unloads)

    def test_a_release_hands_the_freed_heap_back_to_the_system(self) -> None:
        """Freeing is not returning, and the requirement is that it reach the system.

        Dropping the weights frees the reload's staging into glibc's arenas,
        where it stays mapped and stays counted against this process. Measured
        over eight cycles, the released floor sat at 1812 MiB without this step
        and at 396 MiB with it.

        Asserted outside the model lock as well as at all, because a trim walks
        the arenas and a transcription starting behind one would wait for it.
        """
        model = FakeWhisperModel()
        transcriber = self._transcriber(model)
        held: list[bool] = []

        with patch(
            "murmly.stt.return_free_heap",
            side_effect=lambda: held.append(transcriber._model_lock.locked()),
        ) as trim:
            self.assertTrue(transcriber.release())

        trim.assert_called_once_with()
        self.assertEqual([False], held, "the heap was trimmed while the model lock was held")

    def test_a_release_that_frees_nothing_does_not_trim(self) -> None:
        """An idle timer firing against an already released model walks no arenas."""
        transcriber = self._transcriber(None)

        with patch("murmly.stt.return_free_heap") as trim:
            self.assertFalse(transcriber.release())

        trim.assert_not_called()

    def test_reporting_residency_loads_nothing(self) -> None:
        transcriber = self._transcriber(None)

        with patch.object(transcriber, "_load_model") as load_model:
            self.assertFalse(transcriber.resident)
        load_model.assert_not_called()

        model = FakeWhisperModel(loaded=False)
        transcriber._model = model
        self.assertFalse(transcriber.resident)
        self.assertEqual(0, model.model.loads)

        model.model.model_is_loaded = True
        self.assertTrue(transcriber.resident)

    def test_a_failed_reload_is_retried_rather_than_remembered(self) -> None:
        model = FakeWhisperModel(loaded=False, load_error=RuntimeError("out of memory"))
        transcriber = self._transcriber(model)

        with self.assertRaisesRegex(RuntimeError, "out of memory"):
            transcriber.transcribe_pcm16(b"\x01\x00" * 16_000)

        self.assertEqual("the transcript", transcriber.transcribe_pcm16(b"\x01\x00" * 16_000))
        self.assertEqual(2, model.model.loads)

    def test_warming_the_model_does_not_block_begin_capture(self) -> None:
        gate = threading.Event()
        model = FakeWhisperModel(loaded=False, load_gate=gate)
        transcriber = self._transcriber(model)

        transcriber.begin_capture()

        # begin_capture returned while the reload is still parked on the gate,
        # which is the guarantee: capture starts without waiting for weights.
        self.assertFalse(transcriber.resident)
        self.assertEqual(0, model.model.loads)

        gate.set()
        transcriber._warm_up_thread.join(GATE_TIMEOUT_SECONDS)

        self.assertTrue(transcriber.resident)
        self.assertEqual(1, model.model.loads)

    def test_repeated_capture_starts_do_not_stack_warm_up_threads(self) -> None:
        gate = threading.Event()
        model = FakeWhisperModel(loaded=False, load_gate=gate)
        transcriber = self._transcriber(model)

        transcriber.begin_capture()
        warming = transcriber._warm_up_thread
        transcriber.begin_capture()
        transcriber.begin_capture()

        self.assertIs(warming, transcriber._warm_up_thread)

        gate.set()
        warming.join(GATE_TIMEOUT_SECONDS)

        self.assertEqual(1, model.model.loads)

    def test_capture_does_not_warm_a_model_that_is_already_resident(self) -> None:
        model = FakeWhisperModel()
        transcriber = self._transcriber(model)

        transcriber.begin_capture()

        self.assertIsNone(transcriber._warm_up_thread)
        self.assertEqual(0, model.model.loads)

    def test_capture_does_not_construct_a_model_that_was_never_loaded(self) -> None:
        """The warm-up undoes a release; it is not a second eager-load switch.

        Building the first model here would put the cold 1.99 s load on exactly
        the path `lazy_load_model` keeps it off.
        """
        transcriber = self._transcriber(None)

        with patch.object(transcriber, "_load_model") as load_model:
            transcriber.begin_capture()

        load_model.assert_not_called()
        self.assertIsNone(transcriber._warm_up_thread)

    def test_a_failed_warm_up_does_not_reach_the_caller_that_started_capture(self) -> None:
        model = FakeWhisperModel(loaded=False, load_error=RuntimeError("out of memory"))
        transcriber = self._transcriber(model)

        with self.assertLogs("murmly.stt", level="WARNING"):
            transcriber.begin_capture()
            transcriber._warm_up_thread.join(GATE_TIMEOUT_SECONDS)

        self.assertFalse(transcriber.resident)
        # The pass that follows loads the model itself, so a failed warm-up costs
        # latency rather than the transcript.
        self.assertEqual("the transcript", transcriber.transcribe_pcm16(b"\x01\x00" * 16_000))

    def test_a_runtime_without_the_unload_methods_stays_resident(self) -> None:
        """A CTranslate2 build that cannot be asked behaves as murmly does today.

        Degrading to always-resident costs the memory a release would have
        returned. Raising out of the residency check would cost the transcript,
        which is the worse of the two by a long way.
        """
        older_runtime = SimpleNamespace(
            model=SimpleNamespace(),
            transcribe=lambda audio, **_kwargs: (
                [SimpleNamespace(text=" an older runtime ")],
                object(),
            ),
        )
        transcriber = self._transcriber(older_runtime)

        self.assertTrue(transcriber.resident)
        self.assertFalse(transcriber.release())
        self.assertEqual(
            "an older runtime", transcriber.transcribe_pcm16(b"\x01\x00" * 16_000)
        )
