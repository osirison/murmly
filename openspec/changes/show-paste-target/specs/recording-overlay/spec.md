# Spec Delta

## ADDED Requirements

### Requirement: The overlay names where the transcript will go

When target naming is enabled and the session can observe the focused application, the overlay SHALL show the display name of the application the transcript will be delivered to: while listening, the application holding focus now; once capture stops, the recorded target. The name MUST NOT include a window title.

#### Scenario: Listening names the application in front

- **WHEN** capture starts while an application holds focus, on a session that can observe focus, with target naming enabled
- **THEN** the overlay shows that application's display name with the listening presentation

#### Scenario: Focus moves while listening

- **WHEN** another application takes focus while capture is running
- **THEN** the overlay shows the new application's name within one second

#### Scenario: Capture stops

- **WHEN** capture stops and the target is recorded
- **THEN** the overlay shows the recorded target's name with the processing presentation
- **AND** the name does not change if focus moves before delivery

#### Scenario: Delivery is refused

- **WHEN** delivery is refused because the focused application differs from the recorded target
- **THEN** the error presentation names the recorded target as the application the words were meant for

#### Scenario: Capture bound for a speech session

- **WHEN** capture started by the speech-session hotkey is running
- **THEN** the overlay names the speech session as the destination, not the focused application

#### Scenario: The session cannot observe focus

- **WHEN** capture runs on a session where the focused application cannot be observed
- **THEN** the overlay shows no destination name and no placeholder in its place

#### Scenario: Target naming is turned off

- **WHEN** `overlay.show_target` is `false`
- **THEN** the overlay shows no destination name in any state

### Requirement: The destination name never disturbs the overlay's placement

The destination name SHALL be drawn in a bounded label below the indicator and truncated to fit. Showing, changing or removing it MUST NOT change the indicator's position or dimensions, and the label MUST NOT request focus or intercept pointer input.

#### Scenario: A long application name

- **WHEN** the display name is longer than the label can hold
- **THEN** the name is truncated with an ellipsis
- **AND** the indicator keeps its position and dimensions

#### Scenario: Pointer crosses the label

- **WHEN** the person clicks where the label is drawn
- **THEN** the application underneath receives the click
