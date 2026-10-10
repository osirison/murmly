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

- **A space between pasted segments.** In a continuous session bound for the focused
  window, every segment after the first one that produced a transcript is pasted with
  a single space in front of it. The first segment of a session is pasted as it is
  today, with nothing in front of it. This applies in every speaker mode.
- **Nothing for a segment that pastes nothing.** A segment that yields no text, or
  that mine-only empties, pastes nothing and adds no space, so the next segment still
  gets exactly one space, never two.
- **The final segment too.** The segment delivered by the toggle that ends the
  session follows the same rule as the segments delivered at pauses.
- **A refused segment keeps its space.** When a later segment's delivery is refused
  or cannot be injected, the clipboard holds that segment exactly as it would have
  been pasted, space included. This is design.md Open question 3.
- **The transcript itself does not change.** The text a toggle response reports for
  a session already joins segments with one space and stays exactly as it is. The
  speech-session target receives each segment as its own `transcript` frame, without
  a space added (design.md Open question 1). Speaker labels and filtering are
  unchanged.
- **Unchanged:** with auto-transcribe off or set to `stop`, a recording is one
  transcript and is pasted exactly as today, even straight after an earlier
  recording.

This changes what is pasted in mode `off`, the default, for anyone who uses
continuous mode. It changes no configuration key, response field, or protocol frame.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `live-transcription`: two requirements are added next to "Continuous mode delivers
  segments and keeps listening". One states that pasted segments after the first are
  separated by a single space, and when no space is added. The other states that the
  space is not part of any transcript: the toggle response and the speech-session
  frames are unchanged. No existing requirement's text changes.

## Impact

- `src/murmly/daemon.py`: `SpeechSession.process_recording` takes the text to put
  in front of the transcript and uses it for both the paste and the clipboard copy
  on refusal. The transcript it returns stays bare. `MurmlyDaemon` passes a space
  for window-bound segments after the first one that produced text. It does this
  where segments are delivered at a pause and where the final segment is delivered
  by the toggle.
- `tests/test_daemon.py`, `tests/test_daemon_speakers.py`,
  `tests/test_speech_session.py`: the session doubles accept the new argument, the
  assertions on pasted segment lists expect the space, and new tests cover the
  rule and its edge cases.
- `manual/pause-to-finish.md`, `manual/typing-only-your-own-voice.md`,
  `manual/for-developers.md`: say that segments are pasted with a space between them,
  and that speech-session frames carry one segment each with nothing added.
- No dependency, configuration, or platform change. Every paste method (`wtype`,
  `xdotool`, `ydotool`, Windows `SendInput`, macOS) receives the same text, because
  all of them paste the clipboard.
- Interaction with the open `add-speaker-recognition` change: none of its files are
  edited. Its requirements that a labelled transcript begins with a label and stays
  on one line still hold, because the space is put between deliveries and is not
  part of the transcript.
