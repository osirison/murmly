## Why

Speech output runs on `CUDAExecutionProvider`, and the memory that choice costs
never comes back. Measured on this machine (RTX 3080 Laptop, 16 GiB, kokoro-v1.0,
reproduced across two runs), with the daemon holding 3340 MiB of GPU and 1812 MB
of host RSS at 0% utilisation:

| | host RSS held after the session is destroyed | GPU |
| --- | --- | --- |
| `CUDAExecutionProvider` | **876 MiB** | **1208 MiB** |
| `CPUExecutionProvider` | 65 MiB | 0 MiB |

Destroying the ONNX session returns only 186 MB of the CUDA path's 1085 MB; `gc`
and `malloc_trim` return nothing further. The CPU path returns essentially all of
it. So this memory cannot be reclaimed by the idle-release work in
`unload-idle-gpu-models` — the only way not to hold it is not to allocate it.

What makes the trade affordable is that synthesis is already streamed. Speech is
produced a sentence at a time by a producer running ahead of the loudspeaker, so
what a listener waits for is one sentence, not the whole utterance:

| sentence | audio | CUDA | CPU | producer lead on CPU |
| --- | --- | --- | --- | --- |
| "Copied to the clipboard." | 1.37 s | 56 ms | 255 ms | +1.11 s |
| "Murmly is listening." | 1.28 s | 54 ms | 260 ms | +1.02 s |
| "The transcript was delivered to the focused window." | 2.77 s | 87 ms | 461 ms | +2.31 s |
| "Speech output is running on the processor rather than the graphics card." | 4.03 s | 115 ms | 660 ms | +3.37 s |

The listener waits about 200 ms longer before the first word. Every sentence after
that is gapless: the producer finishes each one between 1.0 s and 3.4 s before the
audio it produced would finish playing, so it never falls behind at a real-time
factor of 0.185.

There is no way to ask for this today. `resolve_providers` reads the `[stt]`
device setting, and its own docstring says "there is no separate one for
synthesis" — so the only way to move synthesis to the processor also drops
transcription to CPU `int8`, which is not a trade anyone would want.

Two smaller findings come from the same measurements. The synthesis session is
constructed with no `SessionOptions` at all, which is the only session in this
dependency tree that accepts every ONNX Runtime default — faster-whisper's own
bundled VAD sets `intra_op_num_threads=1`, `inter_op_num_threads=1` and
`enable_cpu_mem_arena=False` on its session. And `KokoroSynthesizer.__init__`
runs a start-up probe costing 219.3 MB of RSS and 15 idle threads before any model
is constructed; 190.1 MB of that is the CUDA library preload, which the provider
change removes on its own because `resolve_providers` returns before reaching it.

## What Changes

- Synthesis gets its own device setting, `[tts] device`, independent of `[stt]`.
- **BREAKING for existing installs with speech output enabled**: the new setting
  defaults to the processor, so synthesis moves off the GPU on upgrade. The first
  word of a speech session arrives roughly 200 ms later; nothing else about speech
  output changes. Setting `[tts] device = "cuda"` restores today's behaviour.
- Synthesis sessions are constructed with an explicit `SessionOptions` that
  disables the ONNX Runtime CPU arena, bounding the working set at 510 MB rather
  than letting it grow to 784 MB. Warm per-sentence latency is unchanged
  (261 ms against 257 ms for a short sentence).
- The intra-op thread count is deliberately left at the runtime default. Capping
  it to 4 was measured at 2083 ms against 1537 ms for the same audio — a 36%
  latency cost for no memory saving.
- Diagnostics report which processor synthesis is using and which was configured.
- As a consequence of the default, the start-up probe no longer preloads the CUDA
  libraries, dropping 190.1 MB from every daemon start with speech output enabled.
  The probe otherwise stays exactly as eager as it is today, so speech
  unavailability is still reported with a reason at start-up rather than at first
  use.

### Deliberately not in scope

Deferring the `import onnxruntime` in the start-up probe. It costs 32.2 MB and 15
idle threads, but removing it would mean the daemon could not report why speech
output is unavailable until someone tried to speak, reversing a deliberate
existing decision. The residual is accepted.

Changing how transcription selects its device. `[stt] device` keeps its current
meaning; this change only stops synthesis from borrowing it.

## Capabilities

### New Capabilities

<!-- None. Where synthesis runs is a property of speech output, not a new
     capability, and the existing spec already owns its configuration,
     availability and diagnostics requirements. -->

### Modified Capabilities

- `speech-output`: adds a requirement fixing which processor synthesis runs on,
  how that is configured, and what happens when the configured one is
  unavailable. Extends "Voice and speech settings are configurable and bounded"
  to cover the new setting, and "Diagnostics report speech output configuration
  and availability" to report the processor in use.

## Impact

- `src/murmly/config.py` — a new bounded `[tts] device` setting, defaulting to the
  processor, validated against the same vocabulary as `[stt] device`.
- `src/murmly/tts.py` — `resolve_providers` reads the new setting instead of
  `config.device`, and the session is built with an explicit `SessionOptions`.
  The CUDA preload becomes unreachable under the default, which is the point.
- `src/murmly/cli.py` — the processor in use, and any configured value not
  honoured, in `murmly doctor`.
- `config.example.toml`, `README.md` — document the setting and the trade.
- No new dependencies. `SessionOptions` is already part of the pinned
  `onnxruntime-gpu` 1.24.4, and the CPU provider ships in the same wheel.
- Interacts with `unload-idle-gpu-models`: that change releases the synthesis
  session after an idle period, which under this default releases a CPU session
  holding no GPU memory. Its synthesis timer keeps its value — it still returns
  the host memory — but the 528 MiB of GPU it was written to reclaim is memory
  this change never allocates.
