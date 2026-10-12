"""What Settings writes and shows, with no Qt: the control-to-key mapping, the
refusals, the unreadable file and the hand edit, all against a temporary file."""

from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path

from murmly import config
from murmly.app import settings
from murmly.settings_apply import RestartOutcome, RestartStatus
from murmly.settings_writer import SettingsWriter

ORIGINAL = """\
# Notes of my own, kept through every change.
[audio]
channels = 1 # mono

[stt]
model_profile = "balanced" # fast | balanced | accurate
unload_after_idle_s = 0
auto_transcribe = "off"

[tts]
quiet_hours = "22:00-07:00"
"""

# Every control, what it reports, and the one key and value that must reach the
# file. The first column is checked against `settings.CONTROLS`, so a control
# added without a row here fails the test that follows.
WRITES = [
    ("profile", "fast", "stt.model_profile", "fast"),
    ("stt-device", "cuda", "stt.device", "cuda"),
    ("unload", True, "stt.unload_after_idle_s", 300),
    ("finish", "continuous", "stt.auto_transcribe", "continuous"),
    ("pause", 750, "stt.auto_transcribe_silence_ms", 750),
    ("live", True, "stt.live_transcribe", True),
    ("verify", False, "clipboard.verify_target", False),
    ("restore", False, "clipboard.restore", False),
    ("overlay", False, "overlay.enabled", False),
    ("reduced", True, "overlay.reduced_motion", True),
    ("text-size", 20, "overlay.text_size_px", 20),
    ("speak", True, "tts.enabled", True),
    ("voice", "bf_emma", "tts.voice", "bf_emma"),
    ("rate", 125, "tts.rate", 125),
    ("quiet", False, "tts.quiet_hours", ""),
    ("quiet-from", "21:30", "tts.quiet_hours", "21:30-07:00"),
    ("quiet-to", "08:00", "tts.quiet_hours", "22:00-08:00"),
    ("tts-device", "cuda", "tts.device", "cuda"),
]


def lookup(data: dict, key: str) -> object:
    table, _, name = key.partition(".")
    return data.get(table, {}).get(name)


class SettingsCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "murmly" / "config.toml"
        self.path.parent.mkdir()
        self.path.write_text(ORIGINAL, encoding="utf-8")
        self.model = settings.SettingsModel(SettingsWriter(self.path))

    def data(self) -> dict:
        return tomllib.loads(self.path.read_text(encoding="utf-8"))


class EveryControlWritesItsKeyTests(SettingsCase):
    def test_the_table_covers_every_control_and_each_key_is_one_the_writer_accepts(self) -> None:
        self.assertEqual({row[0] for row in WRITES}, set(settings.CONTROL_BY_ID))
        for control in settings.CONTROLS:
            with self.subTest(control.id):
                self.assertIn(control.key, config.SETTING_RULES)

    def test_each_control_writes_its_documented_key_and_value_and_nothing_else(self) -> None:
        for control_id, raw, key, expected in WRITES:
            with self.subTest(control_id):
                self.path.write_text(ORIGINAL, encoding="utf-8")
                self.model.load()
                before = self.data()

                outcome, state = self.model.apply(control_id, raw)

                self.assertEqual("saved", outcome.status, outcome.message)
                self.assertEqual(key, outcome.key)
                after = self.data()
                self.assertEqual(expected, lookup(after, key))
                self.assertEqual(expected, state.values[key])
                # Only that key changed, and the person's own words are still there.
                table, _, name = key.partition(".")
                before.setdefault(table, {}).pop(name, None)
                after.setdefault(table, {}).pop(name, None)
                # A table the key was added to may be new; compare without empty ones.
                self.assertEqual(
                    {k: v for k, v in before.items() if v},
                    {k: v for k, v in after.items() if v},
                )
                text = self.path.read_text(encoding="utf-8")
                self.assertIn("# Notes of my own, kept through every change.", text)
                self.assertIn("channels = 1 # mono", text)

    def test_a_slider_that_reports_a_whole_float_writes_an_integer(self) -> None:
        outcome, _ = self.model.apply("pause", 1500.0)

        self.assertTrue(outcome.saved, outcome.message)
        self.assertIsInstance(lookup(self.data(), "stt.auto_transcribe_silence_ms"), int)

    def test_switching_memory_release_on_writes_the_default_period_and_off_writes_zero(self) -> None:
        self.model.apply("unload", True)
        self.assertEqual(config.DEFAULT_STT_UNLOAD_AFTER_IDLE_S, lookup(self.data(), "stt.unload_after_idle_s"))

        self.model.apply("unload", False)
        self.assertEqual(0, lookup(self.data(), "stt.unload_after_idle_s"))

    def test_switching_quiet_hours_on_writes_the_default_window_when_none_is_set(self) -> None:
        self.model.apply("quiet", False)
        self.model.apply("quiet", True)

        self.assertEqual(settings.DEFAULT_QUIET_WINDOW, lookup(self.data(), "tts.quiet_hours"))

    def test_a_control_this_page_does_not_have_is_refused_before_anything_is_written(self) -> None:
        before = self.path.read_bytes()

        with self.assertRaises(settings.UnknownControl):
            self.model.apply("overlay-show-target", True)

        self.assertEqual(before, self.path.read_bytes())


