## ADDED Requirements

### Requirement: Partial transcript visibility
When live transcription is enabled and the overlay is enabled, Murmly SHALL display the most recent partial transcript in a transcript panel that is separate from the recording indicator. The recording indicator MUST keep the dimensions and position it has without live transcription, so adding transcript text never disturbs the existing presentation. Murmly MUST replace the displayed partial with each newer partial rather than accumulating them.

#### Scenario: First partial arrives
- **WHEN** a partial transcript is produced while the overlay shows the listening presentation
- **THEN** the transcript panel appears displaying that text
- **AND** the recording indicator keeps the dimensions and position it had before the partial arrived

#### Scenario: A newer partial supersedes an earlier one
- **WHEN** a partial transcript is produced while an earlier partial is displayed
- **THEN** the transcript panel displays only the newer text

#### Scenario: Live transcription disabled
- **WHEN** the overlay shows the listening presentation while live transcription is disabled
- **THEN** no transcript panel is created and the overlay is indistinguishable from a session without this capability

#### Scenario: Overlay disabled while live transcription is enabled
- **WHEN** capture starts with live transcription enabled and the overlay disabled
- **THEN** no overlay is created and no partial transcript is displayed anywhere

### Requirement: Transcript panel sizing is bounded and configurable
The transcript panel SHALL size itself to the text it displays, and its width MUST NOT exceed a bounded fraction of the display it appears on, so a long partial cannot span the screen or push the panel off it. Murmly SHALL provide a configurable text size for the panel. Text that does not fit the panel at its maximum size MUST be shown truncated rather than overflowing or resizing the panel further.

#### Scenario: Short partial
- **WHEN** the transcript panel displays a partial that fits well within its bound
- **THEN** the panel is only as wide as that text requires

#### Scenario: Partial reaches the width bound
- **WHEN** the transcript panel displays a partial longer than its maximum width allows
- **THEN** the panel stops at its maximum width
- **AND** the text is shown truncated within that width

#### Scenario: Text size is configured
- **WHEN** the transcript panel appears with a configured text size
- **THEN** the panel renders its text at that size

#### Scenario: Text size outside the supported bounds
- **WHEN** the configured text size is below the supported minimum or above the supported maximum
- **THEN** Murmly uses the default text size

### Requirement: Partial text discarded when listening ends
Murmly SHALL discard any displayed partial transcript when the overlay leaves the listening presentation, whether it advances to processing, to the error presentation, or to idle. A partial transcript MUST NOT remain visible once the audio it describes is no longer being captured.

#### Scenario: Capture stops with a partial displayed
- **WHEN** capture stops while a partial transcript is displayed
- **THEN** the overlay clears that text as it advances to the processing presentation

#### Scenario: Session ends with a partial displayed
- **WHEN** Murmly returns to idle while a partial transcript is displayed
- **THEN** the overlay clears that text before hiding

## MODIFIED Requirements

### Requirement: Processing state visibility
Murmly SHALL replace the listening presentation with a visually distinct processing presentation after capture stops and SHALL keep that presentation visible while transcription and configured paste handling run. When transcription and configured paste handling run for a segment while microphone capture continues, Murmly SHALL retain the listening presentation instead, because the waveform still represents live microphone input.

#### Scenario: Capture stops for transcription
- **WHEN** a toggle command transitions Murmly from listening to processing
- **THEN** the live waveform changes to the processing presentation and no longer represents microphone input

#### Scenario: Processing completes
- **WHEN** transcription and configured paste handling finish successfully
- **THEN** Murmly hides the overlay as it returns to idle

#### Scenario: A segment is transcribed while capture continues
- **WHEN** a segment is transcribed and delivered while microphone capture is still running
- **THEN** the overlay keeps the listening presentation with live audio-level feedback
- **AND** the overlay does not show the processing presentation for that segment

### Requirement: Error state visibility
When capture or processing fails after an overlay transition begins, Murmly SHALL show a visually distinct error presentation for a bounded interval before hiding the overlay. The error presentation MUST NOT expose recorded audio or transcribed text, including any partial transcript displayed before the failure.

#### Scenario: Microphone startup fails
- **WHEN** Murmly cannot start microphone capture
- **THEN** the overlay shows the transient error presentation and then hides

#### Scenario: Transcription fails
- **WHEN** processing raises an error
- **THEN** the overlay shows the transient error presentation without displaying partial transcription content and then hides

#### Scenario: Failure while a partial transcript is displayed
- **WHEN** capture or processing fails while a partial transcript is displayed
- **THEN** the overlay clears that text before showing the error presentation
