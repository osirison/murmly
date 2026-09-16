## Why

`murmly doctor` and `murmly status` report a count of playback periods a
producer failed to keep up with — the half of the dropout report that says
synthesis was too slow, distinct from the half that says the output device was
not fed in time. It exists so a person hearing choppy speech can tell "the
synthesizer is slow" from "the device was starved" from "nothing is wrong, that
is just the gap between sentences" — and it currently gets the third case
wrong at a sentence boundary.

Reproduced directly against `SoundDevicePlayer`: two sentences of one reply,
spoken back to back. The first sentence's own trailing audio is still queued —
routine now that the output buffer floor is 200 ms — when the second
sentence's turn begins. The device callback that drains that leftover has no
way to tell it apart from the second sentence's own first sound, so it credits
the second sentence with having already played something. The silence that
follows — the second sentence's ordinary synthesis latency, before its own
audio exists at all — then satisfies the rule "something has played for this
piece and it has gone quiet again," and gets counted as the producer falling
behind. In the reproduction this produced 5 falsely counted periods for
ordinary think-time that never happened again once the boundary case was
fixed; a control run with no leftover residue at the boundary reported 0 in
both cases, which is what confirms the boundary itself is the trigger and not
some other change in behaviour.

A second, narrower fault sits in the same bookkeeping. The fields that decide
this — whether a producer is working on the piece now playing, whether that
piece has been heard from yet, and the run of silence not yet charged either
way — are written from whichever thread declares or cuts off a piece of text,
and read and written from the audio device's own callback thread, with nothing
serializing the two. A callback landing between two writes on the other thread
can see one of the fields updated and not the other, and miscount a period at
the same kind of boundary for a different reason. It is the same fields and
the same user-visible consequence, so it belongs in the same change.

Neither fault changes what is played. Both are diagnostics-only: the count
`murmly doctor` and `murmly status` report, not the audio a person hears.

## What Changes

- The count of periods a producer failed to keep up with no longer credits a
  piece of text with having played something until the played position has
  actually advanced into audio that piece produced — not audio still leaving
  the device from the piece spoken immediately before it. Two pieces of text
  following one another with no pause between them, or with the first piece's
  tail still draining when the second begins, is routine and is not counted
  against the second piece's producer.
- A genuine gap inside one piece's own playback is still counted exactly as it
  is today; this narrows what counts as evidence of the gap, it does not
  loosen when a gap that did occur gets reported.
- The bookkeeping behind this count is restructured so the audio device's
  callback thread owns the fields it decides the count from outright, and the
  thread that starts or ends a piece of text publishes one signal for the
  callback to notice, rather than the two sides sharing fields with no lock
  between them. The callback still takes no lock of its own; it must stay
  cheap enough to run inside the device's own timing budget.

Not in scope: anything about the device-fed half of the dropout report, which
this does not touch, and any change to the audio that plays or its timing —
see `speech-stutters-output-buffer-shorter-than-a-graph-cycle` for that half
of the system, which this builds on rather than revisits.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `speech-output`: the diagnostics requirement gains an attribution rule for
  the "producer fell behind" half of the playback dropout count — it counts
  silence only between two sounds of the same piece of text, never a sender's
  ordinary pause between pieces or the tail of the piece spoken before.

## Impact

- `src/murmly/audio.py` — `SoundDevicePlayer.expect_audio`, `_forget_silent_run`
  and the playback callback change how the "producer fell behind" count
  decides what to credit; no change to `write`, `abort`, or anything that
  reaches the device.
- `tests/test_audio.py` — regression coverage for the boundary case, a check
  that a genuine mid-piece gap is still counted, and a check that the two
  threads that write the shared signal serialize on the existing write lock
  while the callback still takes none.
- No configuration change, no change to any command's response shape: the
  same two counts are reported, with one of them corrected.
