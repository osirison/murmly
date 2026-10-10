## ADDED Requirements

### Requirement: The overlay reports when its own control work has failed

When the background work that drives the overlay cannot be started, or ends because
of an unexpected failure, Murmly SHALL report the overlay as unavailable and SHALL
name that failure as the cause, replacing whatever the overlay reported before. From
then on Murmly MUST drop every update meant for the overlay — a state change, an
audio level, a partial transcript, or an error presentation — rather than hold it, so
that nothing accumulates for work that no longer exists or never began. Updates that
were already waiting when the work ended, or when it could not be started, MUST be
discarded with it. Murmly MUST NOT try to start that work again. An overlay renderer
process still running when the work ended MUST be terminated, as it is when Murmly
shuts down, and a request to close the overlay that arrives while that is still
happening MUST NOT return before the process has been terminated.

This requirement covers the overlay's own control work failing to start or ending
unexpectedly, not the renderer process terminating, which "Visual failure isolation"
already covers. It changes nothing about microphone capture, transcription, clipboard
and paste handling, or the toggle command response contract.

#### Scenario: The overlay's control work fails

- **WHEN** the background work that drives the overlay ends because of an unexpected
  failure
- **THEN** the overlay is reported as unavailable
- **AND** the reported cause names that failure rather than the state the overlay
  reported before it
- **AND** the overlay's renderer process is terminated

#### Scenario: The overlay's control work cannot be started

- **WHEN** Murmly tries to start the background work that drives the overlay and it
  cannot be started
- **THEN** the overlay is reported as unavailable
- **AND** the reported cause names why it could not be started
- **AND** updates that were already waiting for the overlay are discarded
- **AND** every update meant for the overlay afterwards is dropped rather than held

#### Scenario: Updates arrive after the work has failed

- **WHEN** Murmly has a state change, an audio level, a partial transcript, or an
  error presentation for the overlay after that work has ended
- **THEN** the update is dropped rather than held
- **AND** no number of further updates makes the overlay hold more than it did before

#### Scenario: Updates were waiting when the work failed

- **WHEN** updates are waiting for the overlay at the moment its control work ends
- **THEN** they are discarded

#### Scenario: An update is being handed over as the work fails

- **WHEN** an update is being handed to the overlay at the moment its control work
  ends
- **THEN** the update is not held for later, whichever side of the failure it reaches
  the overlay on

#### Scenario: The work fails before it has processed anything

- **WHEN** the overlay's control work ends because of an unexpected failure before it
  has handled a single update, while the overlay was still reporting that it had not
  started
- **THEN** the reported cause names that failure rather than the earlier report

#### Scenario: The overlay is asked to start after the work has failed

- **WHEN** Murmly is asked to start the overlay after its control work has ended
  because of an unexpected failure, or after it could not be started
- **THEN** the control work is not started again
- **AND** the reported cause is still the original failure

#### Scenario: The work fails while Murmly is shutting down

- **WHEN** the overlay's control work fails while Murmly is shutting down
- **THEN** shutdown returns within its existing bounded time
- **AND** the overlay's renderer process is terminated
- **AND** the overlay is reported as unavailable with that failure as the cause

#### Scenario: The overlay is closed while the failed work is still tearing down

- **WHEN** Murmly's overlay is closed after its control work has ended because of an
  unexpected failure, while that work is still shutting the renderer process down
- **THEN** the renderer process is terminated before the close returns
- **AND** the reported cause is still the original failure

#### Scenario: The overlay is closed after the work has failed

- **WHEN** Murmly's overlay is closed, once or more than once, after its control work
  has ended because of an unexpected failure
- **THEN** each close returns without error
- **AND** the reported cause is still the original failure

#### Scenario: The overlay is closed after its control work could not be started

- **WHEN** Murmly's overlay is closed, once or more than once, after its control work
  could not be started
- **THEN** each close returns without error
- **AND** no close waits for work that never began
- **AND** the reported cause is still the one for the failed start
