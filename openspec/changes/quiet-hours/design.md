## Context

See proposal.md — Why. The constraints that shape the approach are already in the
code:

- Every spoken word passes one gate. `_declare_session`
  (`src/murmly/daemon.py:1862`) accepts or refuses, in a fixed order: speech output
  disabled, then the synthesizer unavailable at startup, then unavailable now, then
  the state lock and `BUSY`, then a session already open, then opening the output
  device. Nothing speaks without getting through it.
- The announcement hook plays its chime only after a successful declaration
  (`hooks/murmly-announce.py`), and surfaces a refusal as `refused: <code>` in its
  own diagnostic line. So a new refusal code is silent and legible for free.
- Configuration is read once, at start (`src/murmly/cli.py:475`), into a frozen-ish
  `MurmlyConfig`. There is no reload. A quiet window that were resolved at load
  time would therefore be resolved once for the life of the daemon.
- Settings that cannot be read fall back and record what was rejected —
  `_bounded_int`, `_idle_period`, `_rejected_value`, and the `*_rejected_value`
  fields on `MurmlyConfig`. `murmly doctor` prints the rejected value beside the
  one in use.
- The daemon already takes an injected clock (`src/murmly/daemon.py:527`), but it
  is `time.monotonic` for the idle tick. A window in local time needs a different
  one.

## Goals / Non-Goals

**Goals:**

- One place decides whether it is quiet, and it is a pure function of two times and
  a third — trivially testable at any hour without waiting for one.
- The check costs a clock read. No timer, no scheduled wake-up, no state that has
  to be kept correct across suspend, resume, or a daylight-saving change.
- A misread setting cannot stop the daemon starting, and cannot silence it.

**Non-Goals:**

- No configuration reload. This change does not make the window editable without a
  restart, because nothing else in Murmly is, and adding reload for one setting
  would leave a config file where one line behaves unlike every other.
- No interruption of speech in progress, no per-weekday schedules, no override
  flag. See proposal.md.
- No timezone setting. The machine's local time is what the person means by "night";
  a Murmly-specific timezone would be one more thing to get wrong on a laptop that
  travels.

## Decisions

### The window is one string, not two settings

`[tts] quiet_hours = "22:00-07:00"`. Empty or absent means no window.

The alternative was `quiet_start` and `quiet_end` as separate settings. Rejected
because a window is one thing and two settings can be half-written: a start with no
end has no honest reading — silence forever, silence never, or a refusal to start,
and each is a bad answer to a typo. One string is either a window or it is not.

Accepted form is `HH:MM-HH:MM` in 24-hour local time, with optional surrounding
whitespace. Seconds are not accepted: nobody sets a bedtime to the second, and
accepting them widens the parser for no gain.

**The value must be quoted in TOML.** `quiet_hours = 22:00-07:00` unquoted is not
valid TOML, and a TOML file that does not parse raises out of `load_config` — the
whole file is lost, not one setting. That hazard is pre-existing and general, but
this is the first setting whose natural form looks like TOML's own local-time
literal, so the example config must show the quotes and say why.

### It is read at declaration, off the local wall clock

`is_quiet_at(start, end, now)` is a pure function called from `_declare_session`.
Nothing else keeps state.

The alternative was resolving a "quiet now" flag on a timer that flips at each
boundary. Rejected on three counts: it is state that can be wrong, it needs a
wake-up the daemon does not otherwise take, and it computes boundaries in advance —
which is exactly what gets a daylight-saving change wrong. Reading `datetime.now()`
at the moment of the question has none of those problems and costs nothing on a
path that is about to open an audio device.

Half-open interval: quiet begins at the start time and ends at the end time, so
`"22:00-07:00"` refuses at 22:00:00 and accepts at 07:00:00. When start is later
than end the window spans midnight, which is `now >= start or now < end`; otherwise
it is `start <= now < end`. Start equal to end yields no window rather than a
24-hour one — someone who wants Murmly permanently silent has `enabled = false`,
and reading an equal pair as "always" would turn a plausible typo into a daemon
that never speaks again.

