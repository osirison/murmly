"""The desktop app by keyboard alone, through the real Qt layer: Tab reaches every
control that is shown and enabled, in both directions with no trap; the control that
is focused stays in view and keeps the focus after it is operated.

Keys only reach a window the display has made active, so each test skips itself on a
display that will not give it the focus."""

from __future__ import annotations

import time
import unittest

from home_fakes import delivered
from test_app_health_qt import HealthQtCase
from test_app_home_qt import HomeQtCase
from test_app_keys_qt import KeysQtCase
from test_app_removal_qt import RemovalQtCase

KEYS = {"window": "Meta+X", "session": "Meta+A"}
# What a focus ring draws outside a control, so a ring is never clipped by the viewport.
RING = 4


def tabbable(item) -> bool:
    return item.activeFocusOnTab() and item.isVisible() and item.isEnabled()


def flickables(item):
    """The scrolling views `item` sits in, nearest first."""
    parent = item.parentItem()
    while parent is not None:
        if parent.property("contentY") is not None and parent.property("contentHeight") is not None:
            yield parent
        parent = parent.parentItem()


class KeyboardMixin:
    def activate_window(self) -> None:
        """Give the window the focus, as a person clicking into it would."""
        self.window.requestActivate()
        started = time.monotonic()
        while time.monotonic() - started < 2.0 and not self.window.isActive():
            self.settle()
            time.sleep(0.01)
        if not self.window.isActive():
            self.skipTest("this display does not give a test window the focus")

    def key(self, name: str) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        QTest.keyClick(self.window, getattr(Qt.Key, f"Key_{name}"))
        self.settle()

    def press_tab(self, back: bool = False) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        if back:
            QTest.keyClick(self.window, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
        else:
            QTest.keyClick(self.window, Qt.Key.Key_Tab)
        self.settle()

    def focus_name(self) -> str | None:
        item = self.window.activeFocusItem()
        return item.objectName() if item is not None else None

    def walk(self, count: int, back: bool = False) -> list[str | None]:
        names = []
        for _ in range(count):
            self.press_tab(back)
            names.append(self.focus_name())
        return names

    def tabbables(self, root_name: str) -> list[str]:
        names = []
        pending = [self.child(root_name)]
        while pending:
            item = pending.pop()
            if tabbable(item):
                names.append(item.objectName())
            pending.extend(item.childItems())
        self.assertNotIn("", names, "a control Tab can reach has no name")
        return names

    def assert_tab_covers(self, root_name: str, must_include: tuple[str, ...] = ()) -> None:
        """Tab, then Shift+Tab, visit exactly the controls that are shown and enabled."""
        expected = self.tabbables(root_name)
        for name in must_include:
            self.assertIn(name, expected)
        start = self.focus_name()
        self.assertIn(start, expected)

        forward = self.walk(len(expected))
        self.assertEqual(sorted(expected), sorted(forward), "Tab missed a control or visited one twice")
        self.assertEqual(start, forward[-1])
        backward = self.walk(len(expected), back=True)
        self.assertEqual(forward[-2::-1] + [forward[-1]], backward, "Shift+Tab does not retrace Tab")

    def focus(self, name: str) -> None:
        from PySide6.QtCore import QMetaObject

        self.assertTrue(QMetaObject.invokeMethod(self.child(name), "forceActiveFocus"))
        self.settle()
        self.assertEqual(name, self.focus_name())

    def assert_in_view(self, item) -> None:
        from PySide6.QtCore import QRectF

        rect = item.mapRectToScene(QRectF(0, 0, item.width(), item.height())).adjusted(0, -RING, 0, RING)
        for view in flickables(item):
            port = view.mapRectToScene(QRectF(0, 0, view.width(), view.height())).adjusted(-1, -0.5, 1, 0.5)
            self.assertTrue(
                port.contains(rect),
                f"{item.objectName()} at {rect} is outside the view {port} of {view.objectName() or view}",
            )

    def tab_through_in_view(self, limit: int = 150) -> list[str]:
        start = self.focus_name()
        visited = []
        for _ in range(limit):
            self.press_tab()
            item = self.window.activeFocusItem()
            self.assertIsNotNone(item)
            self.assert_in_view(item)
            if item.objectName() == start:
                break
            visited.append(item.objectName())
        else:
            self.fail("Tab never came back to where it began")
        return visited


class FirstRunTabTests(KeyboardMixin, KeysQtCase):
    def start(self, record=None) -> None:
        self.build(record=record)
        self.activate_window()
        self.focus("first-run-key-field-focus")

    def test_with_no_key_chosen_tab_visits_every_enabled_control_and_back(self) -> None:
        self.start()
        self.assertFalse(self.child("first-run-next").property("enabled"))

        self.assert_tab_covers("first-run", must_include=("first-run-try", "first-run-later"))

    def test_with_a_key_chosen_tab_also_reaches_continue(self) -> None:
        self.start(KEYS)
        self.assertTrue(self.child("first-run-next").property("enabled"))

        self.assert_tab_covers("first-run", must_include=("first-run-try", "first-run-later", "first-run-next"))

    def test_the_second_step_reaches_back_as_well(self) -> None:
        self.start(KEYS)
        self.click(self.child("first-run-next"))
        self.assertEqual("accuracy", self.first_run.view["step"])
        self.wait_for(lambda: self.find("first-run-profile-fast") is not None, message="the choices")
        self.focus("first-run-profile-balanced")

        self.assert_tab_covers(
            "first-run",
            must_include=("first-run-profile-fast", "first-run-later", "first-run-back", "first-run-next"),
        )

    def test_tab_leaves_the_try_it_box_instead_of_typing_a_tab(self) -> None:
        self.start(KEYS)
        self.focus("first-run-try")

        self.press_tab()

        self.assertEqual("", self.child("first-run-try").property("text"))
        self.assertEqual("first-run-later", self.focus_name())
        self.assertTrue(self.child("first-run-later").property("visualFocus"))


class OperatedKeepsFocusTests(KeyboardMixin, KeysQtCase):
    def choose_with_space(self, prefix: str) -> None:
        """Space on an option that is not chosen: it becomes chosen and keeps the focus."""
        options = [
            item.objectName()
            for item in self.tabbables_by_prefix(prefix)
            if not item.property("selected")
        ]
        self.assertTrue(options, f"every {prefix} option is already chosen")
        name = options[0]
        self.focus(name)

        self.key("Space")
        self.wait_for(lambda: self.child(name).property("selected") is True, message=f"{name} to be chosen")
        self.settle()

        self.assertEqual(name, self.focus_name())

    def tabbables_by_prefix(self, prefix: str):
        found = []
        pending = [self.window.contentItem()]
        while pending:
            item = pending.pop()
            if item.objectName().startswith(prefix) and tabbable(item):
                found.append(item)
            pending.extend(item.childItems())
        return found

    def open_settings(self) -> None:
        self.build(show_first_run=False, record=KEYS)
        self.activate_window()
        self.click(self.child("nav-settings"))
        self.wait_for(lambda: self.settings.view.get("loaded") is True, message="the file to be read")

    def test_an_accuracy_card_keeps_the_focus(self) -> None:
        self.open_settings()
        self.choose_with_space("set-profile-")

    def test_run_it_on_keeps_the_focus(self) -> None:
        self.open_settings()
        self.choose_with_space("set-stt-device-")

    def test_how_a_recording_ends_keeps_the_focus(self) -> None:
        self.open_settings()
        self.choose_with_space("set-finish-")

    def test_a_voice_card_keeps_the_focus(self) -> None:
        self.open_settings()
        self.click(self.child("settings-go-speech"))
        self.choose_with_space("set-voice-")

    def test_a_first_run_profile_card_keeps_the_focus(self) -> None:
        self.build(record=KEYS)
        self.activate_window()
        self.click(self.child("first-run-next"))
        self.wait_for(lambda: self.find("first-run-profile-fast") is not None, message="the choices")
        self.choose_with_space("first-run-profile-")

    def test_a_text_size_button_that_reaches_its_limit_hands_the_focus_to_the_other(self) -> None:
        self.build(show_first_run=False, record=KEYS)
        self.path.write_text(self.path.read_text(encoding="utf-8") + "\n[overlay]\ntext_size_px = 47\n", encoding="utf-8")
        self.activate_window()
        self.click(self.child("nav-settings"))
        self.wait_for(lambda: self.settings.view.get("loaded") is True, message="the file to be read")
        self.focus("set-text-size-more")

        self.key("Space")
        self.wait_for(lambda: self.child("set-text-size-more").property("enabled") is False, message="the limit")
        self.settle()

        self.assertEqual("set-text-size-less", self.focus_name())
        self.assertTrue(self.child("set-text-size-less").property("visualFocus"))

    def test_finishing_first_run_with_enter_leaves_the_focus_on_a_control(self) -> None:
        self.build(record=KEYS)
        self.activate_window()
        self.click(self.child("first-run-next"))
        self.wait_for(lambda: self.find("first-run-profile-fast") is not None, message="the choices")
        self.focus("first-run-next")

        self.key("Return")
        self.wait_for(lambda: self.find("first-run-next") is None, message="first run to close")
        self.settle()

        self.assertEqual("nav-home", self.focus_name())
        self.assertTrue(self.child("nav-home").property("visualFocus"))


class ScrollIntoViewTests(KeyboardMixin, KeysQtCase):
    def short(self) -> None:
        self.activate_window()
        self.window.resize(1040, 480)
        self.settle()

    def test_tab_through_settings_keeps_each_focused_control_in_view(self) -> None:
        self.build(show_first_run=False, record=KEYS)
        self.short()
        self.click(self.child("nav-settings"))
        self.wait_for(lambda: self.settings.view.get("loaded") is True, message="the file to be read")

        visited = self.tab_through_in_view()

        self.assertIn("set-overlay", visited)
        self.assertIn("settings-go-remove", visited)

    def test_tab_through_the_speech_settings_keeps_each_focused_control_in_view(self) -> None:
        self.build(show_first_run=False, record=KEYS)
        self.short()
        self.click(self.child("nav-settings"))
        self.wait_for(lambda: self.settings.view.get("loaded") is True, message="the file to be read")
        self.click(self.child("settings-go-speech"))

        visited = self.tab_through_in_view()

        self.assertIn("set-tts-device-cpu", visited)

    def test_tab_through_the_second_step_of_first_run_keeps_each_control_in_view(self) -> None:
        self.build(record=KEYS)
        self.short()
        self.click(self.child("first-run-next"))
        self.wait_for(lambda: self.find("first-run-profile-fast") is not None, message="the choices")
        self.focus("first-run-profile-fast")
        self.window.resize(1040, 480)
        self.settle()

        visited = self.tab_through_in_view()

        self.assertIn("first-run-profile-accurate", visited)


class SettingsSubNavTests(KeyboardMixin, KeysQtCase):
    GROUPS = ("keys", "accuracy", "finishing", "live", "delivery", "indicator")

    def open_settings(self) -> None:
        self.build(show_first_run=False, record=KEYS)
        self.window.resize(1040, 480)
        self.click(self.child("nav-settings"))
        self.wait_for(lambda: self.settings.view.get("loaded") is True, message="the file to be read")

    def pause(self, seconds: float = 0.3) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.settle()
            time.sleep(0.01)

    def assert_section_at_top(self, section: str) -> None:
        """The section starts at the top of the view, or the page is scrolled as far as it goes."""
        item = self.child(f"settings-section-{section}")
        view = next(flickables(item))
        top = item.mapToScene(item.boundingRect().topLeft()).y() - view.mapToScene(view.boundingRect().topLeft()).y()
        furthest = view.property("contentHeight") - view.height()
        if view.property("contentY") < furthest - 1:
            self.assertAlmostEqual(8, top, delta=1.5, msg=f"{section} starts {top} px below the top")

    def test_the_first_click_from_speech_lands_on_the_group(self) -> None:
        self.open_settings()
        self.child("settings-go-speech").click()
        self.pause()

        self.child("settings-go-live").click()
        self.pause()

        self.assert_section_at_top("live")

    def test_the_first_click_from_this_computer_lands_on_the_group(self) -> None:
        self.open_settings()
        self.child("settings-go-remove").click()
        self.pause()

        self.child("settings-go-finishing").click()
        self.pause()

        self.assert_section_at_top("finishing")

    def test_each_entry_is_marked_when_it_is_the_one_shown(self) -> None:
        self.open_settings()
        entries = [f"settings-go-{name}" for name in self.GROUPS + ("speech", "remove")]
        self.assertEqual("settings-go-keys", entries[0])

        for chosen in entries:
            self.child(chosen).click()
            self.pause(0.1)
            with self.subTest(chosen):
                self.assertEqual([chosen], [name for name in entries if self.child(name).property("selected")])

    def test_the_focus_moving_into_a_group_marks_it(self) -> None:
        self.open_settings()
        self.activate_window()

        self.focus("set-overlay")

        entries = [f"settings-go-{name}" for name in self.GROUPS + ("speech", "remove")]
        self.assertEqual(["settings-go-indicator"], [name for name in entries if self.child(name).property("selected")])


class HomeScrollTests(KeyboardMixin, HomeQtCase):
    def test_tab_through_home_keeps_each_focused_control_in_view(self) -> None:
        self.channel.last_words = delivered()
        self.build()
        self.activate_window()
        self.window.resize(640, 480)
        self.wait_for(lambda: self.child("clear-last-words").isVisible(), message="the last words")
        self.settle()
        self.focus("nav-home")

        visited = self.tab_through_in_view()

        self.assertIn("clear-last-words", visited)


class HealthScrollTests(KeyboardMixin, HealthQtCase):
    def test_tab_through_health_keeps_each_focused_control_in_view(self) -> None:
        self.build()
        self.activate_window()
        self.window.resize(640, 480)
        self.open_health()
        self.wait_for(lambda: self.rows_shown() > 0, message="the rows")
        self.settle()

        visited = self.tab_through_in_view()

        self.assertGreater(len(visited), 3)


class RemoveDialogScrollTests(KeyboardMixin, RemovalQtCase):
    def test_tab_to_the_delete_switch_scrolls_it_into_view(self) -> None:
        from PySide6.QtCore import QObject

        self.build()
        self.activate_window()
        self.window.resize(640, 480)
        self.child("nav-settings").click()
        self.wait_for(lambda: self.settings.view.get("loaded") is True, message="the file to be read")
        self.child("settings-go-remove").click()
        self.settle()
        self.child("remove-open").click()
        self.wait_for(lambda: self.window.findChild(QObject, "remove-dialog").property("opened"), message="the dialog")
        self.settle()

        visited = self.tab_through_in_view()

        self.assertIn("remove-purge", visited)


if __name__ == "__main__":
    unittest.main()
