---
title: Deletion probes must run in a child process with a sandboxed HOME
description: A review probe calling removal.purge_targets with a tmp HOME deleted the real ~/.config/murmly when the path helpers did not honour the given env
trigger: removal.py, purge, murmly uninstall --purge, settings_writer, deletion probes, Path.home()
depends_on: src/murmly/removal.py, src/murmly/paths.py, .github/workflows/tests.yml, "#94"
recorded: 2026-10-13
---

## What happened

On 2026-10-12, a review probe called `removal.purge_targets(env={"HOME": tmp})`
and then `purge()`. The path helpers at the time resolved HOME from the real
`Path.home()` when the given env had no XDG values, and `purge()` deleted the
real `~/.config/murmly` and `~/.local/share/murmly` on the test runner — settings,
models, enrolled voiceprints, gone.

#94 made the helpers honour the given env. The rule stands.

## What to do

Run any probe that deletes or writes in a child process with every home-like env
var sandboxed to a scratch directory:

- HOME
- USERPROFILE (Windows)
- XDG_CONFIG_HOME
- XDG_DATA_HOME
- XDG_CACHE_HOME
- XDG_RUNTIME_DIR
- APPDATA (Windows)
- LOCALAPPDATA (Windows)

Assert every resolved target is inside the scratch dir before anything runs. On
Fedora and systems like it where `/home` is a separate btrfs subvolume, snapper's
root config does not snapshot it, so there is no copy to restore from.

Write a full JSON dump with `murmly doctor` after the probe. It is the only
complete record of what the path helpers resolved.
