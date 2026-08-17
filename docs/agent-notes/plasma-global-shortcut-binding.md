---
title: Bind Plasma global shortcuts via X-KDE-Shortcuts, never via D-Bus setters or kglobalshortcutsrc
description: Preconditions and traps when registering a KDE Plasma 6 global shortcut for a .desktop launcher from a CLI installer
trigger: busctl kglobalaccel, kbuildsycoca6, kwriteconfig6 kglobalshortcutsrc, setForeignShortcutKeys, X-KDE-Shortcuts

depends_on: contrib/murmly.service, README.md
recorded: 2026-08-17
verified_on: Plasma 6.7.4, Fedora 44, XDG_SESSION_TYPE=x11
---

## Symptom

You want to bind a hotkey to a command from an installer, taking effect without a
logout. Three approaches look correct and are not:

1. Writing `~/.config/kglobalshortcutsrc` — invisible to the running daemon, and
   later actively deleted.
2. Calling `setForeignShortcutKeys` / `setShortcutKeys` over D-Bus — registers a
   shortcut that reads back correctly everywhere and never fires.
3. Shelling out to `busctl` for any method taking a `(ai)` QKeySequence —
   **crashes the user's shortcut daemon**.

## Do not shell out to busctl for QKeySequence methods

`kglobalacceld` 6.7.4 demarshals a QKeySequence with four unconditional reads
(`argument >> s1 >> s2 >> s3 >> s4`). An `(ai)` struct with any element count
other than exactly 4 reads past the end and SIGABRTs the daemon:

```bash
# CRASHES kglobalacceld — do not run
busctl --user call org.kde.kglobalaccel /kglobalaccel \
  org.kde.KGlobalAccel globalShortcutAvailable "(ai)s" 1 268435542 ""
```

Affected: `globalShortcutAvailable`, `globalShortcutsByKey`, `actionList`,
`setForeignShortcutKeys`, `setShortcutKeys`.

Safe scalar-taking equivalents, use these instead:

```bash
# is  -> bool ; conflict check. Works for a component that does not exist yet.
busctl --user call org.kde.kglobalaccel /kglobalaccel \
  org.kde.KGlobalAccel isGlobalShortcutAvailable is 268435544 ""

# i -> rows ; names the conflicting owner, and detects double-binds
busctl --user call org.kde.kglobalaccel /kglobalaccel \
  org.kde.KGlobalAccel getGlobalShortcutsByKey i 268435544
```

## The working mechanism

Write one file to `~/.local/share/applications/`. `X-KDE-Shortcuts` is
**mandatory** — `detectAppsWithShortcuts` filters out any file without it.
`X-KDE-GlobalAccel-CommandShortcut=true` is cosmetic (read only by the System
Settings KCM, to label the row "Command"); it does not bind anything.

```ini
[Desktop Entry]
Type=Application
Name=murmly
Exec=/abs/path/.venv/bin/murmly toggle
NoDisplay=true
StartupNotify=false
X-KDE-GlobalAccel-CommandShortcut=true
X-KDE-Shortcuts=Meta+X
```

`kglobalacceld` holds an inotify watch on the ksycoca database, so the binding
takes effect in the running daemon. Measured: the component appeared ~200ms after
the write, **without** running `kbuildsycoca6` — ksycoca rebuilds in-process when
the applications directory changes. Run `kbuildsycoca6` anyway as belt-and-braces,
then poll rather than assuming:

```bash
busctl --user call org.kde.kglobalaccel /kglobalaccel \
  org.kde.KGlobalAccel getComponent s "net.local.murmly.desktop"
```

Double `%` in the `Exec` value, matching what the KCM does.

## Never write kglobalshortcutsrc

`loadSettings()` early-returns if components already exist, and the installed
`libKGlobalAccelD.so` contains no `KConfigWatcher`/`QFileSystemWatcher` — the file
is read once at daemon start. `KServiceActionComponent::writeSettings()` then does
`config.deleteGroup()` and rewrites from memory on a 500ms timer, so an external
edit is destroyed, not merely ignored. `kwriteconfig6 --notify` cannot help;
there is no watcher to notify.

A pure `X-KDE-Shortcuts` install writes nothing to that file at all, because
active == default takes the `revertToDefault()` branch. **Uninstall must not
assume a `[services]` group exists.**

## Rebinding is not a file rewrite

`detectAppsWithShortcuts` skips components it already knows, so editing
`X-KDE-Shortcuts` in place does nothing while the daemon runs — verified: the file
said `Meta+Y` while the live session kept firing `Meta+X`, and it would look
correct again only after the next login. Rebind must be:

```
delete .desktop -> kbuildsycoca6 -> poll until getComponent errors -> write new -> kbuildsycoca6
```

Measured ~800ms in each direction. Uninstall is the first half alone.

## Verify, because nothing else will

Plasma does **not** refuse a clobber. `registerKey()` refcounts an already-grabbed
combination and returns true; `activeShortcutByKey()` returns the lowest serial,
i.e. the incumbent. Two `.desktop` files declaring the same key both register with
no error anywhere, and only the first one ever fires. After binding:

1. Read `shortcutKeys` back and require the first int to equal your computed
   keycode. This catches a wrong keycode table — `Qt::Key_End` is `0x01000011`;
   `0x01000015` is `Qt::Key_Down`. Formula is `Qt::Key | modifier bits`
   (Shift `0x02000000`, Ctrl `0x04000000`, Alt `0x08000000`, Meta `0x10000000`).
2. Require `getGlobalShortcutsByKey` to return **exactly one** row, owned by you.
   More than one is a silent double-bind.

Neither check proves the X server granted the grab — only a real keypress does.

## Fallback

If the poll times out, `systemctl --user restart plasma-kglobalaccel.service`
re-runs `loadSettings()`, which ends in `detectAppsWithShortcuts()` and reads the
same file. It is a ~4s global-hotkey outage, so prompt first. Otherwise the
binding is already persisted in the `.desktop` and will be live at next login.

## Scope

Verified end-to-end on X11 only. The grab goes through `KGlobalAccelInterface`
from `org.kde.kglobalacceld.platforms`, and `activeShortcutByKey` has an explicit
`if (KWindowSystem::isPlatformX11())` divergence, so Wayland needs its own test.
