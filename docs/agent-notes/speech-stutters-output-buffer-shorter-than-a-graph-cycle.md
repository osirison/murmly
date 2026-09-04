---
title: Stuttering speech is measured in wall clock, not in dropped frames
description: A playback buffer shorter than one cycle of the audio graph underneath stalls every cycle while reporting perfect frame counts; the fault is only visible as audio taking longer to play than it occupies, and the two counters that tell a device fault from a synthesis one
trigger: speech stutters, jittery speech, choppy playback, murmly sounds horrible, playback_dropouts, playback_starvations, negotiated_output_buffer_ms, pw-top, MIN_PLAYBACK_LATENCY_SECONDS

depends_on: src/murmly/audio.py, src/murmly/speech.py, openspec/changes/fix-jittery-speech-playback/repro.py
recorded: 2026-09-04
---

# Stuttering speech is measured in wall clock, not in dropped frames

**Symptom:** speech stutters, and it is much worse while anything else is
playing. Every frame is accounted for and no error is logged.

## Measure the wall clock against the audio's own duration

This is the whole diagnosis. A stalling device plays every frame it was given
and reports perfect counts; it just takes longer to do it. Eight seconds of
audio that takes eighteen seconds to come out is the fault, and nothing else
sees it.

```bash
PYTHONPATH="$PWD/src" python3 openspec/changes/.../repro.py --sentences 4 --seconds 2
```

The harness is committed with the change that fixed this; after it is archived,
`tests/test_audio.py::LivePlaybackTests` asserts the same thing against the real
device and skips itself where there is none. `AMPLITUDE = 0.0` keeps a run
silent -- the fault is in the timing, not the signal, so nothing has to be
audible to reproduce it.

## Two counters, and they mean opposite things

`murmly doctor`, under `speech_output`:

| Field | Above zero means |
| --- | --- |
| `playback_dropouts` | the device asked and could not be fed in time -- the buffer is too small for the audio graph |
| `playback_starvations` | the device asked in good time, mid-utterance, and nothing was synthesized yet -- synthesis is behind |

`playback_starvations` counts only gaps with audio on both sides. The wait before the
first sentence, and the partial period every playback ends on, are both expected and are
not counted -- otherwise the number would be non-zero for playback that was perfect.

Do not conflate them. They send an investigation to opposite halves of the
pipeline, which is why they are counted apart. Both read `null` beside a
`playback_detail` when the running service predates them; `null` is "not
measured", not zero.

## The cause, when it is the first counter

The output buffer has to be longer than one cycle of the audio graph
underneath. PipeWire's cycle here is `clock.quantum / clock.rate`:

```bash
pw-metadata -n settings | grep -E 'clock.(quantum|rate)'   # 2048 / 48000 = 42.67 ms
pw-top -b -n 2 | tail                                       # QUANT and RATE per node, live
```

`sounddevice`'s default `latency="high"` is not a safe answer to this. It
resolves to whatever the device advertises -- 34.67 ms on the ALSA `default`
device here -- which was *shorter* than the cycle, so the buffer ran dry on
every cycle: the device stalled, recovered, and stalled again.

The two numbers are not independent, which is why lowering the buffer does not
escape it. PipeWire sizes its cycle from the buffer asked for and rounds up
(34.67 ms is 1664 frames at 48 kHz, which rounds to the 2048 maximum), while
PortAudio subdivides that same buffer into periods of its own. Ask for less and
the graph follows down; ask for more and the graph stops at `clock.max-quantum`
while the buffer keeps growing. So the fix is a floor:
`MIN_PLAYBACK_LATENCY_SECONDS` in `src/murmly/audio.py`, 200 ms, with a ladder
down to the host's own answer for a host that refuses it.

The threshold is sharp, and it sits exactly at the cycle. Measured with another
stream playing, 8 s of audio:

| Buffer asked for | Dropouts | Wall clock |
| --- | --- | --- |
| 34.67 ms (`latency="high"`) | 213 | 18.13 s |
| 40 ms | 61 | 10.66 s |
| **42.67 ms -- one cycle** | | |
| 45 ms | 0 | 7.98 s |
| 200 ms | 0 | 7.97 s |

## Three things it is not, all ruled out by measurement

- **Synthesis falling behind.** The harness feeds pre-generated audio that is
  always ready, and the fault reproduces unchanged.
- **Resampling.** `resample_float32` only runs when the device refuses 24 kHz.
  It does not here, so it never runs.
- **The byte slicing in the playback callback.** It is O(n^2) in chunk length
  and worth cleaning up, but it costs about 0.1 % of the callback's budget and
  is identical in the runs that pass and the runs that fail.

## A dead end worth not repeating

Murmly's process keeps a PortAudio **JACK** client node in the PipeWire graph
for the life of the daemon -- `client.api = "jack"`, `node.always-process`,
`node.lock-quantum` -- because PortAudio initialises every host API at import.
`node.lock-quantum` looks like it would pin the graph quantum and explain
everything. It does not: opening a stream that asks for a 5 ms buffer moved the
quantum from 2048 to 128 with the daemon running. PipeWire follows the client.

The stream itself is on the **ALSA** host API, not JACK. `sd.query_hostapis()`
reports ALSA at index 0 and it is the default; the JACK node is a side effect of
initialisation, and the only other thing it is responsible for is the exit
teardown `disable_portaudio_exit_teardown` unregisters.

**Why it was not obvious:** nothing reported it. The underrun counter existed in
`SoundDevicePlayer` and no command read it, so the fault could only ever be
described as a sound. That is what the two `doctor` fields above are for.
