## Context

See `proposal.md` — Why, for motivation and the measurements.

Two caches, both written the same way and both permanent:

| | `src/murmly/stt.py` | `src/murmly/tts.py` |
| --- | --- | --- |
| holder | `FasterWhisperTranscriber._model` | `KokoroSynthesizer._model` |
| construction | `_load_model_locked`, guarded by `_load_lock` | `_load_model`, guarded by `_load_lock` |
| use | `_decode`, guarded by `_model_lock` | `_create`, guarded by `_model_lock` |
| release | none | none |

The two runtimes differ in a way that shapes everything below. CTranslate2 can
evict a model's device memory in place and keep the Python object valid.
ONNX Runtime cannot: memory is owned by the `InferenceSession`, and the only way
to return it is to drop the session and build another.

Measured on an RTX 3080 Laptop (see `proposal.md` for the table): the
transcription model returns 2080 MiB for a 0.78 s reload; the synthesis session
returns 528 MiB for a 0.80 s rebuild.

## Goals / Non-Goals

**Goals:**

- Release accelerator memory without any code path outside the two model holders
  needing to know a model can vanish.
- Make the reload cost invisible for the common case — the user dictates again
  after a gap.
- Keep the whole feature inert when unconfigured, so the risky paths do not exist
  in a default install.

**Non-Goals:**

- Evicting the CUDA context. See `proposal.md` — Deliberately not in scope.
- A shared eviction policy or a global memory manager across the two models. They
  have different runtimes, different costs, and different triggers.
- Unloading on memory pressure, or reacting to other processes' allocations.
  Time-based only.

## Decisions

### Use `unload_model(to_cpu=False)` for transcription, not a reference drop

CTranslate2 4.8.1 exposes `unload_model(to_cpu: bool)`, `load_model()`, and
`model_is_loaded` on the `Whisper` object reached through `WhisperModel.model`.

Both modes were measured, and they return the **same** GPU memory. Re-measured
across two runs with both models warm in one process:

| | unload | reload | host RSS held |
| --- | --- | --- | --- |
| `to_cpu=False` | **0.05 s** | 0.78 s | **none** (−6 MiB) |
| `to_cpu=True` | 0.77 s | **0.22 s** | **+1541 MiB** |

`to_cpu=False` is chosen. The reload difference — 0.78 s against 0.22 s — is paid
in the one place this design already hides it: behind warm-on-capture, while the
user is still speaking. `to_cpu=True` buys that 0.56 s by parking the weights in
host RAM, measured at 1316.6 MiB → 2857.9 MiB of RSS, reproduced exactly across two
runs.

For a daemon that is idle almost all of the time, that is not a reduction. It
moves the memory from a 16 GiB GPU into system RAM, and murmly's host RSS is
itself being reduced by separate work — so `to_cpu=True` would hand back with one
change what another is taking away. The GPU residual is the same either way
(696 MiB against 702 MiB), so nothing is lost by declining the trade.

*Alternative considered:* `to_cpu=True`. Rejected on the host-memory cost above.
The mode is a one-word design constant, so if murmly's host footprint later stops
mattering it can be revisited without touching a requirement.

*Alternative considered:* dropping the `WhisperModel` reference and letting the
allocator collect it. Rejected — it depends on garbage-collection timing for a
resource the user is watching in `nvidia-smi`, and it discards the tokenizer and
feature extractor along with the weights, making every reload a cold 1.99 s load.

### The residency check must be `model_is_loaded`, not `is None`

This is the defect most likely to ship if the change is implemented from the
proposal alone.

After `unload_model()`, `self._model` is **still a valid `WhisperModel`**. Only
CTranslate2's weights leave the device. So the existing guard:

```python
if self._model is None:          # src/murmly/stt.py:234
    self._model = WhisperModel(...)
return self._model
```

returns an evicted model, and the decode that follows fails inside CTranslate2.
The residency test is `model.model_is_loaded`, and reloading is `model.load_model()`
— not reconstruction.

### One lock, and the check belongs under it

`_transcribe` currently does:

