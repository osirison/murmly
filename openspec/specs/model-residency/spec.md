# Model Residency Specification

## Purpose

Defines when Murmly's transcription model and synthesis session occupy accelerator
memory, when that memory is released back to the system, what releasing it must
never disturb, and how a person configures and inspects that behaviour.

## Requirements

### Requirement: Idle models release their accelerator memory

Murmly SHALL release the accelerator memory held by the transcription model, and
the memory held by the synthesis session, after each has been unused for its own
configured idle period. Releasing MUST return the memory to the system rather than
to an internal pool, so that another process can allocate it.

Each is governed independently: the transcription model and the synthesis session
have separate idle periods and are released separately. The memory each reclaims
and the time each costs to restore differ by roughly a factor of four in opposite
directions, so one shared period cannot serve both.

Murmly MUST NOT release a model that is in use. A release MUST NOT interrupt a
transcription pass, a synthesis in progress, or playback.

#### Scenario: Transcription model released after its idle period

- **GIVEN** the transcription model is resident and its idle period is configured
- **WHEN** no capture has been active for longer than that period
- **THEN** Murmly releases the accelerator memory the model held
- **AND** the memory is observable as free to other processes

#### Scenario: Synthesis session released on its own period

- **GIVEN** both models are resident and each has a different idle period
- **WHEN** only the synthesis period has elapsed
- **THEN** Murmly releases the synthesis session
- **AND** the transcription model remains resident

#### Scenario: A pass in progress is never interrupted

- **GIVEN** an idle period has elapsed
- **WHEN** a transcription pass or a synthesis is still running
- **THEN** Murmly does not release that model until the work completes

### Requirement: Idle means no capture is active

For the transcription model, the idle period SHALL be measured from the end of a
recording session, and MUST be reset whenever capture begins. A session that is
still capturing is never idle, however long it has been since the last transcript.

This makes a continuous auto-transcribe session, which closes segments on silence
and keeps recording, immune to release for as long as it runs.

#### Scenario: Silence between segments does not count as idle

- **GIVEN** a continuous auto-transcribe session is running
- **AND** the configured idle period is shorter than the pause between utterances
- **WHEN** the user pauses for longer than that period and then speaks again
- **THEN** Murmly does not release the transcription model at any point during the session

#### Scenario: The clock restarts when capture begins

- **GIVEN** a recording session has ended and the idle period is counting down
- **WHEN** the user starts a new recording before the period elapses
- **THEN** the countdown is abandoned
- **AND** it begins again only when that new session ends

### Requirement: A released model reloads on next use without user action

A model whose memory has been released SHALL be reloaded automatically when it is
next needed. A release MUST NOT be observable as a failure, a refusal, or a lost
transcript: the only difference a person can detect is the time the first use
after a release takes.

Murmly SHALL begin reloading the transcription model when capture starts, rather
than waiting until a transcription pass needs it, so that the reload overlaps with
the user speaking instead of adding to the wait after they stop.

#### Scenario: Dictation after a release succeeds

- **GIVEN** the transcription model's memory has been released
- **WHEN** the user records and stops
- **THEN** Murmly reloads the model, transcribes the complete captured audio, and delivers the transcript
- **AND** the transcript is the same as it would have been without the release

#### Scenario: Reload overlaps with capture

- **GIVEN** the transcription model's memory has been released
- **WHEN** capture begins
- **THEN** Murmly starts reloading the model while capture continues

#### Scenario: Speech after a release succeeds

- **GIVEN** the synthesis session has been released
- **WHEN** a speech session sends text
- **THEN** Murmly rebuilds the session and speaks the text

### Requirement: Releasing never delays capture or delivery

Releasing or reloading a model SHALL NOT delay the start of microphone capture,
extend the interval between capture stopping and delivery beyond the reload itself,
or cause a recording to be lost. A reload that fails MUST be reported as a
transcription or synthesis failure under the existing rules for those, and MUST NOT
leave Murmly unable to serve later requests.

This holds the same line as the existing live-transcription requirement that live
work yields to capture and delivery.

#### Scenario: Capture starts immediately even when a reload is running

