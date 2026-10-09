---
title: Add Speaker Recognition Design
description: Technical approach for per-segment speaker attribution with a small CPU embedding model, voiceprints enrolled by name, and filtering or labelling applied at the one place every transcript passes before delivery
---

## Context

See proposal.md — Why. The current code shapes the approach in these ways. Each
point was confirmed by reading the code unless it says otherwise.

- **Whisper already returns timed segments, and Murmly throws the times away.**
  `FasterWhisperTranscriber._decode` calls `model.transcribe(...)` and returns
  `" ".join(segment.text.strip() for segment in segments).strip()`. Each segment
  carries `start` and `end` that nothing reads. The final pass writes a temporary
  WAV at the capture rate, which may be 44.1 or 48 kHz, and faster-whisper
  resamples it. With `vad_filter=True` the times are in original-audio seconds;
  this was checked in faster-whisper's source (task 1.6, source half). See
  "Segment times".
- **Every delivered transcript passes through two methods.**
  `SpeechSession.process_recording` (window-bound) and
  `SpeechSession.process_for_session` (speech-session-bound) both call
  `transcribe_pcm16` and then `if not text: return ProcessingResult(..., "DONE")`.
  `MurmlyDaemon._process_recording` picks between them by capture destination.
  Toggle (`_finish_toggle`), stop mode (`_finish_auto_stop`) and continuous mode
  (`_run_segment`) all call `_process_recording`.
- **An empty transcript already has the behaviour mine-only needs.** In
  `_run_segment`, `if not result.text: return` comes before the segment is
  counted. `_session_response` drops empty texts and keeps a session "delivered"
  when the final segment produced nothing. An exception is different: it ends a
  continuous session (`_run_segment`) or turns a toggle into `COMMAND_FAILED`, and
  the text is lost.
- **Murmly has never pasted a line break.** `_decode` joins Whisper segments with
  one space, and `_session_response` joins continuous segments with one space.
  Every injector puts the text on the clipboard and sends a paste keystroke:
  `wtype`/`xdotool`/`ydotool` sending Ctrl+V on Linux, `SendInput` on Windows,
  and the macOS equivalent. A pasted line break reaches the target as a line
  break. A terminal program that does not use bracketed paste runs each line as
  it arrives, which submits a command early. Speech-session replies are sent as
  one JSON `text` field to an agent.
- **Precedent for a feature that cannot run.** When silence detection cannot run,
  `SpeechSession._create_silence_detector` logs one warning at capture start
  (`"Auto-transcribe disabled for this session: %s"`). Separately,
  `live_transcription_diagnostics` probes the same condition and reports
  `silence_detection_available` with a detail string. Nothing reaches the
  overlay, the toggle response, or the pasted text. The user learns only from the
  daemon log or from `murmly doctor`.
- **Configuration never refuses to start.** `load_config` falls back to defaults.
  Enumerations keep a `*_rejected_value` for `doctor`. `_bounded_int` and
  `_rejected_value` are integer-based: `int(0.55)` is `0`.
- **Residency has a pattern for an ONNX session.** The synthesis session is an
  `onnxruntime.InferenceSession` with `enable_cpu_mem_arena = False`. It is
  released by dropping it and calling `return_free_heap()` (`malloc_trim` on
  glibc), and the provider is read back with `session.get_providers()`.
  `IdleRelease` timers drive release. The transcription countdown is armed when
  capture ends and cancelled when it begins. `status` answers `model_resident`
  and `synthesis_resident` without loading or locking.
- **Dependencies are tight.** `numpy` (2.5.2) and `onnxruntime` (1.28.0) are in
  `uv.lock` only as transitive dependencies. GPU machines swap in
  `onnxruntime-gpu` 1.24.4 by hand, and any `uv sync` reinstalls the CPU build
  over it (`docs/agent-notes/onnxruntime-gpu-cuda-version.md`). The CI matrix is
  Python 3.12 and 3.14 on `ubuntu-latest`, `windows-latest` and `macos-14`, each
  running `uv sync --locked`. A new dependency therefore needs wheels for cp312
  and cp314 on manylinux x86_64, win_amd64 and macOS arm64, or a platform marker.
