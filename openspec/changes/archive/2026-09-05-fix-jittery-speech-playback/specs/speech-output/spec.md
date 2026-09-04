## ADDED Requirements

### Requirement: Speech plays without dropouts against a busy audio host

Murmly SHALL open its speech output with a buffer large enough to survive the
scheduling period of the audio host it is playing into, and MUST NOT accept the
host's advertised largest buffer as sufficient on its own. A host that advertises a
buffer shorter than one of its own scheduling cycles will underflow every cycle: the
device drains the buffer, stalls while it recovers, and the person hears stuttering
rather than speech.

Speech MUST play in the time the audio itself occupies, whether or not another
application is already playing. A passage that takes eight seconds to speak MUST
take approximately eight seconds to come out of the loudspeaker. Wall-clock time
exceeding the audio's own duration is the observable form of this fault, and is what
a test can check without listening.

Murmly MUST NOT require the buffer it prefers. A host that refuses it SHALL be
offered progressively smaller buffers down to the one it advertises, and speech
through a smaller buffer than Murmly wanted is better than a device that will not
open at all.

Enlarging the buffer MUST NOT delay stopping. When speech is stopped, audio already
handed to the device MUST be discarded rather than drained, so the time between the
person asking for silence and getting it does not grow with the buffer.

The position Murmly reports as heard MUST remain no finer than the piece of text it
was given. A deeper buffer widens the gap between what has been handed to the device
and what has reached the person's ears; it does not change what may be claimed.

#### Scenario: Another application is already playing

- **WHEN** another application is playing audio and a session sends a passage to speak
- **THEN** the passage plays through in approximately the time the audio itself occupies
- **AND** the number of device dropouts reported for that playback is zero

#### Scenario: Nothing else is playing

- **WHEN** no other application is playing audio and a session sends the same passage
- **THEN** the passage plays through in approximately the time the audio itself occupies
- **AND** the number of device dropouts reported for that playback is zero

#### Scenario: The host refuses the preferred buffer

- **WHEN** the audio host will not open a stream with the buffer Murmly prefers
- **THEN** Murmly opens the stream with a smaller buffer the host accepts
- **AND** speech is produced rather than the session being refused

#### Scenario: Stopping is not slowed by the buffer

- **WHEN** a capture hotkey is pressed while a passage is playing through a buffer
  larger than the host's advertised default
- **THEN** speech stops without waiting for the buffered audio to play out
- **AND** the microphone opens as promptly as it did before the buffer was enlarged

### Requirement: Text reported as heard has left the loudspeaker

Murmly SHALL NOT report that everything queued has been heard until the audio for it
has left the loudspeaker, not merely until it has been handed to the output device.
A sender told that everything was heard closes its session, and closing a session
stops speech and discards audio the device has not played yet — so reporting on the
handover cuts off however much audio the device was still holding. The deeper the
output buffer, the more of the last sentence that is.

This MUST hold for whatever buffer the device negotiated, without the report
depending on a buffer of any particular size.

#### Scenario: The last sentence is not cut off by the session closing

- **WHEN** a session states it has finished sending, is told everything was heard, and
  closes its connection immediately
- **THEN** every word of the last piece of text has been played
- **AND** nothing is cut off by the close

#### Scenario: The report waits for the device, not for a fixed delay

- **WHEN** the same passage is spoken through a device that negotiated a larger output
  buffer than another
- **THEN** everything queued is still reported as heard only once it has been played
- **AND** the report is not made earlier on the device with the larger buffer

## MODIFIED Requirements

### Requirement: Diagnostics report speech output configuration and availability

`murmly doctor` SHALL report whether speech output is enabled, whether it can run,
the voice and rate in use alongside any configured values that were not honoured,
the output device it would use, the size of the output buffer that was negotiated,
the processor synthesis will run on alongside any configured processor that was not
honoured, and the quiet window in use alongside any configured window that was not
honoured, in addition to its existing sections. When speech output cannot run, the
report MUST name the remedy. Reporting the processor MUST NOT itself construct a
synthesis session.

The report SHALL also state how many playback dropouts have been recorded since the
daemon started. A person who hears stuttering speech otherwise has nothing to look
at, and the report arrives as a description of a sound rather than a count that
identifies where the fault is. A count of zero MUST be reported as such rather than
omitted, because "no dropouts" and "not measured" send an investigation to different
places.

When a quiet window is in use, the report SHALL also state whether it is in force
at the moment the report is taken. A person whose agent has gone quiet needs to
tell a window that is doing its job from a synthesizer that has stopped working,
and the configured window alone does not tell them which they are looking at.

#### Scenario: Speech output enabled and working

- **WHEN** diagnostics run with speech output enabled and able to run
- **THEN** the report states that speech output is available and names the voice, rate, output device, output buffer, and processor in use

#### Scenario: Dropouts are reported whether or not there were any

- **WHEN** diagnostics run with speech output enabled
- **THEN** the report states the number of playback dropouts recorded since the daemon started
- **AND** states it as a number when there have been none, rather than omitting the field

#### Scenario: Speech output disabled

- **WHEN** diagnostics run with speech output disabled
- **THEN** the report states that speech output is disabled

#### Scenario: Speech output enabled but unable to run

- **WHEN** diagnostics run with speech output enabled and its runtime or model files absent
- **THEN** the report states that speech output is unavailable
- **AND** names what to install or place to make it available

#### Scenario: Accelerator asked for but not available

- **WHEN** diagnostics run with the synthesis processor configured as the accelerator and that accelerator unusable
- **THEN** the report names the accelerator as configured, the CPU as in use, and the remedy

#### Scenario: One probe failing does not abandon the report

- **WHEN** the speech output probe fails unexpectedly
- **THEN** the report states that the speech section could not be determined
- **AND** every other section is still reported

#### Scenario: A quiet window is configured and currently in force

- **WHEN** diagnostics run at a local time inside the configured quiet window
- **THEN** the report names the window
- **AND** states that speech is being refused for it at this moment

#### Scenario: A quiet window is configured and not in force

- **WHEN** diagnostics run at a local time outside the configured quiet window
- **THEN** the report names the window
- **AND** states that it is not in force at this moment

#### Scenario: No quiet window is configured

- **WHEN** diagnostics run with no quiet window configured
- **THEN** the report states that no quiet window is set
- **AND** does not report speech as being refused for the time of day
