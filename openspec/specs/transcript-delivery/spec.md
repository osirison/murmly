# Transcript Delivery Specification

## Purpose

Defines how a completed transcript reaches the application the user was dictating into, when Murmly must refuse to deliver it, and how a transcript that was not delivered stays recoverable instead of being silently destroyed.

## Requirements

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

Signals that Murmly emits about delivery outcomes SHALL contain no transcript text, no clipboard contents, and no identifying details of the target application beyond what Murmly already exposes. A refused delivery MUST be observable without revealing what was said. Partial transcripts sent to the overlay while listening are the sole exception: they carry transcript text by design, MUST be sent only while capture is running, and MUST NOT accompany any delivery outcome signal.

#### Scenario: Refusal signalled to the overlay

- **WHEN** delivery is refused and the overlay is active
- **THEN** the overlay shows its existing error state
- **AND** the message sent to the overlay carries no transcript text and no window identity

#### Scenario: Refusal recorded in logs

- **WHEN** delivery is refused and Murmly writes a log entry
- **THEN** the entry states that delivery was refused without including transcript text or clipboard contents

#### Scenario: Segment delivery outcome carries no transcript

- **WHEN** a segment is delivered or refused during a continuous session
- **THEN** the messages Murmly sends to the overlay about that outcome carry no transcript text

### Requirement: Refused delivery ends a continuous session

When a segment's delivery is refused during a continuous auto-transcribe session, Murmly SHALL stop capture and end the session rather than continuing to record speech it has already shown it cannot deliver. The refused transcript MUST remain on the clipboard under the existing refusal rules.

#### Scenario: Focus moves during a continuous session

- **WHEN** a segment's delivery is refused because the focused window differs from that segment's recorded target
- **THEN** Murmly stops capture and returns to idle
- **AND** the refused transcript is on the clipboard
- **AND** Murmly signals a delivery failure

#### Scenario: Segments delivered before the refusal are unaffected

- **WHEN** a continuous session delivers one or more segments and a later segment is refused
- **THEN** the segments delivered before the refusal remain in the application they were injected into

### Requirement: Clipboard restoration is serialized across segments

When a session delivers more than one transcript, Murmly SHALL NOT begin delivering a segment while a previous segment's clipboard restoration is still pending. A restoration MUST NOT overwrite a transcript that has not yet been delivered, and a pending restoration MUST NOT delay Murmly's return to idle beyond the supported maximum interval.

#### Scenario: Second segment closes during the restore interval

- **WHEN** a segment closes while the previous segment's clipboard restoration is still pending
- **THEN** Murmly completes that restoration before placing the new transcript on the clipboard
- **AND** the new transcript is the clipboard contents when its paste is injected

#### Scenario: Session ends with a restoration pending

- **WHEN** a continuous session ends while a clipboard restoration is pending
- **THEN** Murmly completes or abandons that restoration within the supported maximum interval
- **AND** Murmly returns to idle

### Requirement: Multi-segment session outcome reported to the caller

When a session delivers more than one transcript, the toggle response that ends the session SHALL report the session's combined transcript text and whether every segment in the session was delivered. Responses for sessions that produce a single transcript MUST keep their existing fields and meanings.

#### Scenario: Every segment delivered

- **WHEN** a continuous session ends after delivering every segment successfully
- **THEN** the toggle response reports the session's transcripts in the order they were captured
- **AND** the response reports the session as delivered

#### Scenario: One segment was refused

- **WHEN** a continuous session ends after a segment was refused
- **THEN** the toggle response reports that the session was not fully delivered
- **AND** the response reports that a transcript was copied but not pasted

#### Scenario: Single-transcript session

- **WHEN** a session produces exactly one transcript
- **THEN** the toggle response carries the same fields and meanings as a session recorded without auto-transcribe

### Requirement: Injection method is chosen by what the session can execute

