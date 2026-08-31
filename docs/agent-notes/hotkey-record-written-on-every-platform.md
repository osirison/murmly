---
title: hotkeys.json is written on every platform, not only Windows and macOS
description: Installer._write_hotkey_record() runs unconditionally, so KDE and GNOME installs also gain a fourth on-disk file the pre-existing install docs never named
trigger: manual/install.md, "installing murmly writes exactly three files", hotkeys.json, default_hotkey_record_path

depends_on: src/murmly/installer.py, src/murmly/hotkey_record.py
recorded: 2026-09-01
---

## Symptom

`manual/install.md` claimed, before this pass, that installing murmly on Linux
writes exactly three files: the systemd unit and the two `.desktop` files that
carry KDE's hotkeys. That claim was true when it was written and is false now.

## Cause

Task 5.4 added a hotkey record (`hotkey_record.py`'s `HotkeyRecordStore`,
persisted at `default_config_path(env).parent / "hotkeys.json"`) so an
in-process backend — Windows' `RegisterHotKey`, macOS's `RegisterEventHotKey`
— has something to read the bound keys back from at daemon start, since
neither desktop holds that state itself the way KDE's `.desktop` file or
GNOME's dconf value does.

`Installer._write_hotkey_record()`'s own docstring says why it is not gated to
those two platforms: "Written unconditionally, on every platform: the record
costs nothing to keep where nothing reads it yet, and is exactly what an
in-process backend needs the day one exists." So a KDE or GNOME install now
also writes `~/.config/murmly/hotkeys.json` — a file the KDE and GNOME
backends never read, existing purely so the record is already correct by the
time an in-process backend exists to want it.

`Installer.uninstall()` clears it too (`self._record_store.remove()`), so the
existing "uninstall removes every one of these files" claim stayed true; only
the count of files was wrong.

## Fix

`manual/install.md`'s "What installing writes" section now lists four paths on
Linux instead of three, and gives Windows' own two-item table (`MurmlyDaemon`
in Task Scheduler, plus `%APPDATA%\murmly\hotkeys.json`).

## Why it was not obvious

The task that added this file (section 5, hotkey parsing across platforms)
landed in an earlier phase than the documentation task that names every file
an install writes (section 19), and nothing forced the second to re-derive the
first's side effects — the file it changed, `install.md`, was not in section
5's own task list at all. Any future change that touches what `Installer`
writes should grep `manual/install.md`'s "What installing writes" table before
calling itself done, the same way this note exists so the next one does not
have to re-discover the gap by reading `installer.py` end to end again.
