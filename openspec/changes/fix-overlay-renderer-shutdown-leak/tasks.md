## 1. Confirm the defect before changing anything

- [x] 1.1 Trace `close()`, `_run()`, and `_launch_renderer()` in
      `src/murmly/overlay.py` to confirm `self._process` is not assigned until
      `Popen()` returns, and that `close()`'s single `_terminate_process()` call
      runs before that assignment when the launch outlives both of `close()`'s
      0.5s joins.
- [x] 1.2 Confirm the reproduction: a fake `popen_factory` that sleeps 1.5s before
      returning a process, `start()` immediately followed by `close()`. `close()`
      returns after its ~1.0s bound; the fake process's `terminated` flag stays
      `False` forever.

## 2. Fix

- [x] 2.1 Wrap `_run`'s whole loop — including the initial `_launch_renderer()`
      call — in `try/finally`. In the `finally`, call `_close_transport()` then
      `_terminate_process()`, so every exit path (the loop's two `return`s, or an
      uncaught exception) tears both down exactly once.
- [x] 2.2 Remove the `_close_transport()` call the `stop_after_send` branch made
      inline, now covered by the `finally`.
- [x] 2.3 Leave `close()`'s own `_terminate_process()` call in place — confirm it
      is still safe to run twice (`_transport_lock`-guarded, swaps `self._process`
      to `None`) and still the only thing that terminates a process for a thread
      that never reaches `_run`'s `finally`.

## 3. Tests

- [x] 3.1 Add a regression test that drives a renderer launch still inside
      `Popen()` when `close()`'s joins expire, and polls the fake process's
      `terminated` flag with a deadline past the point `close()` itself returns —
      asserting immediately would be flaky by construction, since the cleanup now
      happens on `_run`'s thread after `close()` has already returned.
- [x] 3.2 Confirm the test fails against the pre-fix code (stashed the fix, ran the
      test, restored it) and passes with the fix applied.
- [x] 3.3 Add the same `terminated` assertion to the existing fast-path clean-
      shutdown test (`test_clean_shutdown_sends_shutdown_message`), closing the gap
      that `FakeProcess.terminated` was defined but never asserted anywhere.
- [x] 3.4 Run the full suite: `uv run --no-sync python -m unittest discover -s
      tests`.
- [x] 3.5 Add a regression test for the third exit path — an uncaught exception —
      forcing `_send` to raise something other than the `BrokenPipeError`/`OSError`
      it already handles, once a renderer is recorded, and asserting the transport
      is closed and the process is terminated before `close()` runs (test cleanup
      only). Confirmed it fails against the pre-fix code (swapped in `overlay.py`
      from `main`, ran the single test, restored it) and passes with the fix
      applied.

## 4. Spec

- [x] 4.1 Add a requirement to `recording-overlay` stating that Murmly does not
      abandon a renderer process at shutdown, including one still launching when
      shutdown began, scoped to Murmly's own shutdown control flow rather than the
      process being killed outright.
- [x] 4.2 Run `openspec validate fix-overlay-renderer-shutdown-leak --strict`.
