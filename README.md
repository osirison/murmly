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

Then bind a GNOME or KDE shortcut to:

```bash
/path/to/murmly/.venv/bin/murmly toggle
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
```

Profile mapping:

- `fast` -> `tiny.en`
- `balanced` -> `large-v3-turbo`
- `accurate` -> `large-v3`

With `device = "auto"`, Murmly uses CUDA `float16` when a compatible GPU and
the CUDA extra are available. It falls back to CPU `int8` otherwise. The first
use of a profile downloads its model; later daemon sessions reuse the local
model cache. The tested CUDA runtime wheels use about 1.4 GB of downloads, and
the cached `large-v3-turbo` model uses about 1.6 GB. Include `--extra cuda` in
`uv run` commands, or invoke `.venv/bin/murmly` after syncing the extra.

## Development

Run the focused stdlib test suite with:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```
