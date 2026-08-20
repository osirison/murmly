---
title: Add Speech Output Tasks
description: Track implementation and validation of local synthesis, the speech session transport, hotkey-triggered barge-in, and session-routed transcripts
---

## 1. Measurement before commitment

- [x] 1.1 Measure resident VRAM and host RSS with a transcription model and the synthesis model loaded at the same time, and record the figures in `design.md` under Risks; the 684 MiB synthesis figure was measured alone
- [x] 1.2 Confirm both runtimes initialise in one process against a single CUDA stack, following `docs/agent-notes/onnxruntime-gpu-cuda-version.md`, and confirm the session actually reports the GPU provider rather than silently falling back
- [x] 1.3 Confirm the phoneme library resolves without hardcoding a distribution path, following `docs/agent-notes/espeakng-loader-data-path.md`, and that a failure to resolve it is reported rather than producing silent empty audio

## 2. Test scaffolding for audio output

- [x] 2.1 Extend the fake sounddevice module (`tests/test_audio.py:69-78`) with `check_output_settings` and a raw output stream, leaving every existing input fake untouched
- [x] 2.2 Extend `FakeStream` (`tests/test_audio.py:21-35`) with a `write` that records what was written, so playback can be asserted without hardware
- [x] 2.3 Add a fake synthesizer returning known audio for known text, so daemon and session tests never load a model
- [x] 2.4 Confirm the full existing suite passes unchanged with the new fakes present

## 3. Synthesis and configuration

- [x] 3.1 Add `src/murmly/tts.py` with a synthesizer that yields `(audio_chunk, sample_rate)` per unit rather than one buffer, so a chunked one-pass model and a natively streaming model stay interchangeable
- [x] 3.2 Split input text into units no larger than a sentence, and reinsert the inter-sentence silence that independent production drops; derive the duration rather than hardcoding the measured 0.28 s, and pin it with a test asserting the chunked total matches the whole-passage total within a small tolerance
- [x] 3.3 Resolve the synthesis runtime independently of `resolve_runtime` (`stt.py:169`), which validates against CTranslate2 compute types (`config.py:58`)
- [x] 3.4 Report availability as an `available` flag plus `unavailable_reason` logged once, following `silence.py:56-62`, rather than raising
- [x] 3.5 Import the synthesis package lazily inside the function that needs it and convert `ModuleNotFoundError` into a `RuntimeError` naming the package and the remedy, following `audio.py:83-88`
- [x] 3.6 Register a `[tts]` table in `load_config` (`config.py:137-141`); a table absent from that list is silently ignored (`config.py:225`)
- [x] 3.7 Add voice, rate, output device, and enabled keys to `MurmlyConfig`, validated through `_bounded_int` / `_boolean` (`config.py:232-241`) so an out-of-range or unrecognized value falls back to a named default
- [x] 3.8 Add an annotated `[tts]` section to `config.example.toml`, stating the default for every key
- [x] 3.9 Add tests for an unrecognized voice, an out-of-range rate, and an absent runtime, asserting each falls back or reports rather than refusing to start

## 4. Audio output path

- [ ] 4.1 Add an output stream to `audio.py` using the same preflight-then-negotiate device selection as `_open_stream`, accumulating every failure and raising one combined `RuntimeError` only after all candidates are exhausted (`audio.py:104-140`)
- [ ] 4.2 Read the negotiated output rate back off the opened stream and expose it as a property, following `audio.py:134`; do not trust the configured value
- [ ] 4.3 Convert float32 synthesis output to the device format using the existing idiom at `stt.py:136-144`, and resample only when the device cannot take the synthesis rate directly
- [ ] 4.4 Keep the playback callback lock-free using the `deque.append` / `deque.popleft` pair with the lock on the producer side, mirroring `audio.py:94-101` and `audio.py:187-200`
- [ ] 4.5 Add an abort that stops audio already handed to the device and reports how much of it was played, since the reported position must be what was heard
- [ ] 4.6 Nest teardown so a failing stop still closes the stream, clearing the handle before the close, following `audio.py:141-156`
- [ ] 4.7 Add tests for device negotiation, underrun, abort mid-buffer, and a device that fails to open

## 5. Speech queue and playback lifecycle

- [ ] 5.1 Add a queue of named units with an explicit end-of-input marker, so "empty because the sender is still thinking" and "empty because the exchange is over" are distinguishable
- [ ] 5.2 Add a daemon state meaning output is active, alongside the existing three (`daemon.py:945-962`), and leave `OverlayState` (`overlay.py:43-46`) unchanged, since speech has no visual indicator in this change
- [ ] 5.3 Run playback on its own `daemon=True` thread named `murmly-speech`, wrapping `thread.start()` so a `RuntimeError` degrades the feature rather than failing the command, following `audio.py:255-266`
- [ ] 5.4 Implement cancellation as a `threading.Event` plus a bounded join plus a generation check that discards a late result, following `_live_stop` (`daemon.py:439-453`) and `stop_partials` (`stt.py:47`); never a forced kill
- [ ] 5.5 Track played position per unit and assert in tests that it never reports a unit that was only produced
- [ ] 5.6 Add a `CommandCode` member for speech that was interrupted (`daemon.py:56-70`); the spec requires distinct codes for distinct categories
- [ ] 5.7 Stop speech and close the output stream on shutdown before the drain expires (`SHUTDOWN_DRAIN_SECONDS`, `daemon.py:44`), alongside `_close_overlay` (`daemon.py:696`)