class RefusedValueTests(SettingsCase):
    def test_a_value_out_of_range_is_refused_with_the_range_and_the_file_is_unchanged(self) -> None:
        cases = [
            ("pause", 99, "250 to 30000"),
            ("rate", 500, "50 to 200"),
            ("text-size", 3, "8 to 48"),
            ("profile", "perfect", "fast"),
            ("voice", "xx_nobody", "af_heart"),
            ("quiet-from", "25:99", "HH:MM-HH:MM"),
        ]
        for control_id, raw, expected_range in cases:
            with self.subTest(control_id):
                before = self.path.read_bytes()
                self.model.load()

                outcome, state = self.model.apply(control_id, raw)

                self.assertEqual("refused", outcome.status)
                self.assertIn(expected_range, outcome.message)
                self.assertEqual(before, self.path.read_bytes())
                view = settings.build_view(state, outcome=outcome)
                self.assertEqual(outcome.message, view["message"])

    def test_two_equal_quiet_times_are_refused(self) -> None:
        before = self.path.read_bytes()

        outcome, _ = self.model.apply("quiet-to", "22:00")

        self.assertEqual("refused", outcome.status)
        self.assertEqual(before, self.path.read_bytes())


class UnreadableFileTests(SettingsCase):
    def broken(self) -> bytes:
        text = b'[stt\nmodel_profile = "fast"\n'
        self.path.write_bytes(text)
        return text

    def test_a_file_that_does_not_parse_shows_the_parsers_message_verbatim(self) -> None:
        text = self.broken()
        try:
            tomllib.loads(text.decode())
        except tomllib.TOMLDecodeError as error:
            parser_says = str(error)

        state = self.model.load()
        view = settings.build_view(state)

        self.assertTrue(state.parse_error)
        self.assertEqual(parser_says, state.error)
        self.assertEqual(parser_says, view["error"])
        self.assertTrue(view["disabled"])

    def test_a_change_to_a_file_that_does_not_parse_writes_nothing(self) -> None:
        text = self.broken()
        self.model.load()

        outcome, state = self.model.apply("live", True)

        self.assertEqual("unreadable", outcome.status)
        self.assertEqual(state.error, outcome.message)
        self.assertEqual(text, self.path.read_bytes())

    def test_the_page_recovers_once_the_file_parses_again(self) -> None:
        self.broken()
        self.assertTrue(self.model.load().error)

        self.path.write_text(ORIGINAL, encoding="utf-8")
        state = self.model.poll()

        self.assertIsNotNone(state)
        self.assertEqual("", state.error)
        self.assertFalse(settings.build_view(state)["disabled"])

    def test_a_file_that_cannot_be_opened_is_reported_in_the_systems_words(self) -> None:
        self.path.unlink()
        self.path.mkdir()

        state = self.model.load()

        self.assertFalse(state.parse_error)
        self.assertNotEqual("", state.error)
        self.assertTrue(settings.build_view(state)["disabled"])


class HandEditTests(SettingsCase):
    def test_a_change_after_a_hand_edit_keeps_the_edit_and_says_the_file_changed(self) -> None:
        self.model.load()
        self.path.write_text(ORIGINAL + "\n[overlay]\nbottom_margin_px = 99 # by hand\n", encoding="utf-8")

        outcome, state = self.model.apply("live", True)

        self.assertTrue(outcome.saved, outcome.message)
        self.assertTrue(outcome.file_changed)
        self.assertIn("bottom_margin_px = 99 # by hand", self.path.read_text(encoding="utf-8"))
        self.assertTrue(lookup(self.data(), "stt.live_transcribe"))
        view = settings.build_view(state, notice=settings.NOTICE_FILE_CHANGED, outcome=outcome)
        self.assertEqual(settings.NOTICE_FILE_CHANGED, view["notice"])

    def test_a_hand_edit_is_noticed_without_a_change_and_not_noticed_twice(self) -> None:
        self.model.load()
        self.assertIsNone(self.model.poll())

        self.path.write_text(ORIGINAL.replace('"fast"', '"accurate"').replace('"balanced"', '"accurate"'))
        state = self.model.poll()

        self.assertIsNotNone(state)
        self.assertTrue(state.changed)
        self.assertEqual("accurate", state.values["stt.model_profile"])
        self.assertIsNone(self.model.poll())

    def test_the_apps_own_write_is_not_mistaken_for_a_hand_edit(self) -> None:
        self.model.load()
        self.model.apply("live", True)

        self.assertIsNone(self.model.poll())


