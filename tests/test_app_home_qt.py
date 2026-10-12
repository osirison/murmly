"""Home through the real Qt layer: the window, the controller and its worker thread."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from channel_helpers import command_channel_address
from home_fakes import FakeChannel, delivered, refused
from murmly.app import home

from qt_app import MessageLog, qt_application

NOT_TYPED_DETAIL = "  <b>Not typed</b> & \u00e9\u00e8 \u2014 paste failed:\nsecond line\t  "


class HomeQtCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = qt_application()

    def setUp(self) -> None:
        self.channel = FakeChannel()

    def build(self, channel=None, start_service=None, config_loader=None) -> None:
        from murmly.app import controller, qt

        self.log = MessageLog()
        self.log.__enter__()
        self.addCleanup(self.log.__exit__)
        self.assertEqual([], qt.prepare_application())
        if channel is not None:
            self.channel = channel
        self.copied: list[str] = []
        self.starts = 0

        def started() -> home.StartResult:
            self.starts += 1
            return start_service() if start_service else home.StartResult(True)

        model = home.HomeModel(self.channel, config_loader=config_loader, start_service=started)
        self.home = controller.HomeController(model)
        self.engine, self.theme = qt.create_engine(self.home)
        self.addCleanup(self._close)
        self.window = self.engine.rootObjects()[0]
        self.settle()

    def _close(self) -> None:
        self.home.shutdown()
        self.window.close()
        self.engine.deleteLater()
        self.settle()

    def settle(self) -> None:
        self.application.processEvents()

    def wait_for(self, predicate, timeout: float = 3.0, message: str = "condition") -> float:
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

    def text(self, name: str) -> str:
        return self.child(name).property("text")

    def chip(self) -> str:
        return self.text("chip-text")


class HomeStateTests(HomeQtCase):
    def test_each_state_is_shown_in_the_chip_and_the_heading(self) -> None:
        self.build()
        self.wait_for(lambda: self.window.property("onScreen"), message="the window to show")
        self.wait_for(lambda: self.chip() == "ready", message="ready")
        self.assertEqual("Ready when you are", self.text("hero-headline"))

        for daemon_state, chip, headline in (
            ("LISTENING", "listening", "Listening"),
            ("THINKING", "writing", "Writing your words"),
            ("SPEAKING", "speaking", "Speaking"),
        ):
            with self.subTest(daemon_state):
                self.channel.state = daemon_state
                self.wait_for(lambda: self.chip() == chip, message=chip)
                self.assertEqual(headline, self.text("hero-headline"))

        self.channel.state = "IDLE"
        self.channel.last_words = refused()
        self.wait_for(lambda: self.chip() == "1 waiting on you", message="needs you")

        self.channel.failure = FileNotFoundError("no socket")
        self.wait_for(lambda: self.chip() == "service not running", message="not running")

    def test_a_state_change_appears_within_one_second(self) -> None:
        self.build()
        self.wait_for(lambda: self.chip() == "ready", message="ready")
        changed = time.monotonic()
        self.channel.state = "LISTENING"
        self.wait_for(lambda: self.chip() == "listening", timeout=1.0, message="listening within 1 s")
        self.assertLess(time.monotonic() - changed, 1.0)

    def test_requests_are_at_most_every_500_ms_and_stop_when_the_window_is_hidden(self) -> None:
        self.build()
        self.wait_for(lambda: self.chip() == "ready", message="ready")
        self.wait_for(lambda: self.channel.count("status") >= 4, timeout=4, message="four polls")
        times = [at for at, command in list(self.channel.requests) if command == "status"]
        gaps = [later - earlier for earlier, later in zip(times, times[1:])]
        self.assertGreaterEqual(min(gaps), home.STATUS_INTERVAL_S - 0.02)

        self.window.hide()
        self.settle()
        self.assertFalse(self.window.property("onScreen"))
        time.sleep(0.15)  # a request already on its way may still land
        self.settle()
        before = len(self.channel.requests)
        deadline = time.monotonic() + 1.4
        while time.monotonic() < deadline:
            self.settle()
            time.sleep(0.02)
        self.assertEqual(before, len(self.channel.requests), "a status request was sent while hidden")

        self.window.show()
        self.wait_for(lambda: len(self.channel.requests) > before, timeout=1.0, message="polling to resume")

    def test_a_minimised_window_does_not_poll(self) -> None:
        self.build()
        self.wait_for(lambda: self.chip() == "ready", message="ready")
        self.window.showMinimized()
        self.settle()
        if self.window.property("visibility") != 3:  # QWindow.Minimized
            self.skipTest("This platform does not minimise the test window.")
        self.assertFalse(self.window.property("onScreen"))
        time.sleep(0.15)
        self.settle()
        before = len(self.channel.requests)
        time.sleep(1.2)
        self.settle()
        self.assertEqual(before, len(self.channel.requests))

    def test_the_daemon_is_asked_from_a_worker_thread_never_the_gui_thread(self) -> None:
        import threading

        self.build()
        self.wait_for(lambda: self.chip() == "ready", message="ready")
        self.assertTrue(self.channel.threads)
        self.assertNotIn(threading.get_ident(), self.channel.threads)

    def test_the_header_chip_is_on_every_section(self) -> None:
        self.build()
        self.wait_for(lambda: self.chip() == "ready", message="ready")
        self.child("nav-settings").click()
        self.settle()
        self.channel.state = "LISTENING"
        self.wait_for(lambda: self.chip() == "listening", message="listening")


class HomeContentTests(HomeQtCase):
    def test_the_last_words_show_with_when_and_outcome_and_clear_removes_them(self) -> None:
        self.channel.last_words = delivered("Ship it on Friday.")
        self.build()
        self.wait_for(lambda: self.text("last-words-text") == "Ship it on Friday.", message="last words")
        self.assertEqual("typed", self.text("last-words-outcome"))
        self.assertTrue(self.text("last-words-when").endswith("ago"))

        self.child("clear-last-words").click()
        self.wait_for(lambda: self.child("last-words-empty").property("visible"), message="empty state")
        self.assertEqual(1, self.channel.count("clear_last_words"))
        self.assertIsNone(self.channel.last_words)
        self.assertFalse(self.child("last-words-text").property("visible"))

    def test_the_keys_and_setup_are_shown(self) -> None:
        from murmly.config import MurmlyConfig

        config = MurmlyConfig(socket_path=Path("s"), config_path=Path("/x/config.toml"), speaker_mode="mine-only")
        self.build(config_loader=lambda: config)
        self.model_keys = {"window": "Meta+X"}
        self.home._model._key_reader = lambda: self.model_keys
        self.home._model.reload()
        self.wait_for(lambda: self.child("setup").property("visible"), message="setup summary")
        self.wait_for(lambda: self.text("footer") == f"service running \u00b7 {config.config_path}", message="footer")
        self.wait_for(lambda: self.text("key-note-0") == "set by murmly install", message="keys")

    def test_service_not_running_with_no_socket_offers_to_start(self) -> None:
        from murmly.app import controller, qt

        self.log = MessageLog()
        self.log.__enter__()
        self.addCleanup(self.log.__exit__)
        self.assertEqual([], qt.prepare_application())
        self.starts = 0
        with tempfile.TemporaryDirectory() as temp_dir:
            address = command_channel_address(temp_dir)

            def start() -> home.StartResult:
                self.starts += 1
                return home.StartResult(False, home.NOT_INSTALLED_TEXT)

            model = home.HomeModel(home.default_send(str(address)), start_service=start)
            self.home = controller.HomeController(model)
            self.engine, self.theme = qt.create_engine(self.home)
            self.addCleanup(self._close)
            self.window = self.engine.rootObjects()[0]

            self.wait_for(lambda: self.chip() == "service not running", message="not running")
            self.assertEqual("The background service is not running", self.text("hero-headline"))
            self.assertTrue(self.child("service-card").property("visible"))
            self.assertTrue(self.text("service-reason"))

            self.child("start-service").click()
            self.wait_for(lambda: self.text("start-error") == home.NOT_INSTALLED_TEXT, message="start failure")
        self.assertEqual(1, self.starts)

    def test_a_successful_start_shows_starting_until_the_daemon_answers(self) -> None:
        self.channel.failure = ConnectionRefusedError("refused")
        self.build()
        self.wait_for(lambda: self.chip() == "service not running", message="not running")
        self.child("start-service").click()
        self.wait_for(lambda: self.child("start-service").property("text").startswith("Starting"), message="starting")
        self.channel.failure = None
        self.wait_for(lambda: self.chip() == "ready", message="ready again")
        self.assertFalse(self.child("service-card").property("visible"))


class WordsNotTypedTests(HomeQtCase):
    def setUp(self) -> None:
        super().setUp()
        self.channel.last_words = refused("Can we push the release to Friday?", NOT_TYPED_DETAIL)
        self.build()
        self.wait_for(lambda: self.chip() == "1 waiting on you", message="needs you")

    def test_the_clipboard_reassurance_comes_first(self) -> None:
        self.assertEqual("Your words are safe on the clipboard", self.text("hero-headline"))
        headline = self.child("hero-headline")
        card = self.child("refused-card")
        self.assertTrue(card.property("visible"))
        # Layout settles a moment after the state does, more slowly on some platforms.
        self.wait_for(
            lambda: headline.mapToItem(None, 0, 0).y() < card.mapToItem(None, 0, 0).y(),
            message="the reassurance above the card",
        )

    def test_the_detail_string_is_shown_byte_for_byte(self) -> None:
        self.assertEqual(NOT_TYPED_DETAIL, self.text("refused-detail"))

    def test_the_detail_and_the_transcript_are_plain_text_not_markup(self) -> None:
        from PySide6.QtQml import QQmlEngine, QQmlExpression

        for name in ("refused-detail", "refused-text", "hero-body"):
            with self.subTest(name):
                item = self.child(name)
                expression = QQmlExpression(QQmlEngine.contextForObject(item), item, "textFormat === Text.PlainText")
                self.assertIs(True, expression.evaluate()[0])


    def test_copy_again_writes_the_transcript_to_the_clipboard(self) -> None:
        from PySide6.QtGui import QGuiApplication

        clipboard = QGuiApplication.clipboard()
        clipboard.setText("something else")
        self.child("copy-again").click()
        self.settle()
        if clipboard.text() == "" and QGuiApplication.platformName() == "wayland" and not self.window.isActive():
            # Wayland lets only the focused client set the clipboard, and a
            # click in a real window is what gives it focus.
            self.skipTest("Wayland ignores clipboard writes from a window that is not active.")
        self.assertEqual("Can we push the release to Friday?", clipboard.text())

    def test_done_acknowledges_the_refusal_and_keeps_the_words(self) -> None:
        self.child("dismiss-refusal").click()
        self.wait_for(lambda: self.chip() == "ready", message="ready")
        self.assertEqual("Can we push the release to Friday?", self.text("last-words-text"))
        self.assertIn("not typed", self.text("last-words-outcome"))

    def test_a_newer_refusal_is_shown_again_after_done(self) -> None:
        self.child("dismiss-refusal").click()
        self.wait_for(lambda: self.chip() == "ready", message="ready")
        self.channel.state = "THINKING"
        self.wait_for(lambda: self.chip() == "writing", message="writing")
        newer = refused("Another one.", "Nothing could be pasted.")
        newer["produced_at"] = "2026-10-12T10:05:00+00:00"
        self.channel.last_words = newer
        self.channel.state = "IDLE"
        self.wait_for(lambda: self.chip() == "1 waiting on you", message="needs you again")
        self.assertEqual("Nothing could be pasted.", self.text("refused-detail"))


class FocusTests(HomeQtCase):
    def test_the_spies_do_see_a_call_made_through_the_window_class(self) -> None:
        from unittest.mock import patch

        self.build()
        calls: list[str] = []
        with patch.object(type(self.window), "requestActivate", lambda *a, **k: calls.append("requestActivate")):
            self.window.requestActivate()
        self.assertEqual(["requestActivate"], calls)

    def test_no_daemon_state_raises_activates_or_focuses_a_window(self) -> None:
        from unittest.mock import patch

        from PySide6.QtGui import QGuiApplication, QWindow
        from PySide6.QtQuick import QQuickWindow

        calls: list[str] = []

        def spy(name):
            def record(*args, **kwargs):
                calls.append(name)

            return record

        names = ("requestActivate", "raise_", "lower", "show", "showNormal", "showMaximized",
                 "showFullScreen", "showMinimized", "setVisible", "setWindowState", "alert")
        self.build()
        self.wait_for(lambda: self.chip() == "ready", message="ready")

        focus_changes: list[object] = []
        QGuiApplication.instance().focusWindowChanged.connect(focus_changes.append)
        active_before = self.window.isActive()
        focus_window_before = QGuiApplication.focusWindow()
        visibility_before = self.window.property("visibility")
        item_before = self.window.activeFocusItem()

        patches = []
        for cls in (QWindow, QQuickWindow, type(self.window)):
            for name in names:
                if hasattr(cls, name):
                    patches.append(patch.object(cls, name, spy(f"{cls.__name__}.{name}")))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        steps = (
            ("LISTENING", None, "listening"),
            ("THINKING", None, "writing"),
            ("SPEAKING", None, "speaking"),
            ("IDLE", delivered(), "ready"),
            ("LISTENING", None, "listening"),
            ("THINKING", None, "writing"),
            ("IDLE", refused(), "1 waiting on you"),
        )
        for state, words, chip in steps:
            with self.subTest(state=state, chip=chip):
                self.channel.state = state
                self.channel.last_words = words
                self.wait_for(lambda: self.chip() == chip, message=chip)
        self.channel.failure = ConnectionRefusedError("gone")
        self.wait_for(lambda: self.chip() == "service not running", message="not running")
        self.channel.failure = None
        self.channel.state = "IDLE"
        self.channel.last_words = None
        self.wait_for(lambda: self.chip() == "ready", message="back to ready")

        self.assertEqual([], calls)
        self.assertEqual(active_before, self.window.isActive())
        self.assertEqual(visibility_before, self.window.property("visibility"))
        self.assertEqual(focus_window_before, QGuiApplication.focusWindow())
        self.assertEqual(item_before, self.window.activeFocusItem())
        self.assertEqual([], focus_changes)


if __name__ == "__main__":
    unittest.main()