Murmly SHALL choose a paste injection method that the active session can actually execute, and MUST NOT choose a method the session cannot support merely because the tool that implements it is installed. When more than one installed method exists, Murmly MUST prefer one it has determined the session can execute over one it has not.

#### Scenario: An installed method the session cannot support

- **WHEN** a paste injection tool is installed but the active session cannot execute it
- **THEN** Murmly does not select that method for delivery
- **AND** Murmly selects an installed method the session can execute, if one exists

#### Scenario: A usable method exists

- **WHEN** at least one installed injection method can be executed in the active session
- **THEN** Murmly injects the paste with that method

#### Scenario: No method can be executed

- **WHEN** no installed injection method can be executed in the active session
- **THEN** Murmly reports paste injection as unavailable for the session
- **AND** delivery follows the rules for an unavailable injection method

### Requirement: An undeliverable transcript is never lost to an injector failure

When no usable paste injection method is available, or the selected method fails while delivering, Murmly SHALL treat that delivery as refused under the existing refusal rules rather than as an error. The transcript MUST be on the clipboard, the caller MUST be told it was copied but not pasted, and the previous clipboard contents MUST NOT be restored over it. Murmly MUST be able to copy a transcript in a session where it cannot inject a paste at all.

#### Scenario: No injection method available

- **WHEN** a transcript is produced in a session with no usable injection method
- **THEN** the transcript is on the clipboard
- **AND** the toggle response reports that the transcript was copied but not pasted
- **AND** the response does not report the toggle as failed

#### Scenario: Injection fails during delivery

- **WHEN** the selected injection method fails while delivering a transcript
- **THEN** the transcript is on the clipboard
- **AND** the toggle response reports that the transcript was copied but not pasted

#### Scenario: Continuous session with no injection method

- **WHEN** a segment cannot be injected during a continuous auto-transcribe session because no usable injection method is available
- **THEN** Murmly stops capture and returns to idle as it does for any refused delivery
- **AND** that segment's transcript is on the clipboard

#### Scenario: Overlay is told without transcript content

- **WHEN** delivery is not injected because no usable injection method is available
- **THEN** the overlay shows its existing error state
- **AND** the message sent to the overlay carries no transcript text

### Requirement: Diagnostics report paste injection support

`murmly doctor` SHALL report whether Murmly can inject a paste in the active session and which method it would use. When it cannot inject, the report MUST name what the user has to install or enable to make injection work in this session, and MUST distinguish an injection tool that is absent from one that is installed but unusable here.

#### Scenario: Injection available

- **WHEN** diagnostics run in a session where Murmly can inject a paste
- **THEN** the report names the injection method Murmly would use

#### Scenario: No injection tool installed

- **WHEN** diagnostics run in a session where no injection tool is installed
- **THEN** the report states that transcripts will be copied but not pasted
- **AND** names what to install for this session

#### Scenario: Installed tool is unusable in this session

- **WHEN** diagnostics run in a session where an installed injection tool cannot be executed
- **THEN** the report states that the installed tool cannot be used in this session
- **AND** names what to install or enable instead

### Requirement: An unconfirmable injection method must not overwrite the transcript

Some injection methods report success whether or not the keystroke reached the focused window, so Murmly cannot tell delivery from silent failure. When delivery was attempted with such a method, Murmly SHALL leave the transcript on the clipboard and MUST NOT restore the previous contents over it, whatever the restoration setting says. Restoration remains as configured for a method whose failure Murmly can observe.

#### Scenario: Delivery by a method whose success cannot be observed

- **WHEN** a transcript is injected with a method that cannot confirm the keystroke arrived
- **THEN** the transcript is still the clipboard contents after Murmly returns to idle
- **AND** the contents present before capture are not restored
- **AND** Murmly does not wait out the restoration interval

#### Scenario: Delivery by a method whose failure is observable

- **WHEN** a transcript is injected with a method that reports its failures
- **THEN** clipboard restoration behaves as configured
