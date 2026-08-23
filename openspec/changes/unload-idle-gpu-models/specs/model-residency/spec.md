## Purpose

Defines when Murmly's transcription model and synthesis session occupy accelerator
memory, when that memory is released back to the system, what releasing it must
never disturb, and how a person configures and inspects that behaviour.

## ADDED Requirements

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

### Requirement: Idle release is configurable and bounded

Murmly SHALL provide a separate configuration setting for the transcription idle
period and the synthesis idle period. A value of zero, or an absent setting, SHALL
disable idle release for that model, leaving it resident once loaded. A value
outside the supported bounds MUST fall back to the default rather than being
applied or refused.

#### Scenario: Disabled by default keeps a model resident

- **GIVEN** neither idle period is configured
- **WHEN** a model is loaded and Murmly is left idle indefinitely
- **THEN** the model stays resident and its memory is not released

#### Scenario: Out-of-range value falls back

- **WHEN** an idle period is configured outside the supported bounds
- **THEN** Murmly uses the default for that setting and starts normally

### Requirement: Diagnostics report residency and configuration

Murmly's diagnostics SHALL report, for the transcription model and for the
synthesis session, whether it is currently resident and what idle period is in
effect. Reporting residency MUST NOT itself load a model that is not loaded.

#### Scenario: Residency reported without loading

- **GIVEN** no model has been loaded since the daemon started
- **WHEN** the user runs diagnostics
- **THEN** the output reports both as not resident and names each configured idle period
- **AND** neither model is loaded as a result
