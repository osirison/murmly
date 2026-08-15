from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from murmly.cli import _run_doctor
from murmly.config import MurmlyConfig
from murmly.stt import FasterWhisperTranscriber


class CliTests(unittest.TestCase):
    def test_doctor_reports_effective_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )

            with (
                patch.object(
                    FasterWhisperTranscriber,
                    "resolve_runtime",
                    return_value=("cuda", "float16"),
                ),
                patch("murmly.cli.choose_clipboard_copy_command", return_value=["xclip"]),
                patch("murmly.cli.choose_paste_command", return_value=["xdotool"]),
                redirect_stdout(StringIO()) as output,
            ):
                _run_doctor(config)

        report = json.loads(output.getvalue())
        self.assertEqual("auto", report["device"])
        self.assertEqual("auto", report["compute_type"])
        self.assertEqual("cuda", report["runtime_device"])
        self.assertEqual("float16", report["runtime_compute_type"])