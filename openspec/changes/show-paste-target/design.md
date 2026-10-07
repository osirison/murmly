# Design

## Context

Murmly already records the delivery target's identity when capture stops, as a
`WindowIdentity` of window id, process id and application class
(`transcript-delivery`: "Delivery target recorded before transcription"). On X11 the
application class is the class part of `WM_CLASS` (`konsole`, `firefox`, `Code`). On
Windows it is the basename of the foreground process's executable
(`WindowsTerminal.exe`). Wayland has no focus observer, so it records nothing.

Two earlier decisions constrain this change:

- `all-os-distributions/design.md`, "Focus observation by owning application,
  never by window title". Titles would need Screen Recording permission on macOS,
  and the target is an application, not a document.
- `transcript-delivery`'s "Delivery signals exclude transcript content", which keeps
  window identity out of overlay messages. This change relaxes that rule for one
  thing — the application's display name — and the spec delta says so in full.

## Goals / Non-Goals

**Goals:**

- Show a name a person recognises, not an internal identifier, wherever the
  platform makes one available without a permission.
- Name the application that will actually receive the paste: the live focus while
  listening, then the recorded target, which is the one verified before pasting.

**Non-Goals:**

- Window or document titles, in any form.
- A name on Wayland. Murmly cannot see the focused application there, and guessing
  would be worse than saying nothing.
- Choosing a destination. The name is information; changing focus is still how the
  person redirects their words.

## Decisions

### Readable names from the platform's own catalogue

- **Linux (X11)**: match the `WM_CLASS` class against the installed XDG desktop
  entries (`StartupWMClass` first, then the entry's file name), and use the entry's
  localised `Name`. Read the entries with `pyxdg` (`xdg.DesktopEntry`,
  `xdg.BaseDirectory`), a small pure-Python library, rather than parsing
  `.desktop` files by hand. Fall back to the class string when nothing matches.
- **Windows**: read `FileDescription` from the executable's version resource with
  `win32api.GetFileVersionInfo`. `pywin32` is already a Windows dependency. Fall back
  to the executable name without `.exe`.

Results are cached per identity for the life of the daemon, so resolution runs once
per application, not once per poll.

Alternatives considered: `Gio.DesktopAppInfo` (needs PyGObject and its native
stack); `GetWindowText` or `_NET_WM_NAME` (window titles, ruled out above).

### Live name while listening: poll the existing observer

While capture runs, the overlay is shown and `overlay.show_target` is on, the daemon
calls the focus observer's `active_window()` four times a second, and sends the
overlay a message only when the resolved name changes. Polling uses the observer
already in place, on both platforms, with no event loop to add. Event-driven
alternatives (X11 `PropertyNotify` on `_NET_ACTIVE_WINDOW`, Windows
`SetWinEventHook` for `EVENT_SYSTEM_FOREGROUND`) would react faster but need
per-platform event plumbing; the spec allows a second, which polling meets
comfortably. Polling stops the moment capture stops; from then on the recorded
target's name is used and never refreshed.

### Carried as one optional field

Overlay state messages gain an optional `target_name`; the toggle response gains
`target` and, on a refusal, `in_front`. Each is omitted when unknown or when naming
is off, never set to an empty string. Existing fields are untouched, so current
clients keep working.

### Speech-session capture shows a fixed label

Capture started by the speech-session key is bound for the session, not a window,
and the session declaration carries no client name today. The overlay shows "your
assistant". If the declaration later gains a name, the label can use it without a
spec change.

### `overlay.show_target`, on by default

An application name reveals far less than the live transcript, which is off by
default for exactly that reason, and it is what makes a refused delivery
understandable before it happens. The setting exists for screen sharing and
recordings. Doctor reports it under the overlay section.

## Risks / Trade-offs

- [Class names can be cryptic (`org.kde.konsole`) or shared by every Electron app]
  → Resolve through desktop entries first; the fallback is still the identity
  Murmly verifies against, so it is never wrong, only terse.
- [Polling while listening costs CPU] → One property read or Win32 call per 250 ms,
  only while capture runs and only with the setting on; resolution is cached.
- [An application name on a shared screen is still information] → The setting turns
  it off; titles are never shown.
- [The live name can differ from the final target if focus moves in the last
  instant] → The name freezes to the recorded target when capture stops, which is
  the one Murmly verifies, so what the person sees after stopping is always the
  truth.

## Migration Plan

Additive. The new key defaults to `true`, so the name appears after an upgrade. A
person who prefers not to see it sets `overlay.show_target = false`. The toggle
response only gains fields.
