# All the settings

Murmly runs on its defaults, so you never have to configure it. This page
exists for when you want to change one thing.

## Where the file lives

Murmly reads its settings from `~/.config/murmly/config.toml`, or from
`$XDG_CONFIG_HOME/murmly/config.toml` when you have that variable set. If you
are ever unsure which one is in effect, [`murmly doctor`](troubleshooting.md)
reports the path it is actually using.

The easiest way to start is to copy the annotated example that ships with
murmly into place:

```bash
mkdir -p ~/.config/murmly
cp config.example.toml ~/.config/murmly/config.toml
```

Every option in that example file is already written at its default value, so
copying it in changes nothing about how murmly behaves — nothing takes effect
until you edit a line yourself.

!!! note
    A value you set outside the range documented for it does not stop murmly
    from starting. It falls back to that setting's own default instead, and
    the murmly service keeps running normally.

## The whole file, at its defaults

Every key below, together, exactly as it reads in the example file. Copy any
part of it, or all of it — nothing here changes murmly's behaviour until you
edit a value.

Two of its comments say "see ... below". They were written when all of this
was one long file: *The command socket* is now on
[for developers](for-developers.md), and *Speech output* is now
[making murmly speak](making-murmly-speak.md). The comments are left exactly
as they are, because this is a file you copy.

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

## `[daemon]` — the background service

This section controls the part of murmly that keeps running in the
background — the murmly service — rather than any one recording.

### `daemon.socket_path` { #daemon-socket-path }

Not set by default; commented out in the example. The service defaults to
`$XDG_RUNTIME_DIR/murmly.sock`. Setting your own path brings permission rules
with it — no directory on the path may be writable by group or other — covered
in full on [for developers](for-developers.md).

## `[audio]` — capturing sound

### `audio.sample_rate_hz` { #audio-sample-rate-hz }

Default `16000`. The capture rate murmly asks the
microphone for.

### `audio.channels` { #audio-channels }

Default `1`. How many audio channels murmly records.

## `[stt]` — turning speech into text

### `stt.model_profile` { #stt-model-profile }

Default `"balanced"`. Permitted words: `fast | balanced | accurate`. Chooses
which transcription model murmly uses — a trade between speed, memory, and
accuracy explained in full on
[speed, memory, and your graphics card](speed-and-memory.md).

### `stt.device` { #stt-device }

Default `"auto"`. Permitted words: `auto | cpu | cuda`. Chooses the processor
transcription runs on. See
[speed, memory, and your graphics card](speed-and-memory.md) for how `auto`
resolves.

### `stt.compute_type` { #stt-compute-type }

Default `"auto"`. Left alongside `stt.device`; see
[speed, memory, and your graphics card](speed-and-memory.md).

### `stt.lazy_load_model` { #stt-lazy-load-model }

Default `true`. With `true`, the transcription model
waits for your first recording before it loads; set to `false` and it loads
as soon as the service starts. More on model loading is on
[speed, memory, and your graphics card](speed-and-memory.md).

### `stt.unload_after_idle_s` { #stt-unload-after-idle-s }

Default `300`. Range `30-86400`; `0` never releases the model. Releases the
transcription model's memory after this many idle seconds. Fully explained,
with what it costs and returns, on
[speed, memory, and your graphics card](speed-and-memory.md).

Two more keys live in this section but are commented out by default, and
follow whichever profile you chose unless you set them yourself:

### `stt.beam_size` { #stt-beam-size }

Not set by default; commented out in the example. Its own default is `5`.
Follows the profile unless you pin it.

### `stt.vad_filter` { #stt-vad-filter }

Not set by default; commented out in the example. Its own default is `true`.
Follows the profile unless you pin it.

### `stt.live_transcribe` { #stt-live-transcribe }

Default `false`. Shows partial transcripts while you
speak. Explained in full on
[seeing your words as you speak](words-as-you-speak.md).

### `stt.live_interval_ms` { #stt-live-interval-ms }

Default `1000`. Range `250-10000`. How often a partial transcript is
produced. See
[seeing your words as you speak](words-as-you-speak.md).

