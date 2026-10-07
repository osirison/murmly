# Tasks

Run the suite with `uv run --no-sync python -m unittest discover -s tests`.

## 1. Readable application names

- [ ] 1.1 Add `pyxdg` (Linux only) and resolve a display name from the X11 application class through installed desktop entries (`StartupWMClass`, then file name), falling back to the class; verify tests with fixture desktop entries for a match by `StartupWMClass`, a match by file name, and no match
- [ ] 1.2 Resolve a display name on Windows from the executable's `FileDescription` with `win32api.GetFileVersionInfo`, falling back to the executable name without `.exe`, behind a seam the tests can replace; verify tests with a fake version reader for both cases
- [ ] 1.3 Cache resolved names per identity and return none where no observer exists (Wayland); verify tests that a second lookup does not resolve again and that the null observer yields no name

## 2. Configuration

- [ ] 2.1 Add `overlay.show_target` (boolean, default `true`) to `config.py`, `config.example.toml` and the doctor report; verify tests for the default, an explicit `false`, and an invalid value falling back to `true`; document the key in `manual/settings.md` and check `mkdocs build --strict` passes

## 3. Daemon

- [ ] 3.1 While capture runs with the overlay shown and the setting on, poll the focus observer every 250 ms and send the overlay a `target_name` only when it changes, stopping when capture stops; verify tests with a fake observer and clock that a focus change reaches the overlay within one second and that nothing is sent with the setting off or on Wayland
- [ ] 3.2 After capture stops, send the recorded target's name with the processing and error presentations, and "your assistant" for speech-session capture; verify tests that the name stays fixed when focus moves before delivery
- [ ] 3.3 Add `target` and, on a refusal, `in_front` to the toggle response, omitted when unknown or when naming is off; verify tests for delivered, refused, unobservable-session and speech-session cases, and that no log record contains either name; update `manual/where-your-words-go.md` with the new fields

## 4. Overlay renderers

- [ ] 4.1 Draw the name in a bounded label below the indicator in the X11, Wayland and Qt renderers, truncated with an ellipsis, taking no focus and passing pointer input through; verify renderer tests where the platform allows that the indicator's position and size are unchanged with and without a long name
- [ ] 4.2 Update `manual/using-murmly.md` with the named states and new screenshots; verify `mkdocs build --strict` passes

## 5. Integration

- [ ] 5.1 Check end to end on KDE Plasma X11 and on Windows: the name follows focus while listening, freezes at stop, appears on a refused delivery, is absent with the setting off; on KDE Plasma Wayland confirm no name is shown; record the results in the change before archiving
