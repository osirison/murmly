from __future__ import annotations

import os
import stat
import tempfile
import textwrap
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from murmly.config import (
    SETTING_RULES,
    SettingValueError,
    default_config_path,
    load_config,
    validate_setting,
)
from murmly.settings_writer import ConfigParseError, SettingsFileError, SettingsWriter

COMMENTED = textwrap.dedent(
    """\
    # Murmly settings, kept by hand.

    [stt]
    # Accuracy against speed.
    model_profile = "fast"  # tiny model
    device = "cpu"

    [overlay]
    enabled = true   # keep the pill

    [speakers]
    mode = "off"
    # 30 to 90
    match_threshold = 40
    """
)


class WriterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / "murmly" / "config.toml"

    def write_file(self, text: str | bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = text if isinstance(text, bytes) else text.encode("utf-8")
        self.path.write_bytes(data)


class RoundTripTests(WriterTestCase):
    def test_only_the_value_line_differs(self) -> None:
        self.write_file(COMMENTED)

        SettingsWriter(self.path).set("stt.model_profile", "accurate")

        expected = COMMENTED.replace(
            'model_profile = "fast"  # tiny model',
            'model_profile = "accurate"  # tiny model',
        )
        self.assertEqual(expected, self.path.read_text(encoding="utf-8"))

    def test_an_integer_changes_in_place_and_key_order_stays(self) -> None:
        self.write_file(COMMENTED)

        SettingsWriter(self.path).set("speakers.match_threshold", 55)

        self.assertEqual(
            COMMENTED.replace("match_threshold = 40", "match_threshold = 55"),
            self.path.read_text(encoding="utf-8"),
        )

    def test_a_missing_key_is_added_to_its_table(self) -> None:
        self.write_file(COMMENTED)

        SettingsWriter(self.path).set("stt.beam_size", 3)

        text = self.path.read_text(encoding="utf-8")
        self.assertEqual(3, tomllib.loads(text)["stt"]["beam_size"])
        self.assertIn("# Accuracy against speed.", text)
        self.assertIn("# 30 to 90", text)
        self.assertLess(text.index("[stt]"), text.index("beam_size"))
        self.assertLess(text.index("beam_size"), text.index("[overlay]"))

    def test_a_missing_table_is_added(self) -> None:
        self.write_file(COMMENTED)

        SettingsWriter(self.path).set("tts.voice", "af_heart")

        text = self.path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(COMMENTED.rstrip("\n")))
        self.assertEqual("af_heart", tomllib.loads(text)["tts"]["voice"])

    def test_a_missing_file_is_created_where_murmly_reads(self) -> None:
        result = SettingsWriter(self.path).set("overlay.enabled", False)

        self.assertTrue(result.created)
        self.assertEqual({"overlay": {"enabled": False}}, tomllib.loads(self.path.read_text()))
        self.assertFalse(load_config(self.path).overlay_enabled)

    def test_the_default_location_is_the_one_config_reads(self) -> None:
        # Whatever this platform resolves to, the writer's default is the reader's.
        env = {
            "XDG_CONFIG_HOME": str(self.directory / "xdg"),
            "APPDATA": str(self.directory / "appdata"),
            "HOME": str(self.directory),
        }

        self.assertEqual(default_config_path(env), SettingsWriter(env=env).path)
        self.assertEqual(default_config_path(env), load_config(env=env).config_path)

    def test_the_default_location_on_linux_follows_xdg(self) -> None:
        env = {"XDG_CONFIG_HOME": str(self.directory / "xdg"), "HOME": str(self.directory)}

        with patch("sys.platform", "linux"):
            path = SettingsWriter(env=env).path

        self.assertEqual(self.directory / "xdg" / "murmly" / "config.toml", path)

    def test_windows_line_endings_are_kept(self) -> None:
        crlf = COMMENTED.replace("\n", "\r\n")
        self.write_file(crlf)

        SettingsWriter(self.path).set("stt.model_profile", "balanced")

        self.assertEqual(
            crlf.replace('"fast"', '"balanced"').encode("utf-8"),
            self.path.read_bytes(),
        )

    def test_a_value_the_file_cannot_hold_as_a_table_is_refused_unchanged(self) -> None:
        self.write_file('stt = "oops"\n')

        with self.assertRaises(SettingsFileError):
            SettingsWriter(self.path).set("stt.model_profile", "fast")

        self.assertEqual(b'stt = "oops"\n', self.path.read_bytes())


