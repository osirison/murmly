from __future__ import annotations

import unittest

from murmly.app import theme


class ColourTokenTests(unittest.TestCase):
    def test_every_colour_token_has_both_a_dark_and_a_light_value(self) -> None:
        self.assertGreater(len(theme.COLORS), 0)
        for name, values in theme.COLORS.items():
            with self.subTest(token=name):
                self.assertEqual(set(theme.SCHEMES), set(values))
                for scheme in theme.SCHEMES:
                    self.assertTrue(values[scheme], f"{name} has an empty {scheme} value")

    def test_every_value_is_a_colour_qml_can_read(self) -> None:
        for name, values in theme.COLORS.items():
            for scheme, css in values.items():
                with self.subTest(token=name, scheme=scheme):
                    converted = theme.qml_color(css)
                    self.assertRegex(converted, r"^#([0-9a-f]{6}|[0-9a-f]{8})$")

    def test_both_schemes_expose_the_same_names(self) -> None:
        self.assertEqual(set(theme.palette("dark")), set(theme.palette("light")))

    def test_names_stay_distinct_once_written_for_qml(self) -> None:
        for table in (theme.COLORS, theme.TYPE, theme.SPACING, theme.RADII):
            camelled = [theme.camel(name) for name in table]
            self.assertEqual(len(camelled), len(set(camelled)))

    def test_the_colours_the_design_gives_a_meaning_exist(self) -> None:
        # warm = listening, cool = writing and speaking, crimson = needs you,
        # amber = a note.
        for name in (
            "listening-from",
            "listening-to",
            "listening-label",
            "writing-from",
            "writing-to",
            "writing-label",
            "needs-you",
            "needs-you-text",
            "needs-you-band-border",
            "note",
            "note-band-border",
            "surface",
            "panel",
            "text",
            "focus-ring",
        ):
            with self.subTest(token=name):
                self.assertIn(name, theme.COLORS)

    def test_the_canvas_values_are_carried_exactly(self) -> None:
        self.assertEqual({"dark": "#0a0b0d", "light": "#faf8f5"}, theme.COLORS["surface"])
        self.assertEqual({"dark": "#ff6b6b", "light": "#be123c"}, theme.COLORS["needs-you"])
        self.assertEqual({"dark": "#3b82f6", "light": "#1d4ed8"}, theme.COLORS["focus-ring"])

    def test_translucent_colours_become_alpha_first_hex(self) -> None:
        self.assertEqual("#0dff6b6b", theme.qml_color("rgba(255, 107, 107, 0.05)"))
        self.assertEqual("#00000000", theme.qml_color("rgba(0, 0, 0, 0)"))
        self.assertEqual("#80000000", theme.qml_color("rgba(0, 0, 0, 0.5)"))

    def test_a_colour_it_does_not_understand_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            theme.qml_color("hsl(0, 0%, 0%)")

    def test_an_unknown_scheme_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            theme.palette("sepia")

    def test_the_default_scheme_is_one_of_the_two(self) -> None:
        self.assertIn(theme.DEFAULT_SCHEME, theme.SCHEMES)


class TypeSpacingRadiusTests(unittest.TestCase):
    def test_every_type_step_names_a_known_family_and_a_bundled_weight(self) -> None:
        sans_weights = {400, 500, 600}
        mono_weights = {400, 600}
        for name, style in theme.TYPE.items():
            with self.subTest(step=name):
                self.assertIn(style.family, ("sans", "mono"))
                self.assertIn(style.weight, sans_weights if style.family == "sans" else mono_weights)

    def test_half_pixel_sizes_round_up_for_qt(self) -> None:
        self.assertEqual(13, theme.text_spec("button")["pixelSize"])  # 12.5
        self.assertEqual(14, theme.text_spec("band-title")["pixelSize"])  # 13.5
        self.assertEqual(11, theme.text_spec("mono-footnote")["pixelSize"])  # 10.5
        self.assertEqual(12, theme.text_spec("helper")["pixelSize"])

    def test_letter_spacing_is_converted_from_em_to_pixels(self) -> None:
        self.assertAlmostEqual(1.6, theme.text_spec("label")["letterSpacing"])  # 0.16em of 10px
        self.assertAlmostEqual(-0.65, theme.text_spec("title-26")["letterSpacing"])  # -0.025em of 26px

    def test_the_mono_steps_use_the_mono_family_and_labels_are_uppercase(self) -> None:
        self.assertEqual(theme.FONT_MONO, theme.text_spec("mono-caption")["family"])
        self.assertEqual(theme.FONT_SANS, theme.text_spec("body")["family"])
        self.assertTrue(theme.text_spec("label")["uppercase"])
        self.assertFalse(theme.text_spec("body")["uppercase"])

    def test_spacing_and_radius_ladders_are_ported(self) -> None:
        self.assertEqual(6, theme.spacing()["space6"])
        self.assertEqual(40, theme.spacing()["space40"])
        self.assertEqual(13, theme.radii()["r13"])
        self.assertEqual(99, theme.radii()["rPill"])


if __name__ == "__main__":
    unittest.main()
