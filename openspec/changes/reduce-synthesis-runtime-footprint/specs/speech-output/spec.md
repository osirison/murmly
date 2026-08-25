## ADDED Requirements

### Requirement: Synthesis runs on a configurable processor of its own

Murmly SHALL let the processor speech synthesis runs on be configured
independently of the one transcription uses, and SHALL default it to the CPU.
A configured processor that cannot be used MUST cause a fall back to the CPU with
a reported reason, never a refusal to speak and never a silent substitution.

Synthesis and transcription have opposite needs here. Transcription is a burst the
person is waiting on with nothing to overlap it. Synthesis is produced a sentence
at a time ahead of playback, so all that a slower processor costs is the wait
before the first word — every later sentence is finished before the audio ahead of
it has played out.

#### Scenario: Synthesis runs on the CPU by default

- **GIVEN** speech output is enabled and no synthesis processor is configured
- **WHEN** a speech session speaks
- **THEN** synthesis runs on the CPU
- **AND** it does so whether or not transcription is using an accelerator

#### Scenario: The accelerator can be asked for explicitly

- **GIVEN** speech output is enabled and the synthesis processor is configured as the accelerator
- **AND** that accelerator is usable
- **WHEN** a speech session speaks
- **THEN** synthesis runs on the accelerator

#### Scenario: An unusable accelerator falls back rather than refusing

- **GIVEN** the synthesis processor is configured as the accelerator
- **WHEN** that accelerator cannot be used
- **THEN** synthesis runs on the CPU and speaks the text
- **AND** Murmly reports which processor was asked for, which is in use, and the remedy

#### Scenario: Transcription's processor does not decide synthesis

- **GIVEN** transcription is configured to use the accelerator
- **AND** no synthesis processor is configured
- **WHEN** a speech session speaks
- **THEN** synthesis runs on the CPU
- **AND** transcription continues to use the accelerator

### Requirement: Default synthesis holds no accelerator memory

Under the default configuration, speaking SHALL NOT cause Murmly to hold
accelerator memory for synthesis, at any point during or after a speech session.
Accelerator memory taken for synthesis is not returned when a synthesis session
ends, so the guarantee here is that it is never taken, not that it is released
promptly.

#### Scenario: Speaking leaves the accelerator untouched

- **GIVEN** speech output is enabled with no synthesis processor configured
- **WHEN** a speech session speaks and completes
- **THEN** no accelerator memory is attributed to synthesis at any point
- **AND** the accelerator memory available to other processes is unchanged from before the session

#### Scenario: Transcription's accelerator memory is unaffected

- **GIVEN** the transcription model is resident on the accelerator
- **WHEN** a speech session speaks and completes
- **THEN** the transcription model remains resident and usable

### Requirement: The synthesis working set does not grow with utterances spoken

The system memory synthesis occupies SHALL reach a steady state and stay there
across a long-running daemon, rather than growing with the number of utterances
spoken. A daemon that has spoken many times MUST NOT hold materially more memory
for synthesis than one that has spoken once.

#### Scenario: Repeated speech reaches a steady state

- **GIVEN** speech output is enabled
- **WHEN** many utterances are spoken over the life of one daemon
- **THEN** the memory held for synthesis settles rather than rising with each utterance

## MODIFIED Requirements

### Requirement: Voice and speech settings are configurable and bounded

Murmly SHALL let the voice, the speaking rate, the output device, and the
processor synthesis runs on be configured, MUST state a default for each, and MUST
fall back to that default when a configured value is unrecognized or outside the
supported range rather than refusing to start. A misconfigured speech setting is
not a reason to leave the person without a working daemon, and none of these
settings may prevent transcription from working.

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

### Requirement: Diagnostics report speech output configuration and availability

`murmly doctor` SHALL report whether speech output is enabled, whether it can run,
the voice and rate in use alongside any configured values that were not honoured,
the output device it would use, and the processor synthesis will run on alongside
any configured processor that was not honoured, in addition to its existing
sections. When speech output cannot run, the report MUST name the remedy.
Reporting the processor MUST NOT itself construct a synthesis session.

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
