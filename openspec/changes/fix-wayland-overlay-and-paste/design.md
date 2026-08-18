## Context

See proposal.md — Why for the three symptoms and the evidence behind each. This
section records only the constraints that shape the approach, and marks each fact
as **confirmed** (reproduced on the reporting machine: Fedora 44, Plasma Wayland,
`WAYLAND_DISPLAY=wayland-0`, gtk4-layer-shell 1.3.0, GTK 4.22.4) or **inferred**.

- **Confirmed.** Under PyGObject, `Gtk4LayerShell.is_supported()` is `False` unless
  the library reaches the global symbol scope before `gi` is imported. `/usr/bin/python3
  overlay_renderer.py --check --backend wayland` returned
  `{"available": false, ... "error": "The active Wayland compositor does not support
  Layer Shell."}` before this change and `{"available": true, "gtk4_layer_shell": true}`
  after it, with no `LD_PRELOAD` in the renderer's environment.
- **Confirmed.** The bare soname `libgtk4-layer-shell.so.0` resolves through the
  standard loader search path, so no distro-specific absolute path is needed, whether
  it is named to `ld.so` or to `ctypes.CDLL`.
- **Confirmed.** `ctypes.CDLL(..., RTLD_GLOBAL)` establishes the same interposition as
  `LD_PRELOAD` when it runs before `from gi.repository import Gtk`, and does not when
  it runs after. A bare `import gi` is harmless; the `gi.repository` import is what
  loads libgtk-4 and with it libwayland-client. Murmly's guard refuses on a bare
  `import gi` too, because the cheap check is the safe one.
- **Confirmed.** Upstream's `examples/simple-example.py` performs this same ctypes
  load before importing gi, and `linking.md` presents `LD_PRELOAD` as the workaround
  for programs you cannot modify. Murmly owns the renderer, so the in-process load is
  the documented path rather than a substitute for it. Upstream uses the default
  `CDLL` mode, which also reports supported here; Murmly passes `RTLD_GLOBAL` because
  that is the mode under which the documented symbol interposition holds, and it is
  the one whose full render path was verified on screen.
- **Confirmed.** `Gtk4LayerShell.init_for_window()` does not raise when the library
  was not preloaded — it silently does nothing, which is why both overlay windows are
  presented as ordinary toplevels and KWin centres them, the panel overlapping the
  indicator.
- **Confirmed.** `wayland-info` on this session lists `zwlr_layer_shell_v1` (v5),
  `zwp_input_method_v1`, and `zwp_text_input_manager_v2/v3`, and does **not** list
  `zwp_virtual_keyboard_manager_v1`, which `wtype` requires. This is a fact about this
  session, not a claim that KWin can never offer that protocol.
- **Confirmed.** Neither `wtype` nor `ydotool` is installed here; `wl-copy`,
  `wl-paste`, `xclip`, and `xdotool` are. `ClipboardPaster.__init__`
  (`src/murmly/integrations.py:74`) resolves the paste command eagerly, so
  `MissingToolError` is raised before any copy happens and
  `MurmlyDaemon._finish_toggle` (`src/murmly/daemon.py:464`) turns it into
  `{"ok": false, ...}` — the transcript is destroyed.
- **Confirmed.** `overlay_diagnostics` (`src/murmly/cli.py:526`) runs the helper check
  with no `env=`, so it inherits the caller's environment rather than the one
  `renderer_environment()` builds for the renderer.
- **Confirmed.** `tests/test_overlay.py:209` asserts `LD_PRELOAD` is absent from the
  renderer environment, as a code-injection guard against inherited values. That
  assertion survives this change unchanged.
- **Confirmed.** Fedora's `ydotool` package ships `/usr/bin/ydotool`,
  `/usr/bin/ydotoold`, and `/usr/lib/systemd/system/ydotool.service`.
- **Inferred.** `ydotool` is the only injector that can work on this session, because
  it drives `/dev/uinput` rather than a Wayland protocol. Its socket path, whether the
  client needs `YDOTOOL_SOCKET`, and what the shipped unit sets are **unverified**.

## Goals / Non-Goals

**Goals:**

- The overlay is either presented exactly as `recording-overlay` specifies, or not
  presented at all.
- `murmly doctor` predicts what the overlay and the paste will actually do.
- A transcript is never destroyed by an injector that is missing or fails.

**Non-Goals:**

