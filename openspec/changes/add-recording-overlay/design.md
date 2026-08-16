---
title: Recording Overlay Design
description: Design the daemon-to-overlay architecture for KDE Plasma X11 and Wayland
---

## Context

The daemon currently owns microphone capture, transcription, clipboard handling, and a synchronous UNIX socket command loop. `SoundDeviceRecorder` buffers raw PCM in its PortAudio callback but does not expose signal levels. The second toggle blocks its request handler until transcription and paste handling finish.

The target environment is Fedora KDE Plasma on X11 and Wayland. The current workstation uses X11. Plasma Wayland implements the layer-shell protocol needed for reliable bottom-center placement, while Plasma X11 implements EWMH window-state conventions and the X Shape extension. GTK 4 and PyGObject are supplied by Fedora RPMs, but the project's isolated `uv` environment cannot import the system `gi` module. GTK4 Layer Shell is an additional package for Wayland and is not currently installed; the X11 libraries are already part of the active desktop runtime.

See `proposal.md` for motivation and `specs/recording-overlay/spec.md` for observable behavior.

## Goals and non-goals

### Goals

* Keep microphone callback work bounded and free of process or socket I/O
* Isolate optional GUI dependencies and crashes from capture and transcription
* Keep raw audio and transcript content out of the visual process
* Produce deterministic state transitions that can be unit tested without a compositor
* Diagnose missing Fedora packages without preventing daemon startup

### Non-goals

* Support GNOME, non-Plasma window managers, or non-Linux desktops in the first implementation
* Make the overlay clickable, draggable, or responsible for stopping capture
* Display transcription text, elapsed time, device selection, or settings
* Build a general notification framework or theme editor
* Package Murmly as an RPM as part of this change

## Decisions

### Run the renderer as a system-Python child process

The daemon will own an `OverlayController` that starts a small renderer with `/usr/bin/python3`, an absolute helper path, `shell=False`, and one inherited socket descriptor. The helper will import Fedora's PyGObject, GTK 4, and GTK4 Layer Shell packages directly. It will not import the daemon, transcription, or clipboard modules.

The renderer starts hidden with the daemon when the overlay is enabled. This avoids first-use latency. If startup fails, the controller records the failure, closes its transport, and becomes a no-op. It may retry on a later idle-to-listening transition with bounded backoff, but it will not enter an unbounded restart loop.

The child receives only the environment needed for the selected display protocol, locale, home directory, and session D-Bus. Wayland receives its display and runtime-directory values. X11 receives `DISPLAY` and `XAUTHORITY`. Python search-path, dynamic-loader, and GTK module-path variables are removed. This limits accidental disclosure of unrelated daemon environment values and prevents interpreter-path or native-library injection.

Alternatives considered:

* Running GTK on a daemon thread was rejected because GTK requires a main-loop owner and would couple GUI failures to the core process.
* Installing PyGObject into the `uv` environment was rejected because it adds native build-tool and development-header requirements despite Fedora already shipping supported bindings.
* A Plasma widget or KWin script was rejected because it would add a separate installation lifecycle and a more complex state transport for a small passive surface.
* Desktop notifications were rejected because they cannot provide stable placement or live amplitude animation.

### Use a private one-way JSON protocol

The parent and renderer will communicate through a private UNIX `socketpair` inherited at process creation. No new filesystem socket, public command, or network listener is introduced. Messages are newline-delimited JSON with a strict maximum size and one of these forms:

```json
{"type":"state","value":"LISTENING"}
{"type":"level","value":0.42}
{"type":"error","duration_ms":2000}
{"type":"shutdown"}
```

The protocol carries state, a normalized scalar level from 0 through 1, and bounded control values. It never carries PCM, transcription text, clipboard content, filenames, or user configuration paths. The renderer rejects malformed, oversized, unknown, and out-of-range messages without terminating.

A dedicated controller thread coalesces level updates and writes at no more than 30 frames per second. Socket backpressure can block that thread without blocking the audio callback, daemon command handling, or transcription. State and error messages take precedence over pending level updates.

Alternatives considered:

* Extending the command socket with subscriptions was rejected because the existing synchronous server blocks during processing and a public stream would expand the security and compatibility surface.
* Passing Python objects through `multiprocessing` was rejected because the renderer uses a different interpreter environment and the protocol should remain explicit and constrained.

