---
title: Add Speech Output Design
description: Technical approach for local synthesis, a bidirectional speech session, hotkey-triggered barge-in, and transcripts routed to the session that asked for them
---

## Context

See proposal.md — Why. The constraints that shape the approach, each verified
against the current tree or measured on the target machine:

- **No output path exists.** `sd.RawInputStream` (`audio.py:119`) is the only
  PortAudio object the repository ever constructs. There is no
  `check_output_settings` preflight, no playback buffer, no writer thread, and no
  way to abort audio already handed to a device.
- **Everything in the repository is little-endian int16 PCM `bytes`**
  (`audio.py:110`, `audio.py:123`, `stt.py:143`, `silence.py:145`). Synthesis
  produces float32. The conversion idiom already exists at `stt.py:136-144`. The
  only rate adaptation in the tree is integer-multiple decimation that refuses
  non-integer ratios (`silence.py:121-128`).
- **A command carries no payload.** `handle_command` takes `(command: str)`
  (`daemon.py:937`) and `_dispatch_request` reads only `request["command"]`
  (`daemon.py:930`), so a `text` field is discarded silently.
- **One request, one response.** `_read_request` reads until the first newline
  (`daemon.py:901-918`) and `_write_response` writes exactly one frame
  (`daemon.py:783-798`), guarded by `_claim_response` so shutdown and the worker
  cannot both answer.
- **`MAX_COMMAND_BYTES` is 4096** (`daemon.py:38`, enforced at `daemon.py:915`) and
  there are **eight worker slots** (`MAX_COMMAND_WORKERS`, `daemon.py:39`).
- **The state machine answers BUSY for any command that does not match the current
  state** (`daemon.py:958-961`), and `CommandCode` (`daemon.py:56-70`) has no member
  for cancelled or interrupted.
- **There is no state meaning output is active.** `OverlayState` has exactly IDLE,
  LISTENING and THINKING (`overlay.py:43-46`); the daemon's `_state` uses the same
  three (`daemon.py:945-962`).
- **Three consumers read the live microphone while LISTENING**: the silence
  detector (`daemon.py:493`), live partial transcription (`daemon.py:500`), and the
  final transcript via `take_segment` (`audio.py:173`). Each would ingest Murmly's
  own voice if capture and playback overlapped.
- **`load_config` reads exactly five named tables** (`config.py:137-141`) and
  `_get_table` returns `{}` for any other (`config.py:225`), so a `[tts]` table
  added today is a silent no-op with no warning.
- **`resolve_runtime` validates against CTranslate2's vocabulary**
  (`stt.py:169`, `VALID_COMPUTE_TYPES` at `config.py:58`). Passing `config.device`
  and `config.compute_type` to an ONNX runtime would hand it values such as
  `int8_float16`, which it does not accept.
- **The installer models one shortcut.** A fixed `murmly toggle` Exec line with a
  single `X-KDE-Shortcuts` binding (`installer.py:176`), and `murmly install` takes
  exactly one hotkey argument (`cli.py:75-79`).
- **The test suite has no output fakes.** The fake sounddevice module defines only
  `PortAudioError`, `query_devices`, `check_input_settings` and `RawInputStream`
  (`tests/test_audio.py:69-78`), and `FakeStream` exposes `start`, `stop`, `close`
  and `samplerate` (`tests/test_audio.py:21-35`) with no `write`.

Measured on the target machine (RTX 3080 Laptop 16 GB, i9-11980HK, Fedora 44):

- Measured: synthesis real-time factor is flat across input length — 0.024 on GPU
  and 0.18–0.21 on CPU for inputs from 2 to 588 words. Flat RTF is the signature of
  a model that produces the whole utterance in one pass, so cost tracks output
  duration and nothing is audible until all of it exists.
- Measured: time to first audio for a 349-word passage is 3066 ms produced whole
  and 210 ms produced sentence by sentence. For 59 words it is 504 ms against
  204 ms. Sentence-by-sentence is flat at roughly 205 ms regardless of length and
  costs about 33% more total compute.