- Installing or enabling `ydotoold`. That is root-owned system state; Murmly detects
  and instructs (user's decision, recorded in this change).
- Any new Python or system dependency, and any new binding to a Wayland protocol.
- Changing overlay geometry, animation, transcript panel sizing, or focus
  verification. Wayland/X11 placement parity is already required by
  `recording-overlay` — this change makes the implementation obey it, not the spec.
- Making injection work for XWayland clients through `xdotool` on a Wayland session.

## Decisions

### 1. The renderer loads gtk4-layer-shell itself, before it imports `gi`

`load_layer_shell()` runs `ctypes.CDLL("libgtk4-layer-shell.so.0",
mode=RTLD_GLOBAL)` as the first thing the Wayland renderer does, ahead of every
`import gi` in the process. That puts the library in the global symbol scope before
PyGObject loads `libwayland-client`, which is the whole requirement. No environment
variable is involved, so `renderer_environment()` keeps discarding `LD_PRELOAD`
outright and the allowlist stays absolute.

**Revised during apply.** This decision first shipped as a controlled
`LD_PRELOAD=libgtk4-layer-shell.so.0` set by `renderer_environment()`, and rejected
the in-process load on the grounds that "ordering inside an already-running
interpreter is not something Murmly can guarantee". That reasoning was wrong and the
test that settles it is cheap: `overlay_renderer.py` imports `gi` only inside
functions, so the ctypes call can and does run first.

Measured on this machine, `Gtk4LayerShell.is_supported()`:

| how the library is loaded | supported |
| --- | --- |
| not loaded | `False` |
| `LD_PRELOAD=libgtk4-layer-shell.so.0` | `True` |
| `ctypes.CDLL(..., RTLD_GLOBAL)` before any gi import | `True` |
| after a bare `import gi`, before `from gi.repository import Gtk` | `True` |
| after `from gi.repository import Gtk` | `False` |

The in-process load wins on three counts: the renderer environment carries no
`LD_PRELOAD` at all, so the code-injection guard is a plain absence rather than a
value that must be audited; the requirement lives in the file that depends on it
instead of in the launcher two modules away; and a missing library raises a
catchable `OSError` naming it, where the preload could only print an `ld.so` line to
stderr and carry on.

Its one fragility is that the ordering is invisible: a future module-scope `import
gi` would break it silently. Three guards, cheapest first — a test parses
`overlay_renderer.py` and asserts no module-scope `gi` or `cairo` import;
`load_layer_shell()` refuses when `gi` is already in `sys.modules` — conservative,
since only the `gi.repository` import actually closes the window — and says so; and
the `is_supported()` check still refuses to present the overlay, so the worst case
is a reported absence rather than a mis-placed window.

Alternatives considered:

- *Absolute path to the library.* Rejected: `/usr/lib64/...` is Fedora-specific and
  would need per-distro discovery for no benefit — the bare soname resolves.
- *`LD_PRELOAD` in the renderer environment.* Superseded, as above.
- *Dropping the sanitized environment and inheriting the daemon's.* Rejected: the
  allowlist exists to keep code-injection variables out of a subprocess that runs
  under the system interpreter.

### 2. The renderer refuses to present an overlay it cannot anchor

On the Wayland backend, `OverlayApplication` checks `Gtk4LayerShell.is_supported()`
before building any window and raises `OSError` when it is false. `main()` already
maps that to `Error: overlay runtime unavailable: ...` and exit 1, and
`OverlayController` already marks health unavailable when the renderer exits, so
capture, transcription, clipboard, and paste continue untouched. Nothing new is
needed for failure isolation — the change is to stop drawing a wrong overlay.

Alternative considered: keep presenting the window and try to position it as a normal
toplevel. Rejected: on Wayland a client cannot position its own toplevel, cannot keep
it above other windows, and cannot guarantee it will not take focus — the presentation
`recording-overlay` requires is not reachable that way.

### 3. The unsupported-reason text is split by cause

When layer shell is unsupported, `check_visual_runtime` distinguishes the two causes
by whether Murmly's own preload is present in the renderer's environment:

- preload absent → the runtime Murmly prepares is at fault; report that.
- preload present but `is_supported()` still false → the compositor does not offer
  `zwlr_layer_shell_v1`; report that, and name the compositor as the reason.

The second branch is **inferred**: this compositor does offer layer shell, so the
false-with-preload case cannot be reproduced here.

### 4. Diagnostics run the helper under the renderer's environment

`overlay_diagnostics` passes `env=renderer_environment(backend)` to the `--check`
subprocess. Without it, doctor answers a different question than the one the user
cares about, and today answers it wrongly on every Plasma Wayland session.

### 5. Injector selection: probe the tool, then trust the delivery

Selection becomes a list of candidates per session type, each with a no-op probe run
once per daemon lifetime and cached:

| Session | Candidates, in order | Probe |
| --- | --- | --- |
| Wayland | `wtype`, `ydotool` | run the tool with an empty payload (`wtype ""`, `ydotool type ""`) |
| X11 | `xdotool` | none — unchanged from today |

A candidate is selected only if it is installed and its probe succeeds. If a selected
candidate then fails during a real delivery, it is marked unusable for the rest of the
session and the next delivery re-selects — the probe is an optimisation for
diagnostics and first-paste correctness, never the only line of defence.

The probes' exact semantics are **unverified** (neither tool is installed here). The
design does not depend on them being right: a probe that wrongly passes costs one
delivery, which degrades to copy-only rather than failing, and a probe that wrongly
fails is visible in `murmly doctor`. Verifying both probes is an apply-time task.

Alternatives considered:

- *Enumerate Wayland globals over `libwayland-client` via `ctypes` and look for
  `zwp_virtual_keyboard_manager_v1`.* Rejected for now: precise, and idiomatic for
  this codebase (`X11FocusObserver` is ctypes), but it needs a registry listener with
  function-pointer structs and `wl_registry_interface` symbol lookup — a large surface
  to answer one boolean that the tool itself answers by exiting non-zero.
- *Shell out to `wayland-info`.* Rejected: not installed by default, and it answers
  the protocol question rather than the "can this tool run" question.
- *Prefer `ydotool` whenever the desktop is Plasma.* Rejected: hardcodes a
  compositor's current protocol support, and forces a root-owned daemon on Plasma
  users whose compositor may later support `wtype`.
- *No probe; demote purely on failure.* Rejected: `murmly doctor` must answer before
  any delivery has happened, and that is exactly the moment the user asks.

### 6. Copy survives an unusable injector

`ClipboardPaster` keeps resolving the clipboard copy command eagerly — without it
there is no delivery of any kind — and makes the injector optional: it records the
method and the reason it is unavailable instead of raising from `__init__`.
`copy_and_paste` returns an outcome that says whether injection happened, and skips
clipboard restoration when it did not, which is what the existing refusal rule
("Refused transcripts remain on the clipboard") already requires.

`SpeechSession.process_recording` maps a non-injected outcome onto the
`ProcessingResult` it already returns for a refused delivery, so every downstream
behavior — the overlay error state, the `copied but not pasted` response, ending a
continuous session — is reached through the paths that exist today rather than a new
one.

### 7. Reporting, not remediation

`murmly doctor` gains a paste-injection section (method, usable, reason, remedy) and
`murmly install` prints the same remedy when injection is unavailable. Remedy text for
a Plasma Wayland session names `ydotool`; its exact commands are written only after
the unit and socket details are verified during apply.

## Risks / Trade-offs

- **The in-process load depends on an import order nothing enforces at runtime.** →
  Guarded three ways: a test that parses the file for module-scope `gi`/`cairo`
  imports, a `sys.modules` check inside `load_layer_shell()` that names the ordering
  bug, and the `is_supported()` refusal that keeps a mis-placed overlay off screen.
- **A machine without gtk4-layer-shell now fails at the `ctypes` call.** → It raises
  `OSError` naming the library, which `main()` already turns into an
  `overlay runtime unavailable` message and a non-zero exit, and diagnostics report
  as the cause. Capture and delivery are unaffected.
- **Users on Plasma Wayland who never installed an injector will now see the overlay
  work while paste still does not.** → That is the honest state; doctor, install, and
  the toggle response all name it, and the transcript stays on the clipboard instead
  of being lost.
- **The probe commands are unverified and could have side effects.** → Empty payloads
  are chosen precisely so nothing is typed; apply verifies both before the change is
  archived, and the empirical demotion path means a wrong probe cannot lose a
  transcript.
- **`ydotool` needs a root-owned daemon and `/dev/uinput`.** → Out of scope by
  decision; Murmly reports the remedy and changes no system state.
- **The two failing behaviors ship as one change.** → They share a cause (an
  unchecked session capability) and a reporting story; splitting them would leave
  `murmly doctor` half-truthful in either half.

## Migration Plan

No configuration, on-disk state, or protocol changes. The overlay protocol, config
schema, and toggle response fields are untouched, so a rollback is a plain revert with
no cleanup. Users who had the overlay silently mis-placed will see it either correctly
placed or absent with a reason; no user action is required for that half. The paste
half requires the user to install an injector, which is what the new reporting tells
them.

## Open Questions

Resolved during apply. The `ydotool` remedy text was written only after reading the
packaged binaries (task 5.1): Fedora ships `ydotool.service`, a system unit running
`/usr/bin/ydotoold` with no arguments; the client resolves its socket as
`YDOTOOL_SOCKET`, else `$XDG_RUNTIME_DIR/.ydotool_socket`, else
`/tmp/.ydotool_socket`; and the daemon's default socket permission is `0600`. A root
daemon therefore writes a socket the user's client neither reads nor could open, so
the printed remedy is a drop-in that sets both the path and the owner. Murmly prints
it filled in for the live session and runs none of it.
