---
title: Fix Daemon Shutdown Abort
description: Stop the daemon aborting in PortAudio's JACK teardown at shutdown, so a logout leaves murmly.service inactive rather than failed with a core dump
---

## Why

The daemon aborts during shutdown with `pa_jack.c:867: Terminate: Assertion 'err == 0' failed`,
dumps a 178 MB core, and leaves `murmly.service` in `failed`, so the service does not come back
without `systemctl --user reset-failed` (issue #11). It happens at logout, which is exactly when
the user is least able to notice and correct it.

The mechanism is confirmed by reproduction. `sounddevice` registers an `atexit` handler that calls
`Pa_Terminate()`. PortAudio's JACK host API tears down there, and its `Terminate()` asserts that
`jack_deactivate()` returned 0. On this system `libjack.so.0` resolves to PipeWire's JACK shim, so
that call fails whenever PipeWire has already gone away — which is what happens at logout, where
nothing orders Murmly's stop before PipeWire's. A scratch process that only imported `sounddevice`,
never opening a stream, aborts with the identical assertion when its PipeWire server is killed
before it exits. So the trigger is not stream state or process age; it is the teardown of a host API
Murmly never asked for, against a server that is already gone.

## What Changes

- Murmly's daemon shutdown closes the microphone stream. `MurmlyDaemon.shutdown()` currently stops
  the socket, the speech session, and the overlay, but never the recorder, so a capture running when
  the signal arrives is left open until the interpreter exits.
- The daemon process no longer runs PortAudio's exit-time teardown. It drops `sounddevice`'s
  `atexit` hook, so `Pa_Terminate()` — and with it the JACK host API's `Terminate()` — never runs.
  Every stream Murmly opened is closed by Murmly first, so the hook has nothing left to do.
- The installed service unit is ordered after PipeWire, so Murmly stops before the audio server it
  depends on rather than racing it.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `desktop-integration`: the session-lifetime requirement gains the outcome of a stop — the daemon
  exits cleanly and the service is left inactive rather than failed — and a new requirement covers
  releasing the audio device before the process exits.

## Impact

- `src/murmly/audio.py`: a function that neutralizes PortAudio's exit-time teardown.
- `src/murmly/cli.py`: the daemon path calls it as it unwinds.
- `src/murmly/daemon.py`: `shutdown()` stops capture.
- `src/murmly/installer.py`: the unit template's ordering.
- `tests/test_audio.py`, `tests/test_cli.py`, `tests/test_daemon.py`, `tests/test_installer.py`.
- Existing installations need `murmly install` run again before the unit ordering takes effect. The
  process-level fix needs only a restart.

Out of scope: Fedora's `TimeoutStopFailureMode=abort` drop-in, which turns any stop that overruns
`TimeoutStopSec` into a second SIGABRT-and-core path. It is a distinct failure that has not been
observed here, and it is not corrected by anything in this change.