- Measured: sentence-by-sentence output is **1.11 s shorter** than the same passage
  produced whole, across four internal sentence boundaries — about 0.28 s of
  inter-sentence silence discarded per boundary. A three-sentence passage showed the
  same effect at 0.15 s per boundary.
- Measured: 684 MiB resident VRAM, ~1.5 GB host RSS, 532 ms model load on CPU and
  803 ms on GPU, with a further ~300 ms warmup on the first GPU call.

## Goals / Non-Goals

Goals:

- Speech begins promptly and does not degrade as the text gets longer.
- A sender learns, without asking, that it was cut off and what was not heard.
- The person's reply reaches the sender that was speaking.
- Murmly never transcribes its own voice.
- Transcription, delivery, the overlay, and the existing hotkey behave exactly as
  they do today.

Non-Goals:

- **Reading a highlighted selection aloud.** The engine supports it; it needs a
  hotkey and a subcommand and is deferred to keep this change to one behaviour.
- **Acoustic echo cancellation and voice-activated barge-in.** Interruption is a
  keypress, so playback and capture never overlap and there is nothing to cancel.
- **A visual indicator while speaking.** `recording-overlay` forbids an overlay
  outside the capture lifecycle (`recording-overlay` spec.md:16-18). Rather than
  weaken that rule, speech is deliberately silent visually in this change. This is
  recorded here so a later reader does not assume the overlay already covers it.
- **Voice cloning.** Not offered by the chosen model and not asked for.
- **Speaking languages other than English.** `stt.py:123` hardcodes `language="en"`
  on the transcription side; matching that is deliberate.

## Decisions

### The session is a connection, not a job identifier

The alternative was submit-returns-handle: `speak` answers immediately with an id,
`stop` names that id, `status` reports progress. It preserves the one-response rule
exactly and needs no transport change, which makes it the cheaper option.

It cannot deliver the central requirement. **The person who interrupts is not the
sender.** They press a capture hotkey, which is a separate process on a separate
connection, and the response to that keypress goes to the process that pressed it.
The sender asked no question, so there is no response frame that can carry the news
to it. Its only recourse is polling `status`, which either burns cycles or delays
the sender's reaction, and which races: a poll landing between the interruption and
the next speech sees a state that has already moved on.

Submit-returns-handle has a second defect for this use. Each `speak` would be its
own connection dispatched to one of eight workers (`daemon.py:39`), so two pieces of
text sent back to back can be enqueued out of order. Guaranteeing order would force
the sender to await each response before sending the next, putting a round trip
between every sentence in a design whose whole purpose is to start speaking sooner.
One connection gives ordering for free.

### Sessions do not draw on the command worker pool

A session lasts as long as an exchange between a person and a sender. Eight open
sessions holding all eight worker slots would deny `status` and `toggle` for the
duration, which is why the modified `command-interface` requirement states this as a
rule rather than leaving it to implementation. Sessions get their own accounting.

### Text is produced one sentence at a time, and the dropped pause is put back

Because real-time factor is flat, the only lever on how soon speech starts is how
small the first unit of work is. Producing sentence by sentence takes time to first
audio from 3066 ms to 210 ms on a long passage and holds it flat at roughly 205 ms
regardless of length.

It also loses something, which is not obvious without measuring: the inter-sentence
silence the model generates when it sees the whole passage. Measured at ~0.28 s per
boundary, which is why the requirement is written as *the pauses between sentences
are preserved* rather than as *produce sentence by sentence*. The mitigation is to
reinsert that silence between units; with it reinserted the two are within 10 ms of
each other in total duration and were judged indistinguishable by ear.

