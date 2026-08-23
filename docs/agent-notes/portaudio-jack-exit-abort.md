---
title: Test a PortAudio exit crash against a private PipeWire, never the session's own
description: How to reproduce the SIGABRT in PortAudio's JACK teardown on demand, and why murmly's daemon unregisters sounddevice's atexit hook
trigger: sounddevice, Pa_Terminate, pa_jack.c, systemctl --user stop murmly, murmly daemon, coredumpctl, leave_without_finalizing, os._exit, pw-PortAudio

depends_on: src/murmly/audio.py, src/murmly/cli.py, src/murmly/installer.py
recorded: 2026-08-22
updated: 2026-08-23
verified_on: Fedora 44, portaudio 19.7.0-3.fc44, pipewire-jack-audio-connection-kit 1.6.8-1.fc44, sounddevice 0.5.5
---

## Symptom

A process that uses `sounddevice` aborts as it exits, and systemd records it as a
crash rather than a stop:

```text
python3: src/hostapi/jack/pa_jack.c:867: Terminate: Assertion `err == 0' failed.
murmly.service: Main process exited, code=dumped, status=6/ABRT
murmly.service: Failed with result 'core-dump'.
```

The unit is then stuck in `failed` until `systemctl --user reset-failed`, and each
occurrence leaves a core dump behind — around 178 MB for the daemon with its models
resident.

## Cause

`import sounddevice` runs `Pa_Initialize()` and registers `_exit_handler` with
`atexit` (`sounddevice.py:2971-2972`). At exit that calls `Pa_Terminate()`, which
tears down *every* host API PortAudio initialised, not only the one a stream was
opened on.

On Fedora, `/etc/ld.so.conf.d/pipewire-jack-x86_64.conf` puts PipeWire's
`libjack.so.0` on the loader path, so PortAudio's JACK host API always initialises
and holds a JACK client for the life of the process. `pa_jack.c:867` is:

```c
ASSERT_CALL( jack_deactivate( jackHostApi->jack_client ), 0 );
```

`jack_deactivate()` fails once the server behind libjack is gone, and `ASSERT_CALL`
aborts. So any exit that happens after PipeWire has stopped is a crash — which is
what a logout is, since nothing orders a user service's stop before PipeWire's.

Nothing about this depends on having recorded. A process that only imports
`sounddevice` aborts the same way.

## Reproducing it without touching your own audio

Do **not** kill the session's PipeWire to test this. Run a private instance and
point only the test process at it:

```bash
# Server: a socket name of its own, so no existing client finds it.
PIPEWIRE_CORE=pw-test pipewire &

# Client: the only process that connects to it.
PIPEWIRE_REMOTE=pw-test python -c 'import sounddevice, time; time.sleep(30)' &

# Kill the server first, then let the client reach the end of main.
kill -9 %1
```

The client exits 134 (SIGABRT). `PIPEWIRE_CORE` names the socket the server
publishes under `$XDG_RUNTIME_DIR`; `PIPEWIRE_REMOTE` names the one a client looks
for. Both default to `pipewire-0`, which is the session's — setting them is the
whole isolation.

`PIPEWIRE_REMOTE` also steers PipeWire's ALSA and PulseAudio client paths, so it
cannot be used in production to make PortAudio's JACK backend fail to initialise:
it would take capture down with it.

## Fix

There is no PortAudio switch for disabling a host API at runtime; the set is fixed
at build time. The teardown has to not run:

```python
import atexit, sys

sounddevice = sys.modules.get("sounddevice")
if sounddevice is not None:
    atexit.unregister(sounddevice._exit_handler)
```

Murmly does this on the daemon path only (`disable_portaudio_exit_teardown` in
`audio.py`, called from `_run_daemon`). Two consequences to keep in mind when
touching audio code:

- That hook was also what closed the last open stream. Every stream Murmly opens
  must now be closed by Murmly before the process exits.
- `_exit_handler` is a private symbol. The helper warns and continues if a future
  `sounddevice` renames it, and a test pinned against the real module is what
  catches the rename.

The installed unit is also ordered `After=pipewire.service wireplumber.service`, so
systemd stops Murmly before the audio server. That narrows the window but does not
close it — it does nothing for a PipeWire that crashed or was restarted under a
running daemon.

## Cleaning up after a test

Core dumps from deliberate reproductions land in `/var/lib/systemd/coredump` and
need root to remove. Check what a run left behind with:

```bash
coredumpctl list python
```


## Dropping the teardown traded the SIGABRT for a SIGSEGV

Recorded 2026-08-23, issue #27. `Pa_Terminate` was also the only thing that stopped
PortAudio's own `pw-PortAudio` loop threads, and nothing else in the tree calls it.
Without it the daemon reaches `Py_Finalize` with those threads still running, and
finalization unloads the extension libraries they are executing in:

```text
Thread A  #0  0x00007faa280df0a0 n/a (n/a + 0x0)          <- unmapped
          #1  do_loop            (libpipewire-0.3.so.0)
Thread B  #0  insertdict.isra.0        (libpython3.14.so.1.0)
          #1  _PyModule_ClearDict
          #2  finalize_modules
          #3  _Py_Finalize
```

`murmly.service: Failed with result 'core-dump'` again, with signal 11 instead of 6.

The reproduction is not what you would guess. Opening streams is irrelevant; the
**loaded extension modules** are what decide it:

| loaded | teardown | exit |
| --- | --- | --- |
| anything | registered | 0 |
| nothing heavy | unregistered | 0 |
| `numpy`, `ctranslate2`, `av`, `tokenizers` or `huggingface_hub` alone | unregistered | 0 |
| **`onnxruntime` or `faster_whisper`** | unregistered | **139** |

So a reproduction has to import `onnxruntime`, call
`disable_portaudio_exit_teardown()`, and return through ordinary interpreter shutdown.
A test that only opens a stream exits 0 and proves nothing.

The daemon now leaves through `leave_without_finalizing` in `cli.py`, which flushes
and calls `os._exit`. Verify any change to this against **both** stop conditions,
because the first fix passed one and broke the other:

| stop condition | how to test |
| --- | --- |
| audio server alive | `systemctl --user stop murmly.service`, then check `Result` is `success` and `ExecMainCode` is 1, not 3 |
| audio server already gone | the private-PipeWire harness above |

Re-registering the teardown with the server gone still aborts with 134, which is the
control that proves the harness is exercising the right path.
