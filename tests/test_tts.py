from __future__ import annotations

import ast
import re
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import numpy as np

import murmly.tts
from fakes import FakeKokoroModel
from module_stubs import injected_module
from murmly.config import DEFAULT_TTS_RATE_PERCENT, DEFAULT_TTS_VOICE, MurmlyConfig, load_config
from murmly.tts import (
    CALIBRATION_TEXT,
    CPU_PROVIDER,
    CUDA_EXTRA_REMEDY,
    CUDA_PROVIDER,
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
        with tempfile.TemporaryDirectory() as temp_dir:
            return make_config(temp_dir, device=device)

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
        with patch("murmly.tts.load_cuda_libraries") as loader:
            self.assertEqual([CPU_PROVIDER], resolve_providers(self._config("cpu")))

        loader.assert_not_called()

    def test_cuda_is_preferred_when_its_libraries_are_present(self) -> None:
        with self._gpu_build(), patch("murmly.tts.load_cuda_libraries", return_value=True):
            providers = resolve_providers(self._config("auto"))

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
        """`auto` is the default and the documented CPU install hits this branch.

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
        """`device` is the transcription setting; it cannot disable speech.

        A person who pinned Whisper to the GPU has not asked for GPU synthesis,
        so a missing synthesis library falls back rather than refusing every
        speech session. Silence about it is what is not allowed.
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

    def test_speech_survives_transcription_being_pinned_to_the_gpu(self) -> None:
        """The documented CUDA install: CTranslate2 on the GPU, ONNX on the CPU.

        `uv sync --extra cuda --extra tts` deliberately carries the CPU runtime,
        so this is the ordinary configuration rather than a broken one, and a
        synthesizer must come out of it available.
        """
        config = self._config("cuda")
        with patch("onnxruntime.get_available_providers", return_value=[CPU_PROVIDER]):
            providers = resolve_providers(config)

        self.assertEqual([CPU_PROVIDER], providers)

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
