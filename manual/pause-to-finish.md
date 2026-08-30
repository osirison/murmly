# Finishing a recording by pausing

Normally you end a recording by pressing your hotkey a second time. Murmly
can instead let a pause in your speech end it for you, so you never have to
touch the key twice. This is off by default.

The setting that controls it, `stt.auto_transcribe`, is independent of
[live transcription](words-as-you-speak.md) — either works without the other.

## The three modes

Set [`stt.auto_transcribe`](settings.md#stt-auto-transcribe) to one of three
values:

- `off` (default) — silence never ends a recording.
- `stop` — silence ends the recording exactly as pressing the hotkey would:
  capture stops, the transcript is produced and delivered, and murmly returns
  to idle.
- `continuous` — silence closes a segment, which is transcribed and delivered
  while capture keeps running for your next sentence. The session ends when
  you toggle, when a delivery is refused, or on error.

In `continuous` mode, a refused delivery ends the session rather than
continuing to record speech murmly has just shown it cannot deliver — see
[where your words go](where-your-words-go.md) for what a refused delivery is.

Two more settings tune the trigger: how long a pause has to last before it
counts as silence, and how much speech has to happen first. See
[`stt.auto_transcribe_silence_ms`](settings.md#stt-auto-transcribe-silence-ms)
and
[`stt.auto_transcribe_min_speech_ms`](settings.md#stt-auto-transcribe-min-speech-ms).

## Two things that will surprise you

**A muted microphone will not end a recording.** Silence only counts once
speech has been detected in the current recording or segment, so a muted
microphone keeps murmly listening instead of ending the recording on an empty
transcript.

**An auto-stopped recording delivers without printing.** The toggle that
started the recording already returned, so `murmly toggle` shows only the
acknowledgement that capture began. The transcript is still pasted and still
lands on your clipboard — it just never appears in command output.

## When murmly cannot use it

Detection uses the voice activity model bundled with faster-whisper and needs
a capture rate that is an integer multiple of 16 kHz — set by
[`audio.sample_rate_hz`](settings.md#audio-sample-rate-hz). On other rates,
murmly disables auto-transcribe for that session and says so in
`murmly doctor` — see [When something goes wrong](troubleshooting.md).

Next: see [where your words go](where-your-words-go.md) once a transcript has
been delivered, or [Seeing your words as you speak](words-as-you-speak.md) for
the setting this one is independent of.
