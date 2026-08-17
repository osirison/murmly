## Why

Murmly works once it is running, but there is no supported path from a cloned
repository to a working dictation hotkey. The shipped `contrib/murmly.service`
points at `%h/.local/bin/murmly`, which nothing installs; it activates on
`default.target`, before the graphical session environment the daemon depends on
exists; and it never stops on logout, so it outlives the `XAUTHORITY` file it was
started with. There is no shortcut story at all beyond a README line asking the
user to type an absolute path into System Settings by hand.

The result is that every user must reconstruct, by hand and without guidance, the
one arrangement in which Murmly is usable: a daemon running inside the graphical
session, and a hotkey bound to the absolute path of a virtual-environment console
script.

## What Changes

- Add `murmly install`: writes a systemd user unit and a KDE Plasma global
  shortcut, then starts the service. Re-running it is safe and repairs a binding
  broken by a moved repository or a rebuilt virtual environment.
- Add `murmly uninstall`: removes the unit, the shortcut, and the launcher entry.
- Replace `contrib/murmly.service` with a generated unit anchored on
  `graphical-session.target`, so the daemon starts with the Plasma session and
  stops at logout rather than attempting to start at boot.
- `murmly toggle` recovers when the daemon is not listening: it starts the user
  service, waits for the socket, and retries once, instead of raising an
  unhandled `FileNotFoundError` into a hotkey's invisible stderr.
- `murmly doctor` reports installation state alongside its existing delivery and
  overlay sections.
- Restructure `README.md` around install → bind a key → speak, demoting the
  current `uv run murmly ...` material to a development section.

Shortcut registration is declarative: Murmly writes a `.desktop` launcher
carrying `X-KDE-Shortcuts` and lets Plasma's own service discovery bind the key.
Murmly makes **no mutating D-Bus call** and never writes
`~/.config/kglobalshortcutsrc`. It uses D-Bus read-only, to refuse a conflicting
key and to verify the binding afterwards. The rejected alternatives, and the
evidence against them, are recorded in `design.md`.

Scope is KDE Plasma, matching the existing scope of the recording overlay. The
binding mechanism is verified end-to-end on X11 only; Wayland is untested and is
declared as such rather than claimed.

## Capabilities

### New Capabilities

- `desktop-integration`: how Murmly installs itself into a desktop session — the
  daemon's session lifecycle, global shortcut registration and removal, refusal
  to clobber a hotkey owned by another application, recovery when the hotkey is
  pressed while the daemon is not listening, and reporting of installation state.

### Modified Capabilities

None. Transcription, transcript delivery, and the recording overlay keep their
current requirements; this change only governs how Murmly comes to be running and
how it is reached.

## Impact

- **New code**: an installer module covering unit generation, launcher-file
  generation, hotkey parsing, conflict detection, and verification.
- **Modified code**: `cli.py` gains `install` and `uninstall` subcommands and a
  hotkey argument; `send_command` in `daemon.py` grows a recovery path;
  `_run_doctor` gains an installation section.
- **Removed**: `contrib/murmly.service` as a hand-edited template.
- **Docs**: `README.md` restructured. `docs/agent-notes/plasma-global-shortcut-binding.md`
  already records the operational constraints this change must respect.
- **Dependencies**: none added. The installer is stdlib-only and shells out to
  `systemctl`, `busctl`, and `kbuildsycoca6`, which ship with the target desktop.
- **External state written**: `~/.config/systemd/user/murmly.service` and
  `~/.local/share/applications/net.local.murmly.desktop`. Both are removed by
  `murmly uninstall`.
- **Platform risk**: the shortcut mechanism is proven on Plasma 6.7.4 / X11.
  Plasma Wayland shares the discovery and persistence code but dispatches grabs
  through a different platform plugin.
