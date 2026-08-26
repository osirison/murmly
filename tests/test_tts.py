from __future__ import annotations

import ast
import re
import sys
import tempfile
import textwrap
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import numpy as np

import murmly.tts
from fakes import GATE_TIMEOUT_SECONDS, FakeKokoroModel, FakeSession
from module_stubs import injected_module
from murmly.config import (
    DEFAULT_TTS_DEVICE,
    DEFAULT_TTS_RATE_PERCENT,
    DEFAULT_TTS_VOICE,
    MurmlyConfig,
    load_config,
)
from murmly.tts import (
    CALIBRATION_TEXT,
    CPU_PROVIDER,
    CUDA_EXTRA_REMEDY,
    CUDA_PROVIDER,
    MODEL_FILE_NAME,
    VOICES_FILE_NAME,
    KokoroSynthesizer,
    resolve_espeak,
    resolve_providers,
    split_sentences,
)


PASSAGE = (
    "The daemon starts when you log in. It waits until you press the key. "
    "Then it listens. Nothing is written to disk."
)


def make_config(temp_dir: str, **overrides: object) -> MurmlyConfig:
    return MurmlyConfig(
        socket_path=Path(temp_dir) / "murmly.sock",
        config_path=Path(temp_dir) / "config.toml",
        tts_model_dir=Path(temp_dir) / "models",
        tts_enabled=True,
        **overrides,
    )


class SentenceSplitTests(unittest.TestCase):
    def test_splits_on_terminators_and_keeps_every_unit(self) -> None:
        self.assertEqual(
            ["One thing.", "Then another!", "And a question?"],
            split_sentences("One thing. Then another! And a question?"),
        )

    def test_text_with_no_terminator_is_one_unit(self) -> None:
        self.assertEqual(["still speaking"], split_sentences("  still speaking  "))

    def test_a_closing_quote_stays_with_its_sentence(self) -> None:
        self.assertEqual(
            ['He said "no."', "Then he left."],
            split_sentences('He said "no." Then he left.'),
        )

    def test_empty_text_produces_no_units(self) -> None:
        self.assertEqual([], split_sentences("   "))


class SynthesisTests(unittest.TestCase):
    def _synthesizer(self, model: FakeKokoroModel, **overrides: object):
        temp_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: None)
        return KokoroSynthesizer(make_config(temp_dir, **overrides), model=model)

    def test_one_chunk_per_sentence_rather_than_one_buffer(self) -> None:
        model = FakeKokoroModel()
        synthesizer = self._synthesizer(model)

        chunks = list(synthesizer.synthesize(PASSAGE))

        self.assertEqual(4, len(chunks))
        for _samples, rate in chunks:
            self.assertEqual(model.sample_rate_hz, rate)

    def test_chunked_total_matches_the_whole_passage(self) -> None:
        """The pause between sentences survives being produced one at a time.

        Producing sentences independently drops the silence the model puts
        between them, and the passage runs together. This is the assertion that
        the silence is put back.
        """
        model = FakeKokoroModel()
        synthesizer = self._synthesizer(model)

        chunked = sum(len(samples) for samples, _rate in synthesizer.synthesize(PASSAGE))
        whole, rate = model.create(PASSAGE, voice=DEFAULT_TTS_VOICE, speed=1.0)

        tolerance = int(0.01 * rate)
        self.assertLessEqual(
            abs(chunked - len(whole)),
            tolerance,
            f"chunked {chunked / rate:.3f}s against whole {len(whole) / rate:.3f}s",
        )

    def test_naive_concatenation_would_fail_that_comparison(self) -> None:
        """Pins the defect the previous test guards, so it cannot pass vacuously."""
        model = FakeKokoroModel()
        naive = sum(
            len(model.create(sentence, voice=DEFAULT_TTS_VOICE, speed=1.0)[0])
            for sentence in split_sentences(PASSAGE)
        )
        whole, rate = model.create(PASSAGE, voice=DEFAULT_TTS_VOICE, speed=1.0)

        self.assertGreater(abs(naive - len(whole)), int(0.01 * rate))

    def test_the_pause_holds_when_sentences_begin_on_silence(self) -> None:
        """The leading term of the derivation, which nothing else exercises.

        `FakeKokoroModel.lead` defaults to zero and no other test sets it, so
        `_leading_silence` evaluated to zero in every case and the whole
        subtraction could be deleted with the suite still green. A voice whose
        chunks begin on silence over-pads every boundary by that much without
        it, which is the doubling the derivation exists to prevent.
        """
        model = FakeKokoroModel(lead=0.08)
        synthesizer = self._synthesizer(model)

        chunked = sum(len(samples) for samples, _rate in synthesizer.synthesize(PASSAGE))
        whole, rate = model.create(PASSAGE, voice=DEFAULT_TTS_VOICE, speed=1.0)

        tolerance = int(0.01 * rate)
        self.assertLessEqual(
            abs(chunked - len(whole)),
            tolerance,
            f"chunked {chunked / rate:.3f}s against whole {len(whole) / rate:.3f}s",
        )

    def test_the_pause_is_measured_rather_than_assumed(self) -> None:
        """Two models that pause differently produce differently long output."""
        short_pause = self._synthesizer(FakeKokoroModel(boundary_pause=0.20))
        long_pause = self._synthesizer(FakeKokoroModel(boundary_pause=0.60))

        short_total = sum(len(samples) for samples, _rate in short_pause.synthesize(PASSAGE))
        long_total = sum(len(samples) for samples, _rate in long_pause.synthesize(PASSAGE))

        self.assertGreater(long_total, short_total)

    def test_a_single_sentence_is_not_charged_for_calibration(self) -> None:
        model = FakeKokoroModel()
        synthesizer = self._synthesizer(model)

        list(synthesizer.synthesize("Just the one sentence."))

        self.assertEqual(["Just the one sentence."], [text for text, _voice, _speed in model.calls])

    def test_calibration_runs_once_and_is_reused(self) -> None:
        model = FakeKokoroModel()
        synthesizer = self._synthesizer(model)

        list(synthesizer.synthesize(PASSAGE))
        list(synthesizer.synthesize(PASSAGE))

        calibrations = [text for text, _voice, _speed in model.calls if text == CALIBRATION_TEXT]
        self.assertEqual(1, len(calibrations))

    def test_the_configured_voice_and_rate_reach_the_model(self) -> None:
        model = FakeKokoroModel()
        synthesizer = self._synthesizer(model, tts_voice="bf_emma", tts_rate_percent=120)

        list(synthesizer.synthesize("One sentence."))

        self.assertEqual(("bf_emma", 1.2), model.calls[0][1:])

    def test_empty_text_produces_nothing_and_loads_no_model(self) -> None:
        model = FakeKokoroModel()
        synthesizer = self._synthesizer(model)

        self.assertEqual([], list(synthesizer.synthesize("   ")))
        self.assertEqual([], model.calls)

    def test_a_pause_that_cannot_be_measured_does_not_stop_speech(self) -> None:
        """A calibration failure costs the pause, not the utterance."""
        model = FakeKokoroModel()
        synthesizer = self._synthesizer(model)

        with patch("murmly.tts._median_internal_gap", side_effect=RuntimeError("no runs")):
            chunks = list(synthesizer.synthesize(PASSAGE))

        self.assertEqual(4, len(chunks))
        self.assertTrue(all(len(samples) for samples, _rate in chunks))


