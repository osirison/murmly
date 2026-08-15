from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from murmly.audio import SoundDeviceRecorder
from murmly.config import MurmlyConfig


class FakePortAudioError(Exception):
    pass


class FakeStream:
    def __init__(self) -> None:
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class AudioTests(unittest.TestCase):
    def test_recorder_prefers_default_input(self) -> None:
        stream = FakeStream()
        sounddevice = ModuleType("sounddevice")
        sounddevice.PortAudioError = FakePortAudioError
        sounddevice.query_devices = self._fake_query_devices

        def check_input_settings(**kwargs: object) -> None:
            if kwargs["device"] is not None or kwargs["samplerate"] != 44_100:
                raise FakePortAudioError("unsupported input setting")

        def raw_input_stream(**kwargs: object) -> FakeStream:
            self.assertIsNone(kwargs["device"])
            self.assertEqual(44_100, kwargs["samplerate"])
            return stream

        sounddevice.check_input_settings = check_input_settings
        sounddevice.RawInputStream = raw_input_stream

        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            with patch.dict(sys.modules, {"sounddevice": sounddevice}):
                recorder = SoundDeviceRecorder(config)
                recorder.start()

        self.assertEqual(44_100, recorder.sample_rate_hz)
        self.assertTrue(stream.started)

    def test_recorder_falls_back_to_physical_input_native_rate(self) -> None:
        preflight_attempts: list[tuple[int | None, int]] = []
        stream_attempts: list[tuple[int | None, int]] = []
        stream = FakeStream()
        sounddevice = ModuleType("sounddevice")
        sounddevice.PortAudioError = FakePortAudioError
        sounddevice.query_devices = self._fake_query_devices

        def check_input_settings(**kwargs: object) -> None:
            device = kwargs["device"]
            sample_rate_hz = kwargs["samplerate"]
            preflight_attempts.append((device if isinstance(device, int) else None, int(sample_rate_hz)))
            if device == 0 and sample_rate_hz == 48_000:
                return
            raise FakePortAudioError("unsupported input setting")

        def raw_input_stream(**kwargs: object) -> FakeStream:
            device = kwargs["device"]
            sample_rate_hz = kwargs["samplerate"]
            stream_attempts.append((device if isinstance(device, int) else None, int(sample_rate_hz)))
            if device == 0 and sample_rate_hz == 48_000:
                return stream
            raise FakePortAudioError("unsupported input setting")

        sounddevice.check_input_settings = check_input_settings
        sounddevice.RawInputStream = raw_input_stream

        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            with patch.dict(sys.modules, {"sounddevice": sounddevice}):
                recorder = SoundDeviceRecorder(config)
                recorder.start()

        self.assertEqual(
            [(None, 16_000), (None, 44_100), (0, 16_000), (0, 48_000)],
            preflight_attempts,
        )
        self.assertEqual([(0, 48_000)], stream_attempts)
        self.assertEqual(48_000, recorder.sample_rate_hz)
        self.assertTrue(stream.started)

    @staticmethod
    def _fake_query_devices(device: int | None = None, kind: str | None = None):
        devices = [
            {
                "name": "Built-in Audio",
                "max_input_channels": 2,
                "default_samplerate": 48_000,
            },
            {
                "name": "default",
                "max_input_channels": 2,
                "default_samplerate": 44_100,
            },
        ]
        if kind == "input":
            return devices[1]
        if device is None:
            return devices
        return devices[device]