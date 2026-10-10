## Context

See proposal.md — Why for the mechanism and the reproduction. What matters here is
what the controller's three exits have in common, who reads what, and why the fix
reuses an existing flag.

`OverlayController` runs one background thread, `_run`, for its whole life. Two of
its exits are orderly and both follow `close()`, which sets `_closed` under
`_condition`, in the same critical section that queues the shutdown message. The
third is an uncaught exception, which nothing but a bug in `_run` itself can cause:
`_send()` already handles a renderer that went away, and `_launch_renderer()` handles
a renderer that would not start. After `fix-overlay-renderer-shutdown-leak` that
third exit tears the renderer down, and does nothing else.

The thread can also fail to exist at all. `start()` calls `Thread.start()` inside a
handler, and a process that cannot create another thread makes it raise. The handler
records health and nothing else, which leaves the controller in the state a death
does — no reader and no `_closed` — minus a renderer to tear down.

Who reads the state this leaves behind:

- Every publisher (`publish_state`, `publish_level`, `publish_partial`,
  `publish_error`) checks `_closed` under `_condition` before it queues anything, and
  `_next_message()` reads it under the same lock. `start()` and
  `_ensure_renderer_for_listening()` read it without the lock. `_closed` is already
  the one flag that means "nothing will consume this queue again".
- `health` is read by nothing in production. `Daemon` publishes to the overlay and
  closes it, and never reads its health. `murmly doctor`'s `overlay_diagnostics()`
  runs the renderer's own `--check` in a fresh subprocess and does not consult the
  running controller. The only places a dead thread is visible today are the
  controller's own `health` property and the warning `_set_health()` logs on a
  transition to unavailable. This change makes both correct; it does not wire the
  health into `doctor`, which is a separate decision about what a diagnostics report
  of a running process should mean.
- Nothing restarts the controller. `Daemon` builds it once; `start()` is called by the
  constructor and nowhere else.

## Goals / Non-Goals

**Goals:**

- A thread that dies from an uncaught exception leaves the controller reporting
  unavailable, with the exception as the detail, in the same way a launch failure
  already does.
- A thread that cannot be started leaves the controller reporting unavailable with
  the start error as the detail, as it already does, and now also closed to updates.
- After either, a publish is dropped, not queued — including one that was already
  past its own `_closed` check on another thread when the thread died.
- Whatever was still queued is released, so a controller whose thread died or could
  not be started holds nothing.
- The renderer teardown `fix-overlay-renderer-shutdown-leak` added still happens,
  unchanged, on this path.
- A `close()` that arrives while the dying thread is still tearing down returns only
  once the renderer is terminated, as it does on every other path.

**Non-Goals:**

- Restarting the thread, or retrying a refused start. A thread that died from a bug
  may die again on the same input; a restart policy needs a bound (like
  `restart_delays` for the renderer) and a decision about what to do on exhaustion.
  Neither is needed to stop the leak.
- Making `murmly doctor` report the live controller's health. See Context.
- A thread ended by `BaseException` that is not an `Exception` (`SystemExit` raised
  in the thread, for instance). Nothing in `_run` does that, and every other handler
  in this module catches `Exception`.

## Decisions

### Reuse `_closed`; do not add a second flag

Considered a separate "stopped" flag so that `close()` keeps behaving exactly as it
does today after a death. Rejected: it means a second check in every place that
already tests `_closed`, and a publisher that forgets the second one silently
reintroduces the leak. Setting `_closed` is also the right reading of the state: the
controller has closed, just not through `close()`.

The observable consequences of that choice, all intended:

- Later publishes return at once. `publish_level()` and `publish_partial()` already
  return before touching their slots when `_closed` is set; `_enqueue_control()`
  does the same for states and errors.
- `start()` becomes a no-op instead of raising "threads can only be started once"
  into its own handler and overwriting the cause in `health`.
- After a refused thread start, `start()` is a no-op as well, not a second attempt.
  Nothing calls `start()` after construction, so no caller has a retry to lose; see
  Risks.
- `close()` can no longer treat `_closed` as "already closed, nothing left to do",
  because the thread now sets it too, before its `finally` has torn anything down.
  See the next decision.

### `close()` guards the shutdown message with `_closed`, not the teardown

Until now `_closed` was set only by `close()`, so a second `close()` returning on it
was safe: the first call had already done the waiting and the terminating, or was
still doing it. With the thread setting it too, the flag no longer says that. It is
set at the start of the `except` clause, and the renderer is terminated at the end of
the `finally` that follows; a `close()` arriving between the two, if it still
returned on the flag, would return with the renderer running. The window is one
health write, one log call and one transport close, so it is short, but `Daemon`
closes the overlay last on both of its shutdown paths and the process can exit
inside it. The `Shutdown after the renderer is already running` scenario promises the
renderer is terminated before shutdown returns, and this would break that promise.

