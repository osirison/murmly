"""What the tray shows and decides, with no Qt: the four switches against a real
settings file, the icon, the panel's place and when a click shows it."""

from __future__ import annotations

import tempfile
import tomllib
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from murmly.app import home, settings, theme, tray
from murmly.settings_writer import SettingsWriter

ORIGINAL = """\
# my own notes
[stt]
auto_transcribe = "off"
auto_transcribe_silence_ms = 2000

[tts]
enabled = false
"""


def lookup(data: dict, key: str) -> object:
    table, _, name = key.partition(".")
    return data.get(table, {}).get(name)


class SwitchWriteTests(unittest.TestCase):
    """A click on a switch writes the key Settings writes for the same control."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "config.toml"
        self.path.write_text(ORIGINAL, encoding="utf-8")
        self.model = settings.SettingsModel(SettingsWriter(self.path))
        self.state = self.model.load()

    def click(self, switch_id: str) -> None:
        view = settings.build_view(self.state)
        control, value = tray.change_for(switch_id, view["values"])
        outcome, self.state = self.model.apply(control, value)
        self.assertTrue(outcome.saved, outcome.message)

    def written(self, key: str) -> object:
        return lookup(tomllib.loads(self.path.read_text(encoding="utf-8")), key)

    def test_each_switch_writes_its_key_and_value_and_back_again(self) -> None:
        cases = [
            # switch, key, value when switched on, value when switched off
            ("live", "stt.live_transcribe", True, False),
            ("finish", "stt.auto_transcribe", "stop", "off"),
            ("announce", "tts.enabled", True, False),
            ("quiet", "tts.quiet_hours", settings.DEFAULT_QUIET_WINDOW, ""),
        ]
        for switch_id, key, on_value, off_value in cases:
            with self.subTest(switch=switch_id):
                shown = tray.switches_view(settings.build_view(self.state))
                before = next(row["on"] for row in shown["switches"] if row["id"] == switch_id)
                self.click(switch_id)
                expected = off_value if before else on_value
                self.assertEqual(expected, self.written(key))
                self.click(switch_id)
                self.assertEqual(on_value if before else off_value, self.written(key))

    def test_a_switch_writes_the_same_key_as_its_settings_control(self) -> None:
        for switch in tray.SWITCHES:
            with self.subTest(switch=switch.id):
                self.assertIn(switch.control, settings.CONTROL_BY_ID)

    def test_there_are_four_switches_in_the_specs_order(self) -> None:
        self.assertEqual(["live", "finish", "announce", "quiet"], [switch.id for switch in tray.SWITCHES])

    def test_a_click_keeps_the_rest_of_the_file(self) -> None:
        self.click("live")
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# my own notes", text)
        self.assertEqual(2000, self.written("stt.auto_transcribe_silence_ms"))

    def test_auto_transcribe_is_on_for_both_pause_choices_and_off_only_for_off(self) -> None:
        self.assertFalse(tray.is_on("finish", {"finish": "off"}))
        self.assertTrue(tray.is_on("finish", {"finish": "stop"}))
        self.assertTrue(tray.is_on("finish", {"finish": "continuous"}))

    def test_switching_auto_transcribe_off_from_continuous_writes_off(self) -> None:
        self.assertEqual(("finish", "off"), tray.change_for("finish", {"finish": "continuous"}))
        self.assertEqual(("finish", "stop"), tray.change_for("finish", {"finish": "off"}))

    def click_remembering(self, switch_id: str, memory: tray.SwitchMemory) -> None:
        values = settings.build_view(self.state)["values"]
        memory.observe(values)
        control, value = tray.change_for(switch_id, values, memory)
        outcome, self.state = self.model.apply(control, value)
        self.assertTrue(outcome.saved, outcome.message)

    def set_file(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")
        self.state = self.model.load()

    def test_continuous_goes_off_then_on_and_comes_back_as_continuous(self) -> None:
        self.set_file('[stt]\nauto_transcribe = "continuous"\n')
        memory = tray.SwitchMemory()
        self.click_remembering("finish", memory)
        self.assertEqual("off", self.written("stt.auto_transcribe"))
        self.click_remembering("finish", memory)
        self.assertEqual("continuous", self.written("stt.auto_transcribe"))

    def test_a_custom_quiet_window_comes_back_after_off_then_on(self) -> None:
        self.set_file('[tts]\nquiet_hours = "23:30-06:00"\n')
        memory = tray.SwitchMemory()
        self.click_remembering("quiet", memory)
        self.assertEqual("", self.written("tts.quiet_hours"))
        self.click_remembering("quiet", memory)
        self.assertEqual("23:30-06:00", self.written("tts.quiet_hours"))

    def test_with_nothing_known_the_defaults_apply(self) -> None:
        self.set_file('[stt]\nauto_transcribe = "off"\n\n[tts]\nquiet_hours = ""\n')
        memory = tray.SwitchMemory()
        self.click_remembering("finish", memory)
        self.assertEqual("stop", self.written("stt.auto_transcribe"))
        self.click_remembering("quiet", memory)
        self.assertEqual(settings.DEFAULT_QUIET_WINDOW, self.written("tts.quiet_hours"))

    def test_a_forgotten_memory_falls_back_to_the_defaults(self) -> None:
        self.set_file('[stt]\nauto_transcribe = "off"\n\n[tts]\nquiet_hours = ""\n')
        self.assertEqual(("finish", "stop"), tray.change_for("finish", {"finish": "off"}, tray.SwitchMemory()))

    def test_settings_own_quiet_switch_is_unchanged(self) -> None:
        self.assertEqual(settings.DEFAULT_QUIET_WINDOW, settings.to_value("quiet", True, {}))
        self.assertEqual("", settings.to_value("quiet", False, {}))

    def test_an_unknown_switch_is_refused(self) -> None:
        with self.assertRaises(tray.UnknownSwitch):
            tray.change_for("verify", {})
        with self.assertRaises(tray.UnknownSwitch):
            tray.is_on("verify", {})


class SwitchViewTests(unittest.TestCase):
    def test_before_the_file_is_read_every_switch_is_off_and_disabled(self) -> None:
        view = tray.switches_view(settings.build_view(None))
        self.assertTrue(view["disabled"])
        self.assertFalse(view["loaded"])
        self.assertEqual([False] * 4, [row["on"] for row in view["switches"]])

    def test_a_file_that_cannot_be_written_disables_the_switches(self) -> None:
        state = settings.SettingsState(Path("config.toml"), error="Expected '=' at line 3", parse_error=True)
        view = tray.switches_view(settings.build_view(state))
        self.assertTrue(view["disabled"])

    def test_the_notes_say_what_each_switch_does_now(self) -> None:
        values: dict[str, object] = {
            "stt.live_transcribe": False,
            "stt.auto_transcribe": "off",
            "stt.auto_transcribe_silence_ms": 2000,
            "tts.enabled": False,
            "tts.quiet_hours": "",
        }
        state = settings.SettingsState(Path("config.toml"), values=values)
        rows = {row["id"]: row for row in tray.switches_view(settings.build_view(state))["switches"]}
        self.assertEqual("press the keys again", rows["finish"]["note"])
        self.assertEqual("off", rows["quiet"]["note"])
        values["stt.auto_transcribe"] = "stop"
        values["stt.auto_transcribe_silence_ms"] = 2000
        values["tts.quiet_hours"] = "21:30-07:00"
        state = settings.SettingsState(Path("config.toml"), values=values)
        rows = {row["id"]: row for row in tray.switches_view(settings.build_view(state))["switches"]}
        self.assertEqual("after 2 s of quiet", rows["finish"]["note"])
        self.assertEqual("21:30 to 07:00", rows["quiet"]["note"])

    def test_a_refusal_and_the_restart_line_are_passed_on_in_the_writers_words(self) -> None:
        state = settings.SettingsState(Path("config.toml"), values={})
        outcome = settings.SaveOutcome("refused", "stt.live_transcribe", "That value is not allowed.")
        view = tray.switches_view(settings.build_view(state, outcome=outcome, restart="waiting"))
        self.assertEqual("That value is not allowed.", view["message"])
        self.assertIn("will apply when Murmly is idle", view["status"])


class IconTests(unittest.TestCase):
    def palette(self, scheme: str = "dark") -> dict[str, str]:
        return theme.palette(scheme)

    def test_every_presentation_has_an_icon_in_both_schemes_and_it_is_valid_svg(self) -> None:
        for scheme in theme.SCHEMES:
            for presentation in tray.ICON_STATES:
                with self.subTest(scheme=scheme, presentation=presentation):
                    root = ET.fromstring(tray.icon_svg(presentation, self.palette(scheme)))
                    self.assertTrue(root.tag.endswith("svg"))

    def test_the_states_use_the_themes_colour_meanings(self) -> None:
        colors = self.palette()
        self.assertIn(colors["listeningLabel"], tray.icon_svg(home.LISTENING, colors))
        self.assertIn(colors["writingLabel"], tray.icon_svg(home.WRITING, colors))
        self.assertIn(colors["needsYou"], tray.icon_svg(home.NEEDS_YOU, colors))
        self.assertIn(colors["needsYou"], tray.icon_svg(home.NOT_RUNNING, colors))

    def test_crimson_is_a_ring_never_a_fill(self) -> None:
        colors = self.palette()
        for presentation in (home.NEEDS_YOU, home.NOT_RUNNING):
            with self.subTest(presentation=presentation):
                svg = tray.icon_svg(presentation, colors)
                self.assertIn(f'stroke="{colors["needsYou"]}"', svg)
                self.assertNotIn(f'fill="{colors["needsYou"]}"', svg)

    def test_ready_and_neutral_are_the_mark_alone_and_not_running_dims_it(self) -> None:
        colors = self.palette()
        self.assertEqual(tray.icon_svg(home.READY, colors), tray.icon_svg(tray.NEUTRAL, colors))
        self.assertNotIn("<circle", tray.icon_svg(home.READY, colors))
        self.assertIn(colors["lineInert"], tray.icon_svg(home.NOT_RUNNING, colors))
        self.assertNotIn(colors["lineInert"], tray.icon_svg(home.NEEDS_YOU, colors))

    def test_the_mark_follows_the_scheme(self) -> None:
        dark = tray.icon_svg(home.READY, self.palette("dark"))
        light = tray.icon_svg(home.READY, self.palette("light"))
        self.assertNotEqual(dark, light)
        self.assertIn(self.palette("dark")["textBright"], dark)
        self.assertIn(self.palette("light")["textBright"], light)

    def test_the_icon_is_neutral_while_nothing_polls_or_nothing_has_answered(self) -> None:
        self.assertEqual(tray.NEUTRAL, tray.icon_presentation({"state": "listening"}, polling=False))
        self.assertEqual(tray.NEUTRAL, tray.icon_presentation({}, polling=True))
        self.assertEqual("listening", tray.icon_presentation({"state": "listening"}, polling=True))

    def test_the_tooltip_is_the_homes_chip_text_or_just_the_name(self) -> None:
        self.assertEqual("murmly: listening", tray.tooltip("listening", {"chip": "listening"}))
        self.assertEqual("murmly", tray.tooltip(tray.NEUTRAL, {"chip": "listening"}))


class PlacementTests(unittest.TestCase):
    SCREEN = tray.Rect(0, 0, 1920, 1080)
    SIZE = (360, 500)

    def test_a_bottom_taskbar_puts_the_panel_above_the_icon(self) -> None:
        available = tray.Rect(0, 0, 1920, 1040)
        x, y = tray.place_panel(tray.Rect(1800, 1040, 24, 40), (0, 0), self.SIZE, available)
        self.assertEqual(1040 - 500 - tray.PANEL_GAP, y)
        self.assertEqual(1920 - 360, x)

    def test_a_top_taskbar_puts_the_panel_below_the_icon(self) -> None:
        available = tray.Rect(0, 40, 1920, 1040)
        x, y = tray.place_panel(tray.Rect(1800, 0, 24, 40), (0, 0), self.SIZE, available)
        self.assertEqual(40 + tray.PANEL_GAP, y)
        self.assertEqual(1920 - 360, x)

    def test_a_right_taskbar_puts_the_panel_to_the_left_of_the_icon(self) -> None:
        available = tray.Rect(0, 0, 1880, 1080)
        x, y = tray.place_panel(tray.Rect(1880, 900, 40, 24), (0, 0), self.SIZE, available)
        self.assertEqual(1880 - 360 - tray.PANEL_GAP, x)
        self.assertTrue(0 <= y <= 1080 - 500)

    def test_an_icon_with_no_rectangle_uses_the_cursor(self) -> None:
        x, y = tray.place_panel(tray.Rect(0, 0, 0, 0), (960, 1070), self.SIZE, self.SCREEN)
        self.assertEqual(960 - 180, x)
        self.assertEqual(1070 - 500 - tray.PANEL_GAP, y)
        self.assertEqual((x, y), tray.place_panel(None, (960, 1070), self.SIZE, self.SCREEN))

    def test_the_panel_stays_on_the_screen_in_every_corner(self) -> None:
        for point in ((0, 0), (1919, 0), (0, 1079), (1919, 1079)):
            with self.subTest(point=point):
                x, y = tray.place_panel(None, point, self.SIZE, self.SCREEN)
                self.assertTrue(0 <= x <= 1920 - 360)
                self.assertTrue(0 <= y <= 1080 - 500)

    def test_a_screen_smaller_than_the_panel_pins_it_to_the_corner(self) -> None:
        x, y = tray.place_panel(None, (100, 100), (360, 900), tray.Rect(0, 0, 800, 600))
        self.assertEqual(0, y)
        self.assertTrue(0 <= x <= 800)


class PanelToggleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 100.0
        self.toggle = tray.PanelToggle(clock=lambda: self.now)

    def test_a_click_on_a_hidden_panel_shows_it(self) -> None:
        self.assertTrue(self.toggle.should_show(visible=False))

    def test_a_click_on_a_visible_panel_does_not_show_it_again(self) -> None:
        self.assertFalse(self.toggle.should_show(visible=True))

    def test_the_click_that_made_it_hide_does_not_show_it_again(self) -> None:
        self.toggle.hid_itself()
        self.now += 0.1
        self.assertFalse(self.toggle.should_show(visible=False))
        # Only that one click: the next shows it.
        self.assertTrue(self.toggle.should_show(visible=False))

    def test_a_later_click_shows_it(self) -> None:
        self.toggle.hid_itself()
        self.now += tray.REOPEN_GUARD_S + 0.1
        self.assertTrue(self.toggle.should_show(visible=False))


class CloseWindowTests(unittest.TestCase):
    def test_closing_the_window_quits_only_when_there_is_no_tray(self) -> None:
        self.assertTrue(tray.quits_when_window_closes(False))
        self.assertFalse(tray.quits_when_window_closes(True))


if __name__ == "__main__":
    unittest.main()
