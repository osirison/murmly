---
title: Add Speaker Recognition Tasks
description: Track verification, implementation and validation of voice enrolment, mine-only filtering, label-everyone labelling, voiceprint privacy and speaker-model residency
---

## 1. Verification before any code

- [x] 1.1 Using the matrix in `.github/workflows/tests.yml` (Python 3.12 and 3.14 on `ubuntu-latest`, `windows-latest`, `macos-14`), confirm on PyPI that the latest `kaldi-native-fbank` has a wheel for cp312 and cp314, or an abi3 wheel covering both, on manylinux x86_64, win_amd64 and macosx arm64. Inspect the win_amd64 wheel per `docs/agent-notes/inspecting-a-foreign-platform-wheel.md`. Done when every one of the six cells is listed with its exact wheel filename, or marked missing.
- [x] 1.2 Confirm that `numpy` and `onnxruntime` can be declared as direct dependencies with lower bounds the current `uv.lock` already meets, and that the locked `onnxruntime` has CPU wheels for cp312 and cp314 on all three platforms. Done when each wheel is recorded by filename from `uv.lock`.
- [x] 1.3 For CAM++ (3D-Speaker, VoxCeleb) and WeSpeaker ResNet34 (VoxCeleb), read the licence of the published ONNX file and the terms of its training data, and decide whether end users may download it and run it in Murmly. Done when one model is chosen with links to both texts, or the change is stopped here because neither qualifies.
- [x] 1.4 Pick the download source: the publisher's own release, pinned to an exact file URL. Done when the URL, file size and SHA-256 are recorded, and the ONNX inputs and outputs (80-dim filterbank at 16 kHz, embedding dimension) are recorded from the model's metadata.
- [x] 1.5 Choose feature extraction: `kaldi-native-fbank` if 1.1 found every cell, otherwise the numpy fallback. Done when the choice and its reason are written down.
- [x] 1.6 Confirm from faster-whisper's source (`restore_speech_timestamps`) and from one real recording with a known pause that `segment.start` and `segment.end` are in original-audio seconds when `vad_filter=True`. Done when the slice cut at a segment's reported times plays back as that segment's words.
- [ ] 1.7 On Windows, run `icacls` on `%LOCALAPPDATA%\murmly`. Done when the output shows no access for other standard accounts, or a decision to set an explicit ACL on the voiceprint file is recorded.
- [ ] 1.8 Write the results of 1.1–1.7 into `design.md`, under Decisions and Risks, before group 2 starts. Done when no "verified in task group 1" statement in the design is left open.
- [ ] 1.9 Download the model from the pinned URL on Linux and on Windows. Done when both SHA-256 values equal the value recorded in 1.4.

## 2. Dependencies and model fetch

- [x] 2.1 Declare `numpy` and `onnxruntime` in `pyproject.toml`. Add `kaldi-native-fbank` only if 1.5 chose it, with a platform marker if 1.1 found a missing cell. Run `uv lock`. Done when the `uv.lock` diff adds no new version of any package already locked.
- [x] 2.2 Add the speaker model to `setup.sh`: fetch it into the data directory with `curl`, verify its SHA-256, delete the file and warn on a mismatch, and skip the fetch when a verified file is already present. Done when a run with a corrupted local file replaces it, and a second run does not download again.
- [x] 2.3 Add the same fetch and `Get-FileHash` verification to `bootstrap.ps1`. Done when it is checked per `docs/agent-notes/bootstrap-ps1-local-testing.md`.
- [x] 2.4 On a machine with the GPU swap, re-apply `onnxruntime-gpu` after the sync that 2.1 causes, per `docs/agent-notes/onnxruntime-gpu-cuda-version.md`. Done when the transcription session's `get_providers()` reports the same providers as before the change.
- [x] 2.5 Add the chosen model's licence text to `licenses/`. Done when the file names the model, its source URL and its licence.
- [x] 2.6 Create `src/murmly/speakers.py` holding only the model URL, filename and SHA-256 recorded in 1.4, and add a test asserting that `setup.sh`, `bootstrap.ps1` and those constants carry the same three values. Done when `uv run --no-sync python -m unittest discover -s tests` passes.

## 3. Configuration

