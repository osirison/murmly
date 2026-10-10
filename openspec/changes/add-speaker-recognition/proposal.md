---
title: Add Speaker Recognition
description: Let Murmly type only the owner's speech, or type everyone's speech labelled by who said it, from voices enrolled once by name
---

## Why

Murmly types every voice its microphone hears. In a shared room, with a
television on, or on a call played through speakers, other people's words land
in the user's document as if the user had said them, and nothing in the
transcript shows where they came from. A continuous session makes this worse,
because it keeps delivering for as long as it runs.

The same capture could serve a second purpose as well: a quick record of a
conversation, split by who said what, typed where the user is working.

## What Changes

- **A speaker mode setting.** A new `[speakers]` table selects one of three
  modes: `off` (the default), `mine-only`, or `label-everyone`. With `off`, the
  daemon reads no voiceprints, loads no speaker model, and produces exactly the
  transcript it produces today. Enrolment and the voice commands work in every
  mode, so voices can be enrolled before a mode is switched on.
- **Enrolment by name.** `murmly enrol <name>` records a short voice sample in
  the command's own process, the way `murmly spike` does, and stores a voiceprint
  under that name. It refuses while the daemon is listening, transcribing, or
  speaking. One enrolled person is named as the owner in configuration.
- **Mine-only.** Only the owner's speech is delivered. Speech from other
  enrolled people, from unknown voices, and from a television is dropped before
  delivery.
- **Label-everyone.** All speech is delivered, split by speaker on one line: the
  owner as `You:`, other enrolled people by their enrolled name, and unknown
  voices as `Speaker 1:`, `Speaker 2:` in order of first appearance. In a
  continuous session the numbering holds for the whole session.
- **Every delivered transcript is covered.** The speaker mode applies to
  window-bound and speech-session-bound capture, in toggle, stop, and continuous
  modes. Partial text on the overlay stays unfiltered.
- **Failing open.** When the speaker mode cannot run — no owner enrolled for
  mine-only, a missing or broken speaker model, voiceprints made with a
  different model — the transcript is delivered as with `off`, a warning is
  logged, and `murmly doctor` says why. A segment that mine-only drops entirely
  behaves like an empty transcript today: nothing is pasted and a continuous
  session keeps listening.
- **Voiceprints are private and removable.** Only the embedding vector is
  stored, never the enrolment audio. The file lives in Murmly's data location and
  is readable only by the user. `murmly speakers list` and
  `murmly speakers remove` show and delete enrolled voices, and the running
  daemon picks up changes at the next capture without a restart.
- **A small speaker model on the CPU.** A speaker-embedding model runs through
  the `onnxruntime` already installed. It is built lazily, never holds
  accelerator memory, and is released with the transcription model when that
  model's idle period elapses. `status` and `murmly doctor` report its residency.
- **Diagnostics** report the mode, the match threshold, the owner, the enrolled
  names and count (never the voiceprints), the model file, and whether speaker
  recognition can run.

No existing configuration key, spec requirement, or response field changes
meaning. With the default `off`, nothing changes at runtime. Setup does change
for everyone: `setup.sh` and `bootstrap.ps1` fetch the speaker model whether or
not a mode is ever selected. This change is additive.

## Capabilities

### New Capabilities

- `speaker-recognition`: enrolling voices by name, designating the owner, the
  speaker mode and its filtering and labelling rules, the behaviour when
  recognition cannot run, how voiceprints are stored and deleted, the speaker
  model's residency, and what diagnostics report about all of it.

### Modified Capabilities

None. The baseline requirements on residency, diagnostics, and delivery are
written for the transcription model and the synthesis session, or in terms
general enough to cover a third model, and each stays true as written. What this
change adds lives in the new capability.

## Impact

- `src/murmly/stt.py` — a transcription entry point that returns Whisper's timed
  segments instead of one joined string. `transcribe_pcm16` keeps its exact
  output.
- New `src/murmly/speakers.py` — the voiceprint store, feature extraction, the
  embedding session, matching against enrolled voices, clustering of unknown
  voices, and formatting of filtered or labelled text.
- `src/murmly/daemon.py` — `SpeechSession.process_recording` and
  `process_for_session` apply the speaker mode before their empty-text check.
  Unknown-speaker state is kept per capture session. The speaker session is
  built at capture start and released with the transcription model.
  `speaker_model_resident` is added to `status`.
- `src/murmly/cli.py` — the `enrol` and `speakers` subcommands, and a
  `speaker_recognition` section in `murmly doctor`.
- `src/murmly/config.py` — a `[speakers]` table with `mode`, `owner`, and
  `match_threshold`, using the existing fallback and rejected-value reporting.
- `setup.sh`, `bootstrap.ps1` — fetch the speaker model into the data location
  and verify its SHA-256 checksum.
- `config.example.toml`, `manual/settings.md`, `manual/where-your-words-go.md`, a
  new manual page on enrolment, `mkdocs.yml`, `licenses/`.
- Dependencies: `numpy` and `onnxruntime` become direct dependencies; both are
  already installed as transitive dependencies. `kaldi-native-fbank` is added
  only if task group 1 confirms its wheels; otherwise filterbank features are
  computed in numpy. One model file of a few tens of megabytes is downloaded.
  Its licence and wheel availability are verified in task group 1 before any
  code is written.
