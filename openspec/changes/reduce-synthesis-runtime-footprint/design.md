## Context

See `proposal.md` — Why, for the motivation and the measurements.

The constraint that shapes everything below is that ONNX Runtime does not give
back what its CUDA provider takes. Dropping the `InferenceSession` returns 186 MiB
of the 1085 MiB the CUDA path holds; `gc.collect()` and `malloc_trim(0)` return
nothing further. The same session on the CPU provider returns 706 MiB of 784 MiB.
So for synthesis there is no release path to build — there is only a choice about
whether to allocate.

Two existing facts make the choice cheap. Synthesis is already produced one
sentence at a time by a producer thread running ahead of the loudspeaker, so a
slower processor costs a wait before the first word rather than a wait for the
whole utterance. And `resolve_providers` already returns the CPU provider before
it reaches the CUDA library preload, so a CPU default removes 190.1 MiB from daemon
start-up without any separate work.

## Goals / Non-Goals

**Goals:**

- Make the common install hold no accelerator memory for synthesis, without
  needing anyone to find a setting.
- Give synthesis a device setting of its own, so choosing one processor for
  transcription stops implying the same one for speech.
- Keep the fall back to the CPU reported rather than silent, matching how the
  existing provider resolution already behaves.

**Non-Goals:**

- Releasing synthesis accelerator memory after use. Not achievable — see Context.
  `unload-idle-gpu-models` owns idle release for the case where someone has opted
  back into the accelerator.
- Reducing the start-up probe below its remaining 32.2 MiB and 15 threads. See
  `proposal.md` — Deliberately not in scope.
- Changing how transcription resolves its device, or what `[stt] device` means.
- Speeding up CPU synthesis. The measured real-time factor is already 5.4x ahead
  of playback; the work is memory, not throughput.

## Decisions

### A setting of its own, not a rule coupling it to `[stt]`

`[tts] device` takes the same vocabulary as `[stt] device` — `auto`, `cpu`,
`cuda` — and defaults to `cpu`. `auto` reproduces exactly what synthesis does
today, so the previous behaviour stays reachable by name and not only by pinning
`cuda`.

*Alternative considered:* a conditional rule — "synthesis uses the CPU when
transcription resolves to the accelerator, and otherwise follows transcription."
Rejected, because it is the same behaviour with more machinery. When `[stt]
device` is `cpu`, `resolve_providers` already returns the CPU provider at
`tts.py:207`, so "already CPU when transcription is not on the accelerator" plus
"CPU when it is" resolves to "CPU always":

| `[stt] device` | accelerator present | today | this design | conditional rule |
| --- | --- | --- | --- | --- |
| `cpu` | either | CPU | CPU | CPU |
| `cuda` | yes | accelerator | **CPU** | **CPU** |
| `auto` | yes | accelerator | **CPU** | **CPU** |
| `auto` | no | CPU | CPU | CPU |

The four cases are identical, and the conditional costs a cross-section coupling
to specify, implement, document and test. It also reproduces in weaker form the
exact defect being fixed: synthesis reading a setting that does not name it.

### Default to the CPU rather than shipping the saving as opt-in

This is the one decision that changes behaviour on upgrade, and it is deliberate.
An opt-in default would leave 876 MiB of system memory and 1208 MiB of accelerator
memory held on every install where speech output is enabled, because a setting
nobody knows about is a setting nobody sets.

What the default costs is bounded and measured: about 200 ms more before the first
word. It does not compound, because the producer finishes each sentence between
1.0 s and 3.4 s before the audio ahead of it has played out.

*Alternative considered:* defaulting to `auto` — no upgrade change — matching how
`unload-idle-gpu-models` defaults its idle periods to `0`. Rejected because the
two situations are not alike. That change defaults off because releasing a model
mid-session is a correctness risk worth opting into. This one has no correctness
dimension: the same audio is produced either way, and the only difference a person
can detect is a fifth of a second before the first word.

### Disable the CPU memory arena, leave the thread count alone

The session is currently constructed with no `SessionOptions` at all, which is
what leaves every ONNX Runtime default in force. Two of those defaults were
measured; only one is worth changing.

| | steady RSS over 16 utterances | short sentence, warm (1.37 s audio) | long text, warm (8.02 s audio) |
| --- | --- | --- | --- |
| defaults | 452 → 784 MiB | 257 ms | 1487 ms |
| `enable_cpu_mem_arena=False` | 446 → 510 MiB | 261 ms | 1537 ms |
| also `intra_op_num_threads=4` | 451 → 469 MiB | 401 ms | 2083 ms |

Disabling the arena bounds the working set at 510 MiB instead of letting it grow to
784 MiB, for 4 ms on a short sentence and 50 ms on a long one.

Capping the intra-op pool on top does save a further 41 MiB, so the trade is real
rather than free — but it costs **+54% on a short sentence** (261 → 401 ms) and
**+36% on 8.02 s of audio** (1537 → 2083 ms). The percentage is worse on the short
sentence because the fixed per-call overhead does not scale with the work, and the
short sentence is precisely what a listener waits through before the first word.
41 MiB is not worth that, so it is not done.

