## Context

See proposal.md — Why for the fault and the measurements. What matters here is the
shape of the mechanism, because it determines what a fix has to be a function of.

`SoundDevicePlayer.start` walks candidate devices, channel counts and sample rates,
preflighting each with `check_output_settings` and opening the first combination
that takes. `_open` constructs `sd.RawOutputStream` with no `latency` and no
`blocksize`, so both come from `sounddevice`'s defaults: `latency='high'`, which
PortAudio resolves to the device's advertised `default_high_output_latency`, and
`blocksize=0`, which lets the host choose the period.

On the reporting machine those defaults resolve to a 34.67 ms ring served in 208
frame periods at 24 kHz. The audio graph underneath runs a 42.67 ms cycle (PipeWire
quantum 2048 at 48 kHz). The ring is shorter than the cycle, so it cannot hold enough
audio to cover the interval between the graph's visits to it.

The two numbers are not independent, which is what makes the fault self-inflicted
rather than bad luck. PipeWire sizes its cycle from the buffer the client asks for
and rounds up: 34.67 ms is 1664 frames at 48 kHz, which rounds to the graph's 2048
maximum. PortAudio then subdivides that same 34.67 ms into four periods of its own.
So Murmly asks for a buffer, the graph rounds it *up* into a cycle longer than the
buffer, and Murmly is left holding less than one cycle of audio. Asking for a smaller
buffer does not escape this — the graph follows it down, measured at task 1.4 — and
asking for a larger one does, because the graph stops at 2048 while the buffer keeps
growing. That is why the fix is a floor and why the threshold is sharp: any ring at
or above the graph's maximum cycle works, and 42.67 ms is that maximum here.

The measured bracket is what makes this a cause rather than a correlation. With a
competing stream and 8 s of audio:

| Stream buffer | Device dropouts | Wall clock |
| --- | --- | --- |
| 34.67 ms (shipped) | 206 | 17.54 s |
| 34.67 ms (shipped, repeat) | 196 | 16.67 s |
| 40 ms | 64 | 10.93 s |
| **42.67 ms — one graph cycle** | | |
| 45 ms | 0 | 8.00 s |
| 50 ms | 0 | 7.99 s |
| 200 ms | 0 | 7.96 s |

The cliff sits at the cycle length, not at some size that merely happened to work.

Three things the same harness ruled out, so the design does not have to address
them: the producer never starved (audio was pre-generated and always ready, and the
queue was found empty at most five times per run, all of them the tail drain);
resampling never runs, because the stream opens at the synthesizer's own 24 kHz; and
the per-chunk byte slicing in the callback costs about 0.1 % of the callback's
budget and is identical in the passing and failing runs.

## Goals / Non-Goals

**Goals:**

- Size the output buffer from something that survives an audio host whose scheduling
  period Murmly cannot see and does not control.
- Keep the device-negotiation loop's existing shape: preflight, try, fall back, and
  report one combined failure only when every candidate is exhausted.
- Make the fault visible after the fix, so a recurrence on a different host arrives
  as a number rather than as a description of a sound.

**Non-Goals:**

- Reading the host's scheduling period. PortAudio does not expose it, and the three
  audio stacks Murmly supports each hide it somewhere different. The design must work
  without it.
- Adopting a configuration setting for the buffer. A person who can diagnose their
  graph quantum can already set `[tts] output_device`; a person who cannot is not
  helped by another number to guess at. If diagnostics show this needs a knob on some
  host, that is a later change with evidence behind it.
- Reworking the callback's byte handling. Measured, not the cause; see Context.

## Decisions

### Ask for a fixed buffer floor, not a multiple of anything measured

Open the stream with an explicit `latency` of 200 ms, falling back to smaller values
and finally to the host's own `'high'` if a host refuses it.

It is a floor on what is *asked for*, never an assertion about what is granted.
`suggestedLatency` is a suggestion — sounddevice states that the reported value "may
differ significantly from the latency value(s) passed to `Stream()`" — and CoreAudio was
measured granting 174.8 ms for a 200 ms request. A device-backed test can therefore
assert the behaviour (audio plays in the time it occupies, nothing is dropped) and that
the buffer is far larger than any host's own default, but not that the number came back
equal to the floor.

Why a constant rather than a computed multiple: the quantity that must be covered is
the host's scheduling cycle, and nothing portable reports it. A floor generous enough
to cover any plausible cycle is therefore the only formulation that does not depend
on reading a number that is not available. 200 ms covers the 42.67 ms measured here
with room for a host configured to the 2048-frame maximum at 44.1 kHz (46.4 ms), for
Bluetooth sinks, and for a laptop under load; it is also within the range PortAudio's
WASAPI and CoreML host APIs accept without special handling.

Why `latency` rather than `blocksize`: both worked in the bracket (`blocksize=1024`
and `2048` each produced zero dropouts), but `blocksize` fixes the period in frames,
so the same constant means a different duration at every rate the device might
negotiate — and the rate is negotiated, not chosen. `latency` is expressed in
seconds, which is the unit the cycle is actually in.

Alternative considered and rejected: probing at startup by playing a short silent
buffer and measuring dropouts, then choosing a size. It measures the right thing, but
it puts a second device open and roughly a second of silence in front of the first
speech session of every daemon, and it would have to be repeated whenever the graph
changes underneath — which is exactly the event that causes the fault.

### Fall back rather than refuse

