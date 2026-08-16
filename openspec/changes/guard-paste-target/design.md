---
title: Guard Paste Target Design
description: Technical approach for verifying the delivery target and ordering clipboard restoration behind transcript consumption
---

## Context

See proposal.md — Why. The relevant current state:

`SpeechSession.process_recording` transcribes and then calls `ClipboardPaster.copy_and_paste`, which reads the old clipboard, copies the transcript, spawns the paste injector, sleeps a fixed `restore_delay_ms`, and copies the old clipboard back. Nothing in that path records or checks which window will receive the keystroke, and the restore is ordered by a timer rather than by the receiving application.

Two constraints shape the approach:

* Delivery correctness must not depend on the overlay. The overlay is optional, KDE Plasma-gated, and runs in a separate system-Python process; transcript delivery runs on plain X11 or Wayland with no desktop gating.
* The daemon lives in an isolated `uv` environment without PyGObject. Verified during exploration: `ctypes.util.find_library("X11")` resolves and `XOpenDisplay` succeeds from that environment, so native X11 reads need no new package and no helper process.

```text
  BEFORE                             AFTER

  stop capture                       stop capture ──▶ record target
       │                                  │
  transcribe (seconds)               transcribe (seconds)
       │                                  │
  read old clipboard                 target still focused?
  copy transcript                     ├── no ──▶ copy only, no restore,
  inject Ctrl+V  ◀── lands anywhere   │           signal error
  sleep 200ms                         └── yes ─▶ read old clipboard
  restore old  ◀── races the app                 copy transcript
                                                 inject Ctrl+V
                                                 wait for consumption
                                                 (floor ≤ t ≤ ceiling)
                                                 restore old
```

## Goals / Non-Goals

**Goals:**

* Decide the delivery target early and verify it late, with a single comparison that is cheap enough to run on every delivery
* Distinguish "this session cannot observe focus" (deliver unverified) from "this session can observe focus but the read failed now" (refuse)
* Order clipboard restoration behind evidence that the transcript was taken, with both a floor and a ceiling so neither an early probe nor a slow application can corrupt the result
* Require no change to the overlay protocol or renderer

**Non-Goals:**

* Re-focusing the original window before pasting. Stealing focus back contradicts the design principle the overlay already establishes.
* Verifying the target on Wayland. No compositor-portable focus query exists; a KWin-scripting path was considered and declined.
* Confirming that the injected keystroke was received. Murmly can observe that the clipboard was read, not that the application acted on the key event.
* Any transcript history or retrieval command.

## Decisions

### Use `_NET_ACTIVE_WINDOW` as the target identity, not `XGetInputFocus`

`XGetInputFocus` returns the window holding input focus, which moves between child widgets inside a single application. Comparing it directly would refuse delivery whenever the user clicked from one text field to another in the same window — a false refusal on the most common interaction there is.

`_NET_ACTIVE_WINDOW` on the root window is the window manager's notion of the active toplevel and is stable across intra-application focus movement. Verified present under KWin during exploration.

*Alternative considered:* walk up from `XGetInputFocus` to the toplevel with `WM_STATE`. Equivalent result, more code, more round trips, and it re-implements what the WM already publishes.

### Probe verification support once at startup; refuse only on a failed read

A non-EWMH window manager never publishes `_NET_ACTIVE_WINDOW`. Treating that as "read failed" would refuse every delivery forever, which is worse than the bug being fixed.

So the daemon classifies the session once, at startup, into *verifying* or *unverified*:

| Session | `_NET_ACTIVE_WINDOW` present | Mode | On focus change |
| --- | --- | --- | --- |
| X11, EWMH WM | yes | verifying | refuse |
| X11, non-EWMH WM | no | unverified | deliver |
| Wayland | n/a | unverified | deliver |

Within a *verifying* session, a read that fails at delivery time is a refusal — fail closed, matching the convention the overlay work established for native runtime checks. This is what makes the spec's two behaviors consistent rather than contradictory.

### Pair the window id with a stable property

X11 window ids are recycled. A target that closed and whose id was reissued to a new window would compare equal and deliver into the wrong application — the exact failure this change exists to prevent.

The recorded identity is therefore the window id together with a property read at record time (`_NET_WM_PID`, falling back to `WM_CLASS` when absent). Both must match at delivery time. The extra property read costs one round trip on a path that already opens a display.

### Detect consumption through the clipboard tool's own selection-request signal

Both clipboard tools Murmly already requires can report that the selection was served:

* X11: `xclip -loops N` exits after serving N selection requests
* Wayland: `wl-copy --paste-once` exits after serving one paste request

Process exit is the consumption signal; no polling, no new dependency. Both flags were confirmed present during exploration.

Two consequences drive the rest of the design:

