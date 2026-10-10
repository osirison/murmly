"""The `speaker_recognition` section of `murmly doctor`."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from murmly.cli import _run_doctor, daemon_residency, speaker_recognition_diagnostics
from murmly.config import MurmlyConfig, load_config
from murmly.integrations import PasteInjection
from murmly.platform import Desktop, OperatingSystem, PlatformProfile
from murmly.speakers import (
    CPU_PROVIDER,
    EMBEDDING_DIMENSION,
    SPEAKER_MODEL_FILENAME,
    SPEAKER_MODEL_SHA256,
)
from murmly.stt import FasterWhisperTranscriber
from murmly.voiceprints import VoiceprintStore

KEYS = {
    "mode",
    "mode_rejected_value",
    "match_threshold",
    "match_threshold_rejected_value",
    "owner",
    "owner_enrolled",
    "enrolled_count",
    "enrolled_names",
    "voiceprints_path",
    "voiceprints_need_reenrolment",
    "model_path",
    "model_present",
    "model_checksum_matches",
    "available",
    "detail",
    "provider",
    "resident",
    "resident_detail",
    "unload_after_idle_s",
}
MODEL_BYTES = b"a stand-in for the speaker model"
MODEL_SHA256 = hashlib.sha256(MODEL_BYTES).hexdigest()
OTHER_MODEL = "0" * 64
PROFILES = (
    PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64", desktop=Desktop.PLASMA),
    PlatformProfile(operating_system=OperatingSystem.WINDOWS, architecture="x86_64"),
    PlatformProfile(operating_system=OperatingSystem.MACOS, architecture="arm64"),
)


class SpeakerDiagnosticsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.model = self.data_dir / SPEAKER_MODEL_FILENAME
        # The pinned checksum of the file is swapped for the stand-in's, so "the
        # right file" can be made here without the real 26 MB model. Voiceprints
        # keep the real pin, as the store's default does.
        patch("murmly.cli.SPEAKER_MODEL_SHA256", MODEL_SHA256).start()
        patch("murmly.cli.default_data_dir", return_value=self.data_dir).start()
        self.addCleanup(patch.stopall)

    def config(self, **overrides: object) -> MurmlyConfig:
        return MurmlyConfig(
            socket_path=self.root / "murmly.sock",
            config_path=self.root / "config.toml",
            **overrides,
        )

    def install_model(self, contents: bytes = MODEL_BYTES) -> None:
        self.model.write_bytes(contents)

    def enrol(self, *names: str, model_sha256: str = SPEAKER_MODEL_SHA256) -> list[np.ndarray]:
        store = VoiceprintStore(self.data_dir, model_sha256=model_sha256)
        vectors = []
        for index, name in enumerate(names, start=1):
            vector = np.random.default_rng(index).standard_normal(EMBEDDING_DIMENSION)
            store.add_or_replace(name, vector)
            vectors.append(vector / np.linalg.norm(vector))
        return vectors

    def section(self, config: MurmlyConfig, **kwargs: object) -> dict[str, object]:
        return json.loads(json.dumps(speaker_recognition_diagnostics(config, **kwargs)))


class ShapeTests(SpeakerDiagnosticsTestCase):
    def test_the_same_keys_in_every_mode(self) -> None:
        self.install_model()
        self.enrol("Milo")
        for mode in ("off", "mine-only", "label-everyone"):
            with self.subTest(mode=mode):
                report = self.section(self.config(speaker_mode=mode, speaker_owner="Milo"))

                self.assertEqual(KEYS, set(report))
                self.assertEqual(mode, report["mode"])

    def test_the_same_keys_with_nothing_installed_or_enrolled(self) -> None:
        for mode in ("off", "mine-only", "label-everyone"):
            with self.subTest(mode=mode):
                report = self.section(self.config(speaker_mode=mode))

                self.assertEqual(KEYS, set(report))

    def test_the_same_keys_for_every_platform_profile_in_the_whole_report(self) -> None:
        self.install_model()
        for profile in PROFILES:
            for mode in ("off", "mine-only"):
                with self.subTest(operating_system=profile.operating_system, mode=mode):
                    config = self.config(speaker_mode=mode)
                    with (
                        patch.object(
                            FasterWhisperTranscriber, "resolve_runtime", Mock(return_value=("cpu", "int8"))
                        ),
                        patch("murmly.cli.choose_clipboard_copy_command", return_value=["xclip"]),
                        patch(
                            "murmly.cli.select_paste_injection",
                            return_value=PasteInjection("xdotool", ("xdotool", "key", "ctrl+v")),
                        ),
                        patch("murmly.cli.system_memory_returnable", return_value=True),
                        redirect_stdout(StringIO()) as output,
                    ):
                        _run_doctor(config, profile)

                    self.assertEqual(KEYS, set(json.loads(output.getvalue())["speaker_recognition"]))

    def test_a_probe_that_fails_keeps_the_keys_and_the_daemons_answer(self) -> None:
        config = self.config(speaker_mode="label-everyone")
        with (
            patch.object(FasterWhisperTranscriber, "resolve_runtime", Mock(return_value=("cpu", "int8"))),
            patch("murmly.cli.choose_clipboard_copy_command", return_value=["xclip"]),
            patch(
                "murmly.cli.select_paste_injection",
                return_value=PasteInjection("xdotool", ("xdotool", "key", "ctrl+v")),
            ),
            patch("murmly.cli.system_memory_returnable", return_value=True),
            patch("murmly.cli.daemon_residency", return_value=((False, None), (False, None), (True, None))),
            patch("murmly.cli.speaker_recognition_diagnostics", side_effect=RuntimeError("the probe broke")),
            redirect_stdout(StringIO()) as output,
        ):
            _run_doctor(config, PROFILES[0])

        report = json.loads(output.getvalue())["speaker_recognition"]
        self.assertEqual(KEYS, set(report))
        self.assertIs(False, report["available"])
        self.assertIn("the probe broke", report["detail"])
        self.assertIs(True, report["resident"])

    def test_the_provider_is_always_the_cpu_provider(self) -> None:
        for mode in ("off", "label-everyone"):
            self.assertEqual(CPU_PROVIDER, self.section(self.config(speaker_mode=mode))["provider"])


class VectorTests(SpeakerDiagnosticsTestCase):
    def test_no_vector_value_appears_anywhere_in_the_output(self) -> None:
        self.install_model()
        vectors = self.enrol("Milo", "Ana")
        config = self.config(speaker_mode="mine-only", speaker_owner="Milo")

        report = self.section(config)
        text = json.dumps(report)

        self.assertEqual(["Milo", "Ana"], report["enrolled_names"])
        self.assertEqual(2, report["enrolled_count"])
        for vector in vectors:
            for value in vector[:20]:
                self.assertNotIn(repr(float(value)), text)
        # No long run of digits after a decimal point, which any stored value has.
        self.assertIsNone(re.search(r"-?\d\.\d{8,}", text))
        self.assertNotIn("embedding", text)

    def test_the_voiceprint_file_itself_does_carry_the_numbers_this_test_looks_for(self) -> None:
        # The check above is only worth something if the values would show up.
        self.enrol("Milo")
        stored = (self.data_dir / "voiceprints.json").read_text(encoding="utf-8")

        self.assertIsNotNone(re.search(r"-?\d\.\d{8,}", stored))


class ResidencyTests(SpeakerDiagnosticsTestCase):
    def test_no_daemon_answering_is_unknown_and_not_false(self) -> None:
        def nobody(_socket: str, _command: str):
            raise FileNotFoundError("no socket")

        _transcription, _synthesis, speaker = daemon_residency(self.config(), nobody)
        report = self.section(self.config(speaker_mode="label-everyone"), residency=speaker)

        self.assertIsNone(report["resident"])
        self.assertIn("No Murmly daemon is running", report["resident_detail"])

    def test_a_daemon_holding_the_model_is_reported_as_holding_it(self) -> None:
        status = {"ok": True, "model_resident": False, "speaker_model_resident": True}

        *_, speaker = daemon_residency(self.config(), lambda *_: status)
        report = self.section(self.config(), residency=speaker)

        self.assertIs(True, report["resident"])
        self.assertIsNone(report["resident_detail"])

    def test_a_daemon_not_holding_the_model_is_reported_as_not_holding_it(self) -> None:
        status = {"ok": True, "model_resident": False, "speaker_model_resident": False}

        *_, speaker = daemon_residency(self.config(), lambda *_: status)

        self.assertEqual((False, None), speaker)

    def test_a_build_error_is_carried_beside_not_resident(self) -> None:
        status = {
            "ok": True,
            "model_resident": False,
            "speaker_model_resident": False,
            "speaker_model_resident_detail": "the model could not be loaded: bad protobuf",
        }

        *_, speaker = daemon_residency(self.config(), lambda *_: status)

        self.assertEqual((False, "the model could not be loaded: bad protobuf"), speaker)

    def test_a_daemon_that_does_not_know_the_question_is_unknown(self) -> None:
        *_, speaker = daemon_residency(self.config(), lambda *_: {"ok": True, "model_resident": False})

        self.assertIsNone(speaker[0])
        self.assertIn("does not report speaker model residency", speaker[1])

    def test_the_section_asks_the_daemon_nothing(self) -> None:
        asked = Mock()

        with patch("murmly.cli.send_command", asked):
            self.section(self.config(speaker_mode="label-everyone"))

        asked.assert_not_called()

    def test_the_idle_period_is_the_transcription_period(self) -> None:
        report = self.section(self.config(unload_after_idle_s=123))

        self.assertEqual(123, report["unload_after_idle_s"])


class AvailabilityTests(SpeakerDiagnosticsTestCase):
    def test_mode_off_is_stated_and_still_reports_everything_else(self) -> None:
        self.install_model()
        self.enrol("Milo")

        report = self.section(self.config(speaker_owner="Milo"))

        self.assertEqual("off", report["mode"])
        self.assertIs(False, report["available"])
        self.assertIn("mode is off", report["detail"])
        self.assertEqual(1, report["enrolled_count"])
        self.assertIs(True, report["owner_enrolled"])
        self.assertEqual(str(self.data_dir / "voiceprints.json"), report["voiceprints_path"])

    def test_defaults_report_the_threshold_in_effect_and_no_voices(self) -> None:
        report = self.section(self.config())

        self.assertEqual(self.config().speaker_match_threshold_percent, report["match_threshold"])
        self.assertEqual(0, report["enrolled_count"])
        self.assertEqual([], report["enrolled_names"])
        self.assertIsNone(report["owner"])
        self.assertIsNone(report["owner_enrolled"])

    def test_a_rejected_mode_and_threshold_are_named(self) -> None:
        path = self.root / "config.toml"
        path.write_text('[speakers]\nmode = "loud"\nmatch_threshold = 5\n', encoding="utf-8")

        report = self.section(load_config(path))

        self.assertEqual("off", report["mode"])
        self.assertEqual("loud", report["mode_rejected_value"])
        self.assertEqual(5, report["match_threshold_rejected_value"])
        self.assertEqual(self.config().speaker_match_threshold_percent, report["match_threshold"])

    def test_a_threshold_that_is_not_a_number_is_named(self) -> None:
        path = self.root / "config.toml"
        path.write_text('[speakers]\nmatch_threshold = "high"\n', encoding="utf-8")

        report = self.section(load_config(path))

        self.assertEqual("high", report["match_threshold_rejected_value"])

    def test_nothing_rejected_reports_none(self) -> None:
        report = self.section(self.config())

        self.assertIsNone(report["mode_rejected_value"])
        self.assertIsNone(report["match_threshold_rejected_value"])

    def test_a_missing_model_is_named_by_path(self) -> None:
        for mode in ("mine-only", "label-everyone"):
            with self.subTest(mode=mode):
                report = self.section(self.config(speaker_mode=mode, speaker_owner="Milo"))

                self.assertIs(False, report["model_present"])
                self.assertIsNone(report["model_checksum_matches"])
                self.assertIs(False, report["available"])
                self.assertIn("speaker model is missing", report["detail"])
                self.assertIn(str(self.model), report["detail"])
                self.assertEqual(str(self.model), report["model_path"])

    def test_a_model_with_the_wrong_checksum_cannot_run(self) -> None:
        self.install_model(b"not the model")
        self.enrol("Milo")

        report = self.section(self.config(speaker_mode="label-everyone"))

        self.assertIs(True, report["model_present"])
        self.assertIs(False, report["model_checksum_matches"])
        self.assertIs(False, report["available"])
        self.assertIn("checksum", report["detail"])
        self.assertIn(str(self.model), report["detail"])

    def test_the_right_model_is_confirmed(self) -> None:
        self.install_model()

        report = self.section(self.config(speaker_mode="label-everyone"))

        self.assertIs(True, report["model_checksum_matches"])
        self.assertIs(True, report["available"])
        self.assertIsNone(report["detail"])

    def test_label_everyone_needs_no_voices(self) -> None:
        self.install_model()

        report = self.section(self.config(speaker_mode="label-everyone"))

        self.assertIs(True, report["available"])
        self.assertEqual(0, report["enrolled_count"])

    def test_an_owner_who_is_not_enrolled_is_stated(self) -> None:
        self.install_model()
        self.enrol("Ana")

        report = self.section(self.config(speaker_mode="mine-only", speaker_owner="Milo"))

        self.assertEqual("Milo", report["owner"])
        self.assertIs(False, report["owner_enrolled"])
        self.assertIs(False, report["available"])
        self.assertIn("mine-only needs an enrolled owner", report["detail"])
        self.assertIn('"Milo" is not enrolled', report["detail"])

    def test_mine_only_without_an_owner_set_says_so(self) -> None:
        self.install_model()
        self.enrol("Milo")

        report = self.section(self.config(speaker_mode="mine-only"))

        self.assertIsNone(report["owner"])
        self.assertIs(False, report["available"])
        self.assertIn("no owner is set", report["detail"])

    def test_the_owner_matches_without_regard_to_case(self) -> None:
        self.install_model()
        self.enrol("Milo")

        report = self.section(self.config(speaker_mode="mine-only", speaker_owner="milo"))

        self.assertIs(True, report["owner_enrolled"])
        self.assertIs(True, report["available"])

    def test_voices_from_another_model_are_reported_as_needing_enrolment_again(self) -> None:
        self.install_model()
        self.enrol("Milo", model_sha256=OTHER_MODEL)

        report = self.section(self.config(speaker_mode="mine-only", speaker_owner="Milo"))

        self.assertIs(True, report["voiceprints_need_reenrolment"])
        self.assertEqual(0, report["enrolled_count"])
        self.assertIs(False, report["owner_enrolled"])
        self.assertIs(False, report["available"])
        self.assertIn("different speaker model", report["detail"])
        self.assertIn("enrolled again", report["detail"])

    def test_voices_from_another_model_are_noted_but_do_not_stop_label_everyone(self) -> None:
        self.install_model()
        self.enrol("Milo", model_sha256=OTHER_MODEL)

        report = self.section(self.config(speaker_mode="label-everyone"))

        self.assertIs(True, report["voiceprints_need_reenrolment"])
        self.assertIs(True, report["available"])
        self.assertIn("enrol those voices again", report["detail"])

    def test_an_unreadable_voiceprint_file_is_reported_in_mine_only(self) -> None:
        self.install_model()
        (self.data_dir / "voiceprints.json").write_text("{not json", encoding="utf-8")

        report = self.section(self.config(speaker_mode="mine-only", speaker_owner="Milo"))

        self.assertIs(False, report["available"])
        self.assertIn("voiceprint file cannot be used", report["detail"])
        self.assertIs(False, report["voiceprints_need_reenrolment"])


if __name__ == "__main__":
    unittest.main()
