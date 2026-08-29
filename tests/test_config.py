from __future__ import annotations

import dataclasses
import re
import tempfile
import textwrap
import unittest
from pathlib import Path

import murmly.config

from murmly.config import (
    DEFAULT_LIVE_INTERVAL_MS,
    DEFAULT_LIVE_WINDOW_SECONDS,
    DEFAULT_MIN_SPEECH_MS,
    DEFAULT_OVERLAY_TEXT_SIZE_PX,
    DEFAULT_RESTORE_DELAY_MS,
    DEFAULT_SILENCE_MS,
    DEFAULT_STT_UNLOAD_AFTER_IDLE_S,
    DEFAULT_TTS_DEVICE,
    DEFAULT_TTS_UNLOAD_AFTER_IDLE_S,
    MAX_LIVE_INTERVAL_MS,
    MAX_LIVE_WINDOW_SECONDS,
    MAX_OVERLAY_TEXT_SIZE_PX,
    MAX_RESTORE_DELAY_MS,
    MAX_SILENCE_MS,
    MAX_UNLOAD_AFTER_IDLE_S,
    MIN_LIVE_INTERVAL_MS,
    MIN_OVERLAY_TEXT_SIZE_PX,
    MIN_SILENCE_MS,
    MIN_UNLOAD_AFTER_IDLE_S,
    default_socket_path,
    load_config,
)


