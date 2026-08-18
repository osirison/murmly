---
title: Add Live Transcription Design
description: Technical approach for partial transcription during capture and silence-triggered auto-transcribe without degrading the delivered transcript
---

## Context

See proposal.md — Why. The constraints that shape the approach:

- **faster-whisper has no streaming API.** `WhisperModel.transcribe()` accepts a
  complete buffer (`str | BinaryIO | np.ndarray`) and returns a segment iterator.
  There is no partial-result callback and no incremental decode. Anything "live"
  has to be built from repeated inference.
- **One model, one decode at a time.** `WhisperModel` is constructed with
  `num_workers=1`, which maps to CTranslate2's `inter_threads`. Concurrent
  `transcribe()` calls from two Python threads serialize inside the engine.
- **Capture is already incremental, but not readable.** The PortAudio callback in
  `SoundDeviceRecorder` appends every block to a private `bytearray`, and the
  overlay level meter already taps the newest block at 30 Hz. Nothing exposes the
  accumulated audio without stopping the stream.
- **The capture rate is negotiated, not fixed.** `_candidate_sample_rates` falls
  back to the device's native rate when 16 kHz is refused, so a session can be
  running at 44.1 or 48 kHz. Today this is invisible because the temp WAV carries
  its own rate and faster-whisper resamples on decode.
- **The overlay protocol carries no text.** `encode_overlay_message` accepts only
  `{"type", "value"}` where `value` is an `OverlayState` or a level.
- **The daemon is a three-state machine** (`IDLE`/`LISTENING`/`THINKING`) guarded
  by one lock, and `status` exposes those names as a CLI contract.

## Goals / Non-Goals

**Goals:**

- Partial results that are provably incapable of corrupting a delivered
  transcript.
- Silence detection accurate enough to end a recording on, on ordinary desktop
  microphones in ordinary rooms.
- No change to the public daemon state names or to any existing response field.
- No new package dependency.

**Non-Goals:**

- Assembling the delivered transcript from partial results (LocalAgreement-style
  prefix commitment). This is the natural follow-up once partial quality is
  measured, but it makes partials load-bearing, which this change's specs forbid.
- Typing partial text into the focused application.
- Speaker diarization, punctuation restoration, or any post-processing.
- Replacing the transcription engine.

## Decisions

### Pseudo-streaming by repeated re-transcription

Each live tick re-transcribes a bounded trailing window of the audio captured so
far and replaces the displayed partial wholesale. The transcript that is actually
delivered comes from a separate, final pass over the complete recording — the
same call today's code makes.

This is what makes the central spec guarantee cheap to honor: the delivered
transcript is byte-identical to a non-live session because it is produced by
identical code over identical audio. Partial results are a read-only side
channel.

*Alternatives considered.* **LocalAgreement-2** (commit the prefix two
consecutive passes agree on, deliver the accumulated commits) gives lower latency
and avoids re-work, but the delivered text then depends on window boundaries and
agreement heuristics — a regression risk against a delivery path that is
currently exact. **A streaming engine** (whisper.cpp streaming, or a
natively-streaming ASR model) would be a better fit for the problem and a worse
fit for this codebase: it discards the CUDA runtime trust checks, the pinned
model revision, and the warm-model behavior in `stt.py`.

### Bounded window, skipped ticks

The live pass transcribes at most the last `live_window_seconds` (default 15) of
audio, not the whole recording. Unbounded re-transcription costs time
proportional to utterance length on every tick, so a three-minute dictation would
re-transcribe three minutes each second.

When a pass is still running as the next tick elapses, the tick is skipped rather
than queued. A queue would let the worker fall arbitrarily far behind and display
text describing audio from ten seconds ago.

The consequence to accept: on a slow device/profile combination the partial
display updates rarely. It degrades to "less feedback", never to "delayed
delivery".

### Explicit block handoff out of the audio callback

