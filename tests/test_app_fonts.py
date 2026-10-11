from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

from murmly.app import fonts, theme

from qt_app import qt_application

REPO = Path(__file__).resolve().parent.parent
APP_DIR = Path(fonts.__file__).resolve().parent


class BundledFontFileTests(unittest.TestCase):
    def test_every_font_the_app_loads_exists_in_the_package(self) -> None:
        for path in fonts.font_paths():
            with self.subTest(font=path.name):
                self.assertTrue(path.is_file())
                self.assertEqual(APP_DIR / "fonts", path.parent)

    def test_every_font_has_a_licence_file_with_the_ofl_in_it(self) -> None:
        for name in fonts.FONT_FILES:
            family = name.split("-")[0]
            with self.subTest(font=name):
                licence = REPO / "licenses" / fonts.LICENSE_FILES[family]
                self.assertTrue(licence.is_file())
                self.assertIn("SIL OPEN FONT LICENSE Version 1.1", licence.read_text(encoding="utf-8"))

    def test_the_licences_are_listed_in_the_licences_readme(self) -> None:
        readme = (REPO / "licenses" / "README.md").read_text(encoding="utf-8")
        for licence in fonts.LICENSE_FILES.values():
            with self.subTest(licence=licence):
                self.assertIn(licence, readme)

    def test_the_fonts_are_latin_subsets_not_the_full_files(self) -> None:
        # The upstream files are 85 to 175 kB each; a subset is about 27 to 50.
        for path in fonts.font_paths():
            with self.subTest(font=path.name):
                self.assertLess(path.stat().st_size, 60_000)

    def test_the_wheel_ships_the_font_files(self) -> None:
        config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        package_data = config["tool"]["setuptools"]["package-data"]
        self.assertIn("fonts/*.ttf", package_data["murmly.app"])
        self.assertIn("qml/*.qml", package_data["murmly.app"])

    def test_the_wheel_carries_the_font_licences(self) -> None:
        config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        shipped = config["tool"]["setuptools"]["license-files"]
        for licence in fonts.LICENSE_FILES.values():
            self.assertIn(f"licenses/{licence}", shipped)


class NoNetworkFontTests(unittest.TestCase):
    def test_nothing_in_the_app_names_a_url(self) -> None:
        # Fonts come from files in the package. A web address anywhere in the
        # app's Python or QML would be a way to fetch one, or anything else.
        sources = [*APP_DIR.glob("*.py"), *(APP_DIR / "qml").glob("*.qml")]
        self.assertGreater(len(sources), 3)
        for path in sources:
            with self.subTest(file=path.name):
                self.assertIsNone(
                    re.search(r"https?://", path.read_text(encoding="utf-8")),
                    f"{path.name} names a URL",
                )

    def test_qml_never_loads_a_font_by_url(self) -> None:
        for path in (APP_DIR / "qml").glob("*.qml"):
            with self.subTest(file=path.name):
                self.assertNotIn("FontLoader", path.read_text(encoding="utf-8"))


class QtFontTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        qt_application()

    def test_loading_registers_both_families_and_refuses_nothing(self) -> None:
        from PySide6.QtGui import QFontDatabase

        refused = fonts.load_fonts()

        self.assertEqual([], refused)
        families = set(QFontDatabase.families())
        self.assertIn(theme.FONT_SANS, families)
        self.assertIn(theme.FONT_MONO, families)

    def test_every_weight_the_scale_uses_resolves_inside_the_bundled_files(self) -> None:
        from PySide6.QtGui import QFont, QFontDatabase, QFontInfo

        fonts.load_fonts()
        # Registered from the package's own files, not found on the system.
        for path in fonts.font_paths():
            with self.subTest(font=path.name):
                registered = QFontDatabase.applicationFontFamilies(
                    QFontDatabase.addApplicationFont(str(path))
                )
                expected = theme.FONT_SANS if path.name.startswith("Instrument") else theme.FONT_MONO
                self.assertIn(expected, registered)
        wanted = {theme.FONT_SANS: (400, 500, 600), theme.FONT_MONO: (400, 600)}
        for family, weights in wanted.items():
            for weight in weights:
                with self.subTest(family=family, weight=weight):
                    font = QFont(family)
                    font.setWeight(QFont.Weight(weight))
                    info = QFontInfo(font)
                    self.assertEqual(family, info.family())
                    self.assertEqual(weight, info.weight())

    def test_the_application_font_and_the_mono_font_are_the_bundled_ones(self) -> None:
        from PySide6.QtGui import QFont, QFontInfo, QGuiApplication

        from murmly.app import qt

        self.assertEqual([], qt.prepare_application())

        self.assertEqual(theme.FONT_SANS, QFontInfo(QGuiApplication.font()).family())
        mono = QFontInfo(QFont(theme.FONT_MONO))
        self.assertEqual(theme.FONT_MONO, mono.family())
        self.assertTrue(mono.fixedPitch())


if __name__ == "__main__":
    unittest.main()
