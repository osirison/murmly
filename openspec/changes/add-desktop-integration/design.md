## Context

See `proposal.md` — Why. The constraints below were established empirically on
Plasma 6.7.4 / Fedora 44 / X11 and are recorded in
`docs/agent-notes/plasma-global-shortcut-binding.md`.

Two facts about the target platform shape everything here:

1. **The daemon's dependencies are session-scoped, not boot-scoped.** Clipboard
   tools, paste injection, the focus observer, and the overlay all need
   `DISPLAY`/`WAYLAND_DISPLAY` and `XAUTHORITY`. On this machine `XAUTHORITY`
   points at a per-session random path (`/tmp/xauth_*`), so a process that
   outlives its session holds a stale handle.
2. **Plasma does not arbitrate hotkey conflicts.** `registerKey()` refcounts an
   already-grabbed combination and returns success; `activeShortcutByKey()`
   returns the lowest serial, which is the incumbent. Verified: two launcher
   files declaring the same key both registered with no error on any channel, and
   only the first ever fired. A colliding install is indistinguishable from a
   working one except by pressing the key.

The existing overlay already scopes itself to KDE Plasma, and `murmly doctor`
already establishes the pattern of reporting capability rather than assuming it.

## Goals / Non-Goals

**Goals:**

- One command takes a working checkout to a working hotkey, and one command
  reverses it.
- The installer never leaves the desktop in a state that looks installed but
  does not work.
- Murmly writes only files it owns.
- The installer adds no runtime dependency to a two-dependency project.

**Non-Goals:**

- Supporting desktop environments other than KDE Plasma for hotkey registration.
  The service half is desktop-agnostic; the hotkey half is not.
- Managing the model's memory residency. An autostarted daemon holds the model
  from first toggle until logout; that is documented, not solved here.
- Providing a graphical configuration surface. The desktop's own settings remain
  the place to change a hotkey after installation.

## Decisions

### Register the hotkey declaratively; make no mutating D-Bus call

Murmly writes one launcher file to `~/.local/share/applications/` carrying
`X-KDE-Shortcuts=<key>`, then lets Plasma's own service discovery bind it.
`kglobalacceld` holds an inotify watch on the ksycoca database, so a rebuild
reaches the running daemon and the grab happens in-process:

```
write .desktop → ksycoca rebuild → KSycoca::databaseChanged
  → refreshServices → detectAppsWithShortcuts → createServiceActionComponent
  → loadSettings → registerShortcut("_launch") → setIsPresent(true)
  → setActive() → registerKey()          ← the grab, no logout
```

Measured: the component appeared ~200ms after the write, and a synthetic keypress
launched the target with `ppid` equal to the systemd user manager, proving the
grab was real and not merely registry state.

**Alternative rejected — the D-Bus setter path.** `doRegister(actionId)` followed
by `setForeignShortcutKeys` looks like the obvious API and **registers a shortcut
that never fires**. `getOrCreateComponent()` already creates and grabs the real
`_launch` shortcut; `addAction()` then inserts a second `GlobalShortcut` named
`_launch` into a `QHash`, overwriting the grabbed one. `findAction` returns the
shadow with `_isRegistered == false`, and `setForeignShortcutKeys` passes only
`NoAutoloading` (4), never `SetPresent` (2), so `setKeys` skips its `if (active)`
re-grab. The result reads back correctly over D-Bus and displays correctly in
System Settings while the key is never grabbed. This is the single most expensive
trap in the area and is why the declarative path is preferred even though it is
less obvious.

**Alternative rejected — writing `~/.config/kglobalshortcutsrc`.**
`loadSettings()` early-returns when components exist and the installed library
contains no config watcher, so the file is read once at daemon start; an external
edit is invisible until logout. Worse,
`KServiceActionComponent::writeSettings()` calls `config.deleteGroup()` then
rewrites from memory on a 500ms timer, so the edit is actively destroyed. This
also motivates the spec requirement that Murmly not modify configuration it does
not own.

**Consequence.** Because `X-KDE-Shortcuts` sets the *default* and the active
shortcut then equals it, `writeSettings()` takes its `revertToDefault()` branch
and writes nothing. Verified: the user's shortcut configuration was byte-identical
(sha256) across a full install/rebind/uninstall cycle. Uninstall must therefore
**not** assume a configuration entry exists to remove.

### Treat `kbuildsycoca6` as belt-and-braces, and poll for the outcome

The component appeared without running `kbuildsycoca6` at all — ksycoca rebuilds
in-process when the applications directory changes. Murmly runs it anyway because
it costs little and removes a timing assumption, but correctness comes from
polling for the component rather than from assuming either path succeeded. The
bounded wait, and the failure report when it expires, are what the spec requires.

### Anchor the service on `graphical-session.target`, resident

```
[Unit] PartOf=graphical-session.target  After=graphical-session.target
[Install] WantedBy=graphical-session.target
```

