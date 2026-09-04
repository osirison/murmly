from __future__ import annotations

from dataclasses import replace
import shutil
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from module_stubs import injected_module, removed_module
from murmly.audio import (
    MIN_PLAYBACK_LATENCY_SECONDS,
    PLAYBACK_LATENCY_FALLBACKS,
    LevelSmoother,
    SoundDeviceRecorder,
    SoundDevicePlayer,
    disable_portaudio_exit_teardown,
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
        # Whether PortAudio would be calling the callback. `abort` is
        # Pa_AbortStream and `stop` is Pa_StopStream, and both leave the stream
        # stopped until it is started again -- a fake that keeps running after
        # an abort tests something PortAudio does not do.
        self.running = False
        self.samplerate = sample_rate_hz
        self.written = bytearray()

    def start(self) -> None:
        self.started = True
        self.running = True

    def stop(self) -> None:
        self.running = False

    def abort(self) -> None:
        self.aborted = True
        self.running = False

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
        latency: float = 0.0,
    ) -> None:
        super().__init__(sample_rate_hz)
        self.callback = callback
        self.channels = channels
        self.frame_bytes = channels * sample_width
        # What PortAudio reports the stream settled on. The fake grants whatever
        # was asked for, which is the case worth modelling: a host that refuses
        # is modelled by `open_error` below raising instead.
        self.latency = latency

    def pump(self, frames: int, status: object = None) -> bytes:
        """Run one callback period and record what it produced.

        A stopped stream produces nothing, because PortAudio does not call the
        callback of one. Silence here rather than a refusal: a test can pump a
        device that has been aborted and see that it stayed silent.
        """
        block = bytearray(frames * self.frame_bytes)
        if not self.running:
            produced = bytes(block)
            self.write(produced)
            return produced
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
    open_error=None,
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
        # Called before the stream exists, so a host that refuses a buffer
        # refuses it the way PortAudio does: at open, not at start.
        if open_error is not None:
            open_error(**kwargs)
        stream = FakeOutputStream(
            kwargs["callback"],
            sample_rate_hz=kwargs["samplerate"],
            channels=kwargs["channels"],
            latency=kwargs.get("latency", 0.0),
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
            with injected_module("sounddevice", sounddevice):
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
            with injected_module("sounddevice", sounddevice):
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
        stack = ExitStack()
        stack.enter_context(injected_module("sounddevice", sounddevice))
        self.addCleanup(stack.close)
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
            with injected_module("sounddevice", sounddevice):
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
            with injected_module("sounddevice", sounddevice):
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
                injected_module("sounddevice", sounddevice),
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
            with injected_module("sounddevice", sounddevice):
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
            with injected_module("sounddevice", sounddevice):
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
            with injected_module("sounddevice", sounddevice):
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
        stack = ExitStack()
        stack.enter_context(injected_module("sounddevice", sounddevice))
        self.addCleanup(stack.close)
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
        stack = ExitStack()
        stack.enter_context(injected_module("sounddevice", sounddevice))
        self.addCleanup(stack.close)
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
        """The construction succeeds and the start fails, which is the only way
        there is a stream object to leak. Raising from the constructor instead
        means nothing was ever built, so the close the test is named for is
        never reached and the assertion holds with the close deleted.
        """
        opened: list[FakeOutputStream] = []

        def raw_output_stream(**kwargs: object) -> FakeOutputStream:
            stream = FakeOutputStream(kwargs["callback"], kwargs["samplerate"], kwargs["channels"])
            opened.append(stream)
            if kwargs["samplerate"] == 24_000:

                def refuse() -> None:
                    raise FakePortAudioError("device busy")

                stream.start = refuse
            return stream

        player, _streams, sounddevice = self._player()
        sounddevice.RawOutputStream = raw_output_stream
        player.start()

        self.assertEqual(44_100, player.sample_rate_hz)
        self.assertTrue(opened[0].closed, "the stream that would not start was left open")
        self.assertFalse(opened[-1].closed, "the stream that did start was closed")

    def test_a_constructor_that_raises_still_falls_through_to_the_next_candidate(self) -> None:
        """The other half of the same path, kept separately named."""
        opened: list[FakeOutputStream] = []

        def raw_output_stream(**kwargs: object) -> FakeOutputStream:
            if kwargs["samplerate"] == 24_000:
                raise FakePortAudioError("device busy")
            stream = FakeOutputStream(kwargs["callback"], kwargs["samplerate"], kwargs["channels"])
            opened.append(stream)
            return stream

        player, _streams, sounddevice = self._player()
        sounddevice.RawOutputStream = raw_output_stream
        player.start()

        self.assertEqual(44_100, player.sample_rate_hz)
        self.assertEqual(1, len(opened))

    def test_written_audio_reaches_the_device_unchanged_at_the_negotiated_rate(self) -> None:
        player, streams, _sd = self._player()
        player.start()

        frames = player.write(self._tone(480), 24_000)
        streams[0].pump(480)

        self.assertEqual(480, frames)
        self.assertEqual(480, player.frames_played)
        self.assertEqual(pcm16_from_float32(self._tone(480)), bytes(streams[0].written))

    # ------------------------------------------------ the output buffer floor

    def test_the_buffer_floor_is_asked_for_before_anything_smaller(self) -> None:
        """The fault this exists for: a host's own largest is not large enough.

        Measured on PipeWire -- `latency="high"` gave a 34.67 ms buffer against a
        42.67 ms graph cycle, and 8 s of audio took 18 s to play.
        """
        player, streams, _sd = self._player()
        player.start()

        self.assertEqual(MIN_PLAYBACK_LATENCY_SECONDS, streams[0].kwargs["latency"])
        self.assertEqual(MIN_PLAYBACK_LATENCY_SECONDS, player.output_latency_seconds)

    def test_a_device_that_advertises_more_than_the_floor_keeps_its_own_answer(self) -> None:
        """The floor raises a host's answer; it never lowers one.

        A host asking for half a second knows something about itself that a
        constant here does not.
        """

        def query_devices(device: int | None = None, kind: str | None = None):
            found = AudioTests._fake_query_devices(device, kind)
            if isinstance(found, dict):
                return {**found, "default_high_output_latency": 0.5}
            return [{**each, "default_high_output_latency": 0.5} for each in found]

        player, streams, _sd = self._player(query_devices=query_devices)
        player.start()

        self.assertEqual(0.5, streams[0].kwargs["latency"])

    def test_a_host_that_refuses_the_floor_takes_less_on_the_same_device(self) -> None:
        """The retry is per device, not a second pass over all of them.

        A host that will not give the preferred buffer on the best device should
        keep that device with a smaller one rather than move to a worse device to
        hold the buffer.
        """
        refused: list[object] = []

        def open_error(**kwargs: object) -> None:
            if kwargs["latency"] == MIN_PLAYBACK_LATENCY_SECONDS:
                refused.append(kwargs["device"])
                raise FakePortAudioError("buffer too large")

        player, streams, _sd = self._player(open_error=open_error)
        player.start()

        self.assertEqual([None], refused, "the floor was tried once, on the first device")
        self.assertEqual(1, len(streams), "no stream survived from the refused attempt")
        self.assertEqual(PLAYBACK_LATENCY_FALLBACKS[0], streams[0].kwargs["latency"])
        self.assertIsNone(streams[0].kwargs["device"], "the device must not have moved")

    def test_the_ladder_ends_at_the_host_own_answer(self) -> None:
        """Exhausting the ladder still opens a stream, on what shipped before.

        `"high"` last is what makes the worst case no worse than the behaviour
        this change replaces.
        """

        def open_error(**kwargs: object) -> None:
            if kwargs["latency"] != "high":
                raise FakePortAudioError("buffer too large")

        player, streams, _sd = self._player(open_error=open_error)
        player.start()

        self.assertEqual("high", streams[0].kwargs["latency"])
        self.assertTrue(player.active)
        self.assertEqual(
            0.0,
            player.output_latency_seconds,
            "a buffer that is not a number of seconds must not hold the report forever",
        )

    def test_a_device_that_refuses_every_buffer_is_reported_with_each_attempt(self) -> None:
        def open_error(**_kwargs: object) -> None:
            raise FakePortAudioError("no")

        player, _streams, _sd = self._player(open_error=open_error)

        with self.assertRaises(RuntimeError) as raised:
            player.start()
        self.assertIn("latency=", str(raised.exception), "the buffer tried names itself")

    # ---------------------------------------------- the two kinds of dropout

    def test_a_device_that_could_not_be_fed_counts_a_dropout_and_not_starvation(self) -> None:
        player, streams, _sd = self._player()
        player.start()
        player.write(self._tone(480), 24_000)

        streams[0].pump(480, status="output underflow")

        self.assertEqual(1, player.underruns)
        self.assertEqual(0, player.starved_periods, "the queue had everything asked for")

    def test_a_gap_in_the_middle_of_playback_counts_starvation_and_not_a_dropout(self) -> None:
        """The other half of the pipeline, and why one number cannot say both.

        The device asked in good time, part-way through a piece of text whose
        producer was still working, and was given silence. That is a producer
        that fell behind rather than a buffer the host could not keep fed.
        """
        player, streams, _sd = self._player()
        player.start()
        player.expect_audio(True)
        player.write(self._tone(480), 24_000)
        streams[0].pump(480)

        streams[0].pump(480)
        streams[0].pump(480)
        player.write(self._tone(480), 24_000)
        streams[0].pump(480)

        self.assertEqual(2, player.starved_periods)
        self.assertEqual(0, player.underruns, "the device never complained")

    def test_a_sender_that_pauses_between_pieces_is_not_a_slow_synthesizer(self) -> None:
        """The device cannot tell an empty queue apart from a sender thinking.

        A session stays open while its sender decides what to say next, and the
        stream keeps asking for audio the whole time. Counted, that reports a
        synthesizer that is too slow for a session in which synthesis was never
        late, and sends the reader to the wrong half of the pipeline. Only the
        producer knows which it is, so only the producer says.
        """
        player, streams, _sd = self._player()
        player.start()
        player.expect_audio(True)
        player.write(self._tone(480), 24_000)
        streams[0].pump(480)
        player.expect_audio(False)

        for _ in range(50):
            streams[0].pump(480)

        player.expect_audio(True)
        player.write(self._tone(480), 24_000)
        streams[0].pump(480)

        self.assertEqual(0, player.starved_periods)

    def test_the_wait_for_a_piece_first_sentence_is_not_starvation(self) -> None:
        """Between taking a piece of text and its first audio, the model is working.

        Expected, and not a gap in anything: nothing of this piece has been
        heard yet for there to be a gap in.
        """
        player, streams, _sd = self._player()
        player.start()
        player.expect_audio(True)

        for _ in range(5):
            streams[0].pump(480)
        player.write(self._tone(480), 24_000)
        streams[0].pump(480)

        self.assertEqual(0, player.starved_periods)

    def test_the_silence_before_the_first_word_is_not_starvation(self) -> None:
        """A device open while the first sentence is being synthesized.

        The stream is started when the session is declared, so it asks for audio
        for as long as the model takes. That is synthesis latency and it is
        expected; counting it would make the number non-zero for every session
        that ever spoke.
        """
        player, streams, _sd = self._player()
        player.start()
        player.expect_audio(True)

        for _ in range(5):
            streams[0].pump(480)
        player.write(self._tone(480), 24_000)
        streams[0].pump(480)

        self.assertEqual(0, player.starved_periods)

    def test_the_last_partial_period_of_a_healthy_playback_is_not_starvation(self) -> None:
        """Every playback ends on a period the queue cannot fill.

        Counted where it happens, that makes the number non-zero for playback
        that was perfect, which leaves it saying nothing at all. It is only a
        gap if audio follows it.
        """
        player, streams, _sd = self._player()
        player.start()
        player.expect_audio(True)
        player.write(self._tone(500), 24_000)

        streams[0].pump(480)
        streams[0].pump(480)
        streams[0].pump(480)

        self.assertEqual(500, player.frames_played, "every frame written was played")
        self.assertEqual(0, player.starved_periods)

    def test_the_silence_after_an_interruption_is_not_charged_to_what_follows(self) -> None:
        """A person asking for silence is not synthesis failing to keep up."""
        player, streams, _sd = self._player()
        player.start()
        player.expect_audio(True)
        player.write(self._tone(480), 24_000)
        streams[0].pump(480)

        player.abort()
        for _ in range(3):
            streams[0].pump(480)
        player.write(self._tone(480), 24_000)
        streams[0].pump(480)

        self.assertEqual(0, player.starved_periods)

    def test_the_counters_survive_an_abort_and_a_reopen(self) -> None:
        """A session suspended for capture and resumed is one playback.

        Zeroing on reopen would clear the count every time the person dictated,
        which is exactly when they are investigating.
        """
        player, streams, _sd = self._player()
        player.start()
        player.expect_audio(True)
        player.write(self._tone(480), 24_000)
        streams[0].pump(480, status="output underflow")
        streams[0].pump(480)
        player.write(self._tone(480), 24_000)
        streams[0].pump(480)
        player.abort()
        player.stop()
        player.start()

        self.assertEqual(1, player.underruns)
        self.assertEqual(1, player.starved_periods)

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

    def test_speech_written_after_an_abort_still_reaches_the_device(self) -> None:
        """`abort` is Pa_AbortStream, which leaves the stream stopped.

        A player that does not start it again queues every later chunk to a
        device that will never ask for one. The session stays open and is never
        audible again: nothing plays, the played position never moves, and the
        sender is never told anything was heard.
        """
        player, streams, _sd = self._player()
        player.start()
        player.write(self._tone(4_800), 24_000)
        streams[0].pump(480)

        player.abort()
        player.write(self._tone(4_800), 24_000)
        streams[0].pump(480)

        self.assertTrue(player.active, "the player reports a device it does not have")
        self.assertEqual(960, player.frames_played, "audio after an abort was never played")

    def test_the_reported_device_is_the_one_that_opened(self) -> None:
        """Not the configured value, which is empty in a default installation.

        `murmly doctor` reports this name, and the spec has it naming the device
        speech would use. A person whose speech comes out of the wrong sink has
        nothing else to compare against.
        """
        opened: list[object] = []

        def check_output_settings(**kwargs: object) -> None:
            # Refuses the configured device and the system default, so selection
            # falls through to an indexed one and the name cannot come from the
            # configuration.
            if kwargs["device"] in (None, "Missing Headset"):
                raise FakePortAudioError("no such device")
            opened.append(kwargs["device"])

        player, _streams, _sd = self._player(check_output_settings=check_output_settings)
        player._config = replace(player._config, tts_output_device="Missing Headset")

        player.start()

        self.assertEqual([0], opened)
        self.assertEqual("Built-in Audio", player.output_device)
        self.assertIn("Missing Headset", player.device_detail)

    def test_the_channel_count_is_published_before_the_stream_starts(self) -> None:
        """PortAudio may call the callback before `start()` returns.

        The callback divides by the channel count to turn bytes into frames, so
        published afterwards it runs with the previous open's value and the
        played position is wrong from the first period -- and the position is
        what a sender is told the person heard.
        """
        def check_output_settings(**kwargs: object) -> None:
            if kwargs["channels"] == 1:
                raise FakePortAudioError("this device will not take mono")

        player, _streams, sounddevice = self._player(check_output_settings=check_output_settings)
        seen: list[int] = []

        def raw_output_stream(**kwargs: object) -> FakeOutputStream:
            stream = FakeOutputStream(kwargs["callback"], kwargs["samplerate"], kwargs["channels"])
            starting = stream.start

            def start_and_call_back() -> None:
                seen.append(player.channels)
                starting()

            stream.start = start_and_call_back
            return stream

        sounddevice.RawOutputStream = raw_output_stream
        player.start()

        self.assertEqual(2, player.channels, "the stereo fallback was expected here")
        self.assertEqual(
            [2], seen, "a callback at start() would have used the previous open's channel count"
        )

    def test_the_stream_is_halted_before_the_counters_are_squared(self) -> None:
        """The callback runs on its own thread and advances the played position.

        Pinning `_frames_written` to it first leaves one more period free to
        run, which pushes the played position past the written one -- and the
        position reported to a sender past what the device was ever given.
        Asserting the ordering rather than racing a real callback against it.
        """
        player, streams, _sd = self._player()
        player.start()
        player.write(self._tone(4_800), 24_000)
        observed: dict[str, int] = {}
        stream = streams[0]
        original = stream.abort

        def record_then_abort() -> None:
            observed["written"] = player.frames_written
            observed["played"] = player.frames_played
            original()

        stream.abort = record_then_abort

        player.abort()

        self.assertGreater(
            observed["written"],
            observed["played"],
            "the counters were squared up before the device was halted",
        )

    def test_a_device_that_will_not_restart_after_an_abort_is_closed(self) -> None:
        """Reported through `active`, so the next start rebuilds it.

        Leaving the stream in place would make `active` claim a working device
        while every write piled up behind a callback that never runs.
        """
        player, streams, _sd = self._player()
        player.start()
        player.write(self._tone(4_800), 24_000)
        streams[0].start = lambda: (_ for _ in ()).throw(FakePortAudioError("device gone"))

        player.abort()

        self.assertFalse(player.active)
        self.assertTrue(streams[0].closed)

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
        with injected_module("sounddevice", sounddevice):
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
        with injected_module("sounddevice", sounddevice):
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


class ExitTeardownTests(unittest.TestCase):
    """PortAudio tears down every host API it initialised when the process exits.

    Its JACK backend aborts the process when the server behind it has already
    gone, which is what a logout is, so the daemon drops the hook that runs it.
    """

    def test_nothing_is_unregistered_when_sounddevice_was_never_imported(self) -> None:
        with removed_module("sounddevice"):
            with patch("murmly.audio.atexit.unregister") as unregister:
                disable_portaudio_exit_teardown()

        unregister.assert_not_called()

    def test_the_exit_hook_is_unregistered_when_sounddevice_is_imported(self) -> None:
        module = ModuleType("sounddevice")
        module._exit_handler = lambda: None
        with injected_module("sounddevice", module):
            with patch("murmly.audio.atexit.unregister") as unregister:
                disable_portaudio_exit_teardown()

        unregister.assert_called_once_with(module._exit_handler)

    def test_a_hook_that_is_no_longer_there_is_reported_not_raised(self) -> None:
        module = ModuleType("sounddevice")
        with injected_module("sounddevice", module):
            with patch("murmly.audio.atexit.unregister") as unregister:
                with self.assertLogs("murmly.audio", level="WARNING") as logs:
                    disable_portaudio_exit_teardown()

        unregister.assert_not_called()
        self.assertIn("exit handler", logs.output[0])

    def test_the_installed_sounddevice_still_exposes_the_hook(self) -> None:
        """`_exit_handler` is private, so a release is free to rename it.

        This is what turns that rename into a failing test here rather than an
        abort at the user's next logout, where the warning above is the only
        sign anything changed.
        """
        try:
            import sounddevice
        except Exception as error:  # noqa: BLE001 - nothing to check against here
            self.skipTest(f"sounddevice is not importable: {error}")

        self.assertTrue(callable(getattr(sounddevice, "_exit_handler", None)))


class LivePlaybackTests(unittest.TestCase):
    """Against a real output device, which is the only place the fault existed.

    Every other playback test drives the callback itself, and a fake that is
    always ready cannot show a device stalling. The original fault -- a buffer
    shorter than one cycle of the audio graph underneath -- produced perfect
    frame counts and took twice as long to play them, which no fake reproduces.

    Silent audio throughout: the fault is in the timing, not the signal, so
    nothing here has to be audible to anyone near the machine.

    Skips itself rather than failing where there is no audio session, as the
    rest of the suite does. CI has no sound card.
    """

    TONE_SECONDS = 2.0
    RATE_HZ = 24_000

    def _player(self) -> SoundDevicePlayer:
        try:
            import sounddevice  # noqa: F401 - imported to see whether it loads
        except Exception as error:  # noqa: BLE001 - nothing to play through
            self.skipTest(f"sounddevice is not importable: {error}")
        temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp_dir, ignore_errors=True)
        config = MurmlyConfig(
            socket_path=Path(temp_dir) / "murmly.sock",
            config_path=Path(temp_dir) / "config.toml",
            tts_enabled=True,
        )
        player = SoundDevicePlayer(config)
        try:
            player.start()
        except Exception as error:  # noqa: BLE001 - no device is not a failure here
            self.skipTest(f"no output device to play through: {error}")
        self.addCleanup(player.stop)
        return player

    @staticmethod
    def _silence(seconds: float, rate_hz: int) -> list[float]:
        return [0.0] * int(seconds * rate_hz)

    def test_audio_plays_in_the_time_it_occupies_and_drops_nothing(self) -> None:
        """The assertion the original fault would have failed.

        It played every frame it was given and reported perfect counts; it just
        took 18 seconds to play 8 seconds of audio, because the device stalled
        and recovered on every graph cycle. Wall clock is what sees that.
        """
        player = self._player()
        chunk = self._silence(self.TONE_SECONDS, self.RATE_HZ)

        started = time.monotonic()
        for _ in range(2):
            while player.pending_frames > int(3.0 * player.sample_rate_hz):
                time.sleep(0.02)
            player.write(chunk, self.RATE_HZ)
        while player.pending_frames > 0:
            time.sleep(0.02)
        elapsed = time.monotonic() - started

        audio_seconds = 2 * self.TONE_SECONDS
        self.assertLess(
            elapsed,
            audio_seconds * 1.35,
            f"{audio_seconds:.1f} s of audio took {elapsed:.2f} s: the device is stalling",
        )
        self.assertEqual(0, player.underruns, "the device could not be kept fed")

    def test_a_buffer_far_larger_than_a_host_default_was_negotiated(self) -> None:
        """Not an assertion that the host granted what was asked for.

        `suggestedLatency` is a suggestion. sounddevice says the reported value
        "may differ significantly from the latency value(s) passed to Stream()",
        and CoreAudio granted 174.8 ms for a 200 ms request. The margin here is
        wide on purpose: it catches the latency argument being dropped
        altogether -- every host's own default is an order of magnitude smaller
        than this -- without asserting a contract PortAudio does not make.
        """
        player = self._player()

        self.assertGreater(player.output_latency_seconds, 0.0, "no buffer was reported")
        self.assertGreaterEqual(
            player.output_latency_seconds,
            MIN_PLAYBACK_LATENCY_SECONDS / 2,
            "the buffer looks like a host default, so the floor was not asked for",
        )

    def test_stopping_is_not_slowed_by_the_larger_buffer(self) -> None:
        """A deeper buffer holds more audio; the abort still discards it.

        `Pa_AbortStream` rather than `Pa_StopStream` is what keeps barge-in
        immediate, and enlarging the buffer is only safe while that stays true.
        """
        player = self._player()
        player.write(self._silence(10.0, self.RATE_HZ), self.RATE_HZ)
        # Enough for the device to have taken a period or two of it.
        time.sleep(0.1)

        started = time.monotonic()
        player.abort()
        elapsed = time.monotonic() - started

        self.assertLess(
            elapsed,
            0.5,
            "stopping waited for buffered audio instead of discarding it",
        )
        self.assertEqual(0, player.pending_frames, "audio survived the abort")


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
