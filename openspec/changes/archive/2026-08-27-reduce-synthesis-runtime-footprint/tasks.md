## 1. Configuration

- [x] 1.1 Add a `device` setting to the `[tts]` section in `src/murmly/config.py`, taking the same `auto | cpu | cuda` vocabulary as `[stt] device` and defaulting to `"cpu"`
- [x] 1.2 Make an unrecognized value fall back to the default rather than raising, and record the rejected value the way `tts_voice_rejected_value` and `tts_rate_rejected_value` already do, so diagnostics can report it
- [x] 1.3 Tests: the default is `cpu` when the key is absent; each of `auto`, `cpu`, `cuda` is accepted; an unrecognized value falls back to `cpu` and is recorded as rejected

## 2. Provider resolution

- [x] 2.1 Change `resolve_providers` in `src/murmly/tts.py` to read the new `[tts]` device setting instead of `config.device`, and update its docstring — the current one states that `device` is the `[stt]` setting and that there is no separate one for synthesis, which this change makes false
- [x] 2.2 Confirm `auto` reproduces today's resolution exactly, including the existing fall backs when the CUDA provider is absent from the runtime build and when `load_cuda_libraries` fails
- [x] 2.3 Verify by test that the default path returns at the `cpu` shortcut and never reaches `load_cuda_libraries`, since that is what removes 190.1 MiB from daemon start-up
- [x] 2.4 Tests: `cpu` and default return the CPU provider with transcription still on `cuda`; `cuda` with a usable accelerator returns the CUDA provider first; `cuda` with an unusable accelerator falls back to CPU and logs the remedy

## 3. Session construction

- [x] 3.1 Build the `InferenceSession` in `_construct_model` with an explicit `onnxruntime.SessionOptions` that sets `enable_cpu_mem_arena = False`
- [x] 3.2 Leave `intra_op_num_threads` and `inter_op_num_threads` at the runtime defaults, and say why in a comment — capping intra-op to 4 saves 41 MiB but costs +54% on a short sentence (401 ms against 261 ms) and +36% on 8.02 s of audio (2083 ms against 1537 ms)
- [x] 3.3 Tests: the session is constructed with a `SessionOptions` whose CPU arena is disabled, and the provider read back off the session is the one resolution asked for

## 4. Diagnostics

- [x] 4.1 Report the synthesis processor in use, and any configured value not honoured, in `murmly doctor` alongside the existing voice, rate and output device reporting in `src/murmly/cli.py`
- [x] 4.2 Take the processor from a constructed session where one exists, and otherwise from what resolution would choose — never from `onnxruntime.get_available_providers()`, which advertises a provider a session may still fail to use (see `docs/agent-notes/onnxruntime-gpu-cuda-version.md`)
- [x] 4.3 Confirm reporting the processor does not construct a synthesis session as a side effect
- [x] 4.4 Tests: a default install reports the CPU as in use; a configured-`cuda`-but-unusable install reports `cuda` configured, CPU in use, and the remedy; the report does not load the model

## 5. Documentation

- [x] 5.1 Document `[tts] device` in `config.example.toml`, stating the default and that it is independent of `[stt] device`
- [x] 5.2 Document the trade in `README.md`: synthesis on the CPU holds no accelerator memory and returns its system memory, at the cost of roughly 200 ms before the first word, and `[tts] device = "cuda"` restores the previous behaviour
- [x] 5.3 Note that this changes behaviour on upgrade for installs with `[tts] enabled = true`. There is no changelog in this repo, so the note goes in `README.md`'s speech-output section, where an upgrading reader meets the setting

## 6. Verification

- [x] 6.1 Run the full suite with `.venv/bin/python -m unittest discover -s tests` — never `uv run --extra`, which resyncs the environment and reinstalls the CPU `onnxruntime` over the GPU build, making any synthesis measurement silently report a CPU session (see `docs/agent-notes/onnxruntime-gpu-cuda-version.md`)
- [x] 6.2 On a CUDA machine, confirm with `nvidia-smi --query-compute-apps` that a default-configured daemon attributes no accelerator memory to synthesis before, during, or after a speech session
- [x] 6.3 Re-measure host RSS held after the synthesis session is destroyed under both settings and check it against the proposal's 876 MiB and 65 MiB, recording any drift
- [x] 6.4 Re-measure per-sentence synthesis latency under the default and check it against the proposal's table, confirming the producer still finishes each sentence before the audio ahead of it has played out
- [x] 6.5 Confirm the rollback rule — `[tts] device` set to whatever `[stt] device` is — reproduces today's behaviour end to end for both affected configurations: `cuda` against a pinned accelerator, and `auto` against resolve-and-fall-back. Check accelerator memory (1208 MiB), warm latency (195-209 ms for 8.02 s of audio), and that `CUDAExecutionProvider` is read back off the session
- [x] 6.6 Confirm the new `SessionOptions` stays inert on the accelerator path — the graph runs some operators on the CPU even under the CUDA provider, so `enable_cpu_mem_arena = False` is not obviously a no-op there. Measured at 51-112 ms per sentence against 55-116 ms without; re-check rather than assume
- [x] 6.7 Confirm a daemon with `[tts] enabled = false` is unaffected in every respect, since `KokoroSynthesizer` is not constructed at all in that case
- [x] 6.8 Confirm the two configurations that should not move are unmoved: `[stt] device = "cpu"`, and `[stt] device = "auto"` on a machine with no usable accelerator. Both already synthesise on the CPU, so neither should observe any change
