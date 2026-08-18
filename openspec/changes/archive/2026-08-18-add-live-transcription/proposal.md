---
title: Add Live Transcription
description: Show partial transcripts while listening and let a configurable run of silence end a recording or close a segment on the user's behalf
---

## Why

Murmly tells the user nothing about what it heard until the transcript lands in
their application. Between toggle-off and paste there is only a processing
symbol, so a misheard sentence, a muted microphone, or a wrong input device is
discovered after the fact — when the damage is already in the document. The wait
also scales with how long the user spoke, because the entire utterance is
transcribed in a single blocking pass only after capture stops.

Ending a session also requires reaching for the hotkey a second time, which is
exactly what a hands-busy dictation tool should not demand mid-sentence.

## What Changes

- **Live transcription.** While listening, Murmly transcribes the audio captured
  so far on a repeating interval and shows the partial text in the recording
  overlay. Partial text is feedback only: it never reaches the clipboard, never
  reaches the focused application, and never determines what is delivered. The
  delivered transcript is still produced by a final pass over the complete
  recording, so today's delivery behavior is bit-for-bit unchanged.
- **Auto-transcribe on silence.** Murmly detects a configurable run of silence
  (default 2000 ms) during capture and acts on it in one of two user-selected
  modes:
  - `stop` — behaves as though the user pressed the hotkey: capture stops, the
    delivery target is recorded, the transcript is produced and delivered, and
    Murmly returns to idle.
  - `continuous` — the silence closes a segment; that segment is transcribed and
    delivered while capture keeps running for the next utterance. The session
    ends on an explicit toggle, on a refused delivery, or on error.
- **Both features are opt-in and independent.** `stt.live_transcribe` defaults to
  false and `stt.auto_transcribe` defaults to `off`, so an untouched
  configuration behaves exactly as it does today. Either can be enabled without
  the other.
- **The overlay gains a partial-text presentation.** Its control protocol
  currently admits only lifecycle states and audio levels; it gains a bounded
  text channel used solely during listening. The error presentation continues to
  expose no transcribed text, and partial text is discarded on every transition
  out of listening.
- **Continuous sessions deliver more than one transcript.** Delivery target
  verification therefore runs once per segment rather than once per session, and
  clipboard restoration is serialized so one segment's restore cannot overwrite
  the next segment's transcript.
- **Diagnostics report the new configuration** and whether the session can
  actually support silence detection at the negotiated capture rate.

No existing configuration key, spec requirement, or toggle response field changes
meaning. This change is additive.

## Capabilities

### New Capabilities

- `live-transcription`: incremental transcription during capture — partial
  results produced on an interval while listening, silence-triggered
  auto-transcribe in stop and continuous modes, the configuration that governs
  both, and the guarantee that neither path degrades the transcript that is
  finally delivered.

### Modified Capabilities

- `recording-overlay`: the listening presentation may display partial transcript
  text; the processing presentation is not shown for background segments while
  capture continues; error and idle transitions must discard partial text.
- `transcript-delivery`: a single capture session may produce multiple
  transcripts, each with its own recorded delivery target and its own
  verification; delivery refusal in a continuous session ends the session rather
  than continuing to capture speech that cannot be delivered.

## Impact

- `src/murmly/audio.py` — incremental read access to the capture buffer without
  stopping the stream, and online silence detection over the captured audio.
- `src/murmly/stt.py` — a partial transcription entry point and serialized access
  to the single resident `WhisperModel`, which decodes one request at a time
  (`num_workers=1`).
- `src/murmly/daemon.py` — a live worker thread during `LISTENING`, the
  auto-transcribe trigger, segment delivery in continuous mode, and the session
  outcome reported by `toggle`.
- `src/murmly/overlay.py`, `src/murmly/overlay_renderer.py` — a partial-text
  message type with a length bound, and text rendering within the existing
  overlay dimensions.
- `src/murmly/config.py` — new `[stt]` keys with bounded validation.
- `src/murmly/cli.py` — `murmly doctor` reporting for the new options.
- Dependencies: none added. Silero VAD ships inside `faster-whisper`
  (`faster_whisper/assets/silero_vad_v6.onnx`) and `onnxruntime` is already
  installed as its transitive dependency.
