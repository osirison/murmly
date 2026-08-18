---
title: Disable vad_filter when benchmarking faster-whisper on synthetic audio
description: Why timing a transcription pass over generated audio reports near-zero work unless the voice activity filter is turned off
trigger: model.transcribe, transcribe_partial, transcribe_pcm16, measure_partial_pass_ms, murmly doctor

depends_on: src/murmly/cli.py, src/murmly/stt.py, src/murmly/config.py
recorded: 2026-08-18
---

## Symptom

A timing harness over generated audio (a sine wave, shaped noise, anything not
real speech) reports roughly the same duration on every device and every model
profile — around 80-160 ms for a 15-second clip whether the run is CUDA
`float16` `large-v3-turbo` or CPU `int8`. The numbers look implausibly good and
implausibly uniform.

## Fix

Disable the voice activity filter for the measurement:

```python
from dataclasses import replace

transcriber = FasterWhisperTranscriber(replace(config, vad_filter=False))
```

Then discard one pass before timing the next, so the one-time model load is not
counted:

```python
transcriber.transcribe_partial(clip, sample_rate_hz)   # warm-up, discarded
started = time.perf_counter()
transcriber.transcribe_partial(clip, sample_rate_hz)   # the measurement
```

## Why it was not obvious

`vad_filter` defaults to `true` for the `balanced` and `accurate` profiles.
faster-whisper runs Silero VAD before decoding and passes only the detected
speech spans to the model. Synthetic audio is not speech, so Silero returns no
spans, the decoder receives nothing, and `transcribe()` returns an empty
transcript in a few milliseconds. Nothing errors and nothing warns — the call
succeeds, returns `''`, and the timing is real. It is simply timing the filter.

The tell is uniformity across devices: a GPU and a CPU should not agree on
decode latency.

## Reference figures

Measured on this machine with a 15-second window and `vad_filter = false`, so
the whole window is decoded. This is the worst case; real dictation is faster
because the filter removes silence.

| Device | Profile | Model | Ceiling |
| --- | --- | --- | --- |
| CUDA `float16` | `fast` | `tiny.en` | 244 ms |
| CUDA `float16` | `balanced` | `large-v3-turbo` | 319 ms |
| CPU `int8` | `fast` | `tiny.en` | 641 ms |
| CPU `int8` | `balanced` | `large-v3-turbo` | 12364 ms |

Re-measure rather than trusting these after a model, driver, or hardware change:
`murmly doctor` reports `live_transcription.partial_pass_ceiling_ms` whenever
`stt.live_transcribe` is enabled.
