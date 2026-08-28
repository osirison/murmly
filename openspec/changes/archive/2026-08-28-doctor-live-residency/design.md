## Context

See `proposal.md` — Why. The short version: the two residency fields in `murmly
doctor` are structurally constant, because doctor and the daemon are different
processes and the report only ever asked itself.

Two facts shape everything below.

The daemon already answers a question over the command socket. `handle_command`
returns `{"ok": True, "state": self.state}` for `status`, and `send_command` is
already imported by `cli.py` for the commands that use it. So the transport is not
new; only this report's use of it is.

Both models already expose exactly the property needed. `FasterWhisperTranscriber.resident`
and `KokoroSynthesizer.resident` were added by `unload-idle-gpu-models` and are
specified never to load anything. The daemon holds both. Nothing needs to be
computed — it needs to be carried across a process boundary.

## Goals / Non-Goals

**Goals:**

- Make the residency fields capable of being true.
- Distinguish "not resident" from "no daemon answered", which the current report
  collapses.
- Keep the report complete when the daemon cannot be reached or cannot answer.

**Non-Goals:**

- Changing when models are released or reloaded. This change only observes.
- Reporting residency for a daemon on another machine, or for more than one
  daemon. There is one socket and one daemon.
- Moving the partial-pass measurement out of the default report. Decided with the
  user; see the scenario decision below.
- A general daemon-introspection command. The question is residency, not "tell
  doctor everything".

## Decisions

### Extend the `status` response rather than adding a command

`status` already means "what is the daemon doing right now", which is the question
being asked. Adding fields to its response is compatible in both directions: a
reader that only looks at `state` is unaffected, and a doctor talking to a daemon
too old to include the fields sees them absent, which is exactly the "could not be
determined" case it must already handle for an unreachable daemon.

*Alternative considered:* a new `residency` command. Rejected because it needs the
same absent-answer handling — an old daemon returns `UNSUPPORTED_COMMAND` — while
also adding a protocol verb and a second round trip. The fallback path is the
work, and a new command does not avoid it.

*Alternative considered:* reading residency out of a status file the daemon
writes. Rejected: a file outlives the process that wrote it, so a daemon killed
mid-session would leave a report claiming a model is resident in a process that no
longer exists. The socket cannot lie that way — if nobody answers, nobody is
there.

### Three states, reported the way this report already reports uncertainty

`cli.py` has an established shape for "could not be determined": the value is
`null` and a sibling `*_detail` key says why. `providers` / `provider_detail` and
`model_resident` / `model_resident_detail` both already do this.

So residency becomes `true`, `false`, or `null` with a detail naming the reason —
no daemon answered, the daemon is too old to say, or the query failed. The
requirement's distinction falls out of the convention already in the file rather
than needing a new one.

### The daemon answers from the properties, not from the timers

Residency is read off `FasterWhisperTranscriber.resident` and
`KokoroSynthesizer.resident`, not inferred from whether an idle countdown is
armed. A countdown that has fired says a release was attempted, not that it
succeeded — a runtime whose CTranslate2 build cannot be asked reports resident and
releases nothing. The question is what is held, and only the model holders know.

Reading them is safe from the command thread: both are specified not to load and
not to block. `resident` on the transcriber deliberately skips `_model_lock` so it
cannot park behind a decode, and on the synthesizer it is a field read.

### The measurement stays; the scenario is corrected

Decided with the user. The requirement says reporting residency "MUST NOT
**itself** load a model". Its scenario said "neither model is loaded as a result",
which is broader than the requirement it sits under, and broader than `murmly
doctor` has ever behaved: with `[stt] live_transcribe = true`,
`measure_partial_pass_ms` constructs a transcriber and runs two passes, and says
so in its own docstring.

That measurement is the only thing that tells someone whether live partials keep
pace on their machine, and it predates this work. The scenario is corrected to
say what the requirement means, and a second scenario is added requiring a section
that loads a model to declare it. That is a tightening: the report must now
disclose the load, which it does not do today.

*Alternative considered:* putting the measurement behind `--measure`. Rejected
with the user — it removes a check that runs automatically today, on upgrade,
silently, to satisfy wording rather than a behaviour anyone wanted.

### Residency is read before any section that loads

Already true and now load-bearing, so it is stated rather than left incidental.
`_run_doctor` reads residency before assembling the report, ahead of
`live_transcription_diagnostics`. Reading it afterwards would report the report's
own side effect — "resident: true" caused by the measurement two lines above —
which is the least useful true answer available.

## Risks / Trade-offs

- **Doctor gains a dependency on a running daemon for two fields** → It already
  degrades every probe it cannot complete, and this follows the same shape. The
  fields become `null` with a reason rather than absent or wrong. Someone running
  doctor precisely because the daemon will not start gets a clearer report than
  today's, which currently says "not resident" as though it had checked.
- **A stale or wedged daemon could answer slowly** → `send_command` is the same
  path every other command uses and carries its existing timeout. A daemon that
  does not answer in time is the "could not be determined" case, not a hang. The
  timeout must not be extended for this: doctor is the command people run when
  things are wrong.
- **The report's meaning changes on upgrade for anyone parsing it** → `model_resident`
  and the speech section's `resident` can now be `true` or `null`, where they were
  always `false`. Anything treating `false` as "the daemon holds nothing" was
  already wrong; anything treating the field as a constant was reading a bug. The
  keys and their types are otherwise unchanged.
- **Two round trips if the socket probe and the residency query stay separate** →
  Acceptable. `command_socket_diagnostics` inspects the path's permissions and
  never connects, so there is no existing connection to share, and one short
  connection is what every other command already costs.

## Migration Plan

No migration. The fields already exist and keep their names and types; they gain
the ability to be `true` or `null`. No configuration changes and nothing to
restart beyond the daemon picking up the new command response, which it does by
being the new version.

A new doctor against an old daemon reports residency as `null` with the reason,
which is correct rather than degraded — that daemon genuinely cannot say.

## Open Questions

None.
