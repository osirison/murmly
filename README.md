---
title: murmly
description: Fedora-first local voice-to-text daemon for Linux desktops
---

# murmly

`murmly` is a Fedora-first, local voice-to-text tool for Linux desktops. Press a
hotkey, speak, press it again: the transcript is typed into whatever you were
working in. Everything runs locally.

**[osirison.github.io/murmly](https://osirison.github.io/murmly/)** — what it is,
in one page with pictures. The
**[manual](https://osirison.github.io/murmly/manual/)** is everything else.

## What you need

- Fedora
- KDE Plasma, for the hotkey and the recording overlay
- Python 3.12 or newer
- A terminal, to run the installer
- An X11 session. Plasma Wayland uses the same registration path with a
  different key-grab mechanism and has not been verified end to end

## Install

```bash
./setup.sh install Meta+X
```

One script installs, upgrades, and removes. It installs the system packages for
your session after showing you the `dnf` command and asking, syncs the Python
environment, binds the hotkey, and starts the service.

```bash
./setup.sh upgrade     # pull, re-sync, rebind, restart
./setup.sh uninstall   # remove the service, hotkeys, and announcements
uv run murmly doctor   # report what murmly found on this machine
```

`--yes` answers every prompt for an unattended run, which includes the one
confirming what `--purge` is about to delete, so those two together remove the
environment, the models, and your configuration without asking.

Installing by hand, the GPU runtime, and what to do if murmly cannot paste on
your desktop are on the
[install page](https://osirison.github.io/murmly/manual/install/).

## Use it

1. Press your hotkey. The overlay appears at the bottom of the screen and its
   bars follow your voice.
2. Speak.
3. Press the hotkey again. Murmly transcribes, copies, and pastes into the
   window you started in.

Registration is confirmed at install time, but only a keypress proves the
desktop actually delivers the key — so press it once after installing.

## It can also speak

Murmly can read a coding assistant's output aloud and tell you when it has
finished a turn. It is off by default:
[making murmly speak](https://osirison.github.io/murmly/manual/making-murmly-speak/).

## Documentation

Every setting, changing your hotkey, where transcripts go, speech output and
troubleshooting are in the
**[manual](https://osirison.github.io/murmly/manual/)**.

## Development

```bash
uv run --no-sync python -m unittest discover -s tests
```

`--no-sync` matters: `uv run` otherwise syncs first, and the sync reinstalls the
CPU build of `onnxruntime` over the GPU build on a machine that has had the swap
applied.

Run any command against the project environment without installing anything:

```bash
uv run murmly doctor
uv run murmly daemon          # run the daemon in the foreground
uv run murmly toggle          # drive it from another shell
uv run murmly toggle-session  # the same, delivered to the open speech session
uv run murmly status
uv run murmly spike --seconds 5   # one-off recording, no daemon
```

Add `--extra cuda` to any of these to use the GPU-backed runtime, or activate
the environment and use the bare command:

```bash
. .venv/bin/activate
murmly doctor
```

Behavioral changes are planned with OpenSpec; see `openspec/specs/` for the
current capability baseline. Operational preconditions that are not documented
elsewhere live in [docs/agent-notes/](docs/agent-notes/).