- **Data location and model fetch.** `default_tts_model_dir()` is in practice the
  general data directory: `$XDG_DATA_HOME/murmly`, `%LOCALAPPDATA%\murmly`, or
  `~/Library/Application Support/murmly`. `setup.sh` fetches the Kokoro files
  there with `curl` from a GitHub release, without a checksum. `bootstrap.ps1`
  fetches nothing today: it installs `uv` if needed and runs
  `uv run --project <repo> murmly`, and the manual tells Windows users to place
  the Kokoro files by hand. The speaker model will be the first download that
  script makes. `setup.sh` also refuses macOS ("Murmly does not support macOS.
  It supports Linux and Windows."), so the macOS path above is never reached
  through it.
- **A command that records in its own process exists.** `murmly spike` builds a
  `SoundDeviceRecorder` in the CLI process and calls `record_for_seconds`.
- **Synthetic audio cannot test speaker code.** Speaker embeddings of tones or
  noise mean nothing (`docs/agent-notes/faster-whisper-benchmark-vad-filter.md`
  makes the same point for Whisper). CI has no real voices.

No baseline requirement becomes false as written, so the change has no MODIFIED
blocks:

- The model-residency requirements on independent release and on residency
  diagnostics name only the transcription model and the synthesis session.
- "A model that is in use is never released" and "Reporting residency never
  loads a model" are written for any model.
- command-interface's "Diagnostics report every section they can determine"
  already covers sections from other capabilities.
- speech-output's "A transcript from the focused-window hotkey is delivered as it
  is today" is about routing when a speech session is open. The speaker mode
  changes the transcript's content the same way for both destinations, so the
  routing it guarantees is unchanged.

## Goals / Non-Goals

**Goals:**

- `off` runs no new code beyond reading the setting. The `off` path calls the same
  `transcribe_pcm16` it calls today.
- One place in the code covers every delivered transcript.
- No new daemon command. One field is added to `status`. The overlay message
  format does not change.
- A speaker problem never costs the user a transcript.
- The speaker model holds no accelerator memory, is built lazily, and is
  released.
- No torch, and no second ONNX runtime in the daemon process.

**Non-Goals:**

- Full diarization with a segmentation model, overlapping speech, or speaker
  changes inside one Whisper segment.
- Filtering or labelling partial text on the overlay.
- Remembering unknown voices across sessions, or enrolling voices automatically.
- A line-per-speaker output format, or other per-speaker formatting options.
- Treating mine-only as authentication. A recording of the owner's voice is the
  owner's voice to this feature.
- A calibration tool for the threshold.

## Assumptions

These choices were made while planning, not confirmed with the user. Each one is
explained under Decisions. The less certain ones are repeated under Open
Questions.

1. When mine-only cannot run, Murmly fails open: the transcript is delivered as
   with `off`, one warning is logged at capture start, and `doctor` says why.
   This follows the silence-detection precedent.
2. A part too short to identify takes the speaker of the nearest identifiable
   part in the same recording or segment, preferring the part before it. A
   recording or segment with no identifiable part is delivered as with `off`.
3. A segment that mine-only drops entirely behaves like today's empty transcript.
   It is not a refusal and does not end the session.
4. Partial text on the overlay is not filtered.
5. Labelled text stays on one line.
6. The speaker mode applies to speech-session replies as well.
7. Speakers are attributed per Whisper segment. Matching uses cosine similarity
   against voiceprints, and unknown voices are clustered online.
8. Speaker embeddings run on the existing `onnxruntime`, on the CPU, released
   together with the transcription model.
9. Wheel availability, the model's licence, its hosting and its checksum are
   verified before any code is written (task group 1).
10. Only the embedding is stored. The voiceprint file is private to the user.
    Voices can be listed and removed. Names are never logged together with
    transcript text.
11. The daemon checks the voiceprint file when each capture starts. There is no
    reload command.
12. Configuration is a `[speakers]` table with `mode`, `owner` and
    `match_threshold`, and no separate idle period.

## Decisions

### Attribute speakers per Whisper segment

A new `FasterWhisperTranscriber.transcribe_segments_pcm16(pcm, rate)` returns
`list[TimedText(start_s, end_s, text)]` from the same decode the final pass runs
today. `transcribe_pcm16` keeps its exact output, and the `off` path keeps
calling it. For each segment, the PCM between `start` and `end` is converted to
mono float32 and resampled to 16 kHz with the existing `resample_float32`. The
embedding is computed from that audio.

A segment with less audio than the minimum (a placeholder of 1.0 s, set by
measurement in task 4.7) is not embedded. It takes its neighbour's speaker, as
the spec requires. Continuous mode already cuts segments at a configured silence
of 2 s by default, so most units contain one speaker's turn.

### Segment times are original-audio seconds, rounded to 0.01 s

Murmly builds a plain `WhisperModel`, not a batched one, and `_decode` calls
`model.transcribe(audio, language="en", beam_size=..., vad_filter=...)` with no
`clip_timestamps`, `without_timestamps` or `word_timestamps`. Those keep their
defaults (`clip_timestamps="0"`, `without_timestamps=False`,
`word_timestamps=False`). Checked in faster-whisper's source (task 1.6, source
half):

- `transcribe.py` lines 885-892: `if vad_filter and clip_timestamps == "0":`
  speech chunks are found with `get_speech_timestamps(audio, vad_parameters)` and
  the audio becomes `np.concatenate(audio_chunks, axis=0)`, with the silence
  removed.
- Lines 1009-1010: `if speech_chunks: segments = restore_speech_timestamps(...)`.
- Lines 1867-1868: `segment.start = ts_map.get_original_time(segment.start)` and
  `segment.end = ts_map.get_original_time(segment.end, is_end=True)`.
