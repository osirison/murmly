---
title: murmly
description: Fedora-first local voice-to-text daemon for Linux desktops
---

## Overview

`murmly` is a Fedora-first, local voice-to-text tool for Linux desktops. Press a
hotkey, speak, press it again: the transcript is typed into whatever you were
working in. Everything runs locally.

It is built around Wayland's refusal to hand out global hotkeys. Rather than
grabbing a key itself, `murmly` runs a small daemon on a per-user UNIX socket
and lets the desktop's own shortcut system call `murmly toggle`.

## Requirements

- Python 3.12+
- KDE Plasma, for the hotkey and the recording overlay
- Clipboard and paste tools for your session:
  - Wayland: `wl-clipboard` plus `wtype` or `ydotool`
  - X11: `xclip` and `xdotool`

For the recording overlay:

```bash
sudo dnf install gtk4 python3-gobject libX11 libXext
sudo dnf install gtk4-layer-shell   # Plasma Wayland only
```

The overlay runs as a separate helper under `/usr/bin/python3` so it can use
Fedora's tested PyGObject and GTK packages instead of compiling duplicates
inside the virtual environment. Missing visual packages disable only the
overlay; recording, transcription, clipboard, and paste handling continue.

## Install

```bash
uv sync
uv run murmly install Meta+X
```

That is the whole installation. It:

- writes a systemd user service that starts `murmly` with your graphical
  session and stops it at logout,
- registers `Meta+X` as a global hotkey, taking effect immediately, and
- refuses, naming the current owner, if that hotkey already belongs to another
  application.

For the GPU-backed runtime, sync the CUDA extra first so the same environment
carries it:

```bash
uv sync --extra cuda
uv run --extra cuda murmly install Meta+X
```

Check everything was detected correctly:

```bash
uv run murmly doctor
```

### Choosing a hotkey

A hotkey needs at least one modifier: `Meta`, `Ctrl`, `Alt`, or `Shift`.
`Super` and `Win` are accepted as aliases for `Meta`. Keys may be `A`-`Z`,
`0`-`9`, `F1`-`F35`, or a named key such as `Space`, `End`, or `Escape`.

Pick something free. `Meta+V` is Plasma's clipboard history, for example.
Murmly checks before binding and tells you who owns a key it will not take.

### What installation writes

| Path | Purpose |
| --- | --- |
| `~/.config/systemd/user/murmly.service` | starts the daemon with your session |
| `~/.local/share/applications/net.local.murmly.desktop` | carries the hotkey |

Nothing else. Murmly never edits your global shortcut configuration, and
`murmly uninstall` removes both files.

## Use it

1. Press your hotkey. The overlay appears at the bottom of the screen and its
   bars follow your voice.
2. Speak.
3. Press the hotkey again. Murmly transcribes, copies, and pastes into the
   window you started in.

Registration is confirmed at install time, but only a keypress proves the
desktop actually delivers the key — so press it once after installing.

## Change or remove the hotkey

Rebind by installing again with a different key:

```bash
uv run murmly install Meta+Shift+Space
```

Remove everything:

```bash
uv run murmly uninstall
```

If you move the project directory or rebuild its environment, the recorded path
goes stale. Run `murmly install <hotkey>` again from the new location to repair
it; `murmly doctor` shows the path currently recorded.

## Scope and limitations

- **Hotkey registration is KDE Plasma only.** On other desktops `murmly install`
  still installs the service and prints the command to bind manually.
- **Verified on X11.** Plasma Wayland uses the same registration path but a
  different key-grab mechanism, and has not been verified end-to-end. Murmly
  says so during installation and checks whether the binding took effect.
- **The daemon runs for the whole session.** From the first toggle it keeps the
  transcription model in memory — roughly 1.6 GB for the balanced profile, in
  VRAM when running on CUDA — until you log out. Set a smaller
  `stt.model_profile` if that matters on your machine.

## Configuration

Configuration lives at `~/.config/murmly/config.toml` (or
`$XDG_CONFIG_HOME/murmly/config.toml`).

```toml
[daemon]
socket_path = "/run/user/1000/murmly.sock"

[audio]
sample_rate_hz = 16000 # Preferred capture rate
channels = 1

[stt]
model_profile = "balanced" # fast | balanced | accurate
device = "auto" # auto | cpu | cuda
compute_type = "auto"
beam_size = 5
vad_filter = true
lazy_load_model = true

[clipboard]
restore = true
restore_delay_ms = 500 # Wait before restoring the previous clipboard, 0-5000
verify_target = true # Refuse to paste if focus left the window you dictated into

[overlay]
enabled = true
bottom_margin_px = 32 # Logical pixels from the display's bottom edge, 0-512
reduced_motion = false
```

Profile mapping:

