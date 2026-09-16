## MODIFIED Requirements

### Requirement: Diagnostics report speech output configuration and availability

`murmly doctor` SHALL report whether speech output is enabled, whether it can run,
the voice and rate in use alongside any configured values that were not honoured,
the output device it would use, the size of the output buffer that was negotiated,
the processor synthesis will run on alongside any configured processor that was not
honoured, and the quiet window in use alongside any configured window that was not
honoured, in addition to its existing sections. When speech output cannot run, the
report MUST name the remedy. Reporting the processor MUST NOT itself construct a
synthesis session.

The report SHALL also state, as two separate counts, how many playback dropouts
have been recorded since the daemon started: periods the output device reported
it could not be fed in time, and periods filled with silence because a producer
had not yet supplied the sound they needed. A person who hears stuttering speech
otherwise has nothing to look at, and the report arrives as a description of a
sound rather than a count that identifies where the fault is. Conflating the two
counts would point that person at whichever half of the pipeline they happened to
guess. A count of zero MUST be reported as such for either count rather than
omitted, because "no dropouts" and "not measured" send an investigation to
different places.

The second of these counts SHALL count only silence that falls between two sounds
the same piece of text produced. Silence before a piece of text has produced any
sound at all, silence after its last sound, and silence between two separate
pieces of text are not the fault of whatever is producing sound for the piece now
being spoken, and MUST NOT be added to its count — including when the piece
spoken immediately before it is still leaving the device at the moment the new
piece begins. Two pieces of text following one another without a pause between
them are routine, not a fault, and the count MUST NOT depend on how closely one
piece's own trailing sound happens to overlap the next piece beginning.

When a quiet window is in use, the report SHALL also state whether it is in force
at the moment the report is taken. A person whose agent has gone quiet needs to
tell a window that is doing its job from a synthesizer that has stopped working,
and the configured window alone does not tell them which they are looking at.

#### Scenario: Speech output enabled and working

- **WHEN** diagnostics run with speech output enabled and able to run
- **THEN** the report states that speech output is available and names the voice, rate, output device, output buffer, and processor in use

#### Scenario: Dropouts are reported whether or not there were any

- **WHEN** diagnostics run with speech output enabled
- **THEN** the report states both counts of playback dropouts recorded since the daemon started
- **AND** states each as a number when there have been none, rather than omitting the field

#### Scenario: One piece's trailing sound overlapping the next piece's start is not counted against it

- **WHEN** one piece of text finishes speaking and the next piece begins before the first piece's own trailing sound has finished leaving the device
- **THEN** the ordinary delay before the new piece's first sound is produced does not raise the count of periods a producer failed to keep up
- **AND** the trailing sound of the piece that just finished is not counted either

#### Scenario: A silence inside a piece's own playback is still counted

- **WHEN** a piece of text has already produced sound and falls silent again before producing more
- **THEN** the silence is counted against the count of periods a producer failed to keep up
- **AND** this holds whether or not the piece spoken immediately before it also ended with sound still leaving the device

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
