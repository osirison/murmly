## ADDED Requirements

### Requirement: Pasted continuous segments are separated by a single space

In a continuous session bound for the focused window, every segment after the first one that produced a transcript SHALL be delivered with a single space before its transcript, in every speaker mode, so consecutive segments do not run together. The space MUST be part of the text pasted, or left on the clipboard when the paste is refused. The first segment that produces a transcript MUST have nothing before it, and a segment that produces none MUST add nothing.

#### Scenario: Second segment in speaker mode off

- **GIVEN** a continuous session in speaker mode off
- **WHEN** the first segment's transcript is "That is the first sentence." and the next segment's transcript is "Second sentence"
- **THEN** the first segment is pasted as "That is the first sentence."
- **AND** the next segment is pasted as " Second sentence"
- **AND** the window reads "That is the first sentence. Second sentence"

#### Scenario: First segment of a session

- **WHEN** the first segment of a continuous session produces a transcript
- **THEN** it is pasted exactly as transcribed, with no space before it

#### Scenario: Segments in label-everyone mode

- **GIVEN** a continuous session in label-everyone mode
- **WHEN** one segment's transcript is "Speaker 1: that's right" and the next segment's transcript is "You: Yeah, maybe"
- **THEN** the next segment is pasted as " You: Yeah, maybe"
- **AND** the window reads "Speaker 1: that's right You: Yeah, maybe"

#### Scenario: The final segment delivered by the toggle

- **WHEN** the toggle that ends a continuous session delivers a final segment after an earlier segment of that session produced a transcript
- **THEN** the final segment is pasted with a single space before its transcript

#### Scenario: A segment that mine-only empties

- **GIVEN** a continuous session in mine-only mode
- **WHEN** a segment whose transcript is "first thing" is followed by a segment holding only other voices, and then by a segment whose transcript is "closing words"
- **THEN** nothing is pasted for the segment holding only other voices
- **AND** the last segment is pasted as " closing words", with exactly one space before it

#### Scenario: Segments before the first transcript

- **WHEN** the first segments of a continuous session produce no transcript and a later segment is the first to produce one
- **THEN** that segment is pasted with no space before it

#### Scenario: A later segment is refused

- **WHEN** a segment after the first one that produced a transcript is refused, or cannot be injected
- **THEN** the clipboard holds that segment's transcript with a single space before it, exactly as it would have been pasted

#### Scenario: Recordings that are not continuous

- **WHEN** auto-transcribe is disabled or set to stop and a recording ends, including straight after an earlier recording into the same window
- **THEN** its transcript is pasted exactly as transcribed, with no space before it

### Requirement: The space between segments is not part of the transcript

The space Murmly pastes between continuous segments SHALL NOT be added to any transcript it reports or sends. The toggle response that ends a session MUST report the session's transcripts joined by exactly one space, as it does without this space, and a speech session MUST receive each segment's transcript as transcribed, with nothing added before it.

#### Scenario: Toggle response after several segments

- **WHEN** a continuous session pastes "first segment", " second segment" and " final segment" and the toggle ends it
- **THEN** the toggle response reports the text "first segment second segment final segment"
- **AND** that text contains no two spaces in a row

#### Scenario: Continuous capture bound for a speech session

- **WHEN** capture bound for a speech session runs in continuous mode and delivers two segments
- **THEN** the session receives each segment's transcript as transcribed, each in its own transcript frame
- **AND** neither transcript has a space added before it