class RefusalTests(WriterTestCase):
    def test_a_value_out_of_range_is_refused_with_the_range_and_nothing_written(self) -> None:
        self.write_file(COMMENTED)
        before = self.path.read_bytes()

        with self.assertRaises(SettingValueError) as caught:
            SettingsWriter(self.path).set("speakers.match_threshold", 95)

        self.assertIn("30 to 90", str(caught.exception))
        self.assertEqual(before, self.path.read_bytes())

    def test_a_value_outside_the_choices_names_them(self) -> None:
        self.write_file(COMMENTED)

        with self.assertRaises(SettingValueError) as caught:
            SettingsWriter(self.path).set("stt.model_profile", "huge")

        for name in ("fast", "balanced", "accurate"):
            self.assertIn(name, str(caught.exception))

    def test_a_refused_value_creates_no_file(self) -> None:
        with self.assertRaises(SettingValueError):
            SettingsWriter(self.path).set("tts.rate", 500)

        self.assertFalse(self.path.exists())

    def test_an_unknown_key_is_refused(self) -> None:
        with self.assertRaises(SettingValueError):
            SettingsWriter(self.path).set("stt.nonsense", 1)

    def test_a_file_that_does_not_parse_is_never_written(self) -> None:
        broken = b'[stt\nmodel_profile = "fast"\n'
        self.write_file(broken)
        with self.assertRaises(tomllib.TOMLDecodeError) as expected:
            tomllib.loads(broken.decode())

        with self.assertRaises(ConfigParseError) as caught:
            SettingsWriter(self.path).set("stt.model_profile", "accurate")

        self.assertEqual(str(expected.exception), str(caught.exception))
        self.assertEqual(broken, self.path.read_bytes())
        self.assertEqual([self.path.name], [entry.name for entry in self.path.parent.iterdir()])

    def test_reading_a_file_that_does_not_parse_reports_the_same_error(self) -> None:
        self.write_file(b"a = = 1\n")

        with self.assertRaises(ConfigParseError):
            SettingsWriter(self.path).read()


class ChangedOnDiskTests(WriterTestCase):
    def test_a_hand_edit_after_the_read_is_kept_and_the_caller_is_told(self) -> None:
        self.write_file(COMMENTED)
        writer = SettingsWriter(self.path)
        writer.read()
        edited = COMMENTED.replace("device = \"cpu\"", "device = \"cuda\"  # edited by hand")
        self.write_file(edited)

        result = writer.set("stt.model_profile", "accurate")

        self.assertTrue(result.file_changed)
        self.assertEqual(
            edited.replace('model_profile = "fast"', 'model_profile = "accurate"'),
            self.path.read_text(encoding="utf-8"),
        )

    def test_a_same_size_edit_is_still_noticed(self) -> None:
        self.write_file(COMMENTED)
        writer = SettingsWriter(self.path)
        writer.read()
        before = self.path.stat()
        self.write_file(COMMENTED.replace('"cpu"', '"gpu"'))
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns + 5_000_000_000))

        self.assertTrue(writer.set("stt.model_profile", "accurate").file_changed)

    def test_an_edit_that_keeps_the_size_and_the_modification_time_is_still_noticed(self) -> None:
        self.write_file(COMMENTED)
        writer = SettingsWriter(self.path)
        writer.read()
        before = self.path.stat()
        edited = COMMENTED.replace('"cpu"', '"gpu"')
        self.assertEqual(len(COMMENTED), len(edited))
        self.write_file(edited)
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = self.path.stat()
        self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))

        result = writer.set("stt.model_profile", "accurate")

        self.assertTrue(result.file_changed)
        self.assertIn('"gpu"', self.path.read_text(encoding="utf-8"))

    def test_an_unchanged_file_is_not_reported_as_changed(self) -> None:
        self.write_file(COMMENTED)
        writer = SettingsWriter(self.path)
        writer.read()

        self.assertFalse(writer.set("stt.model_profile", "accurate").file_changed)
        self.assertFalse(writer.set("stt.model_profile", "balanced").file_changed)

    def test_a_hand_edit_that_breaks_the_file_is_not_written_over(self) -> None:
        self.write_file(COMMENTED)
        writer = SettingsWriter(self.path)
        writer.read()
        self.write_file(b"[stt\n")

        with self.assertRaises(ConfigParseError):
            writer.set("stt.model_profile", "accurate")

        self.assertEqual(b"[stt\n", self.path.read_bytes())

    def test_an_edit_landing_during_the_write_is_picked_up(self) -> None:
        self.write_file(COMMENTED)
        writer = SettingsWriter(self.path)
        edited = COMMENTED.replace('"cpu"', '"cuda"')
        real_parse = writer._parse
        calls: list[int] = []

        def edit_once(text: str) -> dict[str, object]:
            result = real_parse(text)
            if not calls:
                calls.append(1)
                self.write_file(edited)
                os.utime(self.path, ns=(0, self.path.stat().st_mtime_ns + 5_000_000_000))
            return result

        writer._parse = edit_once  # type: ignore[method-assign]

        result = writer.set("stt.model_profile", "accurate")

        self.assertTrue(result.file_changed)
        self.assertIn('device = "cuda"', self.path.read_text())
        self.assertIn('model_profile = "accurate"', self.path.read_text())


