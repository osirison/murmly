## Context

See proposal.md, Why. This section covers only how segments reach the window today.

- **Where segments are produced.** In continuous mode a qualifying silence calls
  `MurmlyDaemon._deliver_segment`, which runs `_run_segment` on its own thread. That
  takes the segment's audio with `SpeechSession.take_segment`, records its target, and
  calls `_process_recording`. The toggle that ends the session stops capture and
  delivers the remaining audio as a final segment through `_finish_toggle`, which
  calls the same `_process_recording`. Both run under `_unit_lock`, so segments and
  the final delivery are already strictly ordered.
- **Where they are delivered.** `_process_recording` picks the target by capture
  destination. Window-bound capture goes to `SpeechSession.process_recording`, which
  either pastes with `copy_and_paste(text)` or, when the target check refuses, puts
  `text` on the clipboard with `copy(text)`. Capture bound for a speech session goes
  to `process_for_session`, which sends `{"event": "transcript", "text": text}` as one
  frame per segment and falls back to the clipboard when the session is gone.
- **Every paste method pastes the clipboard.** `wtype`, `xdotool` and `ydotool` on
  Linux, `SendInput` on Windows and the macOS injector all send Ctrl+V (or Cmd+V)
  after the text is copied. None of them types characters. `wl-copy` and `xclip` are
  run without options that trim. Whatever string is copied is the string pasted.
- **What a segment's text looks like.** Every path that produces it strips it: the
  plain decode joins Whisper's segments with one space and strips the result,
  speaker attribution strips each part and folds line breaks into a space, and the
  attribution fallback strips the joined text. Labelled text begins with a label.
  Transcription is fixed to English. A delivered segment therefore never starts or
  ends with whitespace and never holds a line break.
- **What is already separated.** `_segments` holds the text of each segment that
  produced a transcript, reset when capture starts. A segment that produced none,
  including one mine-only emptied, is never appended. `_session_response` builds the
  toggle response's `text` as `" ".join(...)` over `_segments` and the final
  transcript. So the response already reads correctly, and only the pasted text runs
  together.
- **Speaker recognition is still an open change.** `add-speaker-recognition` is
  merged but not archived, so `speaker-recognition` is not a baseline capability and
  this change cannot hold a delta against it. Its requirements that every delivered
  labelled transcript begins with a label and stays on one line must stay true.

## Goals / Non-Goals

**Goals:**

- One space between consecutive pasted segments of a continuous session, in every
  speaker mode, with no stray or doubled space.
- No change to any transcript Murmly reports or sends anywhere other than the window
  paste and the clipboard fallback for that paste.

**Non-Goals:**

- Separating two separate recordings (toggle or stop mode). Each starts a new
  capture session. Murmly cannot see the text before the cursor, and the user may
  have moved it on purpose.
- A space before the first segment of a session, for the same reason.
- Changing the speech-session protocol (see Decisions, and Open questions 1).
- Detecting a moved cursor or a different window when focus cannot be checked (see
  Open questions 2).

## Decisions

### The space goes before a segment, not after

A segment after the first one that produced a transcript gets a single leading
space. The first gets nothing.

*Alternative considered.* A trailing space after every segment. When a segment is
pasted, Murmly does not know whether another will follow, so the last segment of
every session would leave a space dangling at the end of the user's text. A
leading space is added only when there is already a segment to separate from.

*Alternative considered.* A line break between segments. Murmly has never pasted a
line break. The speaker-recognition design rejected them, because a terminal
without bracketed paste would run each line as a command. A space is what Murmly
already uses between the parts of one transcript.

*Alternative considered.* Injecting a separate space keystroke before the paste.
That is a second injection with its own failure modes, and not every method can
type. Putting the space in the copied text keeps one paste and works the same way
on every platform.

### The rule is "an earlier segment of this capture produced a transcript"

The daemon adds the space when `_segments` is non-empty at the moment the segment
is delivered. This needs no new state:

- `_segments` is reset when capture starts, so the first segment of every session
  gets nothing, and so does every toggle or stop-mode recording.
- A segment with no transcript is never appended, whether its decode was empty,
  label-everyone found no speech in it, or mine-only dropped every part. Such a
  segment pastes nothing and adds no space, and the next segment still gets one.
- `_segments` is read under `_lock` while `_unit_lock` is held, which both
  `_run_segment` and the final delivery in the toggle path already take. The answer
  cannot change between reading it and pasting.

A refused segment ends the session, so in every ordinary case a non-empty
`_segments` means the earlier segments were pasted. The one exception is a race
that the existing tests already cover: a toggle that is waiting on `_unit_lock` when
a segment is refused still delivers its final segment. That final segment gets a
space even though the segment before it went to the clipboard. This is accepted.
The overlay shows an error for that session anyway, and the space is what the user
needs if they paste the refused segment back in by hand.

*Alternative considered.* A separate flag set only when a segment is actually
pasted. It differs from `_segments` only in the race above, and it adds state that
has to be reset and locked in step with `_segments`.

### The space is added at the paste, and the transcript stays bare

`SpeechSession.process_recording` gains a keyword argument, `separator`, defaulting
to `""`. It forms `separator + text` once and uses that string for both
`copy_and_paste` and the refusal `copy`. The `ProcessingResult.text` it returns is
still the bare transcript. The daemon passes `" "` for window-bound capture when the
rule above holds, and `""` otherwise. It does so from both `_run_segment` and
`_finish_toggle`. `_finish_auto_stop` never passes it. `process_for_session` is not
given a separator.

