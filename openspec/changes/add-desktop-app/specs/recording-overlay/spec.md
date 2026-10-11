# Spec Delta

## MODIFIED Requirements

### Requirement: Processing state visibility
Murmly SHALL replace the listening presentation with a visually distinct processing presentation after capture stops and SHALL keep that presentation visible while transcription and configured paste handling run. When transcription and configured paste handling run for a segment while microphone capture continues, Murmly SHALL retain the listening presentation instead, because the waveform still represents live microphone input.

#### Scenario: Capture stops for transcription
- **WHEN** a toggle command transitions Murmly from listening to processing
- **THEN** the live waveform changes to the processing presentation and no longer represents microphone input

#### Scenario: Processing completes
- **WHEN** transcription and configured paste handling finish successfully
- **THEN** the overlay shows the delivered confirmation and then hides as Murmly returns to idle

#### Scenario: A segment is transcribed while capture continues
- **WHEN** a segment is transcribed and delivered while microphone capture is still running
- **THEN** the overlay keeps the listening presentation with live audio-level feedback
- **AND** the overlay does not show the processing presentation for that segment

## ADDED Requirements

### Requirement: A successful delivery is confirmed

When transcription and configured paste handling succeed for a recording, Murmly SHALL show a visually distinct delivered confirmation for no more than 1.5 seconds before hiding the overlay. The confirmation MUST NOT display transcript text, and MUST NOT be shown for a segment delivered while capture continues.

#### Scenario: A recording is delivered

- **WHEN** transcription and configured paste handling finish successfully after capture stops
- **THEN** the overlay shows the delivered confirmation
- **AND** hides it no more than 1.5 seconds later

#### Scenario: Delivery is refused

- **WHEN** transcription finishes but delivery is refused
- **THEN** the overlay shows the error presentation, not the delivered confirmation

#### Scenario: Confirmation under reduced motion

- **WHEN** reduced motion is enabled and a delivery succeeds
- **THEN** the delivered confirmation appears without animation

#### Scenario: Capture starts during the confirmation

- **WHEN** capture starts while the delivered confirmation is showing
- **THEN** the overlay changes to the listening presentation at once

#### Scenario: A segment delivered while capture continues

- **WHEN** a segment is delivered while microphone capture is still running
- **THEN** the overlay keeps the listening presentation and shows no delivered confirmation

### Requirement: A recording left empty by mine-only is acknowledged

When mine-only leaves out every part of a recording, Murmly SHALL show a neutral presentation saying that the voice heard was not the owner's, for no more than 1.5 seconds, before hiding the overlay. It MUST differ from both the error presentation and the delivered confirmation, and MUST carry no speaker name and no transcript text.

#### Scenario: Only other voices were heard

- **WHEN** a recording in mine-only mode ends and every part of it was left out
- **THEN** the overlay shows the not-your-voice presentation
- **AND** hides no more than 1.5 seconds later

#### Scenario: A continuous segment with only other voices

- **WHEN** a segment of a continuous session in mine-only mode is left out entirely while capture continues
- **THEN** the overlay keeps the listening presentation and does not show the not-your-voice presentation

#### Scenario: Nothing identifying is shown

- **WHEN** the not-your-voice presentation is shown
- **THEN** it displays no speaker name and no words from the recording
