## Why

Murmly loads the transcription model on first use and the synthesis session on
first speech, and then holds both for the life of the daemon. Neither has a
release path: `FasterWhisperTranscriber._model` and `KokoroSynthesizer._model`
are assigned once and never cleared. On this machine that is **3340 MiB of a
16 GiB GPU held indefinitely** after a single dictation, at 0% compute — a
dictation tool that is idle almost all of the time keeps a third of the memory a
game or a training run would want.

Measured here (RTX 3080 Laptop, `large-v3-turbo` float16, kokoro-onnx on
`CUDAExecutionProvider`):

| | Holds | Reclaimable | Cost to restore |
| --- | --- | --- | --- |
| Transcription (CTranslate2) | ~2245 MiB | **2080 MiB** | **0.28 s** |
| Synthesis (ONNX Runtime) | ~701 MiB | **528 MiB** | **0.80 s** |

The reclaim is cheap enough to be invisible. CTranslate2 exposes
`unload_model(to_cpu=True)`, which returns the GPU memory while keeping the
weights in system RAM, so the next dictation pays 0.28 s — and that 0.28 s can be
hidden entirely by warming on record-start, while the user is still speaking.

## What Changes

- Murmly releases the transcription model's accelerator memory after a
  configurable idle period, and reloads it on next use.
- Murmly releases the synthesis session's accelerator memory after its own,
  separately configurable, idle period.
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
opt-in. Defaulting them on would reclaim ~2.6 GB for every user without their
asking, at the cost of a reload on the first dictation after a gap. Decide before
implementation.

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
