## 1. Reproduce and instrument before changing anything

- [x] 1.1 Stand up a repro harness that drives `SoundDevicePlayer` with pre-generated
      PCM at zero amplitude, counting PortAudio status flags and queue-empty pads
      separately, and recording each callback's arrival time and frame count. Zero
      amplitude keeps the run silent while leaving the timing behaviour intact, so
      the suite can be run repeatedly without filling the room with test tones.
- [x] 1.2 Confirm the fault on this machine: shipped settings with another stream
      playing must take substantially longer in wall clock than the audio it is
      playing, and must raise device status flags. Record the numbers.
- [x] 1.3 Confirm the threshold: sweep the requested latency across the host's
      scheduling cycle and record where dropouts reach zero. This is what tells a
      later reader whether a regression is the same fault or a different one.
- [x] 1.4 Answer design.md's open question — run the sweep with the daemon stopped
      and record whether the graph quantum changes. If Murmly's always-on PortAudio
      JACK client is pinning it, note that for a separate report; it does not change
      what is built here.

## 2. Size the output buffer

- [x] 2.1 Give `SoundDevicePlayer` a preferred output latency and a descending ladder
      of fallbacks ending at the host's own `'high'`, so the worst case is today's
      behaviour.
- [x] 2.2 Pass the latency through `_open`, retrying the ladder within one
      device/channel/rate combination before the candidate loop moves on. A host that
      refuses the preferred buffer on its best device must keep that device.
- [x] 2.3 Record the buffer the stream actually negotiated, alongside the device name
      and rate that are already recorded.
- [x] 2.4 Confirm the callback still divides by the channel count published before
      `stream.start()`, which the retry must not disturb — the existing comment in
      `_open` explains why that ordering matters.

## 3. Stop the deeper buffer cutting off the last sentence

- [x] 3.1 Hold the everything-heard report until the stream's negotiated output
      latency has elapsed since the played position first caught up with the written
      one. Without this the report fires when the last frame reaches the device, the
      sender closes on it, and the close discards audio the device had not played —
      up to a whole buffer's worth, which is the thing this change makes larger.
- [x] 3.2 Confirm the hold is derived from the buffer the stream negotiated, not from
      a constant, so a device that negotiated a smaller buffer is not made to wait for
      one it does not have.
- [x] 3.3 Confirm the hold does not delay an interruption, a session close, or a
      shutdown — it gates one report, not the playback thread.

## 4. Count both kinds of dropout

- [x] 4.1 Keep the existing device-status count, and add a separate count of periods
      the callback filled with silence because the queue was empty. They point at
      different halves of the pipeline and must not be conflated.
- [x] 4.2 Confirm neither counter is reset by an abort or a resume, since a session
      interrupted and resumed is one playback from the person's point of view.

## 5. Report it

- [x] 5.1 Add the negotiated output buffer to the speech section of `murmly doctor`,
      beside the device and rate it already reports.
- [x] 5.2 Report both dropout counts in the same section, stating zero as a number
      rather than omitting the field.
- [x] 5.3 Confirm the section still degrades the way the spec requires when the probe
      fails: the speech section reports that it could not be determined and every
      other section is still produced.

## 6. Tests

- [x] 6.1 Cover the latency ladder against a fake host: the preferred value is tried
      first, a refusal falls back within the same combination, and exhausting the
      ladder still opens a stream.
- [x] 6.2 Cover the two dropout counters independently — a period with a status flag,
      and a period with an empty queue — so a change that conflates them fails.
- [x] 6.3 Cover the two new diagnostics fields, including the zero case.
- [x] 6.4 Add a device-backed test that skips itself without a live audio session, in
      the style the suite already uses: play a known duration of silent PCM and assert
      the wall clock is close to it and the device dropout count is zero. This is the
      only test that would have caught the original fault.
- [x] 6.5 Verify against a real device that stopping speech is not slowed by the
      larger buffer: time from the stop request to silence must not grow with it.
- [x] 6.6 Cover the everything-heard hold: the report is not made until the negotiated
      buffer has elapsed, and a session that closes the instant it arrives loses none
      of the last piece of text. Without this test the deeper buffer silently trades
      stuttering for a clipped final word.
- [x] 6.7 Run the full suite with `uv run --no-sync python -m unittest discover -s tests`.

## 7. Write it down

- [x] 7.1 Record a field note under `docs/agent-notes/` naming the symptom (stuttering
      speech, worse when something else is playing), the measurement that identifies
      it (wall clock exceeding the audio's own duration), and the one-line check that
      distinguishes a device dropout from producer starvation.
- [x] 7.2 Update the speech-output documentation on the site if it describes the
      output device negotiation, so the buffer floor is not a surprise to the next
      reader.
- [x] 7.3 Run `openspec validate fix-jittery-speech-playback --strict`.
