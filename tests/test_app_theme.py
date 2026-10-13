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


SURFACES = ("surface", "panel", "panel-alt", "panel-raised")
# The tints a band or a card lays over a surface.
TINTS = ("note-band-bg", "needs-you-band-bg", "needs-you-card-bg", "destructive-card-bg")
# Every text token and the backgrounds it is drawn on. A tinted background (a
# band, a tinted button) is laid over a surface, so it is checked over each one.
TEXT_ON = {
    "text": SURFACES + TINTS,
    "text-bright": SURFACES + TINTS,
    "text-2": SURFACES + TINTS,
    "text-3": SURFACES + TINTS,
    "text-4": SURFACES + TINTS,
    "text-5": SURFACES + TINTS,
    # "What you said" on Home sits on the needs-you band.
    "text-label": SURFACES + ("needs-you-band-bg",),
    "needs-you-text": SURFACES + ("needs-you-band-bg", "needs-you-btn-bg"),
    "destructive-text": SURFACES + ("destructive-btn-bg",),
    "note": SURFACES + ("note-band-bg",),
    "listening-label": SURFACES,
    "writing-label": SURFACES,
    "btn-quiet-text": SURFACES,
    "btn-secondary-text": SURFACES + ("btn-secondary-bg", "btn-secondary-bg-alt"),
    "btn-primary-text": ("btn-primary-bg",),
    "btn-destructive-text": ("btn-destructive-bg",),
}


def _channels(css: str) -> tuple[tuple[int, int, int], float]:
    hexed = theme.qml_color(css)
    if len(hexed) == 7:
        hexed = "#ff" + hexed[1:]
    alpha, *rgb = (int(hexed[i : i + 2], 16) for i in (1, 3, 5, 7))
    return (rgb[0], rgb[1], rgb[2]), alpha / 255


def _laid_over(top: str, under: tuple[float, float, float], scheme: str) -> tuple[float, float, float]:
    rgb, alpha = _channels(theme.COLORS[top][scheme])
    return tuple(alpha * rgb[i] + (1 - alpha) * under[i] for i in range(3))


def _luminance(rgb) -> float:
    def linear(value: float) -> float:
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(v) for v in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(foreground, background) -> float:
    lighter, darker = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _pairs(foreground: str, backgrounds, scheme: str):
    """(background name, foreground colour, background colour) for each way it is drawn."""
    for name in backgrounds:
        _, alpha = _channels(theme.COLORS[name][scheme])
        unders = SURFACES if alpha < 1 else (None,)
        for under in unders:
            base = _channels(theme.COLORS[under][scheme])[0] if under else (0, 0, 0)
            back = _laid_over(name, base, scheme)
            front = _laid_over(foreground, back, scheme)
            yield (f"{name} over {under}" if under else name), front, back


class ContrastTests(unittest.TestCase):
    def test_every_text_colour_reaches_4_5_to_1_on_each_background_it_is_drawn_on(self) -> None:
        for scheme in theme.SCHEMES:
            for token, backgrounds in TEXT_ON.items():
                for label, front, back in _pairs(token, backgrounds, scheme):
                    with self.subTest(scheme=scheme, text=token, on=label):
                        self.assertGreaterEqual(contrast(front, back), 4.5)

    def test_the_focus_ring_reaches_3_to_1_on_every_surface(self) -> None:
        for scheme in theme.SCHEMES:
            for label, front, back in _pairs("focus-ring", SURFACES, scheme):
                with self.subTest(scheme=scheme, on=label):
                    self.assertGreaterEqual(contrast(front, back), 3.0)

    def test_the_dimmer_text_steps_stay_in_order(self) -> None:
        for scheme in theme.SCHEMES:
            surface = _channels(theme.COLORS["surface"][scheme])[0]
            ratios = [
                contrast(_channels(theme.COLORS[name][scheme])[0], surface) for name in ("text-4", "text-5", "text-label")
            ]
            with self.subTest(scheme=scheme):
                self.assertEqual(ratios, sorted(ratios, reverse=True))


if __name__ == "__main__":
    unittest.main()
