"""What the app tells a screen reader, read back through Qt's accessibility layer: the
state of the navigation, the names of the key buttons and the value of a slider."""

from __future__ import annotations

import unittest

from test_app_keys_qt import KeysQtCase

KEYS = {"window": "Meta+X", "session": "Meta+A"}


class AccessibilityCase(KeysQtCase):
    def accessible(self, name: str):
        from PySide6.QtGui import QAccessible

        interface = QAccessible.queryAccessibleInterface(self.child(name))
        self.assertIsNotNone(interface, f"{name} has no accessible interface")
        return interface

    def text(self, name: str, kind: str = "Name") -> str:
        from PySide6.QtGui import QAccessible

        return self.accessible(name).text(getattr(QAccessible.Text, kind))

    def open_settings(self, record: dict[str, str] = KEYS) -> None:
        from PySide6.QtGui import QAccessible

        # Qt only records the state of items made while accessibility is on.
        QAccessible.setActive(True)
        self.build(show_first_run=False, record=record)
        self.click(self.child("nav-settings"))
        self.wait_for(lambda: self.settings.view.get("loaded") is True, message="the file to be read")


class NavigationStateTests(AccessibilityCase):
    def checked(self, name: str) -> bool:
        return bool(self.accessible(name).state().checked)

    def test_the_section_shown_is_marked_checked_and_the_others_are_not(self) -> None:
        self.open_settings()

        self.assertEqual([False, True, False], [self.checked(n) for n in ("nav-home", "nav-settings", "nav-health")])

    def test_the_group_shown_in_settings_is_marked_checked(self) -> None:
        self.open_settings()
        self.click(self.child("settings-go-accuracy"))

        self.assertEqual(
            [False, True], [self.checked(n) for n in ("settings-go-keys", "settings-go-accuracy")]
        )


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
