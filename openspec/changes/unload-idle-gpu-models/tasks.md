## 1. Configuration

- [x] 1.1 Add `unload_after_idle_s` to the `[stt]` section in `src/murmly/config.py`, bounded 30-86400, **defaulting to `300`** (five minutes, enabled), alongside `lazy_load_model`. `0` disables
- [x] 1.2 Add `unload_after_idle_s` to the `[tts]` section, bounded 30-86400, **defaulting to `0`** (disabled)
- [x] 1.3 Make an out-of-range value fall back to the default rather than raising, matching how the existing bounded settings behave
- [x] 1.4 Document both settings and the memory-versus-latency trade in `config.example.toml` and `README.md`
- [x] 1.5 Tests: `[stt]` defaults to 300 and `[tts]` to 0 when absent; an explicit `0` disables either; an out-of-range value falls back to that setting's own default, not to a shared one

## 2. Transcription residency

- [x] 2.1 Replace the `if self._model is None` residency test in `FasterWhisperTranscriber` with one that also treats an unloaded CTranslate2 model as needing a load — the wrapper survives `unload_model()`, so identity is not residency
- [x] 2.2 Move the residency re-check and any reload inside the `_model_lock` block next to `_decode`, so eviction cannot land between acquiring the model and using it
- [x] 2.3 Add a release method that acquires `_model_lock`, calls `unload_model(to_cpu=False)`, and is a no-op when the model is absent or already unloaded — `to_cpu=True` would free the GPU by moving 1541 MiB into host RSS, which is the opposite of what the daemon needs
- [x] 2.4 Add a `resident` property that reports residency without loading anything
- [x] 2.5 Add an asynchronous warm-up that `begin_capture()` starts and that never blocks the caller
- [x] 2.6 Tests: decode after an eviction returns the same transcript; eviction during a pass waits rather than interrupting; warm-up does not block `begin_capture`; `resident` does not load

## 3. Synthesis residency

- [x] 3.1 Add a release method to `KokoroSynthesizer` that acquires `_model_lock` and drops the `InferenceSession`, since ONNX Runtime has no in-place unload. Dropping it is cheap — 28-36 ms measured, not the 1.02 s this change originally recorded; the rebuild is the expensive half
- [x] 3.2 Make `_load_model` rebuild after a release, and keep a failed rebuild retryable rather than permanently unavailable
- [x] 3.3 Add a `resident` property that reports residency without constructing a session
- [x] 3.4 Tests: synthesis after a release succeeds; release during synthesis waits; a failed rebuild is retried on the next request

## 4. Idle timers

- [x] 4.1 Arm the transcription timer when a recording session ends, and cancel it in `begin_capture()`, so a continuous session is never released mid-run
- [x] 4.2 Arm the synthesis timer when a speech session ends and cancel it when one begins
- [x] 4.3 Route both timers through the daemon's existing background-work shutdown sequencing rather than a bare `threading.Timer`, so a pending timer cannot block or extend exit
- [x] 4.4 Make a resolved value of `0` register no timer at all, so synthesis is inert by default and either model can be switched off entirely
- [x] 4.5 Tests: a continuous auto-transcribe session with pauses longer than the idle period is never released; the countdown restarts when capture begins; daemon shutdown with a timer pending exits cleanly

## 5. Diagnostics

- [x] 5.1 Report transcription and synthesis residency, and each configured idle period, in `murmly doctor`
- [x] 5.2 Confirm the report does not load either model as a side effect
- [x] 5.3 Tests: diagnostics on a fresh daemon report both as not resident and load neither

## 6. Verification

- [x] 6.1 Run the full suite with `.venv/bin/python -m unittest discover -s tests` — not `uv run --extra cuda`, which resyncs the environment and reinstalls the CPU `onnxruntime` over the GPU build, for the same reason 6.3 gives
- [x] 6.2 Confirm an idle release returns memory to the system rather than to an internal pool. For transcription that is `nvidia-smi --query-compute-apps`. For synthesis it depends on `[tts] device`: under the default (`cpu`) there is no accelerator memory to observe and the check is host RSS returning to its released floor of roughly 77 MiB; only under `cuda` does `nvidia-smi` show the 528 MiB
- [x] 6.3 Re-measure release and reload timings against the numbers in `proposal.md` using the scratch benchmarks, and record any drift — run them with `.venv/bin/python`, never `uv run --extra`, which reinstalls the CPU `onnxruntime` and makes a synthesis measurement silently report a CPU session (see `docs/agent-notes/onnxruntime-gpu-cuda-version.md`)
- [x] 6.4 Confirm a default install with neither setting configured releases the transcription model after five idle minutes and holds the synthesis session indefinitely, and that `[stt] unload_after_idle_s = 0` restores today's always-resident behaviour
- [x] 6.5 Measure at least eight transcription unload/reload cycles and confirm no per-cycle drift, against the baseline taken before this default was chosen: GPU returning to exactly 2240 MiB and releasing to 160 MiB each cycle, host RSS drift flat at +2.3 MiB from cycle 1 onward, unload 55-57 ms, reload 740-771 ms. Per-cycle growth here would reach every install, unlike the synthesis path, so this gates the default rather than merely documenting it
- [x] 6.6 Measure host RSS across at least five synthesis release/rebuild cycles under both `[tts] device` values and check it against the drift table in `design.md` — Risks. Expect the `cpu` path to oscillate and return to baseline, and the `cuda` path to take a one-time step of roughly 277 MiB and then creep about 8 MiB per cycle. If the `cuda` creep is materially steeper than measured, the synthesis timer needs a guard rather than only documentation