- `fast` -> `tiny.en`
- `balanced` -> `large-v3-turbo`
- `accurate` -> `large-v3`

With `device = "auto"`, Murmly uses CUDA `float16` when a compatible GPU and the
CUDA extra are available, and falls back to CPU `int8` otherwise. The first use
of a profile downloads its model; later sessions reuse the local cache. The
tested CUDA runtime wheels are about 1.4 GB and the cached `large-v3-turbo`
model about 1.6 GB. The balanced model revision is pinned for reproducible
downloads.

Restart the service after changing configuration:

```bash
systemctl --user restart murmly.service
```

## Transcript delivery

Murmly decides where a transcript goes at the moment it presses Ctrl+V, which is
seconds after you stop speaking. To stop transcripts landing in whatever happened
to take focus in the meantime, Murmly records the focused window when capture
stops and checks it again just before pasting.

If the focused window changed, Murmly **does not paste**. The transcript is left
on your clipboard so you can place it yourself, the previous clipboard is not
restored, and the overlay shows its error symbol. The daemon reports this as
`"delivered": false`:

```json
{
  "ok": true,
  "state": "DONE",
  "text": "the words you spoke",
  "delivered": false,
  "detail": "Transcript copied to the clipboard but not pasted."
}
```

The same check applies to `murmly spike --paste`.

### What each session gets

Verification needs to read the focused window, which only X11 exposes to
applications:

| Session | Target verification | Clipboard preservation |
| --- | --- | --- |
| X11 with an EWMH window manager | yes | yes |
| X11 without EWMH | no | yes |
| Wayland | no | yes |

`murmly doctor` reports which applies under `delivery`.

### Restoring your previous clipboard

Pasting overwrites your clipboard, so Murmly puts the previous contents back
afterwards. It waits `restore_delay_ms` first, giving the receiving application
time to read the transcript. This is a margin, not a guarantee: Murmly cannot
tell whether the application has read the clipboard, because a desktop clipboard
manager such as Klipper takes a copy of every clipboard change immediately, so
any "someone read it" signal reports the manager rather than the application.

Raise `restore_delay_ms` if a slow application ever pastes your previous
clipboard instead of the transcript. Values outside 0-5000 fall back to 500. Set
`restore = false` to keep the transcript on the clipboard and never restore.

To paste unconditionally, as Murmly did before target verification existed:

```toml
[clipboard]
verify_target = false
```

## The recording overlay

The overlay supports KDE Plasma on X11 and Wayland. It uses X11 EWMH window
state plus the X Shape extension on X11, and Layer Shell on Wayland. Both
backends display the same fixed 156 by 48 logical-pixel surface, do not request
keyboard focus, and do not receive pointer input. On multiple displays, Murmly
selects the display containing the desktop origin and keeps that selection for
the recording session. Reduced motion uses stable state symbols and stepped
level feedback instead of continuous animation.

Set `overlay.enabled = false` and restart the service to turn it off without
changing voice-to-text behavior.

## Troubleshooting

Start with the diagnostics, which report the session, clipboard tools, model
runtime, delivery verification, overlay, and installation state:

```bash
uv run murmly doctor
```

Service logs:

```bash
systemctl --user status murmly.service
journalctl --user -u murmly.service -b
```

**The hotkey does nothing.** Check `installation.hotkey_held` in `murmly doctor`.
If another application holds the key, it will be named there. Pressing the hotkey
also starts the service if it is installed but not running, so a hotkey that does
nothing at all usually means the binding, not the daemon.

**Overlay problems.** Inspect the same system-Python imports the renderer uses:

```bash
/usr/bin/python3 src/murmly/overlay_renderer.py --check
```

Add `--backend x11` or `--backend wayland` to inspect a specific backend. Restart
the service after installing visual packages.

**Silent recordings on Intel SOF hardware.** See
[docs/agent-notes/murmly-spike-sof-dmic.md](docs/agent-notes/murmly-spike-sof-dmic.md).

## Development

Run commands against the project environment without installing anything:

```bash
uv run murmly doctor
uv run murmly daemon          # run the daemon in the foreground
uv run murmly toggle          # drive it from another shell
uv run murmly status
uv run murmly spike --seconds 5   # one-off recording, no daemon
```

Add `--extra cuda` to any of these to use the GPU-backed runtime, or activate
the environment and use the bare command:

```bash
. .venv/bin/activate
murmly doctor
```

Run the test suite:

```bash
uv run --extra cuda python -m unittest discover -s tests
```

The suite is stdlib `unittest` with no external test dependencies. Tests that
need a live desktop session skip themselves when it is unavailable.

Behavioral changes are planned with OpenSpec; see `openspec/specs/` for the
current capability baseline. Operational preconditions that are not documented
elsewhere live in [docs/agent-notes/](docs/agent-notes/).
