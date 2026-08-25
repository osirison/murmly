## 1. Configuration

- [ ] 1.1 Add `unload_after_idle_s` to the `[stt]` section in `src/murmly/config.py`, bounded, defaulting to `0` (disabled), alongside `lazy_load_model`
- [ ] 1.2 Add `unload_after_idle_s` to the `[tts]` section, bounded and independently defaulted
- [ ] 1.3 Make an out-of-range value fall back to the default rather than raising, matching how the existing bounded settings behave
- [ ] 1.4 Document both settings and the memory-versus-latency trade in `config.example.toml` and `README.md`
- [ ] 1.5 Tests: defaults, disabled-by-absence, and out-of-range fallback for both settings

## 2. Transcription residency

- [ ] 2.1 Replace the `if self._model is None` residency test in `FasterWhisperTranscriber` with one that also treats an unloaded CTranslate2 model as needing a load — the wrapper survives `unload_model()`, so identity is not residency
- [ ] 2.2 Move the residency re-check and any reload inside the `_model_lock` block next to `_decode`, so eviction cannot land between acquiring the model and using it
- [ ] 2.3 Add a release method that acquires `_model_lock`, calls `unload_model(to_cpu=False)`, and is a no-op when the model is absent or already unloaded — `to_cpu=True` would free the GPU by moving 1541 MB into host RSS, which is the opposite of what the daemon needs
- [ ] 2.4 Add a `resident` property that reports residency without loading anything
- [ ] 2.5 Add an asynchronous warm-up that `begin_capture()` starts and that never blocks the caller
- [ ] 2.6 Tests: decode after an eviction returns the same transcript; eviction during a pass waits rather than interrupting; warm-up does not block `begin_capture`; `resident` does not load

## 3. Synthesis residency

- [ ] 3.1 Add a release method to `KokoroSynthesizer` that acquires `_model_lock` and drops the `InferenceSession`, since ONNX Runtime has no in-place unload
- [ ] 3.2 Make `_load_model` rebuild after a release, and keep a failed rebuild retryable rather than permanently unavailable
- [ ] 3.3 Add a `resident` property that reports residency without constructing a session
- [ ] 3.4 Tests: synthesis after a release succeeds; release during synthesis waits; a failed rebuild is retried on the next request

## 4. Idle timers

- [ ] 4.1 Arm the transcription timer when a recording session ends, and cancel it in `begin_capture()`, so a continuous session is never released mid-run
- [ ] 4.2 Arm the synthesis timer when a speech session ends and cancel it when one begins
- [ ] 4.3 Route both timers through the daemon's existing background-work shutdown sequencing rather than a bare `threading.Timer`, so a pending timer cannot block or extend exit
- [ ] 4.4 Make a configured value of `0` register no timer at all, so the feature is inert when unconfigured
- [ ] 4.5 Tests: a continuous auto-transcribe session with pauses longer than the idle period is never released; the countdown restarts when capture begins; daemon shutdown with a timer pending exits cleanly

## 5. Diagnostics

- [ ] 5.1 Report transcription and synthesis residency, and each configured idle period, in `murmly doctor`
- [ ] 5.2 Confirm the report does not load either model as a side effect
- [ ] 5.3 Tests: diagnostics on a fresh daemon report both as not resident and load neither

## 6. Verification

- [ ] 6.1 Run the full suite with `.venv/bin/python -m unittest discover -s tests` — not `uv run --extra cuda`, which resyncs the environment and reinstalls the CPU `onnxruntime` over the GPU build, for the same reason 6.3 gives
- [ ] 6.2 On a CUDA machine, confirm with `nvidia-smi --query-compute-apps` that an idle release actually returns memory to the system, not to an internal pool
- [ ] 6.3 Re-measure release and reload timings against the numbers in `proposal.md` using the scratch benchmarks, and record any drift — run them with `.venv/bin/python`, never `uv run --extra`, which reinstalls the CPU `onnxruntime` and makes a synthesis measurement silently report a CPU session (see `docs/agent-notes/onnxruntime-gpu-cuda-version.md`)
- [ ] 6.4 Confirm a default install with neither setting configured holds both models exactly as it does today