The PortAudio callback stops owning accumulation. It appends each block to a
`collections.deque` and does nothing else; a consumer drains with `popleft` into
its own accumulator. `stop()` and segment closure both drain before returning.

Reading a `bytearray` while the callback extends it is serialized by the GIL on
today's interpreter (this venv is CPython 3.14 with `Py_GIL_DISABLED=0`), so a
naive concurrent read would probably work. Relying on that is a bad trade: a
free-threaded interpreter removes the guarantee silently, and the failure mode is
a torn audio buffer. `deque.append`/`popleft` are the documented thread-safe
primitive and cost nothing.

A lock around the existing buffer was rejected outright: it would be held across
a multi-megabyte copy inside the real-time audio callback, and the callback
already treats any PortAudio `status` as fatal.

### Silence detection with the VAD that is already installed

faster-whisper ships `assets/silero_vad_v6.onnx` and pulls in `onnxruntime`
(1.28.0 present). Silence detection runs that model over the trailing audio on
the same tick cadence as the live pass, independent of whether live transcription
is enabled.

*Alternative considered.* An RMS threshold over the existing `pcm16_rms` /
`rms_to_level` helpers is free and already written, but a fixed amplitude
threshold is exactly the thing that fails in a noisy room or on a quiet gain
setting — and it would end recordings, which is a destructive action to get
wrong. RMS is retained as the documented fallback only where Silero cannot run.

Silero requires 16 kHz mono float32 in multiples of 512 samples. When the
negotiated capture rate is an integer multiple of 16 kHz (48000 → 3, 32000 → 2),
decimate. Otherwise report silence detection unavailable and disable
auto-transcribe for the session, per the spec — rather than resampling badly and
ending recordings on a corrupted signal.

### Serialized model access, with the final pass privileged

A `threading.Lock` in `FasterWhisperTranscriber` guards `model.transcribe`. The
lock is not for correctness inside CTranslate2 — `num_workers=1` already
serializes — but to make the ordering explicit and to let the live worker check a
`stopping` event before it acquires. Once capture stops, the worker starts no new
pass; the daemon joins it with a bounded timeout and proceeds regardless. A pass
already inside the engine simply finishes and has its result dropped.

Raising `num_workers` to 2 was rejected: it buys real parallelism at the cost of
contending for the same GPU with the one pass whose latency the user actually
feels.

### Public states unchanged; segment work happens inside LISTENING

`IDLE`/`LISTENING`/`THINKING` stay exactly as they are, because `status` returns
them over the socket. In continuous mode a segment is transcribed and delivered
while the public state remains `LISTENING`, since the microphone is still open —
which is also why the overlay keeps the listening presentation for segments
(recording-overlay delta).

The auto-transcribe trigger fires from the live worker thread and must acquire
the daemon's existing lock to transition. Ordering rule: the trigger acquires the
lock, re-reads the state, and does nothing unless it is still `LISTENING` — a
toggle that arrived first always wins. `handle_command` already performs
`process_recording` outside the lock, so delivery does not serialize against the
trigger.

In `stop` mode the transcript is delivered with no client waiting on a socket:
the toggle that started the recording already returned its `LISTENING`
acknowledgement. Delivery is a side effect, and `murmly toggle` will not print
the text for an auto-stopped recording. This is a real observable difference and
belongs in the README.

### Overlay gains a two-key text message

The wire format keeps its `{"type", "value"}` shape; a `partial` type carries a
string `value`. Uniformity matters more than expressiveness here — the renderer's
parser and the encoder's validation both stay single-shaped.

The encoder bounds the text and keeps the **tail**, because the newest speech is
what the user is checking. Truncation happens at encode time, not in the
renderer, so no oversized payload ever crosses the pipe.

### Transcript text renders in a second surface, not in the recording strip

The recording indicator is 156×48 px with the microphone glyph at x≈16–34 and the
waveform bars at x≈53–137 — 19 px of free width. No useful amount of text fits,
and widening it would move a presentation that three shipped requirements pin in
place (dimensions, position, and no resizing).

