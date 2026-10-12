## ADDED Requirements

### Requirement: Continuous segments are separated by a single space

In a continuous session, every segment after the first one that produced a transcript SHALL be delivered with a single space immediately before its transcript, in every speaker mode, whether capture is bound for the focused window or for a speech session, so consecutive segments do not run together. The first segment that produces a transcript MUST be delivered with nothing before it, and a segment that produces no transcript MUST deliver nothing and add no space.

#### Scenario: Second segment in speaker mode off

- **GIVEN** a window-bound continuous session in speaker mode off
- **WHEN** the first segment's transcript is "That is the first sentence." and the next segment's transcript is "Second sentence"
- **THEN** the first segment is pasted as "That is the first sentence."
- **AND** the next segment is pasted as " Second sentence"
- **AND** the window reads "That is the first sentence. Second sentence"

#### Scenario: First segment of a session

- **WHEN** the first segment of a continuous session produces a transcript
- **THEN** it is delivered exactly as transcribed, with no space before it

#### Scenario: Segments in label-everyone mode

- **GIVEN** a window-bound continuous session in label-everyone mode
- **WHEN** one segment's transcript is "Speaker 1: that's right" and the next segment's transcript is "You: Yeah, maybe"
- **THEN** the next segment is pasted as " You: Yeah, maybe"
- **AND** the window reads "Speaker 1: that's right You: Yeah, maybe"

#### Scenario: The final segment delivered by the toggle

- **WHEN** the toggle that ends a continuous session delivers a final segment after an earlier segment of that session produced a transcript
- **THEN** the final segment is delivered with a single space before its transcript

#### Scenario: A segment that mine-only empties

- **GIVEN** a continuous session in mine-only mode
- **WHEN** a segment whose transcript is "first thing" is followed by a segment holding only other voices, and then by a segment whose transcript is "closing words"
- **THEN** nothing is delivered for the segment holding only other voices
- **AND** the last segment is delivered as " closing words", with exactly one space before it

#### Scenario: Segments before the first transcript

- **WHEN** the first segments of a continuous session produce no transcript and a later segment is the first to produce one
- **THEN** that segment is delivered with no space before it

#### Scenario: Recordings that are not continuous

- **WHEN** auto-transcribe is disabled or set to stop and a recording ends, including straight after an earlier recording to the same destination
- **THEN** its transcript is delivered exactly as transcribed, with no space before it

### Requirement: The space between segments goes wherever the segment goes

The space before a continuous segment SHALL be part of the text Murmly delivers for that segment, whatever receives it: the text pasted into the focused window, the text of the transcript frame sent to a speech session, and the text placed on the clipboard when a paste is refused or cannot be injected, or when the speech session can no longer receive it.

#### Scenario: Continuous capture bound for a speech session

- **WHEN** capture bound for a speech session runs in continuous mode and its segments' transcripts are "first segment" and then "second segment"
- **THEN** the session receives a transcript frame whose text is "first segment"
- **AND** then a transcript frame whose text is " second segment"

#### Scenario: A later segment is refused

- **WHEN** a window-bound segment after the first one that produced a transcript is refused, or cannot be injected
- **THEN** the clipboard holds that segment's transcript with a single space before it, exactly as it would have been pasted

#### Scenario: The speech session is gone before a later segment

- **WHEN** a session-bound segment after the first one that produced a transcript cannot be delivered because the speech session has closed
- **THEN** the clipboard holds that segment's transcript with a single space before it, exactly as it would have been sent

### Requirement: The space between segments is not part of the reported transcript

The space Murmly delivers before a continuous segment SHALL NOT be added to the transcript Murmly reports for the session. The toggle response that ends a window-bound session MUST report the session's transcripts joined by exactly one space, with no two spaces in a row, as it does without this space.

#### Scenario: Toggle response after several segments

- **WHEN** a window-bound continuous session pastes "first segment", " second segment" and " final segment" and the toggle ends it
- **THEN** the toggle response reports the text "first segment second segment final segment"
- **AND** that text contains no two spaces in a row
