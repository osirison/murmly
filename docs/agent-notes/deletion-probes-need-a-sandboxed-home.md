---
title: Deletion probes must run in a child process with a sandboxed HOME
description: A review probe calling removal.purge_targets with a tmp HOME deleted the real ~/.config/murmly when the path helpers did not honour the given env
trigger: removal.py, purge, murmly uninstall --purge, settings_writer, deletion probes, Path.home()
depends_on: src/murmly/removal.py, src/murmly/config.py, src/murmly/installer.py, .github/workflows/tests.yml, "#94"
recorded: 2026-10-13
---

## What happened

On 2026-10-12, a review probe called `removal.purge_targets(env={"HOME": tmp})`
and then `purge()`. The path helpers at the time resolved HOME from the real
`Path.home()` when the given env had no XDG values, and `purge()` deleted the
real `~/.config/murmly` and `~/.local/share/murmly` — settings, the hotkey record,
the Kokoro and speaker models, and the enrolled voiceprints.

The loss was permanent. On this Fedora machine `/home` is its own btrfs subvolume
and snapper has only a `root` config, so no snapshot held the files.

#94 made the helpers honour the given env. The rule stands. The helpers are
`home_directory`, `absolute_setting`, `default_config_path` and `default_data_dir`
in `src/murmly/config.py`, the installer's path helpers in `src/murmly/installer.py`,
and `purge_targets` and `purge` in `src/murmly/removal.py`.

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

Assert every resolved target is inside the scratch dir before anything runs. Keep
a copy of anything you cannot recreate before the probe runs.

A `murmly doctor` JSON dump is the only full record of the resolved settings (config
values and the paths in use). It is what made rebuilding `config.toml` possible. It
does not list purge targets, so it is no substitute for a copy.
