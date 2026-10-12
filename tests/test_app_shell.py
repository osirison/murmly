from __future__ import annotations

import time
import unittest
from pathlib import Path

from murmly.app import shell, theme

from qt_app import MessageLog, qt_application

QML_DIR = Path(__file__).resolve().parent.parent / "src" / "murmly" / "app" / "qml"


class SectionsTests(unittest.TestCase):
    def test_the_shell_has_home_settings_and_health_in_that_order(self) -> None:
        self.assertEqual(
            [("home", "Home"), ("settings", "Settings"), ("health", "Health")],
            [(section["id"], section["title"]) for section in shell.SECTIONS],
        )


class ShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = qt_application()

    def setUp(self) -> None:
        from murmly.app import qt

        self.log = MessageLog()
        self.log.__enter__()
        self.addCleanup(self.log.__exit__)
        self.assertEqual([], qt.prepare_application())
        self.engine, self.theme = qt.create_engine()
        self.addCleanup(self._close)
        roots = self.engine.rootObjects()
        self.assertEqual(1, len(roots))
        self.window = roots[0]
        self.settle()

    def _close(self) -> None:
        self.window.close()
        self.engine.deleteLater()
        self.settle()

    def settle(self) -> None:
        self.application.processEvents()

    def child(self, name: str):
        """The item named `name`, found by walking the window's visual tree."""
        pending = [self.window.contentItem()]
        while pending:
            item = pending.pop()
            if item.objectName() == name:
                return item
            pending.extend(item.childItems())
        self.fail(f"no item named {name}")

    def activate(self) -> None:
        """Make the window the active one, or skip when the desktop will not.

        Keyboard input only reaches the item with focus in an active window.
        """
        self.window.requestActivate()
        deadline = time.monotonic() + 2
        while not self.window.isActive() and time.monotonic() < deadline:
            self.settle()
        if not self.window.isActive():
            self.skipTest("The desktop did not activate the test window.")

    def press(self, key) -> None:
        from PySide6.QtTest import QTest

        QTest.keyClick(self.window, key)
        self.settle()

    def focused(self) -> str:
        item = self.window.activeFocusItem()
        return item.objectName() if item is not None else ""

    def test_the_qml_loads_with_no_errors_or_warnings(self) -> None:
        self.assertEqual([], self.log.messages)

    def test_the_three_sections_exist_and_home_shows_first(self) -> None:
        for section in shell.SECTIONS:
            with self.subTest(section=section["id"]):
                self.assertEqual(section["title"], self.child(f"nav-{section['id']}").property("text"))
                self.assertEqual(section["title"], self.child(f"page-{section['id']}").property("title"))
        self.assertEqual(0, self.window.property("current"))
        self.assertTrue(self.child("page-home").property("visible"))
        self.assertFalse(self.child("page-settings").property("visible"))

    def test_clicking_a_section_switches_to_it(self) -> None:
        self.child("nav-health").click()
        self.settle()

        self.assertEqual(2, self.window.property("current"))
        self.assertTrue(self.child("page-health").property("visible"))
        self.assertFalse(self.child("page-home").property("visible"))
        self.assertTrue(self.child("nav-health").property("selected"))

    def test_tab_reaches_each_section_and_space_and_enter_switch_to_it(self) -> None:
        from PySide6.QtCore import Qt

        self.activate()
        self.press(Qt.Key.Key_Tab)
        self.assertEqual("nav-home", self.focused())
        self.press(Qt.Key.Key_Tab)
        self.assertEqual("nav-settings", self.focused())

        self.press(Qt.Key.Key_Space)
        self.assertEqual(1, self.window.property("current"))
        self.assertTrue(self.child("page-settings").property("visible"))

        self.press(Qt.Key.Key_Tab)
        self.assertEqual("nav-health", self.focused())
        self.press(Qt.Key.Key_Return)
        self.assertEqual(2, self.window.property("current"))
        self.assertTrue(self.child("page-health").property("visible"))

        self.press(Qt.Key.Key_Backtab)
        self.assertEqual("nav-settings", self.focused())
        self.press(Qt.Key.Key_Enter)
        self.assertEqual(1, self.window.property("current"))

    def test_the_focused_section_is_visibly_marked(self) -> None:
        from PySide6.QtCore import Qt

        self.activate()
        self.assertFalse(self.child("nav-home").property("visualFocus"))
        self.press(Qt.Key.Key_Tab)
        self.assertTrue(self.child("nav-home").property("visualFocus"))
        self.press(Qt.Key.Key_Tab)
        self.assertFalse(self.child("nav-home").property("visualFocus"))
        self.assertTrue(self.child("nav-settings").property("visualFocus"))

    def test_switching_the_system_scheme_changes_a_theme_colour(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication

        hints = QGuiApplication.styleHints()
        self.addCleanup(hints.unsetColorScheme)
        dark = theme.palette("dark")["surface"]
        light = theme.palette("light")["surface"]

        self.switch_scheme(hints, Qt.ColorScheme.Dark)
        self.assertEqual("dark", self.theme.scheme)
        self.assertEqual(dark, self.theme.colors["surface"])
        self.assertEqual(dark, self.window.property("color").name())

        self.switch_scheme(hints, Qt.ColorScheme.Light)
        self.assertEqual("light", self.theme.scheme)
        self.assertEqual(light, self.theme.colors["surface"])
        self.assertEqual(light, self.window.property("color").name())

    def switch_scheme(self, hints, scheme) -> None:
        """Ask the platform for `scheme`; where it will not change (offscreen), say so as Qt would."""
        hints.setColorScheme(scheme)
        self.settle()
        if hints.colorScheme() != scheme:
            hints.colorSchemeChanged.emit(scheme)
            self.settle()

    def test_an_unknown_scheme_gets_the_default(self) -> None:
        from PySide6.QtCore import Qt

        from murmly.app import qt

        self.assertEqual(theme.DEFAULT_SCHEME, qt.scheme_name(Qt.ColorScheme.Unknown))

    def test_reduced_motion_is_exposed_and_documented_false(self) -> None:
        self.assertIs(False, self.theme.reducedMotion)

    def test_nothing_in_the_qml_raises_or_activates_a_window(self) -> None:
        for path in QML_DIR.glob("*.qml"):
            with self.subTest(file=path.name):
                text = path.read_text(encoding="utf-8")
                for call in ("raise(", "requestActivate", "forceActiveFocus"):
                    self.assertNotIn(call, text)


if __name__ == "__main__":
    unittest.main()