So the strip is left untouched and partial text renders in a **separate
transcript panel** below it. Only the new surface is content-sized: it grows with
the text up to a bounded fraction of the display (75% of the selected monitor's
width) and is centered on the same monitor. Text size is configurable, and text
that still does not fit at maximum width is truncated.

*Alternatives considered and rejected.* Widening the strip to ~520 px when live
transcription is on keeps one surface but changes the dimensions of an existing
presentation. Growing it to two rows has the same problem in the other axis.
Replacing the waveform with ~16 characters of text preserves the geometry but
sacrifices the live audio-level feedback that the overlay spec requires.

The panel occupies the space between the strip's bottom edge and the display
edge, so the strip's own position is arithmetically unchanged. Its height follows
the configured text size and is clamped to the configured bottom margin, which
means a user who wants noticeably larger text also raises
`overlay.bottom_margin_px` to make room.

### Segment delivery runs on one queue

Continuous mode delivers through a single-threaded queue, so a segment's paste
cannot begin while the previous segment's clipboard restoration is still pending.
`ClipboardPaster` restores after a delay of up to 5000 ms; two overlapping
deliveries would let a restore overwrite a transcript that has not been pasted
yet. One queue makes the spec's serialization requirement structural instead of
timing-dependent.

## Risks / Trade-offs

- **Partial passes may not keep pace on CPU.** `large-v3-turbo` at `int8` on CPU
  is the likely failure case, and `device = "auto"` lands there silently on
  machines without the CUDA extra. → Skipped ticks degrade gracefully; measure
  real pass latency per profile during implementation and have `doctor` report
  the measured figure rather than a guess.
- **Ending a recording is destructive and silence detection is a heuristic.** A
  false trigger cuts the user off mid-thought. → `auto_transcribe` defaults to
  `off`; a run of silence only counts after speech was detected in the current
  segment; the threshold is configurable with bounds.
- **Auto-stop records the delivery target on a moment the user did not choose.**
  A manual toggle is a deliberate act, so the window focused at that instant is
  the window the user meant. In `stop` mode the target is recorded when the
  silence run completes — a user who alt-tabs during their own two-second pause
  has the *new* window recorded as the target, and verification then passes on a
  paste they never aimed. → `auto_transcribe` defaults to `off` and the threshold
  is configurable; a shorter threshold narrows the window in which focus can move
  unnoticed.
- **A dead microphone produces exact silence.** The SOF digital-microphone case
  in `docs/agent-notes/murmly-spike-sof-dmic.md` yields all-zero samples. →
  Requiring detected speech before any trigger means a muted mic keeps the
  session in listening rather than auto-stopping into an empty transcript.
- **Continuous mode multiplies the focus-verification surface.** Every segment is
  another chance for focus to have moved. → A refused segment ends the session
  (transcript-delivery delta), so a user who alt-tabs gets one refusal, not a
  stream of them.
- **Partial text on screen is a new privacy surface.** The overlay is visible
  during screen sharing. → Off by default; discarded on every transition out of
  listening; never logged.
- **Two accumulators for one recording.** The consumer-owned accumulator must be
  the single source for both `stop()` and segment closure, or a segment boundary
  could duplicate or drop audio. → Cover with a unit test that drives the
  callback directly and asserts that concatenated segments equal the full
  recording exactly.

## Migration Plan

No migration. Both features are opt-in and default to their current behavior, so
an existing `config.toml` produces an identical session. Rollback is setting
`live_transcribe = false` and `auto_transcribe = "off"`, or reverting the change;
neither leaves persistent state behind.

## Open Questions

- The default `live_interval_ms` and `live_window_seconds` are placeholders
  (1000 ms / 15 s) pending measured pass latency per profile on this hardware.
  Tuning them changes neither the specs nor the task breakdown.
