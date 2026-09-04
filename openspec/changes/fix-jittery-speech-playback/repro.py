"""Reproduce the playback jitter without touching project code.

Drives murmly's own SoundDevicePlayer with pre-generated PCM so synthesis speed
is not a variable, and counts two things the shipped player conflates:

  pads    -- the callback found the queue empty and wrote silence (producer
             starvation, or the stream draining faster than it is fed)
  status  -- PortAudio raised a status flag (the callback itself ran late)

Also records every callback's arrival time and requested frame count, so an
irregular delivery cadence shows up directly.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from array import array
from pathlib import Path

def _source_root() -> Path:
    """The `src/` holding `murmly`, found by walking up from this file.

    Not a fixed path: this script is committed with the change and outlives the
    worktree it was written in, and it moves a directory deeper when the change
    is archived. Walking up keeps it runnable from wherever it lands, which is
    the whole point of committing it rather than leaving it in a scratch
    directory.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "src" / "murmly"
        if candidate.is_dir():
            return parent / "src"
    raise SystemExit("could not find the murmly source tree above this script")


sys.path.insert(0, str(_source_root()))

from murmly.audio import SoundDevicePlayer  # noqa: E402
from murmly.config import load_config  # noqa: E402

LOOKAHEAD_SECONDS = 3.0
POLL_SECONDS = 0.02
SYNTH_RATE_HZ = 24_000
AMPLITUDE = 0.0  # silent: underflow behaviour does not depend on signal content


class InstrumentedPlayer(SoundDevicePlayer):
    """The shipped player, plus the counters the shipped one does not keep."""

    def __init__(self, *args, latency=None, blocksize=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.pads = 0
        self.pad_frames = 0
        self.status_flags = 0
        self.calls = 0
        self.arrivals: list[float] = []
        self.frames_requested: list[int] = []
        self._latency = latency
        self._blocksize = blocksize

    def _callback(self, outdata, frames, time_info, status):
        now = time.perf_counter()
        self.calls += 1
        self.arrivals.append(now)
        self.frames_requested.append(frames)
        if status:
            self.status_flags += 1
        before = self._frames_played
        super()._callback(outdata, frames, time_info, status)
        got = self._frames_played - before
        if got < frames:
            self.pads += 1
            self.pad_frames += frames - got

    def _open(self, sd, device, channels, sample_rate_hz, failures):
        """Same as the shipped one, with latency/blocksize made explicit."""
        try:
            sd.check_output_settings(
                device=device, channels=channels, dtype="int16", samplerate=sample_rate_hz
            )
        except (sd.PortAudioError, ValueError) as error:
            failures.append(f"device={device!r} ch={channels} rate={sample_rate_hz}: {error}")
            return None
        stream = None
        extra = {}
        if self._latency is not None:
            extra["latency"] = self._latency
        if self._blocksize is not None:
            extra["blocksize"] = self._blocksize
        try:
            stream = sd.RawOutputStream(
                device=device,
                samplerate=sample_rate_hz,
                channels=channels,
                dtype="int16",
                callback=self._callback,
                **extra,
            )
            self._channels = channels
            stream.start()
        except (sd.PortAudioError, ValueError) as error:
            if stream is not None:
                stream.close()
            failures.append(f"device={device!r} ch={channels} rate={sample_rate_hz}: {error}")
            return None
        self.stream_latency = stream.latency
        self.stream_blocksize = stream.blocksize
        return stream


def sentence_pcm(seconds: float, rate: int = SYNTH_RATE_HZ):
    """A tone with an envelope, standing in for one synthesized sentence."""
    import numpy as np

    n = int(seconds * rate)
    t = np.arange(n, dtype=np.float32) / rate
    tone = np.sin(2 * math.pi * 220.0 * t) + 0.3 * np.sin(2 * math.pi * 440.0 * t)
    fade = np.minimum(np.minimum(t, seconds - t) / 0.02, 1.0).astype(np.float32)
    return (tone * fade * AMPLITUDE).astype(np.float32)


def run(label: str, latency, blocksize, sentences: int, seconds: float, synth_delay: float):
    config = load_config()
    player = InstrumentedPlayer(config, latency=latency, blocksize=blocksize)
    player.start()

    chunk = sentence_pcm(seconds)
    started = time.perf_counter()
    lookahead = int(LOOKAHEAD_SECONDS * player.sample_rate_hz)
    for _ in range(sentences):
        if synth_delay:
            time.sleep(synth_delay)
        while player.pending_frames > lookahead:
            time.sleep(POLL_SECONDS)
        player.write(chunk, SYNTH_RATE_HZ)
    while player.pending_frames > 0:
        time.sleep(POLL_SECONDS)
    elapsed = time.perf_counter() - started

    gaps = [b - a for a, b in zip(player.arrivals, player.arrivals[1:])]
    result = {
        "run": label,
        "device": player.output_device,
        "rate_hz": player.sample_rate_hz,
        "channels": player.channels,
        "stream_latency_s": round(getattr(player, "stream_latency", 0.0), 5),
        "stream_blocksize": getattr(player, "stream_blocksize", None),
        "callbacks": player.calls,
        "frames_min": min(player.frames_requested) if player.frames_requested else 0,
        "frames_max": max(player.frames_requested) if player.frames_requested else 0,
        "pads": player.pads,
        "pad_frames": player.pad_frames,
        "pad_ms": round(1000.0 * player.pad_frames / max(player.sample_rate_hz, 1), 1),
        "status_flags": player.status_flags,
        "audio_s": round(sentences * seconds, 2),
        "wall_s": round(elapsed, 2),
    }
    if gaps:
        result["cb_gap_ms"] = {
            "median": round(1000 * statistics.median(gaps), 2),
            "p95": round(1000 * sorted(gaps)[int(0.95 * len(gaps))], 2),
            "max": round(1000 * max(gaps), 2),
            "over_2x_median": sum(1 for g in gaps if g > 2 * statistics.median(gaps)),
        }
    player.stop()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="run")
    parser.add_argument("--latency", default=None)
    parser.add_argument("--blocksize", type=int, default=None)
    parser.add_argument("--sentences", type=int, default=6)
    parser.add_argument("--seconds", type=float, default=2.0)
    parser.add_argument("--synth-delay", type=float, default=0.0)
    args = parser.parse_args()

    latency = args.latency
    if latency is not None and latency not in ("low", "high"):
        latency = float(latency)

    print(json.dumps(run(args.label, latency, args.blocksize, args.sentences, args.seconds, args.synth_delay)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
