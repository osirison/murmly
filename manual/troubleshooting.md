# When something goes wrong

## Start here

Start with the diagnostics, which report the session, clipboard tools, model
runtime, delivery verification, overlay, and installation state:

```bash
uv run murmly doctor
```

`murmly doctor` opens the command socket, which is how it talks to the part of
murmly that keeps running in the background — the murmly service. Because of
that, the model residency it reports comes from the running service rather than
from the reporting process itself. Everything else in the report is about this
machine and is unaffected by whether the service answers.

With live transcription enabled, the report also measures a partial pass, which
loads the transcription model in the reporting process — the `murmly doctor`
command itself, not the service. `live_transcription.partial_pass_loaded_model`
says when that partial pass happened, and the residency reported above it was
read from the service before that pass ran.

## Reading the logs

The service's own logs go through systemd:

```bash
systemctl --user status murmly.service
journalctl --user -u murmly.service -b
```

## Nothing happens when I press my hotkey

Check `murmly doctor`. `installation.hotkey_held` and `installation.hotkey_holder`
describe the focused-window hotkey only; `installation.hotkeys` lists every
binding with its `purpose`, `hotkey`, `held`, and `holder`, so read the entry
whose `purpose` is `session` for the speech hotkey. If another application is
holding the key, it is named there.

Pressing the hotkey also starts the service if it is installed but not running,
so a hotkey that does nothing at all usually means a problem with the binding
itself, not with the service.

If you need to change which key is bound, see [changing your
hotkey](changing-your-hotkey.md).

## Murmly says nothing when it should speak

Check `speech_output` in `murmly doctor`. If `available` is `false`, it comes
with a `detail` naming what is missing. `output_device_in_use` names the device
speech would play through.

If a client's `speech_session` declaration is answered `unsupported_command`,
the running service predates speech output — restart it with `systemctl --user
restart murmly.service`.

`speech_session_in_use` means another client already has the session open;
only one session is open at a time.

For how to turn speech output on and use it, see [making murmly
speak](making-murmly-speak.md).

## The overlay does not appear

Inspect the same system-Python imports the overlay renderer uses:

```bash
/usr/bin/python3 src/murmly/overlay_renderer.py --check
/usr/bin/python3 src/murmly/overlay_renderer.py --check --backend wayland
```

Add `--backend x11` or `--backend wayland` to inspect one specific backend. The
check loads the layer-shell library the same way the running overlay does, so
its report is what the overlay would actually do.

If the overlay's packages were missing and you have just installed them — see
[installing murmly](install.md) for the list — restart the service afterwards
so it picks them up.

## My recordings are silent, on an Intel laptop

If murmly keeps transcribing nothing but the single word "you," over and over,
or a recording comes back completely silent — even though your system's sound
settings show the right microphone selected and not muted — the cause is
usually specific to Intel laptops.

Many Intel laptops use a type of audio hardware called SOF. On these machines,
the desktop's sound panel and the lower-level audio driver underneath it can
disagree with each other: the panel shows the microphone at full volume and
unmuted, while the driver's own capture switch for that microphone is still
off. Recording then appears to succeed — nothing reports an error — but every
buffer it captures is nothing but silence.

First, list your sound cards and find the number of the SOF one:

```bash
arecord -l
```

Then, substituting that number for the `1` below, check its capture switch:

```bash
amixer -c 1 scontrols | grep -i dmic
amixer -c 1 sget Dmic0
```

If the `Dmic0` capture channels read `[off]`, turn capture on:

```bash
amixer -c 1 sset Dmic0 cap
```

To confirm this fixed it, record a few seconds of speech and look at the
levels rather than just listening back:

```bash
pw-record --rate 16000 --channels 1 --format s16 --sample-count 128000 \
  /tmp/murmly-diagnostic.wav
ffmpeg -hide_banner -i /tmp/murmly-diagnostic.wav \
  -af astats=metadata=1:reset=0 -f null -
```

A recording with speech in it reports finite peak and RMS levels. A silent one
reports `-inf` for both.

## Nothing here matches

- A transcript that landed on the clipboard instead of in your window — see
  [where your words go](where-your-words-go.md).
- murmly running slower, or using more memory, than you expected — see
  [speed, memory, and your graphics card](speed-and-memory.md).
- Anything you would like to change about how murmly behaves — see [all the
  settings](settings.md).