- `vad.py` lines 274-275: `return round(total_silence_before + time,
  self.time_precision)`, with `time_precision` 2.

So `start` and `end` are seconds from the start of the original audio, rounded
to 0.01 s. Each end is mapped separately, so a segment that spans removed
silence includes that silence when the audio is sliced at its times.
`decode_audio` resamples to 16 kHz mono (`audio.py` lines 37-40), and the times
do not depend on the capture rate.

Slicing the capture PCM uses these times directly. Murmly's `_write_wav` writes
`config.channels` channels, so the PCM can be interleaved. The slice is
downmixed to mono before `resample_float32` is called.

**Still open.** The other half of task 1.6, a real recording with a known pause
where the slice at a segment's reported times plays back as that segment's words,
has not been done. It needs a person to record and listen.

*Alternatives considered.* **Full diarization** runs a segmentation model (for
example pyannote segmentation-3.0, which sherpa-onnx ships as ONNX), clusters the
regions, and aligns them to Whisper's times. It catches speaker changes without a
pause and handles overlap. It costs a second model, alignment code, and in
practice the sherpa-onnx dependency. It stays a later option. **Word timestamps**
(`word_timestamps=True`) would give finer alignment, but they slow every final
decode and still need a segmentation step to use them.

### Matching and clustering

Embeddings are L2-normalised. A part is matched to the enrolled voice with the
highest cosine similarity if that similarity is at least the threshold.
Otherwise it is unknown, and it is compared with the running centroids of the
unknown voices already heard in this capture session. It joins the closest one
at or above the threshold, which updates that centroid as a normalised running
mean. If none is close enough, it starts a new numbered voice.

Mine-only keeps a part only when its best match is the owner. Enrolling other
household voices therefore makes mine-only stricter, because they compete with
the owner for borderline parts. One threshold serves both matching and
clustering, so there is one setting to tune.

*Alternative considered.* Agglomerative clustering over a whole recording groups
voices better within one recording. Continuous mode still needs numbers that
carry from one segment to the next, which requires the online form anyway. One
mechanism for both is simpler.

### Labelled text stays on one line

A labelled transcript is written `You: Shall we start? Speaker 1: Yes, go ahead.`
Each part is the label, a colon, a space and the words, and parts are separated
by a single space. Consecutive parts from one speaker are merged under one label,
and every delivered transcript begins with a label. A continuous segment
therefore starts with its speaker's label even when the previous segment ended
with the same speaker, because the user may have moved the cursor between them.
Mine-only output carries no labels.

The reason is the newline finding under Context. Murmly has never pasted a line
break, and every injector delivers the clipboard as a paste. A line break per
speaker would be the first line break Murmly ever pastes. A terminal program
without bracketed paste would run each labelled line as a command, and an agent
session would receive line structure it never received before. One line keeps
every existing destination safe.

Names are validated at enrolment for the same reason. A name may not be empty,
longer than 32 characters, or contain a line break, a control character, or a
colon. It may not equal "You" or "Speaker N" in any letter case. Otherwise a name
could bring line breaks back, or produce a label that reads as a different
speaker.

*Alternative considered.* One line per speaker is easier to read and may become
an option later. It is not the default, because the default must be safe in
every destination Murmly already delivers to.

### One seam for every destination

`SpeechSession` gains one helper, used by both `process_recording` and
`process_for_session` in place of the direct `transcribe_pcm16` call and before
their `if not text` check. With mode `off`, the helper is exactly
`transcribe_pcm16`. Otherwise it decodes timed segments, attributes them, and
returns filtered or labelled text.

This covers toggle, stop mode, continuous mode, and session-bound capture, as
confirmed by reading `_process_recording`. Because mine-only can return an empty
string, the existing empty-transcript path gives requirement "A transcript left
empty by mine-only is treated as no transcript" its behaviour unchanged. Nothing
is pasted, `_run_segment` returns before counting the segment, and
`_session_response` ignores it.

The helper catches any attribution error. It logs the error without text and
returns the plain joined text, as `off` would. An embedding failure therefore
never reaches `_run_segment`'s session-ending handler or the toggle's
`COMMAND_FAILED` path.

A toggle response's `text` is what was delivered, labels included. The response
is the caller's copy of the transcript, not a delivery signal, which is why the
spec allows names in it.

### Unknown voices live for one capture session, in memory

The unknown-voice centroids and their numbers are held on `SpeechSession` and
cleared in `start_recording`. A toggle or stop-mode recording is one session. A
continuous session keeps its numbering from segment to segment until the toggle
that ends it. Nothing about unknown voices is written to disk.

### Embedding runtime: plain onnxruntime on the CPU

A `SpeakerEmbedder` holder follows the synthesis session's pattern:

- It builds an `onnxruntime.InferenceSession` lazily under a load lock.
- It names `["CPUExecutionProvider"]` explicitly, even on a machine with
  `onnxruntime-gpu`.
