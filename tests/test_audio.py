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

from murmly.audio import (
    LevelSmoother,
    SoundDeviceRecorder,
    SoundDevicePlayer,
    pcm16_from_float32,
    pcm16_rms,
    resample_float32,
    rms_to_level,
)
from murmly.config import MurmlyConfig


class FakePortAudioError(Exception):
    pass


class FakeStream:
    def __init__(self, sample_rate_hz: float = 48_000) -> None:
        self.started = False
        self.closed = False
        self.aborted = False
        self.samplerate = sample_rate_hz
        self.written = bytearray()

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        pass

    def abort(self) -> None:
        self.aborted = True

    def close(self) -> None:
        self.closed = True

    def write(self, data: bytes) -> None:
        """Record audio that reached the device.

        The output stream is callback driven, so nothing calls this on the real
        object. It is what `pump` hands the bytes the callback produced, which is
        how a test asserts playback without hardware.
        """
        self.written.extend(bytes(data))


class FakeOutputStream(FakeStream):
    """A `sd.RawOutputStream` stand-in whose periods the test drives itself.

    PortAudio would call the callback from its own thread. Driving it from the
    test instead makes underrun, abort mid-buffer and exact frame counts
    reproducible rather than timing dependent.
    """

    def __init__(
        self,
        callback,
        sample_rate_hz: float = 48_000,
        channels: int = 1,
        sample_width: int = 2,
    ) -> None:
        super().__init__(sample_rate_hz)
        self.callback = callback
        self.channels = channels
        self.frame_bytes = channels * sample_width

    def pump(self, frames: int, status: object = None) -> bytes:
        """Run one callback period and record what it produced."""
        block = bytearray(frames * self.frame_bytes)
        buffer = memoryview(block)
        self.callback(buffer, frames, object(), status)
        produced = bytes(block)
        self.write(produced)
        return produced


class FailingStopStream(FakeStream):
    def stop(self) -> None:
        raise RuntimeError("stop failed")


def fake_sounddevice(
    *,
    output_streams: list[FakeOutputStream] | None = None,
    input_streams: list[FakeStream] | None = None,
    check_output_settings=None,
    check_input_settings=None,
    query_devices=None,
) -> ModuleType:
    """A fake sounddevice module carrying the output surface beside the input one.

    The input fakes the rest of this suite builds inline are unchanged and keep
    building them; this exists for the tests that need playback, and for the
    barge-in tests that need both directions in one module.
    """
    module = ModuleType("sounddevice")
    module.PortAudioError = FakePortAudioError
    module.query_devices = query_devices or AudioTests._fake_query_devices
    module.check_input_settings = check_input_settings or (lambda **_kwargs: None)
    module.check_output_settings = check_output_settings or (lambda **_kwargs: None)

    def raw_input_stream(**kwargs: object) -> FakeStream:
        stream = FakeStream(sample_rate_hz=kwargs["samplerate"])
        stream.kwargs = kwargs
        if input_streams is not None:
            input_streams.append(stream)
        return stream

    def raw_output_stream(**kwargs: object) -> FakeOutputStream:
        stream = FakeOutputStream(
            kwargs["callback"],
            sample_rate_hz=kwargs["samplerate"],
            channels=kwargs["channels"],
        )
        stream.kwargs = kwargs
        if output_streams is not None:
            output_streams.append(stream)
        return stream

    module.RawInputStream = raw_input_stream
    module.RawOutputStream = raw_output_stream
    return module


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
                "max_output_channels": 2,
                "default_samplerate": 48_000,
            },
            {
                "name": "default",
                "max_input_channels": 2,
                "max_output_channels": 2,
                "default_samplerate": 44_100,
            },
        ]
        if kind in {"input", "output"}:
            return devices[1]
        if device is None:
            return devices
        return devices[device]