class AtomicWriteTests(WriterTestCase):
    def test_no_temporary_file_is_left_behind(self) -> None:
        self.write_file(COMMENTED)

        SettingsWriter(self.path).set("stt.model_profile", "accurate")

        self.assertEqual([self.path.name], [entry.name for entry in self.path.parent.iterdir()])

    def test_a_failed_replace_leaves_the_file_and_no_temporary(self) -> None:
        self.write_file(COMMENTED)
        with patch("murmly.settings_writer.os.replace", side_effect=OSError("disk gone")):
            with self.assertRaises(SettingsFileError):
                SettingsWriter(self.path).set("stt.model_profile", "accurate")

        self.assertEqual(COMMENTED, self.path.read_text(encoding="utf-8"))
        self.assertEqual([self.path.name], [entry.name for entry in self.path.parent.iterdir()])

    @unittest.skipUnless(hasattr(os, "getuid"), "needs POSIX permission bits")
    def test_the_original_permission_bits_are_kept(self) -> None:
        self.write_file(COMMENTED)
        os.chmod(self.path, 0o640)

        SettingsWriter(self.path).set("stt.model_profile", "accurate")

        self.assertEqual(0o640, stat.S_IMODE(self.path.stat().st_mode))


class SharedValidationTests(unittest.TestCase):
    """The writer and the reader judge a value by the same rules."""

    BAD = {
        "stt.model_profile": ("huge", "balanced", lambda c: c.model_profile),
        "stt.device": ("tpu", "auto", lambda c: c.device),
        "stt.beam_size": (11, 5, lambda c: c.beam_size),
        "stt.unload_after_idle_s": (5, 300, lambda c: c.unload_after_idle_s),
        "stt.live_interval_ms": (10, 1000, lambda c: c.live_interval_ms),
        "overlay.text_size_px": (99, 13, lambda c: c.overlay_text_size_px),
        "tts.rate": (500, 100, lambda c: c.tts_rate_percent),
        "tts.voice": ("zz_nobody", "af_heart", lambda c: c.tts_voice),
        "speakers.mode": ("everyone", "off", lambda c: c.speaker_mode),
        "speakers.match_threshold": (10, 30, lambda c: c.speaker_match_threshold_percent),
    }

    def load(self, key: str, value: object):
        table, name = key.split(".")
        rendered = repr(value).replace("'", '"')
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(f"[{table}]\n{name} = {rendered}\n", encoding="utf-8")
            return load_config(path)

    def test_a_value_the_reader_sets_aside_is_refused_by_the_writer(self) -> None:
        for key, (bad, default, read) in self.BAD.items():
            with self.subTest(key=key):
                self.assertEqual(default, read(self.load(key, bad)))
                with self.assertRaises(SettingValueError):
                    validate_setting(key, bad)

    def test_the_edges_the_reader_honours_are_accepted_by_the_writer(self) -> None:
        good = {
            "stt.beam_size": (1, 10),
            "speakers.match_threshold": (30, 90),
            "tts.rate": (50, 200),
            "stt.unload_after_idle_s": (0, 30, 86_400),
        }
        reads = {
            "stt.beam_size": lambda c: c.beam_size,
            "speakers.match_threshold": lambda c: c.speaker_match_threshold_percent,
            "tts.rate": lambda c: c.tts_rate_percent,
            "stt.unload_after_idle_s": lambda c: c.unload_after_idle_s,
        }
        for key, values in good.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    validate_setting(key, value)
                    self.assertEqual(value, reads[key](self.load(key, value)))

    def test_quiet_hours_follow_the_readers_window_rules(self) -> None:
        validate_setting("tts.quiet_hours", "22:00-07:00")
        validate_setting("tts.quiet_hours", "")
        for bad in ("22:00-22:00", "25:00-07:00", "nightly", 2200):
            with self.subTest(bad=bad), self.assertRaises(SettingValueError):
                validate_setting("tts.quiet_hours", bad)

    def test_a_flag_must_be_a_real_boolean_and_a_number_not_a_boolean(self) -> None:
        with self.assertRaises(SettingValueError):
            validate_setting("overlay.enabled", "yes")
        with self.assertRaises(SettingValueError):
            validate_setting("stt.beam_size", True)

    def test_every_rule_says_what_it_accepts(self) -> None:
        for key, rule in SETTING_RULES.items():
            with self.subTest(key=key):
                self.assertTrue(rule.expectation)


if __name__ == "__main__":
    unittest.main()
