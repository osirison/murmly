# Design

## Context

Murmly today is a daemon plus a command-line client. The daemon owns all state and
answers requests on a command socket that only the owning account can reach
(`command-interface`). The overlay is drawn by a renderer per platform: native X11
and Wayland layer-shell renderers on Linux, and a Qt renderer on Windows that comes
with the `overlay` extra (`PySide6`, Windows only today). `murmly doctor` already
gathers every diagnostic section into one structured report. Configuration is read
with `tomllib` and never written by Murmly; the documented way to change it is to
edit `config.toml` and restart `murmly.service` (or the Windows scheduled task) by
hand. The installer already knows how to install, restart and remove the service on
both platforms.

Speaker recognition (`add-speaker-recognition`, which this change builds on) adds a
`[speakers]` table, `murmly enrol` — which records its sample in the command's own
process and refuses while the daemon is busy — `murmly speakers list` and
`remove`, voiceprints in `<data dir>/voiceprints.json`, and a
`speaker_recognition` section in `murmly doctor`. When a mode cannot run it fails
open: everything is typed, one warning is logged, and only `murmly doctor` says
why. Its spec forbids speaker names in delivery signals and logs, but exempts the
toggle response, which carries the labelled transcript.

The visual design is on the canvas linked from proposal.md. It uses the Proton Sync
design system — Instrument Sans for anything a person reads, IBM Plex Mono for
anything the machine owns (keys, paths, config names, raw `doctor` values), and
colour that carries meaning only:

| Colour | Meaning in Murmly |
| --- | --- |
| warm (orange) | your voice going in: listening |
| cool (cyan → blue) | words coming back to you: writing, speaking |
| crimson outline, never filled | something needs you |
| amber | a note where nothing is at risk |
| none | settled: nothing to report |

## Goals / Non-Goals

**Goals:**

- One codebase for the app on Linux and Windows, using a toolkit Murmly already
  depends on.
- The app is a client of the daemon, never part of it, so a broken or missing GUI
  cannot affect dictation.
- No new way for transcript text to leave the daemon except the explicit request for
  the last words, answered only to the owning account.

**Non-Goals:**

- macOS. `platform-support` does not list it; the app adds nothing that would
  change that.
- A transcript history. Only the most recent transcript is held, and only in
  memory.
- Replacing the command line. Every task the app performs stays possible from the
  terminal.
- Changing what the overlay looks like, beyond the delivered confirmation and the
  not-your-voice presentation.
- Changing how speaker recognition decides who spoke. The app only configures it,
  enrols voices and reports its state.

## Decisions

### Qt for Python (PySide6) with Qt Quick, in a `gui` extra that every sync carries

PySide6 is already a Murmly dependency for the Windows overlay. It runs on Linux and
Windows from the same code, has a system tray (`QSystemTrayIcon`), follows the
system's colour scheme (`QStyleHints.colorScheme`) and reduced-motion setting, and
draws the design's radii, type and colour exactly through Qt Quick.

Alternatives considered:

- **Tauri or Electron**: would reuse the canvas markup almost directly, but adds a
  Rust or Node toolchain and a second runtime to a Python project.
- **pywebview**: reuses HTML too, but leaves the tray and key recording to separate
  libraries per platform.
- **GTK**: native on Linux, weak on Windows.
- **Tk**: cannot meet the design without drawing most controls by hand.

The first draft made the extra opt-in because the PySide6 wheel is large. That is
reversed: the person asked for the app to be part of the install, and an app that
needs a manual `uv sync --extra gui` is one nobody opens. Worse, a plain
`uv sync --extra gui` on a machine with the GPU build of ONNX Runtime puts the CPU
build back. So `murmly sync`, which already names every extra it found and reapplies
the GPU swap afterwards, now always names `gui` as well.

It stays an extra, not a default dependency, so CI, which installs no extras, does
not download the wheel for tests that skip without it. It is added unconditionally
rather than kept "if already present": an existing environment without PySide6 must
get it on the next sync. Speech output's rule, where a missing synthesizer in an
existing environment means a deliberate opt-out, is not reused: nothing ever offered
a choice, so reading absence as refusal would leave every existing install without
the app. There is no flag to leave it out.