### The check goes after the enabled check and before the runtime probe

Order in `_declare_session` becomes: disabled → **quiet** → unavailable at startup →
unavailable now → `BUSY` → session in use → open the device.

Before the probes because `unavailable_reason_now()` touches the filesystem and the
espeak-ng library, and a refusal that was decided by a clock should not pay for
that. Before `BUSY` because at 02:00 the answer is quiet hours whether or not the
daemon happens to be recording, and reporting `BUSY` there would send a caller
looking for a conflict that is not the reason. After the disabled check because
disabled is the more fundamental answer: a person who has not enabled speech at all
does not need to be told about a window.

Refusal message names the resume time — "Quiet hours until 07:00." — because a
person reading it at 23:40 wants to know when, not that.

### The refusal gets its own code

`CommandCode.SPEECH_QUIET_HOURS = "speech_quiet_hours"`, alongside
`SPEECH_DISABLED` and `SPEECH_UNAVAILABLE`. A caller that retries in the morning is
right to; one that retries against `SPEECH_DISABLED` is not. The announcement hook
needs no change to benefit — it already prints whatever code it was refused with.

### Parsing falls back to no window, and says what it rejected

A new `_quiet_window(value)` returns `(start, end, rejected)`. It does not reuse
`_rejected_value`, which compares integers. `MurmlyConfig` gains
`tts_quiet_start: time | None`, `tts_quiet_end: time | None`, and
`tts_quiet_rejected_value: str | None`.

Falling back to *no* window rather than to some default window is the point: a
person who believes they will not be disturbed and is, has a bug they can see; one
who is silenced at hours they never wrote has a bug they cannot.

An equal start and end yields no window, and is returned as a value that was not
honoured even though it parsed. `_rejected_value`'s own definition in this file is
"the configured value when it was not the one used", which is exactly what
`22:00-22:00` is. The alternative — reporting no window and nothing else — leaves a
person whose config plainly sets a window looking at a report that says none is
set, with nothing to explain the difference.

### Diagnostics report the window, the rejection, and whether it is in force

`speech_output_diagnostics` gains `quiet_hours` (the configured string, or null),
`quiet_hours_in_force` (bool), and `quiet_hours_rejected_value` when there is one.
All three are carried through the early returns for disabled and unavailable, for
the reason the file already gives about `unload_after_idle_s`: a reader who cannot
see the value cannot tell a setting that is off from one this report did not
mention.

`quiet_hours_in_force` is what makes the report diagnostic rather than decorative.
Without it, a person whose agent went quiet cannot tell a working window from a
broken synthesizer.

### The clock is injectable in both places

`_declare_session` and `speech_output_diagnostics` take `now: Callable[[], datetime]`
defaulting to `datetime.now`. Tests then assert the 22:00 case, the 07:00 boundary,
and the wrap-around at 03:00 without waiting for any of them, and without the suite
passing or failing depending on when it is run.

## Risks / Trade-offs

**A person sets a window, forgets, and thinks Murmly is broken.** → The refusal
message names the resume hour, the hook prints `refused: speech_quiet_hours`, and
`murmly doctor` states the window and whether it is in force right now. Three
places, each reached by a different kind of person looking.

**The window cannot be changed without restarting the daemon.** → True, and true of
every other setting. Accepted rather than fixed here; a reload mechanism is its own
change, and it should arrive for the whole config file rather than for one line of it.

**A machine whose clock is wrong observes the wrong window.** → Not mitigated, and
not worth mitigating. A machine with a wrong clock has larger problems, and the
alternative — an offset from daemon start — would be wrong on every machine rather
than on a broken one.

**Speech that begins at 21:59:58 is still speaking at 22:00.** → Deliberate. The
ticket asks that Murmly not start uttering; cutting a sentence in half at the stroke
of the hour is a worse interruption than the one being prevented. An announcement is
seconds long, so the overrun is bounded by how long one sentence takes to say.
