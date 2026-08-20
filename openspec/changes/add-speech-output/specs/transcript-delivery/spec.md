## MODIFIED Requirements

### Requirement: Delivery target recorded before transcription

Murmly SHALL determine the intended delivery target at the moment capture stops or a segment closes, before transcription of that audio begins. The target MUST NOT be determined after transcription completes. A session that closes more than one segment MUST record a target for each segment independently, and MUST NOT reuse an earlier segment's target.

Capture started by the hotkey designated for an open speech session is the one exception. Its transcript's recipient is that speech session, so Murmly MUST NOT record a window as its target and MUST NOT verify one before delivering. The recipient is known when capture starts and cannot change while capture runs, which is what recording a target before transcription exists to guarantee. Capture started by the hotkey designated for the focused window records and verifies a target exactly as it does today, whether or not a speech session is open.

#### Scenario: Target captured when recording stops

- **WHEN** the user stops capture and the session can observe the focused window
- **THEN** Murmly records the identity of the window that holds focus at that moment
- **AND** transcription begins without altering that recorded identity

#### Scenario: Target captured when a segment closes

- **WHEN** a segment closes while capture continues and the session can observe the focused window
- **THEN** Murmly records the identity of the window that holds focus at that moment as that segment's target
- **AND** that target is verified when that segment is delivered

#### Scenario: No transcript produced

- **WHEN** transcription yields no text
- **THEN** Murmly does not attempt delivery
- **AND** the clipboard is left exactly as it was before capture started

#### Scenario: Segment yields no transcript

- **WHEN** a segment's transcription yields no text
- **THEN** Murmly does not attempt delivery for that segment
- **AND** the session continues under its configured auto-transcribe mode

#### Scenario: Capture bound for a speech session records no window

- **WHEN** capture started by the speech-session hotkey stops and a speech session is open
- **THEN** Murmly records no window as the delivery target
- **AND** the transcript is delivered to that speech session

#### Scenario: Focus changes during capture bound for a speech session

- **WHEN** capture started by the speech-session hotkey runs and the focused window changes before capture stops
- **THEN** the transcript is still delivered to that speech session
- **AND** no window receives it

#### Scenario: Window-bound capture is unchanged by an open speech session

- **WHEN** capture started by the focused-window hotkey stops while a speech session is open
- **THEN** Murmly records and verifies a window target exactly as it does when no speech session is open
