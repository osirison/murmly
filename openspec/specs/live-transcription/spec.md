# Live Transcription Specification

## Purpose

Defines how Murmly transcribes speech while capture is still running: what the user is shown before they stop speaking, when a run of silence may end a recording or close a segment on the user's behalf, and the guarantees that keep either behavior from degrading the transcript that is finally delivered.

## Requirements

### Requirement: Live transcription is opt-in

Murmly SHALL provide a configuration option that enables live transcription, and that option MUST default to disabled. When live transcription is disabled, Murmly MUST NOT transcribe any audio before capture stops.

#### Scenario: Configuration does not mention live transcription

- **WHEN** capture starts with no live transcription setting in configuration
- **THEN** no transcription runs while Murmly is listening
- **AND** the recording lifecycle is indistinguishable from a session without this capability

#### Scenario: Live transcription enabled

- **WHEN** capture starts with live transcription enabled
- **THEN** Murmly transcribes the audio captured so far on a repeating interval while listening

### Requirement: Partial results never determine the delivered transcript

Partial results produced while listening SHALL be treated as feedback only. Murmly MUST NOT place a partial result on the clipboard, MUST NOT inject a partial result into any application, and MUST NOT assemble the delivered transcript from partial results. The transcript Murmly delivers MUST be produced by a transcription pass over the complete captured audio for that recording or segment.

#### Scenario: Delivered transcript matches a non-live session

- **WHEN** the same audio is captured with live transcription enabled and with it disabled
- **THEN** the delivered transcript is the same in both sessions

#### Scenario: Partial results are discarded when capture stops

- **WHEN** capture stops after any number of partial results were produced
- **THEN** Murmly transcribes the complete captured audio and delivers that result
- **AND** no partial result contributes to the delivered text

#### Scenario: Partial results are not retained

- **WHEN** a partial result is produced
- **THEN** it is not written to the clipboard, to any log entry, or to any file

### Requirement: Live transcription yields to capture and delivery

Live transcription SHALL NOT delay microphone capture, extend the time between capture stopping and delivery, or cause a recording to be lost. When a partial pass cannot keep pace with the configured interval, Murmly MUST skip intervals rather than queue them. When capture stops, Murmly MUST abandon any in-flight partial pass within a bounded interval and proceed with the final transcription.

#### Scenario: Partial pass is slower than the interval

- **WHEN** a partial transcription pass is still running when the next interval elapses
- **THEN** Murmly skips that interval instead of starting a second concurrent pass

#### Scenario: Capture stops during a partial pass

- **WHEN** the user stops capture while a partial pass is running
- **THEN** Murmly abandons the partial result within a bounded interval
- **AND** the final transcription proceeds and its transcript is delivered

#### Scenario: Live transcription fails

- **WHEN** a partial transcription pass raises an error
- **THEN** capture continues uninterrupted
- **AND** Murmly stops producing partial results for the remainder of the session
- **AND** the final transcription and delivery are unaffected

### Requirement: Auto-transcribe mode is configurable

Murmly SHALL provide a configuration option selecting one of three auto-transcribe modes — disabled, stop, or continuous — and that option MUST default to disabled. An unrecognized value MUST fall back to disabled. Auto-transcribe MUST be selectable independently of live transcription.

#### Scenario: Configuration does not mention auto-transcribe

- **WHEN** capture starts with no auto-transcribe setting in configuration
- **THEN** silence never ends the recording
- **AND** the recording ends only on an explicit toggle

#### Scenario: Unrecognized mode configured

- **WHEN** configuration names an auto-transcribe mode Murmly does not recognize
- **THEN** Murmly treats auto-transcribe as disabled
- **AND** Murmly reports the rejected value through diagnostics

#### Scenario: Auto-transcribe without live transcription

- **WHEN** auto-transcribe is enabled while live transcription is disabled
- **THEN** silence triggers the configured mode
- **AND** no partial results are produced

### Requirement: Silence duration is configurable and bounded

Murmly SHALL provide a configurable silence duration that triggers auto-transcribe, defaulting to 2000 milliseconds. The duration MUST be bounded, and a value outside those bounds MUST fall back to the default so that no configured value can leave a recording running indefinitely or end it before the user has paused.

#### Scenario: Custom silence duration