- It sets `enable_cpu_mem_arena = False` and leaves intra-op threads at their
  default (journal 2026-08-25).
- Inference runs under a use lock, so a release waits for it.
- `resident` reads a field without taking either lock.
- `release()` drops the session and calls `return_free_heap()`.

The CUDA provider is never used. The 2026-08-26 synthesis measurement showed the
CUDA provider holding 876 MB against 65 MB on the CPU and never returning it. A
model this small gains nothing from a GPU.

**When it is built.** It is never built at daemon start or by `doctor`. When
capture starts and the mode is not `off`, a background build begins, in the same
way the transcription model's warm-up overlaps capture. Capture never waits for
it. If the build has not finished when the segments are ready, attribution waits
for it. If the build fails, attribution does not run for that recording, the
transcript is delivered as with `off`, and a warning is logged.

**When it is released.** It is released by the transcription model's
`IdleRelease` callback, so it follows `stt.unload_after_idle_s`, including 0
meaning never. The model is used only inside a capture unit, so "idle" means the
same thing for both models. A fourth setting for a few tens of megabytes would
add configuration without a use.

`status` gains `speaker_model_resident`, plus `speaker_model_resident_detail` on
error, read without loading or locking.

**Model.** WeSpeaker ResNet34-LM. See "The model: WeSpeaker ResNet34-LM".

*Alternatives considered.*
- **sherpa-onnx** offers a speaker-embedding extractor and full diarization, and
  computes the features itself. It bundles its own onnxruntime native library.
  The daemon would then load a second runtime alongside whichever `onnxruntime`
  or swapped `onnxruntime-gpu` it already has, which costs memory and makes the
  GPU-swap story harder to reason about.
- **pyannote.audio, SpeechBrain, NeMo and Resemblyzer** all require torch, which
  is not in the dependency tree and would add gigabytes.

`numpy` and `onnxruntime` become direct dependencies with lower bounds the
current lock already meets, so resolution does not move.

### Dependencies: `numpy>=2.0.2` and `onnxruntime>=1.20.1`

Verified in task group 1 (task 1.2). The lock holds `numpy` 2.5.2 and
`onnxruntime` 1.28.0. The floors are the ones the existing dependents already
require: `kokoro-onnx` 0.6.1 asks for `onnxruntime>=1.20.1` and `numpy>=2.0.2`,
and `faster-whisper` for `onnxruntime<2,>=1.14`. The change declares the same
floors, so the resolver has nothing new to decide.

The `onnxruntime` floor has to stay at 1.24 or below, and any API the speaker
code calls has to exist in 1.24. GPU machines swap in `onnxruntime-gpu` 1.24.4
under the same import name, and a floor above that would make the swap
unsatisfiable.

The locked wheels, by filename from `uv.lock`. `onnxruntime` 1.28.0 has no sdist.

| Cell | `onnxruntime` 1.28.0 | `numpy` 2.5.2 |
| --- | --- | --- |
| cp312 manylinux x86_64 | `onnxruntime-1.28.0-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl` | `numpy-2.5.2-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl` |
| cp312 win_amd64 | `onnxruntime-1.28.0-cp312-cp312-win_amd64.whl` | `numpy-2.5.2-cp312-cp312-win_amd64.whl` |
| cp312 macOS arm64 | `onnxruntime-1.28.0-cp312-cp312-macosx_14_0_arm64.whl` | `numpy-2.5.2-cp312-cp312-macosx_11_0_arm64.whl` and `-macosx_14_0_arm64.whl` |
| cp314 manylinux x86_64 | `onnxruntime-1.28.0-cp314-cp314-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl` | the same name with `cp314` |
| cp314 win_amd64 | `onnxruntime-1.28.0-cp314-cp314-win_amd64.whl` | the same name with `cp314` |
| cp314 macOS arm64 | `onnxruntime-1.28.0-cp314-cp314-macosx_14_0_arm64.whl` | the same two with `cp314` |

### The model: WeSpeaker ResNet34-LM

Chosen in task 1.3, with the training-data terms accepted by the user on
2026-10-09 (see Risks).

| Fact | Value |
| --- | --- |
| Publisher | The WeSpeaker project. The Hugging Face organisation `Wespeaker` is the official one: WeSpeaker's `docs/pretrained.md` links to it, and the model card says "Official model provided by Wespeaker project". |
| Repository | `Wespeaker/wespeaker-voxceleb-resnet34-LM`, commit `f0c48c298fd835726c27956a5d617bad7115627e` |
| Pinned URL | `https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/f0c48c298fd835726c27956a5d617bad7115627e/voxceleb_resnet34_LM.onnx` |
| Filename | `voxceleb_resnet34_LM.onnx` |
| Size | 26,530,309 bytes (26.5 MB) |
| SHA-256 | `7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068` |
| Download | HTTP 302 to `us.aws.cdn.hf.co`, so `curl` needs `--location`. The `x-linked-etag` header equals the SHA-256. |
| Input | `feats`, float32, shape `['B', 'T', 80]` |
| Output | `embs`, float32, shape `['B', 256]` |
| Embedding dimension | 256 |
| ONNX metadata | None. `custom_metadata_map` is empty, as it is in every publisher ONNX file considered. The feature facts below come from the pinned `config.yaml` and the publisher's scripts. |
| Output scale | Not L2-normalised. A dummy input gave a norm of about 0.81. |
| Training | VoxCeleb2 dev, 5994 speakers. The LM variant is fine-tuned on inputs of about 6 s (`num_frms` 600). |
| Licence | CC BY 4.0 |

