## Why

Murmly's speech stutters, and it is worst when something else is already playing.
Measured on the reporting machine: playing 8 seconds of audio through the shipped
output settings while another stream was running took 17.5 seconds of wall clock
and raised 206 device underflow flags. The same audio through a larger output
buffer took 8.00 seconds and raised none.

The cause is that Murmly asks the audio host for the largest buffer it advertises
and that buffer is smaller than one cycle of the audio graph it is playing into.
`SoundDevicePlayer` opens its stream with `sounddevice`'s default `latency='high'`,
which on this machine resolves to a 34.7 ms ring served in 8.7 ms periods, while
PipeWire runs a 42.7 ms cycle. A stream whose entire buffer is shorter than one
cycle cannot survive a cycle: the device drains it, underflows, and stalls while it
recovers. A bracket across buffer sizes puts the cliff exactly at the cycle length —
40 ms still stalls, 45 ms is clean — which is what identifies the cause rather than
merely correlating with it.

Nothing reports this. `SoundDevicePlayer` counts underruns and no command reads the
counter, so a person hearing the fault has nothing to look at and the report arrives
as "it sounds horrible".

## What Changes

- Murmly opens its speech output stream with a buffer that is explicitly sized to
  survive the host's scheduling period, rather than accepting whatever the host
  advertises as its largest. The device's advertised high latency becomes a floor to
  raise, not the value to use.
- The negotiated output buffer joins the device and rate that diagnostics already
  report, and the count of playback dropouts since the daemon started is reported
  alongside it. A person who hears stuttering can then see whether the device is
  underflowing, and see the buffer that decides it.
- Murmly stops reporting that everything was heard at the moment the last audio
  reaches the output device, and reports it once that audio has been played instead.
  A sender closes its session on that report, and closing discards whatever the device
  has not played yet — so on today's small buffer this quietly clips a few
  milliseconds, and on a deeper one it would clip the end of the last sentence. The
  deeper buffer is only safe once the report means what it says.
- Interruption stays immediate. A larger buffer means more audio has been handed to
  the device when the person barges in, and the abort path already discards it
  rather than draining, but the change is only safe if that stays true, so it is
  verified rather than assumed.
- The reported heard-position stays no finer than a piece of text, which is what the
  existing spec already requires precisely because audio handed to the device is
  heard after Murmly stops sending it. A deeper buffer widens that gap; it does not
  change the guarantee.

Not in scope: the per-chunk byte slicing in the playback callback. It is O(n²) in
chunk length and worth cleaning up, but it was measured at roughly 0.1 % of the
callback's budget here and it is identical in the runs that fail and the runs that
pass, so it is not this fault and fixing it would not close this report.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `speech-output`: a new requirement that speech plays without dropouts against an
  audio host whose scheduling period Murmly does not control, and the existing
  diagnostics requirement gains the negotiated output buffer and the dropout count.

## Impact

- `src/murmly/audio.py` — `SoundDevicePlayer._open` and the candidate preflight
  choose and record the buffer; the playback callback keeps the dropout count it
  already keeps and exposes it unchanged.
- `src/murmly/cli.py` — the speech section of `murmly doctor` gains two fields.
- `tests/test_audio.py`, `tests/test_cli.py` — cover the buffer floor, the fallback
  when a host refuses it, and the two new report fields.
- No configuration change and no dependency change. Assumption recorded: "audio
  already playing" is read as another application's stream, which is what the
  measurements varied; the announcement chime is not it, because the hook waits for
  the chime subprocess to exit before it sends any text to speak.
