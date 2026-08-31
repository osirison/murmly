---
title: murmly
description: Local voice-to-text daemon for Linux and Windows desktops
---

# murmly

`murmly` is a local voice-to-text tool for Linux and Windows desktops. Press a
hotkey, speak, press it again: the transcript is typed into whatever you were
working in. Everything runs locally. macOS is not supported.

**[osirison.github.io/murmly](https://osirison.github.io/murmly/)** — what it is,
in one page with pictures. The
**[manual](https://osirison.github.io/murmly/manual/)** is everything else.

## What you need

- **Linux** (any distribution, glibc-based) or **Windows** (x86_64).
  Two machines within those two platforms cannot run murmly at all: musl-based
  Linux and Windows on ARM64, neither of which has a build of the
  transcription runtime. macOS is not supported.
- **Python 3.12 or newer**, and **a terminal**, on either platform.
- **On Linux, your desktop decides the hotkey and overlay.** KDE Plasma X11 is
  verified end to end; Plasma Wayland uses the same registration path with a
  different key-grab mechanism and has not been verified end to end; GNOME has
  a hotkey backend that has never been run against a live GNOME session; any
  other desktop registers no hotkey and shows no overlay automatically, though
  everything else still installs.
- **On Windows, the hotkey and clipboard always work with no permission
  needed**; the overlay needs a separate `uv sync --extra overlay`, and
  microphone capture depends on Windows' own privacy setting, which
  `murmly doctor` reports. Every Windows capability listed here has run as an
  automated test on a real Windows machine; a second account and an elevated
  window are the one deeper check still open.

See [what you need before you start](https://osirison.github.io/murmly/manual/what-you-need/)
for the full detail behind this, including every permission each platform asks
for.

## Install

```bash
./bootstrap.sh install Meta+X      # Linux
```

```powershell
.\bootstrap.ps1 install Meta+X     # Windows
```

Both `bootstrap.sh` and `bootstrap.ps1` do the same two things first: install
`uv` if it is not already on `PATH`, then hand every argument off. On Linux,
that hand-off goes to `setup.sh`, which installs the system packages for your
session after showing you the command and asking, syncs the Python
environment, binds the hotkey, and starts the service — and which the
commands below call directly, since by then `uv` is already there. On
Windows, the hand-off goes straight to `murmly` — there are no system
packages to install first.

```bash
./setup.sh upgrade      # Linux: pull, re-sync, rebind, restart
./setup.sh uninstall    # Linux: remove the service, hotkeys, and announcements
.\bootstrap.ps1 uninstall   # Windows: the same, via Task Scheduler
uv run murmly doctor    # either platform: report what murmly found here
```

`--yes` answers every Linux `setup.sh` prompt for an unattended run, which
includes the one confirming what `--purge` is about to delete, so those two
together remove the environment, the models, and your configuration without
asking.

Installing by hand, the GPU runtime, the overlay's separate install on
Windows, and what to do if murmly cannot paste are on the
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