- [x] 3.1 Add `VALID_SPEAKER_MODES = {"off", "mine-only", "label-everyone"}` and a `[speakers]` table to `load_config`. `mode` defaults to `off` and records `speaker_mode_rejected_value` on an unrecognised value. `owner` is a string, empty for none. `match_threshold` is an integer percent read through `_bounded_int` with bounds 30–90, with `_rejected_value`. Done when the matching `MurmlyConfig` fields exist and a bad value never raises.
- [x] 3.2 Add `default_data_dir()` returning the path `default_tts_model_dir()` returns today, and make `default_tts_model_dir()` delegate to it. Done when every platform's resolved path is unchanged in the existing config tests.
- [x] 3.3 Document the `[speakers]` keys, their bounds, defaults and fallbacks in `config.example.toml` and in `manual/settings.md`, following the existing `{ #anchor }` heading style. Done when the manual builds without warnings as `docs/agent-notes/building-the-manual.md` describes (`mkdocs.yml` sets `strict: true`).
- [x] 3.4 Extend `tests/test_config.py` to cover:
  - defaults, and each valid mode;
  - an unrecognised mode falling back with its rejected value;
  - a threshold in bounds and out of bounds;
  - a float threshold such as `0.55` rejected and reported;
  - an absent owner;
  - an unchanged data directory on each platform.

  Done when the suite passes with `--no-sync`.

## 4. Speaker attribution core

- [x] 4.1 Extend `src/murmly/speakers.py` with filterbank features chosen in 1.5 and utterance mean normalisation. On the numpy path, commit a small reference fixture produced once with the reference implementation. Done when the computed features match the fixture within a stated tolerance.
- [x] 4.2 Add an `Embedder` protocol and `SpeakerEmbedder`:
  - an ONNX session with `providers=["CPUExecutionProvider"]` and `enable_cpu_mem_arena = False`;
  - a load lock and a use lock;
  - a `resident` property that takes no lock;
  - `release()`, which drops the session and calls `return_free_heap()`;
  - the provider read back through `get_providers()`.

  Done when a unit test with a stub session shows that `release()` waits for an inference in progress.
- [x] 4.3 Add attribution over timed parts:
  - L2-normalised embeddings;
  - the best enrolled match at or above the threshold;
  - online clustering of unknown voices with running centroids, held in a per-session state object;
  - unknown voices numbered by first appearance.

  Done when the fake-embedder tests in 4.8 pass.
- [x] 4.4 Apply the short-part rule. A part below the minimum duration takes the speaker of the nearest identifiable part, preferring the one before it. When no part is identifiable, return a "no identifiable part" result so the caller delivers as with `off`. Done when all three short-part scenarios in the spec pass as tests.
- [x] 4.5 Add formatting:
  - Mine-only joins the owner's parts with single spaces and adds no labels.
  - Label-everyone merges consecutive parts from the same speaker and writes `Label: words`.
  - Label-everyone separates parts with one space and always begins with a label.
  - Neither mode ever emits a line break.

  Done when tests assert the spec's example string exactly and assert that no output contains a line break.
- [x] 4.6 Add name validation and case-insensitive name comparison. Refuse a name that is:
  - empty, or over 32 characters;
  - contains a line break, a control character, or a colon;
  - equal to "You" or "Speaker N" in any letter case.

  Done when each rejected form has a test.
- [x] 4.7 Measure on real recordings:
  - the owner alone;
  - the owner with another person;
  - the owner with a television;
  - the owner on a second microphone.

  Set the default threshold and the minimum part duration against a false-accept and false-reject target stated before measuring. Done when the figures, the target, and whether each was met are recorded in `design.md`, and the defaults in code match.
- [x] 4.8 Add `tests/test_speakers.py`, using a fake embedder that returns chosen vectors. Cover:
  - the threshold boundary;
  - the owner against another enrolled voice;
  - unknown numbering, stable across calls in one session and reset for a new session;
  - mine-only dropping everything;
  - label-everyone with no owner;
  - the cases from 4.4–4.6.

  Done when the suite passes with `--no-sync`.

## 5. Voiceprint store

