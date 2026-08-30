# Seeing your words as you speak

Murmly can show you a running guess of what it heard while you are still
speaking. This is one setting:
[`stt.live_transcribe`](settings.md#stt-live-transcribe) shows partial
transcripts in a panel below the recording indicator while you speak. See
[Using murmly](using-murmly.md) for what the overlay looks like while you
record.

## It cannot change what gets typed

!!! note
    Partials are feedback only: they never reach the clipboard or your
    application, and the transcript murmly delivers is always produced by a
    fresh pass over the complete recording. Enabling live transcription
    cannot change what gets typed.

## Anyone watching your screen can read it

With `stt.live_transcribe` enabled, partial transcripts are visible in the
overlay while you speak, which means they are visible to anyone watching a
shared screen. It is disabled by default, the text is discarded the moment
capture stops, and it is never written to a log or a file.

## Whether it can keep up

Whether partials keep pace depends on the profile and device you use.
`murmly doctor` reports `partial_pass_ceiling_ms`, measured on your machine as
the worst case where a whole window is speech. Measured here with the default
15-second [window](settings.md#stt-live-window-seconds):

| Device | Profile | Ceiling | Verdict |
| --- | --- | --- | --- |
| CUDA `float16` | `fast` | 244 ms | keeps pace |
| CUDA `float16` | `balanced` | 319 ms | keeps pace |
| CPU `int8` | `fast` | 641 ms | keeps pace |
| CPU `int8` | `balanced` | 12364 ms | **falls behind** |

`large-v3-turbo` on CPU is roughly twelve times over the default 1000 ms
[interval](settings.md#stt-live-interval-ms). Murmly skips ticks rather than
queuing them, so the partial display simply updates rarely. Silence detection
is unaffected: it runs on its own thread, so a slow partial pass cannot delay
auto-transcribe — see
[Finishing a recording by pausing](pause-to-finish.md).

One cost does remain. A partial pass already inside the model cannot be
cancelled, and the model decodes one request at a time, so stopping a
recording can wait for an in-flight pass to finish before the final
transcription starts — bounded by one pass, which is the ceiling in the table
above.

On CPU, pair live transcription with the
[`fast`](settings.md#stt-model-profile) profile — see
[Speed, memory, and your graphics card](speed-and-memory.md) for what each
profile costs.

---

For how murmly decides a recording is finished, see
[Finishing a recording by pausing](pause-to-finish.md). For memory and speed
across profiles and devices, see
[Speed, memory, and your graphics card](speed-and-memory.md).
