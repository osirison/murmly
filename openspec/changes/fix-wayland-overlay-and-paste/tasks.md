## 1. Overlay placement on Wayland

- [ ] 1.1 Set a controlled `LD_PRELOAD=libgtk4-layer-shell.so.0` in `renderer_environment()` for the Wayland backend only, keeping the inherited-value strip and leaving the X11 environment without it (`src/murmly/overlay.py`)
- [ ] 1.2 Rewrite `tests/test_overlay.py:209` so it asserts the new meaning: inherited `LD_PRELOAD` discarded, Murmly's constant present on Wayland, absent on X11
- [ ] 1.3 Make `OverlayApplication` verify `Gtk4LayerShell.is_supported()` before building any window on the Wayland backend and raise `OSError` when it is false, so `main()` reports it and exits non-zero instead of presenting unanchored windows (`src/murmly/overlay_renderer.py`)
- [ ] 1.4 Split the layer-shell unsupported reason in `check_visual_runtime` by whether Murmly's preload is present in the environment: preload absent names Murmly's own runtime preparation, preload present names the compositor
- [ ] 1.5 Add tests covering the refusal path and both reason strings, without requiring a live compositor (the suite skips rather than fails when a desktop session is unavailable)

## 2. Overlay diagnostics

- [ ] 2.1 Pass `env=renderer_environment(backend)` to the `--check` subprocess in `overlay_diagnostics` (`src/murmly/cli.py:526`)
- [ ] 2.2 Add a test asserting the helper check receives the renderer environment, not the caller's
- [ ] 2.3 Confirm on this machine that `murmly doctor` reports the overlay as available on Plasma Wayland after 2.1, and reports it unavailable with the compositor-named reason when the preload is removed

## 3. Paste injector selection

- [ ] 3.1 Replace `choose_paste_command` with a session-aware selector in `src/murmly/integrations.py`: Wayland candidates `wtype` then `ydotool`, X11 `xdotool` unchanged, each Wayland candidate gated on an installed binary plus a no-op probe, result cached for the process lifetime
- [ ] 3.2 Return the selected method, its availability, and the reason it is unavailable, so diagnostics and installation can report them without re-running selection
- [ ] 3.3 Mark a candidate unusable for the rest of the session when a real delivery with it fails, so the next delivery re-selects
- [ ] 3.4 Unit-test selection with fake `which` and fake probe results: installed-but-probe-fails is skipped, the next candidate is used, none-usable reports a reason, X11 behavior is byte-for-byte what it is today

## 4. Delivery degrades instead of losing the transcript

- [ ] 4.1 Make `ClipboardPaster` keep resolving the copy command eagerly but treat the injector as optional, so construction no longer raises when no injector exists (`src/murmly/integrations.py`)
- [ ] 4.2 Have `copy_and_paste` report whether injection happened and skip clipboard restoration when it did not
- [ ] 4.3 Map a non-injected outcome in `SpeechSession.process_recording` onto the `ProcessingResult` already used for a refused delivery, so the overlay error state, the `copied but not pasted` response, and continuous-session termination are reached through existing paths (`src/murmly/daemon.py`)
- [ ] 4.4 Test that a session with no usable injector returns `ok: true` with the transcript on the clipboard and `copied but not pasted` in the response, and that a continuous session ends on that outcome
- [ ] 4.5 Test that an injector failing mid-delivery leaves the transcript on the clipboard and does not restore the previous clipboard contents

## 5. Reporting the remedy

- [ ] 5.1 Verify the Fedora `ydotool` unit before writing any remedy text: unit name, socket path, and whether the client needs `YDOTOOL_SOCKET` (resolves the design's open question)
- [ ] 5.2 Add a paste-injection section to `murmly doctor` reporting method, usability, reason, and remedy, distinguishing absent from installed-but-unusable (`src/murmly/cli.py`)
- [ ] 5.3 Report the same remedy at the end of `murmly install` when injection is unavailable, without failing the installation and without changing any system state (`src/murmly/installer.py`)
- [ ] 5.4 Test both reports for the three states: usable, installed-but-unusable, nothing installed

## 6. Documentation

- [ ] 6.1 Correct README.md's Wayland requirements, which recommend `wtype` first, to state that `wtype` needs a compositor offering the virtual-keyboard protocol and that Plasma sessions without it need `ydotool` plus its system service
- [ ] 6.2 Document that the Wayland overlay requires `gtk4-layer-shell` to be preloaded and that Murmly does this itself, so a manually launched renderer needs the preload

## 7. Verification on this machine

- [ ] 7.1 Restart `murmly.service` (or stop it and run the daemon from this worktree) before any live check, so verification exercises the new code rather than the running daemon
- [ ] 7.2 Run the full suite: `uv run --extra cuda python -m unittest discover -s tests`
- [ ] 7.3 Trigger a recording and confirm the overlay is bottom-centred at the configured margin on the selected display, with the transcript panel below the indicator and neither window taking focus or blocking clicks
- [ ] 7.4 Confirm a transcript with no injector installed lands on the clipboard and the toggle reports `copied but not pasted` rather than an error
- [ ] 7.5 Verify the `wtype ""` and `ydotool type ""` probe semantics if either tool is available; if installing them is not wanted, record in the change that the probes remain unverified and that the empirical demotion path is what guarantees correctness
- [ ] 7.6 Optional, requires the user's `sudo`: after `dnf install ydotool` and enabling its service, confirm an end-to-end paste into a Wayland-native window and that doctor reports the method it used
- [ ] 7.7 Write a field note for the gtk4-layer-shell preload requirement and, if verified, the ydotool setup, under `docs/agent-notes/`
