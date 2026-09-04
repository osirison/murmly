from __future__ import annotations

from array import array
import atexit
from collections import deque
import logging
import math
import sys
import threading
import time
from typing import Any

from murmly.config import MurmlyConfig
from murmly.overlay import LevelSink


logger = logging.getLogger(__name__)

MIN_LEVEL_DBFS = -60.0
MAX_LEVEL_DBFS = -6.0

# What the synthesis model produces. The device is preflighted at this rate
# first, so the common case costs no resampling at all.
DEFAULT_PLAYBACK_RATE_HZ = 24_000
MAX_PLAYBACK_CHANNELS = 2

# The floor under the output buffer, in seconds, and the rungs tried when a host
# refuses it.
#
# A host's own idea of its largest buffer is not necessarily larger than one
# cycle of the audio graph underneath it, and a buffer shorter than a cycle runs
# dry every cycle: the device stalls, recovers, and stalls again, which is heard
# as stuttering rather than speech. Measured on PipeWire: `latency="high"` on the
# ALSA default device resolved to 34.67 ms while the graph ran a 42.67 ms cycle,
# and 8 seconds of audio took 18 seconds to play with 213 device underflows. The
# same audio through 200 ms took 7.97 seconds with none.
#
# The two numbers are not independent, which is why raising the buffer works and
# lowering it does not. PipeWire sizes its cycle from the buffer asked for and
# rounds up -- 34.67 ms is 1664 frames at 48 kHz, which rounds to its 2048
# maximum -- while PortAudio subdivides that same buffer into periods of its own.
# Ask for less and the graph follows down; ask for more and the graph stops at
# its maximum while the buffer keeps growing. So the buffer only has to clear the
# largest cycle a graph will choose, and everything at or above it behaves the
# same: 45 ms was already clean here, and 200 ms leaves room for a host whose
# maximum is larger.
#
# In seconds rather than frames because a cycle is a duration, and the rate this
# stream runs at is negotiated with the device rather than chosen here.
MIN_PLAYBACK_LATENCY_SECONDS = 0.2
# Tried in order when the buffer above is refused. `"high"` last is what shipped
# before this floor existed, so the worst case is never worse than it was.
PLAYBACK_LATENCY_FALLBACKS: tuple[float | str, ...] = (0.1, "high")


def disable_portaudio_exit_teardown() -> None:
    """Stop PortAudio tearing its host APIs down when this process exits.

    `sounddevice` registers that teardown with `atexit` when it is imported. It
    disconnects every host API PortAudio initialised rather than only the one a
    stream was opened on, and the JACK one aborts the process when the server
    behind it has already gone -- which is what a logout is, since nothing
    orders a user service's stop before the audio server's.

    The caller owns closing the streams Murmly opened: the hook dropped here is
    also what closed the last one.

    It is also the only caller of `Pa_Terminate`, which is what stopped
    PortAudio's own `pw-PortAudio` loop threads. Dropping it leaves them running
    for the rest of the process, and interpreter finalization then unloads the
    extension libraries underneath them -- a SIGSEGV where issue #11 had a
    SIGABRT, and the same failed unit either way. The daemon therefore leaves
    through `leave_without_finalizing` in `cli.py` rather than returning into
    `Py_Finalize`. Anything else calling this must not rely on finalization
    running cleanly afterwards.

    A module that was never imported has no hook to drop, so this reads
    `sys.modules` rather than importing one just to disable it.
    """
    sounddevice = sys.modules.get("sounddevice")
    if sounddevice is None:
        return
    handler = getattr(sounddevice, "_exit_handler", None)
    if handler is None:
        # Reported, never raised. Losing this protection means a shutdown that
        # outlives the audio server can abort again; it does not mean this
        # shutdown should stop where it stands.
        logger.warning(
            "sounddevice no longer exposes the exit handler Murmly disables, so "
            "PortAudio will tear its host APIs down at exit. A shutdown that "
            "outlives the audio server can abort."
        )
        return
    atexit.unregister(handler)


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
        window = int(self.bytes_per_second * max(window_seconds, 0.0))
        return window - (window % frame_bytes)

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


def pcm16_from_float32(samples, channels: int = 1) -> bytes:
    """Little-endian int16 for the device, from the float32 a synthesizer yields.

    The inverse of the idiom `FasterWhisperTranscriber._as_array` uses on the way
    in. Clipped rather than scaled to fit: a sample past full scale is the
    model's fault and quieting the whole utterance to hide it would be worse.
    """
    import numpy as np

    audio = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    if channels > 1:
        audio = np.repeat(audio, channels)
    return np.round(audio * 32_767.0).astype("<i2").tobytes()


