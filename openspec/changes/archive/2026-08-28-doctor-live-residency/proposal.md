## Why

`murmly doctor` reports whether each model is resident, and the answer is a
constant. It cannot be anything else: doctor runs in its own process, and the
models it reports on live in the daemon's.

Both fields are structurally false. `_run_doctor` holds no transcriber, so
`transcription_residency()` takes its `transcriber is None` branch and returns
`False` every time. The speech section reads `resident` off a `KokoroSynthesizer`
that `speech_output_diagnostics` constructs fresh for the probe, whose `_model` is
None because the probe deliberately never builds a session. No daemon state, and
no configuration, can make either field say anything else.

The `model-residency` capability requires diagnostics to report "whether it is
currently resident". A value that is always `false` does not report that; it
reports a constant that happens to be right on a cold machine. The requirement is
met in wording and not in substance, which is worse than an unmet one, because the
report reads as an answer.

This matters more now that release is on by default. Idle release is the one
feature whose whole observable behaviour is a model going away and coming back,
and the diagnostic built to observe it cannot see it. Someone checking whether
their transcription model was released after five minutes gets `false` — the same
answer they would get if it were resident.

## What Changes

- `murmly doctor` asks the running daemon what it currently holds, over the
  command socket, and reports that.
- The report distinguishes three states that are currently collapsed into `false`:
  the daemon holds the model, the daemon does not hold it, and no daemon is
  running to ask. The third is labelled as such rather than reported as "not
  resident".
- A daemon too old to answer, or one that fails to, degrades to the current
  behaviour with the reason named. Diagnostics never fail because a probe did.
- **The `Residency reported without loading` scenario is tightened**, not
  weakened. Its requirement says reporting residency "MUST NOT **itself** load a
  model"; the scenario said "neither model is loaded as a result", which is
  broader than the requirement it sits under and broader than what `murmly doctor`
  has ever done. With `[stt] live_transcribe = true`, `live_transcription_diagnostics`
  calls `measure_partial_pass_ms`, which constructs a transcriber and runs two
  passes — its own docstring says "it loads the model". That measurement predates
  this work, is deliberate, and is the only way to tell someone whether live
  partials keep pace on their machine. The scenario is corrected to say what the
  requirement means: reporting residency loads nothing, and a section that must
  load something says so.

### What this is not

Not a new diagnostic. The residency fields already exist and are already
documented; this makes them capable of being true.

Not a change to what the daemon does with its models. Nothing about arming,
cancelling, releasing or reloading moves.

Not a measurement moved behind a flag. Decided with the user: removing the
partial-pass timing from the default report would take away a check that runs
automatically today, to satisfy a scenario that was worded more broadly than its
own requirement.

## Capabilities

### New Capabilities

<!-- None. Reporting residency is already a requirement of `model-residency`;
     what changes is whether the report can answer it. -->

### Modified Capabilities

- `model-residency`: the diagnostics requirement gains the distinction between "no
  daemon to ask" and "not resident", and the obligation to report what the daemon
  holds rather than what the reporting process holds. Its
  `Residency reported without loading` scenario is corrected to match the
  requirement's own "MUST NOT itself load" scoping.

## Impact

- `src/murmly/daemon.py` — the residency of both models added to what the daemon
  can be asked. `handle_command` already answers `status` with the daemon's state;
  this is the same shape of question.
- `src/murmly/cli.py` — `transcription_residency` and the speech section read the
  daemon's answer where there is one. Doctor does not currently connect to the
  socket: `command_socket_diagnostics` inspects the path's permissions without
  opening it. The connecting is `send_command`, which `cli.py` already imports for
  the commands that use it, so the transport exists even though this report has
  never used it.
- No new dependencies. No change to the config file.
- Interacts with nothing in flight. `add-project-website` touches neither file.
