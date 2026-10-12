"""Health through the real Qt layer: the window, the controller and its worker thread."""

from __future__ import annotations

import threading
import time
import unittest

from health_fakes import FakeHealthModel, recorded_report, recorded_text, result_for
from home_fakes import FakeChannel
from murmly.app import health, home

from qt_app import MessageLog, qt_application

SPEECH_DETAIL = (
    "Speech output needs <b>kokoro-v1.0.onnx</b> & voices-v1.0.bin in ~/.local/share/murmly:\n"
    "  place both there — café\t"
)
SPEAKER_DETAIL = 'mine-only needs an enrolled owner, and "milo" is not enrolled'


class HealthQtCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = qt_application()

    def build(self, model: FakeHealthModel | None = None, copy_text=None) -> None:
        from murmly.app import controller, qt

        self.log = MessageLog()
        self.log.__enter__()
        self.addCleanup(self.log.__exit__)
        self.assertEqual([], qt.prepare_application())
        self.model = model if model is not None else FakeHealthModel()
        self.copied: list[str] = []
        if copy_text is None:
            self.health = controller.HealthController(self.model)
        else:
            self.health = controller.HealthController(self.model, copy_text=copy_text)
        self.home = controller.HomeController(home.HomeModel(FakeChannel()))
        self.engine, self.theme = qt.create_engine(self.home, self.health)
        self.addCleanup(self._close)
        self.window = self.engine.rootObjects()[0]
        self.settle()

    def _close(self) -> None:
        self.home.shutdown()
        self.health.shutdown()
        self.window.close()
        self.engine.deleteLater()
        self.settle()

    def settle(self) -> None:
        self.application.processEvents()

    def wait_for(self, predicate, timeout: float = 5.0, message: str = "condition") -> float:
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            self.settle()
            if predicate():
                return time.monotonic() - started
            time.sleep(0.01)
        self.fail(f"timed out waiting for {message}")

    def child(self, name: str):
        pending = [self.window.contentItem()]
        while pending:
            item = pending.pop()
            if item.objectName() == name:
                return item
            pending.extend(item.childItems())
        self.fail(f"no item named {name}")

    def has_child(self, name: str) -> bool:
        pending = [self.window.contentItem()]
        while pending:
            item = pending.pop()
            if item.objectName() == name:
                return True
            pending.extend(item.childItems())
        return False

    def text(self, name: str) -> str:
        return self.child(name).property("text")

    def open_health(self) -> None:
        self.child("nav-health").click()
        self.settle()

    def rows_shown(self) -> int:
        count = 0
        while self.has_child(f"health-row-{count}"):
            count += 1
        return count


class OpeningTests(HealthQtCase):
    def test_nothing_runs_until_health_is_opened_and_then_it_runs_off_the_gui_thread(self) -> None:
        self.build()
        self.wait_for(lambda: self.window.property("onScreen"), message="the window to show")
        time.sleep(0.3)
        self.settle()
        self.assertEqual(0, self.model.checks)

        self.open_health()
        self.wait_for(lambda: self.model.checks == 1, message="the first run")

        self.assertNotIn(threading.get_ident(), self.model.threads)

    def test_every_check_is_listed_with_its_sentence_and_raw_values(self) -> None:
        self.build()
        self.open_health()
        expected = health.build_rows(recorded_report())
        self.wait_for(lambda: self.rows_shown() == len(expected), message="the rows")

        for index, row in enumerate(expected):
            with self.subTest(row.title):
                self.assertEqual(row.title, self.text(f"row-title-{index}"))
                self.assertEqual(row.sentence, self.text(f"row-sentence-{index}"))
                self.assertEqual(row.view()["raw"], self.text(f"row-raw-{index}"))
        shown_titles = {self.text(f"row-title-{index}") for index in range(len(expected))}
        self.assertEqual({row.title for row in expected}, shown_titles)

    def test_the_heading_says_how_it_went(self) -> None:
        self.build()
        self.open_health()
        self.wait_for(lambda: self.text("health-headline") == "Dictation works, with something to look at", message="heading")

        self.assertIn("none affects dictation", self.text("health-body"))

    def test_opening_it_again_runs_the_checks_again_and_check_again_does_too(self) -> None:
        self.build()
        self.open_health()
        self.wait_for(lambda: self.model.checks == 1, message="first run")
        self.child("nav-home").click()
        self.settle()
        time.sleep(0.2)
        self.assertEqual(1, self.model.checks)

        self.open_health()
        self.wait_for(lambda: self.model.checks == 2, message="reopened")
        self.wait_for(lambda: self.child("check-again").property("enabled"), message="finished")
        self.child("check-again").click()
        self.wait_for(lambda: self.model.checks == 3, message="checked again")

    def test_nothing_runs_on_a_timer(self) -> None:
        self.build()
        self.open_health()
        self.wait_for(lambda: self.model.checks == 1, message="first run")

        time.sleep(1.5)
        self.settle()

        self.assertEqual(1, self.model.checks)

    def test_check_again_is_off_while_a_run_is_in_progress(self) -> None:
        model = FakeHealthModel()
        model.gate = threading.Event()
        self.build(model)
        self.open_health()
        self.wait_for(lambda: self.model.checks == 1, message="a run to start")
        self.wait_for(lambda: self.text("check-again") == "Checking…", message="the busy label")

        self.assertFalse(self.child("check-again").property("enabled"))
        self.assertFalse(self.child("copy-report").property("enabled"))
        self.assertEqual("Checking this computer", self.text("health-headline"))

        model.gate.set()
        self.wait_for(lambda: self.child("check-again").property("enabled"), message="the run to finish")
        self.assertEqual(1, self.model.checks)

    def test_closing_the_app_stops_a_run_and_does_not_wait_for_it(self) -> None:
        model = FakeHealthModel()
        model.gate = threading.Event()
        self.build(model)
        self.open_health()
        self.wait_for(lambda: self.model.checks == 1, message="a run to start")

        started = time.monotonic()
        self.health.shutdown()

        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(model.cancelled)


