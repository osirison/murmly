from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from murmly.config import default_socket_path, load_config


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
