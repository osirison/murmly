# Making murmly speak

Murmly can also speak. It is off by default, because a machine that starts
talking after an upgrade is producing sound its owner did not ask for.

## What a speech session is

A program connects to murmly and starts a speech session, then sends it text
as it produces it. Murmly speaks that text a sentence at a time and tells the
program what the person actually heard.

When you reach for a capture hotkey, speech stops before the microphone
opens. The program is told it was interrupted and what was never spoken. If
the second hotkey — the one bound to speech sessions — was the one you
pressed, your reply is delivered back to that program rather than pasted into
whatever window has focus.

The program on the other end is usually a coding assistant. Murmly ships one
ready-made connection for this — see
[Hearing when your coding assistant finishes](announcements.md).

## Turning it on

You need murmly installed already: Linux or Windows, Python 3.12 or newer, and
a terminal, on either one. On Linux, KDE Plasma X11 was verified end to end
before the port to Windows and not re-verified on X11 since;
Plasma Wayland and GNOME are not, and any other desktop registers no hotkey
and shows no overlay automatically — though everything below still installs
and works regardless of desktop. On Windows, the hotkey is always automatic
and the overlay needs its own `uv sync --extra overlay`. Speech output itself
needs no permission on either platform, granted or otherwise: it is separate
from the microphone and paste permissions capture already needs. See [What
you need before you start](what-you-need.md) and [installing
murmly](install.md) for the full detail behind capture's own requirements.

```bash
uv sync
sudo dnf install espeak-ng   # Linux; adjust the package manager for your distribution
```

On Windows, no separate espeak-ng install is needed: the phonemizer's own
bundled library works there directly, unlike on Linux, where the system
package above is what murmly actually loads.

The synthesizer is installed by default. It is a dependency group named in
`[tool.uv] default-groups`, so no sync drops it by not mentioning it, and on
a CUDA install `uv sync --extra cuda` keeps it too. To leave it out, ask:
`uv sync --no-group tts`.

What is **not** installed by default is the 340 MB of model files, which is
the part worth deciding about.

Place the model files in `~/.local/share/murmly` on Linux, or
`%LOCALAPPDATA%\murmly` on Windows:

| File | From |
| --- | --- |
| `kokoro-v1.0.onnx` | the Kokoro ONNX release |
| `voices-v1.0.bin` | the same release |

Then set `enabled = true` under `[tts]` in your configuration — see
[the setting](settings.md#tts-enabled).

`murmly doctor` reports what is missing under `speech_output` and names the
remedy for each.

## The two hotkeys

| Hotkey | What it does |
| --- | --- |
| the first, for example `Meta+X` | transcribes into the focused window, exactly as it always has |
| the second, for example `Meta+A` | transcribes into the open speech session, pasting nothing. With no session open — or one that closed before the transcript was ready — the transcript goes to the clipboard instead, overwriting what was there, and is reported as undelivered |

Both hotkeys stop speech before the microphone opens, and both tell the
program's session that it was interrupted, because a sender has to stop
generating whoever the person was talking to. They differ only in where the
transcript goes.

Installing without a second hotkey still leaves speech output reachable, by
a program that opens a session itself. In that case `murmly doctor` reports
the session hotkey as not bound, which is not a failure — it is only telling
you that pasting a reply straight back into a session is not set up.

## What it does not do

Speech output does not read a highlighted selection aloud, does not listen
for a voice to interrupt it (barge-in), shows no visual indicator while
speaking, does not clone a voice, and speaks nothing but English.

Interruption is a keypress, which is what makes echo cancellation
unnecessary: playback and capture never overlap.

## Where to next

- [Speed, memory, and your graphics card](speed-and-memory.md) — where
  speech is produced and what it costs in memory.
- [Hearing when your coding assistant finishes](announcements.md) — the
  announcements murmly ships for coding assistants.
- [All the settings](settings.md) — including `tts.voice`, `tts.rate`, and
  `tts.output_device`.
- [For developers](for-developers.md) — the protocol a program uses to drive
  a speech session.
