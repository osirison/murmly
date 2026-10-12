"""What Health shows, over a recorded `murmly doctor` report. No Qt needed."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from health_fakes import FakeHealthModel, recorded_report, recorded_text, result_for
from murmly.app import health
from murmly.cli import _run_doctor
from murmly.config import MurmlyConfig
from murmly.integrations import PasteInjection
from murmly.platform import Desktop, OperatingSystem, PlatformProfile
from murmly.stt import FasterWhisperTranscriber

# Strings doctor writes itself, with characters a paraphrase or a JSON
# round trip would damage.
SPEECH_DETAIL = (
    "Speech output needs <b>kokoro-v1.0.onnx</b> & voices-v1.0.bin in ~/.local/share/murmly:\n"
    "  place both there — café “quotes”\t"
)
SPEAKER_DETAIL = 'mine-only needs an enrolled owner, and "milo" is not enrolled'
TRANSCRIPT = "Please move the quarterly review to Thursday afternoon."


def rows_for(report: dict) -> list[health.Row]:
    return health.build_rows(report)


def row_for(report: dict, title: str) -> health.Row:
    matches = [row for row in rows_for(report) if row.title == title]
    assert len(matches) == 1, [row.title for row in rows_for(report)]
    return matches[0]


def all_ok(report: dict) -> dict:
    """The recorded report with its one real finding (no focus checking on Wayland) made fine."""
    report = copy.deepcopy(report)
    report["delivery"]["verification_supported"] = True
    report["delivery"]["detail"] = "Delivery target verification is supported and enabled."
    report["platform"]["concerns"]["focus_observation"] = {
        "mechanism": "x11",
        "available": True,
        "limitations": [],
    }
    return report


class EverySectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.report = recorded_report()

    def test_every_section_of_the_recorded_report_is_in_a_row(self) -> None:
        covered = {key for row in rows_for(self.report) for key in row.keys}

        self.assertEqual(set(self.report), covered)

    def test_every_value_in_the_report_is_in_the_rows_values(self) -> None:
        shown = {pair for row in rows_for(self.report) for pair in row.every}
        expected = {pair for key, value in self.report.items() for pair in health.leaves(key, value)}

        self.assertEqual(set(), expected - shown)

    def test_each_row_has_a_sentence_and_a_raw_key_and_value(self) -> None:
        for row in rows_for(self.report):
            with self.subTest(row.title):
                self.assertTrue(row.sentence)
                self.assertTrue(row.pairs)
                view = row.view()
                for key, value in row.pairs:
                    self.assertIn(f"{key}: {value}", view["raw"])

    def test_a_section_with_no_sentence_gets_a_plain_row_with_its_values(self) -> None:
        self.report["brand_new_probe"] = {"state": "ok", "detail": "Shown <as> written."}

        row = row_for(self.report, "Brand new probe")

        self.assertEqual(health.OK, row.status)
        self.assertEqual(("brand_new_probe",), row.keys)
        self.assertIn(("brand_new_probe.detail", "Shown <as> written."), row.pairs)

    def test_sections_of_the_wrong_shape_still_appear(self) -> None:
        broken = {key: "unexpected" for key in self.report}

        rows = rows_for(broken)

        self.assertEqual(set(broken), {key for row in rows for key in row.keys})

    def test_a_report_with_a_section_missing_makes_no_row_for_it(self) -> None:
        del self.report["microphone"]

        self.assertNotIn("Microphone", [row.title for row in rows_for(self.report)])


class SentenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.report = recorded_report()

    def test_when_everything_works_every_check_is_settled_and_the_heading_says_so(self) -> None:
        report = all_ok(self.report)

        rows = rows_for(report)
        view = health.build_view(result_for(report), checking=False, checked_at=None)

        self.assertEqual([health.OK] * len(rows), [row.status for row in rows])
        self.assertEqual("Dictation works end to end", view["headline"])
        self.assertEqual(["none"] * len(rows), [row["tone"] for row in view["rows"]])

    def test_the_recorded_machine_has_one_note_about_checking_the_window(self) -> None:
        problems = [row for row in rows_for(self.report) if row.status == health.PROBLEM]

        self.assertEqual(["Checking the window first"], [row.title for row in problems])
        self.assertFalse(problems[0].urgent)
        self.assertEqual(
            "Delivery target verification requires an X11 session.",
            dict(problems[0].pairs)["delivery.detail"],
        )

    def test_speech_output_unavailable_is_a_problem_with_its_detail_byte_for_byte(self) -> None:
        self.report["speech_output"]["available"] = False
        self.report["speech_output"]["detail"] = SPEECH_DETAIL

        row = row_for(self.report, "Speech")

        self.assertEqual(health.PROBLEM, row.status)
        self.assertFalse(row.urgent)
        self.assertEqual("Murmly cannot speak aloud. Dictation is unaffected.", row.sentence)
        self.assertEqual(SPEECH_DETAIL, dict(row.pairs)["speech_output.detail"])
        self.assertEqual("false", dict(row.pairs)["speech_output.available"])
        self.assertIn(f"speech_output.detail: {SPEECH_DETAIL}", row.view()["raw"])
        self.assertEqual("amber", row.view()["tone"])

    def test_speaker_recognition_that_cannot_run_is_a_problem_with_the_reason_as_reported(self) -> None:
        speakers = self.report["speaker_recognition"]
        speakers.update(mode="mine-only", available=False, detail=SPEAKER_DETAIL, owner_enrolled=False)

        row = row_for(self.report, "Voices")

        self.assertEqual(health.PROBLEM, row.status)
        self.assertEqual(SPEAKER_DETAIL, dict(row.pairs)["speaker_recognition.detail"])
        self.assertEqual("false", dict(row.pairs)["speaker_recognition.available"])
        self.assertIn(f"speaker_recognition.detail: {SPEAKER_DETAIL}", row.view()["raw"])

    def test_speaker_mode_off_is_not_a_problem_though_doctor_says_unavailable(self) -> None:
        self.report["speaker_recognition"].update(
            mode="off", available=False, detail="the speaker mode is off"
        )

        row = row_for(self.report, "Voices")

        self.assertEqual(health.OK, row.status)
        self.assertEqual("Everyone's voice is typed.", row.sentence)

    def test_a_feature_that_is_switched_off_is_not_a_problem(self) -> None:
        self.report["speech_output"].update(
            enabled=False, available=False, detail="Speech output is disabled. Set enabled = true under [tts]."
        )
        self.report["overlay"].update(
            enabled=False, available=False, detail="Overlay is disabled in configuration."
        )

        self.assertEqual(health.OK, row_for(self.report, "Speech").status)
        self.assertEqual(health.OK, row_for(self.report, "On-screen indicator").status)

    def test_what_stops_dictation_is_urgent_and_the_heading_says_it_needs_you(self) -> None:
        self.report["microphone"].update(
            device_present=False, available=False, detail="No microphone input device was found."
        )
        self.report["installation"].update(hotkey_held=False)

        microphone = row_for(self.report, "Microphone")
        key = row_for(self.report, "Your key")
        view = health.build_view(result_for(self.report), checking=False, checked_at=None)

        self.assertEqual("No microphone was found.", microphone.sentence)
        self.assertTrue(microphone.urgent)
        self.assertEqual("No microphone input device was found.", dict(microphone.pairs)["microphone.detail"])
        self.assertEqual("Meta + X is set, but your desktop is not delivering it to murmly.", key.sentence)
        self.assertEqual("crimson", microphone.view()["tone"])
        self.assertEqual("Something needs you before dictation works", view["headline"])

    def test_a_problem_that_leaves_dictation_alone_says_so_in_the_heading(self) -> None:
        self.report["speech_output"].update(available=False, detail=SPEECH_DETAIL)

        view = health.build_view(result_for(all_ok(self.report)), checking=False, checked_at=None)

        self.assertEqual("Dictation works, with something to look at", view["headline"])
        self.assertIn("none affects dictation", view["body"])

    def test_a_runtime_that_could_not_be_worked_out_is_shown_as_reported(self) -> None:
        detail = "Unable to determine the transcription runtime: the cuda extra is not installed"
        self.report["runtime_detail"] = detail
        self.report["runtime_device"] = None

        row = row_for(self.report, "Transcription")

        self.assertTrue(row.urgent)
        self.assertEqual(detail, dict(row.pairs)["runtime_detail"])

    def test_a_remedy_is_shown_one_step_at_a_time_as_written(self) -> None:
        self.report["paste_injection"] = {
            "available": False,
            "method": None,
            "reason": "No paste tool found.",
            "remedy": ["Install xdotool.", "Or install wtype, then sign in again."],
        }

        pairs = dict(row_for(self.report, "Typing your words").pairs)

        self.assertEqual("No paste tool found.", pairs["paste_injection.reason"])
        self.assertEqual("Install xdotool.", pairs["paste_injection.remedy[0]"])
        self.assertEqual("Or install wtype, then sign in again.", pairs["paste_injection.remedy[1]"])

    def test_an_unavailable_platform_concern_is_a_problem_with_its_reason(self) -> None:
        self.report["platform"]["concerns"]["speech_synthesis"] = {
            "mechanism": None,
            "available": False,
            "reason": "No speech runtime for this platform.",
            "remedy": [],
            "limitations": [],
        }

        row = row_for(self.report, "Speech")

        self.assertEqual(health.PROBLEM, row.status)
        self.assertEqual(
            "No speech runtime for this platform.",
            dict(row.pairs)["platform.concerns.speech_synthesis.reason"],
        )


class TrayTests(unittest.TestCase):
    def view(self, found: bool | None) -> dict:
        return health.build_view(result_for(all_ok(recorded_report())), checking=False, checked_at=None, tray_found=found)

    def test_nothing_is_said_until_the_tray_code_reports(self) -> None:
        self.assertNotIn("System tray", [row["title"] for row in self.view(None)["rows"]])

    def test_no_tray_found_is_said_plainly(self) -> None:
        rows = {row["title"]: row for row in self.view(False)["rows"]}

        self.assertEqual("No system tray was found, so murmly runs from its window only.", rows["System tray"]["sentence"])
        self.assertEqual("tray.found: false", rows["System tray"]["raw"])
        self.assertEqual("amber", rows["System tray"]["tone"])

    def test_a_tray_that_was_found_is_settled(self) -> None:
        rows = {row["title"]: row for row in self.view(True)["rows"]}

        self.assertEqual("none", rows["System tray"]["tone"])


class ViewTests(unittest.TestCase):
    def test_before_any_run_the_view_says_it_is_checking(self) -> None:
        view = health.build_view(None, checking=True, checked_at=None)

        self.assertEqual(health.CHECKING, view["state"])
        self.assertEqual([], view["rows"])
        self.assertFalse(view["canCopy"])

    def test_a_failed_run_shows_what_it_said_and_cannot_be_copied(self) -> None:
        result = health.DoctorResult(error="Traceback: boom <b>\nsecond line")

        view = health.build_view(result, checking=False, checked_at=None)

        self.assertEqual(health.FAILED, view["state"])
        self.assertEqual("Traceback: boom <b>\nsecond line", view["error"])
        self.assertFalse(view["canCopy"])

    def test_checking_again_keeps_the_rows_on_screen(self) -> None:
        view = health.build_view(result_for(recorded_report()), checking=True, checked_at=None)

        self.assertEqual(health.CHECKING, view["state"])
        self.assertTrue(view["rows"])
        self.assertTrue(view["canCopy"])


class CopyTests(unittest.TestCase):
    def test_the_copied_text_is_exactly_what_doctor_printed(self) -> None:
        text = recorded_text()

        result = health.parse_doctor_output(text, "", 0)

        self.assertEqual(text, result.text)
        self.assertEqual(json.loads(text), result.report)

    def test_the_recorded_report_is_what_doctor_prints_for_its_own_content(self) -> None:
        # The fixture is doctor's `json.dumps(report, indent=2)` plus a newline.
        text = recorded_text()

        self.assertEqual(json.dumps(json.loads(text), indent=2) + "\n", text)


class DoctorOutputTests(unittest.TestCase):
    """The copied report holds no transcript, over the real report builder."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def doctor_output(self) -> str:
        profile = PlatformProfile(
            operating_system=OperatingSystem.LINUX, architecture="x86_64", desktop=Desktop.PLASMA
        )
        config = MurmlyConfig(socket_path=self.root / "murmly.sock", config_path=self.root / "config.toml")
        # The daemon holds a transcript, and every answer it gives carries it.
        # Nothing in doctor asks for it; this is what would show if it ever leaked.
        daemon_answer = {
            "ok": True,
            "state": "IDLE",
            "restart_ready": True,
            "model_resident": True,
            "last_words": {"text": TRANSCRIPT, "produced_at": "2026-10-12T10:00:00+00:00", "delivered": True},
            "text": TRANSCRIPT,
        }
        with (
            patch.object(FasterWhisperTranscriber, "resolve_runtime", Mock(return_value=("cpu", "int8"))),
            patch("murmly.cli.choose_clipboard_copy_command", return_value=["xclip"]),
            patch(
                "murmly.cli.select_paste_injection",
                return_value=PasteInjection("xdotool", ("xdotool", "key", "ctrl+v")),
            ),
            patch("murmly.cli.system_memory_returnable", return_value=True),
            patch("murmly.cli.send_command", return_value=daemon_answer),
            patch("murmly.cli.default_data_dir", return_value=self.root),
            patch("murmly.cli.live_transcription_diagnostics", return_value={"live_transcribe": False}),
            patch("murmly.cli.speech_output_diagnostics", return_value={"enabled": True, "available": True}),
            patch("murmly.cli.overlay_diagnostics", return_value={"enabled": True, "available": True}),
            patch("murmly.cli.microphone_diagnostics", return_value={"device_present": True, "available": True}),
            patch("murmly.cli.installation_diagnostics", return_value={"installed": True, "service_active": True}),
            redirect_stdout(StringIO()) as output,
        ):
            _run_doctor(config, profile)
        return output.getvalue()

    def test_doctors_own_output_has_no_transcript_and_reaches_health_unchanged(self) -> None:
        text = self.doctor_output()
        self.assertTrue(text.startswith("{"))
        self.assertNotIn(TRANSCRIPT, text)
        self.assertNotIn("last_words", text)

        result = health.parse_doctor_output(text, "", 0)
        view = health.build_view(result, checking=False, checked_at=None)

        self.assertEqual(text, result.text)
        self.assertNotIn(TRANSCRIPT, result.text)
        self.assertNotIn(TRANSCRIPT, json.dumps(view))
        self.assertEqual(set(result.report), {key for row in health.build_rows(result.report) for key in row.keys})
        self.assertTrue(view["canCopy"])


