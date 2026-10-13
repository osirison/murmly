"""The tray through the real Qt layer: the icon, the panel window, the controllers
and their worker threads, against a temporary configuration file and a fake daemon."""

from __future__ import annotations

import tempfile
import time
import tomllib
import unittest
from pathlib import Path

from health_fakes import FakeHealthModel
from home_fakes import FakeChannel, delivered, refused
from murmly.app import home, settings
from murmly.settings_apply import RestartWhenIdle
from murmly.settings_writer import SettingsWriter
from test_app_settings_qt import FakeService

from qt_app import MessageLog, qt_application, read_text_retrying

CONFIG = """\
# my own notes
[stt]
auto_transcribe = "off"
auto_transcribe_silence_ms = 2000

[tts]
enabled = false
quiet_hours = ""
"""

NO_TRAY_SENTENCE = "No system tray was found, so murmly runs from its window only."


class TrayQtCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = qt_application()

    def build(self, available: bool = True) -> None:
        from murmly.app import controller, qt, tray_qt

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "config.toml"
        self.path.write_text(CONFIG, encoding="utf-8")
        self.log = MessageLog()
        self.log.__enter__()
        self.addCleanup(self.log.__exit__)
        # Runs first on the way out: Qt's own warnings (a QML error, a binding loop) fail the test.
        self.addCleanup(lambda: self.assertEqual([], self.log.messages))
        self.assertEqual([], qt.prepare_application())
        self.channel = FakeChannel()
        self.service = FakeService()
        self.copied: list[str] = []
        self.home = controller.HomeController(home.HomeModel(self.channel), copy_text=self.copied.append)
        self.health = controller.HealthController(FakeHealthModel())
        self.settings = controller.SettingsController(
            settings.SettingsModel(SettingsWriter(self.path)),
            lambda: RestartWhenIdle(status=self.service.status, restart=self.service.restart),
        )
        self.available = available
        self.tray = tray_qt.create_tray(
            self.home, self.settings, self.health, self.application, available=lambda: available
        )
        self.engine, self.theme = qt.create_engine(self.home, self.health, self.settings, tray=self.tray)
        self.addCleanup(self._close)
        roots = self.engine.rootObjects()
        self.window = roots[0]
        self.panel = roots[1] if len(roots) > 1 else None
        self.settle()

    def _close(self) -> None:
        self.home.shutdown()
        self.health.shutdown()
        self.settings.shutdown()
        if self.tray is not None:
            self.tray.shutdown()
        self.window.close()
        if self.panel is not None:
            self.panel.close()
        self.engine.deleteLater()
        self.settle()
        # The application outlives the test; give it back its default.
        self.application.setQuitOnLastWindowClosed(True)

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

    def pause(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.settle()
            time.sleep(0.02)

    def child(self, name: str, root=None):
        pending = [(root or self.panel).contentItem()]
        while pending:
            item = pending.pop()
            if item.objectName() == name:
                return item
            pending.extend(item.childItems())
        self.fail(f"no item named {name}")

    def text(self, name: str) -> str:
        return self.child(name).property("text")

    def data(self) -> dict:
        return tomllib.loads(read_text_retrying(self.path))

    def click_icon(self) -> None:
        from PySide6.QtWidgets import QSystemTrayIcon

        self.tray.icon.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
        self.settle()

    def open_panel(self) -> None:
        self.click_icon()
        self.wait_for(lambda: bool(self.panel.property("visible")), message="the panel")
        self.wait_for(lambda: self.tray.view.get("loaded") is True, message="the file to be read")

    def health_rows(self) -> list[dict]:
        self.health.check()
        self.wait_for(lambda: self.health.view.get("state") == "ready", message="Health's rows")
        return list(self.health.view["rows"])


class NoTrayTests(TrayQtCase):
    def test_with_no_tray_the_app_starts_has_no_icon_and_health_says_so(self) -> None:
        self.build(available=False)

        self.assertIsNone(self.tray)
        self.assertIsNone(self.panel)
        self.assertTrue(self.window.property("visible"))
        rows = {row["title"]: row for row in self.health_rows()}
        self.assertEqual(NO_TRAY_SENTENCE, rows["System tray"]["sentence"])
        self.assertEqual("tray.found: false", rows["System tray"]["raw"])

    def test_with_no_tray_closing_the_window_quits_the_app(self) -> None:
        self.build(available=False)
        self.assertTrue(self.application.quitOnLastWindowClosed())


class TrayTests(TrayQtCase):
    def test_with_a_tray_there_is_an_icon_and_health_does_not_say_none_was_found(self) -> None:
        self.build()

        self.assertIsNotNone(self.tray)
        self.assertTrue(self.tray.icon.isVisible())
        self.assertIsNotNone(self.panel)
        self.assertFalse(self.panel.property("visible"))
        rows = self.health_rows()
        self.assertNotIn(NO_TRAY_SENTENCE, [row["sentence"] for row in rows])
        self.assertEqual("tray.found: true", {row["title"]: row for row in rows}["System tray"]["raw"])

    def test_with_a_tray_closing_the_window_keeps_the_app_in_the_tray(self) -> None:
        self.build()
        self.assertFalse(self.application.quitOnLastWindowClosed())

        self.window.close()
        self.settle()
        self.assertFalse(self.window.property("visible"))
        self.assertTrue(self.tray.icon.isVisible())

        self.tray.openWindow("health")
        self.settle()
        self.assertTrue(self.window.property("visible"))
        self.assertEqual(2, self.window.property("current"))

    def test_the_panel_stays_hidden_until_the_icon_is_clicked_and_a_second_click_hides_it(self) -> None:
        self.build()
        self.channel.state = "LISTENING"
        self.pause(0.6)
        self.assertFalse(self.panel.property("visible"))

        self.click_icon()
        self.assertTrue(self.panel.property("visible"))
        self.click_icon()
        self.assertFalse(self.panel.property("visible"))

    def test_the_click_that_took_the_focus_away_does_not_show_the_panel_again(self) -> None:
        self.build()
        self.open_panel()

        self.tray.panelLostFocus()
        self.assertFalse(self.panel.property("visible"))
        self.click_icon()
        self.assertFalse(self.panel.property("visible"))
        self.click_icon()
        self.assertTrue(self.panel.property("visible"))

    def test_the_panel_is_placed_on_the_screen(self) -> None:
        from PySide6.QtGui import QGuiApplication

        self.build()
        self.open_panel()
        area = QGuiApplication.primaryScreen().availableGeometry()
        self.assertGreaterEqual(self.panel.property("x"), area.x())
        self.assertGreaterEqual(self.panel.property("y"), area.y())
        self.assertLessEqual(self.panel.property("x") + self.panel.property("width"), area.right() + 1)

    def test_quit_ends_the_app_and_leaves_the_daemon_alone(self) -> None:
        from unittest.mock import patch

        self.build()
        self.open_panel()
        requests = self.channel.count("clear_last_words")
        with patch.object(self.application, "quit") as quit_:
            self.child("tray-quit").click()
            self.settle()
        quit_.assert_called_once_with()
        self.assertFalse(self.panel.property("visible"))
        self.assertEqual(requests, self.channel.count("clear_last_words"))
        self.assertNotIn("stop", [command for _, command in self.channel.requests])

    def test_open_murmly_shows_the_window_and_hides_the_panel(self) -> None:
        self.build()
        self.window.hide()
        self.open_panel()

        self.child("tray-open").click()
        self.settle()
        self.assertTrue(self.window.property("visible"))
        self.assertFalse(self.panel.property("visible"))


class PanelContentTests(TrayQtCase):
    def test_it_shows_the_state_the_last_words_and_the_four_switches(self) -> None:
        self.build()
        self.channel.last_words = delivered("Move the design review to Thursday.")
        self.home.setupChanged()  # the last words are read again on the next poll
        self.open_panel()
        self.wait_for(lambda: self.text("tray-last-text") != "", message="the last words")

        self.assertEqual("ready", self.child("tray-state-chip").property("text"))
        self.assertEqual("Ready when you are", self.text("tray-headline"))
        self.assertEqual("Move the design review to Thursday.", self.text("tray-last-text"))
        self.assertEqual("typed", self.text("tray-last-outcome"))
        for switch in ("live", "finish", "announce", "quiet"):
            with self.subTest(switch=switch):
                self.assertTrue(self.child(f"tray-switch-{switch}").property("visible"))
                self.assertFalse(self.child(f"tray-switch-{switch}").property("on"))
        self.assertEqual("press the keys again", self.text("tray-note-finish"))

    def test_the_state_follows_the_daemon_while_the_panel_is_open(self) -> None:
        self.build()
        self.open_panel()
        self.channel.state = "LISTENING"
        self.wait_for(lambda: self.child("tray-state-chip").property("text") == "listening", message="listening")
        self.channel.state = "IDLE"
        self.channel.last_words = refused()
        self.wait_for(lambda: self.child("tray-state-chip").property("text") == "1 waiting on you", message="needs you")
        self.assertEqual("Your words are safe on the clipboard", self.text("tray-headline"))
        self.assertIn("not typed", self.text("tray-last-outcome"))

    def test_copy_puts_the_last_words_on_the_clipboard_through_home(self) -> None:
        self.build()
        self.channel.last_words = delivered("Say it again.")
        self.home.setupChanged()  # the last words are read again on the next poll
        self.open_panel()
        self.wait_for(lambda: self.text("tray-last-text") != "", message="the last words")

        self.child("tray-copy-last").click()
        self.settle()
        self.assertEqual(["Say it again."], self.copied)


class SwitchTests(TrayQtCase):
    def toggled(self, switch: str, key: str, expected: object) -> None:
        # What the panel shows once the change is saved and the file read again.
        # Waiting on that, not on reading the file every few milliseconds from this
        # thread, keeps the test from holding the file open while the worker swaps
        # a new one in (Windows refuses to replace a file that is open).
        on = expected not in (False, "", "off")
        self.child(f"tray-switch-{switch}").click()
        self.wait_for(
            lambda: self.child(f"tray-switch-{switch}").property("on") is on,
            message=f"{key} to be written ({self.child('tray-message').property('text')!r})",
        )
        self.assertEqual(expected, self.lookup(key))

    def lookup(self, key: str) -> object:
        table, _, name = key.partition(".")
        return self.data().get(table, {}).get(name)

    def test_toggling_each_switch_writes_its_key(self) -> None:
        self.build()
        self.open_panel()

        self.toggled("live", "stt.live_transcribe", True)
        self.toggled("finish", "stt.auto_transcribe", "stop")
        self.toggled("announce", "tts.enabled", True)
        self.toggled("quiet", "tts.quiet_hours", settings.DEFAULT_QUIET_WINDOW)

        for switch in ("live", "finish", "announce", "quiet"):
            self.wait_for(lambda switch=switch: self.child(f"tray-switch-{switch}").property("on") is True, message=switch)

        self.toggled("live", "stt.live_transcribe", False)
        self.toggled("finish", "stt.auto_transcribe", "off")
        self.toggled("announce", "tts.enabled", False)
        self.toggled("quiet", "tts.quiet_hours", "")

    def test_off_then_on_restores_the_persons_quiet_window_and_pause_mode(self) -> None:
        self.build()
        self.path.write_text(
            '[stt]\nauto_transcribe = "continuous"\n\n[tts]\nquiet_hours = "23:30-06:00"\n', encoding="utf-8"
        )
        self.open_panel()
        self.wait_for(lambda: self.child("tray-switch-quiet").property("on") is True, message="quiet on")

        self.toggled("quiet", "tts.quiet_hours", "")
        self.toggled("finish", "stt.auto_transcribe", "off")
        self.toggled("quiet", "tts.quiet_hours", "23:30-06:00")
        self.toggled("finish", "stt.auto_transcribe", "continuous")

    def test_a_switch_keeps_the_rest_of_the_file_and_the_page_sees_it(self) -> None:
        self.build()
        self.open_panel()
        self.toggled("live", "stt.live_transcribe", True)

        self.assertIn("# my own notes", self.path.read_text(encoding="utf-8"))
        self.wait_for(lambda: self.settings.view["values"]["live"] is True, message="Settings to see it")

    def test_a_change_made_while_the_daemon_is_busy_waits_and_the_panel_says_so(self) -> None:
        self.build()
        self.open_panel()
        self.service.state = "RECORDING"
        self.toggled("live", "stt.live_transcribe", True)

        self.wait_for(lambda: "will apply when Murmly is idle" in self.text("tray-message"), message="the waiting line")
        self.assertEqual(0, self.service.restarts)
        self.service.state = "IDLE"
        self.wait_for(lambda: self.service.restarts == 1, message="the restart once idle")

    def two_quick_clicks(self, switch: str, key: str, off: object) -> None:
        self.build()
        self.open_panel()
        self.child(f"tray-switch-{switch}").click()
        # The panel is given new rows on a change: find the switch again.
        self.child(f"tray-switch-{switch}").click()
        self.pause(0.6)

        self.assertIs(False, self.child(f"tray-switch-{switch}").property("on"))
        self.assertEqual(off, self.lookup(key))

    def test_two_quick_clicks_on_live_leave_it_and_the_file_where_they_started(self) -> None:
        self.two_quick_clicks("live", "stt.live_transcribe", False)

    def test_two_quick_clicks_on_quiet_hours_leave_them_and_the_file_where_they_started(self) -> None:
        self.two_quick_clicks("quiet", "tts.quiet_hours", "")

    def test_two_quick_clicks_on_stop_when_i_pause_leave_it_and_the_file_where_it_started(self) -> None:
        self.two_quick_clicks("finish", "stt.auto_transcribe", "off")

    def test_the_switch_shows_a_click_before_its_save_has_come_back(self) -> None:
        self.build()
        self.open_panel()

        self.child("tray-switch-live").click()

        self.assertIs(True, self.child("tray-switch-live").property("on"))

    def test_a_keyboard_toggle_leaves_the_focus_on_the_same_switch(self) -> None:
        from PySide6.QtCore import QMetaObject, Qt
        from PySide6.QtTest import QTest

        self.build()
        self.open_panel()
        self.panel.requestActivate()
        started = time.monotonic()
        while time.monotonic() - started < 2.0 and not self.panel.isActive():
            self.settle()
            time.sleep(0.01)
        if not self.panel.isActive():
            self.skipTest("this display does not give a test window the focus")
        self.assertTrue(QMetaObject.invokeMethod(self.child("tray-switch-announce"), "forceActiveFocus"))
        self.settle()
        self.assertEqual("tray-switch-announce", self.panel.activeFocusItem().objectName())

        QTest.keyClick(self.panel, Qt.Key.Key_Return)
        self.wait_for(lambda: self.child("tray-switch-announce").property("on") is True, message="the switch")
        self.pause(0.3)

        self.assertEqual("tray-switch-announce", self.panel.activeFocusItem().objectName())

    def test_a_file_that_does_not_parse_disables_the_switches(self) -> None:
        self.build()
        self.path.write_text("[stt\nbroken", encoding="utf-8")
        self.open_panel_without_loaded()
        self.wait_for(lambda: self.settings.view.get("disabled") is True, message="the parse error")
        self.wait_for(lambda: self.tray.view.get("disabled") is True, message="the switches to be disabled")

        self.assertFalse(self.child("tray-switch-live").property("enabled"))
        self.child("tray-switch-live").click()
        self.pause(0.3)
        self.assertEqual("[stt\nbroken", self.path.read_text(encoding="utf-8"))

    def open_panel_without_loaded(self) -> None:
        self.click_icon()
        self.wait_for(lambda: bool(self.panel.property("visible")), message="the panel")

    def test_a_value_the_writer_refuses_is_shown_in_its_own_words(self) -> None:
        self.build()
        self.open_panel()
        refusal = "stt.live_transcribe must be true or false."

        def refuse(key, value):
            from murmly.config import SettingValueError

            raise SettingValueError(refusal)

        self.settings._model.writer.set = refuse  # the writer's own check, forced
        self.child("tray-switch-live").click()
        self.wait_for(lambda: self.text("tray-message") == refusal, message="the refusal")
        self.assertFalse(self.child("tray-switch-live").property("on"))


class PollingTests(TrayQtCase):
    def test_polling_runs_while_the_panel_is_visible_and_stops_when_both_surfaces_are_hidden(self) -> None:
        self.build()
        self.window.hide()
        self.settle()
        self.pause(0.3)  # a request already on its way may still land
        before = len(self.channel.requests)
        self.pause(1.2)
        self.assertEqual(before, len(self.channel.requests), "a status request was sent with nothing visible")

        self.click_icon()
        self.wait_for(lambda: len(self.channel.requests) > before, timeout=1.5, message="polling to start")
        self.wait_for(lambda: self.channel.count("status") >= 3, timeout=4, message="three polls")
        times = [at for at, command in list(self.channel.requests) if command == "status"]
        gaps = [later - earlier for earlier, later in zip(times, times[1:])]
        self.assertGreaterEqual(min(gaps), home.STATUS_INTERVAL_S - 0.02)

        self.click_icon()
        self.assertFalse(self.panel.property("visible"))
        self.pause(0.3)
        after = len(self.channel.requests)
        self.pause(1.2)
        self.assertEqual(after, len(self.channel.requests), "a status request was sent after the panel hid")

    def test_the_window_alone_keeps_polling_after_the_panel_hides(self) -> None:
        self.build()
        self.open_panel()
        self.click_icon()
        self.pause(0.3)
        before = len(self.channel.requests)
        self.wait_for(lambda: len(self.channel.requests) > before, timeout=1.5, message="the window's polling")

    def test_the_settings_file_is_read_when_the_panel_opens(self) -> None:
        self.build()
        self.window.hide()
        self.settle()
        self.assertFalse(self.settings.view.get("loaded"))
        self.open_panel()
        self.assertTrue(self.tray.view["loaded"])


class IconTests(TrayQtCase):
    def test_the_icon_follows_the_state_while_something_is_visible_and_is_neutral_otherwise(self) -> None:
        self.build()
        self.wait_for(lambda: self.tray.icon.toolTip() == "murmly: ready", message="ready")
        ready = self.tray.icon.icon().cacheKey()

        self.channel.state = "LISTENING"
        self.wait_for(lambda: self.tray.icon.toolTip() == "murmly: listening", message="listening")
        listening = self.tray.icon.icon().cacheKey()
        self.assertNotEqual(ready, listening)

        self.channel.state = "THINKING"
        self.wait_for(lambda: self.tray.icon.toolTip() == "murmly: writing", message="writing")
        self.assertNotIn(self.tray.icon.icon().cacheKey(), (ready, listening))

        self.window.hide()
        self.settle()
        self.wait_for(lambda: self.tray.icon.toolTip() == "murmly", message="the neutral icon")
        self.assertEqual("murmly", self.tray.icon.toolTip())

    def test_each_scheme_has_its_own_icon(self) -> None:
        from murmly.app import tray_qt

        dark = tray_qt.render_icon("ready", "dark").pixmap(64, 64).toImage()
        light = tray_qt.render_icon("ready", "light").pixmap(64, 64).toImage()
        self.assertNotEqual(dark, light)
        self.assertFalse(dark.isNull())


class FocusTests(TrayQtCase):
    @staticmethod
    def spy_names() -> tuple[str, ...]:
        return ("requestActivate", "raise_", "lower", "show", "showNormal", "showMaximized",
                "showFullScreen", "showMinimized", "setWindowState", "alert")

    def spied(self, calls: list[str]):
        from unittest.mock import patch

        from PySide6.QtGui import QWindow
        from PySide6.QtQuick import QQuickWindow

        def record(name):
            return lambda *args, **kwargs: calls.append(name)

        patches = []
        for cls in (QWindow, QQuickWindow, type(self.window), type(self.panel)):
            for name in self.spy_names():
                if hasattr(cls, name):
                    patches.append(patch.object(cls, name, record(f"{cls.__name__}.{name}")))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_no_daemon_state_raises_activates_or_focuses_a_window_with_the_panel_open(self) -> None:
        from PySide6.QtGui import QGuiApplication

        self.build()
        self.open_panel()
        self.wait_for(lambda: self.child("tray-state-chip").property("text") == "ready", message="ready")
        # A desktop may activate a window a moment after it is shown (Windows does):
        # what is measured is what the daemon's states change, so let that settle first.
        self.pause(0.5)
        focus_changes: list[object] = []
        QGuiApplication.instance().focusWindowChanged.connect(focus_changes.append)
        focus_before = QGuiApplication.focusWindow()
        window_active = self.window.isActive()
        panel_active = self.panel.isActive()
        item_before = self.panel.activeFocusItem()
        calls: list[str] = []
        self.spied(calls)

        steps = (
            ("LISTENING", None, "listening"),
            ("THINKING", None, "writing"),
            ("SPEAKING", None, "speaking"),
            ("IDLE", delivered(), "ready"),
            ("LISTENING", None, "listening"),
            ("IDLE", refused(), "1 waiting on you"),
        )
        for state, words, chip in steps:
            with self.subTest(state=state, chip=chip):
                self.channel.state = state
                self.channel.last_words = words
                self.wait_for(lambda chip=chip: self.child("tray-state-chip").property("text") == chip, message=chip)
        self.channel.failure = ConnectionRefusedError("gone")
        self.wait_for(lambda: self.child("tray-state-chip").property("text") == "service not running", message="not running")
        self.channel.failure = None
        self.channel.state = "IDLE"
        self.channel.last_words = None
        self.wait_for(lambda: self.child("tray-state-chip").property("text") == "ready", message="ready again")

        self.assertEqual([], calls)
        self.assertEqual(focus_before, QGuiApplication.focusWindow())
        self.assertEqual(window_active, self.window.isActive())
        self.assertEqual(panel_active, self.panel.isActive())
        self.assertEqual(item_before, self.panel.activeFocusItem())
        self.assertEqual([], focus_changes)

    def test_the_spies_do_see_open_murmly_which_a_person_asks_for(self) -> None:
        self.build()
        calls: list[str] = []
        self.spied(calls)

        self.tray.openWindow("")

        self.assertTrue([call for call in calls if call.endswith("requestActivate")], calls)

    def test_opening_the_panel_and_a_state_change_with_it_closed_raise_nothing(self) -> None:
        self.build()
        calls: list[str] = []
        self.spied(calls)

        self.channel.state = "LISTENING"
        self.pause(0.8)
        self.click_icon()
        self.pause(0.3)
        self.click_icon()
        self.pause(0.3)

        self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()
