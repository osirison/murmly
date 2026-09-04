## Why

Murmly speaks whenever a coding agent finishes a turn. That is the whole point of
it during the day, and it is the whole problem of it at night: a session left
running announces itself into a dark room at two in the morning, chime first.

Issue #52 asks for a configured window in which Murmly does not start speaking.
There is no way to express that today. The only control over whether Murmly speaks
at all is `[tts] enabled` (`src/murmly/config.py:250`), and configuration is read
once when the daemon starts (`src/murmly/cli.py:475`) — nothing re-reads it — so
silencing the evening means editing the file, restarting the service, and
remembering to undo both in the morning. A person who forgets the second half
wakes up to a Murmly that has been switched off all day, which is the failure this
change exists to stop them arranging by hand.

The mechanism to hang it on is already there. Every spoken word passes through one
gate: a caller declares a speech session and Murmly accepts or refuses it with a
code (`src/murmly/daemon.py:1862-1889`). The announcement hook plays its chime only
after that declaration is accepted, which the `speech-output` capability already
requires of anything that precedes the words. So a refusal at the gate is already
a silent refusal — no chime, no partial utterance, nothing for the person to be
woken by.

## What Changes

**A speech session declared inside the quiet window is refused, and refused
silently.** The refusal carries its own code, distinct from speech output being
disabled and from it being unavailable, so a caller can tell "not now" from "not
at all" and from "not working". The refusal message names the hour speech resumes,
because a person reading it at 23:40 wants to know when, not that.

That refusal is the entire enforcement. Nothing schedules anything, nothing wakes
up at the boundary, and no timer runs: the window is read off the local wall clock
at the moment a session is declared. Wall clock rather than an offset from start
means the daylight-saving change is handled by the operating system, and a daemon
running across it observes the window the person wrote rather than the one it
computed the previous week.

**The window is one configured value, and its absence is the default.** A new
`[tts] quiet_hours` setting takes a start and an end in 24-hour local time —
`quiet_hours = "22:00-07:00"`. It is empty by default, which is no window, so an
installation that is upgraded and left alone behaves exactly as it did before, on
the same terms as speech output's own opt-in. A start later than its end spans
midnight, which is the shape almost every night has. A value Murmly cannot read
falls back to no window and is reported alongside the value that was rejected,
matching what every other bounded setting in this file already does rather than
refusing to start over a typo in a comfort feature.

**A session already open when the window opens is left to finish.** The ticket
asks that Murmly not *start* uttering, and cutting a sentence in half at 22:00:00
is a worse interruption than the one being prevented. The check is at declaration
and only at declaration.

**`murmly doctor` reports the window and whether it is currently in force.** The
speech output section gains the configured window, any value that was rejected,
and — when a window is configured — whether speech is being refused right now.
Without the last of these, a person whose agent has gone quiet cannot distinguish
quiet hours from a broken synthesizer, which is the diagnosis this report exists
to make.

No delta to `agent-announcements`. Its requirement that no sound precede words
that never arrive — that the undertaking to speak is established before the signal
is produced — already covers a refused declaration, and the announcement hook
already honours it. Quiet hours produce silence through machinery that is
specified and built.

Left out deliberately: per-weekday schedules and more than one window, which the
ticket does not ask for and which turn one string into a calendar; a flag letting a
caller override the window, which would make the setting advisory and is worth
adding only when something actually needs to; interrupting speech already in
progress; and any effect on capture, transcription, delivery, or the overlay —
dictation at night is the person choosing to make a noise, not Murmly choosing for
them.

## Capabilities

### New Capabilities

None. This is a new condition on an existing capability, not a new one.

### Modified Capabilities

- `speech-output`: three requirements change. A new requirement states that a
  speech session declared within the configured quiet window is refused with its
  own code and produces no sound; "Voice and speech settings are configurable and
  bounded" gains the quiet window as a bounded setting that falls back to no window
  rather than refusing to start; and "Diagnostics report speech output
  configuration and availability" gains the window, any rejected value, and whether
  it is in force at the moment the report is taken.

## Impact

| Area | Change |
| --- | --- |
| `src/murmly/config.py` | A `[tts] quiet_hours` string is parsed into a start and end time, or into no window plus the rejected value |
| `src/murmly/daemon.py` | A refusal code joins `CommandCode`; `_declare_session` checks the window after the enabled check and before the runtime probe |
| `src/murmly/cli.py` | `speech_output_diagnostics` reports the window, the rejected value, and whether it is in force |
| `config.example.toml` | The new setting is documented with its format, its default, and what a value Murmly cannot read does |
| `README.md`, `docs/` | The setting is listed where the other speech settings are |
| `tests/` | Parsing, the wrap-around window, the refusal, the absence of a chime, and the report, all against a clock the test supplies |
