---
title: Fix Daemon Shutdown Abort Design
description: Technical approach for releasing Murmly's own audio streams at shutdown, dropping PortAudio's exit-time teardown on the daemon path, and ordering the unit after the session's audio server
---

## Context

See proposal.md — Why for the failure and its diagnosis. The parts of the current
code and environment that shape the approach:

- `audio.py` imports `sounddevice` lazily, inside `SoundDeviceRecorder.start()` and
  `SoundDevicePlayer.start()`. That import runs `Pa_Initialize()` and registers an
  `atexit` hook (`sounddevice.py:2971-2972`). The hook stops and closes the last
  stream, then loops `Pa_Terminate()` until PortAudio's refcount reaches zero.
- `Pa_Terminate()` tears down every host API that initialised, not only the one a
  stream was opened on. On Fedora, `/etc/ld.so.conf.d/pipewire-jack-x86_64.conf` puts
  PipeWire's `libjack.so.0` on the loader path, so PortAudio's JACK host API
  initialises and holds a JACK client for the life of the process. Its teardown is
  `pa_jack.c:867`, `ASSERT_CALL( jack_deactivate( jackHostApi->jack_client ), 0 )`.
- `MurmlyDaemon.shutdown()` (`daemon.py:1024`) closes the server socket, the speech
  session, the accepted connections and the overlay. It never touches `self._recorder`.
- `_run_daemon` (`cli.py:285`) installs SIGINT and SIGTERM handlers that call
  `daemon.shutdown()`, runs `serve_forever()`, and restores the handlers in a `finally`.
- `SpeechEngine.end()` (`speech.py:425`) already aborts and stops the player, so the
  output stream is closed on the shutdown path today.

Reproduction, for anyone re-checking this: start a private PipeWire instance under
`PIPEWIRE_CORE`, run a process with `PIPEWIRE_REMOTE` pointed at it that imports
`sounddevice`, kill the instance, then let the process return from `main`. It aborts
with the same assertion without ever having opened a stream.

## Goals / Non-Goals

**Goals:**

- The daemon exits with status 0 whatever state the audio server is in.
- Every stream Murmly opened is closed by Murmly, on a path that can be tested.
- Reduce the chance of the race at all by stopping Murmly before PipeWire.

**Non-Goals:**

- Changing how capture, playback, or device selection work while the daemon runs.
- Changing the behavior of short-lived CLI commands.
- Repairing or working around Fedora's `TimeoutStopFailureMode=abort` drop-in.
- Delivering a transcript for audio captured when the stop signal arrives. Shutdown
  releases the device; it does not transcribe what was in the buffer.

## Decisions

### Neutralize the exit-time teardown rather than prevent JACK from initialising

`jack_deactivate()` failing is not something Murmly can make succeed — it depends on
whether the JACK server is still there, which at logout it is not. The call therefore
has to not happen.

- **Chosen:** `atexit.unregister(sounddevice._exit_handler)` on the daemon path, so
  `Pa_Terminate()` never runs in that process. PortAudio's memory and threads are
  reclaimed by the kernel when the process exits, which is the only thing left for
  the teardown to have done.
- **Rejected — stop PortAudio initialising the JACK host API.** PortAudio has no
  runtime switch for disabling a host API; the set is fixed at build time. The only
  lever is making `jack_client_open()` fail, which means pointing `PIPEWIRE_REMOTE` at
  a socket that does not exist — and that same variable steers PipeWire's ALSA and
  PulseAudio client paths, so it would take capture down with it.
- **Rejected — `os._exit(0)` after a clean shutdown.** It would also skip the hook,
  and would additionally sidestep any interpreter-finalization hazard, but it skips
  logging and stdio flushing and the multiprocessing resource tracker, and it cannot
  be asserted from an in-process `unittest` without a new injection seam. The
  narrower fix is testable in the style the suite already uses.
- **Rejected — patch or vendor PortAudio.** Out of proportion to a fix that is one
  call in Murmly.

### Unregister from `_run_daemon`, in a `finally`

The abort happens at process exit regardless of why the process is exiting, so the
coverage has to include a startup refusal and an unhandled exception, not just a
clean stop. `_run_daemon` already has a `finally` for restoring signal handlers.

Placing it there rather than at daemon startup also means it does not matter that
`sounddevice` is imported lazily: by the time the daemon unwinds, the module is in
`sys.modules` if any audio was ever touched, and if it is not, there is no hook to
drop. The helper reads `sys.modules.get("sounddevice")` rather than importing, so a
daemon that never opened a device does not pay for the import.

`_exit_handler` is a private symbol. The helper checks for it and logs a warning if a
future `sounddevice` renames it, rather than raising: losing this protection must not
stop the daemon from shutting down.

### Closing Murmly's own streams becomes required

The hook being dropped is also what closed the last stream. `SpeechEngine.end()`
already covers the player. `MurmlyDaemon.shutdown()` gains a recorder stop, placed
with the speech session close — before the connection drain, so the device is
released while the daemon is still able to report a failure to close it.

This is a real gap on its own: a capture running when SIGTERM arrives is currently
left open until the interpreter exits, holding the microphone open for the whole of
shutdown.

### Order the unit after PipeWire

`After=pipewire.service wireplumber.service` in the unit template. systemd stops units
in reverse start order, so this stops Murmly before the audio server rather than
concurrently with it.

This is secondary, not a substitute for the fix above: `After=` orders only units
systemd has in the same transaction, and it does nothing for a PipeWire that crashed
or was restarted under a running daemon. It is worth having because it also removes a
window where capture or playback fails mid-shutdown for the same reason.

## Risks / Trade-offs

- **`sounddevice` renames or drops `_exit_handler`** → The helper degrades to a logged
  warning and the daemon keeps working, with the abort possible again. A test that
  exercises the real module catches the rename at CI time rather than at the user's
  next logout.
- **PortAudio is never terminated in the daemon** → Its host API clients and threads
  live until the process exits. This only happens on the path where the process is
  exiting anyway, so there is nothing to leak into.
- **`shutdown()` now closes an audio stream from a signal handler** → It runs in the
  main thread, the same context in which it already closes sockets and the overlay,
  and PortAudio's close is safe against its own callback thread. A worker inside
  `stop_recording()` at the same moment is handled the way the existing code handles
  it: `SoundDeviceRecorder.stop()` clears `_stream` before closing, so the second
  caller finds nothing to close. The narrow window between that read and write is
  pre-existing and is not widened here.
- **A stream that will not close** → Reported and stepped over, so one stuck device
  cannot leave the socket and the overlay behind.

## Migration Plan

1. The process-level fix takes effect on the next daemon restart.
2. The unit ordering needs `murmly install` run again; existing installations keep the
   old unit file until then. This is stated in the proposal's Impact and belongs in
   the change's summary to the user, not in code.
3. A daemon already in `failed` from a past occurrence still needs one
   `systemctl --user reset-failed murmly.service`. Nothing in this change clears it.

Rollback is reverting the commit; there is no persisted state to undo beyond the unit
file, which `murmly install` rewrites.
