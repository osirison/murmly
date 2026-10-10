## 1. Confirm the defect before changing anything

- [x] 1.1 Trace `_run()`, `_launch_renderer()`, `_send()`, `start()`, the publishers
      and `close()` in `src/murmly/overlay.py`: `_closed` is set only by `close()`, so
      an uncaught exception ends the thread with it unset, and so does a thread start
      that raises, whose handler in `start()` records health only; from the thread,
      `_health` is written only through `_launch_renderer()` and `_send()`'s handler,
      neither of which sees that exception; and the publishers' `_closed` check is
      what stops a publish from queueing.
- [x] 1.2 Find every reader of the controller's health and anything that could
      restart it: none in production reads `health` (`Daemon` only publishes and
      closes; `overlay_diagnostics()` runs its own `--check`), and nothing calls
      `start()` after construction.
- [x] 1.3 Reproduce with the exception-path test's seam: a transport whose `sendall`
      raises `RuntimeError`, then `publish_state(LISTENING)`. `health.available` is
      still `True`, and further `publish_state()` calls grow `_control_messages`.
      Reproduce the refused start too: with the thread's `start` patched to raise
      `RuntimeError`, health reports the start error and every publish still grows
      `_control_messages`.

## 2. Tests, written before the fix

- [x] 2.1 Move the raising transport to module level and add a helper that builds a
      controller whose thread has died from it, joined rather than polled, with
      `threading.excepthook` recorded.
- [x] 2.2 After the thread dies, health is unavailable and its detail names the
      exception — including when the thread dies before its loop, where health was
      already unavailable with a stale detail.
- [x] 2.3 A publish after the death, of every kind, is dropped: the control queue and
      the latest-level slot stay empty.
- [x] 2.4 Messages queued behind the one that killed the thread are released.
- [x] 2.5 A publish that had already encoded its message when the thread died is
      dropped, with the publisher parked between encoding and queueing so the order
      is forced.
- [x] 2.6 `close()` after the death returns, is idempotent, leaves the renderer torn
      down, and leaves the cause in health.
- [x] 2.7 `start()` after the death neither restarts the thread nor replaces the
      cause.
- [x] 2.8 A thread that cannot be started (its `start` patched to raise): messages
      queued before the failed start are released, publishes of every kind after it
      are dropped, health is unavailable with the start error as the detail, a later
      `start()` neither starts the thread nor changes the cause, and `close()`
      returns promptly, twice.
- [x] 2.9 A failure while closing is recorded and `close()` still returns with the
      renderer torn down.
- [x] 2.10 A `close()` that lands while the dying thread is parked inside its teardown
      (transport close held open, death already recorded, renderer not yet
      terminated) returns only once the renderer is terminated, and leaves the cause
      in health.
- [x] 2.11 Run the new tests against the pre-fix `overlay.py` (swapped in from `HEAD`,
      run, restored, with the bytecode caches cleared around the swap): all ten fail.
      Nine fail on the symptom they name. The `close()`-while-tearing-down test fails
      against `HEAD` only at its precondition, that health already reports the death,
      which `HEAD` never does; the `close()` behaviour it exists for is shown red
      separately, in 3.5, against the fixed file with `close()` returning early on
      `_closed` again. With the fix, all pass.

## 3. Fix

- [x] 3.1 Add `_abandon_queue()`: under `_condition`, set `_closed` and clear
      `_control_messages` and `_latest_level`, in one block.
- [x] 3.2 Add an `except Exception` clause to `_run`'s guarded region. It calls
      `_abandon_queue()`, marks health unavailable with the exception's `repr`, and
      re-raises. Leave the `finally` as it is.
- [x] 3.3 Call `_abandon_queue()` from `start()`'s handler, before it records the
      start error as health. Keep the detail it records.
- [x] 3.4 Make `close()` test `_closed` only around queueing the shutdown message, so
      the bounded joins and the renderer termination run on every call, including one
      that arrives after the thread has recorded its death but before its `finally`
      has terminated the renderer.
- [x] 3.5 Confirm by mutation that each part is covered. In `_run`'s clause, dropping
      the re-raise, the call to `_abandon_queue()`, the health write, or the exception
      from the detail each fails at least one new test. In `_abandon_queue()`,
      dropping the flag, the queue clear or the latest-level clear each fails at least
      one death test and the refused-start test. Dropping `start()`'s call, or the
      error from its detail, fails the refused-start test. Returning from `close()`
      early on `_closed` again fails the close-while-tearing-down test.

## 4. Spec

- [x] 4.1 Add a requirement to `recording-overlay` stating that when the overlay's
      own control work cannot be started or fails, Murmly reports the overlay
      unavailable with the cause, drops later updates, discards held ones, does not
      try to start that work again, and still returns from a close only once the
      renderer is terminated.
- [x] 4.2 Run `openspec validate fix-overlay-thread-death --strict`.

## 5. Verify

- [x] 5.1 Run the full suite: `uv run --no-sync python -m unittest discover -s
      tests`.
