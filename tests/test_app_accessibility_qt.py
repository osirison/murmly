"""What the app tells a screen reader, read back through Qt's accessibility layer: the
state of the navigation and of every switch and choice, the names of the key buttons
and the value of a slider.

Accessibility is turned on either before the window is built or after, as when a
screen reader starts later, and the state must read the same either way."""

from __future__ import annotations

import unittest

from test_app_keys_qt import KeysQtCase

KEYS = {"window": "Meta+X", "session": "Meta+A"}


class AccessibilityCase(KeysQtCase):
    #: Whether accessibility is on before the window is built (otherwise it is turned on after).
    early = True

    def accessible(self, name: str):
        from PySide6.QtGui import QAccessible

        interface = QAccessible.queryAccessibleInterface(self.child(name))
        self.assertIsNotNone(interface, f"{name} has no accessible interface")
        return interface

    def text(self, name: str, kind: str = "Name") -> str:
        from PySide6.QtGui import QAccessible

        return self.accessible(name).text(getattr(QAccessible.Text, kind))

    def state(self, name: str) -> tuple[bool, bool]:
        """(checkable, checked), as a screen reader reads them."""
        state = self.accessible(name).state()
        return bool(state.checkable), bool(state.checked)

    def open_settings(self, record: dict[str, str] = KEYS) -> None:
        from PySide6.QtGui import QAccessible

        was_active = QAccessible.isActive()
        self.addCleanup(QAccessible.setActive, was_active)
        QAccessible.setActive(self.early)
        self.build(show_first_run=False, record=record)
        self.click(self.child("nav-settings"))
        self.wait_for(lambda: self.settings.view.get("loaded") is True, message="the file to be read")
        self.wait_for(lambda: self.find("set-profile-balanced") is not None, message="the settings page")
        QAccessible.setActive(True)
        self.settle()

    def option_names(self, prefix: str) -> list[str]:
        names, pending = [], [self.window.contentItem()]
        while pending:
            item = pending.pop()
            if item.objectName().startswith(prefix):
                names.append(item.objectName())
            pending.extend(item.childItems())
        return sorted(names)


class NavigationStateMixin:
    def test_the_section_shown_is_checked_and_the_others_are_not(self) -> None:
        self.open_settings()

        self.assertEqual(
            [(True, False), (True, True), (True, False)],
            [self.state(n) for n in ("nav-home", "nav-settings", "nav-health")],
        )

    def test_the_group_shown_when_settings_opens_is_checked(self) -> None:
        self.open_settings()

        self.assertEqual((True, True), self.state("settings-go-keys"))
        self.assertEqual((True, False), self.state("settings-go-accuracy"))

    def test_the_group_chosen_is_checked_and_the_one_left_is_not(self) -> None:
        self.open_settings()
        self.click(self.child("settings-go-accuracy"))

        self.assertEqual((True, False), self.state("settings-go-keys"))
        self.assertEqual((True, True), self.state("settings-go-accuracy"))

    def test_a_switch_is_checkable_and_checked_when_on(self) -> None:
        self.open_settings()
        on = self.child("set-overlay").property("on")

        self.assertEqual((True, on), self.state("set-overlay"))

    def test_a_card_is_checked_only_when_it_is_the_choice(self) -> None:
        self.open_settings()

        self.assertEqual((True, True), self.state("set-profile-balanced"))
        self.assertEqual((True, False), self.state("set-profile-fast"))

    def test_a_segmented_option_is_checked_only_when_it_is_the_choice(self) -> None:
        self.open_settings()
        states = {name: self.state(name) for name in self.option_names("set-finish-")}

        self.assertGreater(len(states), 1)
        for name, state in states.items():
            with self.subTest(name):
                self.assertEqual((True, bool(self.child(name).property("selected"))), state)
        self.assertEqual(1, sum(1 for _checkable, checked in states.values() if checked))

    def test_a_click_does_not_flip_the_state_away_from_the_file(self) -> None:
        self.open_settings()
        self.click(self.child("set-profile-fast"))
        self.wait_for(lambda: self.child("set-profile-fast").property("selected") is True, message="the choice")

        for name in ("set-profile-fast", "set-profile-balanced"):
            self.assertEqual(
                (True, bool(self.child(name).property("selected"))), self.state(name), name
            )


class AccessibilityOnFirstTests(NavigationStateMixin, AccessibilityCase):
    early = True


class AccessibilityOnLaterTests(NavigationStateMixin, AccessibilityCase):
    early = False


class KeyButtonNameTests(AccessibilityCase):
    def test_each_change_button_names_the_keys_it_changes(self) -> None:
        self.open_settings()

        self.assertIn("Dictate", self.text("settings-key-window-change"))
        self.assertIn("Reply to your assistant", self.text("settings-key-session-change"))
        self.assertIn("Change", self.text("settings-key-window-change"))

    def test_a_row_with_no_key_names_what_the_button_chooses(self) -> None:
        self.open_settings(record={"window": "Meta+X"})

        name = self.text("settings-key-session-change")

        self.assertIn("Choose", name)
        self.assertIn("Reply to your assistant", name)


class SliderValueTests(AccessibilityCase):
    def test_a_slider_says_its_value_as_the_screen_shows_it(self) -> None:
        self.open_settings()

        self.assertEqual("100 %", self.text("set-rate", "Description"))


if __name__ == "__main__":
    unittest.main()
