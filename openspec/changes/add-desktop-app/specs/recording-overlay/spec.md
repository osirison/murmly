# Spec Delta

## MODIFIED Requirements

### Requirement: Processing state visibility
Murmly SHALL replace the listening presentation with a visually distinct processing presentation after capture stops and SHALL keep that presentation visible while transcription and configured paste handling run. When transcription and configured paste handling run for a segment while microphone capture continues, Murmly SHALL retain the listening presentation instead, because the waveform still represents live microphone input. When transcription and configured paste handling succeed, Murmly SHALL show a visually distinct delivered confirmation for no more than 1.5 seconds before hiding the overlay. The confirmation MUST NOT display transcript text.

#### Scenario: Capture stops for transcription
- **WHEN** a toggle command transitions Murmly from listening to processing
- **THEN** the live waveform changes to the processing presentation and no longer represents microphone input

#### Scenario: Processing completes
- **WHEN** transcription and configured paste handling finish successfully
- **THEN** the overlay shows the delivered confirmation
- **AND** Murmly hides the overlay no more than 1.5 seconds later as it returns to idle

#### Scenario: Delivery is refused
- **WHEN** transcription finishes but delivery is refused
- **THEN** the overlay shows the error presentation, not the delivered confirmation

#### Scenario: Confirmation under reduced motion
- **WHEN** reduced motion is enabled and a delivery succeeds
- **THEN** the delivered confirmation appears without animation

#### Scenario: Capture starts during the confirmation
- **WHEN** capture starts while the delivered confirmation is showing
- **THEN** the overlay changes to the listening presentation at once

#### Scenario: A segment is transcribed while capture continues
- **WHEN** a segment is transcribed and delivered while microphone capture is still running
- **THEN** the overlay keeps the listening presentation with live audio-level feedback
- **AND** the overlay does not show the processing presentation or the delivered confirmation for that segment