That 0.28 s is one voice at one speed, and measuring the rest showed it must not be
written into the code. Across four voices at three speeds the boundary gap the model
produces ranges from 0.159 s to 0.435 s, falling as the speaking rate rises, and one
voice (`bf_emma`) produces *longer* audio sentence by sentence than it does in one
pass, so a fixed insertion would stretch it further. The pause is therefore derived
per voice and speaking rate: one calibration passage is produced whole, and the
silent run the model leaves between its sentences is measured out of that audio.
The measurement happens at the first sentence boundary rather than at synthesizer
start, so it runs while the first sentence is already playing instead of ahead of
it — putting a whole extra synthesis in front of the first audible word is the
delay this design exists to remove. A passage of one sentence never pays for it. What is inserted at a boundary is that gap less the
silence the two units already carry — `max(0, gap - trailing(previous) -
leading(next))` — because independently produced units keep their own leading and
trailing silence and inserting the whole gap on top would double it.

Restoring the gap does not close the whole difference, and the requirement is
written about the silence between sentences rather than about total duration for
that reason. Producing a passage sentence by sentence also speaks it slightly
faster, which no amount of inserted silence corrects; on a six-sentence passage
calibration removes roughly a quarter to a half of a naive concatenation's deficit
and the remainder is speech rate, not pause.

The interface between the daemon and the synthesizer therefore yields
`(audio_chunk, sample_rate)` rather than a whole buffer, so a one-pass model driven
in chunks and a model that streams natively remain interchangeable.

### Position reported is what was played, not what was produced

Producing sentence by sentence means sentence five is being produced while sentence
four is audible, and audio already written to the output device is heard after
Murmly stops writing. Reporting the production frontier would tell a sender the
person heard something they did not. The requirement therefore fixes the reported
position to played units, and fixes granularity at the piece of text the sender
gave — no finer position is honest.

### Barge-in is a keypress, so capture and playback never overlap

The alternative is listening while speaking and detecting the person's voice, which
requires acoustic echo cancellation — PipeWire's `module-echo-cancel` with
`monitor.mode` — plus voice activity detection tuned not to trigger on Murmly's own
output. The keypress version stops playback and only then opens the microphone, so
the three live-microphone consumers (`daemon.py:493`, `daemon.py:500`,
`audio.py:173`) cannot hear Murmly at all. This is the single largest simplification
in the change and it is the direct consequence of interruption being explicit.

The converse case is also closed: text arriving while capture is running is held
rather than spoken over the person.

### Which hotkey was pressed decides where the transcript goes

Both hotkeys stop speech and both notify the session that it was interrupted,
because a sender needs to stop generating regardless of who the person was talking
to. They differ in one thing: the focused-window hotkey delivers exactly as today,
and the session hotkey delivers to the open session and does not touch the clipboard
or paste anything.

Routing by hotkey rather than by "is a session open" is deliberate. It keeps the
existing hotkey's behaviour identical whether or not a session is open, which means
no existing scenario in `transcript-delivery` changes meaning, and it lets the person
choose the destination at the moment they press the key rather than inferring it.

### Speech output gets its own runtime resolution

`resolve_runtime` (`stt.py:169`) validates compute types against CTranslate2's
vocabulary (`config.py:58`); an ONNX runtime does not accept `int8_float16`. Sharing
`config.device` would hand one runtime the other's values. Synthesis resolves its own
provider and reports availability the way `silence.py:56-62` does — an `available`
flag with an `unavailable_reason`, logged once and surfaced by `murmly doctor` —
rather than raising.

### `[tts]` must be added to the table list, not just documented

`load_config` reads exactly five named tables (`config.py:137-141`). A user who adds
`[tts]` to `config.toml` before this change lands gets no speech and no warning. The
table has to be registered in the loader, and every new key validated through the
existing `_bounded_int` / `_boolean` helpers (`config.py:232-241`) that fall back to
a named default rather than refusing to start.

### The word "session" is overloaded and the specs must not blur it

`transcript-delivery` already uses "session" for a capture session that closes one or
more segments, and `desktop-integration` uses it for the graphical login session.
This change adds a third meaning. Every requirement added or modified here says
"speech session" in full where the new meaning is intended, and the existing
sentences that use "session" in the old sense are reproduced unchanged.

## Risks / Trade-offs