- **WHEN** auto-transcribe is enabled with a valid custom silence duration
- **THEN** Murmly acts on the configured mode after that much continuous silence

#### Scenario: Silence duration outside the supported bounds

- **WHEN** the configured silence duration is below the supported minimum or above the supported maximum
- **THEN** Murmly uses the default duration

### Requirement: Silence only triggers after speech is detected

Murmly SHALL require that speech was detected during the current recording or segment before a run of silence can trigger auto-transcribe. Silence at the start of a recording, before the user has said anything, MUST NOT end that recording.

#### Scenario: User pauses before speaking

- **WHEN** auto-transcribe is enabled and the user starts capture but does not speak
- **THEN** the recording stays in listening regardless of how long the silence lasts

#### Scenario: Silence after speech

- **WHEN** the user speaks and then stops for the configured silence duration
- **THEN** Murmly acts on the configured auto-transcribe mode

### Requirement: Stop mode ends the recording on silence

In stop mode, a qualifying run of silence SHALL end the recording exactly as an explicit toggle does: capture stops, the delivery target is recorded before transcription begins, the transcript is produced from the complete captured audio, configured delivery runs, and Murmly returns to idle.

#### Scenario: Silence ends the recording

- **WHEN** stop mode is configured and the user falls silent for the configured duration
- **THEN** capture stops without a toggle command
- **AND** the transcript is produced and delivered under the existing delivery rules
- **AND** Murmly returns to idle

#### Scenario: User toggles before the silence elapses

- **WHEN** the user sends a toggle command before the configured silence duration is reached
- **THEN** the toggle ends the recording
- **AND** auto-transcribe does not also act on that recording

#### Scenario: Toggle arrives while an auto-stopped recording is processing

- **WHEN** a toggle command arrives after silence ended the recording and before processing finishes
- **THEN** Murmly reports that it is busy without starting a new recording or disturbing the transcript in flight

### Requirement: Continuous mode delivers segments and keeps listening

In continuous mode, a qualifying run of silence SHALL close the audio captured so far as a segment, and Murmly SHALL transcribe and deliver that segment while capture continues for the next utterance. Audio captured after the segment closes MUST NOT appear in that segment's transcript or in any later segment more than once. The session MUST end on an explicit toggle.

#### Scenario: First pause delivers a segment

- **WHEN** continuous mode is configured and the user pauses for the configured duration after speaking
- **THEN** Murmly transcribes and delivers the audio captured up to that pause
- **AND** capture continues without the user toggling

#### Scenario: Speech resumes after a delivered segment

- **WHEN** the user speaks again after a segment was delivered
- **THEN** the next segment contains only the audio captured after the previous segment closed

#### Scenario: Toggle ends a continuous session

- **WHEN** the user sends a toggle command during a continuous session
- **THEN** capture stops
- **AND** any audio captured since the last segment closed is transcribed and delivered as a final segment
- **AND** Murmly returns to idle

#### Scenario: Session ends with no speech since the last segment

- **WHEN** the user toggles a continuous session while no speech has been captured since the previous segment
- **THEN** Murmly returns to idle without delivering an additional transcript

### Requirement: Silence detection unavailable disables auto-transcribe

When Murmly cannot detect silence in the audio the active capture device produces, it SHALL disable auto-transcribe for that session and report the reason through diagnostics rather than ending recordings on an unreliable signal. Capture, transcription, and delivery MUST continue under explicit toggle control.

#### Scenario: Capture device is unsupported for silence detection

- **WHEN** capture starts with auto-transcribe enabled and silence cannot be detected on the negotiated capture format
- **THEN** the recording continues under explicit toggle control
- **AND** diagnostics report that auto-transcribe is unavailable for this session

### Requirement: Diagnostics report live and auto-transcribe configuration

`murmly doctor` SHALL report whether live transcription is enabled, which auto-transcribe mode is configured, the silence duration in effect, and whether the active session can support silence detection.

#### Scenario: Both features enabled

- **WHEN** diagnostics run with live transcription enabled and an auto-transcribe mode selected
- **THEN** the report states the live transcription setting, the selected mode, and the effective silence duration

#### Scenario: Defaults in effect

- **WHEN** diagnostics run with no live transcription or auto-transcribe settings in configuration
- **THEN** the report states that live transcription is disabled and auto-transcribe is disabled