`check_output_settings` takes no latency argument, so the preferred buffer cannot be
preflighted; it can only be tried. Extend `_open` to attempt the preferred latency
first and retry the same device/channel/rate combination with progressively smaller
values, ending at the host's `'high'` — which is today's behaviour and is therefore
never worse than what ships now. The candidate loop above it is untouched.

The ordering matters: the retry belongs inside `_open`, per combination, not as a
second pass over all candidates. A host that refuses 200 ms on its best device should
use that device with a smaller buffer rather than move to a worse device to keep the
buffer.

### Report the buffer and the dropout count

`SoundDevicePlayer` already carries `sample_rate_hz`, `output_device` and `underruns`,
and `murmly doctor` already reports the first two. Add the negotiated buffer to the
same section and report `underruns` beside it.

One correction is needed while doing so: `underruns` currently counts only the frames
where PortAudio raised a status flag. The other failure — the queue being empty and
the callback writing silence — is invisible, and it is the one that would show a
synthesis problem rather than a device problem. Count it separately so the two do not
have to be told apart by guesswork. The spec asks for "playback dropouts"; report the
device count under that name and keep the starvation count available for the same
section, since they point at different halves of the pipeline.

The starvation count has to be committed rather than counted where it happens, or it
says nothing. The same shortfall — the callback asking for a period the queue cannot
fill — means two different things depending on what follows it: a gap the person heard
as a break in the words, or the last period of an utterance that simply ended. Every
healthy playback ends on one of the latter, so counting them as they occur makes the
number non-zero for playback that was perfect. A run of silent periods is therefore held
undecided and added to the count only when audio follows it, discarded when the stream
stops or is aborted, and never opened before the first audio of a session — the wait for
the first sentence is synthesis latency, which is expected, not a dropout. Measured
against a real device afterwards: a clean 4.5 s playback reports zero, and a deliberate
0.6 s gap mid-playback reports the twelve periods it occupied.

### Hold `heard_all` until the device has actually played the audio

A deeper buffer breaks the completion path, and this has to be fixed in the same
change rather than after it.

`_publish` reports everything heard when `pending_frames == 0`. That counter is
`frames_written - frames_played`, and `frames_played` advances when the callback
copies bytes *into* the device ring — so it reaches zero at the moment the last frame
is handed over, with the whole ring still unplayed. The announcement hook returns from
`wait_until_heard` on that event and leaves its `with Session()` block, the connection
closes, `_close_speech_session` calls `SpeechEngine.end()`, and `end()` calls
`player.abort()` — `Pa_AbortStream`, which discards the ring rather than draining it.

Today that discards at most 34.67 ms, which Kokoro's own trailing silence covers. At
200 ms it clips the end of the last sentence of every announcement, every time.

The fix is to make the event mean what it says: once `pending_frames` first reaches
zero, hold the report until the stream's negotiated output latency has elapsed, then
emit it. `_publish` already runs on a 20 ms poll, so this is one timestamp and one
comparison, and it needs no new thread.

Alternative considered and rejected: draining instead of aborting on a clean end —
skip `abort()` in `end()` when nothing is outstanding and let `stream.stop()` play the
ring out. It fixes the audible symptom, but it leaves `heard_all` still reported early,
so every other consumer of the event is still told the person heard something they
have not; and `Pa_StopStream` blocks until the ring drains, which would hold
`_close_speech_session` for the length of the buffer. Gating the event fixes the
meaning rather than one of its consequences.

### Leave the reported heard-position alone

`frames_played` counts frames handed to the device, and a deeper buffer means more of
them are ahead of the person's ears — up to 200 ms rather than up to 35 ms.

The existing spec already refuses to claim more than this: "The position Murmly
reports MUST be no finer than the piece of text it was given, because audio already
handed to the output device is heard after Murmly stops sending it and no finer
position is honest." Subtracting the stream's latency from the reported position
would be more precise about a quantity nobody consumes at that precision, and it
would introduce a position that can move backwards when the device restarts after an
abort. The guarantee is unchanged; the gap it was written to cover is wider.

## Risks / Trade-offs

- **Speech starts up to 200 ms later than it does now** → Real and unavoidable: the
  buffer has to fill. It is small against the synthesis latency already in front of
  the first word, and it is paid once per session rather than per sentence. If it
  turns out to be noticeable, the floor is one constant to lower — the bracket says
  45 ms was already clean here, so there is a long way down before the fault returns.

- **A host refuses 200 ms in a way that is not a clean `PortAudioError`** → The retry
  ladder ends at today's `'high'`, so the worst case is today's behaviour. Any
  exception the current `_open` already catches is caught the same way.

- **A host whose cycle is longer than 200 ms still stutters** → Not seen, and the
  graph maximum on the reporting machine is 2048 frames at 44.1 kHz, which is 46 ms.
  This is the reason the dropout count is part of the change rather than a follow-up:
  the next report of this fault should arrive with a number in it.

- **The larger buffer changes what an interruption cuts off** → `abort()` calls
  `Pa_AbortStream`, which discards rather than drains, so the audio in the buffer is
  dropped rather than played out. This is behaviour the change depends on rather than
  introduces, which is why the spec states it and tasks.md verifies it against a real
  device rather than only a fake.

## Open Questions

None. The one this design opened — whether Murmly's always-on PortAudio JACK client
was pinning the graph quantum — was answered during task 1.4 and the answer is no.
Opening a stream that asks for a 5 ms buffer moved the graph quantum from 2048 to
128 with the daemon running, so PipeWire follows the client. See Context for what it
follows, which is the part that matters.
