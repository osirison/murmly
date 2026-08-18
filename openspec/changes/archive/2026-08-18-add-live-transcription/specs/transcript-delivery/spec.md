## ADDED Requirements

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

## MODIFIED Requirements

### Requirement: Delivery target recorded before transcription

Murmly SHALL determine the intended delivery target at the moment capture stops or a segment closes, before transcription of that audio begins. The target MUST NOT be determined after transcription completes. A session that closes more than one segment MUST record a target for each segment independently, and MUST NOT reuse an earlier segment's target.

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