### Derive a smoothed scalar level from PCM callbacks

`SoundDeviceRecorder` will accept an optional level sink. Each callback will continue buffering the same PCM bytes and will overwrite a single reference to the latest signed 16-bit frame. A dedicated metering thread wakes at no more than 30 Hz, calculates root mean square amplitude from the latest frame, smooths it, and calls the level sink. The callback performs no RMS calculation, GUI work, subprocess work, queue operation, socket operation, JSON serialization, or lock acquisition.

The controller maps RMS to decibels relative to full scale and then into the display range. Values at or below -60 dBFS map to zero, and values at or above -6 dBFS map to one. Asymmetric exponential smoothing gives speech a fast attack and slower release:

```text
target = clamp((dbfs + 60) / 54, 0, 1)
alpha  = 0.55 when target rises, otherwise 0.18
level  = alpha * target + (1 - alpha) * previous_level
```

The renderer applies a visual minimum bar height, so silence remains visibly alive without misrepresenting it as speech. Level state resets when capture starts and when the daemon leaves listening.

Each meter generation owns its own stop event. If a sink remains blocked beyond the bounded stop interval, metering is disabled for that recorder instead of clearing the event or starting an overlapping worker. Stream references are detached before native stop and close calls, and meter cleanup runs from `finally` blocks so native errors cannot leave the recorder logically active.

Alternatives considered:

* Peak amplitude was rejected because isolated samples produce a jumpy meter.
* Forwarding PCM to the renderer was rejected for privacy, memory, bandwidth, and process-isolation reasons.

### Publish overlay state at daemon transition boundaries

The daemon will notify the controller only after microphone startup succeeds, before synchronous processing starts, after processing completes, and when an exception occurs. Microphone startup will be brought under the same exception boundary as processing so a failed device does not terminate the server loop.

The state sequence is:

```text
IDLE -> LISTENING -> THINKING -> IDLE
  |          |            |
  +-------- ERROR <-------+
```

An error event owns its two-second visual lifetime independently of the daemon's immediate return to idle. Overlay methods never raise into the daemon. Failures are sent to the service journal and retained as controller health for the `doctor` command.

Alternatives considered:

* Polling `murmly status` was rejected because it cannot supply audio levels and can miss short transitions.
* Letting the renderer infer state from audio was rejected because silence is valid during recording and processing has no live microphone stream.

### Bound command handling independently from processing

The UNIX socket accept loop dispatches accepted requests to at most eight daemon worker threads. Each request has a two-second receive timeout and a 4 KiB limit. Accepted sockets are tracked and closed during shutdown, which lets the listener, socket path, and overlay terminate promptly even when transcription is blocked or a client never finishes a request. Speech state remains serialized by the daemon state lock; concurrent toggles during processing receive the existing busy response.

Alternatives considered:

* Keeping all command work on the accept loop was rejected because a slow transcription prevents prompt SIGTERM cleanup.
* Creating unbounded request threads was rejected because incomplete same-user clients could retain resources indefinitely.

### Render one fixed GTK surface through protocol-specific placement adapters

The helper will create one undecorated GTK 4 window and select its placement adapter from the verified Plasma session type. Both adapters use the same view state, Cairo drawing code, dimensions, animation timing, monitor-selection rule, and empty GDK input region.

On Wayland, the Layer Shell adapter places the window on the overlay layer, anchors it to the bottom edge, applies the configured margin, and disables keyboard interactivity.

On X11, the native adapter obtains the GTK surface's XID through GDK X11 and uses the system X11 libraries through `ctypes`. It applies the EWMH notification window type plus above, sticky, skip-taskbar, and skip-pager states; moves the window to the computed bottom-center coordinates; raises it; and clears the X Shape input region. The adapter opens the display selected by GTK, validates every required library symbol, and fails visually if the window manager cannot provide the required behavior. It does not add a Python X11 dependency or execute shell commands.

At the start of each visible session, the renderer selects the monitor whose logical geometry contains the desktop origin. If no monitor contains the origin, it selects the monitor with the lexically first connector identifier. The selection and its absolute geometry remain fixed until the overlay hides, which prevents animation or monitor topology updates from moving an active indicator. The X11 adapter computes `x = monitor.x + (monitor.width - 156) / 2` and `y = monitor.y + monitor.height - 48 - margin`; Layer Shell delegates equivalent centering to the compositor.