Dummy inputs of 100, 200 and 500 frames each returned shape `(1, 256)`.

The pinned `config.yaml` sets `num_mel_bins` 80, `frame_length` 25,
`frame_shift` 10, `dither` 1.0 (for training only) and `embed_dim` 256.

**Licence.** The repository is tagged `cc-by-4.0`. WeSpeaker's
[`docs/pretrained.md`](https://github.com/wenet-e2e/wespeaker/blob/c28dfb71f557a7eee05be164edce2577bf8708f8/docs/pretrained.md)
(lines 19-23) says: "The pretrained model in WeNet follows the license of it's
corresponding dataset. For example, the pretrained model on VoxCeleb follows
Creative Commons Attribution 4.0 International License." Murmly treats the file
as [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/legalcode), which
allows commercial use and redistribution with attribution, a licence notice and
a note of changes. Murmly does not redistribute the file at all: setup downloads
it from the publisher onto the user's machine. The licence text and attribution
go in `licenses/`. The repository's own licence is Apache-2.0.

**Why not CAM++.** The CAM++ VoxCeleb model (3D-Speaker, 512 dimensions) is
Apache-2.0 by its ModelScope card and repository, and is also trained on
VoxCeleb2. It was rejected for two reasons:

- The publisher ships only a PyTorch checkpoint (`campplus_voxceleb.bin`). The
  only ONNX file is a re-export by k2-fsa/sherpa-onnx, whose release assets were
  re-uploaded on 2024-10-14 after publishing on 2023-12-08, so a fixed release
  URL can change. That fails the rule that the source is the publisher's own
  release.
- Both CAM++ ONNX files tried, sherpa-onnx's and WeSpeaker's own, gave wrong
  embeddings at most input lengths. They were good only at 200, 400 and 600
  frames. At 150, 202, 250 and 300 frames every speaker looked about 0.9 similar
  to every other, on three clips. The cause is inferred, not confirmed: shapes
  fixed at export.

**Comparison candidate for task 4.7.** The non-LM ResNet34 has the same inputs and
outputs: `https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34/resolve/ff1ac5bca8ef11e90662b879aa923979e0bd277b/voxceleb_resnet34.onnx`,
26,534,127 bytes, SHA-256
`9fea6516d7ad6bf0a76c7689f5a49b65d330fad6dde96c91bb4435ffbfe056a1`. Its
repository is tagged `apache-2.0` with a 31-byte README, while WeSpeaker's
documentation says CC BY 4.0. If the LM model's fine-tuning on long inputs hurts short parts in
task 4.7, this one is the model to compare it with.

### Filterbank features: a numpy implementation

`kaldi-native-fbank` is not used. Task 1.1 listed the wheels of its latest
release, 1.22.3 (2025-10-09, Apache-2.0, no abi3 wheels), for the six cells of
the CI matrix:

| Cell | Wheel |
| --- | --- |
| cp312 manylinux x86_64 | `kaldi_native_fbank-1.22.3-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.whl` |
| cp312 win_amd64 | `kaldi_native_fbank-1.22.3-cp312-cp312-win_amd64.whl` |
| cp312 macOS arm64 | `kaldi_native_fbank-1.22.3-cp312-cp312-macosx_11_0_arm64.whl` |
| cp314 manylinux x86_64 | `kaldi_native_fbank-1.22.3-cp314-cp314-manylinux2014_x86_64.manylinux_2_17_x86_64.whl` |
| cp314 win_amd64 | **missing**: only `cp314-cp314-win32` exists, and no version ever shipped a cp314 win_amd64 wheel |
| cp314 macOS arm64 | `kaldi_native_fbank-1.22.3-cp314-cp314-macosx_11_0_arm64.whl` |

The package has an sdist, so `uv sync --locked` on `windows-latest` with Python
3.14 would try a CMake and MSVC source build. That is the reason for the numpy
fallback. The cp312 win_amd64 wheel was also inspected: it installs
`_kaldi_native_fbank.cp312-win_amd64.pyd` and `kaldi-native-fbank-core.dll` and
`.lib` into the root of site-packages, with duplicates under
`kaldi_native_fbank/lib/`. The `.pyd` is PE32+ x86-64 and imports
`MSVCP140.dll`, `VCRUNTIME140.dll`, `VCRUNTIME140_1.dll` and `api-ms-win-crt-*`,
none of which the wheel bundles, so a Visual C++ runtime would also be required.

The numpy implementation follows the publisher's recipe
(`wespeaker/bin/infer_onnx.py` and `wespeaker/cli/speaker.py` at commit
`9fecd6cb4f47475d01761d87c826298dff4ef18c`), which calls
`kaldi.fbank(num_mel_bins=80, frame_length=25, frame_shift=10, dither=0.0,
window_type='hamming', use_energy=False)`. The remaining parameters are
torchaudio's `compliance/kaldi.py` defaults (v2.8.0). The full recipe:

- 16 kHz mono input, scaled to the int16 range: `waveform * 32768`. WeSpeaker's
  `infer_onnx.py` does `waveform = waveform * (1 << 15)`, and sherpa-onnx's
  `add_meta_data.py` says "all models from wespeaker expect input samples in the
  range [-32768, 32767]".
- Frames of 25 ms every 10 ms, with `snip_edges=True`: no padding, and a trailing
  partial frame is dropped.
- Remove the DC offset of each frame.
- Pre-emphasis 0.97.
- Hamming window, `torch.hamming_window(periodic=False)`, that is 0.54 and 0.46.
  It is not the Povey window.
- FFT size 512 (the frame length rounded up to a power of two).
- Power spectrum.
- 80 mel bins from 20 Hz (`low_freq`) to the Nyquist frequency.
- Natural log with a floor at the float32 epsilon.
- Dither 0.
- Mean normalisation over the utterance (CMN) without variance normalisation
  (no CVN): `mat = mat - mean(mat, axis=0)`. It is applied identically at
  enrolment and at recognition.

The implementation is checked against a small reference fixture produced once
with the reference implementation and committed (task 4.1).

An ad hoc check, not yet against a reference, ran ResNet34-LM on ModelScope's
three 16 kHz example clips (two speakers) with a numpy filterbank. Whole clips
scored 0.718 for the same speaker and -0.095 and -0.205 for different speakers.
Changes to the window or the scale moved the similarities by less than 0.02 on
this clean audio. See Risks for what it showed for short parts.

### The model is fetched at setup, pinned and checksummed

`setup.sh` and `bootstrap.ps1` download the model into the data directory from
the publisher's own release URL, pinned to an exact file. Each verifies a
SHA-256 recorded in the script and deletes the download on a mismatch. These
are the first checksums either script carries. The daemon never downloads it.
When the file is absent, speaker recognition is unavailable, and both `doctor`
and `murmly enrol` name the file and the setup command. The model's licence text
goes in `licenses/`.

The model's identity is the SHA-256 of its file. That identity is stored with
the voiceprints and compared on load.

The download is 26.5 MB. `bootstrap.ps1` has never fetched anything before, so
this is its first download, made with `Invoke-WebRequest` and checked with
`Get-FileHash`. `setup.sh` refuses macOS, so on macOS the model is not fetched by
either script. The Linux download was repeated on a second fetch and gave the
same size and SHA-256 as recorded (task 1.9, Linux half). The Windows half is
open.

### Voiceprint store

The voiceprints are stored in `<data dir>/voiceprints.json`:

```json
{"version": 1, "model_sha256": "…", "dimension": 256,
 "voices": [{"name": "Milo", "embedding": [0.012, …]}]}
```

- **Writing.** Every write goes to a temporary file in the same directory, then
  `os.replace`. On POSIX the file is created through `os.open(..., 0o600)`, and a
  directory Murmly creates gets mode `0o700`. On Windows the file inherits the
  per-user ACL of `%LOCALAPPDATA%`. **Task 1.7 is still open.** It needs
  `icacls %LOCALAPPDATA%\murmly` on a Windows machine, and nothing has been
  run. This design keeps the assumption that the inherited ACL excludes other
  standard accounts, and the Windows part of task 5.2 depends on the result.
- **No audio is written.** `murmly enrol` computes the embedding from the PCM it
  holds in memory. No WAV is ever written, which is stronger than deleting one.
- **Removing.** Removing the last voice, or `--all`, deletes the file.
- **Model mismatch.** Voiceprints whose `model_sha256` differs from the
  installed model are ignored at load and reported by `doctor` as needing
  re-enrolment.
- **The owner** is the `[speakers] owner` setting, not a field in this file. That
  keeps one source for each fact: configuration says who the owner is, the store
  says what voices exist.

### The daemon picks up voiceprints when capture starts

At each capture start, when the mode is not `off`, `SpeechSession` stats the
voiceprint file (`st_mtime_ns`, `st_size`). It reloads the file if either
changed, and treats a missing file as no voices. This costs one `stat` per
capture. It avoids a new daemon command, which would have touched the
command-interface spec and its failure codes. A removed voice leaves the
daemon's memory at the next capture start, as the spec requires.

### Enrolment and voice management commands

`murmly enrol <name> [--seconds N]` runs these checks first, in order:

1. Name validity.
2. Model presence.
3. A `status` request to the daemon. When no daemon answers, enrolment proceeds.
   When the daemon answers `IDLE`, enrolment proceeds. `LISTENING`, `THINKING`
   and `SPEAKING` are refused.

It then prints a passage to read aloud and records for the placeholder default
of 20 s, in its own process as `spike` does. Silero VAD, the one `silence.py`
loads from faster-whisper, measures the speech. Enrolment needs at least the
placeholder minimum of 10 s of speech, otherwise it is refused and nothing is
stored. The speech regions are embedded in windows of a few seconds, and the
average is L2-normalised to form the voiceprint. The command prints where the
voiceprint is stored and how to remove it. When no owner is configured, it also
prints the `[speakers] owner` line to add.

The recording is a short-lived PortAudio stream in a CLI process, the case
`docs/agent-notes/portaudio-jack-exit-abort.md` covers. Task 6.6 checks it
against that note.

`murmly speakers list` prints the names and marks the owner.
`murmly speakers remove <name>` and `murmly speakers remove --all` delete
voiceprints. None of these commands prints vectors.

### Configuration

A new `[speakers]` table:

| Key | Type | Default | Fallback |
| --- | --- | --- | --- |
| `mode` | `off` \| `mine-only` \| `label-everyone` | `off` | unrecognised value falls back to `off`; `mode_rejected_value` recorded |
| `owner` | string | empty, meaning no owner | none |
| `match_threshold` | integer percent, 30–90 | placeholder 50, set by measurement in task 4.7 | `_bounded_int` and `_rejected_value` |

The threshold is an integer percent, not a float. That is how `tts.rate` already
works, and it reuses the existing helpers. A float such as `0.55` reads as 0,
which is out of bounds, so it falls back to the default and is reported as
rejected rather than silently truncated. Like every other setting, these are
read when the daemon starts.

### Diagnostics

`murmly doctor` gains a `speaker_recognition` section. Its keys are always
present, with `None` where a key does not apply:

- `mode`, `mode_rejected_value`
- `match_threshold`, `match_threshold_rejected_value`
- `owner`, `owner_enrolled`
- `enrolled_count`, `enrolled_names`
- `voiceprints_path`, `voiceprints_need_reenrolment`
- `model_path`, `model_present`, `model_checksum_matches`
- `available`, `detail`
- `provider` (always the CPU provider)
- `resident`, `resident_detail`
- `unload_after_idle_s` (the transcription period it follows)

The section checks the model's presence and checksum by hashing the file. It
never builds a session, so it neither loads a model nor has to declare that it
did. Residency comes from the daemon's `status` answer, asked at the same moment
as the other residency fields.

### Logging and signals

Warnings name the reason only, for example "Speaker recognition disabled for
this capture: no owner is enrolled". Attribution may log counts, such as "mine-only
kept 3 of 5 parts", and similarity scores, but never a name together with
transcript text. Overlay messages are unchanged, so they carry neither.

### How a disabled speaker mode reaches the user

When the mode cannot run, the user is told the way the silence-detection
precedent tells them. The daemon logs one warning naming the reason at capture
start, or when attribution fails. `murmly doctor` reports `available: false` and
the reason. Nothing appears on the overlay, in the toggle response, or in the
pasted text. This is deliberate, to match the precedent, and its cost is listed
under Risks.

### Partial text and speech sessions

Partial passes are not attributed. Attributing them would run several embeddings
on every live tick, against a spec that already says partials never decide what
is delivered. The consequence is that in mine-only mode the overlay can show
other people's words that will not be typed.

Speech-session replies go through the same helper, through
`process_for_session`. In label-everyone mode, the agent therefore receives
`You: …`-prefixed text.

## Risks / Trade-offs

- **Failing open in mine-only types other people's words.** The user finds out
  only from the log or `doctor`, because the precedent signals nothing at
  capture time. → `doctor` states the reason. The manual tells mine-only users to
  run `doctor` after setup. The fail-closed alternative is an open question.
- **The owner's own words can be dropped.** A different microphone, a cold, or
  room noise can push the owner's similarity below the threshold, and mine-only
  then silently drops what they said. This is the costliest failure for the
  mode. → The threshold is configurable with bounds, and its default is
  measured on real voices (task 4.7). Short parts take a neighbour's speaker.
  The manual says to enrol with the microphone used every day, and to re-enrol
  after changing it.
- **Two speakers inside one Whisper segment are attributed as one.** That part
  is kept, dropped, or labelled whole. → This is a documented limit. Continuous
  mode's silence boundary reduces it, and full diarization remains a later
  option.
- **Overlapping speech is not separated.** → The same documented limit applies.
- **Added latency between capture stop and delivery.** One embedding is computed
  per segment on the CPU. A rough first measurement (i9-11980HK, 16 threads,
  `onnxruntime-gpu` 1.24.4 on its CPU provider, random features, one run each)
  gave about 340 ms for 30 calls of 2 s, which is one minute of speech, and about
  500 ms for one 60 s call. The limit is 500 ms per minute of speech, so this is
  close to it on a fast CPU. → Task 7.5
  measures it on real recordings and records the figure. If it exceeds 500 ms per
  minute of speech, the approach is revisited before shipping.
- **Short parts may not match the owner at the placeholder threshold.** In the ad
  hoc check under "Filterbank features", slices of 2 s from the same speaker
  scored 0.49 and slices of 1 s scored 0.35, against different speakers below 0.
  The placeholder threshold is 50, so in this one sample the owner would be
  rejected on 1 to 2 s parts. This is one sample of two speakers, and it is
  indicative only. The LM model is fine-tuned on inputs of about 6 s, which may
  make this worse. → Task 4.7 sets the threshold and the minimum part length on
  real recordings, and compares the non-LM model.
- **The model's training data has unclean terms.** The model is trained on
  VoxCeleb2, and the terms published for VoxCeleb disagree:
  - KAIST ([mm.kaist.ac.kr/datasets/voxceleb](https://mm.kaist.ac.kr/datasets/voxceleb/)):
    "The VoxCeleb dataset is available to download for research purposes under a
    Creative Commons Attribution 4.0 International License. The copyright remains
    with the original owners of the video."
  - KAIST's `files/license.txt`: "The copyright of both the original and cropped
    versions of the videos remains with the original owners. The data is covered
    under a Creative Commons Attribution 4.0 International license".
  - Oxford VGG ([vox1](https://www.robots.ox.ac.uk/~vgg/data/voxceleb/vox1.html),
    [vox2](https://www.robots.ox.ac.uk/~vgg/data/voxceleb/vox2.html)): "The
    provided VoxCeleb metadata is licensed under a Creative Commons
    Attribution-ShareAlike 4.0 International License."

  The model itself is CC BY 4.0. Augmentation data is MUSAN (CC BY 4.0,
  [openslr.org/17](https://www.openslr.org/17/)) and RIRS_NOISES (Apache 2.0,
  [openslr.org/28](https://www.openslr.org/28/)). Every candidate considered was
  trained on VoxCeleb2, so the choice of model does not avoid this. The user
  accepted the ambiguity on 2026-10-09. → The licence file in `licenses/` and the
  manual both state the training-data terms. Murmly does not redistribute the
  file.
- **Daemon memory.** The session costs tens of megabytes while resident.
  `onnxruntime` is usually already imported, through the VAD filter. → The
  session is built only in a speaker mode and released with the transcription
  model.
- **Biometric data about other people.** Enrolling Milo stores Milo's voiceprint.
  → It is stored locally only, in a file private to the user, and can be listed
  and removed. The manual says to ask before enrolling someone, and says that
  uninstalling leaves voiceprints in place until they are removed.
- **Licence or wheel availability may fail verification.** → Task group 1 was a
  gate, and it passed: ResNet34-LM is CC BY 4.0, `numpy` and `onnxruntime` have
  wheels for all six cells, and `kaldi-native-fbank` lacks a cp314 win_amd64
  wheel, so the numpy fallback is used and no dependency is added. The two
  dependencies that are added have wheels on `macos-14`, so no marker is needed.
  A future required dependency with no macOS arm64 wheel would break that CI job,
  and would need a marker.
- **Adding dependencies resyncs the environment.** On GPU machines that
  reinstalls CPU `onnxruntime` over the GPU build. → Task 2.4 re-applies the swap
  according to the agent note. Tests always run with `--no-sync`.
- **Mine-only is not access control.** Played-back audio of the owner passes. →
  The manual says so.

## Migration Plan

No migration. `mode` defaults to `off`, so an existing `config.toml` produces an
identical session, and the daemon reads no voiceprint file. To roll back, set
`mode = "off"`, run `murmly speakers remove --all`, and delete the model file
from the data directory, or revert the change. Nothing else persists.

## Open Questions

Each item below is decided for this change. Reversing one would change a spec
scenario but not the approach or the task breakdown.

- **Fail open or fail closed.** Should mine-only that cannot run type nothing
  instead? The current choice follows the silence-detection precedent. A related
  question is whether the overlay should show a cue when the mode is disabled
  for a capture, since today nothing at capture time tells the user.
- **Short recordings.** A recording or segment with no identifiable part is
  delivered as with `off`. In continuous mode this types a television's short
  "Yes!" in mine-only. Is that acceptable, or should such segments be dropped in
  mine-only?
- **Speech-session replies.** Should label-everyone label replies sent to an
  agent, or only filter them?
- **Placeholder values pending measurement.** These are tuned by task 4.7 and
  task 6.7 without changing the specs:
  - the default threshold (placeholder 50);
  - the minimum part length (1.0 s);
  - the enrolment length (20 s);
  - the minimum enrolment speech (10 s).
- **Implicit owner.** Should a lone enrolled voice become the owner when `owner`
  is unset?
- **Uninstall.** Should `murmly uninstall` offer to delete voiceprints?
