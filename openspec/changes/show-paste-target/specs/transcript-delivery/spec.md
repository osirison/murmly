# Spec Delta

## MODIFIED Requirements

### Requirement: Delivery signals exclude transcript content

Signals that Murmly emits about delivery outcomes SHALL contain no transcript text, no clipboard contents, and no identifying details of the target application beyond what Murmly already exposes. A refused delivery MUST be observable without revealing what was said. Partial transcripts sent to the overlay while listening are the sole exception for transcript text: they carry transcript text by design, MUST be sent only while capture is running, and MUST NOT accompany any delivery outcome signal. The target application's display name is the sole exception for target identity: it SHALL be sent to the overlay and reported in the toggle response only while target naming is enabled, and MUST NOT be written to logs. Window ids, process ids and window titles MUST NOT accompany any signal.

#### Scenario: Refusal signalled to the overlay

- **WHEN** delivery is refused and the overlay is active
- **THEN** the overlay shows its existing error state
- **AND** the message sent to the overlay carries no transcript text, no window id, no process id and no window title
- **AND** the message carries the target application's display name only when target naming is enabled

#### Scenario: Refusal recorded in logs

- **WHEN** delivery is refused and Murmly writes a log entry
- **THEN** the entry states that delivery was refused without including transcript text or clipboard contents
- **AND** the entry does not include the target application's display name

#### Scenario: Segment delivery outcome carries no transcript

- **WHEN** a segment is delivered or refused during a continuous session
- **THEN** the messages Murmly sends to the overlay about that outcome carry no transcript text

#### Scenario: Target naming turned off

- **WHEN** `overlay.show_target` is `false` and a transcript is delivered or refused
- **THEN** no message to the overlay and no toggle response carries any application name

## ADDED Requirements

### Requirement: The toggle response names the delivery target

When a transcript is delivered to, or refused for, a recorded target and target naming is enabled, the toggle response SHALL report the target's display name. On a refusal it SHALL also report the display name of the application in front at delivery time when that could be read. A name that is not known MUST be absent, never guessed. Existing fields keep their meanings.

#### Scenario: Delivered

- **WHEN** a transcript is pasted into its recorded target
- **THEN** the toggle response reports the target's display name alongside its existing fields

#### Scenario: Refused because another application came forward

- **WHEN** delivery is refused because a different application holds focus at delivery time
- **THEN** the toggle response reports the recorded target's display name
- **AND** reports the display name of the application that held focus instead

#### Scenario: No target was recorded

- **WHEN** a transcript is produced on a session that cannot observe focus, or by capture bound for a speech session
- **THEN** the toggle response carries no target name

### Requirement: Display names come from the application identity Murmly records

The display name SHALL be derived from the application identity Murmly already records for target verification, resolved to the application's own readable name where the platform provides one, and falling back to the recorded identifier otherwise. Murmly MUST NOT read window titles, and MUST NOT request any additional permission, to obtain it.

#### Scenario: An application with a readable name

- **WHEN** the focused application's recorded identity resolves to a readable name on this platform
- **THEN** that readable name is the display name

#### Scenario: An application without one

- **WHEN** the recorded identity has no readable name available
- **THEN** the recorded identifier itself is the display name

#### Scenario: No permission is requested

- **WHEN** Murmly resolves a display name on any supported platform
- **THEN** no permission prompt is shown and no window title is read