So `close()` now tests `_closed` only around queueing the shutdown message. The two
bounded joins and `_terminate_process()` run on every call. Each is safe to repeat: a
thread that is dead or was never started is not alive, so both joins are skipped, and
`_terminate_process()` is idempotent (`_transport_lock`-guarded, swaps the process to
`None`), which is also what keeps `The overlay is closed more than once` true. A
refused start is the never-started case: there is no thread to join and no renderer
to terminate, so `close()` returns at once.

The cost is that a repeated `close()` while the thread is still alive — still inside
the renderer launch, say — waits up to its two 0.5 s joins again instead of returning
at once. Each call is bounded the same way the first is. Considered instead: ordering
the `except` clause so teardown runs before `_closed` is set. Rejected: it duplicates
the `finally`, and an exception from that teardown would escape before the death was
recorded at all.

### Record under `_condition`, in one critical section, in one place

`_abandon_queue()` sets `_closed` and clears `_control_messages` and `_latest_level`
inside a single `with self._condition:` block, the lock every publisher holds while
it checks `_closed` and appends. `_run`'s `except` clause and `start()`'s handler both
call it, so the order inside it cannot drift between the two. Done as two separate
steps, the order would matter: clearing the queue before setting the flag lets a
publisher on another thread append in between and leave a message behind with no
reader. As one block there is no in-between: a publisher either appended before it —
and is cleared with the rest — or takes the lock after it and sees `_closed`.

A publisher can be mid-way through when the thread dies: it has encoded its message
and not yet taken the lock. It takes the lock later and finds `_closed` set, so it
drops. This is the case the mid-flight test pins, with the publisher parked between
encoding and queueing so the order is forced rather than hoped for. A refused start
goes through the same block, so the same reasoning holds for it; only the death has a
test that parks a publisher.

### Health after the flag, and the exception's `repr` as its detail

Health is set after the critical section, through `_set_health()`, the same writer
`_launch_renderer()` and `_send()` use, so the transition to unavailable logs a
warning exactly once. It is outside `_condition` because `_set_health()` takes
`_health_lock` and logs; the two locks are never held together anywhere in this
module and are not here either. `start()`'s handler follows the same order.

The detail for a death is `repr(error)`, not `str(error)`: the type is the useful part
of an unexpected failure, and some exceptions have no message at all. `start()`'s
handler keeps the detail it already had, `Unable to start overlay controller:`
followed by the error's message. What `Thread.start()` raises carries one, and
changing a string that is already reported is not needed to stop the leak.

### Re-raise

The clause in `_run` records and re-raises. Swallowing the exception would hide the
traceback from `threading.excepthook`, which is the only place the stack of an
unexpected failure ends up, and would change how the thread exits for every test and
tool that watches that hook. `start()`'s handler does not re-raise, as before: it runs
from the constructor, and a raise there would take the daemon's construction down for
a visual failure.

## Risks / Trade-offs

- **A repeated `close()` can wait again.** See the `close()` decision. `Daemon` closes
  the overlay on both of its shutdown paths and an ordinary shutdown runs both, so a
  second close is the usual case. It waits only when the thread is still alive after
  the first, stuck in the renderer launch, and then for the same two 0.5 s joins the
  first call used. Each call stays inside the bound shutdown already has; the two
  waits can add up when they run one after the other.
- **A publish that fails to encode after the controller stopped still writes health.**
  The publishers' own handlers set health to "Unable to queue overlay ..." when
  `encode_overlay_message` raises, and they do that before they look at `_closed`. A
  caller that passed an invalid value after the thread died, or after it was refused,
  would replace the cause with that. Nothing in Murmly passes one: `Daemon` supplies
  states from an enum, levels it computes, and text that encoding cannot reject. Not
  changed here; moving the `_closed` check ahead of encoding in every publisher is a
  wider change than this one needs.
- **The controller is permanently unavailable after a death or a refused start.**
  With no restart, a single bug in `_run` costs the overlay for the rest of the
  daemon's life. Before this change a refused start could be retried by calling
  `start()` again; it cannot now. Nothing in Murmly calls `start()` after
  construction, so no existing caller loses anything. Capture, transcription,
  clipboard and paste are unaffected, which is what `Visual failure isolation`
  already requires of every visual failure, and the cause is now in the log and in
  `health` rather than hidden.

## Open Questions

None.
