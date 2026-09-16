## Why

`OverlayController.close()` can return while a renderer subprocess it launched is
still alive and never gets reaped.

`close()` sets `_closed`, joins the thread that owns the renderer (`_run`) across
two bounded 0.5s timeouts, calls `_terminate_process()` exactly once, and returns.
`_run()` calls `_launch_renderer()` before it looks at `_closed` at all, and
`_launch_renderer()` does not assign `self._process` until `Popen()` itself has
returned. If that call is still running when both of `close()`'s joins expire,
`self._process` is `None`, so `_terminate_process()` has nothing to kill.
`_launch_renderer()` then assigns `self._process` after `close()` has already
returned, and nothing terminates that process afterward: `close()` is idempotent
via its `_closed` guard, and `Daemon.shutdown()` — which calls `overlay.close()` as
its last step — hits that same guard on any later call.

`_run()`'s two return paths compound it. The `encoded is None` path returns without
closing the transport or terminating the process. The `stop_after_send` path (taken
for the shutdown message `close()` itself queues) closes the transport but never
terminates the process. An uncaught exception inside the loop leaves the thread with
no cleanup at all.

In the common case the renderer exits on its own once its socket hits EOF, so the
leak is a zombie handle rather than a running process. The severe case is a renderer
wedged before it reaches its read loop — slow toolkit init, a failed Windows share
handshake — which is never terminated at all. `Daemon.shutdown()` calling
`overlay.close()` last means this fires whenever daemon shutdown races a
hotkey-triggered launch, which is not a rare ordering: a hotkey pressed right before
the process that owns it is asked to stop.

Confirmed by reproduction: a fake `popen_factory` that sleeps 1.5s before returning a
process, then `start()` immediately followed by `close()` — `close()` returns after
about 1.0s (its two joins) and the fake process's `terminated` flag stays `False`
forever, because nothing after `close()` returns ever looks at the process it
belatedly received.

## What Changes

- The thread that owns the launch (`_run`) becomes the thread that owns teardown.
  Its whole loop — including the initial `_launch_renderer()` call — runs inside a
  `try/finally` that closes the transport and terminates the process on the way out,
  regardless of which of the loop's two `return`s is taken or whether an exception
  escapes it. Teardown therefore happens once `_launch_renderer()` actually returns,
  not bounded by how long `close()` was willing to wait for it.
- `close()` keeps its own `_terminate_process()` call. It is not made redundant: it
  is what reaps a process that finished launching while `close()`'s joins were still
  running, and it stays the backstop for a thread that, for some other reason, never
  reaches `_run`'s `finally`.
- `close()`'s own bound on how long it waits for the thread is unchanged — it still
  returns within its existing bounded time regardless of how long the renderer takes
  to launch. What changes is what happens to the renderer after that: today, nothing;
  after this change, `_run`'s `finally` still tears it down once the launch call
  returns.

Not in scope: a daemon process killed outright (`SIGKILL`, a crash, a hard power
loss) rather than shut down through `Daemon.shutdown()` or `serve_forever`'s own
`finally`. `_run` is a daemon thread, so its `finally` runs only if the interpreter
gets to run Python at all during exit; a renderer wedged mid-launch at the instant
the process is killed is stranded regardless of this fix, the same way every other
child process Murmly has launched would be. See design.md's Risks.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `recording-overlay`: a new requirement states that Murmly does not abandon a
  renderer process when it shuts down, including one whose launch was still under
  way when shutdown began.

## Impact

- `src/murmly/overlay.py` — `OverlayController._run` wraps its loop in `try/finally`
  and moves the `stop_after_send` branch's `_close_transport()` call into that
  `finally`, alongside a new `_terminate_process()` call. `close()` is unchanged.
- `tests/test_overlay.py` — a regression test drives a renderer launch that is still
  inside `Popen()` when `close()`'s joins expire and polls for the fake process's
  `terminated` flag past the point where `close()` itself returns. The existing
  clean-shutdown test gains the same assertion for the already-covered fast path.
- No configuration change, no protocol change, no change to any other capability.