This mirrors what faster-whisper's own bundled VAD already does with its session,
minus the thread caps, which it can afford because its model is 2 MB.

### The fallback path already exists; reuse it

`resolve_providers` already logs a warning and returns the CPU provider when the
accelerator is asked for and cannot be used, and `murmly doctor` already reports
providers read back off the session rather than from the module-level list. The
new setting feeds the same resolution, so the "asked for the accelerator, got the
CPU" reporting requirement needs the existing mechanism pointed at the new value,
not a new one.

The one thing that must not regress: residency and processor reporting must read
back off a constructed session where one exists, and otherwise report what
resolution would choose — never `get_available_providers()`, which advertises a
provider that a session may still fail to use. That trap is already documented in
`docs/agent-notes/onnxruntime-gpu-cuda-version.md` and guarded in the existing
code.

### Keep the start-up probe eager

Decided with the user. The probe costs 219.3 MiB and 15 threads today; 190.1 MiB of
that disappears as a consequence of the CPU default, since `resolve_providers`
returns before the CUDA preload. The remaining 32.2 MiB and 15 idle threads come
from `import onnxruntime` itself and are accepted, because deferring the import
would mean the daemon could not report why speech output is unavailable until
someone tried to speak.

## Risks / Trade-offs

- **Speech output gets slower on upgrade for anyone who enabled it** → Bounded at
  roughly 200 ms before the first word, and it does not reach a stock install at
  all: `[tts] enabled` defaults to false, and an install with `[stt] device = "cpu"`
  or no usable accelerator was already synthesising on the CPU. For the two
  configurations that do move, the reversal is one setting, mechanical to derive,
  and measured to restore today's numbers within run-to-run noise — see Migration
  Plan. Nothing else about speech changes: same voice, same audio, same sentence
  pacing, same failure handling.
- **A much slower CPU than the one measured could fall behind playback** → The
  measured margin is large (real-time factor 0.185, producer 1.0–3.4 s ahead per
  sentence), but it was measured on 16 cores. A machine with a quarter of the
  throughput would still lead playback; one with a tenth would not. The setting
  exists precisely so that machine can ask for the accelerator, and the existing
  queue already handles a producer that cannot keep up — it does not lose audio,
  it pauses between sentences.
- **All measurements come from one machine** → The numbers in `proposal.md` are
  from an RTX 3080 Laptop with 16 cores, reproduced across two runs. The decision
  does not depend on their precision: the memory difference is a factor of 13
  (876 MiB against 65 MiB), which no plausible per-machine variation reverses.
- **`enable_cpu_mem_arena=False` interacts with a future accelerator session** →
  The option is a CPU-arena setting and has no effect on accelerator allocation,
  so someone who sets `[tts] device = "cuda"` gets today's behaviour with one
  inert extra option.
- **Overlap with `unload-idle-gpu-models`** → That change releases the synthesis
  session after an idle period to reclaim 528 MiB of accelerator memory. Under
  this default there is no accelerator memory to reclaim, so its synthesis timer
  reclaims host memory instead and its measured GPU figure no longer applies to a
  default install. Neither change breaks the other; whichever lands second should
  restate the synthesis figures rather than leave a stale number in its proposal.

## Migration Plan

No migration step. The setting is absent in every existing install, which now
means the CPU, so the change takes effect on the next daemon restart.

Rollback is mechanical rather than a judgement call: **set `[tts] device` to
whatever `[stt] device` is set to.** Today synthesis resolves from that value, so
copying it across reproduces the prior resolution exactly for every configuration
— `cuda` for a pinned accelerator, `auto` for the resolve-and-fall-back behaviour,
`cpu` for an install that was already on the CPU and is therefore unaffected
anyway. See `proposal.md` — Who this changes, for which configurations move at all.

The restoration is verified, not assumed. `[tts] device = "cuda"` was measured
against today's code across two runs each: identical accelerator memory
(1208 MiB), warm latency of 195–199 ms against 207–209 ms for 8.02 s of audio, and
`CUDAExecutionProvider` read back off the session in both. The run-to-run spread
within today's own numbers is wider than the gap between the two, so the two are
indistinguishable.

That result also settles a question the `SessionOptions` raises: `enable_cpu_mem_arena`
governs the CPU allocator, but this graph places 39 Memcpy nodes and runs some
operators on the CPU even under the accelerator provider, so it was worth checking
rather than asserting. Per-sentence latency on the accelerator was 51–112 ms with
the option against 55–116 ms without. Inert, confirmed.

Two preconditions apply to rollback, neither introduced here. The GPU build of
ONNX Runtime must still be installed — if a `uv sync` or `uv run --extra` has put
the CPU build back over it, asking for `cuda` falls back to the CPU with the
existing logged remedy, exactly as it would today. And the daemon must restart, as
for any setting. This change does not alter the install instructions or let the
`onnxruntime-gpu` swap be skipped.
