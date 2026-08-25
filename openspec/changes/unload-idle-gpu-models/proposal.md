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
- Two new settings, `[stt] unload_after_idle_s` and `[tts] unload_after_idle_s`.
  Absent or `0` keeps today's always-resident behaviour.
- Diagnostics report whether each model is currently resident.

Not breaking: with both settings absent, behaviour is byte-for-byte what it is
today.

### Open decision for review

The proposal assumes both settings **default to `0`, i.e. off**, so that no
existing install changes behaviour on upgrade and the memory/latency trade stays
opt-in.

**This is not a free choice at implementation time.** The spec has already
committed to it: "Idle release is configurable and bounded" states that an absent
setting SHALL disable release, and the scenario "Disabled by default keeps a model
resident" requires an unconfigured install to keep both models resident. Shipping
a non-zero default contradicts that scenario, so choosing it means amending the
spec first, not just changing a constant in `config.py`.

The two settings can be decided separately — the spec requires them to be
independent and never says they share a default:

- **Transcription.** Defaulting on reclaims 2080 MiB for every user without their
  asking. With `to_cpu=False` it costs no host memory, and the 0.78 s reload is
  hidden by warm-on-capture for any dictation longer than 0.78 s, which is
  substantially all of them. Against that, the Risks section below notes the
  feature will be under-exercised if it ships inert — which argues for on, not off.
- **Synthesis.** Defaulting on reclaims 377 MiB of host memory and costs 0.76 s of
  silence before speech resumes after a gap, which a listener notices. Speech
  output is already opt-in (`[tts] enabled` defaults to false), so leaving this
  opt-in too is the consistent choice.

Decide before implementation, and amend the spec in the same change if the answer
is non-zero.

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