The surface uses stable dimensions of 156 by 48 logical pixels. It contains no instructional text. Listening uses a microphone symbol plus seven amplitude bars, processing uses the same stable geometry with an indeterminate bar sequence, and error uses an error symbol. The neutral translucent background, light foreground, and red recording accent provide contrast without making color the only state signal.

Reduced-motion mode replaces continuous interpolation with stable state symbols and level steps updated at no more than four times per second. Processing uses a static processing symbol, and the error symbol remains static.

Alternatives considered:

* One normal GTK utility-window path was rejected because Wayland clients cannot set global position or stacking without compositor support, while GTK 4 removed its portable X11 window-position API.
* Resizing the capsule with amplitude was rejected because it creates distracting layout movement and complicates bottom-center placement.

### Add bounded overlay configuration and diagnostics

Configuration will add an `[overlay]` table with these values:

```toml
[overlay]
enabled = true
bottom_margin_px = 32
reduced_motion = false
```

The bottom margin is constrained to 0 through 512 logical pixels and falls back to 32 when invalid. Existing configurations remain valid because every value has a default.

`murmly doctor` will report the session type, desktop, selected renderer backend, system interpreter, PyGObject availability, GTK version, GDK X11 and native X11 availability, GTK4 Layer Shell availability, and whether the overlay can run. The renderer helper will expose a non-visual check mode so diagnostics use the same imports and native-library checks as the actual process. Missing or unsupported components produce an unavailable result rather than a nonzero daemon startup.

Fedora installation documentation will list `gtk4`, `python3-gobject`, `libX11`, and `libXext` as shared system packages, plus `gtk4-layer-shell` for Wayland. The project will not declare PyGObject or a Python X11 package as a dependency because Fedora already supplies the supported native stack.

### Separate state tests from compositor smoke tests

Unit tests will inject fake overlay controllers and level sinks into the recorder and daemon. They will cover RMS normalization, smoothing, update coalescing, transition order, startup and processing errors, disabled configuration, malformed protocol messages, and child-process failure isolation without importing GTK.

Renderer view-state, backend-selection, geometry, protocol, and native-call preparation logic will remain importable without initializing GTK. Runtime integration tests will select X11 or Wayland from the active Plasma session and skip only when that backend's packages or display server are unavailable. KDE Plasma smoke checks will verify placement, stable dimensions, focus preservation, click-through input, animation, reduced motion, and multi-display selection before release.

## Risks and trade-offs

* System package drift could change introspection APIs. Pin supported minimum versions in diagnostics and fail visually while preserving core operation.
* Executing a helper with system Python creates two Python environments. Keep the helper self-contained, test its check mode with `/usr/bin/python3`, and exchange language-neutral JSON only.
* Layer-shell and EWMH behavior outside KDE Plasma is compositor or window-manager dependent. Gate support on Plasma plus the detected display protocol and document other desktops as unsupported for this release.
* Direct X11 calls can fail or target a stale window. Resolve the XID only after GTK realization, keep native calls inside one adapter, validate return values, and hide the surface if placement or input shaping fails.
* PCM frame copying and buffer extension still consume callback time. Benchmark the real callback under a blocked level sink, coalesce frames without locks, and disable metering rather than starting a second worker after a blocked shutdown.
* A stalled renderer could accumulate updates. Coalesce levels to one latest value, cap the send rate, and isolate all writes on the controller thread.
* Selecting the desktop-origin monitor may not match every user's preferred display. Keep the rule deterministic for this release and leave configurable output selection to a later change.
* A translucent surface can have inconsistent contrast over varied content. Use a sufficiently opaque neutral background, distinct shapes, and contrast checks against light and dark test backgrounds.

## Migration plan

1. Add configuration defaults and controller abstractions with the overlay disabled in tests.
2. Add level reporting and daemon transition publication behind the controller boundary.
3. Add the system-Python renderer, X11 and Wayland placement adapters, runtime check mode, and Fedora package documentation.
4. Install the visual packages needed by the active display protocol and restart the user service on supported KDE Plasma systems.
5. Validate the complete lifecycle in the active Plasma X11 session and retain a skippable Wayland integration check for a compatible session.

Existing users without visual packages retain current voice-to-text behavior and receive an actionable `doctor` result. Rollback consists of setting `overlay.enabled = false` or reverting the package; no stored data or configuration migration must be reversed.
