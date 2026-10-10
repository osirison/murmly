# Tasks

The screens to build are on the canvas linked from proposal.md. Run the suite with
`uv run --no-sync python -m unittest discover -s tests`; tests that need a display
skip themselves when none is available.

## 1. Scaffolding

- [ ] 1.1 Add the `gui` optional extra (PySide6 on Linux and Windows) and `tomlkit` to `pyproject.toml`, re-lock, and add a `murmly app` command that exits with a clear message when the extra is missing; verify the suite passes without the extra and `uv run --no-sync --extra gui murmly app --help` prints usage
- [ ] 1.2 Bundle Instrument Sans and IBM Plex Mono (latin subsets) with their SIL OFL licences in `licenses/`; verify the app renders in those fonts with the network disabled
- [ ] 1.3 Port the canvas's dark and light colour, type, spacing and radius tokens into one Qt Quick theme; verify a test that every colour token has both a dark and a light value

## 2. Daemon support

- [ ] 2.1 Hold the last transcript in a single in-memory slot (text, time, delivery outcome) with socket requests to read and clear it; verify tests that the slot is overwritten by the next transcript, empty after a daemon restart, cleared on request, and that no log record contains its text
- [ ] 2.2 Report in `status` whether the daemon is idle enough to restart (no capture, transcription or speech); verify tests for each busy state and for idle

## 3. Home and the refused-delivery screen

- [ ] 3.1 Build Home: state, keys, last words and setup summary, polling `status` at most every 500 ms only while visible; verify a test against a fake socket that a state change appears within one second and that polling stops when hidden
- [ ] 3.2 Show "service not running" with a start action when the socket cannot be reached; verify a test with no socket
- [ ] 3.3 Build the words-not-typed screen: clipboard reassurance first, the daemon's detail string unaltered, Copy again; verify a test that the detail string is shown byte for byte and Copy again writes the transcript to the clipboard
- [ ] 3.4 Guard focus: no raise or activate in response to daemon state; verify a test that drives every state change and asserts no focus call is made

## 4. Settings

- [ ] 4.1 Write a single key with `tomlkit`, validated by `murmly.config`'s rules, written atomically and mtime-checked; verify tests that comments and key order survive, an out-of-range value is refused, an unparseable file is left byte-for-byte unchanged, and a file changed on disk is re-read first
- [ ] 4.2 Build the Dictation and Speech settings sections from the canvas; verify a test that each control writes the documented key and value
- [ ] 4.3 Apply changes by restarting the service through the installer once the daemon reports idle; verify tests that a save while idle restarts at once and a save during capture waits until idle
- [ ] 4.4 Document the app's Settings in the manual's settings page; verify `mkdocs build --strict` passes

## 5. Health

- [ ] 5.1 Render every `murmly doctor` section as a sentence plus its raw key and value, and add Copy the full report; verify a test over a recorded report that every section appears and the copied text contains no transcript text

## 6. First run

- [ ] 6.1 Key recorder confined to its focused field, validated by `murmly install`'s rules and bound through the same install path; verify tests that a combination without Meta, Ctrl or Alt is refused, a held key is refused (naming the holder where reported), and keystrokes are ignored when the field is not recording
- [ ] 6.2 Accuracy step and the try-it text box; verify a test that choosing a profile writes `stt.model_profile`

## 7. Tray panel

- [ ] 7.1 Tray icon and panel with state, last words and the four quick switches, which save as Settings does; verify a test that toggling a switch writes its key, and that with no tray available the app still starts and Health reports that no tray was found

## 8. Delivered confirmation in the overlay

- [ ] 8.1 Add a delivered state to the overlay protocol and draw it in the X11, Wayland and Qt renderers (check and "Typed", at most 1.5 s, static under reduced motion, never for a continuous-session segment, replaced at once if capture starts); verify renderer tests where the platform allows, and update `manual/using-murmly.md` with the new state and screenshot

## 9. Removing Murmly from the app

- [ ] 9.1 Add a confirmation naming what will be removed, run the same removal as `murmly uninstall`, with purge as a separate explicit choice, and record the app shortcut as installed so uninstall removes it; verify tests that removal without purge keeps settings and models and that the shortcut is gone afterwards

## 10. Voices

- [ ] 10.1 Confirm `add-speaker-recognition` is archived and `openspec/specs/speaker-recognition/spec.md` exists; re-check this change's Voices requirements against that final spec and run `openspec validate --all --strict`
- [ ] 10.2 Build the Voices section from the canvas: mode, owner and match threshold written through the Settings writer, the enrolled-voices list with the owner marked, remove one, and remove all behind a second confirmation; verify tests that each control writes `speakers.mode`, `speakers.owner` or `speakers.match_threshold`, that removal deletes only the named voiceprint, and that remove-all deletes nothing until confirmed twice
- [ ] 10.3 Build in-app enrolment around the code `murmly enrol` runs (factor it into one function both call, if it is not one already): passage, live speech count, the busy refusal, and the replaced-voiceprint message; verify tests with a fake recorder that a short sample stores nothing and reports the speech heard, a busy daemon is refused with its state, and no audio file is written
- [ ] 10.4 Require consent before enrolling anyone not marked as the owner; verify a test that recording cannot start until it is confirmed
- [ ] 10.5 Read `speaker_recognition` from the doctor report on opening Home or Voices, after voice or `[speakers]` changes, and after a restart, and show the voices-not-checked notice on Home, the tray panel and Voices with `detail` verbatim; verify tests over recorded reports that the notice appears for each `detail` the manual lists, disappears when `available` is true, and that Health lists the same problem
- [ ] 10.6 Keep labelled parts in the last-words slot and show them by speaker in the app; verify tests that a labelled transcript is shown part by part and that Copy again copies the delivered line byte for byte
- [ ] 10.7 Send a not-your-voice overlay state when mine-only leaves a recording empty, never for a continuous segment, and draw it in the X11, Wayland and Qt renderers with no name or text; verify daemon tests for both cases and renderer tests where the platform allows
- [ ] 10.8 Add the optional voice step to first run; verify a test that skipping it leaves no voiceprint and finishing it stores one marked as the owner
- [ ] 10.9 Add the app's route to `manual/typing-only-your-own-voice.md` next to each command, and describe the not-your-voice state in `manual/using-murmly.md`; verify `mkdocs build --strict` passes

## 11. Integration

- [ ] 11.1 Write `manual/using-the-app.md` and add it to the `mkdocs.yml` navigation; verify `mkdocs build --strict` passes
- [ ] 11.2 Run the app end to end on KDE Plasma X11 and on Windows: dictate with the app open behind the target window, change a setting while recording, refuse a delivery by switching windows, enrol a voice and switch to "only my voice", remove the owner's voice and see the voices-not-checked notice, and remove Murmly; record the results in the change before archiving
