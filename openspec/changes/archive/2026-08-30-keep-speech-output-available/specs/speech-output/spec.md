## MODIFIED Requirements

### Requirement: Speech output unavailable is reported rather than fatal

When speech output is enabled but cannot run — its runtime is absent, its model files are missing, or no output device can be opened — Murmly SHALL start, refuse speech sessions with a reason, and continue serving transcription unchanged. A missing synthesis dependency MUST NOT prevent capture, delivery, or any existing command from working.

Whether the synthesis runtime is present SHALL be determined when a session is declared and not once at startup, so that a runtime removed from under a running daemon is refused rather than accepted and failed afterwards. Murmly MUST NOT accept a session it cannot serve: a caller that is refused can stay silent, whereas one that is accepted has already committed to whatever it does before speaking.

Determining this MUST NOT require producing speech, and MUST NOT be done when a synthesizer is already loaded — one that is loaded is proof enough, and a check on that path would be paid by every session for a condition that cannot hold.

A runtime found absent at the moment of one declaration SHALL NOT become the permanent reason speech output is unavailable. It refuses that declaration only, and a later declaration MUST be free to succeed. A daemon that recorded a transient absence permanently would stay silent for the rest of its life over a condition that has since been repaired, which is a worse failure than the one being prevented.

#### Scenario: Synthesis runtime absent

- **WHEN** speech output is enabled and its runtime is not installed
- **THEN** Murmly starts and reports speech output as unavailable, naming what to install
- **AND** capture, transcription, and delivery work unchanged

#### Scenario: No output device

- **WHEN** speech output is enabled and no output device can be opened
- **THEN** Murmly refuses speech sessions with a reason naming the device problem
- **AND** continues serving every other command

#### Scenario: Runtime removed while the daemon is running

- **GIVEN** a daemon that started with the synthesis runtime present and has no synthesizer loaded
- **WHEN** the runtime is removed and a caller declares a speech session
- **THEN** Murmly refuses the session, naming the runtime as absent and what to install
- **AND** the caller is refused before it produces any sound of its own

#### Scenario: Runtime restored without a restart

- **GIVEN** a daemon that has refused a speech session because the runtime was absent
- **WHEN** the runtime is reinstalled and a caller declares a speech session
- **THEN** Murmly accepts the session and speaks the text sent on it
- **AND** it does so without the daemon being restarted

#### Scenario: A loaded synthesizer is not re-examined

- **GIVEN** a daemon with a synthesizer already loaded
- **WHEN** a caller declares a speech session
- **THEN** the session is accepted without any further check for the runtime

#### Scenario: Transcription is unaffected by the check

- **WHEN** a speech session is refused because the runtime went missing after startup
- **THEN** capture, transcription, and delivery continue to work unchanged
- **AND** the daemon keeps serving every other command
