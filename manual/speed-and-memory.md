# Speed, memory, and your graphics card

Murmly holds a transcription model, and sometimes a speech model, in memory
for as long as the part of it that keeps running in the background — the
murmly service — is alive. This page is about what that costs, how fast
murmly is in exchange, and what your graphics card changes.

## How accurate murmly is, and what it costs

`stt.model_profile` (see [all the settings](settings.md#stt-model-profile))
chooses which transcription model murmly uses:

- `fast` -> `tiny.en`
- `balanced` -> `large-v3-turbo`
- `accurate` -> `large-v3`

`balanced` is the default: a middle model between the fast, less accurate
`tiny.en` and the slow, most accurate `large-v3`.

With `stt.device = "auto"` (see
[all the settings](settings.md#stt-device)), murmly uses CUDA `float16` when
a compatible graphics card and the CUDA extra are both available, and falls
back to CPU `int8` otherwise.

The first time you use a profile, murmly downloads its model; every session
after that reuses the copy on your disk. The tested CUDA runtime wheels are
about 1.8 GB, and the cached `large-v3-turbo` model — the one behind the
`balanced` default — is about 1.6 GB. The `balanced` model's revision is
pinned, so what downloads today is the same file a fresh install would
download later.

!!! note
    From your first press of the hotkey, the murmly service keeps its
    transcription model in memory — roughly 1.6 GB for the `balanced`
    profile, in graphics-card memory when running on CUDA. If that matters
    on your machine, set a smaller
    [`stt.model_profile`](settings.md#stt-model-profile).

    By default that memory is handed back after five idle minutes and taken
    again the next time it is needed. [Giving memory back when murmly is
    idle](#giving-memory-back-when-murmly-is-idle), below, is where that is
    set, and where turning it off is described.

Speech output, when you turn it on, adds its own model files: about 340 MB,
installed separately from murmly itself.

## Where speech is produced

`tts.device` (see [all the settings](settings.md#tts-device)) chooses the
processor speech output runs on — `auto`, `cpu` or `cuda`, the same
vocabulary as `stt.device` — and defaults to `cpu`. It is speech output's own
setting: `stt.device` is about transcription and does not decide where
speech is produced.

The CPU is the default because the GPU does not give back what speech output
takes from it. Measured on one machine (RTX 3080 Laptop, 16 cores,
reproduced across two runs), still held after the speech session has been
destroyed:

| `[tts] device` | system memory | GPU memory |
| --- | --- | --- |
| `cuda` | 876 MiB | 1208 MiB |
| `cpu` | 65 MiB | none |

The CPU path gives essentially all of its memory back when the session ends.
The CUDA path keeps its 876 MiB however hard it is collected or trimmed, so
the only way not to hold it is not to take it.

Measured on one machine (RTX 3080 Laptop, 16 cores, reproduced across two
runs), what the CPU costs is about 200 ms more before the first word.
Nothing after that: speech is produced at roughly five times real time, so
each sentence finishes between 1.0 and 3.4 seconds before the audio ahead of
it has played out, and every sentence past the first is gapless. Same voice, same audio,
same pacing, same failure handling, whichever processor you choose.

Set `device = "cuda"` to run speech output on the graphics card, which also
needs the GPU build of ONNX Runtime installed — see below. `auto` uses the
GPU when it is usable and the CPU when it is not. Either value is read as a
preference rather than a demand: with `cuda` set and no GPU build installed,
speech output falls back to the CPU and logs the remedy instead of refusing
to speak.

??? note "If you upgraded from an older murmly"
    Speech output used to read `stt.device` before `tts.device` existed, so
    the new `cpu` default moves speech output off the graphics card on
    upgrade for some installs, and leaves others exactly where they were:

    | `[tts] enabled` | `[stt] device` | GPU usable | moves to the CPU? |
    | --- | --- | --- | --- |
    | `false` (the default) | any | any | No — synthesis is never constructed |
    | `true` | `cpu` | any | No — already on the CPU |
    | `true` | `auto` | no | No — already falling back to the CPU |
    | `true` | `cuda` | yes | Yes |
    | `true` | `auto` | yes | Yes |

    To keep what you had, set [`tts.device`](settings.md#tts-device) to
    whatever [`stt.device`](settings.md#stt-device) is set to, and restart
    the service. That reproduces the previous resolution exactly, because
    that is the value speech output used to read: measured across two runs,
    it holds the same 1208 MiB of GPU memory at the same warm latency as
    before, and `CUDAExecutionProvider` is read back off the session.

## Running speech on your graphics card

This needs murmly already installed: Linux or Windows, Python 3.12 or newer,
and a terminal, on either one — see [what you need before you
start](what-you-need.md) and [installing murmly](install.md) for the install
itself. Neither the sync below nor the doctor check it feeds needs any
permission on either platform; the hotkey and overlay disclosures on those
two pages apply to installing murmly in the first place, not to this
follow-on step.

The command below applies on Linux and on Windows, the same command on both —
the CUDA extra and the `onnxruntime-gpu` wheel it swaps in both publish for
either platform. It does not apply to musl-based Linux or Windows on ARM64,
which have no build of the transcription runtime to begin with, and murmly
does not run on those machines at all.

The GPU build of ONNX Runtime **replaces** the CPU one rather than joining
it — both install into the same `onnxruntime` package namespace, and an
environment holding both leaves the survivor of any later uninstall broken:

```bash
uv sync --extra cuda
uv pip uninstall onnxruntime
uv pip install "onnxruntime-gpu==1.24.4"
```

The first line keeps the speech synthesizer, which is installed by default
as a dependency group. It did not always: while speech output was an extra,
that same command removed `kokoro-onnx` and left speech output unavailable,
which is the reason it is a group now.

[`murmly doctor`](troubleshooting.md) reports which execution providers
speech output resolved. Murmly reads the provider back off the session it
actually constructed rather than off the module's advertised list — that
list says CUDA even on a session that is really running on the CPU. When
speech output falls back to the CPU because the GPU build is absent, the
providers it reports say so, and the log names the remedy.

## Giving memory back when murmly is idle

Murmly loads the transcription model on your first recording — or at
startup, if you set [`stt.lazy_load_model`](settings.md#stt-lazy-load-model)
to `false` — and the speech session on your first use of speech output. Each
then sits in memory doing nothing between uses.

[`stt.unload_after_idle_s`](settings.md#stt-unload-after-idle-s) and
[`tts.unload_after_idle_s`](settings.md#tts-unload-after-idle-s) hand that
memory back once a model has gone unused for its own idle period, in
seconds, and murmly loads it again the next time it is needed. Each is
bounded 30-86400. `0` switches release off for that model, leaving it
resident once loaded. A value outside the bounds falls back to that
setting's own default rather than refusing to start — its own default, not
a shared one, because the two defaults differ.

Measured on one machine (RTX 3080 Laptop, 16 cores, reproduced across two
runs):

| Releasing | Returns | Costs |
| --- | --- | --- |
| Transcription | 2080 MiB of GPU memory | 0.78 s to reload |
| Synthesis, `[tts] device = "cpu"` — the default | 377 MiB of system memory, no GPU memory | 759-767 ms before speech resumes |
| Synthesis, `[tts] device = "cuda"` | 528 MiB of GPU memory, 105 MiB of system memory | 607-611 ms before speech resumes |

**Transcription release is on by default, at 300 seconds**, because its cost
is paid where nobody is waiting. Murmly starts reloading the model the
moment you begin capturing, rather than waiting until a transcript is
needed, so the 0.78 s runs while you are still speaking and is over before
you stop. It returns graphics-card memory, which is the resource another
process is most likely to be short of.

**Synthesis release is off by default**, because its cost is silence. There
is nothing to overlap the rebuild with: the wait falls between the moment
something asks murmly to speak and the moment it does. Under the default
`tts.device = "cpu"` it also returns system memory rather than graphics-card
memory. Speech output is opt-in already, so releasing it is too. Set
[`tts.unload_after_idle_s`](settings.md#tts-unload-after-idle-s) to a period
in seconds to turn it on.

"Idle" means no recording is active, not "no recent transcript." The
countdown starts when a recording session ends, and is abandoned the moment
the next one begins — so a `continuous` session (see
[finishing a recording by pausing](pause-to-finish.md)) is never released
while it is still running, however long you pause between sentences.
Synthesis counts idle time the same way, against speech sessions.

**[`murmly doctor`](troubleshooting.md) reports what the running service is
holding**, under `model_resident` for transcription and
`speech_output.resident` for synthesis. The models live inside the service's
own process, so the report asks the service over its command socket rather
than answering for itself — `murmly doctor` runs as its own, separate
process and holds neither model, so its own answer would wrongly be `false`
even on a machine whose service has both loaded.

Each field is `true`, `false`, or `null`. **`null` means the question could
not be answered, not that the models are idle**: no murmly service is
running, the service did not answer, or the service that answered predates
this reporting. A `model_resident_detail` field beside it names which
reason applies, and the synthesis section carries its own `resident_detail`
for the same reason. A service with `tts.enabled = false` never builds a
synthesis session at all, so it reports no synthesis residency rather than
reporting one as released.

```json
{
  "model_resident": null,
  "model_resident_detail": "No Murmly daemon is running, so what it holds could not be asked: ..."
}
```

That is the field to watch to see release actually working: transcribe
once, and `model_resident` is `true`; leave the service alone for
[`stt.unload_after_idle_s`](settings.md#stt-unload-after-idle-s) and it
becomes `false` as the memory goes back.

!!! warning
    `tts.device = "cuda"` together with a non-zero
    [`tts.unload_after_idle_s`](settings.md#tts-unload-after-idle-s) is the
    one combination worth thinking twice about. It is a trade rather than a
    saving: the 528 MiB of GPU memory does come back, but rebuilding the
    session costs a one-time 277 MiB of system memory, and then roughly 8
    MiB more on every release cycle after that — which a service cycling
    twenty times a day will feel. Neither setting's shipped default puts you
    in this combination.

!!! note "If you upgraded from an older murmly"
    This changes behaviour on upgrade. An install that configures neither
    setting begins releasing the transcription model after five idle
    minutes. No transcript changes, and in ordinary dictation there is
    nothing to wait for.
    [`stt.unload_after_idle_s`](settings.md#stt-unload-after-idle-s) set to
    `0` restores the always-resident behaviour murmly had before. Synthesis
    is unaffected, because its default is already `0`.

Restart the service after changing configuration:

```bash
systemctl --user restart murmly.service
```

For the full list of settings mentioned on this page, including every
default and range, see [all the settings](settings.md). If a figure here
does not match what you see on your own machine, start with
[when something goes wrong](troubleshooting.md).
