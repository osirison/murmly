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
  - Wayland: `wl-clipboard`, plus a paste injector — `wtype` where the compositor
    offers the virtual keyboard protocol (`zwp_virtual_keyboard_manager_v1`), or
    `ydotool` where it does not. Plasma's KWin does not advertise that protocol on
    the sessions Murmly has been tested on, so a Plasma Wayland session needs
    `ydotool` and its daemon (see below).
  - X11: `xclip` and `xdotool`

Murmly picks an injector by running the tool's own no-op invocation, so a tool
that is installed but cannot run in your session is never chosen. When none can
run, every transcript is still copied to the clipboard and `murmly doctor` reports
the reason under `paste_injection` along with the commands that fix it.

### Pasting on a Plasma Wayland session

`ydotool` injects through `/dev/uinput`, so its daemon runs as root while Murmly
runs as you. The shipped unit puts its socket where the client does not look for
it, so point it at your own socket path and give it to your user:

```bash
sudo dnf install ydotool
sudo mkdir -p /etc/systemd/system/ydotool.service.d
sudo tee /etc/systemd/system/ydotool.service.d/murmly.conf >/dev/null <<EOF
[Service]
ExecStart=
ExecStart=/usr/bin/ydotoold --socket-path=$XDG_RUNTIME_DIR/.ydotool_socket --socket-own=$(id -u):$(id -g)
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now ydotool.service
```

`murmly doctor` prints these same commands, filled in for your session, whenever
it cannot inject a paste.

For the recording overlay:

```bash
sudo dnf install gtk4 python3-gobject libX11 libXext
sudo dnf install gtk4-layer-shell   # Plasma Wayland only
```

The overlay runs as a separate helper under `/usr/bin/python3` so it can use
Fedora's tested PyGObject and GTK packages instead of compiling duplicates
inside the virtual environment. Missing visual packages disable only the
overlay; recording, transcription, clipboard, and paste handling continue.

On Wayland the helper loads `libgtk4-layer-shell.so.0` itself, with
`ctypes.CDLL(..., RTLD_GLOBAL)`, before it imports `gi`. gtk4-layer-shell works by
interposing on libwayland-client, so it has to be in the global symbol scope first;
PyGObject pulls libwayland in the moment it imports Gtk, and after that the
layer-shell calls silently do nothing and the compositor places the overlay itself.
Rather than draw an overlay it cannot place, the helper refuses to start and reports
the reason.

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
- **Live transcription puts what you say on screen.** With `stt.live_transcribe`
  enabled, partial transcripts are visible in the overlay while you speak, which
  means they are visible to anyone watching a shared screen. It is disabled by
  default, the text is discarded the moment capture stops, and it is never
  written to a log or a file.

## Configuration

Murmly runs on its defaults, so configuration is optional. When you want to
change something, start from the annotated defaults in
[config.example.toml](config.example.toml):

```bash
mkdir -p ~/.config/murmly
cp config.example.toml ~/.config/murmly/config.toml
```

Murmly reads `~/.config/murmly/config.toml`, or
`$XDG_CONFIG_HOME/murmly/config.toml` when that variable is set. Every option in
the example is shown at its default, so copying it in changes nothing until you
edit it. A value outside its documented range falls back to the default rather
than refusing to start, and `murmly doctor` reports the path actually in use.

```toml
[daemon]
# Defaults to $XDG_RUNTIME_DIR/murmly.sock
# socket_path = "/run/user/1000/murmly.sock"

[audio]
sample_rate_hz = 16000 # Preferred capture rate
channels = 1

[stt]
model_profile = "balanced" # fast | balanced | accurate
device = "auto" # auto | cpu | cuda
compute_type = "auto"
lazy_load_model = true
# beam_size and vad_filter follow the profile unless you pin them here
# beam_size = 5
# vad_filter = true
live_transcribe = false        # Show partial transcripts while you speak
live_interval_ms = 1000        # How often a partial is produced, 250-10000
live_window_seconds = 15       # Trailing audio each partial re-reads, 5-60
auto_transcribe = "off"        # off | stop | continuous
auto_transcribe_silence_ms = 2000     # Silence that triggers auto-transcribe, 250-30000
auto_transcribe_min_speech_ms = 300   # Speech required before silence counts, 0-10000

[clipboard]
restore = true
restore_delay_ms = 500 # Wait before restoring the previous clipboard, 0-5000
verify_target = true # Refuse to paste if focus left the window you dictated into

[overlay]
enabled = true
bottom_margin_px = 32 # Logical pixels from the display's bottom edge, 0-512
reduced_motion = false
text_size_px = 13 # Transcript panel text size, 8-48
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

### Live transcription

`stt.live_transcribe` shows partial transcripts in a panel below the recording
indicator while you speak. Partials are feedback only: they never reach the
clipboard or your application, and the transcript Murmly delivers is always
produced by a fresh pass over the complete recording. Enabling live transcription
cannot change what gets typed.

Whether partials keep pace depends on the profile and device. `murmly doctor`
reports `partial_pass_ceiling_ms`, measured on your machine as the worst case
where a whole window is speech. Measured here with the default 15-second window:

| Device | Profile | Ceiling | Verdict |
| --- | --- | --- | --- |
| CUDA `float16` | `fast` | 244 ms | keeps pace |
| CUDA `float16` | `balanced` | 319 ms | keeps pace |
| CPU `int8` | `fast` | 641 ms | keeps pace |
| CPU `int8` | `balanced` | 12364 ms | **falls behind** |

`large-v3-turbo` on CPU is roughly twelve times over the default 1000 ms
interval. Murmly skips ticks rather than queuing them, so the partial display
simply updates rarely. Silence detection is unaffected: it runs on its own
thread, so a slow partial pass cannot delay auto-transcribe.

One cost does remain. A partial pass already inside the model cannot be
cancelled, and the model decodes one request at a time, so stopping a recording
can wait for an in-flight pass to finish before the final transcription starts —
bounded by one pass, which is the ceiling in the table above. On CPU, pair live
transcription with the `fast` profile.

### Auto-transcribe

`stt.auto_transcribe` lets a run of silence act for you. It is independent of
live transcription; either works without the other.

- `off` (default) — silence never ends a recording.
- `stop` — silence ends the recording exactly as pressing the hotkey would:
  capture stops, the transcript is produced and delivered, and Murmly returns to
  idle.
- `continuous` — silence closes a segment, which is transcribed and delivered
  while capture keeps running for your next sentence. The session ends when you
  toggle, when a delivery is refused, or on error.

Silence only counts once speech has been detected in the current recording or
segment, so a muted microphone keeps Murmly listening instead of ending the
recording on an empty transcript. Detection uses the voice activity model bundled
with faster-whisper and needs a capture rate that is an integer multiple of
16 kHz; on other rates Murmly disables auto-transcribe for that session and says
so in `murmly doctor`.

**An auto-stopped recording delivers without printing.** The toggle that started
the recording already returned, so `murmly toggle` shows only the acknowledgement
that capture began. The transcript is still pasted and still lands on your
clipboard — it just never appears in command output.

In `continuous` mode a refused delivery ends the session rather than continuing to
record speech Murmly has just shown it cannot deliver.

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
/usr/bin/python3 src/murmly/overlay_renderer.py --check --backend wayland
```

Add `--backend x11` or `--backend wayland` to inspect a specific backend. The check
loads the layer-shell library the same way the running overlay does, so its report
is what the overlay would do. Restart the service after installing visual packages.

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
