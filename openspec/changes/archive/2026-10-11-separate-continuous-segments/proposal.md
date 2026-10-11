## Why

In continuous mode Murmly pastes each segment as soon as a pause closes it, exactly
as transcribed, with nothing between it and the segment pasted before. Consecutive
segments therefore run together in the user's document. In speaker mode `off` two
sentences come out as `…first sentence.Second sentence`, and in `label-everyone`
a conversation comes out as
`Speaker 1: that's right … workYou: Yeah, maybe, sometimes, who knows, it could be this, it could be thatSpeaker 1: Oh, or, I mean, …`.
`mine-only` has the same problem.

It was found while using speaker recognition, but it is older than that and is not
caused by it: every speaker mode, `off` included, pastes segments this way. That is
why it is a change of its own rather than part of `add-speaker-recognition`.

## What Changes

- **A space between segments.** In a continuous session, every segment after the
  first one that produced a transcript is delivered with a single space in front of
  it. The first segment of a session is delivered as it is today, with nothing in
  front of it. This applies in every speaker mode and to both destinations: the
  text pasted into the focused window, and the `transcript` frame sent to a speech
  session.
- **Nothing for a segment that delivers nothing.** A segment that yields no text, or
  that mine-only empties, delivers nothing and adds no space, so the next segment
  still gets exactly one space, never two.
- **The final segment too.** The segment delivered by the toggle that ends the
  session follows the same rule as the segments delivered at pauses.
- **An undelivered segment keeps its space.** When a later segment's paste is
  refused or cannot be injected, or its speech session has closed, the clipboard
  holds that segment exactly as it would have been delivered, space included.
- **The reported transcript does not change.** The text a toggle response reports
  for a session already joins segments with one space and stays exactly as it is.
  Logs carry no transcript text and are unchanged. Speaker labels and filtering are
  unchanged.
- **Unchanged:** with auto-transcribe off or set to `stop`, a recording is one
  transcript and is delivered exactly as today, even straight after an earlier
  recording.

The user settled the three questions the draft left open: the speech-session
target gets the space, the clipboard copy of an undelivered segment keeps it, and a
stray leading space where Murmly cannot see the cursor is accepted (design.md,
Decisions).

This changes what is pasted in mode `off`, the default, for anyone who uses
continuous mode. It also changes the text of later segments' `transcript` frames
for external speech-session callers. It changes no configuration key, response
field, or frame shape.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `live-transcription`: three requirements are added next to "Continuous mode
  delivers segments and keeps listening". They state:
  - continuous segments after the first one that produced a transcript are
    separated by a single space, in every speaker mode and for both destinations,
    and when no space is added;
  - the space is part of whatever receives the segment: the paste, the speech-session
    frame, or the clipboard when delivery fails;
  - the space is not part of the transcript the toggle response reports.

  No existing requirement's text changes.

## Impact

- `src/murmly/daemon.py`: `SpeechSession.process_recording` and
  `SpeechSession.process_for_session` take the text to put in front of the
  transcript. Each uses it for the delivery (paste or session frame) and for the
  clipboard copy when delivery fails. The transcript they return stays bare.
  `MurmlyDaemon` passes a space for segments after the first one that produced
  text, for both destinations. It does this where segments are delivered at a pause
  and where the final segment is delivered by the toggle.
- `tests/test_daemon.py`, `tests/test_daemon_speakers.py`,
  `tests/test_speech_session.py`: the session doubles accept the new argument, the
  assertions on delivered segment lists expect the space, and new tests cover the
  rule and its edge cases for both destinations.
- `manual/pause-to-finish.md`, `manual/typing-only-your-own-voice.md`,
  `manual/for-developers.md`: say that segments are delivered with a space between
  them, and that a speech session's later continuous `transcript` frames begin with
  that space.
- No dependency, configuration, or platform change. Every paste method (`wtype`,
  `xdotool`, `ydotool`, Windows `SendInput`, macOS) receives the same text, because
  all of them paste the clipboard.
- Interaction with the open `add-speaker-recognition` change: none of its files are
  edited. Its requirements that a labelled transcript begins with a label and stays
  on one line still hold, because the space is put between deliveries and is not
  part of the transcript.