def resample_float32(samples, from_rate_hz: int, to_rate_hz: int):
    """Linear interpolation between two rates.

    The only other rate adaptation in the repository is integer decimation that
    refuses a non-integer ratio, because it feeds a voice activity model that
    aliased energy misleads. This one feeds a loudspeaker at whatever ratio the
    device negotiated, so it has to accept every ratio, and interpolation error
    at 24 kHz against 44.1 or 48 kHz is not audible.
    """
    import numpy as np

    audio = np.asarray(samples, dtype=np.float32)
    if from_rate_hz == to_rate_hz or audio.size == 0:
        return audio
    count = max(int(round(audio.size * to_rate_hz / from_rate_hz)), 1)
    source = np.arange(audio.size, dtype=np.float64)
    target = np.linspace(0.0, audio.size - 1, count, dtype=np.float64)
    return np.interp(target, source, audio).astype(np.float32)


class SoundDevicePlayer:
    """Plays synthesized audio, and stops playing it on demand.

    Selects a device the way `SoundDeviceRecorder` does -- preflight every
    candidate, open the first that takes the settings, and report one combined
    failure only once they are all exhausted -- and keeps the same lock-free
    discipline in the callback: the producer appends under a lock, the callback
    only ever calls `popleft`.
    """

    def __init__(
        self,
        config: MurmlyConfig,
        sample_rate_hz: int = DEFAULT_PLAYBACK_RATE_HZ,
    ) -> None:
        self._config = config
        self._preferred_sample_rate_hz = sample_rate_hz
        self._sample_rate_hz = sample_rate_hz
        self._channels = 1
        self._stream = None
        self._blocks: deque[bytes] = deque()
        self._carry = b""
        self._write_lock = threading.Lock()
        self._frames_written = 0
        self._frames_played = 0
        # Counted for the life of the process, not the life of a stream. The
        # spec asks how many dropouts there have been since the daemon started,
        # and a session suspended for capture and resumed afterwards reopens the
        # device -- so resetting these in `start()` would zero the count every
        # time the person dictated, which is exactly when they are investigating.
        self._underruns = 0
        self._starved_periods = 0
        # The run of silent periods not yet decided either way, whether audio
        # has played for the piece being produced, and whether a producer is
        # working on one at all. All three belong to the stream rather than to
        # the process, so unlike the two counters above they are reset by
        # `start()`: a run left open across a reopen would be committed by the
        # next session's first audio and counted against it.
        self._silent_periods = 0
        self._played_any = False
        self._expecting_audio = False
        self._output_latency_seconds = 0.0
        self._device_detail: str | None = None
        self._device_name: str | None = None

    @property
    def output_device(self) -> str | None:
        """The device that opened, which is not always the one configured.

        Reported by name rather than by the configured value, because the
        configured value is empty in the default installation and a person whose
        speech is coming out of the wrong sink has nothing to compare.
        """
        return self._device_name

    @property
    def sample_rate_hz(self) -> int:
        """The rate the device negotiated, which is not always the one asked for."""
        return self._sample_rate_hz

    @property
    def channels(self) -> int:
        return self._channels

    @property
    def frames_played(self) -> int:
        """Frames handed to the device, which is what the person has heard.

        Never what has been produced. Producing runs ahead of playing by whole
        sentences, and reporting the production frontier would tell a sender the
        person heard something they did not.
        """
        return self._frames_played

    @property
    def frames_written(self) -> int:
        return self._frames_written

    @property
    def pending_frames(self) -> int:
        return max(self._frames_written - self._frames_played, 0)

    @property
    def underruns(self) -> int:
        """Periods the device reported it could not be fed in time.

        The device's own complaint, raised by the host rather than counted here,
        and the one that says the output buffer is too small for the audio graph
        underneath it. Never reset by opening or reopening the stream: a session
        suspended for capture and resumed is one playback to the person listening.
        """
        return self._underruns

    @property
    def starved_periods(self) -> int:
        """Periods filled with silence because nothing had been produced yet.

        The other half of the pipeline, and the reason it is not folded into
        `underruns`: this one says synthesis did not keep up, that one says the
        device was not fed in time, and a person looking at a single number
        cannot tell which of the two they are looking at.
        """
        return self._starved_periods

    @property
    def output_latency_seconds(self) -> float:
        """The output buffer the stream negotiated, which is what it holds.

        Audio counted as written has been handed to this buffer, not played out
        of it, so this is how far ahead of the person's ears the written position
        runs. Zero when no stream is open.
        """
        return self._output_latency_seconds

    @property
    def device_detail(self) -> str | None:
        """Why the device in use is not the one configured, when it is not."""
        return self._device_detail

    @property
    def active(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        try:
            import sounddevice as sd
        except ModuleNotFoundError as error:
            raise RuntimeError(
                "sounddevice is required for speech output. Install it before enabling it."
            ) from error

        if self._stream is not None:
            return
        with self._write_lock:
            self._blocks.clear()
        self._carry = b""
        self._frames_written = 0
        self._frames_played = 0
        self._silent_periods = 0
        self._played_any = False
        self._expecting_audio = False
        self._output_latency_seconds = 0.0
        self._device_detail = None
        self._device_name = None

        configured = self._configured_device()
        failures: list[str] = []
        for device in self._candidate_devices(sd, configured):
            for channels in self._candidate_channels(sd, device):
                for sample_rate_hz in self._candidate_sample_rates(sd, device):
                    stream = self._open(sd, device, channels, sample_rate_hz, failures)
                    if stream is None:
                        continue
                    self._stream = stream
                    self._sample_rate_hz = int(round(stream.samplerate))
                    name = self._device_property(sd, device, "name")
                    self._device_name = (
                        str(name) if name is not None else self._device_label(device)
                    )
                    if configured is not None and device != configured:
                        self._device_detail = (
                            f"The configured output device {configured!r} could not be "
                            f"opened; using {device if device is not None else 'the system default'} "
                            f"instead."
                        )
                    return

        details = "; ".join(failures) or "No output devices were available."
        raise RuntimeError(f"Unable to open an audio output. {details}")

    def _open(self, sd, device, channels: int, sample_rate_hz: int, failures: list[str]):
        try:
            sd.check_output_settings(
                device=device,
                channels=channels,
                dtype="int16",
                samplerate=sample_rate_hz,
            )
        except (sd.PortAudioError, ValueError) as error:
            failures.append(f"device={device!r}, channels={channels}, rate={sample_rate_hz}: {error}")
            return None

        # The buffer is tried, never preflighted: `check_output_settings` takes
        # no latency argument, so the only way to learn whether a host will give
        # one is to ask for it. The ladder is walked here rather than around the
        # candidate loop on purpose -- a host that refuses the preferred buffer
        # on the best device should keep that device with a smaller buffer, not
        # move to a worse device to hold the buffer.
        for latency in self._candidate_latencies(sd, device):
            stream = None
            try:
                stream = sd.RawOutputStream(
                    device=device,
                    samplerate=sample_rate_hz,
                    channels=channels,
                    dtype="int16",
                    latency=latency,
                    callback=self._callback,
                )
                # Published before the stream starts, not after it is chosen. The
                # callback divides by this to count frames, and PortAudio can call
                # it before `start()` returns -- with the previous open's value it
                # reads the wrong number of bytes per frame and the played position
                # is wrong from the first period. The retry above does not change
                # this: every attempt publishes it before its own `start()`.
                self._channels = channels
                stream.start()
            except (sd.PortAudioError, ValueError) as error:
                if stream is not None:
                    stream.close()
                failures.append(
                    f"device={device!r}, channels={channels}, rate={sample_rate_hz}, "
                    f"latency={latency!r}: {error}"
                )
                continue
            # What the host gave, which is not always what was asked for --
            # `suggestedLatency` is a suggestion, and sounddevice says so:
            # "It may differ significantly from the latency value(s) passed to
            # Stream()". Measured: CoreAudio granted 174.8 ms for a 200 ms
            # request. The everything-heard report waits this out, so it has to
            # be the negotiated value rather than the requested one.
            self._output_latency_seconds = self._reported_latency(stream)
            return stream

    def expect_audio(self, expected: bool) -> None:
        """Say whether a producer is working on the audio this is playing.

        The one thing the callback cannot see for itself. An empty queue looks
        identical whether synthesis fell behind or the sender simply has not
        sent the next piece of text yet, and only the first is a fault -- so
        without this the starvation count reports a sender's think-time as a
        synthesizer that is too slow, which is the opposite of where it would
        send someone looking.

        Both edges clear the pending run. Opening one starts the new piece with
        nothing owed from the last; closing one discards the silence after its
        final chunk, which is the tail every playback ends on rather than a gap
        anybody heard.
        """
        self._expecting_audio = expected
        self._silent_periods = 0
        if expected:
            # Silence before this piece has been heard from at all is the model
            # working on its first sentence, which is expected and is not a
            # dropout. Counting starts once it has produced something.
            self._played_any = False

    def _forget_silent_run(self) -> None:
        """Drop the undecided run of silent periods, because playback was cut.

        Called from `abort`, with the device already stopped so the callback
        cannot be adding to it. The silence around an interruption is the person
        asking for silence, not synthesis failing to keep up, and the silence
        after one would otherwise be committed against whatever the session
        speaks next.
        """
        self._silent_periods = 0
        self._played_any = False
        self._expecting_audio = False

    @staticmethod
    def _reported_latency(stream) -> float:
        """The output half of whatever shape the host reported its latency in.

        `sounddevice` sets `_latency` to a bare float for a stream that is only
        one direction and to an `(input, output)` pair for a duplex one. Nothing
        here opens a duplex stream -- `RawOutputStream` is output only, so the
        float is what this actually sees -- but reading the pair costs a line
        and the alternative is falling back to zero, which would silently
        release the everything-heard hold and report no buffer at all.
        """
        reported = getattr(stream, "latency", 0.0)
        if isinstance(reported, (tuple, list)):
            reported = reported[-1] if reported else 0.0
        try:
            return float(reported or 0.0)
        except (TypeError, ValueError):
            # A host that reports its buffer as something other than a number of
            # seconds has still opened a working stream. Zero means the report is
            # not held back rather than held forever, which is what shipped
            # before the hold existed.
            return 0.0
        return None

    def _candidate_latencies(self, sounddevice, device) -> list[float | str]:
        """The output buffers to try, largest first.

        The device's own advertised largest is a floor to raise rather than the
        value to use: see `MIN_PLAYBACK_LATENCY_SECONDS` for what it fails to
        cover. A device that advertises more than the floor keeps its own answer,
        because a host asking for a large buffer knows something about itself
        that this constant does not.
        """
        advertised = self._device_property(sounddevice, device, "default_high_output_latency")
        try:
            preferred = max(MIN_PLAYBACK_LATENCY_SECONDS, float(advertised or 0.0))
        except (TypeError, ValueError):
            preferred = MIN_PLAYBACK_LATENCY_SECONDS
        return [preferred, *PLAYBACK_LATENCY_FALLBACKS]

    def _callback(self, outdata, frames: int, time_info: object, status: object) -> None:
        """Fill one device period from the queue, taking no lock.

        Never raises. The capture callback does, because a capture fault has to
        stop the recording; a playback fault must not tear down the stream in
        the middle of a sentence, so an underrun is counted and reported.
        """
        del time_info
        if status:
            self._underruns += 1
        wanted = frames * self._channels * 2
        view = memoryview(outdata).cast("B")
        filled = 0
        while filled < wanted:
            if not self._carry:
                try:
                    self._carry = self._blocks.popleft()
                except IndexError:
                    break
            take = min(len(self._carry), wanted - filled)
            view[filled : filled + take] = self._carry[:take]
            self._carry = self._carry[take:]
            filled += take
        played = filled // (self._channels * 2)
        if played:
            # Audio after a silent stretch is what proves the stretch was a gap
            # in the middle of playback. Committed here rather than counted as
            # it happened, because the same shortfall means two different things
            # depending on what follows it: a gap the person heard as a break in
            # the words, or the last period of an utterance that simply ended.
            # Every healthy playback ends on a partial period, so counting them
            # where they occur makes the number non-zero for playback that was
            # perfect and leaves it saying nothing.
            if self._silent_periods:
                self._starved_periods += self._silent_periods
                self._silent_periods = 0
            self._played_any = True
        if filled < wanted:
            # Silence rather than stale audio, and not counted as played: a
            # position that advanced through an underrun would report speech
            # nobody heard. Held as a pending run rather than counted: see
            # above. Counted only between two pieces of audio the *same* piece
            # of text produced -- `_expecting_audio` says a producer is working
            # on one, `_played_any` says that one has been heard from. Silence
            # outside that pair is not synthesis falling behind: before the
            # first audio it is the model working on the first sentence, and
            # between two pieces of text it is a sender that has not sent the
            # next one yet, which the player cannot tell from an empty queue
            # and must not report as a synthesizer that is too slow.
            view[filled:wanted] = bytes(wanted - filled)
            if self._played_any and self._expecting_audio:
                self._silent_periods += 1
        self._frames_played += played

    def write(self, samples, sample_rate_hz: int) -> int:
        """Queue one synthesized chunk, and report how many frames it became."""
        audio = resample_float32(samples, sample_rate_hz, self._sample_rate_hz)
        pcm_audio = pcm16_from_float32(audio, self._channels)
        frames = len(pcm_audio) // (self._channels * 2)
        if frames == 0:
            return 0
        with self._write_lock:
            self._blocks.append(pcm_audio)
            self._frames_written += frames
        return frames

    def abort(self) -> int:
        """Stop audio already handed to the device, and report what was played.

        Draining instead would keep speaking for as long as the device buffer
        holds, which is the one thing a person reaching for the hotkey is asking
        it not to do.
        """
        stream = self._stream
        if stream is not None:
            # Halted before the counters are squared up. The callback advances
            # `_frames_played` from its own thread, so pinning `_frames_written`
            # to it first leaves one more period free to run and push the played
            # position past the written one -- and the reported position past
            # what the device was ever given.
            try:
                stream.abort()
            except Exception as error:  # noqa: BLE001 - the caller is stopping, not starting
                logger.warning("Audio output did not abort cleanly: %s", error)
        with self._write_lock:
            self._blocks.clear()
            self._frames_written = self._frames_played
        if stream is None:
            self._carry = b""
            self._forget_silent_run()
            return self._frames_played
        # Cleared while the device is stopped, so the callback cannot be holding
        # a slice of it.
        self._carry = b""
        self._forget_silent_run()
        try:
            # `abort` is Pa_AbortStream, which leaves the stream *stopped*. The
            # callback is not invoked again until the stream is started, so
            # without this the next write() is queued to a device that will
            # never ask for it: nothing plays, the played position never moves,
            # and the session is silent for the rest of its life. Restarting
            # here keeps `active` meaning what it says.
            stream.start()
        except Exception as error:  # noqa: BLE001 - reported through `active`, not raised
            logger.warning("Audio output could not be restarted after an abort: %s", error)
            try:
                self.stop()
            except Exception as close_error:  # noqa: BLE001 - already down; nothing left to save
                logger.warning("Audio output did not close after a failed restart: %s", close_error)
        return self._frames_played

    def stop(self) -> None:
        """Close the output, leaving nothing open if the stop itself fails."""
        stream = self._stream
        self._stream = None
        with self._write_lock:
            self._blocks.clear()
        self._carry = b""
        if stream is None:
            return
        try:
            stream.stop()
        finally:
            stream.close()

    def _configured_device(self) -> int | str | None:
        configured = self._config.tts_output_device.strip()
        if not configured:
            return None
        return int(configured) if configured.isdigit() else configured

    def _candidate_devices(self, sounddevice, configured) -> list[object]:
        """The configured device first, then the system default, then the rest.

        The configured one is not the only candidate: the spec says a device
        that cannot be opened is reported alongside what was used instead, which
        means there has to be something used instead.
        """
        candidates: list[object] = [] if configured is None else [configured]
        try:
            default_device = sounddevice.query_devices(kind="output")
        except (sounddevice.PortAudioError, TypeError, ValueError):
            default_device = None
        if default_device is not None:
            candidates.append(None)

        virtual_device_names = {"default", "pipewire", "pulse", "sysdefault"}
        try:
            devices = list(enumerate(sounddevice.query_devices()))
        except (sounddevice.PortAudioError, TypeError, ValueError):
            devices = []
        for index, device in devices:
            if int(device.get("max_output_channels", 0)) < 1:
                continue
            if str(device["name"]).casefold() in virtual_device_names:
                continue
            candidates.append(index)
        return candidates

    def _candidate_channels(self, sounddevice, device) -> list[int]:
        """Mono first. A device that will not take it gets the mono signal twice."""
        channels = [1]
        maximum = self._device_property(sounddevice, device, "max_output_channels")
        if maximum is not None and maximum >= 2:
            channels.append(MAX_PLAYBACK_CHANNELS)
        return channels

    def _candidate_sample_rates(self, sounddevice, device) -> list[int]:
        sample_rates = [self._preferred_sample_rate_hz]
        native = self._device_property(sounddevice, device, "default_samplerate")
        if native is not None and int(native) > 0 and int(native) not in sample_rates:
            sample_rates.append(int(native))
        return sample_rates

    @staticmethod
    def _device_label(device) -> str:
        """A name for a device the host could not describe."""
        return "the system default" if device is None else str(device)

    @staticmethod
    def _device_property(sounddevice, device, key: str):
        try:
            if device is None:
                properties = sounddevice.query_devices(kind="output")
            else:
                properties = sounddevice.query_devices(device)
            return properties[key]
        except (sounddevice.PortAudioError, KeyError, TypeError, ValueError):
            return None
