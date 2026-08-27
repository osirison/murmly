---
title: Capturing the recording overlay for documentation, on Wayland, without a microphone
description: A full-compositor Spectacle capture on Plasma Wayland does include the layer-shell overlay; speech is fed in through a PipeWire null sink; and the processing presentation has to be held open by OverlayController because it cannot be caught from a live recording
trigger: spectacle, spectacle -b -f -n -o, overlay screenshot, screenshot the overlay, OverlayState.THINKING, OverlayController, pactl load-module module-null-sink, pactl move-source-output, pw-play, murmly toggle

depends_on: src/murmly/overlay.py, src/murmly/overlay_renderer.py, src/murmly/daemon.py, site/assets/overlay-listening.png, site/assets/overlay-partial.png, site/assets/overlay-processing.png
recorded: 2026-08-27
verified_on: Plasma 6.7.4 Wayland, KWin 6.7.4, Fedora 44, Spectacle 6.7.4, PipeWire 1.6.8
---

# Capturing the recording overlay for documentation

Three separate things have to be got right, and two of them are not what you
would guess. See also `wayland-overlay-preload-and-paste.md`, which covers
running the renderer by hand rather than photographing it.

## No X11 session is needed

**Symptom:** a plan that says to log out of Wayland and into X11 before taking
overlay screenshots, on the grounds that the overlay is a `gtk4-layer-shell`
surface a screenshot tool may not include.

**Fix:** stay on Wayland and take a *full-compositor* capture.

```bash
spectacle -b -f -n -o /path/out.png     # background, fullscreen, no notify
```

This runs without a GUI prompt, takes about 0.6 s, and the overlay is in the
image. It is a *region* capture that can miss a layer-shell surface, not a
whole-output one. Crop afterwards with ImageMagick.

**Why it was not obvious:** the layer-shell caveat is real, so the conclusion
"therefore use X11" looks sound. It is worth thirty seconds to test rather than
an hour to act on: toggle the overlay on, capture, look at the file.

## Feeding speech in without speaking

**Symptom:** you need the waveform responding and a live partial transcript, and
either there is nobody to talk or the room is not quiet.

**Fix:** synthesise a line, then move *murmly's own capture stream* onto a null
sink. Do not change the system default source — the daemon has already resolved
its device, and moving the one stream is both more reliable and less invasive.

```bash
pactl load-module module-null-sink sink_name=murmlyshot     # returns a module id
murmly toggle                                               # opens the capture stream
SO=$(pactl list short source-outputs | awk '$0 ~ /1ch 16000Hz/ {print $1; exit}')
pactl move-source-output "$SO" murmlyshot.monitor
pw-play --target=murmlyshot line.wav
murmly toggle
pactl unload-module <module id>                             # always
```

`1ch 16000Hz` is what identifies murmly's stream among the source-outputs.
Synthesise `line.wav` with the Kokoro model already in
`~/.local/share/murmly` if speech output is installed.

**Point the paste somewhere safe first.** Stopping a capture types the transcript
into whatever holds focus, and on Wayland `verify_target` cannot help —
`murmly doctor` reports `verification_supported: false`, because it needs X11.
Open a scratch editor and let it take focus before toggling, or the transcript
lands in whatever you were doing.

## The processing presentation cannot be caught from a live recording

**Symptom:** every capture taken after `murmly toggle` shows no overlay at all.
The transcript is already pasted by the first frame.

**Fix:** drive `OverlayController` the way `daemon.py` drives it and hold the
state open.

```python
import sys, time
sys.path.insert(0, "<repo>/src")
from murmly.overlay import OverlayController, OverlayState

c = OverlayController(bottom_margin_px=50, reduced_motion=False,
                      text_size_px=15, transcript_panel=False)
c.start()
time.sleep(2.0)                      # the helper process has to come up first
c.publish_state(OverlayState.THINKING)
time.sleep(15)                       # capture during this
c.close()
```

`health` is a property, not a method. This is a real capture of the shipped
renderer showing the real state, not a mockup: the daemon does nothing to that
surface except tell it which state to display.

**Why it was not obvious:** the state is published only after capture stops, and
the transcription that follows finishes in well under one capture's latency —
13 s of audio, and still under 0.6 s. Four approaches failed before this one:
capturing in a tight loop, staggering concurrent captures, forcing transcription
onto the CPU with `[stt] device = "cpu"`, and stretching paste handling with
`restore_delay_ms = 5000`. None of them widened the window enough. Do not spend
time there.

## Do not photograph the installed daemon's `doctor`

A venv installed editable (`uv sync` / `pip install -e`) against the main
checkout is often sitting on a feature branch, so its `murmly doctor` reports
fields that `main` does not have. Run the worktree's own code, under a neutral
`HOME` so no personal path is in frame:

```bash
HOME=/home/user PYTHONPATH=<worktree>/src <venv>/bin/python -m murmly doctor
```

Check `git -C <main checkout> rev-parse --abbrev-ref HEAD` before trusting
anything the installed daemon prints about itself. The checkout is the one the
`__editable__.murmly-*.pth` file in the venv's `site-packages` points at.
