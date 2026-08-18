## Context

See proposal.md — Why for the three symptoms and the evidence behind each. This
section records only the constraints that shape the approach, and marks each fact
as **confirmed** (reproduced on the reporting machine: Fedora 44, Plasma Wayland,
`WAYLAND_DISPLAY=wayland-0`, gtk4-layer-shell 1.3.0, GTK 4.22.4) or **inferred**.

- **Confirmed.** Under PyGObject, `Gtk4LayerShell.is_supported()` is `False` unless
  the process was started with the library preloaded. `/usr/bin/python3
  overlay_renderer.py --check --backend wayland` returns
  `{"available": false, ... "error": "The active Wayland compositor does not support
  Layer Shell."}` with no preload and `{"available": true, "gtk4_layer_shell": true}`
  with `LD_PRELOAD=libgtk4-layer-shell.so.0`.
- **Confirmed.** The bare soname `libgtk4-layer-shell.so.0` resolves through the
  standard loader search path, so no distro-specific absolute path is needed.
- **Confirmed.** An `LD_PRELOAD` naming a library that does not exist makes `ld.so`
  print `ERROR: ld.so: object '...' from LD_PRELOAD cannot be preloaded ... ignored.`
  on stderr and the process runs normally. Setting the preload unconditionally on the
  Wayland backend therefore cannot stop the renderer from starting on a machine
  without gtk4-layer-shell; the layer-shell check is what stops it.
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
  renderer environment, as a code-injection guard against inherited values.
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

### 1. A controlled `LD_PRELOAD`, set by Murmly, only on the Wayland backend

`renderer_environment()` keeps building the child environment from an allowlist and
keeps discarding any inherited `LD_PRELOAD`; on the Wayland backend it then sets its
own constant value, `libgtk4-layer-shell.so.0`.

Alternatives considered:

- *Absolute path to the library.* Rejected: `/usr/lib64/...` is Fedora-specific and
  would need per-distro discovery for no benefit — the bare soname resolves.
- *`ctypes.CDLL(..., mode=RTLD_GLOBAL)` before importing GTK.* Rejected: the library
  has to be loaded ahead of `libwayland-client`, which PyGObject pulls in on import;
  ordering inside an already-running interpreter is not something Murmly can
  guarantee.
- *Dropping the sanitized environment and inheriting the daemon's.* Rejected: the
  allowlist exists to keep code-injection variables out of a subprocess that runs
  under the system interpreter.

The security property in `tests/test_overlay.py:209` is preserved but its assertion
changes meaning: an inherited `LD_PRELOAD` is still stripped, and the value present
on the Wayland backend is Murmly's constant, with no `LD_PRELOAD` on X11.

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

- **Setting `LD_PRELOAD` on a subprocess is a code-injection surface.** → The value is
  a constant chosen by Murmly, never taken from the environment, applied only on the
  Wayland backend, and the inherited-value strip is kept and re-asserted in tests.
- **The preload prints an `ld.so` error to the renderer's stderr when
  gtk4-layer-shell is not installed.** → It is harmless (the process runs and then
  refuses on the layer-shell check) and it lands in the journal where it explains
  itself. `overlay_diagnostics` already falls back to stderr for the detail text.
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

- Which exact `ydotool` setup commands to print for Fedora — package install plus unit
  name — pending verification of the shipped unit's socket path and whether the client
  needs `YDOTOOL_SOCKET`. This changes only the text of a diagnostic message, not the
  specs, the approach, or the task breakdown.
