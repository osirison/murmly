## ADDED Requirements

### Requirement: Speech is refused inside a configured quiet window

Murmly SHALL let a quiet window be configured as a start and an end in the
machine's local 24-hour time, and SHALL refuse a speech session declared while the
current local time falls inside that window. The refusal MUST carry a code of its
own, distinct from the one for speech output being disabled and from the one for it
being unavailable, because a caller that is told "not now" may sensibly try again
in the morning while one told "not at all" or "not working" may not.

The refusal MUST produce no sound of any kind, including any signal that would
otherwise precede the words. A window whose purpose is that a person is asleep is
not served by a chime announcing that they were not spoken to.

The window MUST be evaluated against local time at the moment a session is
declared, and MUST NOT be resolved once at startup. A daemon started before the
window begins therefore refuses inside it without being restarted, and a daemon
running across a daylight-saving change observes the window as the person wrote it.

No window SHALL be configured by default. An installation that is upgraded and
whose configuration is unchanged MUST accept and refuse speech sessions exactly as
it did before quiet windows existed.

#### Scenario: A session declared inside the window

- **WHEN** speech output is enabled and available, a quiet window is configured, and
  a caller declares a speech session at a local time inside it
- **THEN** Murmly refuses the session, naming the time speech resumes
- **AND** no audio device is opened and no sound of any kind is produced

#### Scenario: A session declared outside the window

- **WHEN** a quiet window is configured and a caller declares a speech session at a
  local time outside it
- **THEN** the session is accepted and text sent on it is spoken

#### Scenario: The refusal is distinguishable from the other refusals

- **WHEN** a session is refused because the current local time is inside the quiet
  window
- **THEN** the code identifies the quiet window as the reason
- **AND** it is not the code used when speech output is disabled, nor the one used
  when speech output is unavailable

#### Scenario: A window that spans midnight

- **GIVEN** a quiet window whose start is later in the day than its end
- **WHEN** a session is declared at a local time at or after the start, or before
  the end
- **THEN** the session is refused
- **AND** a session declared at any other local time is accepted

#### Scenario: No window configured

- **WHEN** no quiet window is configured and a caller declares a speech session
- **THEN** the declaration is decided exactly as it was before quiet windows existed
- **AND** the outcome does not depend on the time of day

#### Scenario: A window whose start equals its end

- **GIVEN** a quiet window whose start and end are the same time
- **WHEN** a caller declares a speech session
- **THEN** the session is decided as though no window were configured
- **AND** speech is not refused at any hour

#### Scenario: A session open when the window begins

- **GIVEN** a speech session accepted before the quiet window began
- **WHEN** local time passes the start of the window while that session is speaking
- **THEN** the speech in progress is not stopped
- **AND** text the session sends on that connection is still spoken

#### Scenario: The daemon is not restarted at the boundary

- **GIVEN** a daemon that has been running since before the quiet window began
- **WHEN** a session is declared inside the window
- **THEN** the session is refused
- **AND** the refusal does not depend on the daemon having been restarted

#### Scenario: Capture is unaffected

- **WHEN** a capture hotkey is pressed at a local time inside the quiet window
- **THEN** capture, transcription, and delivery work exactly as they do outside it
- **AND** the quiet window silences only what Murmly would have said

## MODIFIED Requirements

### Requirement: Voice and speech settings are configurable and bounded

Murmly SHALL let the voice, the speaking rate, the output device, the processor
synthesis runs on, and the quiet window be configured, MUST state a default for
each, and MUST fall back to that default when a configured value is unrecognized or
outside the supported range rather than refusing to start. A misconfigured speech
setting is not a reason to leave the person without a working daemon, and none of
these settings may prevent transcription from working.

The default the quiet window falls back to is no window. A window Murmly cannot
read is a person who believes they will not be disturbed, so falling back to some
other window would be worse than falling back to none: it would silence Murmly at
hours nobody asked for, and the person would have no way to tell that from the
window they wrote.

#### Scenario: Unrecognized voice

- **WHEN** the configured voice is not one Murmly can produce
- **THEN** Murmly uses the default voice
- **AND** diagnostics report the configured value and the one in use

#### Scenario: Rate outside the supported bounds

- **WHEN** the configured speaking rate is outside the supported range
- **THEN** Murmly uses the default rate

#### Scenario: Configured output device unavailable

- **WHEN** the configured output device cannot be opened
- **THEN** Murmly reports which device it could not open and what it used instead

#### Scenario: Unrecognized synthesis processor

- **WHEN** the configured synthesis processor is not one Murmly recognizes
- **THEN** Murmly uses the default processor and starts normally
- **AND** diagnostics report the configured value and the one in use

#### Scenario: A quiet window Murmly cannot read

- **WHEN** the configured quiet window is not a start and an end Murmly can read, or
  either of them is not a valid time of day
- **THEN** Murmly starts with no quiet window and refuses no session for the time of
  day
- **AND** diagnostics report the configured value that was not honoured

### Requirement: Diagnostics report speech output configuration and availability

`murmly doctor` SHALL report whether speech output is enabled, whether it can run,
the voice and rate in use alongside any configured values that were not honoured,
the output device it would use, the processor synthesis will run on alongside any
configured processor that was not honoured, and the quiet window in use alongside
any configured window that was not honoured, in addition to its existing sections.
When speech output cannot run, the report MUST name the remedy. Reporting the
processor MUST NOT itself construct a synthesis session.

When a quiet window is in use, the report SHALL also state whether it is in force
at the moment the report is taken. A person whose agent has gone quiet needs to
tell a window that is doing its job from a synthesizer that has stopped working,
and the configured window alone does not tell them which they are looking at.

#### Scenario: Speech output enabled and working

- **WHEN** diagnostics run with speech output enabled and able to run
- **THEN** the report states that speech output is available and names the voice, rate, output device, and processor in use

#### Scenario: Speech output disabled

- **WHEN** diagnostics run with speech output disabled
- **THEN** the report states that speech output is disabled

#### Scenario: Speech output enabled but unable to run

- **WHEN** diagnostics run with speech output enabled and its runtime or model files absent
- **THEN** the report states that speech output is unavailable
- **AND** names what to install or place to make it available

#### Scenario: Accelerator asked for but not available

- **WHEN** diagnostics run with the synthesis processor configured as the accelerator and that accelerator unusable
- **THEN** the report names the accelerator as configured, the CPU as in use, and the remedy

#### Scenario: One probe failing does not abandon the report

- **WHEN** the speech output probe fails unexpectedly
- **THEN** the report states that the speech section could not be determined
- **AND** every other section is still reported

#### Scenario: A quiet window is configured and currently in force

- **WHEN** diagnostics run at a local time inside the configured quiet window
- **THEN** the report names the window
- **AND** states that speech is being refused for it at this moment

#### Scenario: A quiet window is configured and not in force

- **WHEN** diagnostics run at a local time outside the configured quiet window
- **THEN** the report names the window
- **AND** states that it is not in force at this moment

#### Scenario: No quiet window is configured

- **WHEN** diagnostics run with no quiet window configured
- **THEN** the report states that no quiet window is set
- **AND** does not report speech as being refused for the time of day