`PartOf` gives stop-on-logout, `After` gives ordering, `WantedBy` gives
activation; the shipped template had only the ordering, which is why it never
started correctly. Note `graphical-session.target` sets `RefuseManualStart=yes`,
so installation must `enable` the unit and then start the service directly rather
than starting through the target.

**Alternative deferred — socket activation.** A `murmly.socket` unit would start
the daemon on first toggle and structurally remove the dead-daemon case, but
requires the daemon to accept an inherited listening descriptor instead of binding
its own. The recovery path below covers the same failure at far lower cost. Worth
revisiting if model residency becomes a problem.

### Recover in `toggle` by starting the service

`send_command` currently raises `FileNotFoundError` out of `socket.connect` when
the daemon is down. From a hotkey that is invisible. `toggle` instead starts the
installed service, waits a bounded time for the socket, and retries once. This
also covers the early-login window before the service is up.

### Hand-roll the key parser, then cross-validate against the desktop

The encoding is `Qt::Key | modifier bits` (Shift `0x02000000`, Ctrl `0x04000000`,
Alt `0x08000000`, Meta `0x10000000`), verified against 12 live shortcuts with no
mismatches. No Qt Python binding exists on the system interpreter or in the
project environment, and adding one to a two-dependency project is
disproportionate — the existing system-Python helper uses GTK, not Qt.

The table is the weak point: during investigation `Qt::Key_End` was reached for as
`0x01000015`, which is actually `Qt::Key_Down`. The mitigation is free and is
promoted to a spec requirement: after binding, read the registered key back and
require it to equal the computed integer. That check compares Murmly's table
against KDE's own parser, so a wrong constant fails loudly at install time instead
of producing a hotkey the user did not ask for.

Parsing is strict rather than Qt-tolerant, because `QKeySequence::fromString`
silently yields `Key_unknown` on garbage. `Super`/`Win` are accepted as aliases
and normalized to `Meta`; a key name containing a comma is rejected, because comma
separates alternatives in `X-KDE-Shortcuts`.

### Rebind by removing and re-adding, never by rewriting in place

`detectAppsWithShortcuts` skips components it already knows, so editing
`X-KDE-Shortcuts` on a registered component does nothing in the running session.
Verified: after rewriting the key and rebuilding, the file said one key while the
session kept firing the other — a state that looks correct on disk and at next
login, and is wrong right now. The sequence is delete → rebuild → poll until the
component is gone → write → rebuild → poll until present, measured at ~800ms in
each direction. Uninstall is the first half alone.

### Conflict detection is Murmly's responsibility alone

Because the desktop accepts a colliding registration silently, the pre-flight
check and the post-install sole-ownership assertion are the only things standing
between the user and a hotkey that reports healthy and never fires. Both use
read-only D-Bus methods taking plain scalars.

**Operational constraint.** Murmly must never send a `QKeySequence` struct over
D-Bus. `kglobalacceld` demarshals one with four unconditional reads, so any array
whose length is not exactly four reads past the end and aborts the daemon — this
crashed the daemon twice during investigation. Under the chosen design Murmly
calls no such method, which is an additional argument for it.

## Risks / Trade-offs

- **Wayland is unverified** → All evidence is X11. Discovery, parsing, and
  persistence are platform-independent, but the grab goes through a different
  platform plugin and `activeShortcutByKey` branches on `isPlatformX11()`. The
  spec's "unverified session type" scenario makes Murmly report this rather than
  claim it; verification decides the outcome either way.
- **Registration is observable; key delivery is not** → `registerKey()` failure is
  logged only at debug level and `setActive()` sets its flag regardless, so
  reading the binding back proves registry state, not that the X server granted
  the grab. Mitigation: the installer says so and asks the user to press the key
  once. It must not claim delivery.
- **A user override in the desktop's own settings takes precedence** → A
  `[services]` entry for Murmly's launcher overrides `X-KDE-Shortcuts`, which is
  arguably correct. Mitigation: detect it read-only, report it as an override, and
  do not overwrite it.
- **Absolute paths make the install location-dependent** → Moving the checkout
  breaks the service and the hotkey silently. Mitigation: install is re-runnable
  and repairs the path; `doctor` reports the recorded path so the breakage is
  visible.
- **The resident daemon holds the model for the session** → With
  `lazy_load_model` enabled the model loads on first toggle and stays, roughly
  1.6 GB, in VRAM under CUDA. Mitigation: document it in the README. Socket
  activation is the structural fix if it becomes a problem.
- **`kbuildsycoca6` rebuilds a shared desktop cache** → It is idempotent and part
  of normal desktop operation, but it is a system-wide side effect of an install
  command. Accepted; the alternative is a longer and less predictable wait.

## Migration Plan

`contrib/murmly.service` is replaced by generated output. Anyone who installed the
old template by hand has a unit at the same path with a non-functional
`ExecStart`; running `murmly install` overwrites it with a correct one, so no
manual migration step is needed. Rollback is `murmly uninstall`, which returns the
session to its pre-install state — the verified byte-identical shortcut
configuration means there is nothing to restore by hand.
