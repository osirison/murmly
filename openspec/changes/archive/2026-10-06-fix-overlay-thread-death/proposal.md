## Why

When `OverlayController`'s message thread dies from an uncaught exception, the
renderer is torn down correctly (`fix-overlay-renderer-shutdown-leak`), but the
controller does not record that it has stopped. Found while adding exception-path
coverage for that change; GitHub issue #59. A thread that never started leaves the
same state behind; see the end of this section.

`_run()` can end three ways: the `encoded is None` return and the `stop_after_send`
return, both reached only once `close()` has set `_closed`, and an uncaught
exception. The third is the only exit that leaves nothing behind that says the
thread is gone:

- **Health stays `available`.** From the thread, `_health` is written by
  `_launch_renderer()` and by `_send()`'s handler for `BrokenPipeError`/`OSError`. An
  exception neither handles escapes both, so `_health` keeps whatever the last
  successful launch wrote — `available=True` — for a thread that no longer exists. A
  later `start()` then replaces even that with a false cause: `Thread.start()` raises
  "threads can only be started once", and `start()`'s own handler reports it as the
  reason the overlay is unavailable.
- **The control queue grows with nothing to read it.** `_closed` is set only by
  `close()`, so every later `publish_state()` and `publish_error()` appends to
  `_control_messages`. Partials are coalesced and levels overwrite a single slot, so
  those two are bounded; states and errors are not. One entry per state change and
  per error for the life of the daemon, none ever consumed.

Neither is likely to show up quickly — it takes a bug in `_run` itself, not a renderer
that went away — which is why it was not noticed until a test forced the path.

Confirmed by reproduction, with the seam `fix-overlay-renderer-shutdown-leak` added:
a transport whose `sendall` raises `RuntimeError`, then `publish_state(LISTENING)`.
The thread dies, `health.available` is still `True` with no detail, and every further
`publish_state()` call stays in `_control_messages`.

The thread can also fail to exist at all. `start()` wraps `Thread.start()` in a
handler, and a process that cannot create another thread makes it raise. The handler
records health ("Unable to start overlay controller: ...") and nothing else, so
`_closed` stays unset with no thread that will ever read the queue, and the second
bullet above applies unchanged: every later `publish_state()` and `publish_error()`
is appended and none is consumed. Health is right in this case; the queue is not.
Confirmed the same way, with the thread's `start` patched to raise `RuntimeError`:
health reports the start error, and every publish stays in `_control_messages`.

## What Changes

- A new `_abandon_queue()` does the recording both cases share. Under `_condition` —
  the lock every publisher appends under — it sets `_closed` and discards whatever is
  in `_control_messages` and `_latest_level`, in one critical section.
- `_run()` gains an `except Exception` clause on its existing guarded region, so the
  initial `_launch_renderer()` call and the loop are both covered. It calls
  `_abandon_queue()`, marks health unavailable with the exception's `repr` as the
  detail, and re-raises. The existing `finally` still tears down the transport and the
  renderer; it is unchanged.
- `start()`'s existing handler calls `_abandon_queue()` before it records the start
  error. The detail it records is unchanged.
- `close()` stops returning early on `_closed`. The thread now sets it too, before
  its `finally` has terminated the renderer, so a `close()` landing in that window
  would return with the renderer still running. The flag now guards only queueing the
  shutdown message; the bounded joins and the renderer termination run on every call,
  and each is safe to repeat.
- No new flag. `_closed` already means "nothing will read this queue again" to every
  publisher, to `start()`, to `_next_message()` and to
  `_ensure_renderer_for_listening()`, and setting it is what makes later publishes
  drop and `start()` a no-op rather than an error. The exception is re-raised, not
  swallowed, so `threading.excepthook` still logs the traceback.
- No restart. A controller whose thread has died or could not be started stays that
  way; restarting is a separate decision with its own failure modes (what a crash
  loop would look like). A `start()` after a refused start is a no-op, not a second
  attempt.

Not in scope: making `murmly doctor` read the running overlay's health (see
design.md, Context), and a thread ended by `BaseException` rather than `Exception`.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `recording-overlay`: a new requirement states that when the overlay's own control
  work cannot be started or fails, Murmly reports the overlay as unavailable with
  that failure as the cause, drops later updates instead of holding them, and does
  not try to start that work again, and a request to close the overlay still returns
  only once its renderer is terminated. `Visual failure isolation` covers a renderer
  that terminates; it does not cover this.

## Impact

- `src/murmly/overlay.py` — `OverlayController` gains `_abandon_queue()`; `_run` gains
  one `except` clause and a comment; `start()`'s handler gains one call and a comment;
  `close` moves its `_closed` test from a `return` to an `if` around the shutdown
  message, so the teardown after it always runs, and gains a comment. Nothing else
  changes.
- `tests/test_overlay.py` — the raising transport the exception-path test defined
  inline moves to module level so more tests can use it; a helper builds a controller
  whose thread has already died. New tests cover health and its cause, a publish
  after death, messages already queued at death, a publish mid-flight at death, a
  death before the loop starts, `close()` and `start()` afterwards, a thread that
  cannot be started, a `close()` that lands while the dying thread is still tearing
  the renderer down, and a failure while closing.
- No configuration change, no protocol change, no change to any other capability.