class CaptureAccumulatorConcurrencyTests(unittest.TestCase):
    """The accumulator has two consumers: the live worker and the toggle path.

    The rest of the suite never runs them together, which is how a lost-update
    race on `_pending` survived a green suite.
    """

    def _recorder(self):
        callback_holder: dict[str, object] = {}
        stream = FakeStream()
        sounddevice = ModuleType("sounddevice")
        sounddevice.PortAudioError = FakePortAudioError
        sounddevice.query_devices = AudioTests._fake_query_devices
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
        return recorder, callback_holder["callback"]

    def test_every_accumulator_consumer_holds_the_lock(self) -> None:
        """Pins the fix directly: a consumer that skips the lock fails here.

        A stress test cannot do this job -- the GIL makes the losing interleaving
        rare enough to pass by luck.
        """
        recorder, callback = self._recorder()
        callback(b"\x01\x00" * 8, 8, object(), None)

        real_lock = recorder._pending_lock
        acquisitions: list[str] = []

        class TrackingLock:
            def __enter__(self) -> object:
                acquisitions.append("acquired")
                return real_lock.__enter__()

            def __exit__(self, *exc: object) -> object:
                return real_lock.__exit__(*exc)

        recorder._pending_lock = TrackingLock()

        acquisitions.clear()
        recorder.snapshot()
        self.assertEqual(1, len(acquisitions), "snapshot must serialize on the accumulator lock")

        acquisitions.clear()
        recorder.take_segment()
        self.assertEqual(1, len(acquisitions), "take_segment must serialize on the accumulator lock")

        acquisitions.clear()
        recorder._pending_lock = real_lock
        recorder.stop()

    def test_take_segment_blocks_while_another_consumer_holds_the_lock(self) -> None:
        recorder, callback = self._recorder()
        callback(b"\x03\x00" * 8, 8, object(), None)
        released = threading.Event()
        finished = threading.Event()

        def hold_then_release() -> None:
            with recorder._pending_lock:
                released.wait(timeout=5)

        holder = threading.Thread(target=hold_then_release)
        holder.start()
        time.sleep(0.05)

        def take() -> None:
            recorder.take_segment()
            finished.set()

        taker = threading.Thread(target=take)
        taker.start()

        self.assertFalse(finished.wait(timeout=0.3), "take_segment ran while the lock was held")
        released.set()
        holder.join(timeout=5)
        taker.join(timeout=5)
        self.assertTrue(finished.is_set())
        recorder.stop()

    def test_concurrent_consumers_preserve_every_captured_byte(self) -> None:
        recorder, callback = self._recorder()
        total_blocks = 200
        block = b"\x01\x00" * 4
        collected: list[bytes] = []
        collected_lock = threading.Lock()
        stop = threading.Event()

        def producer() -> None:
            for _ in range(total_blocks):
                callback(block, 4, object(), None)
                time.sleep(0.0005)
            stop.set()

        def consumer() -> None:
            while not stop.is_set():
                segment = recorder.take_segment()
                if segment:
                    with collected_lock:
                        collected.append(segment)

        threads = [threading.Thread(target=producer)]
        threads += [threading.Thread(target=consumer) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        collected.append(recorder.stop())

        self.assertEqual(block * total_blocks, b"".join(collected))


class PlaybackTests(unittest.TestCase):
    """The output path, driven without hardware through the output fakes."""

    def _player(self, **kwargs) -> tuple[SoundDevicePlayer, list[FakeOutputStream], object]:
        streams: list[FakeOutputStream] = []
        sounddevice = fake_sounddevice(output_streams=streams, **kwargs)
        temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp_dir, ignore_errors=True)
        config = MurmlyConfig(
            socket_path=Path(temp_dir) / "murmly.sock",
            config_path=Path(temp_dir) / "config.toml",
            tts_enabled=True,
        )
        patcher = patch.dict(sys.modules, {"sounddevice": sounddevice})
        patcher.start()
        self.addCleanup(patcher.stop)
        return SoundDevicePlayer(config), streams, sounddevice

    @staticmethod
    def _tone(count: int, value: float = 0.5) -> list[float]:
        return [value] * count

    def test_the_preferred_rate_is_preflighted_before_the_device_native_one(self) -> None:
        attempts: list[tuple[object, int, int]] = []

        def check_output_settings(**kwargs: object) -> None:
            attempts.append((kwargs["device"], kwargs["channels"], kwargs["samplerate"]))

        player, streams, _sd = self._player(check_output_settings=check_output_settings)
        player.start()

        self.assertEqual((None, 1, 24_000), attempts[0])
        self.assertEqual(24_000, player.sample_rate_hz)
        self.assertTrue(streams[0].started)

    def test_the_negotiated_rate_is_read_off_the_stream_not_the_request(self) -> None:
        """A device that refuses 24 kHz lands on its own rate, and that is what counts."""

        def check_output_settings(**kwargs: object) -> None:
            if kwargs["samplerate"] == 24_000:
                raise FakePortAudioError("24 kHz not supported")

        player, streams, _sd = self._player(check_output_settings=check_output_settings)
        player.start()

        self.assertEqual(44_100, player.sample_rate_hz)
        self.assertEqual(44_100, int(streams[0].samplerate))

    def test_a_device_that_takes_no_settings_at_all_reports_every_failure(self) -> None:
        def check_output_settings(**kwargs: object) -> None:
            raise FakePortAudioError(f"nothing at {kwargs['samplerate']}")

        player, streams, _sd = self._player(check_output_settings=check_output_settings)

        with self.assertRaises(RuntimeError) as raised:
            player.start()

        message = str(raised.exception)
        self.assertIn("Unable to open an audio output", message)
        self.assertIn("nothing at 24000", message)
        self.assertIn("nothing at 44100", message)
        self.assertEqual([], streams)

    def test_a_stream_that_fails_to_open_is_closed_before_the_next_candidate(self) -> None:
        opened: list[FakeStream] = []

        def raw_output_stream(**kwargs: object) -> FakeStream:
            stream = FakeOutputStream(kwargs["callback"], kwargs["samplerate"], kwargs["channels"])
            opened.append(stream)
            if kwargs["samplerate"] == 24_000:
                raise FakePortAudioError("device busy")
            return stream

        player, _streams, sounddevice = self._player()
        sounddevice.RawOutputStream = raw_output_stream
        player.start()

        self.assertEqual(44_100, player.sample_rate_hz)

    def test_written_audio_reaches_the_device_unchanged_at_the_negotiated_rate(self) -> None:
        player, streams, _sd = self._player()
        player.start()

        frames = player.write(self._tone(480), 24_000)
        streams[0].pump(480)

        self.assertEqual(480, frames)
        self.assertEqual(480, player.frames_played)
        self.assertEqual(pcm16_from_float32(self._tone(480)), bytes(streams[0].written))

    def test_a_period_larger_than_the_queue_is_padded_and_counted_as_an_underrun(self) -> None:
        player, streams, _sd = self._player()
        player.start()
        player.write(self._tone(100), 24_000)

        produced = streams[0].pump(480, status="output underflow")

        self.assertEqual(100, player.frames_played, "silence must not count as played")
        self.assertEqual(bytes(2 * 380), produced[200:])
        self.assertEqual(1, player.underruns)

    def test_a_chunk_is_played_across_as_many_periods_as_it_takes(self) -> None:
        player, streams, _sd = self._player()
        player.start()
        player.write(self._tone(1_000), 24_000)

        for _ in range(2):
            streams[0].pump(480)

        self.assertEqual(960, player.frames_played)
        self.assertEqual(40, player.pending_frames)

    def test_abort_stops_audio_already_queued_and_reports_what_was_played(self) -> None:
        player, streams, _sd = self._player()
        player.start()
        player.write(self._tone(4_800), 24_000)
        streams[0].pump(480)

        played = player.abort()

        self.assertEqual(480, played)
        self.assertEqual(0, player.pending_frames)
        self.assertTrue(streams[0].aborted)

    def test_nothing_queued_before_an_abort_plays_after_it(self) -> None:
        player, streams, _sd = self._player()
        player.start()
        player.write(self._tone(4_800), 24_000)
        streams[0].pump(480)
        before = bytes(streams[0].written)

        player.abort()
        streams[0].pump(480)

        self.assertEqual(bytes(2 * 480), bytes(streams[0].written)[len(before) :])

    def test_a_failing_stop_still_closes_the_stream(self) -> None:
        player, streams, _sd = self._player()
        player.start()
        streams[0].stop = lambda: (_ for _ in ()).throw(RuntimeError("stop failed"))

        with self.assertRaises(RuntimeError):
            player.stop()

        self.assertTrue(streams[0].closed)
        self.assertFalse(player.active)

    def test_stopping_a_player_that_never_started_is_not_an_error(self) -> None:
        player, _streams, _sd = self._player()

        player.stop()

        self.assertFalse(player.active)

    def test_a_device_that_refuses_mono_gets_the_signal_twice(self) -> None:
        def check_output_settings(**kwargs: object) -> None:
            if kwargs["channels"] == 1:
                raise FakePortAudioError("mono not supported")

        player, streams, _sd = self._player(check_output_settings=check_output_settings)
        player.start()
        player.write(self._tone(4), 24_000)
        streams[0].pump(4)

        self.assertEqual(2, player.channels)
        self.assertEqual(pcm16_from_float32(self._tone(4), channels=2), bytes(streams[0].written))

    def test_the_configured_device_is_tried_first(self) -> None:
        attempts: list[object] = []

        def check_output_settings(**kwargs: object) -> None:
            attempts.append(kwargs["device"])

        streams: list[FakeOutputStream] = []
        sounddevice = fake_sounddevice(
            output_streams=streams, check_output_settings=check_output_settings
        )
        temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp_dir, ignore_errors=True)
        config = MurmlyConfig(
            socket_path=Path(temp_dir) / "murmly.sock",
            config_path=Path(temp_dir) / "config.toml",
            tts_output_device="Built-in Audio",
        )
        with patch.dict(sys.modules, {"sounddevice": sounddevice}):
            player = SoundDevicePlayer(config)
            player.start()

        self.assertEqual("Built-in Audio", attempts[0])
        self.assertIsNone(player.device_detail)

    def test_a_configured_device_that_cannot_be_opened_names_what_was_used_instead(self) -> None:
        def check_output_settings(**kwargs: object) -> None:
            if kwargs["device"] == "Missing Headset":
                raise FakePortAudioError("no such device")

        streams: list[FakeOutputStream] = []
        sounddevice = fake_sounddevice(
            output_streams=streams, check_output_settings=check_output_settings
        )
        temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp_dir, ignore_errors=True)
        config = MurmlyConfig(
            socket_path=Path(temp_dir) / "murmly.sock",
            config_path=Path(temp_dir) / "config.toml",
            tts_output_device="Missing Headset",
        )
        with patch.dict(sys.modules, {"sounddevice": sounddevice}):
            player = SoundDevicePlayer(config)
            player.start()

        self.assertIsNotNone(player.device_detail)
        self.assertIn("Missing Headset", player.device_detail)
        self.assertIn("instead", player.device_detail)

    def test_the_callback_takes_no_lock(self) -> None:
        """Pins the discipline the capture path already keeps.

        A callback that waits on the producer's lock stutters whenever a
        sentence is queued, and a stress test would pass by luck.
        """
        player, streams, _sd = self._player()
        player.start()
        player.write(self._tone(480), 24_000)

        acquisitions: list[str] = []
        real_lock = player._write_lock

        class TrackingLock:
            def __enter__(self) -> object:
                acquisitions.append("acquired")
                return real_lock.__enter__()

            def __exit__(self, *exc: object) -> object:
                return real_lock.__exit__(*exc)

        player._write_lock = TrackingLock()
        streams[0].pump(480)
        self.assertEqual([], acquisitions, "the playback callback must take no lock")

        player._write_lock = real_lock


