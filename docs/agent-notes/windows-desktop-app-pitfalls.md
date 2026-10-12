---
title: Five things that turn the Windows CI job red or break the desktop app on Windows
description: Native path strings in tests, console windows from spawned processes, child output encoding, file-change detection, and files held open by another process; each was a red Windows job in October 2026
trigger: "AssertionError" on a path string, "PermissionError" on config.toml or os.replace, subprocess.run, subprocess.Popen, schtasks, murmly doctor, PYTHONIOENCODING, U+FFFD, st_mtime, tests/qt_app.py, Windows job only
depends_on: src/murmly/app/health.py, src/murmly/installer.py, src/murmly/settings_writer.py, tests/qt_app.py, .github/workflows/tests.yml
recorded: 2026-10-12
---

Each item was a red Windows job (or a behaviour only Windows shows) while
building the desktop app on 2026-10-11 and 2026-10-12. The Linux and macOS jobs
were green every time, so a green run elsewhere proves nothing.

## 1. Paths shown to people are native; tests must not hard-code them (#82, #80)

The app shows `str(path)`, which is `\x\config.toml` on Windows. A test that
expects the literal `/x/config.toml` fails only there (#82, the settings footer).

- Build the expected string from the same `Path` object: `str(path)`.
- Never assume XDG locations in a test. On Windows the config lives under
  `%APPDATA%` and on macOS under `Application Support` (#80). Take the location
  from the code under test, or point it at a temporary directory explicitly.

## 2. A spawned process needs `CREATE_NO_WINDOW` (#84, health runner in #83)

The desktop app has no console. On Windows, any console program it starts
(`schtasks`, `murmly doctor`, installer commands) opens a console window, and that
window can take focus from the window the person was dictating into. That breaks
the rule that the app never takes focus.

Pass `creationflags=subprocess.CREATE_NO_WINDOW` on Windows. The constant exists
only on Windows, so guard it or use `getattr(subprocess, "CREATE_NO_WINDOW",
0x08000000)`, as `installer.py` does.

## 3. A child Python read as UTF-8 needs `PYTHONIOENCODING=utf-8` (#83)

On Windows a Python child writing to a pipe encodes with the ANSI code page
(cp1252), not UTF-8. Reading that output as UTF-8 turns non-ASCII characters into
U+FFFD. Set `PYTHONIOENCODING=utf-8` in the child's environment (copy
`os.environ`, add the key). `app/health.py` does this.

## 4. Detect file changes by content, not by mtime and size (#85)

On Windows a same-size edit made within one clock tick keeps both the modification
time and the size, so a change check on `(mtime, size)` misses it. Hash the
content. Tests that rewrite a file with equal-length text hit this first.

## 5. A file another process has open cannot be replaced (#92)

On Windows `os.replace`, and sometimes plain `open`, raise `PermissionError` while
any other handle on the file is open: antivirus, the search indexer, an editor, or
a test polling the file. Linux and macOS allow it.

- `SettingsWriter` retries the replace for about one second.
- Readers that poll skip the tick instead of failing.
- Tests read `config.toml` through the retrying helper in `tests/qt_app.py`, never
  with a bare `read_text()` while the app may be writing.

**Why it was not obvious:** the first sighting was a plain timeout. The
`PermissionError` only showed on a rerun.