class VoiceAndRateFallbackTests(unittest.TestCase):
    def _config_from(self, body: str) -> MurmlyConfig:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(textwrap.dedent(body))
            return load_config(config_path)

    def test_unrecognized_voice_falls_back_and_reports_what_was_asked_for(self) -> None:
        config = self._config_from(
            """
            [tts]
            enabled = true
            voice = "morgan_freeman"
            """
        )

        self.assertEqual(DEFAULT_TTS_VOICE, config.tts_voice)
        self.assertEqual("morgan_freeman", config.tts_voice_rejected_value)

        synthesizer = KokoroSynthesizer(config, model=FakeKokoroModel())
        self.assertEqual(DEFAULT_TTS_VOICE, synthesizer.voice)
        self.assertEqual("morgan_freeman", synthesizer.rejected_voice)

    def test_a_recognized_voice_is_kept_and_nothing_is_reported_as_rejected(self) -> None:
        config = self._config_from(
            """
            [tts]
            voice = "bf_emma"
            """
        )

        self.assertEqual("bf_emma", config.tts_voice)
        self.assertIsNone(config.tts_voice_rejected_value)

    def test_rate_outside_the_supported_range_falls_back(self) -> None:
        for rate in (10, 500, "quickly"):
            with self.subTest(rate=rate):
                config = self._config_from(
                    f"""
                    [tts]
                    rate = {rate!r}
                    """
                )
                self.assertEqual(DEFAULT_TTS_RATE_PERCENT, config.tts_rate_percent)

    def test_a_rate_inside_the_range_is_kept(self) -> None:
        config = self._config_from(
            """
            [tts]
            rate = 120
            """
        )

        self.assertEqual(120, config.tts_rate_percent)


