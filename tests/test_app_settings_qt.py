"""Settings through the real Qt layer: the window, the controls, the controller and
its worker thread, against a temporary configuration file and a fake service."""

from __future__ import annotations

import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path

from home_fakes import FakeChannel
from murmly.app import home, settings
from murmly.installer import InstallError
from murmly.settings_apply import RestartWhenIdle
from murmly.settings_writer import SettingsWriter

from qt_app import MessageLog, qt_application, read_text_retrying

ORIGINAL = """\
# my own notes
[stt]
model_profile = "balanced"
auto_transcribe = "stop"

[tts]
enabled = true
quiet_hours = "22:00-07:00"
"""


WITH_UNLOAD = ORIGINAL.replace('model_profile = "balanced"', 'model_profile = "balanced"\nunload_after_idle_s = 300')


class RecordingWriter(SettingsWriter):
    """Remembers which threads read and wrote the file, and how often."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.threads: set[int] = set()
        self.reads = 0
        self.writes = 0

    def read(self):
        self.reads += 1
        self.threads.add(threading.get_ident())
        return super().read()

    def set(self, key, value):
        self.writes += 1
        self.threads.add(threading.get_ident())
        return super().set(key, value)


class RecordingModel(settings.SettingsModel):
    def __init__(self, writer: SettingsWriter) -> None:
        super().__init__(writer)
        self.polls = 0

    def poll(self):
        self.polls += 1
        return super().poll()


class FakeService:
    """The daemon's `status` and the service's `restart`, as the restart helper calls them."""

    def __init__(self) -> None:
        self.state = "RECORDING"
        self.restarts = 0
        self.statuses = 0
        self.threads: set[int] = set()
        self.failure: Exception | None = None

    def status(self) -> dict:
        self.statuses += 1
        self.threads.add(threading.get_ident())
        return {"ok": True, "state": self.state, "restart_ready": self.state == "IDLE"}

    def restart(self) -> bool:
        self.threads.add(threading.get_ident())
        if self.failure is not None:
            raise self.failure
        self.restarts += 1
        return True


class SettingsQtCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = qt_application()

    def build(self, text: str | bytes = ORIGINAL) -> None:
        from murmly.app import controller, qt

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "murmly" / "config.toml"
        self.path.parent.mkdir()
        if isinstance(text, bytes):
            self.path.write_bytes(text)
        else:
            self.path.write_text(text, encoding="utf-8")
        self.log = MessageLog()
        self.log.__enter__()
        self.addCleanup(self.log.__exit__)
        # Runs first on the way out: Qt's own warnings (a QML error, a binding loop) fail the test.
        self.addCleanup(lambda: self.assertEqual([], self.log.messages))
        self.assertEqual([], qt.prepare_application())
        self.writer = RecordingWriter(self.path)
        self.model = RecordingModel(self.writer)
        self.service = FakeService()
        self.settings = controller.SettingsController(
            self.model,
            lambda: RestartWhenIdle(status=self.service.status, restart=self.service.restart),
        )
        self.home = controller.HomeController(home.HomeModel(FakeChannel()))
        self.health = controller.HealthController(controller.default_health_model())
        self.engine, self.theme = qt.create_engine(self.home, self.health, self.settings)
        self.addCleanup(self._close)
        self.window = self.engine.rootObjects()[0]
        self.settle()

    def _close(self) -> None:
        self.home.shutdown()
        self.health.shutdown()
        self.settings.shutdown()
        self.window.close()
        self.engine.deleteLater()
        self.settle()

    def settle(self) -> None:
        self.application.processEvents()

    def wait_for(self, predicate, timeout: float = 6.0, message: str = "condition") -> None:
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            self.settle()
            if predicate():
                return
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

    def open_settings(self) -> None:
        self.child("nav-settings").click()
        self.wait_for(lambda: self.settings.view.get("loaded") is True, message="the file to be read")

    def data(self) -> dict:
        return tomllib.loads(read_text_retrying(self.path))

    def text(self, name: str) -> str:
        return self.child(name).property("text")

    def show_speech(self) -> None:
        self.child("settings-go-speech").click()
        self.settle()

    def emit(self, item, signal: str) -> None:
        from PySide6.QtCore import QMetaObject

        self.assertTrue(QMetaObject.invokeMethod(item, signal), signal)


class OpeningTests(SettingsQtCase):
    def test_the_file_is_not_read_until_settings_is_opened_and_then_off_the_gui_thread(self) -> None:
        self.build()
        time.sleep(0.3)
        self.settle()
        self.assertEqual(0, self.writer.reads)

        self.open_settings()

        self.assertGreaterEqual(self.writer.reads, 1)
        self.assertNotIn(threading.get_ident(), self.writer.threads)

    def test_controls_show_what_the_file_says_and_the_footer_names_the_file(self) -> None:
        self.build()
        self.open_settings()

        self.assertTrue(self.child("set-profile-balanced").property("selected"))
        self.assertFalse(self.child("set-profile-fast").property("selected"))
        self.assertTrue(self.child("set-stt-device-auto").property("selected"))
        self.assertTrue(self.child("set-finish-stop").property("selected"))
        self.assertFalse(self.child("set-live").property("on"))
        self.assertTrue(self.child("set-verify").property("on"))
        self.assertEqual("13 px", self.text("set-text-size-value"))
        self.assertEqual(f"saved to {self.path}", self.text("settings-footer"))
        self.assertFalse(self.has_child("settings-message") and self.child("settings-message").property("visible"))

    def test_leaving_settings_stops_looking_for_hand_edits(self) -> None:
        self.build()
        self.open_settings()
        self.wait_for(lambda: self.model.polls >= 1, message="a look at the file")

        self.child("nav-home").click()
        self.settle()
        time.sleep(0.2)
        polled = self.model.polls
        time.sleep(1.3)
        self.settle()

        self.assertEqual(polled, self.model.polls)


class EveryControlTests(SettingsQtCase):
    # (a page the control is on, the control, what to do to it, key, value written)
    CLICKS = [
        ("dictation", "set-profile-fast", "stt.model_profile", "fast"),
        ("dictation", "set-stt-device-cuda", "stt.device", "cuda"),
        ("dictation", "set-unload", "stt.unload_after_idle_s", 0),
        ("dictation", "set-finish-continuous", "stt.auto_transcribe", "continuous"),
        ("dictation", "set-live", "stt.live_transcribe", True),
        ("dictation", "set-verify", "clipboard.verify_target", False),
        ("dictation", "set-restore", "clipboard.restore", False),
        ("dictation", "set-overlay", "overlay.enabled", False),
        ("dictation", "set-reduced", "overlay.reduced_motion", True),
        ("dictation", "set-text-size-more", "overlay.text_size_px", 14),
        ("dictation", "set-text-size-less", "overlay.text_size_px", 12),
        ("speech", "set-speak", "tts.enabled", False),
        ("speech", "set-voice-bf_emma", "tts.voice", "bf_emma"),
        ("speech", "set-quiet", "tts.quiet_hours", ""),
        ("speech", "set-tts-device-cuda", "tts.device", "cuda"),
    ]

    def check_click(self, page: str, name: str, key: str, expected: object) -> None:
        self.build(WITH_UNLOAD)
        self.open_settings()
        if page == "speech":
            self.show_speech()
        writes = self.writer.writes

        self.child(name).click()
        self.wait_for(lambda: self.writer.writes == writes + 1, message="the write")
        self.wait_for(lambda: self.settings.view.get("status", "") != "", message="the save to be reported")

        table, _, field = key.partition(".")
        self.assertEqual(expected, self.data()[table][field])
        self.assertIn("# my own notes", self.path.read_text(encoding="utf-8"))
        self.assertNotIn(threading.get_ident(), self.writer.threads)

    def test_a_slider_writes_where_it_is_let_go_and_a_time_field_when_it_is_left(self) -> None:
        self.build()
        self.open_settings()
        slider = self.child("set-pause")

        slider.setProperty("value", 750.0)
        self.emit(slider, "moved")
        self.wait_for(lambda: self.data()["stt"].get("auto_transcribe_silence_ms") == 750, message="the pause")

        self.show_speech()
        field = self.child("set-quiet-from")
        field.setProperty("text", "21:30")
        self.emit(field, "editingFinished")
        self.wait_for(lambda: self.data()["tts"]["quiet_hours"] == "21:30-07:00", message="the window")

        rate = self.child("set-rate")
        rate.setProperty("value", 125.0)
        self.emit(rate, "moved")
        self.wait_for(lambda: self.data()["tts"].get("rate") == 125, message="the rate")

    def test_a_slider_writes_once_however_far_it_moved(self) -> None:
        self.build()
        self.open_settings()
        slider = self.child("set-pause")
        writes = self.writer.writes

        for value in (500.0, 1000.0, 1500.0, 2500.0):
            slider.setProperty("value", value)
            self.emit(slider, "moved")
            self.settle()
        self.wait_for(lambda: self.writer.writes > writes, message="the write")
        time.sleep(0.4)
        self.settle()

        self.assertEqual(writes + 1, self.writer.writes)
        self.assertEqual(2500, self.data()["stt"]["auto_transcribe_silence_ms"])

    def test_the_text_size_stops_at_its_limits(self) -> None:
        self.build(ORIGINAL + "\n[overlay]\ntext_size_px = 48\n")
        self.open_settings()

        self.assertFalse(self.child("set-text-size-more").property("enabled"))
        self.assertTrue(self.child("set-text-size-less").property("enabled"))


def _add_click_tests() -> None:
    for page, name, key, expected in EveryControlTests.CLICKS:
        def test(self, page=page, name=name, key=key, expected=expected) -> None:
            self.check_click(page, name, key, expected)

        test.__name__ = "test_clicking_" + name.replace("-", "_") + "_writes_" + key.replace(".", "_")
        setattr(EveryControlTests, test.__name__, test)


_add_click_tests()


class RefusedValueTests(SettingsQtCase):
    def test_a_refused_value_shows_the_range_writes_nothing_and_the_control_returns(self) -> None:
        self.build()
        self.open_settings()
        slider = self.child("set-pause")
        before = self.path.read_bytes()
        slider.setProperty("value", 999.0)

        self.settings.change("pause", 99)
        self.wait_for(lambda: self.settings.view.get("message", "") != "", message="the refusal")

        self.assertIn("250 to 30000", self.text("settings-message-text"))
        self.assertEqual(before, self.path.read_bytes())
        self.wait_for(lambda: slider.property("value") == 2000.0, message="the slider to return")

    def test_a_time_the_file_would_not_honour_is_refused_and_the_field_returns(self) -> None:
        self.build()
        self.open_settings()
        self.show_speech()
        field = self.child("set-quiet-from")
        before = self.path.read_bytes()

        field.setProperty("text", "25:99")
        self.emit(field, "editingFinished")
        self.wait_for(lambda: self.settings.view.get("message", "") != "", message="the refusal")

        self.assertIn("HH:MM-HH:MM", self.text("settings-message-text"))
        self.assertEqual(before, self.path.read_bytes())
        self.wait_for(lambda: field.property("text") == "22:00", message="the field to return")

    def test_a_refused_value_asks_for_no_restart(self) -> None:
        self.build()
        self.open_settings()

        self.settings.change("rate", 5000)
        self.wait_for(lambda: self.settings.view.get("message", "") != "", message="the refusal")
        time.sleep(1.2)
        self.settle()

        self.assertEqual(0, self.service.statuses)
        self.assertEqual("", self.settings.view["status"])


class QuickClickTests(SettingsQtCase):
    """A second click made before the first save has come back builds on the first."""

    def test_two_quick_clicks_on_a_switch_leave_it_where_it_started(self) -> None:
        self.build()
        self.open_settings()

        self.child("set-live").click()
        self.child("set-live").click()
        self.wait_for(lambda: self.writer.writes == 2, message="both writes")
        time.sleep(0.2)
        self.settle()

        self.assertIsNot(True, self.data()["stt"].get("live_transcribe"))
        self.assertFalse(self.child("set-live").property("on"))

    def test_the_switch_shows_the_first_click_before_its_save_has_come_back(self) -> None:
        self.build()
        self.open_settings()

        self.child("set-live").click()

        self.assertTrue(self.child("set-live").property("on"))

    def test_two_quick_clicks_on_smaller_text_take_two_steps(self) -> None:
        self.build()
        self.open_settings()

        self.child("set-text-size-less").click()
        self.child("set-text-size-less").click()
        self.assertEqual("11 px", self.text("set-text-size-value"))
        self.wait_for(lambda: self.writer.writes == 2, message="both writes")
        time.sleep(0.2)
        self.settle()

        self.assertEqual(11, self.data()["overlay"]["text_size_px"])
        self.assertEqual("11 px", self.text("set-text-size-value"))

    def test_a_refused_save_puts_the_control_back(self) -> None:
        from murmly.config import SettingValueError

        self.build()
        self.open_settings()

        def refuse(key, value):
            raise SettingValueError("stt.live_transcribe must be true or false.")

        self.writer.set = refuse
        self.child("set-live").click()
        self.assertTrue(self.child("set-live").property("on"))
        self.wait_for(lambda: self.settings.view.get("message", "") != "", message="the refusal")

        self.assertFalse(self.child("set-live").property("on"))
        self.assertIs(False, self.settings.view["values"]["live"])

    def test_the_first_of_two_saves_coming_back_does_not_flicker_the_control(self) -> None:
        self.build()
        self.open_settings()
        shown: list[bool] = []
        self.settings.viewChanged.connect(lambda: shown.append(self.settings.view["values"]["live"]))

        self.child("set-live").click()
        self.child("set-live").click()
        self.wait_for(lambda: self.writer.writes == 2, message="both writes")
        time.sleep(0.2)
        self.settle()

        self.assertEqual([True], shown[:1])
        self.assertNotIn(True, shown[1:])


class TimeFieldTypingTests(SettingsQtCase):
    def activate(self) -> None:
        self.window.requestActivate()
        started = time.monotonic()
        while time.monotonic() - started < 2.0 and not self.window.isActive():
            self.settle()
            time.sleep(0.01)
        if not self.window.isActive():
            self.skipTest("this display does not give a test window the focus")

    def type_into(self, field, text: str) -> None:
        from PySide6.QtCore import QMetaObject, Qt
        from PySide6.QtTest import QTest

        self.assertTrue(QMetaObject.invokeMethod(field, "forceActiveFocus"))
        self.settle()
        field.setProperty("text", "")
        for letter in text:
            key = Qt.Key.Key_Colon if letter == ":" else Qt.Key(ord(letter))
            QTest.keyClick(self.window, key, Qt.KeyboardModifier.NoModifier)
        self.settle()

    def test_a_time_being_typed_survives_while_a_restart_waits(self) -> None:
        self.build()
        self.open_settings()
        self.activate()
        self.child("set-live").click()
        self.wait_for(lambda: "will apply when Murmly is idle" in self.text("settings-status"), message="waiting")
        self.show_speech()
        field = self.child("set-quiet-from")

        self.type_into(field, "21:3")
        polled = self.service.statuses
        self.wait_for(lambda: self.service.statuses >= polled + 3, message="three ticks of the restart")

        self.assertTrue(field.property("activeFocus"))
        self.assertEqual("21:3", field.property("text"))

    def test_with_the_focus_gone_a_change_from_outside_shows(self) -> None:
        self.build()
        self.open_settings()
        self.activate()
        self.show_speech()
        field = self.child("set-quiet-from")

        self.type_into(field, "21:3")
        self.emit(self.child("set-speak"), "forceActiveFocus")
        self.wait_for(lambda: field.property("text") == "22:00", message="the refused time to go")
        self.path.write_text(ORIGINAL.replace("22:00-07:00", "20:00-07:00"), encoding="utf-8")

        self.wait_for(lambda: field.property("text") == "20:00", message="the edit to show")


class UnreadableFileTests(SettingsQtCase):
    BROKEN = b'[stt\nmodel_profile = "fast"\n'

    def parser_says(self) -> str:
        try:
            tomllib.loads(self.BROKEN.decode())
        except tomllib.TOMLDecodeError as error:
            return str(error)
        self.fail("the file parsed")

    def test_a_file_that_does_not_parse_shows_the_error_in_mono_and_disables_every_control(self) -> None:
        self.build(self.BROKEN)
        self.open_settings()

        self.assertEqual(self.parser_says(), self.text("settings-error-text"))
        self.assertEqual("monoMeta", self.child("settings-error-text").property("role"))
        for name in ("set-profile-fast", "set-stt-device-cpu", "set-live", "set-unload", "set-text-size-more"):
            with self.subTest(name):
                self.assertFalse(self.child(name).property("enabled"))

    def test_clicking_a_disabled_control_writes_nothing(self) -> None:
        self.build(self.BROKEN)
        self.open_settings()

        self.child("set-live").click()
        self.child("set-profile-fast").click()
        time.sleep(0.3)
        self.settle()

        self.assertEqual(0, self.writer.writes)
        self.assertEqual(self.BROKEN, self.path.read_bytes())

    def test_the_controls_return_once_the_file_parses(self) -> None:
        self.build(self.BROKEN)
        self.open_settings()

        self.path.write_text(ORIGINAL, encoding="utf-8")

        self.wait_for(lambda: self.child("set-live").property("enabled"), message="the controls")
        self.assertTrue(self.child("set-profile-balanced").property("selected"))

    def test_a_change_that_still_reaches_a_file_that_does_not_parse_leaves_it_byte_for_byte(self) -> None:
        self.build(ORIGINAL)
        self.open_settings()
        self.path.write_bytes(self.BROKEN)

        self.settings.change("live", True)
        self.wait_for(lambda: self.settings.view.get("error", "") != "", message="the error")

        self.assertEqual(self.BROKEN, self.path.read_bytes())
        self.assertEqual(self.parser_says(), self.text("settings-error-text"))


class HandEditTests(SettingsQtCase):
    def test_a_hand_edit_is_noticed_and_read_again(self) -> None:
        self.build()
        self.open_settings()

        self.path.write_text(ORIGINAL.replace("balanced", "accurate"), encoding="utf-8")

        self.wait_for(lambda: self.child("set-profile-accurate").property("selected"), message="the new value")
        self.assertEqual(settings.NOTICE_FILE_CHANGED, self.text("settings-notice-text"))

    def test_a_change_after_a_hand_edit_keeps_the_edit_and_says_so(self) -> None:
        self.build()
        self.open_settings()
        self.path.write_text(ORIGINAL + "\n[overlay]\nbottom_margin_px = 99\n", encoding="utf-8")

        self.settings.change("live", True)
        self.wait_for(lambda: self.data().get("stt", {}).get("live_transcribe") is True, message="the write")

        self.assertEqual(99, self.data()["overlay"]["bottom_margin_px"])


class RestartTests(SettingsQtCase):
    def test_a_burst_of_saves_while_busy_waits_and_then_restarts_once(self) -> None:
        self.build()
        self.open_settings()

        for name in ("set-live", "set-reduced", "set-overlay"):
            self.child(name).click()
            self.wait_for(lambda: self.writer.writes == ("set-live", "set-reduced", "set-overlay").index(name) + 1)
        self.wait_for(lambda: "will apply when Murmly is idle" in self.text("settings-status"), message="waiting")
        self.wait_for(lambda: self.service.statuses >= 2, message="the daemon to be asked")

        self.assertEqual(0, self.service.restarts)

        self.service.state = "IDLE"
        self.wait_for(lambda: self.service.restarts == 1, message="the restart")
        self.wait_for(lambda: "restarted" in self.text("settings-status"), message="the outcome")
        time.sleep(1.2)
        self.settle()

        self.assertEqual(1, self.service.restarts)
        self.assertNotIn("will apply", self.text("settings-status"))
        written = self.data()
        self.assertTrue(written["stt"]["live_transcribe"])
        self.assertTrue(written["overlay"]["reduced_motion"])
        self.assertFalse(written["overlay"]["enabled"])

    def test_a_save_while_idle_restarts_at_once(self) -> None:
        self.build()
        self.service.state = "IDLE"
        self.open_settings()

        self.child("set-live").click()

        self.wait_for(lambda: self.service.restarts == 1, message="the restart")
        self.wait_for(lambda: "restarted" in self.text("settings-status"), message="the outcome")

    def test_nothing_the_restart_needs_runs_on_the_gui_thread(self) -> None:
        self.build()
        self.service.state = "IDLE"
        self.open_settings()

        self.child("set-live").click()
        self.wait_for(lambda: self.service.restarts == 1, message="the restart")

        self.assertNotIn(threading.get_ident(), self.service.threads)
        self.assertNotIn(threading.get_ident(), self.writer.threads)

    def test_a_restart_that_fails_says_why(self) -> None:
        self.build()
        self.service.state = "IDLE"
        self.service.failure = InstallError("systemctl --user restart failed: no such unit")
        self.open_settings()

        self.child("set-live").click()

        self.wait_for(lambda: "could not restart" in self.text("settings-status"), message="the failure")
        self.assertIn("no such unit", self.text("settings-status"))
        self.assertTrue(self.data()["stt"]["live_transcribe"])

    def test_closing_the_app_does_not_wait_for_a_restart_that_is_pending(self) -> None:
        self.build()
        self.open_settings()
        self.child("set-live").click()
        self.wait_for(lambda: "will apply" in self.text("settings-status"), message="waiting")

        started = time.monotonic()
        self.settings.shutdown()

        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(0, self.service.restarts)


if __name__ == "__main__":
    unittest.main()
