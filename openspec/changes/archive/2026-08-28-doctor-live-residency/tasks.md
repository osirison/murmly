## 1. The daemon answers what it holds

- [x] 1.1 Add the transcription model's and the synthesis session's residency to the `status` response in `handle_command`, read from `FasterWhisperTranscriber.resident` and `KokoroSynthesizer.resident` rather than inferred from whether an idle countdown is armed — a countdown that has fired says a release was attempted, not that it succeeded
- [x] 1.2 Report synthesis residency as absent rather than false when no synthesizer was built, since a daemon with `[tts] enabled = false` holds nothing and has nothing to hold
- [x] 1.3 Confirm answering does not load either model, does not block on a model lock, and does not change the existing `state` field or its meaning
- [x] 1.4 Tests: `status` carries both residency values; the answer follows a release and a reload; answering loads neither model; a daemon answering while a transcription holds the model lock is not delayed

## 2. Doctor asks the daemon

- [x] 2.1 Query the daemon over the command socket in `src/murmly/cli.py` and report its answer for both models, using `send_command` rather than a second transport
- [x] 2.2 Report `null` with a sibling `*_detail` naming the reason when there is no answer — no daemon running, a daemon too old to carry the fields, or a failed query — following the `providers` / `provider_detail` convention already in the file
- [x] 2.3 Keep reading residency before the report is assembled, ahead of `live_transcription_diagnostics`, so the report describes what was held when the question was put rather than what the report loaded on its way past. This is already the order; make it deliberate and pin it by test
- [x] 2.4 Confirm a daemon that cannot be reached or cannot answer leaves every other section of the report intact
- [x] 2.5 Tests: a running daemon holding the model reports resident; one that has released it reports not resident; no daemon reports `null` and a reason and never `false`; a daemon whose response omits the fields reports `null` and a reason; a failed query does not abandon the report

## 3. Sections that load a model say so

- [x] 3.1 Have the partial-pass measurement state in the report that it loaded a model, since it does and the report currently does not say so. `measure_partial_pass_ms` is unchanged and stays in the default report — decided with the user, see `design.md`
- [x] 3.2 Tests: with `[stt] live_transcribe = true` the report declares that the measurement loaded a model, and the residency it reports is the value read before that section ran

## 4. Documentation

- [x] 4.1 Document in `README.md` that residency is reported for the running daemon, and that no daemon means the question cannot be answered rather than the models being idle
- [x] 4.2 Note that `murmly doctor` now opens the command socket, which it did not before, so the report reflects a live daemon rather than the reporting process

## 5. Verification

- [x] 5.1 Run the full suite with `.venv/bin/python -m unittest discover -s tests` — never `uv run --extra`, which resyncs the environment and reinstalls the CPU `onnxruntime` over the GPU build (see `docs/agent-notes/onnxruntime-gpu-cuda-version.md`)
- [x] 5.2 Against a real daemon on a CUDA machine: start it, transcribe once, and confirm `murmly doctor` reports the transcription model resident while `nvidia-smi --query-compute-apps` shows the memory held
- [x] 5.3 Wait out `[stt] unload_after_idle_s` — or set it to its 30 s floor — and confirm the report flips to not resident as the accelerator memory is returned, which is the observation the current constant `false` cannot make
- [x] 5.4 Stop the daemon and confirm the report says residency could not be determined, naming the reason, rather than reporting either model as not resident
- [x] 5.5 Confirm a daemon with `[tts] enabled = false` reports no synthesis residency rather than reporting it as released
- [x] 5.6 Confirm the report is unchanged in every other respect against a copy taken before this change, so the only differences are the residency fields and the measurement's new disclosure
