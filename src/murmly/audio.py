from __future__ import annotations

from array import array
from collections import deque
import math
import sys
import threading
import time
from typing import Any

from murmly.config import MurmlyConfig
from murmly.overlay import LevelSink


MIN_LEVEL_DBFS = -60.0
MAX_LEVEL_DBFS = -6.0


def pcm16_rms(pcm_audio: bytes) -> float:
    usable_length = len(pcm_audio) - (len(pcm_audio) % 2)
    if usable_length == 0:
        return 0.0
    samples = array("h")
    samples.frombytes(pcm_audio[:usable_length])
    if sys.byteorder != "little":
        samples.byteswap()
    mean_square = sum(sample * sample for sample in samples) / len(samples)
    return min(math.sqrt(mean_square) / 32_768.0, 1.0)


def rms_to_level(rms: float) -> float:
    if rms <= 0.0:
        return 0.0
    dbfs = 20.0 * math.log10(min(rms, 1.0))
    return min(max((dbfs - MIN_LEVEL_DBFS) / (MAX_LEVEL_DBFS - MIN_LEVEL_DBFS), 0.0), 1.0)


class LevelSmoother:
    def __init__(self, attack: float = 0.55, release: float = 0.18) -> None:
        self._attack = attack
        self._release = release
        self._level = 0.0

    @property
    def level(self) -> float:
        return self._level

    def update(self, target: float) -> float:
        bounded_target = min(max(target, 0.0), 1.0)
        alpha = self._attack if bounded_target > self._level else self._release
        self._level = alpha * bounded_target + (1.0 - alpha) * self._level
        return self._level

    def reset(self) -> None:
        self._level = 0.0


