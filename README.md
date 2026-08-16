---
title: murmly
description: Fedora-first local voice-to-text daemon for Linux desktops
---

## Overview

`murmly` is a Fedora-first, local voice-to-text tool for Linux desktops. It is designed around Wayland defaults: instead of trying to grab a global hotkey directly, the daemon listens on a per-user UNIX socket and the desktop environment shortcut calls `murmly toggle`.

## Implemented shape

- `murmly daemon` starts a UNIX socket server at `/run/user/$UID/murmly.sock` (or `$XDG_RUNTIME_DIR/murmly.sock`).
- `murmly toggle` sends a socket message that moves the daemon from `IDLE -> LISTENING -> THINKING -> DONE -> IDLE`.
- `murmly spike` records a short clip from the default microphone with `sounddevice`, transcribes it with `faster-whisper`, prints the text, and copies it to the clipboard.
- Clipboard and paste integration is Fedora/Wayland-aware:
  - Wayland clipboard: `wl-copy`
  - X11 clipboard fallback: `xclip`
  - Wayland paste injection: `wtype`, then `ydotool`
  - X11 paste fallback: `xdotool`
- `contrib/murmly.service` provides a user-systemd service template.

## Runtime dependencies

`uv sync` or `pip install -e .` installs the Python runtime dependencies. For
GPU-accelerated balanced and accurate profiles, install the CUDA runtime extra:

```bash
uv sync --extra cuda
uv run --extra cuda murmly doctor
```

Install these desktop tools separately:

- Python 3.12+
- `wl-clipboard` on Wayland (`wl-copy`, `wl-paste`)
- `wtype` or `ydotool` for Wayland paste simulation
- `xclip` and `xdotool` on X11; `wl-copy` cannot access an X11 clipboard

For the recording overlay on Fedora KDE Plasma, install the shared native
visual runtime:

```bash
sudo dnf install gtk4 python3-gobject libX11 libXext
```

Plasma Wayland also requires Layer Shell:

```bash
sudo dnf install gtk4-layer-shell
```

The daemon runs from the project's isolated `uv` environment. The overlay is a
separate helper launched with `/usr/bin/python3` so it can use Fedora's tested
PyGObject and GTK introspection packages without compiling duplicate bindings
inside the virtual environment. Missing visual packages disable only the
overlay; recording, transcription, clipboard, and paste handling continue.

## Quick start

```bash
uv sync
uv run murmly doctor
```

`uv run murmly <command>` works from the repository without activating `.venv`. To use the bare `murmly` command in the current shell, activate the environment first:

```bash
. .venv/bin/activate
murmly doctor
```

## How to run it

1. Install the project dependencies and create the local environment:

   ```bash
   uv sync
   ```

2. Check that the desktop environment, clipboard tools, and model configuration are detected correctly:

   ```bash
   uv run murmly doctor
   ```

3. Start the background daemon that listens for toggle requests:

   ```bash
   uv run murmly daemon
   ```

4. Ask the daemon to start or stop capture from a desktop shortcut or shell:

   ```bash
   uv run murmly toggle
   ```

   If you installed the CUDA extra and want the GPU-backed runtime to be used for the active transcription session, run the same toggle through the CUDA-enabled environment:

   ```bash
   uv run --extra cuda murmly toggle
   ```

   The daemon cycles through `IDLE -> LISTENING -> THINKING -> DONE -> IDLE` and writes socket state updates for the active desktop environment.

      On KDE Plasma X11 or Wayland, the overlay appears at the bottom center after the
      microphone opens successfully. Its seven bars follow the smoothed live
      microphone level while listening, switch to a processing animation during
      transcription and paste handling, and disappear on success. Capture or
      processing failures show a brief error symbol without exposing audio or
      transcript content.

5. Check the current daemon state:

   ```bash
   uv run murmly status
   ```

6. Run a one-off recording test without a daemon:

   ```bash
   uv run murmly spike --seconds 5
   ```

   Add `--paste` to copy the transcription and inject it into the active input field.

7. For GNOME or KDE shortcuts, point the hotkey to the venv entrypoint:

   ```bash
   /path/to/murmly/.venv/bin/murmly toggle
   ```

### Fedora-first spike

```bash
uv run murmly spike --seconds 5
```

This first requests 16kHz mono PCM from the default microphone, transcribes it with the balanced profile (`base.en`, `int8`, CPU), prints the result, and copies it to the clipboard. If the selected input does not support 16kHz, `murmly` retries a usable physical microphone at its native rate and lets `faster-whisper` resample the correctly labeled WAV during decoding.

### Daemon and DE shortcut flow

Start the daemon:

```bash
uv run murmly daemon
```

Then bind a GNOME or KDE shortcut to either of these commands:

```bash
/path/to/murmly/.venv/bin/murmly toggle
```

or, when using the CUDA-enabled environment:

```bash
uv run --extra cuda murmly toggle
```

Press once to begin capture, then press again to stop, transcribe, copy, and paste.

## Configuration

Configuration lives at `~/.config/murmly/config.toml` (or `$XDG_CONFIG_HOME/murmly/config.toml`).

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
restore_delay_ms = 200

[overlay]
enabled = true
bottom_margin_px = 32 # Logical pixels from the display's bottom edge, 0-512
reduced_motion = false
```

The recording overlay supports KDE Plasma on X11 and Wayland. It uses X11 EWMH
window state plus the X Shape extension on X11, and Layer Shell on Wayland. Both
backends display the same fixed 156 by 48 logical-pixel surface, do not request
keyboard focus, and do not receive pointer input. On multiple displays, Murmly
selects the display containing the desktop origin and keeps that selection for
the recording session. Reduced motion uses stable state symbols and stepped
level feedback instead of continuous animation.

Profile mapping:

- `fast` -> `tiny.en`
- `balanced` -> `large-v3-turbo`
- `accurate` -> `large-v3`

With `device = "auto"`, Murmly uses CUDA `float16` when a compatible GPU and
the CUDA extra are available. It falls back to CPU `int8` otherwise. The first
use of a profile downloads its model; later daemon sessions reuse the local
model cache. The tested CUDA runtime wheels use about 1.4 GB of downloads, and
the cached `large-v3-turbo` model uses about 1.6 GB. Include `--extra cuda` in
`uv run` commands, or invoke `.venv/bin/murmly` after syncing the extra. The
balanced model revision is pinned for reproducible downloads.

## Development

Run the focused stdlib test suite with:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

## Overlay troubleshooting

Inspect the same system-Python imports used by the renderer:

```bash
uv run murmly doctor
/usr/bin/python3 src/murmly/overlay_renderer.py --check
```

The helper infers `x11` or `wayland` from `XDG_SESSION_TYPE`. To inspect another
backend explicitly, add `--backend x11` or `--backend wayland`. The doctor report
shows GDK X11 and native X11 availability for X11 sessions, or GTK4 Layer Shell
availability for Wayland sessions.

When the daemon runs as a user service, inspect renderer launch failures in the
user journal:

```bash
journalctl --user -u murmly.service -b
```

Confirm that the service receives the active Plasma session environment after
login. Restart it after installing visual packages or changing configuration:

```bash
systemctl --user daemon-reload
systemctl --user restart murmly.service
```

To disable or roll back the visual surface without changing voice-to-text
behavior, set `overlay.enabled = false` and restart the daemon. If the overlay
is unavailable or exits during recording, use the second shortcut press as
normal; Murmly still stops capture and processes the buffered audio.