class RunnerTests(unittest.TestCase):
    def python(self, source: str):
        return lambda: [sys.executable, "-c", source]

    def test_it_runs_the_same_command_a_person_would_with_no_input(self) -> None:
        process = Mock()
        process.communicate.return_value = (recorded_text(), "")
        process.returncode = 0
        popen = Mock(return_value=process)

        result = health.DoctorRunner(popen=popen).run()

        arguments, options = popen.call_args
        self.assertEqual([sys.executable, "-m", "murmly", "doctor"], arguments[0])
        self.assertIs(subprocess.DEVNULL, options["stdin"])
        self.assertEqual(recorded_text(), result.text)
        self.assertEqual("", result.error)

    def test_a_real_process_printing_a_report_is_parsed(self) -> None:
        runner = health.DoctorRunner(command=self.python("print('{\"session\": \"x11\"}')"))

        result = runner.run()

        self.assertEqual({"session": "x11"}, result.report)
        self.assertEqual('{"session": "x11"}\n', result.text)

    def test_a_failure_shows_what_the_command_said_unaltered(self) -> None:
        said = "murmly: something <broke> — see below\nline two"
        runner = health.DoctorRunner(
            command=self.python(f"import sys; sys.stderr.write({said!r}); sys.exit(3)")
        )

        result = runner.run()

        self.assertIsNone(result.report)
        self.assertEqual(said, result.error)

    def test_output_that_is_not_a_report_is_an_error(self) -> None:
        for source in ("print('hello')", "print('[1, 2]')", "pass"):
            with self.subTest(source):
                result = health.DoctorRunner(command=self.python(source)).run()

                self.assertIsNone(result.report)
                self.assertEqual("murmly doctor printed something that was not a report.", result.error)

    def test_a_run_that_does_not_finish_is_stopped_and_reported(self) -> None:
        runner = health.DoctorRunner(command=self.python("import time; time.sleep(30)"), timeout=0.3)
        started = time.monotonic()

        result = runner.run()

        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual("murmly doctor did not finish within 0.3 seconds.", result.error)

    def test_a_command_that_cannot_start_is_an_error_not_a_crash(self) -> None:
        runner = health.DoctorRunner(command=lambda: ["/no/such/program-murmly"])

        result = runner.run()

        self.assertIsNone(result.report)
        self.assertIn("murmly doctor could not be started", result.error)

    def test_cancel_stops_a_run_in_progress_without_waiting_for_it(self) -> None:
        runner = health.DoctorRunner(command=self.python("import time; time.sleep(30)"), timeout=60)
        results: list[health.DoctorResult] = []
        thread = threading.Thread(target=lambda: results.append(runner.run()))
        thread.start()
        deadline = time.monotonic() + 5
        while runner._process is None and time.monotonic() < deadline:
            time.sleep(0.01)
        started = time.monotonic()

        runner.cancel()
        thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNone(results[0].report)

    def test_after_cancel_nothing_more_is_started(self) -> None:
        popen = Mock()
        runner = health.DoctorRunner(popen=popen)

        runner.cancel()
        result = runner.run()

        popen.assert_not_called()
        self.assertIsNone(result.report)


class ModelTests(unittest.TestCase):
    def test_the_model_asks_the_runner_and_can_cancel_it(self) -> None:
        runner = Mock()
        runner.run.return_value = health.DoctorResult(error="x")
        model = health.HealthModel(runner)

        self.assertEqual("x", model.check().error)
        model.cancel()

        runner.cancel.assert_called_once_with()

    def test_the_fake_model_is_a_drop_in(self) -> None:
        self.assertEqual(recorded_text(), FakeHealthModel().check().text)


if __name__ == "__main__":
    unittest.main()
