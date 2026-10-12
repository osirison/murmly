"""First run and the key rows in Settings through the real Qt layer: the window, the
key field, the controllers and their worker thread, against a temporary
configuration file and a fake install path (so no key is ever really bound)."""

from __future__ import annotations

import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path

from home_fakes import FakeChannel
from murmly.app import home, keys
from murmly.installer import HotkeyConflictError
from murmly.settings_apply import RestartWhenIdle
from murmly.settings_writer import SettingsWriter

from qt_app import MessageLog, qt_application
from test_app_keys import META, SHIFT, KEY_SPACE, KEY_X, outcome
from test_app_settings_qt import ORIGINAL, FakeService, RecordingModel, RecordingWriter


class RecordingInstall:
    """The install path, as the keys controller calls it: remembers each call and what
    thread made it, and writes the hotkey record the way a real install does."""

    def __init__(self, record: dict[str, str]) -> None:
        self.record = record
        self.calls: list[tuple[str, str | None]] = []
        self.threads: set[int] = set()
        self.failure: Exception | None = None

    def __call__(self, window, session):
        self.calls.append((window.portable, session.portable if session is not None else None))
        self.threads.add(threading.get_ident())
        if self.failure is not None:
            raise self.failure
        self.record["window"] = window.portable
        if session is not None:
            self.record["session"] = session.portable
        return outcome(True, session is not None)


class KeysQtCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = qt_application()

    def build(self, show_first_run: bool = True, record: dict[str, str] | None = None) -> None:
        from PySide6.QtCore import QObject  # noqa: F401 - Qt must be importable here

        from murmly.app import controller, qt

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "murmly" / "config.toml"
        self.path.parent.mkdir()
        self.path.write_text(ORIGINAL, encoding="utf-8")
        self.log = MessageLog()
        self.log.__enter__()
        self.addCleanup(self.log.__exit__)
        self.addCleanup(lambda: self.assertEqual([], self.log.messages))
        self.assertEqual([], qt.prepare_application())
        self.writer = RecordingWriter(self.path)
        self.model = RecordingModel(self.writer)
        self.service = FakeService()
        self.service.state = "IDLE"
        self.settings = controller.SettingsController(
            self.model,
            lambda: RestartWhenIdle(status=self.service.status, restart=self.service.restart),
        )
        self.record = dict(record or {})
        self.install = RecordingInstall(self.record)
        self.keys = controller.KeysController(
            keys.KeysModel(lambda: dict(self.record), self.install, profile=None), self.settings.executor
        )
        self.first_run = controller.FirstRunController(keys.FirstRun(show=show_first_run))
        self.home = controller.HomeController(home.HomeModel(FakeChannel()))
        self.health = controller.HealthController(controller.default_health_model())
        self.engine, self.theme = qt.create_engine(
            self.home, self.health, self.settings, self.keys, self.first_run
        )
        self.addCleanup(self._close)
        self.window = self.engine.rootObjects()[0]
        self.settle()
        self.wait_for(lambda: self.keys.view.get("loaded") is True, message="the keys to be read")

    def _close(self) -> None:
        self.home.shutdown()
        self.health.shutdown()
        self.settings.shutdown()
        self.keys.shutdown()
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

    def find(self, name: str):
        pending = [self.window.contentItem()]
        while pending:
            item = pending.pop()
            if item.objectName() == name:
                return item
            pending.extend(item.childItems())
        return None

    def child(self, name: str):
        item = self.find(name)
        self.assertIsNotNone(item, f"no item named {name}")
        return item

    def activate(self) -> None:
        """Give the window the focus, as a person clicking into it would."""
        self.window.requestActivate()
        started = time.monotonic()
        while time.monotonic() - started < 2.0 and not self.window.isActive():
            self.settle()
            time.sleep(0.01)
        if not self.window.isActive():
            self.skipTest("this display does not give a test window the focus")

    def press(self, key, modifiers=0) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        QTest.keyClick(self.window, Qt.Key(key), Qt.KeyboardModifier(modifiers))
        self.settle()

    def click(self, item) -> None:
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtTest import QTest

        centre = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre)
        self.settle()

    def invoke(self, item, method: str) -> None:
        from PySide6.QtCore import QMetaObject

        self.assertTrue(QMetaObject.invokeMethod(item, method), method)
        self.settle()

    def data(self) -> dict:
        return tomllib.loads(self.path.read_text(encoding="utf-8"))

    def text(self, name: str) -> str:
        return self.child(name).property("text")

    def recording(self) -> str:
        return self.keys.view["recording"]

    def wait_for_calls(self, count: int) -> None:
        self.wait_for(lambda: len(self.install.calls) >= count, message="the install path to be called")
        self.wait_for(lambda: self.keys.view["busy"] is False, message="the bind to finish")


