## Purpose

Defines how a completed transcript reaches the application the user was dictating into, when Murmly must refuse to deliver it, and how a transcript that was not delivered stays recoverable instead of being silently destroyed.

## ADDED Requirements

### Requirement: Delivery target recorded before transcription

Murmly SHALL determine the intended delivery target at the moment capture stops, before transcription begins. The target MUST NOT be determined after transcription completes.

#### Scenario: Target captured when recording stops

- **WHEN** the user stops capture and the session can observe the focused window
- **THEN** Murmly records the identity of the window that holds focus at that moment
- **AND** transcription begins without altering that recorded identity

#### Scenario: No transcript produced

- **WHEN** transcription yields no text
- **THEN** Murmly does not attempt delivery
- **AND** the clipboard is left exactly as it was before capture started

### Requirement: Delivery refused when the target cannot be confirmed

On a session where the focused window is observable, Murmly SHALL inject the paste only when the window holding focus at delivery time is the recorded target. When the focused window differs, cannot be read, or was never recorded, Murmly MUST refuse to inject the paste. Murmly MUST fail closed: an unreadable or absent target is a refusal, not a permission.

#### Scenario: Focus unchanged

- **WHEN** the window holding focus at delivery time is the recorded target
- **THEN** Murmly copies the transcript and injects the paste into that window

#### Scenario: Focus moved to another window

- **WHEN** the window holding focus at delivery time differs from the recorded target
- **THEN** Murmly does not inject the paste
- **AND** the transcript is placed on the clipboard
- **AND** Murmly signals a delivery failure

#### Scenario: Target no longer exists

- **WHEN** the recorded target window has closed before delivery
- **THEN** Murmly does not inject the paste
- **AND** the transcript is placed on the clipboard

#### Scenario: Focus cannot be read at delivery time

- **WHEN** the session supports verification but the focused window cannot be read at delivery time
- **THEN** Murmly does not inject the paste
- **AND** the transcript is placed on the clipboard

### Requirement: Refused transcripts remain on the clipboard

When delivery is refused, Murmly SHALL leave the transcript on the clipboard and MUST NOT restore the previous clipboard contents. A transcript that was never delivered MUST remain retrievable by the user without any further Murmly command.

#### Scenario: Refusal preserves the transcript

- **WHEN** delivery is refused for any reason
- **THEN** the clipboard holds the transcript after Murmly returns to idle
- **AND** the clipboard contents present before capture are not restored

#### Scenario: Refusal reported to the caller

- **WHEN** delivery is refused
- **THEN** the toggle response reports that the transcript was copied but not pasted

### Requirement: Previous clipboard restored after a bounded delay

When clipboard restoration is enabled and delivery was injected, Murmly SHALL wait a configurable interval before restoring the previous clipboard contents, so the receiving application has time to read the transcript. The interval MUST be bounded, so no configured value can delay Murmly's return to idle indefinitely, and a value outside those bounds MUST fall back to the default.

Murmly cannot observe whether the receiving application has read the clipboard: a desktop clipboard manager takes a copy of every clipboard change immediately, so a selection-request signal reports the manager rather than the application. The interval is therefore a margin, not a guarantee.

#### Scenario: Delivered transcript is restored after the interval

- **WHEN** a transcript is injected and clipboard restoration is enabled
- **THEN** Murmly waits the configured interval
- **AND** then restores the clipboard contents present before capture

#### Scenario: Interval outside the supported bounds

- **WHEN** the configured interval is negative or larger than the supported maximum
- **THEN** Murmly uses the default interval
- **AND** Murmly returns to idle within the supported maximum

#### Scenario: Restoration disabled

- **WHEN** clipboard restoration is disabled in configuration
- **THEN** the transcript remains on the clipboard after delivery
- **AND** Murmly does not read the previous clipboard contents at any point

### Requirement: Sessions without observable focus deliver without verification

On a session where the focused window cannot be observed, Murmly SHALL inject the paste as it does today rather than refusing every delivery. Such a session MUST still receive clipboard preservation and bounded-delay restoration, and MUST NOT report itself as verified.

#### Scenario: Unverifiable session delivers

- **WHEN** the active session cannot expose the focused window and a transcript is produced
- **THEN** Murmly copies the transcript and injects the paste
- **AND** the previous clipboard is restored after the configured interval

#### Scenario: Unverifiable session is not reported as verified

- **WHEN** diagnostics are requested on a session that cannot expose the focused window
- **THEN** the report states that delivery target verification is unavailable

### Requirement: Target verification is configurable

Murmly SHALL provide a configuration option that disables delivery target verification. When verification is disabled, Murmly MUST inject the paste without comparing the focused window, and MUST retain clipboard preservation and bounded-delay restoration.

#### Scenario: Verification disabled by configuration

- **WHEN** target verification is disabled in configuration and the focused window has changed
- **THEN** Murmly injects the paste into the currently focused window
- **AND** the previous clipboard is restored after the configured interval

#### Scenario: Verification enabled by default

- **WHEN** configuration does not mention target verification
- **THEN** verification is enabled

### Requirement: Diagnostics report delivery verification support

`murmly doctor` SHALL report whether the active session supports delivery target verification and whether verification is enabled in configuration.

#### Scenario: Verification supported and enabled

- **WHEN** diagnostics run on a session that exposes the focused window with verification enabled
- **THEN** the report states that delivery target verification is supported and enabled

#### Scenario: Verification supported but disabled

- **WHEN** diagnostics run on a session that exposes the focused window with verification disabled in configuration
- **THEN** the report states that verification is supported and disabled in configuration

### Requirement: Delivery signals exclude transcript content

Signals that Murmly emits about delivery outcomes SHALL contain no transcript text, no clipboard contents, and no identifying details of the target application beyond what Murmly already exposes. A refused delivery MUST be observable without revealing what was said.

#### Scenario: Refusal signalled to the overlay

- **WHEN** delivery is refused and the overlay is active
- **THEN** the overlay shows its existing error state
- **AND** the message sent to the overlay carries no transcript text and no window identity

#### Scenario: Refusal recorded in logs

- **WHEN** delivery is refused and Murmly writes a log entry
- **THEN** the entry states that delivery was refused without including transcript text or clipboard contents
