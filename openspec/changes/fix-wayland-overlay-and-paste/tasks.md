## 1. Overlay placement on Wayland

- [x] 1.1 Load `libgtk4-layer-shell.so.0` with `ctypes.CDLL(..., RTLD_GLOBAL)` inside the Wayland renderer before any `gi` import (`src/murmly/overlay_renderer.py`). Shipped first as an `LD_PRELOAD` set by `renderer_environment()`, then replaced: the in-process load needs no environment variable, so the code-injection guard stays absolute and a missing library raises a catchable `OSError`
- [x] 1.2 Guard the import order the in-process load depends on: a test parsing `overlay_renderer.py` for module-scope `gi`/`cairo` imports, and a `sys.modules` check in `load_layer_shell()` that names an ordering bug instead of blaming the compositor. `tests/test_overlay.py:209` keeps its original assertion that no `LD_PRELOAD` reaches the renderer
- [x] 1.3 Make `OverlayApplication` verify `Gtk4LayerShell.is_supported()` before building any window on the Wayland backend and raise `OSError` when it is false, so `main()` reports it and exits non-zero instead of presenting unanchored windows (`src/murmly/overlay_renderer.py`)
- [x] 1.4 Split the layer-shell unsupported reason in `check_visual_runtime` by whether Murmly's preload is present in the environment: preload absent names Murmly's own runtime preparation, preload present names the compositor
- [x] 1.5 Add tests covering the refusal path and both reason strings, without requiring a live compositor (the suite skips rather than fails when a desktop session is unavailable)

## 2. Overlay diagnostics

- [x] 2.1 Pass `env=renderer_environment(backend)` to the `--check` subprocess in `overlay_diagnostics` (`src/murmly/cli.py:526`)
- [x] 2.2 Add a test asserting the helper check receives the renderer environment, not the caller's
- [x] 2.3 Confirm on this machine that `murmly doctor` reports the overlay as available on Plasma Wayland after 2.1, and reports it unavailable naming the missing preload when the preload is removed (the compositor-named reason cannot be reproduced here: this compositor offers `zwlr_layer_shell_v1`)

## 3. Paste injector selection

- [x] 3.1 Replace `choose_paste_command` with a session-aware selector in `src/murmly/integrations.py`: Wayland candidates `wtype` then `ydotool`, X11 `xdotool` unchanged, each Wayland candidate gated on an installed binary plus a no-op probe, result cached for the process lifetime
- [x] 3.2 Return the selected method, its availability, and the reason it is unavailable, so diagnostics and installation can report them without re-running selection
- [x] 3.3 Mark a candidate unusable for the rest of the session when a real delivery with it fails, so the next delivery re-selects
- [x] 3.4 Unit-test selection with fake `which` and fake probe results: installed-but-probe-fails is skipped, the next candidate is used, none-usable reports a reason, X11 behavior is byte-for-byte what it is today

## 4. Delivery degrades instead of losing the transcript

- [x] 4.1 Make `ClipboardPaster` keep resolving the copy command eagerly but treat the injector as optional, so construction no longer raises when no injector exists (`src/murmly/integrations.py`)
- [x] 4.2 Have `copy_and_paste` report whether injection happened and skip clipboard restoration when it did not
- [x] 4.3 Map a non-injected outcome in `SpeechSession.process_recording` onto the `ProcessingResult` already used for a refused delivery, so the overlay error state, the `copied but not pasted` response, and continuous-session termination are reached through existing paths (`src/murmly/daemon.py`)
- [x] 4.4 Test that a session with no usable injector returns `ok: true` with the transcript on the clipboard and `copied but not pasted` in the response, and that a continuous session ends on that outcome
- [x] 4.5 Test that an injector failing mid-delivery leaves the transcript on the clipboard and does not restore the previous clipboard contents

## 5. Reporting the remedy

- [x] 5.1 Verify the Fedora `ydotool` unit before writing any remedy text: unit name, socket path, and whether the client needs `YDOTOOL_SOCKET` (resolves the design's open question)
- [x] 5.2 Add a paste-injection section to `murmly doctor` reporting method, usability, reason, and remedy, distinguishing absent from installed-but-unusable (`src/murmly/cli.py`)
- [x] 5.3 Report the same remedy at the end of `murmly install` when injection is unavailable, without failing the installation and without changing any system state (`src/murmly/installer.py`)
- [x] 5.4 Test both reports for the three states: usable, installed-but-unusable, nothing installed

## 6. Documentation

- [x] 6.1 Correct README.md's Wayland requirements, which recommend `wtype` first, to state that `wtype` needs a compositor offering the virtual-keyboard protocol and that Plasma sessions without it need `ydotool` plus its system service
- [x] 6.2 Document that the Wayland overlay requires `gtk4-layer-shell` to be preloaded and that Murmly does this itself, so a manually launched renderer needs the preload

## 7. Verification on this machine

- [x] 7.1 Live checks were run against this worktree with `uv run`, not through the installed service: `murmly.service` runs the main checkout's entrypoint, so restarting it would still have exercised the old code
- [x] 7.2 Run the full suite: `uv run --extra cuda python -m unittest discover -s tests`
- [x] 7.3 Drove the renderer with the environment `renderer_environment()` builds and captured the screen: the indicator is bottom-centred with the transcript panel below it, above the Plasma panel's exclusive zone. Focus and click-through were not observable in a screenshot; they come from the unchanged keyboard-mode and empty-input-region calls
- [x] 7.4 Ran the real `ClipboardPaster` in this session with no injector installed: the text is on the clipboard, the previous contents were not restored over it, and the outcome reports the reason. Driven directly rather than through a spoken toggle, which needs speech into the microphone
- [x] 7.5 Verified `ydotool type ""` against the real binary, extracted from the RPM without installing it: it reaches the socket-connect stage and exits non-zero naming the path it tried, and the selector reports that verbatim. The success side of that probe and `wtype ""` are both still unverified — no daemon and no `wtype` here — and demotion-on-failure is what covers a probe that wrongly passes
- [x] 7.6 Superseded, and no `sudo` was needed. `xdotool` turned out to reach Wayland-native windows here because KWin bridges XTEST through libei, so the end-to-end paste was confirmed with it instead: a GTK4 window under `GDK_BACKEND=wayland` received the transcript through `ClipboardPaster.copy_and_paste`, the clipboard kept the transcript afterwards, and `doctor` reports `paste_injection.method: xdotool`
- [x] 7.7 Write a field note for the gtk4-layer-shell preload requirement and, if verified, the ydotool setup, under `docs/agent-notes/`
