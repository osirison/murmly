from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from murmly.audio import LevelSmoother, SoundDeviceRecorder, pcm16_rms, rms_to_level
from murmly.config import MurmlyConfig


class FakePortAudioError(Exception):
    pass


class FakeStream:
    def __init__(self, sample_rate_hz: float = 48_000) -> None:
        self.started = False
        self.closed = False
        self.samplerate = sample_rate_hz

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FailingStopStream(FakeStream):
    def stop(self) -> None:
        raise RuntimeError("stop failed")


class AudioTests(unittest.TestCase):
    def test_pcm16_rms_and_dbfs_mapping_are_bounded(self) -> None:
        self.assertEqual(0.0, pcm16_rms(b""))
        self.assertEqual(0.0, pcm16_rms(b"\x00\x00" * 4))
        self.assertAlmostEqual(0.5, pcm16_rms(b"\x00\x40\x00\xc0"), places=4)
        self.assertAlmostEqual(1.0, pcm16_rms(b"\xff\x7f\x00\x80"), places=4)
        self.assertEqual(0.0, rms_to_level(0.0))
        self.assertEqual(0.0, rms_to_level(10 ** (-60 / 20)))
        self.assertAlmostEqual(1.0, rms_to_level(10 ** (-6 / 20)))
        self.assertEqual(1.0, rms_to_level(2.0))

    def test_level_smoother_has_fast_attack_slow_release_and_reset(self) -> None:
        smoother = LevelSmoother()

        attack = smoother.update(1.0)
        release = smoother.update(0.0)

        self.assertAlmostEqual(0.55, attack)
        self.assertAlmostEqual(0.451, release)
        self.assertGreater(release, 0.0)
        smoother.reset()
        self.assertEqual(0.0, smoother.level)
        self.assertAlmostEqual(0.55, smoother.update(10.0))

    def test_level_sink_failure_does_not_change_captured_audio(self) -> None:
        callback_holder: dict[str, object] = {}
        stream = FakeStream()
        sounddevice = ModuleType("sounddevice")
        sounddevice.PortAudioError = FakePortAudioError
        sounddevice.query_devices = self._fake_query_devices
        sounddevice.check_input_settings = lambda **_kwargs: None

        def raw_input_stream(**kwargs: object) -> FakeStream:
            callback_holder["callback"] = kwargs["callback"]
            return stream

        sounddevice.RawInputStream = raw_input_stream

        def failing_sink(_level: float) -> None:
            raise RuntimeError("renderer failed")

        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            with patch.dict(sys.modules, {"sounddevice": sounddevice}):
                recorder = SoundDeviceRecorder(config, level_sink=failing_sink)
                recorder.start()
                callback = callback_holder["callback"]
                self.assertTrue(callable(callback))
                pcm_audio = b"\x00\x40\x00\xc0"
                callback(pcm_audio, 2, object(), None)
                callback(pcm_audio, 2, object(), None)
                captured = recorder.stop()

        self.assertEqual(pcm_audio * 2, captured)

    def test_audio_callback_does_not_wait_for_blocked_level_sink(self) -> None:
        callback_holder: dict[str, object] = {}
        stream = FakeStream()
        sounddevice = ModuleType("sounddevice")
        sounddevice.PortAudioError = FakePortAudioError
        sounddevice.query_devices = self._fake_query_devices
        sounddevice.check_input_settings = lambda **_kwargs: None
        sounddevice.RawInputStream = lambda **kwargs: callback_holder.setdefault("stream", kwargs) and stream
        sink_entered = threading.Event()
        release_sink = threading.Event()

        def blocking_sink(_level: float) -> None:
            sink_entered.set()
            release_sink.wait(timeout=2)

        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            with patch.dict(sys.modules, {"sounddevice": sounddevice}):
                recorder = SoundDeviceRecorder(config, level_sink=blocking_sink)
                recorder.start()
                callback = callback_holder["stream"]["callback"]
                pcm_audio = b"\x00\x40\x00\xc0" * 120
                callback(pcm_audio, 240, object(), None)
                self.assertTrue(sink_entered.wait(timeout=1))

                started_at = time.perf_counter()
                callback(pcm_audio, 240, object(), None)
                callback_elapsed = time.perf_counter() - started_at
                release_sink.set()
                captured = recorder.stop()

        self.assertLess(callback_elapsed, 0.005)
        self.assertEqual(pcm_audio * 2, captured)

    def test_segments_partition_the_recording_without_loss_or_duplication(self) -> None:
        recorder, callback_holder = self._started_recorder()
        callback = callback_holder["callback"]

        blocks = [bytes([index % 256, 0]) * 8 for index in range(9)]
        segments: list[bytes] = []
        for index, block in enumerate(blocks):
            callback(block, 8, object(), None)
            if index in (2, 5):
                segments.append(recorder.take_segment())
        segments.append(recorder.stop())

        self.assertEqual(b"".join(blocks), b"".join(segments))
        self.assertEqual(b"".join(blocks[0:3]), segments[0])
        self.assertEqual(b"".join(blocks[3:6]), segments[1])
        self.assertEqual(b"".join(blocks[6:9]), segments[2])

    def test_take_segment_leaves_nothing_behind(self) -> None:
        recorder, callback_holder = self._started_recorder()
        callback = callback_holder["callback"]

        callback(b"\x01\x00" * 8, 8, object(), None)
        first = recorder.take_segment()
        second = recorder.take_segment()

        self.assertEqual(b"\x01\x00" * 8, first)
        self.assertEqual(b"", second)

    def test_snapshot_does_not_consume_or_stop_capture(self) -> None:
        recorder, callback_holder = self._started_recorder()
        callback = callback_holder["callback"]

        callback(b"\x02\x00" * 8, 8, object(), None)
        first_snapshot = recorder.snapshot()
        callback(b"\x03\x00" * 8, 8, object(), None)
        second_snapshot = recorder.snapshot()

        self.assertEqual(b"\x02\x00" * 8, first_snapshot)
        self.assertEqual(b"\x02\x00" * 8 + b"\x03\x00" * 8, second_snapshot)
        self.assertIsNotNone(recorder._stream)
        self.assertEqual(second_snapshot, recorder.stop())

    def test_snapshot_window_keeps_the_trailing_audio_on_frame_boundaries(self) -> None:
        recorder, callback_holder = self._started_recorder()
        callback = callback_holder["callback"]

        # 48 kHz mono int16: one second is 96_000 bytes.
        self.assertEqual(96_000, recorder.bytes_per_second)
        for value in (1, 2, 3):
            callback(bytes([value, 0]) * 48_000, 48_000, object(), None)

        windowed = recorder.snapshot(window_seconds=1)

        self.assertEqual(96_000, len(windowed))
        self.assertEqual(bytes([3, 0]) * 48_000, windowed)
        self.assertEqual(0, len(windowed) % 2)
        self.assertEqual(288_000, len(recorder.snapshot()))

    def test_snapshot_window_larger_than_the_recording_returns_everything(self) -> None:
        recorder, callback_holder = self._started_recorder()
        callback_holder["callback"](b"\x04\x00" * 8, 8, object(), None)

        self.assertEqual(b"\x04\x00" * 8, recorder.snapshot(window_seconds=30))

    def _started_recorder(self) -> tuple[SoundDeviceRecorder, dict[str, object]]:
        callback_holder: dict[str, object] = {}
        stream = FakeStream()
        sounddevice = ModuleType("sounddevice")
        sounddevice.PortAudioError = FakePortAudioError
        sounddevice.query_devices = self._fake_query_devices
        sounddevice.check_input_settings = lambda **_kwargs: None

        def raw_input_stream(**kwargs: object) -> FakeStream:
            callback_holder["callback"] = kwargs["callback"]
            return stream

        sounddevice.RawInputStream = raw_input_stream

        temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp_dir, ignore_errors=True)
        config = MurmlyConfig(
            socket_path=Path(temp_dir) / "murmly.sock",
            config_path=Path(temp_dir) / "config.toml",
        )
        patcher = patch.dict(sys.modules, {"sounddevice": sounddevice})
        patcher.start()
        self.addCleanup(patcher.stop)
        recorder = SoundDeviceRecorder(config)
        recorder.start()
        return recorder, callback_holder

    def test_stop_failure_still_closes_stream_and_meter(self) -> None:
        callback_holder: dict[str, object] = {}
        stream = FailingStopStream()
        sounddevice = ModuleType("sounddevice")
        sounddevice.PortAudioError = FakePortAudioError
        sounddevice.query_devices = self._fake_query_devices
        sounddevice.check_input_settings = lambda **_kwargs: None

        def raw_input_stream(**kwargs: object) -> FakeStream:
            callback_holder["callback"] = kwargs["callback"]
            return stream

        sounddevice.RawInputStream = raw_input_stream
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            with patch.dict(sys.modules, {"sounddevice": sounddevice}):
                recorder = SoundDeviceRecorder(config, level_sink=lambda _level: None)
                recorder.start()
                callback_holder["callback"](b"\x00\x40" * 120, 120, object(), None)
                with self.assertRaisesRegex(RuntimeError, "stop failed"):
                    recorder.stop()

        self.assertTrue(stream.closed)
        self.assertIsNone(recorder._stream)
        self.assertTrue(recorder._meter_thread is None or not recorder._meter_thread.is_alive())

    def test_blocked_meter_disables_restart_instead_of_creating_second_worker(self) -> None:
        callbacks: list[object] = []
        streams: list[FakeStream] = []
        sounddevice = ModuleType("sounddevice")
        sounddevice.PortAudioError = FakePortAudioError
        sounddevice.query_devices = self._fake_query_devices
        sounddevice.check_input_settings = lambda **_kwargs: None

        def raw_input_stream(**kwargs: object) -> FakeStream:
            callbacks.append(kwargs["callback"])
            stream = FakeStream()
            streams.append(stream)
            return stream

        sounddevice.RawInputStream = raw_input_stream
        sink_entered = threading.Event()
        release_sink = threading.Event()

        def blocking_sink(_level: float) -> None:
            sink_entered.set()
            release_sink.wait(timeout=2)

        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            with patch.dict(sys.modules, {"sounddevice": sounddevice}):
                recorder = SoundDeviceRecorder(config, level_sink=blocking_sink)
                recorder.start()
                callbacks[0](b"\x00\x40" * 120, 120, object(), None)
                self.assertTrue(sink_entered.wait(timeout=1))
                recorder.stop()
                first_meter = recorder._meter_thread

                recorder.start()

                self.assertIs(recorder._meter_thread, first_meter)
                self.assertIsNone(recorder._level_sink)
                release_sink.set()
                first_meter.join(timeout=1)
                recorder.stop()

        self.assertFalse(first_meter.is_alive())
        self.assertEqual(2, len(streams))

    def test_meter_thread_start_failure_degrades_without_leaking_stream(self) -> None:
        stream = FakeStream()
        sounddevice = ModuleType("sounddevice")
        sounddevice.PortAudioError = FakePortAudioError
        sounddevice.query_devices = self._fake_query_devices
        sounddevice.check_input_settings = lambda **_kwargs: None
        sounddevice.RawInputStream = lambda **_kwargs: stream

        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            with (
                patch.dict(sys.modules, {"sounddevice": sounddevice}),
                patch("murmly.audio.threading.Thread.start", side_effect=RuntimeError("no thread")),
            ):
                recorder = SoundDeviceRecorder(config, level_sink=lambda _level: None)
                recorder.start()

            self.assertIs(recorder._stream, stream)
            self.assertIsNone(recorder._meter_thread)
            self.assertIsNone(recorder._level_sink)
            recorder.stop()

        self.assertTrue(stream.closed)

    def test_recorder_prefers_default_input(self) -> None:
        stream = FakeStream(48_000.4)
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

        self.assertEqual(48_000, recorder.sample_rate_hz)
        self.assertTrue(stream.started)

    def test_recorder_falls_back_when_default_input_is_unavailable(self) -> None:
        stream = FakeStream()
        sounddevice = ModuleType("sounddevice")
        sounddevice.PortAudioError = FakePortAudioError

        def query_devices(device: int | None = None, kind: str | None = None):
            if kind == "input":
                raise FakePortAudioError("no default input")
            return self._fake_query_devices(device)

        sounddevice.query_devices = query_devices

        def check_input_settings(**kwargs: object) -> None:
            if kwargs["device"] != 0 or kwargs["samplerate"] != 48_000:
                raise FakePortAudioError("unsupported input setting")

        sounddevice.check_input_settings = check_input_settings
        sounddevice.RawInputStream = lambda **_kwargs: stream

        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            with patch.dict(sys.modules, {"sounddevice": sounddevice}):
                recorder = SoundDeviceRecorder(config)
                recorder.start()

        self.assertEqual(48_000, recorder.sample_rate_hz)
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