class FirstRunShowingTests(KeysQtCase):
    def test_it_shows_when_asked_with_its_own_header_in_place_of_the_windows(self) -> None:
        self.build()

        self.assertTrue(self.child("first-run-keys-step").property("visible"))
        self.assertTrue(self.child("first-run-key-field").property("visible"))
        self.assertTrue(self.child("first-run-try").property("visible"))
        self.assertFalse(self.child("nav-home").property("visible"))
        self.assertEqual(["Your keys", "How careful"], [step["title"] for step in self.first_run.view["steps"]])

    def test_it_is_not_there_when_not_asked_and_the_windows_own_header_is(self) -> None:
        self.build(show_first_run=False)

        self.assertIsNone(self.find("first-run-keys-step"))
        self.assertTrue(self.child("nav-home").property("visible"))

    def test_the_try_it_box_is_an_ordinary_text_field_that_takes_typing(self) -> None:
        self.build()
        self.activate()
        box = self.child("first-run-try")

        self.invoke(box, "forceActiveFocus")
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        for letter in "hello":
            QTest.keyClick(self.window, Qt.Key(ord(letter.upper())), Qt.KeyboardModifier.NoModifier)
        self.settle()

        self.assertEqual("hello", box.property("text"))

    def test_the_placeholder_names_the_keys_once_they_are_chosen(self) -> None:
        self.build(record={"window": "Meta+X"})

        self.assertIn("Press Meta + X, say a sentence", self.child("first-run-try").property("placeholderText"))


class RecordingKeysTests(KeysQtCase):
    def start_recording(self) -> None:
        self.activate()
        self.click(self.child("first-run-key-field-click"))
        self.wait_for(lambda: self.recording() == "window", message="recording to start")
        self.assertTrue(self.child("first-run-key-field").property("focused"))

    def test_a_valid_combination_is_bound_through_the_install_path_off_the_gui_thread(self) -> None:
        self.build()
        self.start_recording()

        self.press(KEY_X, META)
        self.wait_for_calls(1)

        self.assertEqual([("Meta+X", None)], self.install.calls)
        self.assertNotIn(threading.get_ident(), self.install.threads)
        self.assertEqual("", self.recording())
        self.wait_for(lambda: self.keys.view["canContinue"] is True, message="the key to be shown as set")
        self.assertTrue(self.child("first-run-next").property("enabled"))
        self.assertEqual("Meta + X · set", self.text("first-run-status"))

    def test_a_key_with_no_meta_ctrl_or_alt_is_refused_and_recording_goes_on(self) -> None:
        self.build()
        self.start_recording()

        self.press(KEY_X)
        self.press(KEY_X, SHIFT)

        self.assertEqual([], self.install.calls)
        self.assertEqual("window", self.recording())
        self.assertEqual(keys.HINT_NEEDS_MODIFIER, self.text("first-run-key-field-hint"))
        self.assertFalse(self.child("first-run-next").property("enabled"))

    def test_a_held_key_is_refused_naming_its_holder_and_nothing_is_set(self) -> None:
        self.build()
        self.install.failure = HotkeyConflictError(
            "Meta+X is already used by Klipper. Choose a different hotkey, or release that one first."
        )
        self.start_recording()

        self.press(KEY_X, META)
        self.wait_for_calls(1)
        self.wait_for(lambda: self.keys.view["note"].get("status") == "held", message="the refusal")

        self.assertIn("Klipper", self.text("first-run-note-text"))
        self.assertIn("Klipper", self.text("first-run-detail"))
        self.assertTrue(self.child("first-run-note").property("visible"))
        self.assertFalse(self.keys.view["canContinue"])
        self.assertFalse(self.child("first-run-next").property("enabled"))

    def test_keystrokes_are_ignored_when_the_field_is_not_recording(self) -> None:
        self.build()
        self.activate()
        field = self.child("first-run-key-field")
        self.invoke(self.child("first-run-key-field-focus"), "forceActiveFocus")
        self.assertTrue(field.property("focused"))

        self.press(KEY_X, META)
        self.press(KEY_SPACE, META | SHIFT)

        self.assertEqual([], self.install.calls)
        self.assertEqual("", self.recording())

    def test_keystrokes_are_ignored_when_the_field_has_lost_the_focus(self) -> None:
        self.build()
        self.start_recording()
        self.invoke(self.child("first-run-try"), "forceActiveFocus")
        self.wait_for(lambda: self.recording() == "", message="recording to stop")

        self.press(KEY_X, META)

        self.assertEqual([], self.install.calls)

    def test_escape_cancels_recording_and_binds_nothing(self) -> None:
        self.build()
        self.start_recording()

        self.press(0x01000000)

        self.assertEqual("", self.recording())
        self.assertEqual([], self.install.calls)

    def test_a_suggestion_is_bound_through_the_same_path(self) -> None:
        self.build()

        self.child("first-run-pick-Meta+Shift+Space").click()
        self.wait_for_calls(1)

        self.assertEqual([("Meta+Shift+Space", None)], self.install.calls)

    def test_binding_never_moves_the_focus_or_raises_the_window(self) -> None:
        from PySide6.QtGui import QGuiApplication

        self.build()
        self.start_recording()
        changes: list[object] = []
        QGuiApplication.instance().focusWindowChanged.connect(changes.append)
        before = QGuiApplication.focusWindow()

        self.press(KEY_X, META)
        self.wait_for_calls(1)

        self.assertEqual(before, QGuiApplication.focusWindow())
        self.assertEqual([], changes)


