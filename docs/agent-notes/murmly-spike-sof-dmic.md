---
title: Unmute SOF digital microphones before recording
description: Diagnose exact-silence captures from Murmly on Intel SOF audio hardware
trigger: uv run murmly spike, uv run murmly daemon, pw-record
depends_on: src/murmly/audio.py, src/murmly/stt.py
recorded: 2026-08-15
---

## Symptom

Murmly repeatedly transcribes `you`, or a retained recording contains only zero
samples even though PipeWire reports a selected, unmuted digital microphone.

## Fix

Find the SOF card number and inspect its digital microphone controls:

```bash
arecord -l
amixer -c 1 scontrols | grep -i dmic
amixer -c 1 sget Dmic0
```

Replace `1` with the SOF card number reported by `arecord -l`. If the `Dmic0`
capture channels are `[off]`, enable them:

```bash
amixer -c 1 sset Dmic0 cap
```

Confirm the fix with a retained PipeWire recording. A healthy speech recording
has finite peak and RMS levels; an exact-silence capture reports `-inf` for both.

```bash
pw-record --rate 16000 --channels 1 --format s16 --sample-count 128000 \
  /tmp/murmly-diagnostic.wav
ffmpeg -hide_banner -i /tmp/murmly-diagnostic.wav \
  -af astats=metadata=1:reset=0 -f null -
```

## Why it was not obvious

PipeWire can show the source at full volume and not muted while the lower-level
ALSA capture switch remains off. PortAudio and PipeWire then open successfully
and return correctly sized buffers containing only zeros.
