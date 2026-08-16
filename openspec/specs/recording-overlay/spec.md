# Recording Overlay Specification

## Purpose

Provide unobtrusive visual confirmation of Murmly's microphone capture and transcription lifecycle on KDE Plasma X11 and Wayland while preserving focused application input and core voice-to-text operation.

## Requirements

### Requirement: Recording state visibility
When the recording overlay is enabled, Murmly SHALL display the overlay after microphone capture starts and SHALL keep it visible for the duration of the listening state. The listening presentation MUST include a microphone symbol and a waveform so the active capture state is identifiable without relying on color alone.

#### Scenario: Capture starts successfully
- **WHEN** a toggle command transitions Murmly from idle to listening
- **THEN** the bottom-centered overlay appears with the listening presentation

#### Scenario: Murmly remains idle
- **WHEN** Murmly is idle and no transient error is being shown
- **THEN** no recording overlay is visible

### Requirement: Live audio-level feedback
While listening, Murmly SHALL animate the waveform from the microphone signal level. The displayed level MUST be smoothed to reduce frame-to-frame jitter, MUST settle to a visible baseline during silence, and MUST remain bounded within the waveform's layout.

#### Scenario: Speech reaches the microphone
- **WHEN** microphone amplitude increases during listening
- **THEN** the waveform visibly rises in response without changing the overlay's dimensions or position

#### Scenario: Microphone input becomes quiet
- **WHEN** microphone amplitude falls to the silence range during listening
- **THEN** the waveform decays toward its baseline instead of disappearing or freezing at its prior level

### Requirement: Processing state visibility
Murmly SHALL replace the listening presentation with a visually distinct processing presentation after capture stops and SHALL keep that presentation visible while transcription and configured paste handling run.

#### Scenario: Capture stops for transcription
- **WHEN** a toggle command transitions Murmly from listening to processing
- **THEN** the live waveform changes to the processing presentation and no longer represents microphone input

#### Scenario: Processing completes
- **WHEN** transcription and configured paste handling finish successfully
- **THEN** Murmly hides the overlay as it returns to idle

### Requirement: Error state visibility
When capture or processing fails after an overlay transition begins, Murmly SHALL show a visually distinct error presentation for a bounded interval before hiding the overlay. The error presentation MUST NOT expose recorded audio or transcribed text.

#### Scenario: Microphone startup fails
- **WHEN** Murmly cannot start microphone capture
- **THEN** the overlay shows the transient error presentation and then hides

#### Scenario: Transcription fails
- **WHEN** processing raises an error
- **THEN** the overlay shows the transient error presentation without displaying partial transcription content and then hides

### Requirement: Non-disruptive Plasma placement
On a supported KDE Plasma X11 or Wayland session, Murmly SHALL center the overlay along the bottom edge of one display with a configurable bottom margin. The overlay MUST remain above ordinary application windows, MUST NOT request keyboard focus, MUST NOT intercept pointer input, and MUST NOT move or resize in response to animation. Display-protocol differences MUST NOT change the visible recording lifecycle.

#### Scenario: Overlay appears over the focused application
- **WHEN** the overlay becomes visible while another application has focus
- **THEN** it appears at the configured bottom-center position without changing the focused application

#### Scenario: Pointer crosses the overlay
- **WHEN** the user points or clicks through the overlay area
- **THEN** the underlying application receives the pointer interaction

#### Scenario: Multiple displays are connected
- **WHEN** the overlay becomes visible in a multi-display Plasma session
- **THEN** Murmly selects one display deterministically and keeps the overlay on that display for the current recording session

#### Scenario: Plasma uses X11
- **WHEN** the overlay becomes visible in a KDE Plasma X11 session
- **THEN** Murmly provides the same position, stacking, focus, input, dimensions, and visual states as the Wayland presentation

#### Scenario: Plasma uses Wayland
- **WHEN** the overlay becomes visible in a KDE Plasma Wayland session
- **THEN** Murmly provides the same position, stacking, focus, input, dimensions, and visual states as the X11 presentation

### Requirement: Configurable and accessible motion
Murmly SHALL allow users to disable the recording overlay and configure its bottom margin. Murmly MUST provide a reduced-motion presentation that communicates listening, processing, and error states without continuous waveform or processing animation.

#### Scenario: Overlay is disabled
- **WHEN** capture starts while the recording overlay is disabled
- **THEN** voice capture and processing continue without creating a visible overlay

#### Scenario: Reduced motion is enabled
- **WHEN** the overlay is visible while reduced motion is configured
- **THEN** Murmly uses stable state symbols and non-continuous level feedback instead of continuous animation

#### Scenario: Bottom margin is configured
- **WHEN** the overlay appears with a valid custom bottom margin
- **THEN** its distance from the selected display's bottom edge matches that configuration

### Requirement: Visual failure isolation
Murmly SHALL preserve capture, transcription, clipboard, and paste behavior when the visual runtime is missing, unsupported, or terminates unexpectedly. Visual failures MUST be reported through diagnostics without changing the existing toggle command response contract.

#### Scenario: Visual dependencies are unavailable
- **WHEN** capture starts on a system without the required visual runtime
- **THEN** Murmly records and processes speech normally while reporting the unavailable indicator through diagnostics

#### Scenario: Overlay terminates during recording
- **WHEN** the overlay terminates unexpectedly while Murmly is listening
- **THEN** microphone capture continues and the next toggle still initiates transcription