Both extras name `PySide6-Essentials`, not the `PySide6` meta package. The app and
the overlay import only QtCore, QtGui, QtWidgets, QtSvg, QtQml and QtQuick with its
Controls, Layouts, Shapes and Window modules, all of which ship in Essentials, and
the meta package adds Addons, WebEngine and Pdf that nothing here imports. The whole
Qt test set for the app and the overlay runs, and passes, in an environment built
from the lock with Essentials only. Cost, measured for Linux x86_64 and PySide6
6.12.0: about 85 MB to download (the Essentials wheel is 84 MB, shiboken6 0.3 MB) and
about 245 MB installed. The meta package would have been about 264 MB to download.

The toolkit has wheels only for glibc 2.34 or newer on x86_64 and 2.39 or newer on
aarch64, and `uv sync` fails outright on an older system, taking the whole install
with it. So `murmly sync` reads the oldest glibc the lock has an Essentials wheel
for on this architecture, compares it with the installed glibc, and below it leaves
`gui` out and says so, rather than carrying a floor of its own that dependency
updates would make wrong. Where the lock or the glibc cannot be read it keeps `gui`.

The `overlay` extra keeps its current meaning. On Windows, `murmly sync` could not
see whether `overlay` had been installed and so dropped it; naming `gui` keeps
PySide6 there, which is all the overlay needs.

### The app is a separate process that talks to the daemon over the command socket

`murmly app` starts a Qt process that connects to the existing command socket like
`murmly status` does. The daemon never imports Qt. This keeps the daemon's start-up
light, follows `recording-overlay`'s visual failure isolation, and means the app can
be closed, crash, or be absent without affecting capture or delivery.

Rejected: running the window inside the daemon process. A Qt crash or a stuck event
loop would take dictation down with it.

### State by polling `status`, not a new event stream

While its window or tray panel is visible, the app sends `status` at most every
500 ms, and stops when both are hidden. That meets "within one second" using the
existing command, with no protocol change. A streaming subscription can replace it
later without changing the spec.

### Last words: one in-memory slot in the daemon

The daemon keeps one slot: the latest transcript, when it was produced, and its
delivery outcome. It is overwritten by the next transcript, cleared on request, and
never logged or persisted. Two new socket requests read and clear it. Because they
travel over the command socket, they inherit its owner-only permission. The slot
holds the transcript a toggle response already returns; it creates no new copy of
what was said beyond the clipboard Murmly already writes.

### Configuration: `tomlkit` round-trip, validated, atomic, mtime-checked

The app edits `config.toml` with `tomlkit`, which keeps comments and key order.
Before writing, it validates the value with the same rules `murmly.config` applies
when reading, so the app cannot write a value the daemon would ignore. It writes to
a temporary file in the same directory and replaces the original. It records the
file's modification time when it reads it and re-reads if that changed. A file that
`tomllib` cannot parse is never written.

### Applying settings: restart through the installer once idle

The app asks the daemon whether it is idle (nothing captured, transcribed or
spoken), waiting while it is not, then restarts the service through the installer's
existing service manager: the systemd user unit on Linux, the scheduled task on
Windows. This matches what the manual tells people to do today. A live reload
inside the daemon would avoid the restart, but every setting would need its own
reload path; that can come later.

### Key recording: Qt key events on a focused field

The key field records only while it has focus and the person has asked to record.
It builds the combination from Qt's key and modifier events, checks it with the same
validation `murmly install` uses, and binds it by running the same install path. No
system-wide keyboard hook is installed.

### Removal reuses `murmly uninstall`

The app runs the same uninstall, and `--purge` only when the person has chosen it
explicitly. The app shortcut is recorded as something Murmly installed, so the
existing "uninstall removes everything Murmly installed" requirement covers it.

`murmly uninstall` had no purge: only `setup.sh` deleted settings and models, along
with the Python environment. It gains `--purge`, which deletes the settings folder and
the data folder (models and voices) and leaves the environment alone, because the app
runs from it and Windows cannot delete files in use. Both the command and the app call
one function, `remove_murmly`, so the removal and the purge cannot differ. A purge
leaves the announcement hook's script, because the assistants' own settings point at
it until `setup.sh` unregisters it.

The shortcut is a fixed-name `.desktop` file written beside the hotkey launchers when
PySide6 is importable, and the installer lists what is installed from the same objects
it removes them through, so the confirmation names exactly what will go. There is no
separate manifest. Linux only: the installer has no Windows or macOS shortcut code, and
adding some was left out of this change.

### Delivered confirmation in every renderer

