## Why

The daemon crashes as it exits. `murmly daemon` PID 560208 dumped core with signal 11
on 2026-08-22 and systemd recorded `murmly.service: Failed with result 'core-dump'` —
the same failed-unit state the change archived as `2026-08-19-fix-wayland-overlay-and-paste`'s
sibling shutdown work set out to remove. Issue #11 removed a SIGABRT and left a SIGSEGV
behind it.

The two threads in that core show the whole thing:

```
Thread A  #0  0x00007faa280df0a0 n/a (n/a + 0x0)          <- unmapped
          #1  do_loop            (libpipewire-0.3.so.0)
          #2  start_thread       (libc.so.6)

Thread B  #0  insertdict.isra.0        (libpython3.14.so.1.0)
          #1  _PyModule_ClearDict      (libpython3.14.so.1.0)
          #2  finalize_modules.lto_priv.0
          #3  _Py_Finalize.constprop.0
```

`disable_portaudio_exit_teardown()` unregisters sounddevice's atexit hook, which is
correct — that hook runs `Pa_Terminate`, whose JACK teardown aborts when the audio
server has already stopped, which is what a logout is. But that hook was also the only
thing that stopped PortAudio's `pw-PortAudio` loop threads. Nothing else in the tree
calls `Pa_Terminate`; `src/murmly/audio.py` only ever calls `stream.close()`. So the
daemon reaches `Py_Finalize` with those threads running, and finalization unloads the
libraries they are executing in.

Reproduced outside Murmly, deterministically:

| loaded extension modules | atexit hook | exit |
| --- | --- | --- |
| any | intact | 0 |
| none | stripped | 0 |
| `onnxruntime` or `faster_whisper` | stripped | **139** |
| `onnxruntime` or `faster_whisper` | stripped, leaving via `os._exit` | **0** |

Opening and closing real audio streams makes no difference; the loaded extension
modules do. The daemon loads both of those, so it is in the crashing configuration
every time it runs.

## What Changes

- The daemon process leaves without running interpreter finalization, once it has
  determined its exit status and released what it owns.
- Everything the daemon is responsible for flushing or closing is flushed or closed
  explicitly before that, rather than relying on finalization to do it.
- **No change to `disable_portaudio_exit_teardown`.** Keeping PortAudio's teardown
  unregistered is still right; this change stops the consequence of it, not the cause.

Not breaking: the daemon's exit status for every outcome is unchanged. What changes is
that the status is what the process actually exits with, instead of being replaced by a
signal.

### Why not the alternatives

- **Stop only the JACK host API's threads.** PortAudio offers no per-host-API
  terminate; `Pa_Terminate` is all or nothing, which is what issue #11 ran into.
- **Keep PortAudio from initialising the JACK host API**, so the teardown could be left
  registered. Tested: `JACK_DEFAULT_SERVER` pointed at a non-existent server changes
  nothing, because pipewire-jack ignores it. The `pw-PortAudio` threads persist.
- **Re-register the teardown at exit when the audio server is still alive.** Requires
  Murmly to decide whether the server will outlive it, which is exactly the race that
  makes a logout abort.

## Capabilities

### Modified Capabilities

- `command-interface`: the requirement that no command terminates with an unhandled
  error covers what a command reports. It does not yet say that the daemon's exit
  status is the status the process actually exits with. This change adds that, because
  a core dump after a clean run is indistinguishable to systemd from a failure during
  one.

## Impact

- `src/murmly/cli.py` — the daemon branch, after `_run_daemon` returns. It must sit
  there rather than inside `_run_daemon`: `UnhandledFailureTests` calls `main()` and
  `DaemonExitTeardownTests` calls `_run_daemon` in-process expecting a return, so an
  unconditional hard exit inside either would kill the test process.
- Whatever the daemon must flush before leaving — logging handlers, buffered streams —
  becomes explicit rather than implied by finalization.
- No new dependencies.
- `docs/agent-notes/portaudio-jack-exit-abort.md` and the docstring of
  `disable_portaudio_exit_teardown` both claim "there is nothing else the teardown does
  that the kernel does not do when the process exits". That is true of the host-API
  disconnect and false of stopping the threads before the interpreter unloads the code
  under them. Both need correcting.

## Verification

Both rows, not one — issue #11 fixed the second and broke the first:

| stop condition | before | required after |
| --- | --- | --- |
| audio server alive (an ordinary `systemctl --user stop`) | SIGSEGV | exit 0, no core |
| audio server already gone (the logout ordering) | SIGABRT before #11, exit 0 after | exit 0, no core |

`docs/agent-notes/portaudio-jack-exit-abort.md` documents the private-PipeWire harness
for the second row; it is reused rather than rebuilt.
