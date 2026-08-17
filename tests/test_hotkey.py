from __future__ import annotations

import unittest

from murmly.hotkey import (
    ALT_MODIFIER,
    CONTROL_MODIFIER,
    META_MODIFIER,
    SHIFT_MODIFIER,
    Hotkey,
    HotkeyError,
    parse_hotkey,
)


class HotkeyParsingTests(unittest.TestCase):
    def test_parses_the_combination_verified_against_the_live_desktop(self) -> None:
        # kglobalacceld reported exactly this integer for the Ctrl+Alt+End
        # binding present on the reference machine.
        self.assertEqual(218103825, parse_hotkey("Ctrl+Alt+End").keycode)

    def test_parses_meta_letter(self) -> None:
        hotkey = parse_hotkey("Meta+X")

        self.assertEqual(META_MODIFIER | 0x58, hotkey.keycode)
        self.assertEqual(268435544, hotkey.keycode)
        self.assertEqual("Meta+X", hotkey.portable)

    def test_letters_are_case_insensitive_and_canonicalized(self) -> None:
        self.assertEqual(parse_hotkey("meta+x"), parse_hotkey("Meta+X"))
        self.assertEqual("Meta+X", parse_hotkey("meta+x").portable)

    def test_digits_and_function_keys(self) -> None:
        self.assertEqual(META_MODIFIER | 0x37, parse_hotkey("Meta+7").keycode)
        self.assertEqual(CONTROL_MODIFIER | 0x01000030, parse_hotkey("Ctrl+F1").keycode)
        # F9 = base + 8, verified live as Ctrl+F9 -> 83886136.
        self.assertEqual(83886136, parse_hotkey("Ctrl+F9").keycode)

    def test_named_keys(self) -> None:
        self.assertEqual(META_MODIFIER | 0x20, parse_hotkey("Meta+Space").keycode)
        self.assertEqual(CONTROL_MODIFIER | 0x01000000, parse_hotkey("Ctrl+Esc").keycode)
        self.assertEqual(parse_hotkey("Ctrl+Escape"), parse_hotkey("Ctrl+Esc"))
        self.assertEqual(parse_hotkey("Meta+PageDown"), parse_hotkey("Meta+PgDown"))

    def test_multi_word_named_key_accepts_spaced_and_collapsed_spellings(self) -> None:
        self.assertEqual(parse_hotkey("Meta+Volume Mute"), parse_hotkey("Meta+VolumeMute"))
        self.assertEqual("Meta+Volume Mute", parse_hotkey("Meta+volumemute").portable)

    def test_all_modifiers_combine(self) -> None:
        hotkey = parse_hotkey("Shift+Alt+Ctrl+Meta+X")

        expected = META_MODIFIER | CONTROL_MODIFIER | ALT_MODIFIER | SHIFT_MODIFIER | 0x58
        self.assertEqual(expected, hotkey.keycode)

    def test_portable_form_uses_qt_modifier_order_regardless_of_input_order(self) -> None:
        self.assertEqual("Meta+Ctrl+Alt+Shift+X", parse_hotkey("Shift+Alt+Ctrl+Meta+X").portable)
        self.assertEqual("Ctrl+Alt+End", parse_hotkey("Alt+Ctrl+End").portable)

    def test_keycode_never_sets_the_sign_bit(self) -> None:
        hotkey = parse_hotkey("Shift+Alt+Ctrl+Meta+F35")

        self.assertGreater(hotkey.keycode, 0)
        self.assertLess(hotkey.keycode, 2**31 - 1)


class HotkeyAliasTests(unittest.TestCase):
    def test_super_and_win_normalize_to_meta(self) -> None:
        expected = parse_hotkey("Meta+X")

        self.assertEqual(expected, parse_hotkey("Super+X"))
        self.assertEqual(expected, parse_hotkey("Win+X"))
        self.assertEqual("Meta+X", parse_hotkey("super+x").portable)

    def test_control_spelled_out_normalizes(self) -> None:
        self.assertEqual(parse_hotkey("Ctrl+X"), parse_hotkey("Control+X"))

    def test_surrounding_whitespace_is_tolerated(self) -> None:
        self.assertEqual(parse_hotkey("Meta+X"), parse_hotkey("  Meta + X  "))


class HotkeyRejectionTests(unittest.TestCase):
    """Each rejection must be distinguishable and name what was not understood."""

    def _message(self, text: str) -> str:
        with self.assertRaises(HotkeyError) as raised:
            parse_hotkey(text)
        return str(raised.exception)

    def test_rejects_empty(self) -> None:
        self.assertIn("No hotkey", self._message(""))
        self.assertIn("No hotkey", self._message("   "))

    def test_rejects_comma_because_it_separates_alternatives(self) -> None:
        message = self._message("Meta+X,Meta+Y")

        self.assertIn("comma", message)
        self.assertIn("alternative", message)

    def test_rejects_missing_modifier(self) -> None:
        message = self._message("X")

        self.assertIn("no modifier", message)
        self.assertIn("Meta", message)

    def test_rejects_unknown_key_and_names_it(self) -> None:
        message = self._message("Meta+Frobnicate")

        self.assertIn("'Frobnicate'", message)
        self.assertIn("Supported keys", message)

    def test_rejects_unknown_modifier_and_names_it(self) -> None:
        message = self._message("Hyper+X")

        self.assertIn("'Hyper'", message)
        self.assertIn("Supported modifiers", message)

    def test_rejects_trailing_modifier_with_no_key(self) -> None:
        message = self._message("Ctrl+Meta")

        self.assertIn("instead of a key", message)

    def test_rejects_empty_part(self) -> None:
        self.assertIn("empty part", self._message("Meta++X"))
        self.assertIn("empty part", self._message("Meta+"))

    def test_rejects_repeated_modifier(self) -> None:
        self.assertIn("repeats", self._message("Meta+Meta+X"))

    def test_rejects_out_of_range_function_key(self) -> None:
        message = self._message("Meta+F99")

        self.assertIn("F1-F35", message)

    def test_every_rejection_reason_is_distinct(self) -> None:
        messages = {
            self._message(""),
            self._message("Meta+X,Meta+Y"),
            self._message("X"),
            self._message("Meta+Frobnicate"),
            self._message("Hyper+X"),
            self._message("Ctrl+Meta"),
            self._message("Meta++X"),
            self._message("Meta+Meta+X"),
            self._message("Meta+F99"),
        }

        self.assertEqual(9, len(messages))


class HotkeyValueTests(unittest.TestCase):
    def test_hotkey_is_hashable_and_compares_by_value(self) -> None:
        self.assertEqual(Hotkey(268435544, "Meta+X"), parse_hotkey("Meta+X"))
        self.assertEqual({parse_hotkey("Meta+X")}, {Hotkey(268435544, "Meta+X")})

    def test_str_is_the_portable_form(self) -> None:
        self.assertEqual("Meta+X", str(parse_hotkey("super+x")))


if __name__ == "__main__":
    unittest.main()