class ProblemTests(HealthQtCase):
    def row_index(self, title: str) -> int:
        index = 0
        while self.has_child(f"health-row-{index}"):
            if self.text(f"row-title-{index}") == title:
                return index
            index += 1
        self.fail(f"no row titled {title}")

    def test_a_problem_is_marked_and_its_detail_is_shown_byte_for_byte(self) -> None:
        report = recorded_report()
        report["speech_output"].update(available=False, detail=SPEECH_DETAIL)
        report["speaker_recognition"].update(mode="mine-only", available=False, detail=SPEAKER_DETAIL)
        self.build(FakeHealthModel(result_for(report)))
        self.open_health()
        expected = health.build_rows(report)
        self.wait_for(lambda: self.rows_shown() == len(expected), message="the rows")

        speech = self.row_index("Speech")
        voices = self.row_index("Voices")
        self.assertEqual("amber", self.child(f"health-row-{speech}").property("tone"))
        self.assertEqual("Murmly cannot speak aloud. Dictation is unaffected.", self.text(f"row-sentence-{speech}"))
        self.assertIn(f"speech_output.detail: {SPEECH_DETAIL}", self.text(f"row-raw-{speech}"))
        self.assertEqual("amber", self.child(f"health-row-{voices}").property("tone"))
        self.assertIn(f"speaker_recognition.detail: {SPEAKER_DETAIL}", self.text(f"row-raw-{voices}"))
        microphone = self.row_index("Microphone")
        self.assertEqual("none", self.child(f"health-row-{microphone}").property("tone"))

    def test_what_stops_dictation_is_marked_as_needing_you(self) -> None:
        report = recorded_report()
        report["microphone"].update(device_present=False, available=False, detail="No microphone input device was found.")
        self.build(FakeHealthModel(result_for(report)))
        self.open_health()
        self.wait_for(lambda: self.has_child("health-row-0"), message="the rows")

        index = self.row_index("Microphone")
        self.assertEqual("crimson", self.child(f"health-row-{index}").property("tone"))
        self.wait_for(lambda: self.text("health-headline") == "Something needs you before dictation works", message="heading")

    def test_every_value_can_be_shown_beside_a_row(self) -> None:
        self.build()
        self.open_health()
        self.wait_for(lambda: self.rows_shown() > 0, message="the rows")
        index = self.row_index("Voices")
        button = self.child(f"row-all-{index}")
        self.assertFalse(self.child(f"row-values-{index}").property("visible"))

        button.click()
        self.settle()

        self.assertTrue(self.child(f"row-values-{index}").property("visible"))
        self.assertEqual("Hide values", button.property("text"))

    def test_a_run_that_failed_shows_what_it_said_and_cannot_be_copied(self) -> None:
        said = "murmly doctor did not finish within 120 seconds.\n<detail> & more"
        self.build(FakeHealthModel(health.DoctorResult(error=said)))
        self.open_health()

        self.wait_for(lambda: self.child("health-error-card").property("visible"), message="the error")

        self.assertEqual(said, self.text("health-error"))
        self.assertFalse(self.child("copy-report").property("enabled"))
        self.assertFalse(self.has_child("health-row-0"))

    def test_no_tray_found_is_listed_once_the_tray_code_says_so(self) -> None:
        self.build()
        self.open_health()
        expected = len(health.build_rows(recorded_report()))
        self.wait_for(lambda: self.rows_shown() == expected, message="the rows")

        self.health.set_tray_found(False)
        self.wait_for(lambda: self.rows_shown() == expected + 1, message="the tray row")

        self.assertEqual("System tray", self.text(f"row-title-{expected}"))
        self.assertEqual(
            "No system tray was found, so murmly runs from its window only.",
            self.text(f"row-sentence-{expected}"),
        )
        self.assertEqual("tray.found: false", self.text(f"row-raw-{expected}"))


class CopyTests(HealthQtCase):
    def test_copy_hands_over_doctors_output_exactly(self) -> None:
        copied: list[str] = []
        self.build(copy_text=copied.append)
        self.open_health()
        self.wait_for(lambda: self.child("copy-report").property("enabled"), message="a report")

        self.child("copy-report").click()
        self.settle()

        self.assertEqual([recorded_text()], copied)

    def test_the_copied_report_leaves_out_the_apps_own_tray_entry(self) -> None:
        copied: list[str] = []
        self.build(copy_text=copied.append)
        self.health.set_tray_found(False)
        self.open_health()
        self.wait_for(lambda: self.child("copy-report").property("enabled"), message="a report")

        self.child("copy-report").click()
        self.settle()

        self.assertEqual([recorded_text()], copied)
        self.assertNotIn("tray", copied[0])

    def test_copy_writes_the_clipboard(self) -> None:
        from PySide6.QtGui import QGuiApplication

        self.build()
        self.open_health()
        self.wait_for(lambda: self.child("copy-report").property("enabled"), message="a report")
        clipboard = QGuiApplication.clipboard()
        clipboard.setText("something else")

        self.child("copy-report").click()
        self.settle()

        if clipboard.text() == "" and QGuiApplication.platformName() == "wayland" and not self.window.isActive():
            # Wayland lets only the focused client set the clipboard.
            self.skipTest("Wayland ignores clipboard writes from a window that is not active.")
        self.assertEqual(recorded_text(), clipboard.text())


if __name__ == "__main__":
    unittest.main()
