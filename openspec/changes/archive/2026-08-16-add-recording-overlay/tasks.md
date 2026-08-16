---
title: Recording Overlay Tasks
description: Track implementation and validation of the KDE Plasma recording overlay
---

## 1. Configuration and contracts

- [x] 1.1 Add `[overlay]` configuration fields for enabled state, bottom margin, and reduced motion with bounded parsing and defaults
- [x] 1.2 Add configuration tests for defaults, valid values, invalid table shapes, and out-of-range margins
- [x] 1.3 Define the overlay state, level-sink, controller health, and lifecycle contracts without importing visual dependencies

## 2. Audio-level pipeline

- [x] 2.1 Implement signed 16-bit PCM RMS normalization and dBFS mapping as pure functions
- [x] 2.2 Implement asymmetric attack and release smoothing with reset behavior at recording boundaries
- [x] 2.3 Add unit tests for silence, representative speech levels, clipping, bounds, attack, release, and reset behavior
- [x] 2.4 Add an optional non-raising level sink to `SoundDeviceRecorder` while preserving the existing PCM buffer and sample-rate fallback behavior
- [x] 2.5 Extend recorder tests to prove sink failure cannot interrupt or alter captured audio

## 3. Overlay process controller

- [x] 3.1 Implement the private socketpair protocol with strict message types, numeric bounds, maximum message size, and no audio or transcript payloads
- [x] 3.2 Implement the controller thread that prioritizes state events and coalesces level events to at most 30 updates per second
- [x] 3.3 Launch the renderer through absolute `/usr/bin/python3` and an absolute helper path without a shell or inherited Python search paths
- [x] 3.4 Limit the renderer environment to required Wayland, locale, home, desktop, and session D-Bus values
- [x] 3.5 Implement clean shutdown, child health reporting, bounded restart backoff, and no-op degradation when launch or transport fails
- [x] 3.6 Add controller tests for event order, coalescing, backpressure isolation, sanitized environment, shutdown, child exit, and restart bounds
- [x] 3.7 Detect the active Plasma X11 or Wayland backend and sanitize only that backend's display environment, with focused launch tests

## 4. Daemon lifecycle integration

- [x] 4.1 Inject the level sink and overlay controller into speech-session and daemon construction paths
- [x] 4.2 Publish listening only after microphone startup succeeds, thinking before processing, and idle after successful completion
- [x] 4.3 Bring microphone startup under daemon error handling and publish a bounded error event for startup or processing failures
- [x] 4.4 Ensure overlay exceptions never change toggle responses, capture, transcription, clipboard, or paste behavior
- [x] 4.5 Close the overlay controller during daemon shutdown without delaying socket cleanup
- [x] 4.6 Extend daemon tests for successful transitions, startup failure, processing failure, error lifetime, disabled overlay, renderer failure, and shutdown
- [x] 4.7 Bound concurrent command clients, request size, and receive time while closing accepted sockets during shutdown

## 5. GTK4 Layer Shell renderer

- [x] 5.1 Create a self-contained system-Python renderer with delayed visual imports and a non-visual dependency check mode
- [x] 5.2 Implement incremental JSON-line parsing that rejects malformed, oversized, unknown, and out-of-range messages without terminating
- [x] 5.3 Add parser and view-state tests that run inside the project environment without initializing GTK
- [x] 5.4 Build the fixed 156 by 48 logical-pixel Layer Shell surface with bottom anchoring, configured margin, overlay stacking, and stable geometry
- [x] 5.5 Disable keyboard interaction and set an empty input region so focus and pointer input remain with the underlying application
- [x] 5.6 Implement deterministic desktop-origin monitor selection that remains fixed during each visible session
- [x] 5.7 Render microphone plus seven bounded level bars for listening, an indeterminate processing presentation, and a static transient error presentation
- [x] 5.8 Implement reduced-motion state symbols and level steps capped at four updates per second
- [x] 5.9 Add optional renderer integration tests that skip cleanly without GTK4 Layer Shell or a compatible Wayland compositor
- [x] 5.10 Extend runtime checks to report GTK, GDK X11, native X11, and Layer Shell availability independently for the selected backend
- [x] 5.11 Implement the X11 EWMH placement, stacking, sticky, taskbar/pager exclusion, and X Shape click-through adapter without a Python X11 dependency
- [x] 5.12 Apply transparent GTK surface styling and verify fixed geometry through the shared X11 and Wayland view path
- [x] 5.13 Add pure X11 geometry/backend tests and an optional live Plasma X11 integration test

## 6. Diagnostics and documentation

- [x] 6.1 Extend `murmly doctor` with desktop, system interpreter, PyGObject, GTK 4, GTK4 Layer Shell, and aggregate overlay availability results
- [x] 6.2 Add doctor tests for available, partially installed, unsupported-session, and helper-failure results
- [x] 6.3 Document Fedora installation of `gtk4`, `python3-gobject`, and `gtk4-layer-shell` and explain why the system interpreter owns the renderer
- [x] 6.4 Document overlay configuration, state behavior, reduced motion, failure isolation, and KDE Plasma support boundaries
- [x] 6.5 Update the user-service guidance and troubleshooting notes for renderer startup, journal diagnostics, disablement, and rollback
- [x] 6.6 Extend doctor tests and documentation for KDE Plasma X11, its native packages, and protocol-specific troubleshooting

## 7. End-to-end validation

- [x] 7.1 Run the complete unit test suite and resolve regressions introduced by the overlay change
- [x] 7.2 Run the helper check mode with `/usr/bin/python3` and verify the active X11 backend plus actionable output when Wayland Layer Shell is missing
- [x] 7.3 Measure audio-callback and controller update behavior to verify bounded callback work and the 30-frame-per-second send cap
- [x] 7.4 Validate listening, silence decay, processing, success, startup error, processing error, and disabled-overlay lifecycles on the active KDE Plasma X11 session
- [x] 7.5 Validate X11 bottom-center placement, stable geometry, focus preservation, pointer click-through, reduced motion, light and dark contrast, and multi-display selection
- [x] 7.6 Run strict OpenSpec validation and reconcile the implementation with every recording-overlay scenario