- [x] 5.1 Add the store at `<data dir>/voiceprints.json`, with `version`, `model_sha256`, `dimension` and `voices[{name, embedding}]`. Write each update to a temporary file in the same directory and move it into place with `os.replace`. Done when an interrupted write in a test leaves the previous file intact.
- [x] 5.2 Create the file with mode 0600, and any directory Murmly creates with mode 0700, on POSIX. Apply the Windows decision from 1.7. Done when a POSIX test reads the modes back. The test skips itself elsewhere.
- [x] 5.3 Ignore voiceprints whose `model_sha256` differs from the installed model, and expose that state for diagnostics. Done when a test with a mismatched hash yields no voices and the flag set.
- [x] 5.4 Add change detection by `(st_mtime_ns, st_size)`, and treat a missing file as no voices. Done when tests cover an unchanged file, a changed file and a deleted file.
- [x] 5.5 Add remove-one and remove-all. Removing the last voice, or all voices, deletes the file. Done when tests show the file is gone and that a remaining voice is byte-identical after removing another.
- [x] 5.6 Add a test that the stored JSON holds only the documented keys, with no audio and no timestamps. Done when the suite passes with `--no-sync`.

## 6. Enrolment and voice commands

- [x] 6.1 Add `murmly enrol <name> [--seconds N]` to `build_parser` and `_dispatch`. It validates the name, checks the model file, then asks the daemon for `status`. It proceeds when no daemon answers or the daemon reports `IDLE`, and refuses `LISTENING`, `THINKING` and `SPEAKING`. Done when 6.8 covers each branch.
- [x] 6.2 Record in the CLI process as `_run_spike` does, print a passage to read aloud, and keep the audio in memory only. Done when a test with a fake recorder shows no file other than the voiceprint store is created.
- [x] 6.3 Measure the sample's speech with the Silero VAD that faster-whisper ships. Below the minimum, refuse with "too little speech" and store nothing. Otherwise, embed the speech in windows, average, L2-normalise and store. Done when tests cover a silent sample, a short sample and an accepted sample.
- [x] 6.4 Report the stored path, how to remove the voice, and whether an existing voiceprint was replaced. When no owner is configured, also print the `[speakers] owner` line to add. Done when tests assert each message.
- [x] 6.5 Add three commands, none of which prints a vector:
  - `murmly speakers list`, which prints names and marks the owner;
  - `murmly speakers remove <name>`;
  - `murmly speakers remove --all`.

  A name that is not enrolled exits non-zero. Done when tests cover list, remove, remove-all and an unknown name.
- [x] 6.6 Check the enrolment process's exit against `docs/agent-notes/portaudio-jack-exit-abort.md`, and apply the exit handling it calls for, if any. Done when a real enrolment on PipeWire leaves no core dump in `coredumpctl`.
- [x] 6.7 Measure, on real voices, the enrolment length and the minimum speech needed to reach the threshold from 4.7. Done when the measured values are recorded in `design.md` and set as the defaults.
- [x] 6.8 Add `tests/test_cli_speakers.py` with a fake recorder, a fake VAD and a fake embedder. Cover:
  - a busy daemon refused, and no daemon proceeding;
  - a silent sample refused;
  - a missing model refused before recording;
  - bad names refused;
  - a replacement reported;
  - nothing stored on any refusal.

  Done when the suite passes with `--no-sync`.

## 7. Daemon integration

- [x] 7.1 Add `transcribe_segments_pcm16` to `FasterWhisperTranscriber`, returning timed parts from the same decode. Done when a fake-model test shows `transcribe_pcm16` output is byte-identical before and after the change.
- [x] 7.2 Add one `SpeechSession` helper, used by `process_recording` and `process_for_session` before their empty-text check. With `off` it calls `transcribe_pcm16` unchanged. Otherwise it slices each part's PCM, converts it to 16 kHz mono with `resample_float32`, attributes and formats. Done when 7.6 passes.
- [x] 7.3 Catch any attribution error inside the helper, log it without text, and return the plain joined text. Done when a test with a raising embedder shows the transcript delivered and a continuous session still listening.
- [x] 7.4 In `start_recording`, when a speaker mode is set:
  - reset the unknown-voice state;
  - reload voiceprints if they changed;
  - decide whether the mode can run (an owner for mine-only, the model present);
  - log one warning naming the reason when it cannot;
  - start a background build of the embedder, without delaying capture.

  Done when a test with a slow fake build shows capture starting immediately.