class SampleConversionTests(unittest.TestCase):
    def test_float32_becomes_little_endian_int16(self) -> None:
        self.assertEqual(b"\x00\x00\xff\x7f\x01\x80", pcm16_from_float32([0.0, 1.0, -1.0]))

    def test_samples_past_full_scale_are_clipped_not_scaled(self) -> None:
        self.assertEqual(b"\xff\x7f\x01\x80", pcm16_from_float32([4.0, -4.0]))

    def test_a_stereo_device_gets_each_sample_twice(self) -> None:
        self.assertEqual(
            pcm16_from_float32([0.5, 0.5, -0.5, -0.5]),
            pcm16_from_float32([0.5, -0.5], channels=2),
        )

    def test_a_matching_rate_is_not_resampled(self) -> None:
        samples = [0.1, 0.2, 0.3]
        self.assertEqual(samples, list(resample_float32(samples, 24_000, 24_000)))

    def test_resampling_up_keeps_the_duration(self) -> None:
        resampled = resample_float32([0.0] * 24_000, 24_000, 48_000)
        self.assertEqual(48_000, len(resampled))

    def test_resampling_down_keeps_the_duration(self) -> None:
        resampled = resample_float32([0.0] * 48_000, 48_000, 24_000)
        self.assertEqual(24_000, len(resampled))

    def test_resampling_a_non_integer_ratio_is_accepted(self) -> None:
        """Unlike the decimator, which refuses one: this feeds a loudspeaker."""
        resampled = resample_float32([0.0] * 24_000, 24_000, 44_100)
        self.assertEqual(44_100, len(resampled))

    def test_an_empty_chunk_resamples_to_nothing(self) -> None:
        self.assertEqual(0, len(resample_float32([], 24_000, 48_000)))