class AccuracyStepTests(KeysQtCase):
    def to_accuracy(self) -> None:
        self.build(record={"window": "Meta+X"})
        self.wait_for(lambda: self.first_run.view["visible"], message="first run")
        self.child("first-run-next").click()
        self.wait_for(lambda: self.first_run.view["step"] == "accuracy", message="the accuracy step")
        self.wait_for(lambda: self.settings.view.get("loaded") is True, message="the file to be read")

    def test_the_cards_show_the_file_and_choosing_one_writes_the_profile(self) -> None:
        self.to_accuracy()
        self.assertTrue(self.child("first-run-profile-balanced").property("selected"))
        writes = self.writer.writes

        self.child("first-run-profile-accurate").click()
        self.wait_for(lambda: self.writer.writes == writes + 1, message="the write")
        self.wait_for(lambda: self.settings.view.get("status", "") != "", message="the save to be reported")

        self.assertEqual("accurate", self.data()["stt"]["model_profile"])
        self.assertIn("# my own notes", self.path.read_text(encoding="utf-8"))
        self.assertTrue(self.child("first-run-profile-accurate").property("selected"))
        self.assertNotIn(threading.get_ident(), self.writer.threads)

    def test_the_choice_is_applied_by_restarting_once_the_daemon_is_idle(self) -> None:
        self.to_accuracy()
        self.service.state = "RECORDING"

        self.child("first-run-profile-fast").click()
        self.wait_for(lambda: self.text("first-run-profile-status") != "", message="the save to be reported")
        self.assertEqual(0, self.service.restarts)
        self.assertIn("idle", self.text("first-run-profile-status"))

        self.service.state = "IDLE"
        self.wait_for(lambda: self.service.restarts == 1, message="the restart")

    def test_finish_closes_first_run_and_back_returns_to_the_keys(self) -> None:
        self.to_accuracy()
        self.child("first-run-back").click()
        self.wait_for(lambda: self.first_run.view["step"] == "keys", message="the keys step")
        self.child("first-run-next").click()
        self.wait_for(lambda: self.first_run.view["step"] == "accuracy", message="the accuracy step")

        self.assertEqual("Finish", self.text("first-run-next"))
        self.child("first-run-next").click()
        self.wait_for(lambda: not self.first_run.view["visible"], message="first run to close")
        self.assertTrue(self.child("nav-home").property("visible"))

    def test_set_up_later_closes_it_and_home_offers_to_set_up_again(self) -> None:
        self.build()
        self.child("first-run-later").click()
        self.wait_for(lambda: not self.first_run.view["visible"], message="first run to close")

        self.assertTrue(self.child("nav-home").property("visible"))


