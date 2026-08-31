"""Task 18.9: one hotkey specification produces the same physical key on each
platform's encoding, and a modifier a platform does not have is refused by
name.

`tests/test_hotkey.py` proves `parse_hotkey`'s KDE output is unchanged by the
parse/encode split (task 5.1); this file exercises the neutral spec and the
per-platform encoders it now feeds, including GNOME's, which did not exist
before this split.
"""

from __future__ import annotations

import unittest

from murmly.hotkey import (
    HotkeyError,
    HotkeySpec,
    decode_kde_keycode,
    encode_for_gnome,
    encode_for_kde,
    gnome_accelerator,
    gnome_accelerator_for_keycode,
    parse_gnome_accelerator,
    parse_specification,
)


class SamePhysicalKeyTests(unittest.TestCase):
    """One `HotkeySpec` must name the same physical key on every encoder."""

    def test_letter_with_two_modifiers(self) -> None:
        spec = parse_specification("Meta+Shift+X")

        self.assertEqual("Meta+Shift+X", encode_for_kde(spec).portable)
        self.assertEqual("<Super><Shift>x", encode_for_gnome(spec))

    def test_digit(self) -> None:
        spec = parse_specification("Ctrl+7")

        self.assertEqual("Ctrl+7", encode_for_kde(spec).portable)
        self.assertEqual("<Control>7", encode_for_gnome(spec))

    def test_function_key(self) -> None:
        # Verified live as Ctrl+F9 -> 83886136 (see tests/test_hotkey.py).
        spec = parse_specification("Ctrl+F9")

        self.assertEqual(83886136, encode_for_kde(spec).keycode)
        self.assertEqual("<Control>F9", encode_for_gnome(spec))

    def test_named_key(self) -> None:
        spec = parse_specification("Meta+Volume Mute")

        self.assertEqual("Meta+Volume Mute", encode_for_kde(spec).portable)
        self.assertEqual("<Super>XF86AudioMute", encode_for_gnome(spec))

    def test_all_four_shared_modifiers_combine(self) -> None:
        spec = parse_specification("Shift+Alt+Ctrl+Meta+X")

        kde = encode_for_kde(spec)
        gnome = encode_for_gnome(spec)

        # Both encodings name the same physical key: decoding KDE's own
        # integer back to a spec reproduces exactly what GNOME encoded from.
        self.assertEqual(spec, decode_kde_keycode(kde.keycode))
        self.assertEqual(parse_gnome_accelerator(gnome), spec)

    def test_command_and_cmd_mean_the_same_key_as_meta_on_every_encoder(self) -> None:
        meta = parse_specification("Meta+X")

        for spelling in ("Cmd+X", "Command+X", "Super+X", "Win+X"):
            spec = parse_specification(spelling)
            self.assertEqual(meta, spec)
            self.assertEqual(encode_for_kde(meta), encode_for_kde(spec))
            self.assertEqual(encode_for_gnome(meta), encode_for_gnome(spec))


class PlatformMissingModifierTests(unittest.TestCase):
    """Hyper: GNOME's accelerator format has it, Qt's modifier flags do not."""

    def test_kde_refuses_hyper_by_name(self) -> None:
        spec = parse_specification("Hyper+X")

        with self.assertRaises(HotkeyError) as raised:
            encode_for_kde(spec)

        message = str(raised.exception)
        self.assertIn("'Hyper'", message)
        self.assertIn("KDE Plasma", message)

    def test_gnome_accepts_hyper(self) -> None:
        spec = parse_specification("Hyper+X")

        self.assertEqual("<Hyper>x", encode_for_gnome(spec))

    def test_kde_refusal_names_the_platform_not_a_generic_unknown(self) -> None:
        """Distinct from an unrecognized modifier: `Hyper` is a real modifier
        `parse_specification` accepts. Only KDE's own encoding lacks it."""
        # Accepted by the neutral parser -- the rejection happens only when
        # asked for a Qt key code.
        parse_specification("Hyper+X")

        with self.assertRaises(HotkeyError) as raised:
            encode_for_kde(HotkeySpec(modifiers=frozenset({"Hyper"}), key="X"))

        self.assertIn("has no key for", str(raised.exception))


class GnomeAcceleratorRoundTripTests(unittest.TestCase):
    def test_gnome_accelerator_from_kde_portable_text(self) -> None:
        kde = encode_for_kde(parse_specification("Meta+X"))

        self.assertEqual("<Super>x", gnome_accelerator(kde.portable))

    def test_gnome_accelerator_from_kde_keycode(self) -> None:
        kde = encode_for_kde(parse_specification("Ctrl+Alt+End"))

        self.assertEqual("<Control><Alt>End", gnome_accelerator_for_keycode(kde.keycode))

    def test_primary_token_reads_as_control(self) -> None:
        self.assertEqual(
            HotkeySpec(modifiers=frozenset({"Ctrl"}), key="C"),
            parse_gnome_accelerator("<Primary>c"),
        )

    def test_unparseable_accelerator_is_none_not_a_raise(self) -> None:
        for value in ("", "garbage", "<Nope>x", "<Super>"):
            self.assertIsNone(parse_gnome_accelerator(value))


if __name__ == "__main__":
    unittest.main()
