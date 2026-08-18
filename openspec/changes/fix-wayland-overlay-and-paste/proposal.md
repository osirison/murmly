## Why

On a KDE Plasma Wayland session Murmly's overlay appears as an ordinary window in the
centre of the screen with the transcript panel stacked over the recording indicator,
and a finished transcript is never pasted. Both are reproducible on the reporting
machine and both come from Murmly trusting a capability it never checks: the overlay
assumes `gtk4-layer-shell` took effect when it did not, and delivery assumes a paste
injector exists and works when none does.

Confirmed on this session (Fedora 44, Plasma Wayland, `wayland-0`):

- `Gtk4LayerShell.is_supported()` returns `False` under the environment
  `renderer_environment()` builds, and `True` when the same interpreter runs with
  `LD_PRELOAD=libgtk4-layer-shell.so.0`. Without the preload
  `Gtk4LayerShell.init_for_window()` has no effect, so both overlay windows are
  presented as ordinary toplevels that KWin centres — the reported symptom.
- `wayland-info` on this session advertises `zwlr_layer_shell_v1` (v5) but no
  `zwp_virtual_keyboard_manager_v1`, the protocol `wtype` requires. `wtype` is the
  tool README.md recommends first for Wayland.
- Neither `wtype` nor `ydotool` is installed, so `ClipboardPaster.__init__` raises
  `MissingToolError` before anything is copied. `SpeechSession.process_recording`
  propagates it and `MurmlyDaemon._finish_toggle` turns it into a generic error, so
  the transcript is destroyed rather than left on the clipboard.

## What Changes

- Launch the Wayland overlay renderer with a controlled `LD_PRELOAD` for
  `gtk4-layer-shell`, still discarding any `LD_PRELOAD` inherited from the caller.
- Make the renderer refuse to present its windows when layer-shell anchoring cannot
  take effect, so a mis-anchored overlay is reported as unavailable through the
  existing visual-failure path instead of being drawn in the wrong place.
- Run the `murmly doctor` overlay runtime check under the same environment the
  renderer is launched with, and distinguish "the layer-shell library was not loaded
  early enough" from "this compositor has no layer shell".
- Select the paste injector by what the active session can actually execute rather
  than by which binary happens to be on `PATH`, and stop preferring a tool the
  session cannot support.
- Degrade to copy-only with the existing "copied but not pasted" outcome when no
  injector can deliver, instead of failing the toggle and losing the transcript.
- Report the injector state and the exact remedy in `murmly doctor` and at the end of
  `murmly install`. Murmly does not install or enable `ydotoold` itself; that needs
  root and stays the user's action.
- Correct README.md's Wayland requirements, which currently recommend `wtype` first.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `recording-overlay`: the renderer must not present an overlay it cannot anchor, and
  overlay diagnostics must evaluate the runtime under the environment the renderer
  actually receives.
- `transcript-delivery`: injector selection must match what the session supports, and
  an unavailable or failing injector must degrade to copy-only rather than lose the
  transcript.
- `desktop-integration`: installation must report whether a paste can be injected in
  the session it just installed into.

## Impact

- `src/murmly/overlay.py` — `renderer_environment()` gains a controlled `LD_PRELOAD`
  on the Wayland backend.
- `src/murmly/overlay_renderer.py` — layer-shell support is verified before windows
  are built; the unsupported-reason text is split by cause.
- `src/murmly/cli.py` — `overlay_diagnostics()` runs the helper check under
  `renderer_environment()`; the doctor report gains injector state and remedy.
- `src/murmly/integrations.py` — injector selection becomes capability-based;
  clipboard copy survives an unavailable injector.
- `src/murmly/daemon.py` — delivery treats an unavailable injector as a refusal.
- `src/murmly/installer.py` — post-install report names a missing injector.
- `tests/test_overlay.py:209` asserts `LD_PRELOAD` is absent from the renderer
  environment; that assertion changes meaning to "inherited value stripped,
  controlled value present on Wayland".
- README.md Wayland requirements.
- No new Python dependencies. `ydotool` becomes the documented Wayland injector for
  Plasma; installing it stays a user action.
