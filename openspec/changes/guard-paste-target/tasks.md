---
title: Guard Paste Target Tasks
description: Track implementation and validation of delivery target verification and consumption-ordered clipboard restoration
---

## 1. Configuration and session capability detection

- [x] 1.1 Add a `[clipboard] verify_target` configuration field defaulting to `true`, using the existing bounded boolean parsing
- [x] 1.2 Add configuration tests for the default, explicit values, an invalid table shape, and a non-boolean value
- [x] 1.3 Implement a startup probe that classifies the session as verifying or unverified by reading `_NET_ACTIVE_WINDOW` from the root window, treating an absent property or an unopenable display as unverified rather than as a failure
- [ ] 1.4 Implement a startup probe for clipboard consumption support that detects whether the resolved copy command accepts its selection-request flag, falling back to fixed-delay restoration when it does not
- [x] 1.5 Add tests proving Wayland sessions, non-EWMH X11 sessions, and sessions with no display all classify as unverified without raising

## 2. Delivery target identity

- [x] 2.1 Implement reading the active toplevel window id plus its `_NET_WM_PID`, falling back to `WM_CLASS` when the process id property is absent
- [x] 2.2 Implement an identity comparison that treats a differing window id, a differing paired property, an absent target, or a failed read as a non-match
- [x] 2.3 Resolve design.md's open question on identity pairing by checking captured identities against real applications, and record the outcome in the design
- [x] 2.4 Add tests covering an unchanged target, a changed target, a recycled window id whose paired property differs, a closed target, and a read failure
- [x] 2.5 Confirm the recording overlay never registers as the active toplevel, so its presence cannot cause a refusal

## 3. Lifecycle integration

- [x] 3.1 Record the delivery target in the toggle handler immediately after capture stops, while the state lock is held and before the state becomes `THINKING`
- [x] 3.2 Thread the recorded target from the daemon through the speech session into transcript delivery without widening what delivery knows about the daemon
- [x] 3.3 Ensure target recording failures never abort capture or transcription, leaving no recorded target so a verifying session fails closed and refuses delivery, per the spec's "was never recorded" refusal
- [x] 3.4 Add daemon tests asserting the target is recorded before transcription begins and is not re-read afterwards

## 4. Delivery decision

- [x] 4.1 Implement the refusal path: copy the transcript with an ordinary persistent copy, skip the paste injection, and skip clipboard restoration
- [x] 4.2 Publish the existing overlay error state on refusal without adding an overlay message type or changing the renderer
- [x] 4.3 Return a toggle response distinguishing a delivered transcript from one that was copied but not pasted
- [x] 4.4 Log refusals with the reason and without transcript text, clipboard contents, or window identity
- [x] 4.5 Bypass verification entirely when `verify_target` is disabled or the session is unverified, preserving today's injection behavior
- [x] 4.6 Add tests for delivery, refusal on focus change, refusal on unreadable focus, disabled verification, and unverified sessions
- [x] 4.7 Apply the same guard to `murmly spike --paste`, which also stops capture before transcribing and delivering, with tests

## 5. Consumption-ordered clipboard restoration

- [ ] 5.1 Implement transcript copying that exposes a consumption signal on the deliver-and-restore path only, keeping ordinary persistent copies for the refusal and restoration-disabled paths
- [ ] 5.2 Restore the previous clipboard only after the consumption signal, never before the restoration floor, and never after the ceiling
- [ ] 5.3 Choose concrete floor and ceiling values against real applications and record them in the design, resolving design.md's open question
- [ ] 5.4 Ensure restoration never blocks the daemon's return to idle beyond the ceiling
- [ ] 5.5 Add tests for prompt consumption, consumption before the floor, no consumption within the ceiling, and restoration disabled

## 6. Diagnostics and documentation

- [ ] 6.1 Report delivery target verification support, configured state, and any degraded consumption mode in `murmly doctor`
- [ ] 6.2 Add diagnostics tests for a supported and enabled session, a supported and disabled session, an unverified session, and degraded consumption
- [ ] 6.3 Document the delivery guarantee, the X11 and Wayland asymmetry, the `verify_target` option, and the manual-paste recovery path in the README
- [ ] 6.4 Document the breaking behavior change and the configuration rollback for users who relied on redirecting a paste mid-transcription

## 7. Validation

- [ ] 7.1 Run the full unit suite and confirm no regression in existing audio, daemon, overlay, and integration coverage
- [ ] 7.2 Validate the change with `openspec validate guard-paste-target --strict`
- [x] 7.3 Verify live on Plasma X11 that a transcript delivers normally when focus is unchanged
- [x] 7.4 Verify live on Plasma X11 that alt-tabbing during transcription refuses delivery, leaves the transcript on the clipboard, and shows the overlay error
- [ ] 7.5 Verify live that a slow-reading application receives the transcript rather than the restored previous clipboard
- [ ] 7.6 Record any Wayland behavior that could not be exercised in this environment