```python
model = self._load_model()       # takes and releases _load_lock
...
with self._model_lock:           # a different lock, acquired later
    return self._decode(model, audio)
```

An evictor thread that ran between those two statements would unload a model the
caller already holds a reference to. Widening `_load_lock` does not help; the gap
is between the two acquisitions.

The fix is placement, not a new lock:

- The evictor acquires `_model_lock` before calling `unload_model()`.
- The residency re-check and any reload happen **inside** the `_model_lock` block,
  next to the decode.

`_model_lock` then serialises decode against eviction, which is the invariant that
matters. No second lock, no ordering hazard, and the "never interrupt a pass in
progress" requirement falls out of the same acquisition rather than needing its
own mechanism.

*Alternative considered:* a reader-writer lock or a use-count. Rejected as
unnecessary — decode is already serialised one-at-a-time by `_model_lock`, so
there is no concurrency for a more permissive lock to recover.

### Idle is driven by session lifecycle, not by a last-use timestamp

The timer is armed when a recording session ends and cancelled in
`begin_capture()`. A "seconds since last transcription" timestamp would fire
during a continuous auto-transcribe session, where segments are separated by
exactly the silence the timer is counting — the spec forbids that, and lifecycle
edges express it directly instead of by choosing a period longer than any pause.

Synthesis has no capture to key on; its timer is armed when a speech session ends
and cancelled when one begins.

### Warm on capture start

`begin_capture()` already exists and already runs on the recording path. Kicking an
asynchronous reload there hides the 0.78 s behind the user speaking. It must not
block: capture starting is on the critical path and the spec requires it not be
delayed.

If the user stops speaking before the reload finishes, the transcription path
waits on the same `_model_lock` and proceeds when it completes — correctness does
not depend on the warm-up winning the race, only latency does.

### Synthesis: drop and rebuild, with its own period

ONNX Runtime has no in-place unload, so releasing means dropping the
`InferenceSession` and constructing a new one. Measured: 1.02 s to drop, 528 MiB
returned, 0.80 s until speech can start again.

That ratio is much worse than transcription's — a quarter of the memory for triple
the latency — which is why the spec gives it a separate setting rather than one
shared period. A user who wants the 2 GB back without a delay before speech can
enable one and not the other.

## Risks / Trade-offs

- **A reload lands in the delivery path rather than during capture** (the user
  starts and stops faster than the warm-up completes) → The wait is bounded at
  0.78 s, and the transcript is unaffected. Acceptable; the spec permits the first
  use after a release to be slower.
- **`to_cpu=False` makes an unhidden reload 0.78 s rather than 0.22 s** → Only
  reachable when the user stops speaking before the warm-up completes. Bounded,
  and the spec permits the first use after a release to be slower. The mode is a
  design constant that can be revisited without changing any requirement.
- **An evictor firing during a long partial pass holds `_model_lock` and stalls**
  → It cannot: the evictor acquires the lock, so it waits for the pass rather than
  interrupting it. Worst case the release happens later than configured, which no
  requirement forbids.
- **A rebuild failure leaves synthesis permanently unavailable** → The reload path
  must treat failure the same way first load does, reporting it and remaining
  retryable, per the spec's "does not disable Murmly" scenario.
- **Timer threads outliving daemon shutdown** → Arming must go through whatever the
  daemon already uses for its own shutdown sequencing, so a pending timer cannot
  extend or block exit. `src/murmly/daemon.py` already has this problem solved for
  other background work; reuse it rather than adding a bare `threading.Timer`.
- **The feature is inert by default, so it will be under-exercised in practice** →
  Tests must cover the evicted-then-reused path directly rather than relying on it
  being hit incidentally.

## Migration Plan

No migration. Both settings are absent in existing installs, which means disabled,
which is exactly today's behaviour. Rollback is setting them back to `0`.

## Open Questions

- Whether the shipped defaults should be `0` (off) or a non-zero period. Recorded
  as an open decision in `proposal.md`; it changes no requirement, since the spec
  is written in terms of "the configured period" either way.
- What the upper bound on each period should be. Any bounded value satisfies the
  spec; picking the number can wait for implementation.