### `stt.live_window_seconds` { #stt-live-window-seconds }

Default `15`. Range `5-60`. How much trailing audio each partial re-reads.
See [seeing your words as you speak](words-as-you-speak.md).

### `stt.auto_transcribe` { #stt-auto-transcribe }

Default `"off"`. Permitted words: `off | stop | continuous`. Lets a pause in
your speech end the recording instead of pressing the key again. Explained in
full on [finishing a recording by pausing](pause-to-finish.md).

### `stt.auto_transcribe_silence_ms` { #stt-auto-transcribe-silence-ms }

Default `2000`. Range `250-30000`. How much silence triggers auto-transcribe.
See [finishing a recording by pausing](pause-to-finish.md).

### `stt.auto_transcribe_min_speech_ms` { #stt-auto-transcribe-min-speech-ms }

Default `300`. Range `0-10000`. How much speech is required before silence
counts toward triggering auto-transcribe. See
[finishing a recording by pausing](pause-to-finish.md).

## `[clipboard]` — pasting what you said

### `clipboard.restore` { #clipboard-restore }

Default `true`. Whether murmly restores whatever was on
your clipboard before it pasted your transcript. Explained in full on
[where your words go](where-your-words-go.md).

### `clipboard.restore_delay_ms` { #clipboard-restore-delay-ms }

Default `500`. Range `0-5000`. How long murmly waits before restoring the
previous clipboard contents. See
[where your words go](where-your-words-go.md).

### `clipboard.verify_target` { #clipboard-verify-target }

Default `true`. Refuses to paste if focus left the
window you dictated into. See [where your words go](where-your-words-go.md).

## `[overlay]` — the on-screen indicator

### `overlay.enabled` { #overlay-enabled }

Default `true`. Whether the small on-screen indicator
appears while you record.

### `overlay.bottom_margin_px` { #overlay-bottom-margin-px }

Default `32`. Range `0-512`. How many logical pixels the indicator sits above
the bottom edge of your display.

### `overlay.reduced_motion` { #overlay-reduced-motion }

Default `false`. Turns off the indicator's animation.

### `overlay.text_size_px` { #overlay-text-size-px }

Default `13`. Range `8-48`. The text size of the transcript panel shown in
the indicator.

## `[tts]` — speaking back to you

### `tts.enabled` { #tts-enabled }

Default `false`. Turns speech output on. Explained in
full on [making murmly speak](making-murmly-speak.md).

### `tts.voice` { #tts-voice }

Default `"af_heart"`. An English voice the speech model
carries. See [making murmly speak](making-murmly-speak.md).

### `tts.rate` { #tts-rate }

Default `100`. Range `50-200`. A percentage of the voice model's own speaking
rate. See [making murmly speak](making-murmly-speak.md).

### `tts.device` { #tts-device }

Default `"cpu"`. Permitted words: `auto | cpu | cuda`, independent of
`stt.device`. Chooses the processor speech output runs on. Fully explained,
with the memory and speed trade-off, on
[speed, memory, and your graphics card](speed-and-memory.md).

### `tts.unload_after_idle_s` { #tts-unload-after-idle-s }

Default `0`. Range `30-86400`; `0` never releases the synthesis session.
Releases speech output's memory after this many idle seconds. Fully
explained, with what it costs and returns, on
[speed, memory, and your graphics card](speed-and-memory.md).

### `tts.output_device` { #tts-output-device }

Default `""`. An empty value lets the system choose the
audio output device. See [making murmly speak](making-murmly-speak.md).

### `tts.model_dir` { #tts-model-dir }

Not set by default; commented out in the example. Its own default is
`~/.local/share/murmly`. Where the speech model and its voices are stored.

## After you change something

Restart the service after changing configuration:

```bash
systemctl --user restart murmly.service
```

For the details behind any one setting above, see
[speed, memory, and your graphics card](speed-and-memory.md) for the model
profile and memory settings, or [when something goes wrong](troubleshooting.md)
if a change did not take effect the way you expected.
