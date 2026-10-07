# Proposal

## Why

Murmly decides where a transcript goes at the moment capture stops: the application
in front then. Nothing on screen says which application that is, so the person
finds out only afterwards — when the words land somewhere unexpected, or when
delivery is refused because another application came forward. Naming the
destination while they speak lets them see where their words are going before they
go, and switch if it is wrong.

## What Changes

- While listening, the overlay names the application currently in front, and
  updates the name as focus moves, because that application is where the words will
  go if capture stops now.
- Once capture stops, the overlay names the recorded target. That name no longer
  changes, matching the target Murmly verifies before pasting.
- When delivery is refused, the error presentation names the application the words
  were meant for.
- Capture started by the speech-session key names the speech session as its
  destination instead of an application.
- The toggle response reports the target's application name and, on a refusal, the
  name of the application that was in front instead.
- The name is the application's own readable name ("Konsole", "Firefox"), derived
  from the identity Murmly already records for verification. Window titles are
  never read, so no document name, email subject or page title reaches the screen,
  and no new permission is needed.
- Where the session cannot observe the focused application (Wayland), no name is
  shown and nothing pretends to know.
- A new setting, `overlay.show_target` (default `true`), turns the name off, for
  example while sharing a screen.
- **Relaxes an existing privacy rule.** `transcript-delivery` currently says that
  messages to the overlay carry "no window identity". With this change they carry
  the target application's display name — never a window id, process id or title —
  while `overlay.show_target` is on. Logs are unchanged.

The desktop app planned in `add-desktop-app` shows the same name on its listening
and words-not-typed screens. The canvas linked from that change shows both.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `recording-overlay`: the overlay names where the transcript will go, in a bounded
  label that never changes the overlay's placement, focus or input behavior.
- `transcript-delivery`: delivery signals may carry the target application's
  display name; the toggle response reports the target; the display name is
  derived from the application identity Murmly records, never from window titles.

## Impact

- **Focus observation** (`focus.py`, `win_focus.py`): resolve a readable
  application name from the recorded identity on X11 and Windows; Wayland
  continues to report none.
- **Daemon**: observes focus while listening, only when the overlay is shown and the
  setting is on, and sends a name only when it changes. It adds the name to the
  overlay messages and to the toggle response.
- **Overlay renderers** (X11, Wayland layer-shell, Qt): draw the name in a label
  below the indicator.
- **Configuration**: the new `overlay.show_target` key in `config.py`,
  `config.example.toml`, `murmly doctor`, and the manual's settings page.
- **Documentation**: `manual/where-your-words-go.md` and `manual/using-murmly.md`.