## 6. Speech sessions on the command socket

- [ ] 6.1 Add a session declaration to the request path, refused with a single response when speech output is disabled or unavailable, so a caller that cannot have a session is answered like any other unsupported request
- [ ] 6.2 Read many frames from a declared session rather than one, leaving `_read_request` (`daemon.py:901-918`) unchanged for every other connection
- [ ] 6.3 Write many frames to a declared session, leaving `_write_response` and `_claim_response` (`daemon.py:783-798`) governing one-shot connections exactly as they do today
- [ ] 6.4 Account for sessions outside the eight command worker slots (`MAX_COMMAND_WORKERS`, `daemon.py:39`) so open sessions cannot deny `status` or `toggle`
- [ ] 6.5 Bound the size of a single text frame with its own named constant; `MAX_COMMAND_BYTES` (`daemon.py:38`) stays 4096 for every existing command
- [ ] 6.6 Stop speech and discard that session's queue when its connection closes for any reason
- [ ] 6.7 Disconnect a session that will not accept its events rather than letting the write block playback, taking the same posture as `audio.py:296-298`
- [ ] 6.8 Add tests over a real socket asserting frame ordering, the end-of-input marker, refusal when disabled, a session that stops reading, and that `status` and `toggle` still answer with sessions open

## 7. Barge-in and capture gating

- [ ] 7.1 Stop playback and confirm the output stream is closed before `start_recording` opens the microphone (`daemon.py:315`)
- [ ] 7.2 Send the interruption event to the open session before capture begins, and before any transcript that follows it
- [ ] 7.3 Hold text that arrives while capture is running, and speak it once capture ends
- [ ] 7.4 Add a test asserting the recording produced by a barge-in contains none of the synthesized audio, driven through the output fake
- [ ] 7.5 Add a test asserting the interruption names the unit that was playing and every unit that never started

## 8. Transcript routing

- [ ] 8.1 Carry the pressed hotkey's purpose through the capture lifecycle so the destination is fixed when capture starts, not inferred when the transcript is ready
- [ ] 8.2 Skip target recording and verification for session-bound capture (`integrations.py`, and the target recorded at `daemon.py:331`), and deliver to the session instead
- [ ] 8.3 Leave the focused-window path byte-identical in behaviour whether or not a session is open
- [ ] 8.4 Report an undeliverable transcript when the session closed before it was produced, and do not fall back to pasting it
- [ ] 8.5 Add tests for both hotkeys with a session open, the session hotkey with no session open, and a session that closes mid-transcription

## 9. Second hotkey and installer

- [ ] 9.1 Extend the launcher and shortcut writing (`installer.py:176`) to bind two hotkeys, each verified independently
- [ ] 9.2 Add a second hotkey argument to `murmly install` (`cli.py:75-79`), and refuse an installation requesting the same key for both, naming the collision
- [ ] 9.3 Release every bound hotkey on uninstall, succeeding when only one is present
- [ ] 9.4 Add tests for both bound, one colliding with another application, the same key requested twice, and partial uninstall

## 10. Diagnostics and documentation

- [ ] 10.1 Nest the `doctor` report (`cli.py:351-370`) so a speech section can sit alongside the existing flat keys without changing the success shape pinned at `tests/test_cli.py:88`
- [ ] 10.2 Report speech enablement, availability, voice and rate in use with any unhonoured configured values, and the output device; name the remedy when unavailable
- [ ] 10.3 Guard the speech probe individually with `except Exception  # noqa: BLE001 - diagnostics must not raise` and report its failure in a dedicated detail field, following the existing probes
- [ ] 10.4 Report both hotkeys with their purposes and whether each is held by Murmly
- [ ] 10.5 Document speech output, the two hotkeys, and the session protocol in `README.md`
- [ ] 10.6 Move `docs/agent-notes/onnxruntime-gpu-cuda-version.md` and `docs/agent-notes/espeakng-loader-data-path.md` into the tree with this change; both are currently untracked

## 11. Validation

- [ ] 11.1 `openspec validate add-speech-output --strict` passes
- [ ] 11.2 `uv run --extra cuda python -m unittest discover -s tests` passes with no test skipped that was not skipped before
- [ ] 11.3 Confirm `status` and `toggle` request shapes, response shapes, and wording are unchanged, including the exact-dict assertions in `tests/test_daemon.py`
- [ ] 11.4 Confirm an installation with speech output disabled behaves identically to the previous release
