---
title: Add Live Transcription Tasks
description: Track implementation and validation of partial transcription during capture and silence-triggered auto-transcribe in stop and continuous modes
---

## 1. Configuration

- [x] 1.1 Add `live_transcribe` (bool, default false), `live_interval_ms`, and `live_window_seconds` to `MurmlyConfig` and the `[stt]` table in `load_config`, using `_bounded_int` for the two numeric keys
- [x] 1.2 Add `auto_transcribe` (`off` | `stop` | `continuous`, default `off`) with a `VALID_AUTO_TRANSCRIBE_MODES` set, falling back to `off` on an unrecognized value and recording the rejected value for diagnostics
- [x] 1.3 Add `auto_transcribe_silence_ms` (default 2000) and `auto_transcribe_min_speech_ms` with bounds, falling back to the defaults when out of range
- [x] 1.4 Extend `tests/test_config.py` with defaults, valid values, out-of-bounds fallback, and unrecognized-mode fallback

## 2. Incremental capture

- [x] 2.1 Change the `SoundDeviceRecorder` callback to append each block to a `collections.deque` and stop extending a shared `bytearray` directly, preserving the existing `status` raise and level-meter tap
- [x] 2.2 Add a consumer-owned accumulator with a `_drain()` that moves pending blocks out of the deque, and route `stop()` through it so it still returns the complete recording
- [x] 2.3 Add `snapshot(window_seconds)` returning a trailing copy of the audio captured so far without stopping the stream
- [x] 2.4 Add `take_segment()` returning everything accumulated since the last call and resetting the accumulator
- [x] 2.5 Add tests that drive the callback directly and assert that concatenated `take_segment()` results equal the full recording exactly, with no duplicated or dropped bytes across boundaries

## 3. Silence detection

- [x] 3.1 Add a silence detector that loads the VAD bundled with faster-whisper (`faster_whisper.vad.get_vad_model`) and reports whether the trailing audio contains speech and how long the current silence run is
- [x] 3.2 Convert PCM16 at the negotiated capture rate to the 16 kHz mono float32 frames the VAD requires, decimating when the rate is an integer multiple of 16 kHz
- [x] 3.3 Report silence detection as unavailable when the capture rate is not an integer multiple of 16 kHz, and when `onnxruntime` or the VAD asset cannot be loaded
- [x] 3.4 Track whether speech has been detected in the current recording or segment, so a silence run only qualifies after speech
- [x] 3.5 Add tests covering: silence run measured on synthetic audio, no trigger before speech, decimation from 48 kHz, and the unavailable path at an unsupported rate

## 4. Partial transcription

- [x] 4.1 Add a `threading.Lock` around `model.transcribe` in `FasterWhisperTranscriber` and a `stopping` event the live path checks before acquiring it
- [x] 4.2 Add a partial transcription entry point that transcribes a trailing window and returns text, sharing the resident model with the final pass
- [x] 4.3 Ensure the final transcription is never blocked behind a queued partial pass and that a partial result produced after `stopping` is set is discarded
- [x] 4.4 Add tests with a fake model asserting: partial results are dropped once stopping is set, no two passes run concurrently, and a partial failure does not propagate to the caller

## 5. Overlay partial text

- [x] 5.1 Add a `partial` message type to `OverlayState` handling in `encode_overlay_message`, keeping the `{"type", "value"}` shape with a string value
- [x] 5.2 Bound the partial text at encode time, keeping the tail, and reject or truncate before anything crosses the pipe
- [x] 5.3 Add `publish_partial` to `OverlayLifecycle`, `OverlayController`, and `NullOverlayController`
- [x] 5.4 Add an `overlay.text_size_px` config key with bounds, and pass it plus the live-transcription flag to the renderer process
- [x] 5.5 Render partial text in a separate transcript panel below the recording indicator, leaving the indicator's dimensions and position unchanged, including under reduced motion
- [x] 5.6 Size the transcript panel to its text, bounded to 75% of the selected monitor's width, truncating text that still does not fit
- [x] 5.7 Clear displayed partial text on every transition out of listening, including the error presentation
- [x] 5.8 Extend `tests/test_overlay.py` and `tests/test_overlay_renderer.py` with encode bounds, truncation, panel sizing, clearing on state change, and the null controller accepting partials

## 6. Live worker and auto-transcribe

- [x] 6.1 Add a live worker thread started when capture starts and stopped when capture stops, ticking on `live_interval_ms`, running silence detection every tick and partial transcription only when `live_transcribe` is enabled
- [x] 6.2 Skip a tick when the previous partial pass is still running instead of queuing it
- [x] 6.3 Stop producing partials for the remainder of the session after a partial pass raises, without interrupting capture
- [x] 6.4 Join the worker with a bounded timeout when capture stops and proceed with final transcription regardless of whether it exited
- [x] 6.5 Implement `stop` mode: on a qualifying silence run, acquire the daemon lock, confirm the state is still `LISTENING`, then run the existing stop-capture, record-target, transcribe, deliver, return-to-idle path
- [x] 6.6 Implement `continuous` mode: close a segment, record its delivery target, transcribe and deliver it while capture continues, and reset speech tracking for the next segment
- [x] 6.7 End a continuous session on a refused segment delivery, stopping capture and returning to idle with the transcript on the clipboard
- [x] 6.8 Transcribe and deliver the trailing audio as a final segment when a toggle ends a continuous session, and deliver nothing when no speech was captured since the last segment
- [x] 6.9 Serialize the whole produce-and-deliver of one unit of audio across the live worker and the toggle path, so a segment's paste never begins while a previous clipboard restoration is pending
- [x] 6.10 Report the session outcome on the toggle that ends a multi-segment session: combined transcript text in capture order and delivered only when every segment was delivered, leaving single-transcript responses unchanged

## 7. Diagnostics and documentation

- [x] 7.1 Report live transcription, auto-transcribe mode, effective silence duration, any rejected mode value, and silence-detection availability in `murmly doctor`
- [x] 7.2 Measure partial pass latency per model profile on this hardware and report the measured figure through `doctor` rather than a fixed estimate
- [x] 7.3 Document the new `[stt]` keys in the README configuration section, including that an auto-stopped recording delivers without `murmly toggle` printing the transcript
- [x] 7.4 Note the partial-text privacy surface in the README scope and limitations section
- [x] 7.5 Record a field note if VAD loading, decimation, or the overlay text path turns up an undocumented precondition

## 8. Verification

- [x] 8.1 Add a daemon test asserting that a session with `live_transcribe` enabled delivers the same transcript as one with it disabled, over identical audio
- [x] 8.2 Add daemon tests for `stop` mode: auto-stop after silence, a toggle arriving first taking precedence, and a toggle during auto-stopped processing reporting busy
- [x] 8.3 Add daemon tests for `continuous` mode: multiple segments delivered, per-segment target verification, refusal ending the session, and the final segment on toggle
- [x] 8.4 Confirm the public `IDLE`/`LISTENING`/`THINKING` state names and every existing toggle response field are unchanged
- [x] 8.5 Run `uv run --extra cuda python -m unittest discover -s tests` and confirm the suite passes
- [x] 8.6 Exercise both modes end to end in a live desktop session on the balanced profile, confirming partial text appears, silence triggers each mode, and delivery behaves as specified
