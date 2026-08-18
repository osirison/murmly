## MODIFIED Requirements

### Requirement: Visual failure isolation

Murmly SHALL preserve capture, transcription, clipboard, and paste behavior when the visual runtime is missing, unsupported, or terminates unexpectedly. Visual failures MUST be reported through diagnostics without changing the existing toggle command response contract. An overlay Murmly cannot place as this specification requires MUST be treated as an unavailable visual runtime: Murmly MUST NOT present an overlay whose position, stacking, or focus behavior differs from the specified presentation.

#### Scenario: Visual dependencies are unavailable

- **WHEN** capture starts on a system without the required visual runtime
- **THEN** Murmly records and processes speech normally while reporting the unavailable indicator through diagnostics

#### Scenario: Overlay terminates during recording

- **WHEN** the overlay terminates unexpectedly while Murmly is listening
- **THEN** microphone capture continues and the next toggle still initiates transcription

#### Scenario: The overlay cannot be placed as specified

- **WHEN** capture starts in a session where Murmly cannot place the overlay at the specified position with the specified stacking and focus behavior
- **THEN** no overlay window is presented at any position
- **AND** microphone capture, transcription, clipboard, and paste handling continue
- **AND** diagnostics report the overlay as unavailable

## ADDED Requirements

### Requirement: Overlay diagnostics evaluate the overlay's own runtime

Murmly's diagnostics SHALL evaluate the overlay runtime under the same conditions the overlay itself runs under, so that a report of available predicts an overlay presented as specified and a report of unavailable predicts no overlay at all. When the overlay is unavailable, the reported cause MUST distinguish a runtime Murmly failed to prepare from a session that does not offer a capability the overlay requires, because those have different remedies.

#### Scenario: Diagnostics agree with what the overlay does

- **WHEN** diagnostics run in a session where the overlay would be presented as specified
- **THEN** the report states that the overlay is available

#### Scenario: Diagnostics agree with a refused overlay

- **WHEN** diagnostics run in a session where the overlay would refuse to present itself
- **THEN** the report states that the overlay is unavailable

#### Scenario: Murmly did not prepare the runtime

- **WHEN** the session offers every capability the overlay requires but Murmly's own preparation of the visual runtime is what failed
- **THEN** the reported cause names that preparation rather than attributing the failure to the session

#### Scenario: The session lacks a required capability

- **WHEN** the session does not offer a capability the overlay requires
- **THEN** the reported cause names the missing session capability