The overlay protocol gains a "delivered" state, sent when delivery succeeds and
followed by the existing hide after at most 1.5 s. Each renderer (X11, Wayland, Qt)
draws a check and the word "Typed"; under reduced motion it appears without
animation. Continuous sessions never send it for a segment, as the spec requires.

### Enrolment runs in the app's process, through the enrolment command's code

The speaker spec has enrolment record "in the command's own process", not the
daemon's. The app calls the same enrolment function `murmly enrol` uses, from its
own process, so the sample length, minimum speech, name rules, busy refusal and
storage are the same code rather than a second copy of the rules. The app adds
only the screens around it: the passage to read, a live count of speech heard, and
the consent confirmation for anyone who is not the owner. The busy check is the
same `status` request the command makes.

### Voices-not-checked comes from the doctor report, not from `status`

Whether speaker recognition can run is reported in `murmly doctor`'s
`speaker_recognition` section (`available`, `detail`). The app reads that section
when Home or Voices opens, after any change to voices or the `[speakers]` table,
and after a restart. It does not poll it every 500 ms: availability changes only
when the person changes something. The `detail` string is shown verbatim, following
the design system's rule never to paraphrase a daemon error.

### Last words keep the labelled parts

When a transcript is labelled, the daemon's last-words slot keeps its parts with
their labels as well as the delivered line, so the app can lay out who said what
without parsing the line (a colon in someone's words would make parsing
ambiguous). The slot is answered over the owner-only command socket, under the same
exemption the speaker spec gives the toggle response.

### Not-your-voice in the overlay

When mine-only leaves a recording with nothing to type, the daemon sends the
overlay a "not-your-voice" state carrying no name and no text, then hides it after
at most 1.5 s, as with the delivered confirmation. Continuous sessions never send
it for a segment. The outcome is still not a refused delivery, as the speaker spec
requires: nothing is pasted and no failure is signalled.

## Risks / Trade-offs

- [The app could take focus and turn a good delivery into a refused one] → The app
  never calls raise or activate in response to daemon state; only the person
  opening it brings it forward. A test asserts that state changes make no focus
  calls.
- [PySide6 adds a download: about 85 MB, 245 MB on disk] → Accepted, because the app
  is meant to be part of the install. Essentials holds every Qt module the app and
  overlay use, and the larger meta package is not installed.
- [A glibc older than the toolkit's wheels would fail the whole sync] → The sync
  leaves the app out below the floor the lock records, and says so.
- [GNOME without an AppIndicator extension has no tray] → The app runs from its
  window, and Health says that no tray was found.
- [A hand edit to `config.toml` races with an app edit] → The mtime check makes the
  app re-read instead of overwriting.
- [Restarting the service drops the model from memory, so the next recording
  reloads it] → The model reload already starts when capture begins (about 0.8 s on
  the measured machine), so the cost falls while the person is speaking.
- [The last words could linger on a shared machine] → They live only in daemon
  memory, readable only by the owning account, and can be cleared from the app.
- [Enrolment needs the microphone while the daemon might want it] → The app checks
  `status` before recording and refuses while the daemon is busy, as the command
  does; a press of the dictation key during enrolment is answered by the daemon as
  usual, and the app discards the sample.
- [Consent is only as good as the person confirming it] → The app cannot verify
  agreement; it makes the question explicit, says the voiceprint can be removed, and
  keeps removal one click away.
- [`add-speaker-recognition` changes before it is archived] → The Voices
  requirements reference its behaviour, not its code; re-check them against its
  final spec when archiving it.

## Migration Plan

Nothing to migrate. The command line, the configuration file format and the
service are unchanged. An existing installation gets the app on its next
`./setup.sh upgrade`: the sync adds PySide6, and the `murmly install` that upgrade
runs afterwards, in a new process, writes the menu entry (and rewrites an older
entry that has no icon). On Windows `bootstrap.ps1 install` and `upgrade` now run
`murmly sync` first, and every other `bootstrap.ps1` command runs `uv run --no-sync`,
so no on-demand sync puts the CPU ONNX Runtime back after the GPU swap.
Removing the app means removing murmly. Voices enrolled with `murmly enrol` appear in the
app as they are, and voices enrolled in the app are listed by
`murmly speakers list`.

## Open Questions

- Whether installing with `gui` should also start the app at sign-in, or only add a
  shortcut. Either fits the spec; it can be decided when the installer work starts.
