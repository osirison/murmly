## Context

See proposal.md — Why for the fault and the reproduction. What matters here is
the shape of the mechanism it lives in.

`SoundDevicePlayer` decides the "producer fell behind" count from three
fields: whether a producer is working on the piece now playing (set by
`expect_audio`, called from the speech thread around each piece of text, and
by `_forget_silent_run`, called from `abort` when an interruption cuts a piece
off), whether that piece has been heard from at all, and a run of silent
periods not yet charged either way. The playback callback — PortAudio's own
thread — reads all three every period and writes two of them: it flips "heard
from" the first time it copies real audio out of the queue, and it commits or
discards the silent run.

Before this change, none of that was protected by a lock. It relied on the
producer thread's writes and the callback's reads happening to interleave
safely, which held as long as the fields it was writing to were true exactly
when the write happened. They are not, at a boundary the buffer floor made
common: the callback that copies the previous piece's own trailing audio out
of the queue cannot tell that audio apart from the new piece's first sound, so
"heard from" flips true on residue, not on anything the new piece produced.

## Goals / Non-Goals

**Goals:**

- Stop a piece of text being credited with having played until the played
  position has actually advanced into audio that piece itself produced.
- Keep a genuine gap inside one piece's own playback counted exactly as today.
- Remove the unlocked read/write pattern on the shared fields without putting
  a lock the producer thread can hold for any length of time into the
  callback's path — the callback's own timing budget is the thing the buffer
  floor exists to protect, and this change must not spend it.

**Non-Goals:**

- Anything about the device-fed half of the dropout count, or the audio that
  plays. Confirmed unaffected: the reproduction and the new tests exercise
  `SoundDevicePlayer` through the same fake device harness the rest of the
  playback suite uses, with no change to what is written to the device or
  when.
- A configuration knob. Nothing here is a value a person would choose; it is
  a bookkeeping correction.

## Decisions

### Credit a piece with having played only past a recorded watermark, not on the first byte that moves

Two ways to stop the previous piece's tail from arming "heard from" were
considered.

**Rejected: require the queue to be empty at the moment a piece begins.**
Simple in the case that motivated it, but it fails whenever the handoff
between two pieces is seamless rather than merely close — the new piece's
first chunk already queued directly behind the old piece's last one, so the
queue never goes empty at the boundary at all. A flag that waits for "the
queue went empty since this piece began" would then never arm, misjudging
everything that follows, including a real gap later in the very piece it was
supposed to be tracking. It would also have the producer thread read the
callback's queue to decide this, which is a second unlocked cross-thread read
in the same place the change is trying to remove one from.

**Chosen: record how many frames had been written to the device at the moment
a piece begins, and only credit it with having played once the played
position has advanced strictly past that mark.** The queue the callback drains
is first-in-first-out and the frame count is monotonic, so any frame whose
position is past the watermark was necessarily written at or after the piece's
own start — regardless of how much of the previous piece's tail is still
mixed into the same callback period as the new piece's own first bytes.
"Strictly past" rather than "at least" matters at the edge: a period that
exactly drains the old tail and reaches no further must not arm the count,
or the boundary case this exists to fix reappears one frame later.

This also answers the question of what happens if a write races the moment a
piece begins. It does not, by construction: the producer thread calls
`expect_audio` and then writes that piece's own chunks in sequence on itself,
never from two threads at once, so the watermark is always exactly "frames
written before this piece's own first write" with nothing else able to land
in between.

### Move ownership of the run of silent periods, and what has played, onto the callback alone

The unlocked pattern existed because two things needed to be true together —
"a producer is working on this piece" and "this piece has played" — and
whichever thread got there first left the other stale for however long the
gap between the two writes took. Rather than shrinking that gap with a lock
the callback would have to wait on, the fix removes one of the two writers.

The callback becomes the only thread that ever reads or writes the run of
silent periods and the have-we-heard-from-this-piece flag. The producer thread
(and the interruption path, which runs on whichever thread calls it) publish
one immutable signal instead — an edge number, whether a producer is working
on the current piece, and the watermark above — as a single attribute
assignment under the same lock `write()` already takes for the frame count the
watermark reads. The callback reads that one attribute with no lock at all,
compares the edge number against the last one it saw, and resyncs its own
two fields when it changed. A tuple assignment and a tuple read are each one
attribute access, so there is no window in which the callback can see a
publish half-applied — there is only "before" or "after," never a mix.

This is a stronger fix than serializing the existing fields with a lock would
have been: it removes the shared mutable state, rather than protecting it,
which is what makes the callback's own copies un-raceable by construction and
keeps the callback's cost the same one attribute read it already had a
comparable version of.

Why the interruption path shares the lock with the producer thread's
publish, even though the callback needs none: the interruption path can run
on a different thread than whichever is speaking, so the two publishers still
need to be serialized against each other — an edge published by one must not
be partly overwritten by the other. The lock already exists for exactly this
kind of cross-thread coordination and is never held across anything that
blocks, so taking it here adds no risk of the callback waiting on a producer
that is itself waiting on synthesis.

## Risks / Trade-offs

- **A watermark comparison replaces a plain flag on every period the callback
  runs.** One extra unpacked tuple and one integer comparison, on the same
  thread that already does per-sample copying every period; not a measurable
  cost against that budget.
- **The callback now resyncs two fields whenever it notices a new edge,
  discarding whatever run was pending under the old one.** This is the
  existing behaviour, made explicit: `expect_audio` already cleared the
  pending run synchronously on both edges before this change. The discard
  now happens on the callback's own next period instead of at the instant the
  other thread calls in, which only ever narrows the window in which a
  discard and a commit could race, never widens it.

## Open Questions

None. `expect_audio` and `write` are called only from the speech thread that
produces one piece of text at a time, and `abort` is the only other caller of
the publish path; nothing else in the codebase writes to the player between
declaring a piece and finishing it.
