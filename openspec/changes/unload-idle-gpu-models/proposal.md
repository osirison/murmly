## Why

Murmly loads the transcription model on first use and the synthesis session on
first speech, and then holds both for the life of the daemon. Neither has a
release path: `FasterWhisperTranscriber._model` and `KokoroSynthesizer._model`
are assigned once and never cleared. On this machine that is **3340 MiB of a
16 GiB GPU held indefinitely** after a single dictation, at 0% compute — a
dictation tool that is idle almost all of the time keeps a third of the memory a
game or a training run would want.

That 3340 MiB was measured with synthesis on `CUDAExecutionProvider`. The
`reduce-synthesis-runtime-footprint` change has since made
`[tts] device = "cpu"` the default, so on a current default install synthesis
contributes none of it and the transcription model is nearly all of what is left.
The new total was not re-measured; what changed is which row below applies.

Measured here (RTX 3080 Laptop, `large-v3-turbo` float16):

| | Holds | Reclaimable | Cost to restore |
| --- | --- | --- | --- |
| Transcription (CTranslate2) | ~2245 MiB GPU | **2080 MiB GPU** | **0.78 s** |
| Synthesis, `[tts] device = "cpu"` — the default | ~377 MiB host, **no GPU** | **377 MiB host** | **0.76 s** |
| Synthesis, `[tts] device = "cuda"` | ~701 MiB GPU | **528 MiB GPU**, 105 MiB host | **0.61 s** |

Synthesis is the row that moved. Under the default it no longer reclaims
accelerator memory, because it no longer takes any — it reclaims host memory
instead, and more of it. The timer still earns its keep; it just earns it against
a different resource than this proposal was first written against.

The reclaim is cheap enough to be invisible. CTranslate2 exposes
`unload_model(to_cpu=False)`, which frees the GPU memory outright, so the next
dictation pays 0.78 s — and that 0.78 s can be hidden entirely by warming on
record-start, while the user is still speaking.

`to_cpu=True` is the other mode, and this change does not use it. It reloads in
0.22 s rather than 0.78 s, but it buys that by keeping the weights in system RAM.
Measured on this machine across two runs, it moves **1541 MiB into host RSS**
(1316.6 MiB to 2857.9 MiB) to free the same GPU. Murmly is a daemon that is idle
almost all of the time and already holds 1812 MiB of host RSS, so trading a third
of the GPU for one and a half gigabytes of system RAM does not reduce the
footprint — it relocates it. See `design.md` — Use `unload_model(to_cpu=False)`.

## What Changes

- Murmly releases the transcription model's accelerator memory after a
  configurable idle period, and reloads it on next use.
- Murmly releases the synthesis session after its own, separately configurable,
  idle period. Under the default that returns host memory; under
  `[tts] device = "cuda"` it returns accelerator memory as well.
- Idle means **no capture is active**, not "no recent transcription". The timer
  is armed when a session ends and cancelled when capture begins, so it can never
  fire between segments of a continuous-mode session.
- Murmly begins reloading the transcription model when capture starts rather than
  waiting for the first transcription pass, so the reload overlaps with speech.
- Two new settings, `[stt] unload_after_idle_s` and `[tts] unload_after_idle_s`,
  each bounded 30–86400 seconds with its own default. `0` disables release for
  that model. **`[stt]` defaults to `300`** (five minutes, enabled);
  **`[tts]` defaults to `0`** (disabled).
- Diagnostics report whether each model is currently resident.

**This changes behaviour on upgrade for transcription.** An existing install that
sets nothing will begin releasing the transcription model's accelerator memory
after five idle minutes, and reloading it when capture next begins. Setting
`[stt] unload_after_idle_s = 0` restores today's always-resident behaviour.
Synthesis is unchanged on upgrade, because its default is `0`.

### Decision: transcription on, synthesis off

The two settings are decided separately, which the spec permits — it requires
them to be independent and never said they share a default.

**Transcription: on, at 300 s.** It reclaims 2080 MiB for every user without their
asking. With `to_cpu=False` it costs no host memory, and the 0.78 s reload is
hidden by warm-on-capture for any dictation longer than 0.78 s, which is
substantially all of them. The cycle was measured over eight iterations before
committing to this: the GPU returns to exactly 2240 MiB and releases to exactly
160 MiB every time, and host RSS drift is **+2.3 MiB, identical on cycles 1
through 8** — a one-time cost of the first unload, not per-cycle growth. Unload
55–57 ms, reload 740–771 ms, transcribe 13 ms. Stable enough to run unattended.

Five minutes is chosen because it does not fire between dictations in an active
session but does fire during any real break. The period matters less than it
would otherwise, because firing is close to free.

**Synthesis: off, at `0`.** Under the merged `[tts] device = "cpu"` default it
returns 377 MiB of host memory rather than accelerator memory, and it costs 0.76 s
of silence before speech resumes, which a listener notices and which nothing
overlaps. Speech output is already opt-in — `[tts] enabled` defaults to false — so
leaving this opt-in too is consistent. It is also the release path with measured
per-cycle drift on the accelerator, which is a further reason not to arm it by
default.

This required amending the spec, not just choosing a constant: the requirement
"Idle release is configurable and bounded" previously said an absent setting SHALL
disable release, and its scenario "Disabled by default keeps a model resident"
required an unconfigured install to keep both models resident. Both are updated in
this change, and the requirement is renamed to "Idle release is configurable,
bounded, and defaulted per model".

### Deliberately not in scope

Evicting the CUDA context itself. Roughly 165 MiB (transcription) and 173 MiB
(synthesis) do not come back from an unload, because the context and arena outlive
the model. Reclaiming those means tearing down the runtime, which is a different
and much more disruptive change.

## Capabilities

### New Capabilities

- `model-residency`: when the transcription model and synthesis session occupy
  accelerator memory, when Murmly releases it, what releasing must never disturb,
  and how residency is configured and reported.

### Modified Capabilities

<!-- None. The unload must not violate live-transcription's existing "Live
     transcription yields to capture and delivery" requirement or speech-output's
     availability requirements, but neither of those requirements changes. The new
     capability's requirements are written to be consistent with them. -->

## Impact

- `src/murmly/stt.py` — `FasterWhisperTranscriber`: a residency check that
  survives eviction, an evictor, and a warm-on-capture path. The existing
  `if self._model is None` test is not sufficient after an unload, because the
  `WhisperModel` wrapper survives while its weights leave the GPU.
- `src/murmly/tts.py` — `KokoroSynthesizer`: release and rebuild of the ONNX
  `InferenceSession`, which has no in-place unload.
- `src/murmly/config.py` — two new bounded settings alongside `lazy_load_model`.
- `src/murmly/daemon.py` — arming and cancelling the idle timers around session
  lifecycle.
- `src/murmly/cli.py` — residency in `murmly doctor` output.
- `config.example.toml`, `README.md` — document both settings and the trade.
- No new dependencies. `unload_model` / `load_model` / `model_is_loaded` are
  already present in the pinned CTranslate2 4.8.1.
