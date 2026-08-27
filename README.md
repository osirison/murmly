---
title: murmly
description: Fedora-first local voice-to-text daemon for Linux desktops
---

## Overview

**[osirison.github.io/murmly](https://osirison.github.io/murmly/)** — what it is,
in one page with pictures. This file is the reference.

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
  - X11: `xclip` and `xdotool`
  - Wayland: `wl-clipboard`, plus a paste injector. Which one depends on the
    compositor, and Murmly picks by running each tool's own no-op invocation rather
    than by what is installed:
    - **KDE Plasma**: `xdotool`. KWin bridges XTEST through libei into its own input
      handling, so an X11 tool reaches Wayland-native windows. The first paste raises
      KDE's input-control dialog; tick **Always allow** and click Allow, and it is
      done permanently. That permission is revocable under System Settings →
      *Legacy X11 App Support*.
    - **wlroots compositors** (Sway, river, Hyprland): `wtype`, which uses the
      virtual keyboard protocol directly and needs no permission.
    - **Anything offering neither**: `ydotool`, which injects through `/dev/uinput`
      and is the only option here that needs one-time root setup (see below).

Until the KDE dialog is answered, the first transcript of a session reaches the
clipboard only: the events queue while the dialog is open, and `xdotool` has already
exited by the time it is answered. Murmly never restores the previous clipboard over a
transcript delivered this way, because `xdotool` exits 0 whether or not the keystroke
arrived, so a transcript is never lost to a paste that silently did not happen.

`murmly doctor` reports which method it would use under `paste_injection`, and prints
what to install when it has none.

### Pasting with ydotool

Only needed on a Wayland compositor that offers neither the virtual keyboard protocol
nor an XTEST bridge. `ydotoold` needs access to `/dev/uinput`, which is root-owned.
Rather than override the packaged system unit — its `ExecStart` cannot name your
runtime directory, because `/run/user/$UID` does not exist yet when the unit starts at
boot — grant your own user access and run the daemon as yourself:

```bash
sudo dnf install ydotool
echo 'KERNEL=="uinput", SUBSYSTEM=="misc", OPTIONS+="static_node=uinput", TAG+="uaccess"' \
  | sudo tee /etc/udev/rules.d/60-ydotool-uinput.rules
sudo udevadm control --reload && sudo udevadm trigger
```

Log out and back in, then run `ydotoold` as a user service. As your own process it
picks up `XDG_RUNTIME_DIR` and writes its socket exactly where the client looks for
it, with no flags and no socket-ownership juggling. This arrangement is documented
from the packaged binaries and udev's `uaccess` behaviour; it has not been run
end to end on a Murmly machine, because KDE and wlroots both have cheaper routes.

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
./setup.sh install Meta+X
```

One script covers installing, upgrading, and removing. It installs the system
packages for your session after showing you the `dnf` command and asking, syncs
the Python environment, binds the hotkey, and starts the service.

```bash
./setup.sh install Meta+X Meta+A   # also bind the speech-session hotkey
./setup.sh upgrade                 # pull, re-sync, rebind, restart
./setup.sh hooks                   # announce finished agent turns out loud
./setup.sh uninstall               # remove the service, hotkeys, and announcements
./setup.sh uninstall --purge       # and the environment, models, and configuration
```

It offers the GPU runtime when an NVIDIA driver is present and speech output
with its model files, and `--cuda`/`--no-cuda`/`--tts`/`--no-tts` decide either
without being asked. `--yes` answers every prompt, for an unattended run — which
includes the one confirming what `--purge` is about to delete, so those two
together remove the environment, the models, and your configuration without
asking. With nothing attached to the terminal and no `--yes`, every prompt is
declined rather than assumed.

What it exists to get right is the sync. `uv sync` makes the environment match
exactly the extras it is given, so a plain sync removes the CUDA wheels or
speech output; the GPU build of ONNX Runtime replaces the CPU one and every sync
puts the CPU one back. The script reads what is installed before each sync,
names all of it, and reapplies the swap afterwards, so an upgrade never removes
a feature you had.

### Installing by hand

```bash
uv sync
uv run murmly install Meta+X
```

To also bind the hotkey that dictates into an open speech session, pass a
second key:

```bash
uv run murmly install Meta+X Meta+A
```

That is the whole installation. It:

- writes a systemd user service that starts `murmly` with your graphical
  session and stops it at logout,
- registers `Meta+X` as a global hotkey, taking effect immediately, and
- refuses if a hotkey is already taken, naming the application that owns it —
  and refuses, having written nothing, when the key is Murmly's own other hotkey
  (name both keys in one command to move it) or when the same key was given for
  both.

For the GPU-backed runtime, sync the CUDA extra first so the same environment
carries it:

```bash
uv sync --extra cuda
uv run --extra cuda murmly install Meta+X
```

`uv sync` makes the environment match exactly the extras it is given, so name
every extra you want on one line. If speech output is already installed, this is
`uv sync --extra cuda --extra tts` — the shorter form would remove it.

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
| `~/.local/share/applications/net.local.murmly-session.desktop` | carries the speech-session hotkey, when one was requested |

Nothing else. Murmly never edits your global shortcut configuration, and
`murmly uninstall` removes every one of these files.

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

That rebinds the focused-window hotkey only; a speech-session hotkey keeps the
key it had. Name both to move both, and to swap them for each other:

```bash
uv run murmly install Meta+Shift+Space Meta+A
```

Asking for a key that Murmly's *other* binding currently holds is refused rather
than silently unbinding it — name both keys in one command instead.

Remove everything:

```bash
./setup.sh uninstall           # or: uv run murmly uninstall
./setup.sh uninstall --purge   # also the environment, models, and configuration
```

If you move the project directory or rebuild its environment, the recorded path
goes stale. Run `murmly install <hotkey>` again from the new location to repair
it; `murmly doctor` shows the path currently recorded. `./setup.sh upgrade` does
this for you, rebinding the keys it reads back rather than asking for them again.

## Speech output

Murmly can also speak. It is off by default, because a machine that starts
talking after an upgrade is producing sound its owner did not ask for.

An agent connects to the command socket, declares a speech session, and streams
text as it produces it. Murmly speaks that text a sentence at a time and tells
the session what the person actually heard. When the person reaches for a
capture hotkey, speech stops before the microphone opens, the session is told it
was interrupted and what was never spoken, and — if the session hotkey was the
one pressed — the person's reply is delivered back to that session rather than
pasted into whatever window has focus.

### Turning it on

```bash
uv sync --extra tts
sudo dnf install espeak-ng
```

`uv sync` makes the environment match exactly the extras it is given, so name
every extra you want on one line. On a CUDA install this is
`uv sync --extra cuda --extra tts` — the shorter form would remove the CUDA
wheels, exactly as the CUDA line alone would remove this one.

Then place the model files in `~/.local/share/murmly`:

| File | From |
| --- | --- |
| `kokoro-v1.0.onnx` | the Kokoro ONNX release |
| `voices-v1.0.bin` | the same release |

And set `enabled = true` under `[tts]` in your configuration. `murmly doctor`
reports what is missing under `speech_output` and names the remedy for each.

### Where synthesis runs

`[tts] device` chooses the processor synthesis runs on — `auto`, `cpu` or
`cuda`, the same vocabulary as `[stt] device` — and defaults to `cpu`. It is
synthesis's own setting: `[stt] device` is about transcription and does not
decide where speech is produced.

The CPU is the default because the GPU does not give back what synthesis takes
from it. Measured on one machine (RTX 3080 Laptop, 16 cores, reproduced across
two runs), still held after the synthesis session has been destroyed:

| `[tts] device` | system memory | GPU memory |
| --- | --- | --- |
| `cuda` | 876 MiB | 1208 MiB |
| `cpu` | 65 MiB | none |

The CPU path gives essentially all of its memory back when the session ends.
The CUDA path keeps its 876 MiB however hard it is collected or trimmed, so the
only way not to hold it is not to take it. What the CPU costs is about 200 ms
more before the first word. Nothing after that: synthesis runs at roughly five
times real time, so each sentence is finished between 1.0 and 3.4 seconds
before the audio ahead of it has played out, and every sentence past the first
is gapless. Same voice, same audio, same pacing, same failure handling.

Set `device = "cuda"` to run synthesis on the GPU, which also needs the GPU
build of ONNX Runtime installed as below. `auto` uses the GPU when it is usable
and the CPU when it is not. Either value is read as a preference rather than a
demand: with `cuda` set and no GPU build installed, synthesis falls back to the
CPU and logs the remedy instead of refusing sessions.

Synthesis read `[stt] device` before this setting existed, so the `cpu` default
moves speech output off the GPU on upgrade for some installs and leaves others
exactly where they were:

| `[tts] enabled` | `[stt] device` | GPU usable | moves to the CPU? |
| --- | --- | --- | --- |
| `false` (the default) | any | any | No — synthesis is never constructed |
| `true` | `cpu` | any | No — already on the CPU |
| `true` | `auto` | no | No — already falling back to the CPU |
| `true` | `cuda` | yes | Yes |
| `true` | `auto` | yes | Yes |

To keep what you had, set `[tts] device` to whatever `[stt] device` is set to,
and restart the daemon. That reproduces the previous resolution exactly,
because that is the value synthesis used to read: measured across two runs, it
holds the same 1208 MiB of GPU memory at the same warm latency as before, and
`CUDAExecutionProvider` is read back off the session.

The GPU build of ONNX Runtime **replaces** the CPU one rather than joining it —
both install into the same `onnxruntime` package namespace, and an environment
holding both leaves the survivor of any later uninstall broken:

```bash
uv sync --extra cuda --extra tts
uv pip uninstall onnxruntime
uv pip install "onnxruntime-gpu==1.24.4"
```

Both extras on the first line, every time. `uv sync` makes the environment match
exactly what it is given, so `uv sync --extra cuda` alone removes `kokoro-onnx`
and leaves speech output unavailable.

`murmly doctor` reports which execution providers speech output resolved, and
Murmly reads the provider back off the session it constructed rather than off
the module's advertised list — that list says CUDA on a session running on the
CPU. When synthesis falls back to the CPU because the GPU build is absent, the
providers it reports say so and the log names the remedy.

### Announcing a finished agent turn

Speech output is a protocol, so anything can drive it. Murmly ships one thing
that does: a Stop hook that speaks a summary when a coding agent finishes a
turn, so you can look away from the terminal and still know when it is done and
roughly what happened.

```bash
./setup.sh hooks            # offers whichever agents are installed
./setup.sh hooks claude     # or name one
./setup.sh hooks copilot
./setup.sh hooks off        # unregister
```

Claude Code and GitHub Copilot CLI both fire a `Stop` event at the end of a turn
and both hand it the same thing — a JSON payload naming a JSONL transcript — so
one script serves both. What differs is the row shape inside the transcript, and
the hook reads either.

What you hear, in order:

| | |
| --- | --- |
| three rising notes | so you know a message is arriving before any words start |
| one short sentence | the agent, the project, and the branch: "Claude Code in murmly, on branch main." |
| an executive summary | whole sentences from the end of the agent's message, capped at about twenty seconds |

Code fences, tables, links, and headings are stripped before anything is spoken;
they read as noise out loud. The summary is extractive — it is the agent's own
opening sentences, not a second model's paraphrase — so nothing is invented and
no tokens are spent producing it.

The hook stays silent, and exits 0, for every ordinary reason it cannot speak:
speech output disabled or unavailable, another client already holding the
session, a capture running, or nothing worth saying in the last message. It
never fails a turn. It detaches into its own process before speaking, so an
announcement never holds the agent up.

| Variable | Effect |
| --- | --- |
| `MURMLY_ANNOUNCE_CHIME=0` | speak without the notes |
| `MURMLY_ANNOUNCE_LOG=<path>` | append a line per turn explaining what it did or why it stayed quiet |
| `MURMLY_ANNOUNCE_AGENT=<name>` | name the agent yourself rather than inferring it from the transcript |

The notes are generated rather than shipped. To use your own, put a WAV at
`~/.local/share/murmly/announce-chime.wav` and it is played instead. They go to
the default output device through `pw-play`, `paplay`, or `aplay` — whichever is
present — rather than through `[tts] output_device`, which is the speech path.

Registration is written to `~/.claude/settings.json` (merged, with the previous
file kept beside it as `settings.json.murmly-backup`) and to
`~/.copilot/hooks/murmly-announce.json` (a file of its own). Running it twice
leaves one registration, and `./setup.sh uninstall` takes both out.

### The two hotkeys

| Hotkey | What it does |
| --- | --- |
| the first, for example `Meta+X` | transcribes into the focused window, exactly as it always has |
| the second, for example `Meta+A` | transcribes into the open speech session, pasting nothing. With no session open — or one that closed before the transcript was ready — the transcript goes to the clipboard instead, overwriting what was there, and is reported as undelivered |

Both stop speech before the microphone opens, and both tell the session it was
interrupted, because a sender has to stop generating whoever the person was
talking to. They differ only in where the transcript goes. Installing without a
second hotkey leaves speech output reachable by a sender that opens a session
itself; `murmly doctor` reports the session hotkey as not bound, which is not a
failure.

### The session protocol

Nothing in the CLI opens a speech session. `murmly toggle-session` routes a
transcript to one that is already open; the session itself is opened by a client
connecting to the command socket, which is what an agent is. To try speech output
by hand, write that client — the protocol below is all of it.

One connection, newline-delimited JSON in both directions. Declare the session
and wait for the acknowledgement before sending anything else:

```json
{"command": "speech_session"}
```

Murmly answers `{"ok": true, "session": "speech"}`, or one refusal frame and a
closed connection. `speech_disabled`, `speech_unavailable` and
`speech_session_in_use` are the reasons specific to speech; a declaration can
also be refused for the reasons any command can, such as `command_failed`,
`over_capacity` or `shutting_down`, and with `busy` while a capture is running —
accepting one then would reopen the loudspeaker into a live microphone. `busy` is
transient: retry once capture ends. Treat any frame carrying `"ok": false` as a
refusal and report its message, rather than matching the speech reasons alone.
One session is open at a time.

Refusals also arrive **inside** an open session. A frame that is not JSON, is not
an object, or names a command Murmly does not know is answered with
`{"ok": false, "code": ..., "error": ...}` and the session stays open, so a
sender that dispatches on `frame["event"]` alone will meet a frame that has no
`event` key. Branch on whichever of `event` and `ok` the frame carries.

Frames a sender may send:

| Frame | Meaning |
| --- | --- |
| `{"command": "speak", "name": "m1", "text": "..."}` | speak this, and call it `m1`. `name` must be a non-empty string, and one frame must stay under 65536 bytes — a larger one is refused with `malformed_request` and the session is closed, so split a long passage across several frames |
| `{"command": "end"}` | no more text is coming |
| `{"command": "cancel"}` | stop speaking and discard what is queued |

Frames Murmly sends, without being asked:

| Frame | Meaning |
| --- | --- |
| `{"event": "started", "name": "m1"}` | `m1` has begun to be audible |
| `{"event": "heard_all"}` | everything queued was heard, and the sender had said it was finished |
| `{"event": "interrupted", "playing": "m2", "pending": ["m3"], "code": "speech_interrupted"}` | speech stopped: `m2` was cut off and `m3` never started. Sent when the person presses a capture hotkey **and** in answer to the sender's own `cancel`, so a sender that stops generating on this event must not mistake the echo of its own cancel for a barge-in. `playing` is null when nothing was audible |
| `{"event": "transcript", "text": "..."}` | what the person said, when the session hotkey started the capture |
| `{"event": "failed", "name": "m4", "error": "..."}` | `m4` could not be produced; the session continues. `name` is null when the failure is the output device itself rather than a named piece of text |
| `{"event": "shutting_down"}` | Murmly is stopping |

Four things a sender should know:

- **Wait for the acknowledgement.** The declaration is read by the same path
  that reads every other command, which reads one frame; text pipelined behind
  the declaration in a single write arrives as one unreadable request.
- **The position reported is what was heard, not what was produced.** Murmly
  produces sentence five while sentence four is audible, so a position taken
  from production would claim the person heard something they did not.
- **Events carry names, never text.** The one exception is the transcript, which
  is the whole point of delivering it.
- **Read continuously.** Events queue per session and never hold up playback, so
  a sender that stops reading is disconnected once 64 frames are outstanding —
  with no refusal frame, just a closed connection.

### What speech output does not do

Reading a highlighted selection aloud, voice-activated barge-in, a visual
indicator while speaking, voice cloning, and languages other than English are
all out of scope. Interruption is a keypress, which is what makes echo
cancellation unnecessary: playback and capture never overlap.

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
# Defaults to $XDG_RUNTIME_DIR/murmly.sock. No directory on the path may be
# writable by group or other; see "The command socket" below.
# socket_path = "/run/user/1000/murmly.sock"

[audio]
sample_rate_hz = 16000 # Preferred capture rate
channels = 1

[stt]
model_profile = "balanced" # fast | balanced | accurate
device = "auto" # auto | cpu | cuda
compute_type = "auto"
lazy_load_model = true
unload_after_idle_s = 300      # Release the model's GPU memory after this idle time, 30-86400; 0 never
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

[tts]
enabled = false        # Speech output; see "Speech output" below
voice = "af_heart"     # An English voice the model carries
rate = 100             # Percentage of the model's own speaking rate, 50-200
device = "cpu"         # auto | cpu | cuda, independent of [stt] device
unload_after_idle_s = 0        # Drop the synthesis session after this idle time; 0 never
output_device = ""     # Empty lets the system choose
# model_dir = "~/.local/share/murmly"   # Where the model and voices are
```

### The command socket

`murmly toggle`, `murmly toggle-session` and `murmly status` reach the daemon
over a UNIX socket at
`daemon.socket_path`. That socket starts and stops the microphone, so it is
restricted to the account the daemon runs as: the socket is created `0600`, any
directory Murmly creates for it is `0700`, and a connection whose reported
account differs from the daemon's is refused.

The default path is under `$XDG_RUNTIME_DIR`, which is already private, and
needs no action. If you set `daemon.socket_path` yourself, no directory a lookup
of it passes through may be writable by group or other, and every one of them
must be owned by you or by root. That covers the whole path, not just the
directory holding the socket, and it follows any symbolic link on the way:
renaming a directory replaces everything under it, replacing a link redirects
everything reached through it, and an owner can grant itself write access at any
time. The daemon refuses to start otherwise, because such a directory lets
another account create or replace the socket node, and your own `murmly toggle`
would then reach a socket Murmly does not serve. The refusal names the directory
at fault. Either move the socket back under `$XDG_RUNTIME_DIR`, or correct that
directory — `chmod go-w` for a writable one, and for one another account owns
there is nothing to chmod, so move the socket:

```bash
chmod go-w /path/to/the/directory/it/named
```

A directory other accounts can only read or traverse is fine, because the socket
node itself is owner-only and connecting to a UNIX socket requires write
permission on it. A shared directory with the sticky bit set — `/tmp` and the
like, where one account cannot remove another's entries — is fine above the
deepest directory on the path that already exists, though not as that directory
itself. So `/tmp/murmly-yours/murmly.sock` is served once you have created
`murmly-yours` as `0700`, and refused while it is still missing: until it exists,
anyone can create it first. `murmly doctor` reports the condition under
`command_socket` without refusing to run, so you can check the state before
restarting the service.

Profile mapping:

- `fast` -> `tiny.en`
- `balanced` -> `large-v3-turbo`
- `accurate` -> `large-v3`

With `[stt] device = "auto"`, Murmly uses CUDA `float16` when a compatible GPU
and the CUDA extra are available, and falls back to CPU `int8` otherwise. The
first use of a profile downloads its model; later sessions reuse the local
cache. The tested CUDA runtime wheels are about 1.8 GB and the cached
`large-v3-turbo` model about 1.6 GB. The balanced model revision is pinned for
reproducible downloads.

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

### Releasing idle model memory

Murmly loads the transcription model on the first recording — or at startup, if
you set `lazy_load_model = false` above — and the synthesis session on the first
speech, and each then sits in memory doing nothing between uses. `[stt] unload_after_idle_s` and `[tts] unload_after_idle_s` hand that
memory back once a model has gone unused for its own idle period, in seconds,
and Murmly loads it again when it is next needed. Each is bounded 30-86400.
`0` switches release off for that model, leaving it resident once loaded. A
value outside the bounds falls back to that setting's own default rather than
refusing to start — and to its own, not to a shared one, because the two
defaults differ.

Measured on one machine (RTX 3080 Laptop, 16 cores, reproduced across two runs):

| Releasing | Returns | Costs |
| --- | --- | --- |
| Transcription | 2080 MiB of GPU memory | 0.78 s to reload |
| Synthesis, `[tts] device = "cpu"` — the default | 377 MiB of system memory, no GPU memory | 759-767 ms before speech resumes |
| Synthesis, `[tts] device = "cuda"` | 528 MiB of GPU memory, 105 MiB of system memory | 607-611 ms before speech resumes |

**Transcription release is enabled by default, at 300 seconds**, because its
cost is paid where nobody is waiting. Murmly starts the reload when capture
begins rather than when a transcript is needed, so the 0.78 s runs while you are
still speaking and is over before you stop. It returns accelerator memory, which
is the resource another process is most likely to be short of.

**Synthesis release is disabled by default**, because its cost is silence. There
is nothing to overlap the rebuild with: the wait falls between the moment
something asks Murmly to speak and the moment it does. Under the default
`[tts] device = "cpu"` it also returns system memory rather than accelerator
memory. Speech output is opt-in already, so releasing it is too. Set
`[tts] unload_after_idle_s` to a period in seconds to turn it on.

Idle means no capture is active, not "no recent transcript". The countdown
starts when a recording session ends and is abandoned when the next one begins,
so a `continuous` session is never released while it is still running, however
long you pause between utterances. Synthesis counts the same way against speech
sessions.

`[tts] device = "cuda"` together with a non-zero `[tts] unload_after_idle_s` is
the one combination to think twice about. It is a trade rather than a saving:
the 528 MiB of GPU memory comes back, but rebuilding the session costs a
one-time 277 MiB of system memory and then roughly 8 MiB on every release cycle
after it, which a daemon cycling twenty times a day feels. Neither shipped
default puts you there.

**This changes behaviour on upgrade.** An install that configures neither
setting begins releasing the transcription model after five idle minutes. No
transcript changes and, in ordinary dictation, there is nothing to wait for.
`[stt] unload_after_idle_s = 0` restores the always-resident behaviour Murmly
had before. Synthesis is unaffected, because its default is `0`.

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
| Wayland with `wtype` or `ydotool` | no | yes |
| Wayland with `xdotool`, which is the KDE Plasma path | no | no |

`murmly doctor` reports which applies under `delivery`.

### Restoring your previous clipboard

Murmly never restores over a transcript it cannot prove was delivered. `xdotool`
on Wayland exits 0 whether or not the keystroke reached the window, so on that
path the previous clipboard is not read and not put back, whatever
`clipboard.restore` says — an undelivered transcript is the only copy of what you
said, and restoring over it would destroy it. `murmly doctor` reports
`paste_injection.confirms_delivery`.

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

**A hotkey does nothing.** Check `murmly doctor`. `installation.hotkey_held` and
`installation.hotkey_holder` describe the focused-window hotkey only;
`installation.hotkeys` lists every binding with its `purpose`, `hotkey`, `held`
and `holder`, so read the entry whose purpose is `session` for the speech one. An
application holding a key is named there. Pressing the hotkey
also starts the service if it is installed but not running, so a hotkey that does
nothing at all usually means the binding, not the daemon.

**Speech output says nothing.** Check `speech_output` in `murmly doctor`:
`available` is false with a `detail` naming what is missing, and
`output_device_in_use` names the device it would play through. If a client's
`speech_session` declaration is answered `unsupported_command`, the running
daemon predates speech output — restart it with `systemctl --user restart
murmly.service`. `speech_session_in_use` means another client already has the
session; only one is open at a time.

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

Run the test suite:

```bash
uv run --no-sync python -m unittest discover -s tests
```

The suite is stdlib `unittest` with no external test dependencies. Tests that
need a live desktop session skip themselves when it is unavailable. `--no-sync`
matters: `uv run` otherwise syncs first, and because the CPU build of
`onnxruntime` arrives as a dependency of `faster-whisper`, the sync reinstalls it
over the GPU build on a machine that has had the swap applied.

Behavioral changes are planned with OpenSpec; see `openspec/specs/` for the
current capability baseline. Operational preconditions that are not documented
elsewhere live in [docs/agent-notes/](docs/agent-notes/).