class ConfigTests(unittest.TestCase):
    def test_default_socket_path_uses_runtime_dir(self) -> None:
        socket_path = default_socket_path({"XDG_RUNTIME_DIR": "/tmp/runtime"})
        self.assertEqual(Path("/tmp/runtime/murmly.sock"), socket_path)

    def test_load_config_reads_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    [daemon]
                    socket_path = "/tmp/custom.sock"

                    [audio]
                    sample_rate_hz = 16000
                    channels = 1

                    [stt]
                    model_profile = "accurate"
                    device = "cpu"
                    compute_type = "float32"
                    beam_size = 3
                    vad_filter = false
                    lazy_load_model = false

                    [clipboard]
                    restore = false
                    restore_delay_ms = 150

                    [overlay]
                    enabled = false
                    bottom_margin_px = 48
                    reduced_motion = true
                    """
                ).strip()
            )

            config = load_config(config_path)

        self.assertEqual(Path("/tmp/custom.sock"), config.socket_path)
        self.assertEqual("accurate", config.model_profile)
        self.assertEqual("large-v3", config.model_name)
        self.assertEqual("cpu", config.device)
        self.assertEqual("float32", config.compute_type)
        self.assertEqual(3, config.beam_size)
        self.assertFalse(config.vad_filter)
        self.assertFalse(config.lazy_load_model)
        self.assertFalse(config.restore_clipboard)
        self.assertEqual(150, config.restore_clipboard_delay_ms)
        self.assertFalse(config.overlay_enabled)
        self.assertEqual(48, config.overlay_bottom_margin_px)
        self.assertTrue(config.overlay_reduced_motion)

    def test_balanced_profile_uses_accuracy_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = load_config(Path(temp_dir) / "missing.toml")

        self.assertEqual("large-v3-turbo", config.model_name)
        self.assertEqual("0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf", config.model_revision)
        self.assertEqual("auto", config.device)
        self.assertEqual("auto", config.compute_type)
        self.assertEqual(5, config.beam_size)
        self.assertTrue(config.vad_filter)
        self.assertTrue(config.overlay_enabled)
        self.assertEqual(32, config.overlay_bottom_margin_px)
        self.assertFalse(config.overlay_reduced_motion)

    def test_invalid_overlay_table_uses_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text('overlay = "invalid"')

            config = load_config(config_path)

        self.assertTrue(config.overlay_enabled)
        self.assertEqual(32, config.overlay_bottom_margin_px)
        self.assertFalse(config.overlay_reduced_motion)

    def test_invalid_overlay_values_use_defaults(self) -> None:
        for margin in (-1, 513):
            with self.subTest(margin=margin), tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.toml"
                config_path.write_text(
                    textwrap.dedent(
                        f"""
                        [overlay]
                        enabled = "yes"
                        bottom_margin_px = {margin}
                        reduced_motion = 1
                        """
                    ).strip()
                )

                config = load_config(config_path)

            self.assertTrue(config.overlay_enabled)
            self.assertEqual(32, config.overlay_bottom_margin_px)
            self.assertFalse(config.overlay_reduced_motion)

    def test_verify_target_defaults_to_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = load_config(Path(temp_dir) / "missing.toml")

        self.assertTrue(config.verify_target)

    def test_verify_target_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    [clipboard]
                    verify_target = false
                    """
                ).strip()
            )

            config = load_config(config_path)

        self.assertFalse(config.verify_target)

    def test_invalid_verify_target_values_use_defaults(self) -> None:
        for value in ('"no"', "0", "[]"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.toml"
                config_path.write_text(
                    textwrap.dedent(
                        f"""
                        [clipboard]
                        verify_target = {value}
                        """
                    ).strip()
                )

                config = load_config(config_path)

            self.assertTrue(config.verify_target)

    def test_invalid_clipboard_table_uses_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text('clipboard = "invalid"')

            config = load_config(config_path)

        self.assertTrue(config.verify_target)
        self.assertTrue(config.restore_clipboard)

    def test_restore_delay_defaults_to_five_hundred_milliseconds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = load_config(Path(temp_dir) / "missing.toml")

        self.assertEqual(DEFAULT_RESTORE_DELAY_MS, config.restore_clipboard_delay_ms)
        self.assertEqual(500, config.restore_clipboard_delay_ms)

    def test_restore_delay_accepts_an_in_range_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text("[clipboard]\nrestore_delay_ms = 1200")

            config = load_config(config_path)

        self.assertEqual(1_200, config.restore_clipboard_delay_ms)

    def test_out_of_range_restore_delay_falls_back_to_the_default(self) -> None:
        for value in (-1, MAX_RESTORE_DELAY_MS + 1, 999_999_999):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.toml"
                config_path.write_text(f"[clipboard]\nrestore_delay_ms = {value}")

                config = load_config(config_path)

            self.assertEqual(DEFAULT_RESTORE_DELAY_MS, config.restore_clipboard_delay_ms)

    def test_restore_delay_never_exceeds_the_supported_maximum(self) -> None:
        for value in (0, 250, MAX_RESTORE_DELAY_MS, MAX_RESTORE_DELAY_MS + 5_000, -50, "soon"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.toml"
                rendered = f'"{value}"' if isinstance(value, str) else value
                config_path.write_text(f"[clipboard]\nrestore_delay_ms = {rendered}")

                config = load_config(config_path)

            self.assertGreaterEqual(config.restore_clipboard_delay_ms, 0)
            self.assertLessEqual(config.restore_clipboard_delay_ms, MAX_RESTORE_DELAY_MS)

    def test_live_and_auto_transcribe_default_to_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = load_config(Path(temp_dir) / "missing.toml")

        self.assertFalse(config.live_transcribe)
        self.assertEqual("off", config.auto_transcribe)
        self.assertIsNone(config.auto_transcribe_rejected_value)
        self.assertEqual(DEFAULT_LIVE_INTERVAL_MS, config.live_interval_ms)
        self.assertEqual(DEFAULT_LIVE_WINDOW_SECONDS, config.live_window_seconds)
        self.assertEqual(2_000, config.auto_transcribe_silence_ms)
        self.assertEqual(DEFAULT_SILENCE_MS, config.auto_transcribe_silence_ms)
        self.assertEqual(DEFAULT_MIN_SPEECH_MS, config.auto_transcribe_min_speech_ms)

    def test_live_and_auto_transcribe_read_valid_values(self) -> None:
        for mode in ("off", "stop", "continuous"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.toml"
                config_path.write_text(
                    textwrap.dedent(
                        f"""
                        [stt]
                        live_transcribe = true
                        live_interval_ms = 750
                        live_window_seconds = 20
                        auto_transcribe = "{mode}"
                        auto_transcribe_silence_ms = 1500
                        auto_transcribe_min_speech_ms = 500
                        """
                    ).strip()
                )

                config = load_config(config_path)

            self.assertTrue(config.live_transcribe)
            self.assertEqual(750, config.live_interval_ms)
            self.assertEqual(20, config.live_window_seconds)
            self.assertEqual(mode, config.auto_transcribe)
            self.assertIsNone(config.auto_transcribe_rejected_value)
            self.assertEqual(1_500, config.auto_transcribe_silence_ms)
            self.assertEqual(500, config.auto_transcribe_min_speech_ms)

    def test_unrecognized_auto_transcribe_mode_falls_back_and_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text('[stt]\nauto_transcribe = "whenever"')

            config = load_config(config_path)

        self.assertEqual("off", config.auto_transcribe)
        self.assertEqual("whenever", config.auto_transcribe_rejected_value)

    def test_out_of_range_live_and_silence_values_fall_back_to_defaults(self) -> None:
        for interval, silence in (
            (MIN_LIVE_INTERVAL_MS - 1, MIN_SILENCE_MS - 1),
            (MAX_LIVE_INTERVAL_MS + 1, MAX_SILENCE_MS + 1),
        ):
            with self.subTest(interval=interval), tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.toml"
                config_path.write_text(
                    textwrap.dedent(
                        f"""
                        [stt]
                        live_interval_ms = {interval}
                        live_window_seconds = {MAX_LIVE_WINDOW_SECONDS + 1}
                        auto_transcribe_silence_ms = {silence}
                        auto_transcribe_min_speech_ms = -5
                        """
                    ).strip()
                )

                config = load_config(config_path)

            self.assertEqual(DEFAULT_LIVE_INTERVAL_MS, config.live_interval_ms)
            self.assertEqual(DEFAULT_LIVE_WINDOW_SECONDS, config.live_window_seconds)
            self.assertEqual(DEFAULT_SILENCE_MS, config.auto_transcribe_silence_ms)
            self.assertEqual(DEFAULT_MIN_SPEECH_MS, config.auto_transcribe_min_speech_ms)

    def test_overlay_text_size_defaults_and_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = load_config(Path(temp_dir) / "missing.toml")
        self.assertEqual(DEFAULT_OVERLAY_TEXT_SIZE_PX, config.overlay_text_size_px)

        for value, expected in (
            (24, 24),
            (MIN_OVERLAY_TEXT_SIZE_PX - 1, DEFAULT_OVERLAY_TEXT_SIZE_PX),
            (MAX_OVERLAY_TEXT_SIZE_PX + 1, DEFAULT_OVERLAY_TEXT_SIZE_PX),
            ('"large"', DEFAULT_OVERLAY_TEXT_SIZE_PX),
        ):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.toml"
                config_path.write_text(f"[overlay]\ntext_size_px = {value}")

                config = load_config(config_path)

            self.assertEqual(expected, config.overlay_text_size_px)

    def test_invalid_live_transcribe_value_uses_the_default(self) -> None:
        for value in ('"yes"', "1", "[]"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.toml"
                config_path.write_text(f"[stt]\nlive_transcribe = {value}")

                config = load_config(config_path)

            self.assertFalse(config.live_transcribe)

    def test_invalid_stt_settings_fall_back_to_profile_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    [stt]
                    model_profile = "fast"
                    device = "remote"
                    compute_type = "unsafe"
                    beam_size = 1000000
                    """
                ).strip()
            )

            config = load_config(config_path)

        self.assertEqual("tiny.en", config.model_name)
        self.assertIsNone(config.model_revision)
        self.assertEqual("auto", config.device)
        self.assertEqual("auto", config.compute_type)
        self.assertEqual(1, config.beam_size)
        self.assertFalse(config.vad_filter)

    def test_synthesis_runs_on_the_cpu_when_no_device_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = load_config(Path(temp_dir) / "missing.toml")

        self.assertEqual("cpu", config.tts_device)
        self.assertEqual(DEFAULT_TTS_DEVICE, config.tts_device)
        self.assertIsNone(config.tts_device_rejected_value)

    def test_the_transcription_device_does_not_decide_the_synthesis_device(self) -> None:
        """The two settings are independent, which is the point of adding one."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text('[stt]\ndevice = "cuda"')

            config = load_config(config_path)

        self.assertEqual("cuda", config.device)
        self.assertEqual("cpu", config.tts_device)

    def test_every_device_transcription_accepts_is_accepted_for_synthesis(self) -> None:
        for device in ("auto", "cpu", "cuda"):
            with self.subTest(device=device), tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.toml"
                config_path.write_text(f'[tts]\ndevice = "{device}"')

                config = load_config(config_path)

            self.assertEqual(device, config.tts_device)
            self.assertIsNone(config.tts_device_rejected_value)

    def test_unrecognized_synthesis_device_falls_back_and_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text('[tts]\ndevice = "rocm"')

            config = load_config(config_path)

        self.assertEqual("cpu", config.tts_device)
        self.assertEqual("rocm", config.tts_device_rejected_value)

    def test_transcription_release_is_on_and_synthesis_release_off_without_configuration(
        self,
    ) -> None:
        """The two defaults deliberately disagree, so one test pins both together.

        Transcription returns accelerator memory and reloads while the person is
        still speaking; synthesis returns system memory and costs silence with
        nothing to overlap it. A later change that quietly gave them one shared
        default would still satisfy either assertion on its own.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            config = load_config(Path(temp_dir) / "missing.toml")

        self.assertEqual(DEFAULT_STT_UNLOAD_AFTER_IDLE_S, config.unload_after_idle_s)
        self.assertEqual(300, config.unload_after_idle_s)
        self.assertEqual(DEFAULT_TTS_UNLOAD_AFTER_IDLE_S, config.tts_unload_after_idle_s)
        self.assertEqual(0, config.tts_unload_after_idle_s)

    def test_an_explicit_zero_disables_release_for_either_model(self) -> None:
        """Zero lies below the supported minimum, and must not be read as out of range.

        The generic `_bounded_int` answers its default for anything outside the
        bounds, so putting an idle period through it would turn the setting that
        switches release off into the one that switches it on after five minutes.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    [stt]
                    unload_after_idle_s = 0

                    [tts]
                    unload_after_idle_s = 0
                    """
                ).strip()
            )

            config = load_config(config_path)

        self.assertEqual(0, config.unload_after_idle_s)
        self.assertEqual(0, config.tts_unload_after_idle_s)

    def test_an_out_of_range_idle_period_falls_back_to_that_settings_own_default(self) -> None:
        """Both are wrong in the same file, because a shared fallback passes either alone."""
        for stt_value, tts_value in (
            (MIN_UNLOAD_AFTER_IDLE_S - 1, MAX_UNLOAD_AFTER_IDLE_S + 1),
            (MAX_UNLOAD_AFTER_IDLE_S + 1, MIN_UNLOAD_AFTER_IDLE_S - 1),
            (-60, '"never"'),
            ('"forever"', 999_999_999),
        ):
            with (
                self.subTest(stt=stt_value, tts=tts_value),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                config_path = Path(temp_dir) / "config.toml"
                config_path.write_text(
                    textwrap.dedent(
                        f"""
                        [stt]
                        unload_after_idle_s = {stt_value}

                        [tts]
                        unload_after_idle_s = {tts_value}
                        """
                    ).strip()
                )

                config = load_config(config_path)

            self.assertEqual(DEFAULT_STT_UNLOAD_AFTER_IDLE_S, config.unload_after_idle_s)
            self.assertEqual(DEFAULT_TTS_UNLOAD_AFTER_IDLE_S, config.tts_unload_after_idle_s)

    def test_a_fractional_idle_period_falls_back_rather_than_disabling_release(self) -> None:
        """Only an exact zero means never.

        The zero check used to truncate, so 0.5 and -0.9 read as zero and
        switched release off. For `[stt]`, whose default is 300, that turned a
        mistyped period into a silently disabled feature -- the same inversion
        the helper exists to prevent, one layer further in. These values are all
        outside the bounds and are not zero, so each takes its setting's own
        default.
        """
        for value in ("0.5", "0.99", "-0.9", "-0.5", "29.9"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.toml"
                config_path.write_text(
                    textwrap.dedent(
                        f"""
                        [stt]
                        unload_after_idle_s = {value}

                        [tts]
                        unload_after_idle_s = {value}
                        """
                    ).strip()
                )

                config = load_config(config_path)

                self.assertEqual(
                    DEFAULT_STT_UNLOAD_AFTER_IDLE_S,
                    config.unload_after_idle_s,
                    f"{value} disabled transcription release instead of falling back",
                )
                self.assertEqual(
                    DEFAULT_TTS_UNLOAD_AFTER_IDLE_S, config.tts_unload_after_idle_s
                )

    def test_an_idle_period_within_the_bounds_is_used_as_written(self) -> None:
        for value in (MIN_UNLOAD_AFTER_IDLE_S, 300, 3_600, MAX_UNLOAD_AFTER_IDLE_S):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.toml"
                config_path.write_text(
                    f"[stt]\nunload_after_idle_s = {value}\n\n[tts]\nunload_after_idle_s = {value}"
                )

                config = load_config(config_path)

            self.assertEqual(value, config.unload_after_idle_s)
            self.assertEqual(value, config.tts_unload_after_idle_s)

    def test_either_model_can_be_released_while_the_other_stays_resident(self) -> None:
        """The reverse of the shipped defaults, which no fallback path can produce."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    [stt]
                    unload_after_idle_s = 0

                    [tts]
                    unload_after_idle_s = 900
                    """
                ).strip()
            )

            config = load_config(config_path)

        self.assertEqual(0, config.unload_after_idle_s)
        self.assertEqual(900, config.tts_unload_after_idle_s)


REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG_PATH = REPO_ROOT / "config.example.toml"
README_PATH = REPO_ROOT / "README.md"


def option_keys(text: str) -> set[tuple[str, str]]:
    """Every (table, key) named in a TOML sample, commented out or not."""
    keys: set[tuple[str, str]] = set()
    table = ""
    for line in text.splitlines():
        header = re.fullmatch(r"\[(\w+)\]", line.strip())
        if header:
            table = header.group(1)
            continue
        entry = re.match(r"#?\s*(\w+)\s*=", line.strip())
        if entry and table:
            keys.add((table, entry.group(1)))
    return keys


class ExampleConfigTests(unittest.TestCase):
    """The shipped example has to stay a faithful copy of the real defaults."""

    def test_example_config_holds_nothing_but_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            defaults = load_config(Path(temp_dir) / "missing.toml")
        example = load_config(EXAMPLE_CONFIG_PATH)

        self.assertEqual(
            dataclasses.replace(defaults, config_path=EXAMPLE_CONFIG_PATH),
            example,
        )

    def test_example_config_names_every_option_the_loader_reads(self) -> None:
        """A misspelled key is silently ignored, so equality alone proves nothing."""
        source = Path(murmly.config.__file__).read_text()
        read_keys = set(
            re.findall(
                r"\b(daemon|audio|stt|clipboard|overlay|tts)\.get\(\s*\"(\w+)\"",
                source,
            )
        )

        self.assertTrue(read_keys, "no option lookups found in config.py")
        self.assertEqual(read_keys, option_keys(EXAMPLE_CONFIG_PATH.read_text()))

    def test_readme_sample_covers_the_same_options(self) -> None:
        block = re.search(
            r"## Configuration\b.*?```toml\n(.*?)```",
            README_PATH.read_text(),
            re.DOTALL,
        )

        self.assertIsNotNone(block, "no TOML sample under README's Configuration heading")
        self.assertEqual(
            option_keys(EXAMPLE_CONFIG_PATH.read_text()),
            option_keys(block.group(1)),
        )

    def test_the_speech_instructions_never_name_a_command_that_breaks_them(self) -> None:
        """The install prose is read and then run, so a stale command is a bug.

        This guarded the opposite rule until speech output became a default
        dependency group. `uv sync` is exact about extras, so while the
        synthesizer was one, every recipe had to name `--extra tts` alongside
        `--extra cuda` or following it literally produced an environment with no
        synthesizer. That is what happened in the field, three minutes after a
        daemon started, and it is why the packaging changed.

        The two failure modes now. `--extra tts` names an extra that no longer
        exists, so a reader who runs it gets an error rather than a synthesizer.
        `--no-group tts` is the one command that removes speech output, so an
        install recipe naming it installs nothing.

        Nothing else in the suite reads prose, and the same instructions are
        mirrored in three files.
        """
        project_root = Path(murmly.config.__file__).parent.parent.parent
        sections = {
            "README.md": re.search(
                r"^## Speech output\b(.*?)(?=^## )", README_PATH.read_text(), re.DOTALL | re.MULTILINE
            ),
            "config.example.toml": re.search(
                r"^\[tts\]\n(.*)", EXAMPLE_CONFIG_PATH.read_text(), re.DOTALL | re.MULTILINE
            ),
            "pyproject.toml": re.search(
                r"^# Installed by default(.*?)^default-groups",
                (project_root / "pyproject.toml").read_text(),
                re.DOTALL | re.MULTILINE,
            ),
        }
        for name, section in sections.items():
            self.assertIsNotNone(section, f"no speech section found in {name}")
            body = section.group(0)
            for line in re.findall(r"^[#\s]*(uv sync [^\n]*)$", body, re.MULTILINE):
                self.assertNotIn(
                    "--extra tts",
                    line,
                    f"{name}: {line.strip()!r} names an extra that no longer exists",
                )
                if "--no-group tts" in line:
                    self.fail(
                        f"{name}: {line.strip()!r} is the command that removes speech "
                        "output, not one that installs it"
                    )
            # And the rule itself, not only the commands that obey it. Someone
            # adapting an example needs to know the synthesizer arrives without
            # being asked for, or they will add a flag to ask for it.
            self.assertIn(
                "installed by default",
                body.lower(),
                f"{name}: the speech section never states that speech output is a default",
            )