- **The transport contract changes.** `command-interface`'s one-response rule was
  hardened deliberately, and this change carves an exception into it. The exception
  is opt-in and declared by the caller, and every existing scenario is reproduced
  unchanged, so a caller that does not ask for a session cannot observe a
  difference. The risk is that the exception becomes the way future features avoid
  the rule; the requirement is worded to make speech sessions the exception rather
  than a general streaming mechanism.
- **Two ML runtimes in one process.** Transcription runs on CTranslate2 and
  synthesis on ONNX Runtime. They must share one CUDA stack; the pinning constraint
  is recorded in `docs/agent-notes/onnxruntime-gpu-cuda-version.md`. Co-residency is
  now measured rather than assumed, in one process holding `large-v3-turbo` on
  CTranslate2/CUDA at `float16` and `kokoro-v1.0.onnx` on `onnxruntime-gpu` 1.24.4,
  on the target machine:

  | State | Resident VRAM | Host RSS |
  |---|---|---|
  | Transcription model loaded, after one decode | 2290 MiB | 774 MiB |
  | Both models resident, after one synthesis | 2822 MiB | 1566 MiB |

  Synthesis adds 532 MiB of VRAM alongside a loaded transcription model, against a
  16 GB card, so co-residency is comfortable. Load costs are 2.0–3.7 s for the
  transcription model, 539 ms to construct the synthesis session, and 340 ms for the
  first synthesis that follows it.

  Two provider faults were found doing this, and both are silent:

  - **The ONNX CUDA provider needs four cu12 libraries CTranslate2 does not.**
    `libonnxruntime_providers_cuda.so` links `libcudart.so.12`, `libcufft.so.11`,
    `libcurand.so.10` and `libnvJitLink.so.12` on top of the cuBLAS and cuDNN pair
    the `cuda` extra installs today. Without them the provider fails to load, and
    ONNX Runtime reports that as a warning and runs on the CPU. The extra therefore
    gains those four wheels, and they are preloaded through distribution metadata by
    the same provenance check `stt.py:_load_cuda_runtime` applies.
  - **The TensorRT provider must not be requested.** It heads ONNX Runtime's default
    provider list, fails on a missing `libnvinfer.so.10`, and only then falls back.
    Synthesis asks for `CUDAExecutionProvider` by name.

  Availability is confirmed by reading `session.get_providers()` back off the
  constructed session, never `onnxruntime.get_available_providers()`, which
  advertises CUDA on a session that is running on the CPU.
- **A GPL-3.0 phoneme dependency enters the process.** The synthesis stack imports
  `phonemizer` and loads `espeak-ng`, both GPL-3.0, into an Apache-2.0 project. For
  a source-distributed project this is the user assembling the combination at
  install time, and `espeak-ng` is already a distribution package. It would need a
  decision only if Murmly ever ships a bundled artifact.
- **A session that stops reading stalls its own speech.** Murmly writes events to a
  session that may not be reading them. The write must not block the playback
  thread; a session that will not accept its events is disconnected rather than
  allowed to affect audio, which is the same posture `audio.py:296-298` takes with a
  level sink that raises.
- **Deferring read-aloud leaves the connection-drop policy simple.** Every session in
  this change belongs to a sender that wants to be told what happened, so speech
  stops when the connection ends. Adding read-aloud later reintroduces the case
  where speech should outlive the caller, and that will need an explicit field on
  the session rather than an inference from how the socket closed — a clean close
  and a crash are indistinguishable at the socket layer.

## Migration Plan

Speech output defaults to disabled, so an existing installation behaves exactly as
before until its owner opts in and installs the second hotkey. `murmly install`
gains a second hotkey argument; an installation performed without it binds the
focused-window hotkey alone, and `murmly doctor` reports the session hotkey as not
bound rather than as a failure.

No existing configuration key changes meaning. No existing command changes request
shape, response shape, or wording. Rolling back is removing the `[tts]` table and
the second binding; nothing in this change rewrites state that an older version
would fail to read.
