# Proposal

## Why

Everything Murmly does today is reached from a terminal: installing it, choosing a
key, reading `murmly doctor`, editing `config.toml` and restarting the service by
hand. That keeps Murmly out of reach for anyone who does not live in a terminal,
and nothing on screen says what it is doing beyond the 156 × 48 overlay. A desktop
app gives each of those jobs a place to live.

The screens are already designed, on the canvas at
<https://claude.ai/artifact/1qtBy8SYpn8iesseopZbYp>, using the Proton Sync design
system. This change records them, and the new behavior they rely on, so they can be
built later.

## What Changes

- A desktop app on Linux and Windows, `murmly app`, with three sections: Home,
  Settings and Health. It is optional: dictation works the same with or without it.
- **Home** shows the current state (ready, listening, writing, needs you, service
  not running), the keys that are bound, the last words, and a summary of how
  Murmly is set up.
- **Last words**: the most recent transcript is held in the daemon's memory, and
  only there, so the app can show it and copy it again. It is never written to disk
  or to a log, and it is gone when the daemon exits.
- **Words not typed**: when a delivery is refused, the app says that the words are
  on the clipboard, shows the daemon's own detail string word for word, and offers
  to copy them again.
- **Settings** edits `config.toml` in place, keeping every comment, and the change
  takes effect without a manual restart: Murmly restarts its service once nothing is
  being recorded, transcribed or spoken.
- **Health** shows every section of `murmly doctor` as a plain sentence beside the
  raw key it came from, and offers the full report for copying.
- **First run**: choose the dictation key by pressing it, choose accuracy, and try
  dictation in a text box before leaving.
- **Tray panel** with the state, the last words and quick switches, where the
  platform has a tray.
- **Typed confirmation**: after a successful delivery the overlay shows a short
  "Typed" confirmation before it hides, rather than hiding at once.
- **Remove murmly** from inside the app, by the same removal `murmly uninstall`
  performs. Deleting settings and downloaded models is a separate, explicit choice.

## Capabilities

### New Capabilities

- `desktop-app`: the app window, tray panel and first-run setup — what each one
  shows, how settings are written and applied, how the last transcript is held, how
  diagnostics are presented, and how Murmly is removed from the app.

### Modified Capabilities

- `recording-overlay`: when transcription and delivery succeed, the overlay shows a
  bounded delivered confirmation before hiding, instead of hiding immediately.

## Impact

- **Packaging**: a new optional extra, `gui`, carrying PySide6 on Linux as well as
  Windows (today it is installed for the Windows overlay only), and a new
  `murmly app` command. Nothing in the default install changes.
- **Daemon**: holds the last transcript in memory and answers a request for it over
  the command socket; reports whether it is idle enough to restart.
- **Configuration**: a new dependency, `tomlkit`, so the app can change one key
  without discarding the comments in `config.toml`.
- **Fonts**: Instrument Sans and IBM Plex Mono are bundled for offline use, both
  under the SIL Open Font License, with their licences in `licenses/`.
- **Installer**: an app shortcut (desktop entry on Linux, Start menu on Windows);
  uninstall removes it.
- **Overlay renderers**: every renderer (X11, Wayland, Qt) draws the new delivered
  confirmation.
- **Documentation**: a manual page for the app, and the overlay page updated for the
  confirmation.