- **WHEN** capture begins while a reload is still in progress
- **THEN** capture starts without waiting for the reload to finish

#### Scenario: A failed reload does not disable Murmly

- **GIVEN** a reload fails
- **WHEN** the user records again
- **THEN** Murmly attempts the load again rather than refusing permanently

### Requirement: Idle release is configurable, bounded, and defaulted per model

Murmly SHALL provide a separate configuration setting for the transcription idle
period and the synthesis idle period, each with its own default. A value of zero
SHALL disable idle release for that model, leaving it resident once loaded. An
absent setting SHALL take that setting's default. A value outside the supported
bounds MUST fall back to that setting's default rather than being applied or
refused.

The two defaults differ, because the two releases are not alike. Transcription
release SHALL be enabled by default: it returns accelerator memory, and its reload
is already started when capture begins, so the wait is absorbed while the person
is still speaking. Synthesis release SHALL be disabled by default: it returns
system memory rather than accelerator memory, and what it costs is silence before
speech resumes, which has nothing to overlap it.

#### Scenario: Transcription release is enabled without configuration

- **GIVEN** no transcription idle period is configured
- **WHEN** the transcription model is resident and no capture has been active for longer than its default period
- **THEN** Murmly releases the accelerator memory the model held

#### Scenario: Synthesis release is disabled without configuration

- **GIVEN** no synthesis idle period is configured
- **WHEN** the synthesis session is resident and Murmly is left idle indefinitely
- **THEN** the session stays resident and its memory is not released

#### Scenario: Zero disables release for that model

- **GIVEN** an idle period is configured as zero
- **WHEN** that model is loaded and Murmly is left idle indefinitely
- **THEN** the model stays resident and its memory is not released

#### Scenario: Out-of-range value falls back

- **WHEN** an idle period is configured outside the supported bounds
- **THEN** Murmly uses the default for that setting and starts normally

### Requirement: Diagnostics report residency and configuration

Murmly's diagnostics SHALL report, for the transcription model and for the
synthesis session, whether it is currently resident and what idle period is in
effect. Residency SHALL be reported for the models the running daemon holds, not
for the process producing the report, because those are different processes and
only the daemon's answer describes the system.

When no running daemon can be asked, diagnostics MUST say so rather than report
the models as not resident. "Nothing is holding a model" and "nobody was there to
ask" are different facts, and reporting the second as the first makes the report
wrong exactly when a person is checking whether their daemon is running.

Reporting residency MUST NOT itself load a model that is not loaded. Diagnostics
MAY include other sections that load a model, and any such section MUST state
that it does.

A daemon that cannot answer, or that answers unusably, MUST NOT prevent the rest
of the report. The reason MUST be named alongside the affected fields.

#### Scenario: Residency reported for the daemon's models

- **GIVEN** a daemon is running and holds the transcription model
- **WHEN** the user runs diagnostics
- **THEN** the report says the transcription model is resident
- **AND** it does so whether or not the process producing the report holds anything

#### Scenario: A released model is reported as released

- **GIVEN** a daemon is running and its transcription model has been released after its idle period
- **WHEN** the user runs diagnostics
- **THEN** the report says the transcription model is not resident
- **AND** names the configured idle period

#### Scenario: No daemon to ask is not the same as not resident

- **GIVEN** no daemon is running
- **WHEN** the user runs diagnostics
- **THEN** the report states that residency could not be determined because no daemon answered
- **AND** it does not report either model as resident or as not resident

#### Scenario: Residency reported without loading

- **GIVEN** no model has been loaded since the daemon started
- **WHEN** the user runs diagnostics
- **THEN** the output reports both as not resident and names each configured idle period
- **AND** reporting residency loads neither model

#### Scenario: A section that loads a model says so

- **GIVEN** a diagnostics section measures something that requires a loaded model
- **WHEN** that section runs
- **THEN** the report states that the measurement loaded a model
- **AND** the residency it reports is what was held before that section ran

#### Scenario: A daemon that cannot be asked does not abandon the report

- **GIVEN** a daemon is running but does not answer the residency question
- **WHEN** the user runs diagnostics
- **THEN** the report names why residency could not be determined
- **AND** every other section is still reported
