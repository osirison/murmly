## 1. Move speech output into a default dependency group

- [x] 1.1 Remove `tts` from `[project.optional-dependencies]` in `pyproject.toml` and declare it as `[dependency-groups] tts = ["kokoro-onnx>=0.6.1"]`, leaving `cuda` an extra
- [x] 1.2 Add `[tool.uv] default-groups = ["tts"]`, and move the comment block explaining why `onnxruntime-gpu` is not listed so it still sits with the dependency it describes
- [x] 1.3 Run `uv lock` and commit the regenerated `uv.lock`
- [x] 1.4 Confirm on a scratch checkout that `uv sync --extra cuda` leaves `kokoro-onnx` installed, that `uv sync` alone does too, and that `uv sync --extra cuda --no-group tts` removes it

## 2. Rework the installer onto groups

- [x] 2.1 Rework `current_extras` in `setup.sh` so speech output is no longer reported as an installed extra, keeping the `cuda` detection as it is
- [x] 2.2 Rework `resolve_extras` and `sync_environment` to build the sync command from the `cuda` extra plus, when speech output is being declined, `--no-group tts`
- [x] 2.3 Keep `--no-tts` as the user-facing flag name and map it to `--no-group tts`, so a scripted install does not break
- [x] 2.4 Change the interactive speech-output question from installing packages to downloading the model files, since the packages now arrive either way
- [x] 2.5 Check that the `onnxruntime-gpu` swap logic still fires on the right condition now that a tts sync is not signalled by an extra
- [x] 2.6 Exercise `sync_environment` with `uv` stubbed and confirm the command it builds for each case: default, cuda present, `--no-tts`, and an environment previously opted out. A live `./setup.sh update` was not run: `REPO` is the script's own directory, so from a worktree it would sync the worktree and create the `.venv` that `docs/agent-notes/unittest-discover-in-a-worktree.md` warns against

## 3. Re-probe when a session is declared

- [x] 3.1 Add a method to `SpeechEngine` that re-runs `_probe()` and returns its reason or `None`, writing nothing to `_unavailable_reason` and constructing no model
- [x] 3.2 Have it return `None` immediately when `resident` is true, so a loaded synthesizer is never re-examined
- [x] 3.3 Call it from `Daemon._declare_session` beside the existing `available` check, before `self._lock` is taken, refusing with `CommandCode.SPEECH_UNAVAILABLE` and the reason it returned
- [x] 3.4 Confirm the refusal reason names the remedy, matching what the startup probe produces for the same cause

## 4. Tests

- [x] 4.1 Test that a session is refused when the synthesis runtime is absent at declaration time but was present at construction, and that `unavailable_reason` is still `None` afterwards
- [x] 4.2 Test that a later declaration succeeds once the runtime is available again, with no new engine constructed
- [x] 4.3 Test that a resident synthesizer is not re-probed, by making the probe raise if called
- [x] 4.4 Test that the same treatment covers missing model files, not only the missing package
- [x] 4.5 Test that a refused declaration leaves capture, transcription, and delivery working
- [x] 4.6 Test that the announcement hook produces no chime when the daemon refuses the session, covering the `agent-announcements` delta
- [x] 4.7 Run the full suite against an environment synced the new way and confirm nothing depended on `kokoro-onnx` being absent
- [x] 4.8 Invert `test_no_remedy_names_a_command_that_removes_speech_output` in `tests/test_tts.py`: the destructive command is now `--no-group tts`, and `--extra tts` names an extra that no longer exists
- [x] 4.9 Invert `test_the_speech_instructions_never_sync_the_speech_extra_away` in `tests/test_config.py` so it guards the new rule across README, `config.example.toml`, and `pyproject.toml`
- [x] 4.10 Update the remedy strings the suite asserts on in `tests/test_tts.py` and `tests/test_cli.py`

## 5. CI

- [x] 5.1 Confirm `uv sync --locked` in `.github/workflows/tests.yml` resolves with the group present and needs no flag change
- [x] 5.2 Confirm the added install time and download size are acceptable on both Python versions the workflow runs

## 6. Documentation

- [x] 6.1 Replace the three README passages that warn about naming every extra with the install commands that are now correct, deleting the warning rather than rewording it
- [x] 6.2 Update the README's speech output section for the new install path and the `--no-group tts` opt-out
- [x] 6.3 Update `docs/agent-notes/onnxruntime-gpu-cuda-version.md`: the "name every extra every time" section, the recipes that pair `--extra cuda --extra tts`, and the `uv run --extra` warning
- [x] 6.4 Update `docs/agent-notes/uv-sync-cuda-runtime.md` where it instructs syncing both extras on one line
- [x] 6.5 Revise the cause section of `docs/agent-notes/announce-hook-chime-without-speech.md` so it records that the packaging hole is closed, keeping the chime-as-a-probe diagnostic, which still applies to every other way speech output goes missing
- [x] 6.6 Check `config.example.toml` and any other install instructions for `--extra tts`

## 7. End to end

- [x] 7.1 On a live install, remove `kokoro-onnx` from under a running daemon and confirm an announcement produces no sound at all rather than notes followed by silence
- [x] 7.2 Reinstall it and confirm the next announcement is spoken without restarting the daemon
- [x] 7.3 Confirm `murmly doctor` reports the same cause throughout
