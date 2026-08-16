---
title: Add Recording Overlay
description: Propose a bottom-centered recording and transcription indicator for KDE Plasma
---

## Why

Murmly currently gives no persistent visual confirmation that microphone capture is active after a shortcut is pressed. A compact, live indicator will make recording state and audio pickup visible without interrupting the user's focused application.

## What Changes

* Add a bottom-centered, non-focusable overlay for KDE Plasma on X11 and Wayland
* Show a microphone and animated waveform while Murmly is listening
* Drive the waveform from smoothed microphone amplitude rather than a decorative loop
* Transition to a distinct processing animation while transcription and paste handling complete
* Hide the overlay when Murmly returns to idle and expose a brief error state when capture or processing fails
* Allow the indicator to be disabled and its bottom margin to be configured
* Continue voice capture and transcription when the visual runtime is unavailable

## Capabilities

### New Capabilities

* `recording-overlay`: Defines the recording, processing, error, placement, accessibility, and graceful-degradation behavior of Murmly's KDE Plasma indicator

### Modified Capabilities

None.

## Impact

The change affects audio capture level reporting, daemon state publication, configuration, system diagnostics, and user-service packaging. It uses GTK 4 and PyGObject for both display protocols, GTK4 Layer Shell for Wayland placement, and the system X11 libraries for X11 window management while preserving the existing UNIX socket command interface and transcription behavior.
