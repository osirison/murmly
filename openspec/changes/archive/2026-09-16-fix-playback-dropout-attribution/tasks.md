## 1. Reproduce before changing anything

- [x] 1.1 Drive `SoundDevicePlayer` by hand through its device callback to
      reproduce a previous piece's trailing audio still draining when the next
      piece's `expect_audio(True)` lands, and confirm it counts the next
      piece's ordinary synthesis latency as the producer falling behind.
- [x] 1.2 Confirm a control run with no leftover residue at the same boundary
      reports zero, which is what shows the boundary itself is the trigger.

## 2. Stop a piece's own tail standing in for the next piece's first sound

- [x] 2.1 Record the frame count already written to the device at the moment
      a piece begins, alongside whether a producer is working on it.
- [x] 2.2 Credit a piece with having played only once the played position has
      advanced strictly past that mark, so a period that only drains the
      previous piece's tail cannot arm the count.
- [x] 2.3 Confirm a genuine gap inside one piece's own playback is still
      counted, including when that same piece's own boundary with the piece
      before it was a seamless handoff with no gap in the queue at all — the
      case that would defeat an empty-queue-based alternative.

## 3. Remove the unlocked read/write pattern

- [x] 3.1 Make the playback callback the sole reader and writer of the run of
      silent periods and the have-we-heard-from-this-piece flag.
- [x] 3.2 Publish whether a producer is working on the current piece, and the
      watermark above, as one signal the callback reads without a lock and
      resyncs to when it changes.
- [x] 3.3 Serialize the signal's two publishers — the piece-boundary path and
      the interruption path, which can run on different threads — on the
      existing write lock, without ever taking that lock in the callback.
- [x] 3.4 Confirm the callback still takes no lock at all, by the same means
      the existing no-lock-in-the-callback test already uses.

## 4. Tests

- [x] 4.1 Cover the boundary case from the reproduction: back-to-back pieces
      with the first still draining report zero.
- [x] 4.2 Cover a genuine gap that still counts after a seamless handoff from
      the previous piece, so the fix cannot be mistaken for disabling the
      count.
- [x] 4.3 Cover that both publishers of the shared signal serialize on the
      write lock, and that the callback still does not.
- [x] 4.4 Run the full suite with
      `uv run --no-sync python -m unittest discover -s tests`.

## 5. Spec and write-up

- [x] 5.1 Add the attribution rule to the `speech-output` diagnostics
      requirement: the "producer fell behind" count charges only silence
      between two sounds of the same piece of text, never a sender's pause
      between pieces or the tail of the piece spoken before.
- [x] 5.2 Run `openspec validate fix-playback-dropout-attribution --strict`.