class SettingsKeyRowsTests(KeysQtCase):
    def open_settings(self) -> None:
        self.child("nav-settings").click()
        self.wait_for(lambda: self.settings.view.get("loaded") is True, message="the file to be read")

    def test_the_rows_show_the_keys_bound_and_what_is_not_set(self) -> None:
        self.build(show_first_run=False, record={"window": "Meta+X"})
        self.open_settings()

        self.assertEqual("Change", self.text("settings-key-window-change"))
        self.assertEqual("Choose a key", self.text("settings-key-session-change"))

    def test_the_dictate_row_records_and_binds_through_the_same_path(self) -> None:
        self.build(show_first_run=False, record={"window": "Meta+X", "session": "Meta+A"})
        self.open_settings()
        self.activate()

        self.child("settings-key-window-change").click()
        self.wait_for(lambda: self.recording() == "window", message="recording to start")
        self.assertTrue(self.child("settings-key-window").property("focused"))
        self.press(KEY_SPACE, META | SHIFT)
        self.wait_for_calls(1)

        # The reply key goes with it, so changing one never unbinds the other.
        self.assertEqual([("Meta+Shift+Space", "Meta+A")], self.install.calls)
        self.assertNotIn(threading.get_ident(), self.install.threads)
        self.wait_for(lambda: self.keys.view["rows"]["window"]["display"] == "Meta + Shift + Space", message="the new key")

    def test_the_reply_row_binds_the_reply_key_alongside_the_dictate_key(self) -> None:
        self.build(show_first_run=False, record={"window": "Meta+X"})
        self.open_settings()
        self.activate()

        self.child("settings-key-session-change").click()
        self.wait_for(lambda: self.recording() == "session", message="recording to start")
        self.press(0x41, META)
        self.wait_for_calls(1)

        self.assertEqual([("Meta+X", "Meta+A")], self.install.calls)

    def test_a_refused_key_in_settings_shows_the_installers_refusal(self) -> None:
        self.build(show_first_run=False, record={"window": "Meta+X"})
        self.install.failure = HotkeyConflictError(
            "Meta+A is already used by Klipper. Choose a different hotkey, or release that one first."
        )
        self.open_settings()
        self.activate()

        self.child("settings-key-session-change").click()
        self.wait_for(lambda: self.recording() == "session", message="recording to start")
        self.press(0x41, META)
        self.wait_for_calls(1)
        self.wait_for(lambda: self.keys.view["note"].get("status") == "held", message="the refusal")

        self.assertIn("Klipper", self.text("settings-keys-note-text"))
        self.assertEqual("Choose a key", self.text("settings-key-session-change"))

    def test_keystrokes_are_ignored_in_a_row_that_is_not_recording(self) -> None:
        self.build(show_first_run=False, record={"window": "Meta+X"})
        self.open_settings()
        self.activate()
        self.invoke(self.child("settings-key-window-focus"), "forceActiveFocus")

        self.press(KEY_X, META)

        self.assertEqual([], self.install.calls)
        self.assertEqual("", self.recording())

    def test_cancel_stops_recording(self) -> None:
        self.build(show_first_run=False, record={"window": "Meta+X"})
        self.open_settings()
        self.activate()
        self.child("settings-key-window-change").click()
        self.wait_for(lambda: self.recording() == "window", message="recording to start")

        self.child("settings-key-window-cancel").click()

        self.wait_for(lambda: self.recording() == "", message="recording to stop")
        self.assertEqual([], self.install.calls)


class HomeSetupTests(KeysQtCase):
    def test_home_offers_set_up_when_no_key_is_bound_and_it_reopens_first_run(self) -> None:
        self.build(show_first_run=False)
        # Home lists its keys once the daemon (here, a fake) has answered.
        self.wait_for(
            lambda: self.find("key-setup-0") is not None and self.find("key-setup-0").property("visible"),
            message="Home to offer setting up",
        )
        self.assertFalse(self.child("key-setup-1").property("visible"))

        self.child("key-setup-0").click()

        self.wait_for(lambda: self.first_run.view["visible"], message="first run")
        self.assertTrue(self.child("first-run-keys-step").property("visible"))

        # Opened later, it still records when its field is clicked.
        self.activate()
        self.click(self.child("first-run-key-field-click"))
        self.wait_for(lambda: self.recording() == "window", message="recording to start")
        self.assertTrue(self.child("first-run-key-field").property("focused"))
        self.press(KEY_X, META)
        self.wait_for_calls(1)
        self.assertEqual([("Meta+X", None)], self.install.calls)


if __name__ == "__main__":
    unittest.main()
