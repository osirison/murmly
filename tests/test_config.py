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
                    compute_type = "int8"
                    lazy_load_model = false

                    [clipboard]
                    restore = false
                    restore_delay_ms = 150
                    """
                ).strip()
            )

            config = load_config(config_path)

        self.assertEqual(Path("/tmp/custom.sock"), config.socket_path)
        self.assertEqual("accurate", config.model_profile)
        self.assertEqual("small.en", config.model_name)
        self.assertFalse(config.lazy_load_model)
        self.assertFalse(config.restore_clipboard)
        self.assertEqual(150, config.restore_clipboard_delay_ms)