class ViewTests(SettingsCase):
    def test_a_key_the_file_leaves_out_shows_murmlys_default(self) -> None:
        state = self.model.load()

        self.assertEqual(config.DEFAULT_SILENCE_MS, state.values["stt.auto_transcribe_silence_ms"])
        self.assertEqual(False, state.values["tts.enabled"])
        self.assertEqual(config.DEFAULT_TTS_VOICE, state.values["tts.voice"])

    def test_the_view_holds_the_files_values_by_control(self) -> None:
        view = settings.build_view(self.model.load())

        values = view["values"]
        self.assertEqual("balanced", values["profile"])
        self.assertFalse(values["unload"])
        self.assertEqual("off", values["finish"])
        self.assertTrue(values["quiet"])
        self.assertEqual("22:00", values["quiet-from"])
        self.assertEqual("07:00", values["quiet-to"])
        self.assertEqual('stt.unload_after_idle_s = 0', view["keys"]["unload"])
        self.assertEqual('tts.quiet_hours = "22:00-07:00"', view["keys"]["quiet"])
        self.assertEqual("9 quiet hours, every day", view["quietSummary"])

    def test_the_pause_length_shows_only_when_a_pause_ends_a_recording(self) -> None:
        self.assertFalse(settings.build_view(self.model.load())["showPause"])

        self.model.apply("finish", "stop")

        self.assertTrue(settings.build_view(self.model.load())["showPause"])

    def test_a_voice_outside_the_four_drawn_is_listed_so_it_does_not_go_missing(self) -> None:
        self.model.apply("voice", "bm_george")

        view = settings.build_view(self.model.load())

        chosen = [voice for voice in view["options"]["voice"] if voice["checked"]]
        self.assertEqual(["bm_george"], [voice["id"] for voice in chosen])
        self.assertEqual(5, len(view["options"]["voice"]))

    def test_the_processor_warning_needs_live_words_on_the_processor_with_a_slower_profile(self) -> None:
        self.model.apply("live", True)
        self.model.apply("stt-device", "cpu")
        self.assertTrue(settings.build_view(self.model.load())["liveBehind"])

        self.model.apply("profile", "fast")
        self.assertFalse(settings.build_view(self.model.load())["liveBehind"])

    def test_the_footer_names_the_file_with_the_platforms_own_path(self) -> None:
        view = settings.build_view(self.model.load())

        self.assertEqual(f"saved to {self.path}", view["footer"])
        self.assertEqual(str(self.path), view["path"])

    def test_the_models_are_the_ones_the_file_names(self) -> None:
        options = settings.build_view(self.model.load())["options"]

        self.assertEqual(
            [config.MODEL_PROFILES[name].model_name for name in ("fast", "balanced", "accurate")],
            [option["detail"] for option in options["profile"]],
        )

    def test_the_status_line_says_when_a_change_waits_and_how_it_ended(self) -> None:
        state = self.model.load()

        waiting = settings.build_view(state, restart="waiting")["status"]
        done = settings.build_view(state, restart="restarted")["status"]
        failed = settings.build_view(state, restart="no such unit")["status"]

        self.assertIn("will apply when Murmly is idle", waiting)
        self.assertIn("restarted", done)
        self.assertIn("could not restart: no such unit", failed)

    def test_a_restart_attempt_maps_to_the_pages_words(self) -> None:
        self.assertIsNone(settings.restart_status(RestartOutcome(RestartStatus.NOTHING_PENDING)))
        self.assertEqual("waiting", settings.restart_status(RestartOutcome(RestartStatus.WAITING)))
        self.assertEqual("restarted", settings.restart_status(RestartOutcome(RestartStatus.RESTARTED)))
        self.assertEqual("boom", settings.restart_status(RestartOutcome(RestartStatus.FAILED, "boom")))

    def test_the_idle_period_is_said_in_words(self) -> None:
        self.assertEqual("five minutes", settings.idle_period(300))
        self.assertEqual("one minute", settings.idle_period(60))
        self.assertEqual("90 seconds", settings.idle_period(90))
        self.assertEqual("two hours", settings.idle_period(7200))


if __name__ == "__main__":
    unittest.main()