This keeps `_segments`, the toggle response and the logs exactly as they are. If
the space went into `result.text`, `_session_response` would join `" second"` with
another space and report double spaces. It also keeps the open speaker-recognition
change's requirements true: the labelled transcript still begins with its label and
holds no line break, and the space is something Murmly puts between two deliveries.

### No trimming or "already has whitespace" check

Every source strips the text, and the only language is English (see Context). An
unconditional single space is therefore correct, and a check for existing
whitespace would only cover a case that cannot happen. A test asserts that the
pasted text never holds two spaces in a row. If a future text source stops
stripping, that test fails instead of a document quietly gaining double spaces.

### The speech-session target is unchanged

Continuous capture bound for a speech session sends each segment as its own
`transcript` frame. The frame boundary already separates segments. Nothing in this
repository reads those frames and joins them, and the developer manual documents
`text` as "what the person said". Adding a leading space would change the text a
caller compares, displays or forwards as a prompt, to solve a problem a frame-based
caller does not have. This is Open question 1.

### A refused segment keeps the space on the clipboard

When a later segment is refused, or cannot be injected, the clipboard holds
`" " + transcript`, the text that would have been pasted:

- `copy_and_paste` copies before it injects, on all three platform implementations.
  When injection is unavailable or fails, the spaced text is already on the
  clipboard. Leaving the bare transcript there instead would mean copying a second
  time after every failed injection.
- Unconfirmable methods (`xdotool` on KDE Wayland, `SendInput` on Windows) never
  restore the clipboard after a paste, so they leave the spaced text there anyway.
  "Spaced" is the only answer that is the same for all four ways a segment can end
  up on the clipboard.
- After a refusal the user most likely pastes the segment back where they were
  dictating, after the earlier segments, where the space is wanted.

The baseline's "the transcript is placed on the clipboard" is read as including
this separator for a later continuous segment. The new requirement says so
explicitly. This is Open question 3.

### Edge cases

| Case | Answer |
| --- | --- |
| First segment of a session | No space. |
| Segment after the first one that produced a transcript | One space before it. |
| Final segment delivered by the toggle | Same rule as a segment closed by a pause. |
| Empty segment, mine-only dropped everything, label-everyone found only silence | Nothing pasted, no space; the next pasted segment gets exactly one space. |
| Segments before the first transcript yielded nothing | The first segment with a transcript gets no space. |
| Text already starting or ending with whitespace, or holding a newline | Cannot happen: every source strips, and line breaks are folded. One unconditional space. |
| Recording with auto-transcribe off or stop, even straight after another | Unchanged; no space. |
| Focus moves to another window, verification on and focus observable | Already refused and the session ends, so no later segment reaches the other window. |
| Focus moves, but verification is off or focus cannot be observed (Wayland, X11 without EWMH) | The space is still added; Murmly cannot tell. Open question 2. |
| User moves the cursor inside the same window, for example to a new line | The space is still added; Murmly cannot see the cursor. Open question 2. |
| Speech-session target | Unchanged; one frame per segment, nothing added. Open question 1. |
| Refused or uninjectable later segment | Clipboard holds the spaced text. Open question 3. |
| Paste methods (`wtype`, `xdotool`, `ydotool`, `SendInput`, macOS) | All paste the same clipboard text; no method-specific code. |
| Toggle response `text` | Unchanged; segments joined by one space, never two. |
| Overlay, logs, partial text | Unchanged; none of them carry the pasted text. |

## Risks / Trade-offs

- [A leading space lands where it is not wanted: at the start of a new line the user
  began mid-session, or in another window when focus cannot be checked] → This is
  one visible character, and Murmly cannot see the cursor. The manual says it
  happens. Open question 2 asks whether that is acceptable.
- [An application trims leading whitespace from pasted text, so segments still run
  together there] → Out of Murmly's reach. Murmly pastes the space; what the
  application does with it is up to the application.
- [The clipboard keeps a leading space after the session on platforms that never
  restore it (KDE Wayland, Windows)] → A manual paste elsewhere starts with a space.
  This is minor, and it follows from the existing rule that an unconfirmed transcript
  is never restored over.
- [Session doubles in the tests do not accept the new argument] → Every double that
  defines `process_recording` is updated in the same task as the daemon change (see
  tasks.md). A double that is missed fails with a `TypeError`, not silently.

## Migration Plan

None. No configuration, response field or stored data changes. Rolling back is
reverting the commit.

## Open Questions

These need the user's answer. Each recommendation below is what the spec and tasks
assume. A different answer changes one scenario and one or two tests, not the
approach.

1. **Should speech-session frames get the space too?** Recommendation: no. Each
   segment is already its own `transcript` frame, and a caller that joins frames can
   choose its own separator. Adding a leading space changes the text every caller
   receives.
2. **Is a stray leading space acceptable when Murmly cannot know what is before the
   cursor?** This covers the user moving to a new line or clicking elsewhere
   mid-session, and focus moving to another window on a session where focus cannot
   be checked. Recommendation: yes. The alternative is no space at all, which is the
   bug, or comparing each segment's recorded window with the previous one. That
   comparison helps only when verification is switched off on a session that can
   observe focus, and it does nothing for a moved cursor or for KDE Wayland, where
   focus cannot be observed.
3. **Should a refused segment's clipboard copy include the space?** Recommendation:
   yes, for the reasons under Decisions. The alternative, the bare transcript, needs
   a second clipboard write after a failed injection, and it still cannot cover
   unconfirmable methods.