**Applications probe `TARGETS` before transferring data.** A request-count signal can therefore fire on a probe rather than on the real transfer. Rather than guessing a request count, restoration waits for `max(consumption signal, floor)` and is capped by a ceiling. The floor absorbs the probe; the ceiling preserves the existing guarantee that Murmly returns to idle promptly.

**Serving once releases the clipboard.** That is correct for the deliver-and-restore path, and wrong everywhere else. So the consumption-signalling copy is used *only* when a paste was injected and restoration is enabled. The refusal path and the restoration-disabled path both use an ordinary persistent copy, which is what makes "a refused transcript stays on the clipboard" true.

*Alternative considered:* own the X11 selection from inside the daemon and watch for `SelectionRequest` directly. More precise, but it makes the daemon a selection owner for the lifetime of the transcript and duplicates what `xclip` exists to do.

### Record the target in the daemon, not in the paster

The target is recorded in the toggle handler immediately after `stop_recording()` succeeds and while the state lock is still held, then passed forward into processing. Recording it any later — inside the paster, at copy time — would reintroduce the original bug, because by then transcription has already run.

The cost is that the delivery target becomes an explicit parameter threaded from the daemon through the session into the paster. That is the point: the target is a decision made at a moment in the lifecycle, so it belongs to the lifecycle, not to the clipboard helper.

### Reuse the overlay error state as-is

A refusal is signalled with the existing `publish_error()` call, which carries only a duration. No new overlay message type, no renderer change, no protocol version bump — and the spec's requirement that delivery signals carry no transcript content is satisfied by construction rather than by review.

## Risks / Trade-offs

* **A `TARGETS` probe is mistaken for consumption** → the restoration floor means an early signal cannot restore before the real transfer window has passed; the ceiling means a missed signal still returns to idle.
* **Window id recycled between record and delivery** → identity pairs the id with `_NET_WM_PID` / `WM_CLASS`, so a reissued id does not compare equal.
* **A window manager transiently clears `_NET_ACTIVE_WINDOW` during animations or desktop switches, causing a spurious refusal** → refusals are non-destructive by design (the transcript is on the clipboard), the reason is logged, and `verify_target = false` is a one-line escape hatch reported by `murmly doctor`.
* **Users who deliberately alt-tab mid-transcription to redirect the paste lose that workflow** → documented as breaking in the proposal, with the configuration opt-out and the clipboard fallback as the migration path.
* **An installed `xclip` or `wl-copy` predates the flag** → probe the flag at startup and fall back to the current fixed-delay restore, reporting the degraded mode in diagnostics rather than failing delivery.
* **Verification adds X11 round trips to every delivery** → two property reads on a path that already spawns two or three subprocesses; negligible next to transcription.
* **Wayland users get a weaker guarantee than X11 users** → made explicit in `murmly doctor` rather than implied to be equivalent. Accepted deliberately.

## Migration Plan

The change is additive and self-disabling:

1. `[clipboard] verify_target` defaults to `true`; setting it to `false` restores today's unconditional paste while keeping the clipboard-preservation and consumption fixes.
2. Sessions that cannot observe focus keep today's delivery behavior with no configuration.
3. `murmly doctor` reports session support, configured state, and any degraded consumption mode, so the active behavior is inspectable before a user reports a problem.

Rollback is `verify_target = false` and a daemon restart. No state, no schema, and no overlay protocol changes to unwind.

## Open Questions

* Concrete values for the restoration floor and ceiling. Sensible starting points are the existing `restore_delay_ms` as the floor and roughly one second as the ceiling, tuned against real applications during implementation. Tuning these does not change the specs, the approach, or the task breakdown.
*Resolved during implementation:* `WM_CLASS` is always paired with `_NET_WM_PID` rather than used only as a fallback, and identity comparison requires all three fields to match exactly. A property that is absent on both reads compares equal, and a property that became unreadable between the two reads compares unequal and therefore refuses — which is the fail-closed behavior the spec requires. This removes the need to decide empirically which property is authoritative. Verified against a live application, which published both `_NET_WM_PID` and `WM_CLASS`.

## Superseded Decision: consumption detection through the clipboard tool

The decision above to detect consumption via `xclip -loops N` / `wl-copy --paste-once` was **disproven during implementation** and must not be built as written.

Measured on Plasma X11: with **no reader present at all**, `xclip -quiet -selection clipboard -loops 1`, `-loops 2`, and `-loops 3` all exit within 1.2 seconds. Klipper, the KDE clipboard manager, requests the selection immediately on every ownership change. The request count therefore carries no information about the receiving application, and worse, the owner exits before the target ever reads — a follow-up read returned an empty clipboard, meaning the mechanism destroys the transcript rather than protecting it.

The design flagged a `TARGETS` probe as a risk to be absorbed by a timing floor. The real cause is more fundamental: a clipboard manager is a second, always-present consumer, so no fixed request count can distinguish it from the target. A replacement approach must be chosen before the restoration work proceeds.
