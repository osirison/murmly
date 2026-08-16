---
title: Guard Paste Target
description: Deliver transcripts only to the application the user was dictating into, and never destroy a transcript that was not delivered
---

## Why

Murmly decides where a transcript goes at the moment it injects Ctrl+V, which is seconds after the user stopped speaking. Anything that takes focus in that window — alt-tabbing, a notification, a browser tab finishing a load — silently redirects the transcript into an application the user never intended, and the clipboard restore that follows 200 ms later erases the only copy. The same restore also races the target application: an application that reads the clipboard late receives the *restored previous* clipboard instead of the transcript, so the user's own earlier clipboard content is pasted in place of their speech.

Both failures are silent, both are unrecoverable, and both became more visible now that the recording overlay tells users precisely when processing finished — the overlay was built specifically not to disturb focus, while the paste path neither preserves nor verifies it.

## What Changes

* Record the intended delivery target when capture stops, before transcription begins, rather than discovering it after transcription ends
* Verify the target still holds focus immediately before injecting the paste keystroke
* **BREAKING**: refuse to inject the paste when the focused window changed during transcription. The transcript is copied to the clipboard and left there for the user to place manually, instead of being typed into an unintended application. Users who relied on changing focus mid-transcription to redirect a paste must now paste manually
* Suppress the clipboard restore whenever delivery was refused, so a transcript that was never delivered always remains on the clipboard
* Widen and bound the delay before the previous clipboard is restored, so a slow application is less likely to receive stale clipboard content in place of the transcript, and no configured value can stall the daemon
* Signal a refused delivery through the existing overlay error state, carrying no transcript content
* Add a configuration option to disable target verification for users who prefer the current unconditional paste
* Report whether the active session supports target verification in `murmly doctor`
* Limit target verification to X11, where focus is reliably observable. Wayland sessions keep today's delivery behavior and gain only the clipboard-preservation and consumption guarantees, and report themselves as unverified

## Capabilities

### New Capabilities

* `transcript-delivery`: Defines where a completed transcript is delivered, when delivery is refused, how the transcript stays recoverable after a refusal, and when the previous clipboard may be restored

### Modified Capabilities

None. Murmly's clipboard and paste behavior predates spec coverage and has no existing capability under `openspec/specs/`, so this change introduces the capability rather than modifying one.

## Impact

The change affects transcript delivery in `src/murmly/integrations.py`, the daemon lifecycle that decides when delivery is attempted in `src/murmly/daemon.py`, overlay error publication, configuration, and `murmly doctor` diagnostics. Target verification reads the active window through the X11 libraries already required by the overlay backend, using `ctypes` from the daemon's own interpreter, so it adds no Python dependency and no additional helper process. Clipboard consumption detection uses capabilities already present in the clipboard tools Murmly requires. Recording, transcription, and overlay presentation are unchanged.

Users on Wayland receive a strictly smaller guarantee than users on X11. This asymmetry is deliberate and must be visible in diagnostics rather than implied to be equivalent.
