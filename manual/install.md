# Installing murmly

murmly installs on Fedora, under KDE Plasma, and needs Python 3.12 or newer. Everything
below is a command you run in a terminal. X11 is verified end to end; Plasma Wayland
uses the same hotkey-registration path with a different key-grab mechanism underneath,
and has not been verified end to end. See [What you need before you
start](what-you-need.md) for the full detail, including what happens on a desktop other
than KDE Plasma.

## Install it

The install command is one line:

```bash
./setup.sh install Meta+X
```

One script covers installing, upgrading, and removing. It installs the system packages
for your session after showing you the `dnf` command and asking, syncs the Python
environment, binds the hotkey, and starts the part of murmly that keeps running in the
background — the murmly service.

The same script covers a few other cases:

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

??? note "What the script is careful to get right"
    What it exists to get right is the sync. `uv sync` — the command that installs
    murmly's Python dependencies — makes the environment match exactly the extras it is
    given, so a plain sync removes the CUDA wheels; and separately, the GPU build of
    ONNX Runtime, which speech output uses, replaces the CPU one, so every sync puts the
    CPU one back. The script reads what is installed before each sync, names all of it,
    and reapplies the swap afterwards, so an upgrade never removes a feature you had.
    Speech output needs none of that care: it is installed by default, as a dependency
    group rather than an extra, and only `--no-group tts` leaves it out.

## Check it worked

Confirm murmly picked up everything correctly:

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

Installing murmly writes exactly three files:

| Path | Purpose |
| --- | --- |
| `~/.config/systemd/user/murmly.service` | starts the murmly service with your session |
| `~/.local/share/applications/net.local.murmly.desktop` | carries the hotkey |
| `~/.local/share/applications/net.local.murmly-session.desktop` | carries the speech-session hotkey, when one was requested |

Nothing else. murmly never edits your global shortcut configuration, and `murmly
uninstall` removes every one of these files.

## Installing by hand

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

If you have an NVIDIA GPU, sync the CUDA extra first so the same environment carries it:

```bash
uv sync --extra cuda
uv run --extra cuda murmly install Meta+X
```

`uv sync --extra cuda` really is the whole command needed. It wasn't always: speech
output used to be installed the same optional way, as an extra, and running this exact
command would then have uninstalled it — which is why the speech synthesizer is now its
own dependency group instead, and this command can no longer touch it.

For what the GPU actually buys you, see [Speed, memory, and your graphics
card](speed-and-memory.md).

## The recording overlay's packages

The overlay that shows you what murmly is doing while you talk needs a few system
packages of its own:

```bash
sudo dnf install gtk4 python3-gobject libX11 libXext
sudo dnf install gtk4-layer-shell   # Plasma Wayland only
```

`gtk4-layer-shell` is needed on Plasma Wayland only, as the comment above says. The
overlay runs as a separate helper under `/usr/bin/python3` — Fedora's own system Python —
rather than inside murmly's own environment, so it can use Fedora's tested PyGObject and
GTK packages instead of compiling duplicates inside the virtual environment. If any of
these packages are missing, only the overlay is disabled — recording, transcription,
clipboard, and paste handling all keep working.

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

---

With murmly installed, see [Using murmly](using-murmly.md) for how it works day to day.