- [x] 7.5 Measure the added time between capture stop and delivery, per minute of speech, on this machine's CPU. Done when the figure is recorded in `design.md`. If it is above 500 ms per minute, revisit the approach before continuing.
- [x] 7.6 Add daemon tests with a fake transcriber that returns timed parts and a fake embedder. Cover:
  - `off` byte-identical, with no store read and no embedder built;
  - mine-only through toggle, stop mode and continuous mode;
  - a dropped continuous segment: the session keeps listening, the segment is not counted, and no failure is signalled;
  - label-everyone numbering across segments;
  - the toggle response text equal to the labelled delivery;
  - a session-bound reply filtered;
  - the disabled-mode warning carrying no text;
  - overlay messages unchanged.

  Done when the suite passes with `--no-sync`.
- [x] 7.7 Split timed parts at word gaps of 1.5 s or more, using word timestamps in speaker modes only. Done when a fake-model test splits a two-sentence segment at a long word gap and leaves mode off's decode unchanged, and ~/murmly-pause.wav yields two parts through the real code path.
- [x] 7.8 In label-everyone, decode with `vad_filter=False` and `condition_on_previous_text=False` and drop every part with less than 0.15 s of Silero speech before attribution; mine-only keeps the configured filter. Done when tests cover the decode flags by mode (off and mine-only unchanged), the check dropping a near-silent part and keeping a quiet one, and a recording of only invented text delivering nothing, and `round-d.wav` through the real code path labels "Yes, go ahead." as Sam while the noise recordings deliver nothing. The measured threshold is recorded in `design.md`.

## 8. Residency and status

- [x] 8.1 Release the embedder from the transcription `IdleRelease` callback, so it follows `stt.unload_after_idle_s`, including 0, and never releases while the use lock is held. Done when 8.3 passes.
- [x] 8.2 Add `speaker_model_resident`, and `speaker_model_resident_detail` on error, to the daemon's `_residency`, read without loading or locking. Done when a test shows `status` neither builds the embedder nor waits on its locks.
- [x] 8.3 Add tests covering:
  - release together with the transcription model, and no release when the period is 0;
  - no release during attribution;
  - a rebuild at the next capture;
  - no build at daemon start;
  - the CPU provider only.

  Done when the suite passes with `--no-sync`.

## 9. Diagnostics and documentation

- [x] 9.1 Add a `speaker_recognition` section to `murmly doctor`, with every key from the design always present. Check the model's presence and checksum without building a session, and take residency from the daemon answer that `daemon_residency` already obtains. Done when `murmly doctor` prints the section with the mode `off` and with each speaker mode.
- [x] 9.2 Add diagnostics tests covering:
  - the same keys in every mode and for every platform profile;
  - no vector values anywhere in the output;
  - unknown residency when no daemon answers;
  - the rejected mode and threshold;
  - an owner who is not enrolled;
  - a model mismatch reported as needing re-enrolment;
  - a missing model named by path.

  Done when the suite passes with `--no-sync`.
- [x] 9.3 Add a manual page titled by what the reader wants to do, for example "Typing only your own voice". It covers enrolment, the three modes, the one-line label format, and the limits:
  - two speakers inside one segment;
  - overlapping speech;
  - unfiltered partial text;
  - failing open, and how to confirm with `doctor`;
  - no access control.

  Register the page in the `mkdocs.yml` nav. Done when the manual builds as `docs/agent-notes/building-the-manual.md` describes and the page appears in the nav.
- [x] 9.4 Update `manual/where-your-words-go.md`. Say that a voiceprint is stored and never the audio, where it is stored, that only the user can read it, and that it stays on the machine. Cover the list and remove commands, that uninstalling leaves voiceprints in place, and asking before enrolling someone else. Done when the manual builds as `docs/agent-notes/building-the-manual.md` describes.
- [x] 9.5 Record a field note under `docs/agent-notes/` if wheel inspection, the model fetch, the GPU-swap resync, or the enrolment exit turned up an undocumented precondition. Done when each such precondition has a note, or none was found.
- [ ] 9.6 Run `openspec validate --all --strict` and `uv run --no-sync python -m unittest discover -s tests`, then use mine-only and label-everyone end to end in a live desktop session. Use toggle and continuous modes, a second enrolled voice, and a television. Done when both commands pass and the live session behaves as the spec's scenarios state.
