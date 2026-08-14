# murmly

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

The Python package keeps the heavy runtime integrations optional, but the implementation expects these tools to be installed at runtime:

- Python 3.12+
- `sounddevice`
- `faster-whisper`
- `wl-clipboard` on Wayland (`wl-copy`, `wl-paste`)
- `wtype` or `ydotool` for Wayland paste simulation
- `xclip` / `xdotool` for X11 fallback

## Quick start

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
```

### Fedora-first spike

```bash
murmly spike --seconds 5
```

This records 16kHz mono PCM from the default microphone, transcribes it with the balanced profile (`base.en`, `int8`, CPU), prints the result, and copies it to the clipboard.

### Daemon and DE shortcut flow

Start the daemon:

```bash
murmly daemon
```

Then bind a GNOME or KDE shortcut to:

```bash
murmly toggle
```

Press once to begin capture, then press again to stop, transcribe, copy, and paste.

## Configuration

Configuration lives at `~/.config/murmly/config.toml` (or `$XDG_CONFIG_HOME/murmly/config.toml`).

```toml
[daemon]
socket_path = "/run/user/1000/murmly.sock"

[audio]
sample_rate_hz = 16000
channels = 1

[stt]
model_profile = "balanced" # fast | balanced | accurate
compute_type = "int8"
lazy_load_model = true

[clipboard]
restore = true
restore_delay_ms = 200
```

Profile mapping:

- `fast` -> `tiny.en`
- `balanced` -> `base.en`
- `accurate` -> `small.en`

## Development

Run the focused stdlib test suite with:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```