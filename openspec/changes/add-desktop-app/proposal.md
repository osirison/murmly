# Proposal

## Why

Everything Murmly does today is reached from a terminal: installing it, choosing a
key, reading `murmly doctor`, editing `config.toml` and restarting the service by
hand. That keeps Murmly out of reach for anyone who does not live in a terminal,
and nothing on screen says what it is doing beyond the 156 × 48 overlay. A desktop
app gives each of those jobs a place to live.

Speaker recognition (`add-speaker-recognition`) sharpens this. Choosing a mode
means hand-editing `[speakers]`, enrolling means `murmly enrol` in a terminal, and
when a mode cannot run Murmly types every voice while nothing on screen says so: a
setup that does not work looks exactly like one that does, until someone else's
words land in a document.

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
- **Voices**: a Settings section for speaker recognition — the mode (every voice,
  only my voice, everyone labelled), which enrolled voice is the owner, the match
  threshold, and the enrolled voices, each removable, with removing all of them
  confirmed twice.
- **Enrolment in the app**, under the same rules as `murmly enrol`, with an
  explicit confirmation that anyone else being enrolled has agreed. First run
  offers it as an optional step.
- **Voices not checked**: while a speaker mode is set but cannot run, Home, the
  tray and Voices say that every voice is being typed, and why, in the
  diagnostics' own words.
- **Last words show who spoke** when everyone is labelled.
- **Not your voice**: when "only my voice" leaves out everything in a recording,
  the overlay says so briefly instead of simply disappearing.

## Capabilities

### New Capabilities

- `desktop-app`: the app window, tray panel and first-run setup — what each one
  shows, how settings are written and applied, how the last transcript is held, how
  diagnostics are presented, how voices are enrolled and managed, and how Murmly is
  removed from the app.

### Modified Capabilities

- `recording-overlay`: when transcription and delivery succeed, the overlay shows a
  bounded delivered confirmation before hiding, instead of hiding immediately; when
  "only my voice" leaves out everything in a recording, it shows a brief neutral
  presentation saying so.

## Impact

- **Order**: depends on `add-speaker-recognition`, whose `speaker-recognition`
  capability the Voices requirements build on. Archive that change first.
- **Packaging**: a new optional extra, `gui`, carrying PySide6 on Linux as well as
  Windows (today it is installed for the Windows overlay only), and a new
  `murmly app` command. Nothing in the default install changes.
- **Daemon**: holds the last transcript in memory, with its speaker parts when it
  was labelled, and answers a request for it over the command socket; reports
  whether it is idle enough to restart; tells the overlay when "only my voice" left
  nothing to type.
- **Configuration**: a new dependency, `tomlkit`, so the app can change one key
  without discarding the comments in `config.toml`.
- **Fonts**: Instrument Sans and IBM Plex Mono are bundled for offline use, both
  under the SIL Open Font License, with their licences in `licenses/`.
- **Installer**: an app shortcut (desktop entry on Linux, Start menu on Windows);
  uninstall removes it.
- **Overlay renderers**: every renderer (X11, Wayland, Qt) draws the new delivered
  confirmation and the "not your voice" presentation.
- **Documentation**: a manual page for the app, the overlay page updated for the
  confirmation, and `manual/typing-only-your-own-voice.md` given the app's route
  alongside the commands.
