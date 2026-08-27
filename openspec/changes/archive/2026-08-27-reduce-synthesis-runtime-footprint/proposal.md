## Why

Speech output runs on `CUDAExecutionProvider`, and the memory that choice costs
never comes back. Measured on this machine (RTX 3080 Laptop, 16 GiB, kokoro-v1.0,
reproduced across two runs), with the daemon holding 3340 MiB of GPU and 1812 MiB
of host RSS at 0% utilisation:

| | host RSS held after the session is destroyed | GPU |
| --- | --- | --- |
| `CUDAExecutionProvider` | **876 MiB** | **1208 MiB** |
| `CPUExecutionProvider` | 65 MiB | 0 MiB |

Destroying the ONNX session returns only 186 MiB of the CUDA path's 1085 MiB; `gc`
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
runs a start-up probe costing 219.3 MiB of RSS and 15 idle threads before any model
is constructed; 190.1 MiB of that is the CUDA library preload, which the provider
change removes on its own because `resolve_providers` returns before reaching it.

## What Changes

- Synthesis gets its own device setting, `[tts] device`, taking the same
  `auto | cpu | cuda` vocabulary as `[stt] device` and independent of it.
- The new setting **defaults to `cpu`**, so synthesis moves off the accelerator on
  upgrade for the installs listed under "Who this changes" below. The first word of
  a speech session arrives roughly 200 ms later. Nothing else about speech output
  changes: same voice, same audio, same sentence pacing, same failure handling.
- Synthesis sessions are constructed with an explicit `SessionOptions` that
  disables the ONNX Runtime CPU arena, bounding the working set at 510 MiB rather
  than letting it grow to 784 MiB. Warm latency is essentially unchanged: 261 ms
  against 257 ms on a short sentence, 1537 ms against 1487 ms on 8.02 s of audio.
- The intra-op thread count is deliberately left at the runtime default. Capping
  it to 4 does save a further 41 MiB, but costs **+54% on a short sentence**
  (401 ms against 261 ms) and **+36% on 8.02 s of audio** (2083 ms against
  1537 ms). The short sentence is what a listener waits through before the first
  word, and 41 MiB does not pay for it.
- Diagnostics report which processor synthesis is using and which was configured.
- As a consequence of the default, the start-up probe no longer preloads the CUDA
  libraries, dropping 190.1 MiB from every daemon start with speech output enabled.
  The probe otherwise stays exactly as eager as it is today, so speech
  unavailability is still reported with a reason at start-up rather than at first
  use.

### Who this changes

Synthesis reads `[stt] device` today, so who is affected is decided entirely by
that value and whether speech output is on:

| `[tts] enabled` | `[stt] device` | accelerator usable | affected? |
| --- | --- | --- | --- |
| `false` (the default) | any | any | **No.** `KokoroSynthesizer` is never constructed |
| `true` | `cpu` | any | **No.** Synthesis is already on the CPU |
| `true` | `auto` | no | **No.** Resolution already falls back to the CPU |
| `true` | `cuda` | yes | **Yes** |
| `true` | `auto` | yes | **Yes** |

A stock install is in the first row.

### Restoring today's behaviour

The rule is mechanical: **set `[tts] device` to whatever `[stt] device` is set to.**
That reproduces today's resolution exactly for every configuration, because today
synthesis resolves from that value. `cuda` for a pinned accelerator, `auto` for
today's fall-back-if-absent resolution.

Measured across two runs each, `[tts] device = "cuda"` against today:

| | today | restored |
| --- | --- | --- |
| accelerator memory | 1208 / 1216 MiB | 1208 / 1208 MiB |
| warm latency, 8.02 s of audio | 207 / 209 ms | 195 / 199 ms |
| real-time factor | 0.026 / 0.026 | 0.024 / 0.025 |
| system memory after 6 utterances | 1271.0 / 1234.8 MiB | 1240.3 / 1240.5 MiB |
| provider read back off the session | `CUDAExecutionProvider` | `CUDAExecutionProvider` |

Restored is indistinguishable from today: the run-to-run spread within "today"
(1271.0 against 1234.8 MiB) is wider than the gap between the two columns. The new
`SessionOptions` is confirmed inert on the accelerator path — it governs the CPU
arena, and per-sentence latency was 51–112 ms with it against 55–116 ms without.

Two preconditions, neither of them new:

- The GPU build of ONNX Runtime must still be installed. If a `uv sync` or
  `uv run --extra` has reinstalled the CPU build over it, asking for `cuda` falls
  back to the CPU with the existing logged remedy. That is true of today's
  behaviour too — see `docs/agent-notes/onnxruntime-gpu-cuda-version.md`.
- The daemon must be restarted, as with every other setting.

### Deliberately not in scope

Deferring the `import onnxruntime` in the start-up probe. It costs 32.2 MiB and 15
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