class SoundDeviceRecorder:
    def __init__(self, config: MurmlyConfig, level_sink: LevelSink | None = None) -> None:
        self._config = config
        self._level_sink = level_sink
        self._level_smoother = LevelSmoother()
        self._latest_level_frame: bytes | None = None
        self._meter_stop: threading.Event | None = None
        self._meter_thread: threading.Thread | None = None
        self._stream = None
        self._blocks: deque[bytes] = deque()
        self._pending = bytearray()
        self._pending_lock = threading.Lock()
        self._level_smoother.reset()
        self._latest_level_frame = None
        self._sample_rate_hz = config.sample_rate_hz

    @property
    def sample_rate_hz(self) -> int:
        return self._sample_rate_hz

    @property
    def bytes_per_second(self) -> int:
        return self._sample_rate_hz * self._config.channels * 2

    def start(self) -> None:
        try:
            import sounddevice as sd
        except ModuleNotFoundError as error:
            raise RuntimeError(
                "sounddevice is required for microphone capture. Install it before starting murmly."
            ) from error

        self._blocks.clear()
        with self._pending_lock:
            self._pending = bytearray()

        def callback(indata: bytes, frames: int, time_info: object, status: object) -> None:
            del frames, time_info
            if status:
                raise RuntimeError(f"Audio capture error: {status}")
            pcm_audio = bytes(indata)
            self._blocks.append(pcm_audio)
            if self._level_sink is not None:
                self._latest_level_frame = pcm_audio

        failures: list[str] = []
        for device in self._candidate_devices(sd):
            for sample_rate_hz in self._candidate_sample_rates(sd, device):
                try:
                    sd.check_input_settings(
                        device=device,
                        channels=self._config.channels,
                        dtype="int16",
                        samplerate=sample_rate_hz,
                    )
                except (sd.PortAudioError, ValueError) as error:
                    failures.append(f"device={device!r}, rate={sample_rate_hz}: {error}")
                    continue

                stream = None
                try:
                    stream = sd.RawInputStream(
                        device=device,
                        samplerate=sample_rate_hz,
                        channels=self._config.channels,
                        dtype="int16",
                        callback=callback,
                    )
                    stream.start()
                except (sd.PortAudioError, ValueError) as error:
                    if stream is not None:
                        stream.close()
                    failures.append(f"device={device!r}, rate={sample_rate_hz}: {error}")
                    continue

                self._stream = stream
                self._sample_rate_hz = int(round(stream.samplerate))
                self._start_meter()
                return

        details = "; ".join(failures) or "No input devices were available."
        raise RuntimeError(f"Unable to open a microphone input. {details}")

    def stop(self) -> bytes:
        stream = self._stream
        self._stream = None
        if stream is None:
            self._stop_meter()
            return b""
        try:
            stream.stop()
        finally:
            try:
                stream.close()
            finally:
                self._stop_meter()
                self._level_smoother.reset()
                self._latest_level_frame = None
        return self.take_segment()

    def snapshot(self, window_seconds: float | None = None) -> bytes:
        """Return the audio captured since the last segment without stopping capture.

        A window bounds the copy to the trailing `window_seconds`, so a long
        recording does not cost more to inspect than a short one.
        """
        with self._pending_lock:
            self._drain_locked()
            if window_seconds is None:
                return bytes(self._pending)
            window_bytes = self._window_bytes(window_seconds)
            if window_bytes <= 0 or len(self._pending) <= window_bytes:
                return bytes(self._pending)
            return bytes(self._pending[-window_bytes:])

    def take_segment(self) -> bytes:
        """Return everything captured since the last call and reset the accumulator.

        More than one consumer can reach this: the live worker closes segments
        while the toggle path stops the recording. Draining and resetting under
        one lock is what stops those two losing a block into an orphaned buffer,
        or handing the same audio to two transcripts.
        """
        with self._pending_lock:
            self._drain_locked()
            segment = bytes(self._pending)
            self._pending = bytearray()
            return segment

    def _drain_locked(self) -> None:
        """Move blocks the capture callback produced into the consumer's accumulator.

        Callers must hold `_pending_lock`. `deque.append` in the callback and
        `popleft` here are the thread-safe pair, so the real-time audio path
        still takes no lock.
        """
        blocks = self._blocks
        pending = self._pending
        while True:
            try:
                pending.extend(blocks.popleft())
            except IndexError:
                return

    def _window_bytes(self, window_seconds: float) -> int:
        frame_bytes = self._config.channels * 2
        frames = int(self._sample_rate_hz * window_seconds)
        return max(frames, 0) * frame_bytes

    def record_for_seconds(self, seconds: float) -> bytes:
        self.start()
        time.sleep(seconds)
        return self.stop()

    def _candidate_devices(self, sounddevice: Any) -> list[int | None]:
        virtual_device_names = {"default", "pipewire", "pulse", "sysdefault"}
        candidates: list[int | None] = []
        try:
            default_device = sounddevice.query_devices(kind="input")
        except (sounddevice.PortAudioError, TypeError, ValueError):
            default_device = None

        if default_device is not None:
            candidates.append(None)

        for index, device in enumerate(sounddevice.query_devices()):
            if int(device["max_input_channels"]) < self._config.channels:
                continue
            if str(device["name"]).casefold() in virtual_device_names:
                continue
            candidates.append(index)
        return candidates

    def _candidate_sample_rates(self, sounddevice: Any, device: int | None) -> list[int]:
        sample_rates = [self._config.sample_rate_hz]
        try:
            if device is None:
                properties = sounddevice.query_devices(kind="input")
            else:
                properties = sounddevice.query_devices(device)
            native_sample_rate_hz = int(properties["default_samplerate"])
        except (sounddevice.PortAudioError, KeyError, TypeError, ValueError):
            return sample_rates

        if native_sample_rate_hz > 0 and native_sample_rate_hz not in sample_rates:
            sample_rates.append(native_sample_rate_hz)
        return sample_rates

    def _start_meter(self) -> None:
        if self._level_sink is None:
            return
        existing = self._meter_thread
        if existing is not None:
            if existing.is_alive():
                self._level_sink = None
                return
            self._meter_thread = None
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._run_meter,
            args=(stop_event,),
            name="murmly-audio-meter",
            daemon=True,
        )
        try:
            thread.start()
        except RuntimeError:
            self._level_sink = None
            return
        self._meter_stop = stop_event
        self._meter_thread = thread

    def _stop_meter(self) -> None:
        stop_event = self._meter_stop
        thread = self._meter_thread
        if stop_event is not None:
            stop_event.set()
        if thread is not None:
            thread.join(timeout=0.2)
            if thread.is_alive():
                self._level_sink = None
                return
        self._meter_thread = None
        self._meter_stop = None

    def _run_meter(self, stop_event: threading.Event) -> None:
        last_frame: bytes | None = None
        while not stop_event.wait(1.0 / 30.0):
            last_frame = self._publish_latest_frame(last_frame)
            if self._level_sink is None:
                return

    def _publish_latest_frame(self, last_frame: bytes | None) -> bytes | None:
        frame = self._latest_level_frame
        if frame is None or frame is last_frame or self._level_sink is None:
            return last_frame
        level = self._level_smoother.update(rms_to_level(pcm16_rms(frame)))
        try:
            self._level_sink(level)
        except Exception:
            self._level_sink = None
        return frame