class AvailabilityTests(unittest.TestCase):
    def test_missing_model_files_are_reported_rather_than_raised(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("importlib.util.find_spec", return_value=object()):
                synthesizer = KokoroSynthesizer(make_config(temp_dir))

        self.assertFalse(synthesizer.available)
        self.assertIn("model files are missing", synthesizer.unavailable_reason)
        self.assertIn("kokoro-v1.0.onnx", synthesizer.unavailable_reason)

    def test_an_absent_synthesis_package_names_the_remedy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("importlib.util.find_spec", return_value=None):
                synthesizer = KokoroSynthesizer(make_config(temp_dir))

        self.assertFalse(synthesizer.available)
        self.assertIn("kokoro-onnx", synthesizer.unavailable_reason)
        self.assertIn("--extra tts", synthesizer.unavailable_reason)

    def test_an_absent_phoneme_library_is_reported_rather_than_raised(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            model_dir = Path(temp_dir) / "models"
            model_dir.mkdir()
            (model_dir / "kokoro-v1.0.onnx").write_bytes(b"model")
            (model_dir / "voices-v1.0.bin").write_bytes(b"voices")
            with (
                patch("importlib.util.find_spec", return_value=object()),
                patch("ctypes.util.find_library", return_value=None),
            ):
                synthesizer = KokoroSynthesizer(make_config(temp_dir))

        self.assertFalse(synthesizer.available)
        self.assertIn("espeak-ng", synthesizer.unavailable_reason)

    def test_resolving_the_phoneme_library_names_what_to_install(self) -> None:
        with patch("ctypes.util.find_library", return_value=None):
            with self.assertRaises(RuntimeError) as raised:
                resolve_espeak()

        self.assertIn("espeak-ng", str(raised.exception))

    def test_an_undeterminable_data_path_is_a_failure_not_silent_audio(self) -> None:
        """A wrong data path makes the library print and return no audio.

        Reported here instead, because a caller that only watches for exceptions
        would see an empty result and call it success.
        """
        with (
            patch("ctypes.util.find_library", return_value="libespeak-ng.so.1"),
            patch("ctypes.CDLL", return_value=object()),
            patch("murmly.tts._espeak_data_path", return_value=None),
        ):
            with self.assertRaises(RuntimeError) as raised:
                resolve_espeak()

        self.assertIn("data directory", str(raised.exception))

    def test_a_synthesizer_given_a_model_is_available_without_probing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            synthesizer = KokoroSynthesizer(make_config(temp_dir), model=FakeKokoroModel())

        self.assertTrue(synthesizer.available)
        self.assertIsNone(synthesizer.unavailable_reason)


class RuntimeResolutionTests(unittest.TestCase):
    def _config(self, device: str) -> MurmlyConfig:
        """A config asking for `device` for synthesis, leaving transcription's alone."""
        with tempfile.TemporaryDirectory() as temp_dir:
            return make_config(temp_dir, tts_device=device)

    @staticmethod
    def _gpu_build():
        """Stand in for a runtime build that carries the CUDA provider.

        The suite has to pass on either build, so which one is installed must
        not decide what these assert.
        """
        return patch(
            "onnxruntime.get_available_providers",
            return_value=["TensorrtExecutionProvider", CUDA_PROVIDER, CPU_PROVIDER],
        )

    def test_cpu_is_honoured_without_probing_cuda(self) -> None:
        """Stood in for as a GPU build, so the shortcut is what is proven.

        On a CPU-only runtime the resolution returns before the preload anyway,
        and the assertion below would hold without the shortcut existing.
        Transcription is pinned to the GPU here so the two settings are seen to
        be answered separately rather than from one value.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            config = make_config(temp_dir, device="cuda", tts_device="cpu")
        with self._gpu_build(), patch("murmly.tts.load_cuda_libraries") as loader:
            self.assertEqual([CPU_PROVIDER], resolve_providers(config))

        self.assertEqual("cuda", config.device)
        loader.assert_not_called()

    def test_the_default_returns_before_the_cuda_libraries_are_loaded(self) -> None:
        """What removes 190.1 MiB from every daemon start with speech enabled.

        `load_cuda_libraries` dlopens the CUDA runtime, and the cost is paid at
        start-up because the probe resolves providers eagerly. An unconfigured
        `[tts] device` means the CPU, and the CPU returns at the shortcut, so
        the preload is not reached even on a build that carries the provider.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            config = make_config(temp_dir)
        with self._gpu_build(), patch("murmly.tts.load_cuda_libraries") as loader:
            self.assertEqual([CPU_PROVIDER], resolve_providers(config))

        self.assertEqual(DEFAULT_TTS_DEVICE, config.tts_device)
        loader.assert_not_called()

    def test_cuda_is_preferred_when_its_libraries_are_present(self) -> None:
        with self._gpu_build(), patch("murmly.tts.load_cuda_libraries", return_value=True):
            providers = resolve_providers(self._config("auto"))

        self.assertEqual([CUDA_PROVIDER, CPU_PROVIDER], providers)

    def test_the_accelerator_is_used_when_it_is_asked_for_by_name(self) -> None:
        """The setting exists so a machine that needs the GPU can still have it."""
        with self._gpu_build(), patch("murmly.tts.load_cuda_libraries", return_value=True):
            providers = resolve_providers(self._config("cuda"))

        self.assertEqual([CUDA_PROVIDER, CPU_PROVIDER], providers)

    def test_tensorrt_is_never_requested(self) -> None:
        """It heads the runtime's own list and fails on a missing libnvinfer."""
        with self._gpu_build(), patch("murmly.tts.load_cuda_libraries", return_value=True):
            providers = resolve_providers(self._config("auto"))

        self.assertNotIn("TensorrtExecutionProvider", providers)

    def test_auto_falls_back_to_the_cpu_when_the_libraries_are_absent(self) -> None:
        with self._gpu_build(), patch("murmly.tts.load_cuda_libraries", return_value=False):
            self.assertEqual([CPU_PROVIDER], resolve_providers(self._config("auto")))

    def test_a_machine_with_no_gpu_is_not_told_to_replace_its_runtime(self) -> None:
        """`auto` is what someone sets to get resolve-and-fall-back.

        The GPU-swap remedy tells the reader to uninstall the CPU runtime, so
        printing it on a machine with no NVIDIA device sends someone to break a
        working install to fix an absence.
        """
        with (
            patch("onnxruntime.get_available_providers", return_value=[CPU_PROVIDER]),
            patch("murmly.tts.cuda_device_count_available", return_value=0),
        ):
            with self.assertNoLogs("murmly.tts", level="WARNING"):
                self.assertEqual([CPU_PROVIDER], resolve_providers(self._config("auto")))

    def test_a_machine_with_a_gpu_is_still_told_about_the_swap(self) -> None:
        """The remedy is right when there is a device it would put to use."""
        with (
            patch("onnxruntime.get_available_providers", return_value=[CPU_PROVIDER]),
            patch("murmly.tts.cuda_device_count_available", return_value=1),
            self.assertLogs("murmly.tts", level="WARNING") as logged,
        ):
            self.assertEqual([CPU_PROVIDER], resolve_providers(self._config("auto")))

        self.assertIn("onnxruntime-gpu", "\n".join(logged.output))

    def test_cpu_still_proves_the_runtime_exists(self) -> None:
        """CPU synthesis needs onnxruntime every bit as much as CUDA does.

        Returning early for `device = "cpu"` skipped the check entirely, so the
        probe reported speech output available on an environment where the first
        session would fail -- the accept-then-fail the probe exists to prevent.
        """
        with injected_module("onnxruntime", None):
            with self.assertRaises(RuntimeError) as raised:
                resolve_providers(self._config("cpu"))

        self.assertIn("uv sync", str(raised.exception))

    def test_an_unanticipated_probe_failure_refuses_speech_rather_than_the_daemon(self) -> None:
        """The backstop, not the two known arms.

        The spec requires speech output to start and refuse with a reason rather
        than stop the daemon starting, so anything unexpected inside the probe
        has to become a reason too -- including a failure nobody predicted.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            config = make_config(temp_dir)
            (config.tts_model_dir).mkdir(parents=True, exist_ok=True)
            for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
                (config.tts_model_dir / name).write_bytes(b"")
            with (
                patch("importlib.util.find_spec", return_value=object()),
                patch("murmly.tts.resolve_espeak", side_effect=ValueError("something odd")),
            ):
                synthesizer = KokoroSynthesizer(config)

        self.assertFalse(synthesizer.available)
        self.assertIn("something odd", synthesizer.unavailable_reason)

    def test_an_absent_runtime_refuses_speech_rather_than_the_daemon(self) -> None:
        """Speech output degrades on its own; it does not take the daemon down.

        A bare import here ran on the daemon's startup path, so an environment
        without onnxruntime raised ModuleNotFoundError out of the probe and
        killed transcription too, for a feature that is meant to switch itself
        off with a reason.
        """
        with injected_module("onnxruntime", None):
            with self.assertRaises(RuntimeError) as raised:
                resolve_providers(self._config("auto"))

        self.assertIn("uv sync", str(raised.exception))

    def test_a_half_installed_runtime_refuses_speech_rather_than_the_daemon(self) -> None:
        """What an interrupted swap between the CPU and GPU builds leaves.

        The module imports and has no `get_available_providers`, so the failure
        is an AttributeError rather than an ImportError -- which is why catching
        the import error alone was not enough.
        """
        broken = ModuleType("onnxruntime")
        with injected_module("onnxruntime", broken):
            with self.assertRaises(RuntimeError) as raised:
                resolve_providers(self._config("auto"))

        self.assertIn("uv sync", str(raised.exception))

    def test_no_remedy_names_a_command_that_removes_speech_output(self) -> None:
        """`uv sync` is exact, so a bare single-extra command is destructive.

        These strings are printed to the person as the thing to do next. One
        that names `--extra cuda` alone tells someone whose speech output is
        broken to run the command that removes it.
        """
        # Parsed rather than grepped: adjacent string literals are joined by the
        # parser, and a line-wrapped remedy read as a bare command on the source
        # line where it happens to break.
        tree = ast.parse(Path(murmly.tts.__file__).read_text())
        literals: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.append(node.value)
            elif isinstance(node, ast.JoinedStr):
                literals.append(
                    "".join(
                        part.value
                        for part in node.values
                        if isinstance(part, ast.Constant) and isinstance(part.value, str)
                    )
                )

        for literal in literals:
            for command in re.findall(r"`(uv sync [^`]*)`", literal):
                self.assertNotEqual(
                    "uv sync --extra cuda",
                    " ".join(command.split()),
                    f"a remedy names a command that uninstalls speech output: {literal!r}",
                )
        self.assertIn("--extra cuda --extra tts", " ".join(CUDA_EXTRA_REMEDY.split()))

    def test_the_cuda_extra_missing_falls_back_and_logs_the_remedy(self) -> None:
        """An accelerator asked for and unusable falls back rather than refusing.

        `[tts] device` names synthesis, so `cuda` here is a deliberate request
        -- but a request is not a demand, and a missing synthesis library costs
        the person speed rather than speech. Silence about it is what is not
        allowed, so the remedy is logged.
        """
        with (
            patch("murmly.tts.load_cuda_libraries", return_value=False),
            patch("onnxruntime.get_available_providers", return_value=[CUDA_PROVIDER]),
            self.assertLogs("murmly.tts", level="WARNING") as logged,
        ):
            self.assertEqual([CPU_PROVIDER], resolve_providers(self._config("cuda")))

        self.assertIn("--extra cuda", "\n".join(logged.output))

    def test_a_cpu_only_runtime_build_falls_back_and_names_the_swap_it_needs(self) -> None:
        """The CPU and GPU builds share one package namespace.

        An environment holding both leaves the survivor of any later uninstall
        broken, so the remedy has to say replace, not add. It is a remedy, not a
        refusal: the documented CUDA install carries the CPU runtime on purpose.
        """
        with (
            patch("onnxruntime.get_available_providers", return_value=[CPU_PROVIDER]),
            self.assertLogs("murmly.tts", level="WARNING") as logged,
        ):
            self.assertEqual([CPU_PROVIDER], resolve_providers(self._config("cuda")))

        message = "\n".join(logged.output)
        self.assertIn("onnxruntime-gpu==1.24.4", message)
        self.assertIn("uninstall onnxruntime", message)

    def test_pinning_transcription_to_the_gpu_leaves_synthesis_on_the_cpu(self) -> None:
        """Resolution read `[stt] device` until `[tts] device` existed.

        So pinning Whisper to the GPU put speech there too, and the 1208 MiB the
        CUDA provider takes for synthesis and never returns came with it. With
        the accelerator present and usable, an unconfigured synthesis device
        still resolves to the CPU, and transcription keeps what it was given.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            config = make_config(temp_dir, device="cuda")
        with (
            self._gpu_build(),
            patch("murmly.tts.load_cuda_libraries", return_value=True) as loader,
        ):
            providers = resolve_providers(config)

        self.assertEqual([CPU_PROVIDER], providers)
        self.assertEqual("cuda", config.device)
        loader.assert_not_called()

    def test_a_cpu_only_runtime_build_falls_back_rather_than_raising_on_auto(self) -> None:
        with patch("onnxruntime.get_available_providers", return_value=[CPU_PROVIDER]):
            self.assertEqual([CPU_PROVIDER], resolve_providers(self._config("auto")))

    def test_a_library_that_will_not_load_falls_back_rather_than_raising(self) -> None:
        with self._gpu_build(), patch(
            "murmly.tts.load_cuda_libraries",
            side_effect=RuntimeError("Refusing writable CUDA runtime library"),
        ):
            self.assertEqual([CPU_PROVIDER], resolve_providers(self._config("auto")))

    def test_the_provider_reported_is_the_one_the_session_ran_on(self) -> None:
        """`get_available_providers()` advertises CUDA on a CPU session.

        Reading it instead of the session's own list reports a GPU run that
        never happened, which is the fault recorded in the agent note.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            model = FakeKokoroModel(providers=(CPU_PROVIDER,))
            synthesizer = KokoroSynthesizer(make_config(temp_dir), model=model)

            provider = synthesizer._session_provider(model, [CUDA_PROVIDER, CPU_PROVIDER])

        self.assertEqual(CPU_PROVIDER, provider)


class StubKokoro:
    """What `Kokoro.from_session` gives back, carrying the session it was handed.

    `_session_provider` reads the provider off `sess`, so a stand-in that built
    a session of its own would report something the constructor never saw.
    """

    def __init__(self, session) -> None:
        self.sess = session

    @classmethod
    def from_session(cls, session, voices_path, espeak_config=None):
        return cls(session)


def kokoro_onnx_stub() -> ModuleType:
    """Stand in for the optional package, so no weights are read at all."""
    module = ModuleType("kokoro_onnx")
    module.EspeakConfig = lambda lib_path=None, data_path=None: (lib_path, data_path)
    module.Kokoro = StubKokoro
    return module


class SessionConstructionTests(unittest.TestCase):
    """The options the ONNX session is built with, once resolution has chosen."""

    def _construct(self, providers: tuple[str, ...], **overrides: object):
        """Run the load path with the runtime and the package stood in for.

        Neither may be real here: a session reads 326 MB of weights and dlopens
        whatever its provider needs, which is what every fake in this suite
        exists to avoid. `resolve_providers` is left real, so what the session
        is asked for is what resolution chose rather than what a test wrote.
        """
        session = FakeSession(providers)
        with tempfile.TemporaryDirectory() as temp_dir:
            config = make_config(temp_dir, **overrides)
            config.tts_model_dir.mkdir(parents=True, exist_ok=True)
            for name in (MODEL_FILE_NAME, VOICES_FILE_NAME):
                (config.tts_model_dir / name).write_bytes(b"")
            with (
                injected_module("kokoro_onnx", kokoro_onnx_stub()),
                patch("importlib.util.find_spec", return_value=object()),
                patch("murmly.tts.resolve_espeak", return_value=("libespeak-ng.so.1", "/data")),
                patch("onnxruntime.InferenceSession", return_value=session) as constructor,
            ):
                synthesizer = KokoroSynthesizer(config)
                synthesizer._load_model()

        return synthesizer, constructor

    def test_the_session_is_built_with_the_cpu_arena_disabled(self) -> None:
        """No options were passed at all, so every runtime default was in force.

        Measured over 16 utterances, the arena let the working set grow from
        452 MiB to 784 MiB; without it the set holds at 510 MiB, for 4 ms on a
        short sentence and 50 ms on 8.02 s of audio.
        """
        _, constructor = self._construct((CPU_PROVIDER,))

        options = constructor.call_args.kwargs["sess_options"]
        self.assertFalse(options.enable_cpu_mem_arena)

    def test_the_thread_counts_are_left_at_the_runtime_defaults(self) -> None:
        """Capping intra-op to 4 saves 41 MiB and is refused rather than missed.

        It costs +54% on a short sentence (401 ms against 261 ms) and +36% on
        8.02 s of audio (2083 ms against 1537 ms), and the short sentence is
        what a listener waits through before the first word.
        """
        _, constructor = self._construct((CPU_PROVIDER,))

        options = constructor.call_args.kwargs["sess_options"]
        # Zero is the runtime's own "decide for me", not a thread cap of none.
        self.assertEqual(0, options.intra_op_num_threads)
        self.assertEqual(0, options.inter_op_num_threads)

    def test_the_session_is_built_for_the_provider_resolution_chose(self) -> None:
        """The default reaches the session as the CPU and is read back as the CPU.

        The provider the synthesizer reports comes off the session rather than
        from `get_available_providers()`, which advertises CUDA on a session
        that fell back to the CPU.
        """
        synthesizer, constructor = self._construct((CPU_PROVIDER,))

        self.assertEqual([CPU_PROVIDER], constructor.call_args.kwargs["providers"])
        self.assertEqual(CPU_PROVIDER, synthesizer.provider)

    def test_an_accelerator_asked_for_reaches_the_session_as_one(self) -> None:
        """The same guarantee from the other end: `cuda` is not quietly dropped."""
        with (
            patch(
                "onnxruntime.get_available_providers",
                return_value=[CUDA_PROVIDER, CPU_PROVIDER],
            ),
            patch("murmly.tts.load_cuda_libraries", return_value=True),
        ):
            synthesizer, constructor = self._construct(
                (CUDA_PROVIDER, CPU_PROVIDER), tts_device="cuda"
            )

        self.assertEqual([CUDA_PROVIDER, CPU_PROVIDER], constructor.call_args.kwargs["providers"])
        self.assertEqual(CUDA_PROVIDER, synthesizer.provider)


class GatedKokoroModel(FakeKokoroModel):
    """A model that parks inside `create` until a test lets it out.

    A pass already inside the engine cannot be interrupted, and what happens
    while one is in flight is the whole of what there is to test about a
    release. The gate puts a pass in that state exactly, rather than racing a
    sleep against one and passing for the wrong reason.
    """

    def __init__(self, **overrides: object) -> None:
        super().__init__(**overrides)
        self.entered = threading.Event()
        self.gate = threading.Event()

    def create(self, text, voice="af_heart", speed=1.0, lang="en-us"):
        self.entered.set()
        # One-shot: once a test opens the gate every later sentence runs
        # straight through, so the pass the gate held finishes on its own.
        self.gate.wait(GATE_TIMEOUT_SECONDS)
        return super().create(text, voice=voice, speed=speed, lang=lang)


class SynthesisResidencyTests(unittest.TestCase):
    """Releasing the session, and building another one when speech resumes."""

    def _synthesizer(self, model: FakeKokoroModel | None = None) -> KokoroSynthesizer:
        """A synthesizer holding `model`, constructed without probing.

        Handing the model in is how the rest of this suite avoids reading 326 MB
        of weights, and it leaves `_construct_model` free to stand in for the
        whole ONNX path. What a rebuild has to do is `SessionConstructionTests`'
        subject; what these tests are about is when one happens.
        """
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return KokoroSynthesizer(make_config(temp.name), model=model or FakeKokoroModel())

    @contextmanager
    def _load_path(self, **overrides: object):
        """A synthesizer whose loads run the real `_construct_model`.

        Same stand-ins as `SessionConstructionTests._construct` -- neither the
        runtime nor the package may be real -- but the patches stay open for the
        block, so a release and the load after it can both be watched.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            config = make_config(temp_dir, **overrides)
            config.tts_model_dir.mkdir(parents=True, exist_ok=True)
            for name in (MODEL_FILE_NAME, VOICES_FILE_NAME):
                (config.tts_model_dir / name).write_bytes(b"")
            with (
                injected_module("kokoro_onnx", kokoro_onnx_stub()),
                patch("importlib.util.find_spec", return_value=object()),
                patch("murmly.tts.resolve_espeak", return_value=("libespeak-ng.so.1", "/data")),
                patch(
                    "onnxruntime.InferenceSession",
                    side_effect=lambda *args, **kwargs: FakeSession((CPU_PROVIDER,)),
                ) as constructor,
            ):
                yield KokoroSynthesizer(config), constructor

    def test_a_released_synthesizer_reports_itself_as_not_resident(self) -> None:
        synthesizer = self._synthesizer()

        self.assertTrue(synthesizer.resident)
        self.assertTrue(synthesizer.release())

        self.assertFalse(synthesizer.resident)

    def test_asking_whether_a_session_is_resident_does_not_construct_one(self) -> None:
        """`murmly doctor` asks, and the answer must not cost half a second."""
        synthesizer = self._synthesizer()
        synthesizer.release()

        with patch.object(synthesizer, "_construct_model") as construct:
            self.assertFalse(synthesizer.resident)

        construct.assert_not_called()

    def test_releasing_when_nothing_is_resident_constructs_nothing(self) -> None:
        """An idle timer firing twice must not build a session to drop it.

        The second call reports False, which is how the timer knows there was no
        transition to log rather than having to ask about residency again.
        """
        synthesizer = self._synthesizer()

        with patch.object(synthesizer, "_construct_model") as construct:
            self.assertTrue(synthesizer.release())
            self.assertFalse(synthesizer.release())

        construct.assert_not_called()
        self.assertFalse(synthesizer.resident)

    def test_a_release_hands_the_freed_heap_back_to_the_system(self) -> None:
        """Under the shipped `[tts] device = "cpu"` this is the whole release.

        The session's memory is host memory there, so dropping it returns it to
        glibc rather than to the system, and the requirement is that a release
        reach the system so another process can allocate what it gave up.
        Measured over five cycles: the released floor sat between 465 and
        669 MiB without this step and at 83 MiB with it.

        Asserted outside both locks, because a trim walks the arenas and a
        speech session resuming behind one would wait for it.
        """
        synthesizer = self._synthesizer()
        held: list[bool] = []

        with patch(
            "murmly.tts.return_free_heap",
            side_effect=lambda: held.append(
                synthesizer._model_lock.locked() or synthesizer._load_lock.locked()
            ),
        ) as trim:
            self.assertTrue(synthesizer.release())

        trim.assert_called_once_with()
        self.assertEqual([False], held, "the heap was trimmed while a lock was held")

    def test_a_release_that_frees_nothing_does_not_trim(self) -> None:
        """An idle timer firing against an already released session walks no arenas."""
        synthesizer = self._synthesizer()
        self.assertTrue(synthesizer.release())

        with patch("murmly.tts.return_free_heap") as trim:
            self.assertFalse(synthesizer.release())

        trim.assert_not_called()

    def test_synthesis_after_a_release_produces_the_audio_it_would_have(self) -> None:
        """The spec's only detectable difference is time, not what is spoken."""
        first = FakeKokoroModel()
        second = FakeKokoroModel()
        synthesizer = self._synthesizer(first)
        before = [samples.tobytes() for samples, _rate in synthesizer.synthesize(PASSAGE)]
        calls_before = len(first.calls)

        synthesizer.release()
        with patch.object(synthesizer, "_construct_model", return_value=second):
            after = [samples.tobytes() for samples, _rate in synthesizer.synthesize(PASSAGE)]

        self.assertEqual(before, after)
        self.assertTrue(synthesizer.resident)
        # The dropped session produced none of the second passage, so the audio
        # above came from the rebuilt one rather than from a survivor.
        self.assertEqual(calls_before, len(first.calls))
        self.assertEqual(split_sentences(PASSAGE), [call[0] for call in second.calls])

    def test_a_release_and_the_load_after_it_build_two_sessions(self) -> None:
        """The provider goes with the session, and comes back with the next one.

        Left set, it has `murmly doctor` name the runtime of a session that no
        longer exists.
        """
        with self._load_path() as (synthesizer, constructor):
            synthesizer._load_model()
            self.assertEqual(CPU_PROVIDER, synthesizer.provider)

            synthesizer.release()
            self.assertFalse(synthesizer.resident)
            self.assertIsNone(synthesizer.provider)

            synthesizer._load_model()

            self.assertTrue(synthesizer.resident)
            self.assertEqual(CPU_PROVIDER, synthesizer.provider)
            self.assertEqual(2, constructor.call_count)

    def test_a_failed_rebuild_is_retried_rather_than_refusing_speech_for_good(self) -> None:
        """A rebuild that fails is a transient failure, not unavailability.

        `_unavailable_reason` is set once at startup by `_probe` and means
        speech output cannot run at all. Recording a failed rebuild there would
        silence the daemon for the rest of its life over one bad load, which is
        the spec's "a failed reload does not disable Murmly".
        """
        rebuilt = FakeKokoroModel()
        synthesizer = self._synthesizer()
        synthesizer.release()

        with patch.object(
            synthesizer,
            "_construct_model",
            side_effect=[RuntimeError("the session could not be built"), rebuilt],
        ):
            with self.assertRaises(RuntimeError):
                list(synthesizer.synthesize("First attempt."))
            self.assertFalse(synthesizer.resident)
            self.assertTrue(synthesizer.available)
            self.assertIsNone(synthesizer.unavailable_reason)

            chunks = list(synthesizer.synthesize("Second attempt."))

        self.assertTrue(all(len(samples) for samples, _rate in chunks))
        self.assertTrue(synthesizer.resident)
        self.assertEqual(["Second attempt."], [call[0] for call in rebuilt.calls])

    def test_a_release_during_a_synthesis_waits_rather_than_interrupting(self) -> None:
        """The pass in flight finishes whole and the release lands after it."""
        model = GatedKokoroModel()
        synthesizer = self._synthesizer(model)
        order: list[str] = []
        produced: list[np.ndarray] = []

        def speak() -> None:
            produced.extend(samples for samples, _rate in synthesizer.synthesize("One sentence."))

        speaker = threading.Thread(target=speak)
        speaker.start()
        self.assertTrue(model.entered.wait(GATE_TIMEOUT_SECONDS))

        def release() -> None:
            synthesizer.release()
            order.append("released")

        releaser = threading.Thread(target=release)
        releaser.start()
        releaser.join(0.05)

        self.assertTrue(releaser.is_alive(), "the release did not wait for the pass in flight")
        self.assertTrue(synthesizer.resident)
        order.append("gate opened")
        model.gate.set()
        speaker.join(GATE_TIMEOUT_SECONDS)
        releaser.join(GATE_TIMEOUT_SECONDS)

        self.assertEqual(["gate opened", "released"], order)
        # Counted rather than only checked for emptiness: an exception in the
        # speaker thread prints and leaves `produced` empty, and `all([])` would
        # have called that a pass that finished whole.
        self.assertEqual(1, len(produced))
        self.assertTrue(all(len(samples) for samples in produced))
        self.assertFalse(synthesizer.resident)

    def test_a_release_racing_a_construction_does_not_leave_the_session_behind(self) -> None:
        """Why the release takes `_load_lock` as well as `_model_lock`.

        `self._model` is written under `_load_lock` and read under
        `_model_lock`, so a release holding only the latter can clear the field
        while a construction is still running, and the construction then assigns
        the new session over it. The session survives its own release, still
        holding the memory the release was called to return, and the timer that
        armed it has already fired and will not fire again.
        """
        synthesizer = self._synthesizer()
        synthesizer.release()
        constructing = threading.Event()
        finish = threading.Event()

        def construct() -> FakeKokoroModel:
            constructing.set()
            finish.wait(GATE_TIMEOUT_SECONDS)
            return FakeKokoroModel()

        with patch.object(synthesizer, "_construct_model", side_effect=construct):
            loader = threading.Thread(target=synthesizer._load_model)
            loader.start()
            self.assertTrue(constructing.wait(GATE_TIMEOUT_SECONDS))

            releaser = threading.Thread(target=synthesizer.release)
            releaser.start()
            releaser.join(0.05)
            self.assertTrue(releaser.is_alive(), "the release did not wait for the construction")

            finish.set()
            loader.join(GATE_TIMEOUT_SECONDS)
            releaser.join(GATE_TIMEOUT_SECONDS)

        self.assertFalse(synthesizer.resident)


class SilenceMeasurementTests(unittest.TestCase):
    def test_edge_silence_is_not_mistaken_for_a_pause(self) -> None:
        from murmly.tts import _median_internal_gap

        rate = 1_000
        audio = np.concatenate(
            [
                np.zeros(500, dtype=np.float32),  # lead-in, not a pause
                np.full(200, 0.5, dtype=np.float32),
                np.zeros(300, dtype=np.float32),  # the only real pause
                np.full(200, 0.5, dtype=np.float32),
                np.zeros(500, dtype=np.float32),  # fade-out, not a pause
            ]
        )

        self.assertAlmostEqual(0.3, _median_internal_gap(audio, rate), places=3)

    def test_audio_with_no_internal_silence_reports_no_pause(self) -> None:
        from murmly.tts import _median_internal_gap

        self.assertEqual(0.0, _median_internal_gap(np.full(1_000, 0.5, dtype=np.float32), 1_000))


if __name__ == "__main__":
    unittest.main()
