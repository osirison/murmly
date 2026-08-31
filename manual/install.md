# Installing murmly

murmly installs on Linux and on Windows, and needs Python 3.12 or newer on either one.
Everything below is a command you run in a terminal — there is no graphical installer on
either platform. Two machines that are otherwise Linux or Windows cannot run murmly at
all: musl-based Linux, and Windows on the ARM64 architecture. Neither has a build of the
transcription runtime, and murmly refuses to sync on either, naming why. macOS is not
supported.

Whether the hotkey registers itself and whether the recording overlay appears both
depend on your desktop on Linux, and are automatic on Windows with no session-specific
gap. On Linux, X11 under KDE Plasma is verified end to end; Plasma Wayland uses the same
hotkey-registration path with a different key-grab mechanism underneath and has not been
verified end to end; GNOME has a hotkey backend that has never been run against a live
GNOME session; every other desktop has no automatic hotkey registration or overlay at
all. Windows has run every capability below as an automated test on a real Windows
machine, short of the deeper human-driven checks against a second account and an
elevated window, which remain open.

Permissions: on plain X11, none. Under KDE Plasma's Wayland session, the first paste
raises a one-time permission dialog — see [Answering the KDE permission
dialog](what-you-need.md#answering-the-kde-permission-dialog). On Windows, the hotkey
and clipboard need none, but microphone capture depends on two toggles under **Settings
→ Privacy & security → Microphone**, both of which have to be on; `murmly doctor` reports
whether that permission is granted, denied, or undetermined. See [What you need before
you start](what-you-need.md) for the full detail behind all of this.

## Install it on Linux

The install command is one line:

```bash
./bootstrap.sh install Meta+X
```

`bootstrap.sh` is the fresh-machine entry point: it installs `uv` if it is not already
on `PATH`, then hands every argument straight to `setup.sh`. `setup.sh` is what actually
covers installing, upgrading, and removing. It installs the system packages for your
session after showing you the command for your package manager and asking, syncs the
Python environment, binds the hotkey, and starts the part of murmly that keeps running
in the background — the murmly service.

Once `uv` is on `PATH`, the commands below can call `setup.sh` directly — that is what
`bootstrap.sh` itself does on every run after the first:

```bash
./setup.sh install Meta+X Meta+A   # also bind the speech-session hotkey
./setup.sh upgrade                 # pull, re-sync, rebind, restart
./setup.sh hooks                   # announce finished agent turns out loud
./setup.sh uninstall               # remove the service, hotkeys, and announcements
./setup.sh uninstall --purge       # and the environment, models, and configuration
```

`./setup.sh hooks` is covered on its own page: see [Hearing when your coding assistant
finishes](announcements.md).

Without the flags, the script offers the GPU runtime when it detects an NVIDIA driver,
and speech output with its model files. `--cuda`/`--no-cuda` and `--tts`/`--no-tts`
answer those two prompts without asking. `--yes` answers every prompt, for an unattended
run. With nothing attached to the terminal and no `--yes`, every prompt is declined
rather than assumed.

!!! warning "`--yes` together with `--purge`"
    `--yes` also answers the prompt that confirms what `--purge` is about to delete.
    Combined, `--yes --purge` removes the environment, the models, and your
    configuration without asking.

??? note "What keeps a sync from quietly removing a feature you had"
    `uv sync` — the command that installs murmly's Python dependencies — makes the
    environment match exactly the extras it is given, so a plain sync removes the CUDA
    wheels; and separately, the GPU build of ONNX Runtime, which speech output uses,
    replaces the CPU one, so every sync puts the CPU one back. `murmly sync` — which
    `setup.sh` calls rather than `uv sync` directly — reads what is installed before each
    sync, names all of it, and reapplies the swap afterwards, so an upgrade never removes
    a feature you had. Speech output needs none of that care: it is installed by default,
    as a dependency group rather than an extra, and only `--no-group tts` leaves it out.

## Install it on Windows

There is no `setup.sh` equivalent on Windows — no system packages to detect, no `dnf`.
The install command is `bootstrap.ps1`, which does exactly two things: install `uv` if
it is not already on `PATH`, then hand every argument straight to `murmly` inside this
checkout's own environment.

```powershell
.\bootstrap.ps1 install Meta+X
```

`uv run`, which `bootstrap.ps1` hands off to, syncs the environment on demand, so this
one line is the whole install for the base capability: capture, transcription,
clipboard, paste, and the hotkey. Two things are opt-in extras that a plain sync does
not carry, and each needs its own `uv sync` first:

```powershell
uv sync --extra overlay   # the recording overlay's Qt toolkit
uv sync --extra cuda      # the GPU-backed transcription and speech runtime
.\bootstrap.ps1 install Meta+X
```

`uv sync --extra overlay` installs PySide6, the toolkit the Windows overlay renderer
draws with. Without it, only the overlay is disabled — recording, transcription,
clipboard, and paste handling all keep working, the same rule Linux's missing GTK4
packages follow. `uv sync --extra cuda` behaves exactly as it does on Linux: see [Using
your graphics card](#using-your-graphics-card) below.

Registering the hotkey needs no permission. If another application already holds the
combination, Windows refuses the registration and murmly reports the key as taken — but
unlike KDE, Windows gives murmly no way to ask *which* application holds it, so the
report cannot name one.

A few other cases:

```powershell
.\bootstrap.ps1 install Meta+X Meta+A   # also bind the speech-session hotkey
.\bootstrap.ps1 uninstall               # remove the service and release the hotkeys
```

## Check it worked

Confirm murmly picked up everything correctly, on either platform:

```bash
uv run murmly doctor
```

## Choosing a hotkey

A hotkey needs at least one modifier: `Meta`, `Ctrl`, `Alt`, or `Shift`. `Super` and
`Win` are accepted as aliases for `Meta`. The key itself can be `A`-`Z`, `0`-`9`,
`F1`-`F35`, or a named key such as `Space`, `End`, or `Escape`.

Pick something free. `Meta+V`, for example, is already Plasma's clipboard history.
murmly checks before binding, and if a key is already taken, it tells you who owns it
rather than taking it anyway.

## What installing writes

On Linux under KDE Plasma or GNOME, installing murmly writes:

| Path | Purpose |
| --- | --- |
| `~/.config/systemd/user/murmly.service` | starts the murmly service with your session |
| `~/.local/share/applications/net.local.murmly.desktop` | carries the hotkey, on KDE Plasma |
| `~/.local/share/applications/net.local.murmly-session.desktop` | carries the speech-session hotkey, on KDE Plasma, when one was requested |
| `~/.config/murmly/hotkeys.json` | a record of which purposes are bound to which keys, written on every platform regardless of where the desktop itself holds the binding |

On GNOME, the hotkey lives in GNOME's own `custom-keybindings` schema instead of a
`.desktop` file, but the systemd unit and the hotkey record above are still written the
same way.

On Windows, installing murmly writes:

| What | Where |
| --- | --- |
| A Task Scheduler task named `MurmlyDaemon`, with a logon trigger | Task Scheduler, not a file on disk |
| A hotkey record, for the same reason as Linux's — Windows registers the hotkey inside the running daemon's own process, and nothing else remembers it | `%APPDATA%\murmly\hotkeys.json` |

Nothing else, on either platform. murmly never edits your global shortcut
configuration, and `murmly uninstall` removes everything in whichever table above
applies to you.

## Installing by hand

This is the Linux path without `setup.sh`; on Windows, `bootstrap.ps1` already does the
equivalent of this (see [Install it on Windows](#install-it-on-windows) above), since
Windows has no system packages of its own to install first.

If you'd rather not use `setup.sh`, install with `uv` directly:

```bash
uv sync
uv run murmly install Meta+X
```

To also bind the hotkey that dictates into an open speech session, pass a second key:

```bash
uv run murmly install Meta+X Meta+A
```

That is the whole installation. It:

- writes a systemd user service that starts `murmly` with your graphical session and
  stops it at logout,
- registers `Meta+X` as a global hotkey, taking effect immediately, and
- refuses if a hotkey is already taken, naming the application that owns it — and
  refuses, having written nothing, when the key is murmly's own other hotkey (name both
  keys in one command to move it) or when the same key was given for both.

Check everything was detected correctly:

```bash
uv run murmly doctor
```

## Using your graphics card

If you have an NVIDIA GPU, sync the CUDA extra first so the same environment carries it —
the same command on Linux and on Windows:

```bash
uv sync --extra cuda
uv run --extra cuda murmly install Meta+X
```

`uv sync --extra cuda` really is the whole command needed, on both platforms: every
package the extra pins publishes a Windows wheel as well as a Linux one. It wasn't
always this simple: speech output used to be installed the same optional way, as an
extra, and running this exact command would then have uninstalled it — which is why the
speech synthesizer is now its own dependency group instead, and this command can no
longer touch it. `setup.sh` detects an NVIDIA driver and offers this for you on Linux;
`bootstrap.ps1` has no equivalent detection on Windows, so run the sync yourself first.

The CUDA extra has no Windows-on-ARM64 wheels either, which is moot there since ARM64
already has no transcription runtime to accelerate.

For what the GPU actually buys you, see [Speed, memory, and your graphics
card](speed-and-memory.md).

## The recording overlay's packages

On Linux, the overlay that shows you what murmly is doing while you talk needs a few
system packages of its own:

```bash
sudo dnf install gtk4 python3-gobject libX11 libXext
sudo dnf install gtk4-layer-shell   # Plasma Wayland only
```

`gtk4-layer-shell` is needed on Plasma Wayland only, as the comment above says. The
overlay runs as a separate helper under `/usr/bin/python3` — the system Python, not
murmly's own environment — so it can use the distribution's own tested PyGObject and GTK
packages instead of compiling duplicates inside the virtual environment. Replace `dnf`
with your distribution's own package manager and package names if it is not
Fedora-based; `setup.sh` names them for you.

On Windows, the overlay's toolkit is a pip extra rather than a system package:

```powershell
uv sync --extra overlay
```

On either platform, if the overlay's packages are missing, only the overlay is disabled
— recording, transcription, clipboard, and paste handling all keep working.

## If murmly cannot paste on your desktop

This only comes up on a Wayland compositor offering neither the virtual keyboard
protocol nor an XTEST bridge — see [What you need before you start](what-you-need.md)
for when that applies to you. murmly then falls back to `ydotool`, whose background
half, `ydotoold`, needs access to `/dev/uinput`, a device owned by root.

Rather than override the packaged system service for `ydotoold` — its `ExecStart`
cannot name your runtime directory, because `/run/user/$UID` does not exist yet when the
unit starts at boot — grant your own user account access and run the daemon as yourself:

```bash
sudo dnf install ydotool
echo 'KERNEL=="uinput", SUBSYSTEM=="misc", OPTIONS+="static_node=uinput", TAG+="uaccess"' \
  | sudo tee /etc/udev/rules.d/60-ydotool-uinput.rules
sudo udevadm control --reload && sudo udevadm trigger
```

Log out and back in, then run `ydotoold` yourself, as a user service. As your own
process it automatically picks up `XDG_RUNTIME_DIR` and writes its socket exactly where
murmly looks for it — no flags, no socket-ownership juggling.

!!! note
    This route is documented from the packaged binaries and udev's `uaccess` behaviour,
    not from having run it end to end on a murmly machine — both KDE and wlroots have
    cheaper routes that don't need it.

## If murmly cannot paste on Windows

This is not a missing tool the way the Linux case above is — the Win32 clipboard and
`SendInput` are always present. It happens when the window you were dictating into is
running elevated (opened with "Run as administrator"): Windows itself silently discards
the keystrokes, reporting nothing wrong. See [where your words
go](where-your-words-go.md#pasting-into-an-elevated-window-on-windows) for what murmly
does about it.

---

With murmly installed, see [Using murmly](using-murmly.md) for how it works day to day.
