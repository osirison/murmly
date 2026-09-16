## Context

See proposal.md — Why for the mechanism and the reproduction. What matters here is
which thread has to own teardown and why the obvious alternative — making `close()`
wait longer — does not fix it.

`OverlayController` runs one background thread, `_run`, for the controller's whole
life. It calls `_launch_renderer()` once at the top, then loops: pull the next
control message, send it, launch or re-launch the renderer as needed, and exit when
told to. `_launch_renderer()` is the only place that assigns `self._process` and
`self._transport`, and it assigns both only after its `Popen()` call (or, on
Windows, the socket-share handshake around it) has returned.

`close()` runs on whatever thread calls it — `Daemon.shutdown()`'s last step, or
`serve_forever`'s own `finally` (`src/murmly/daemon.py`, both routes end at
`OverlayController.close()`). It has no way to know whether `_run` is mid-`Popen()`
or already looping, so it waits — twice, 0.5s each — and then gives up and calls
`_terminate_process()` itself. That call is correct for every state `_run` can be in
*except* mid-launch, where `self._process` is still `None` and there is nothing to
terminate. The bug is not that `close()` waits too briefly; no finite wait bounds
`Popen()`, which can block on toolkit init, a compositor handshake, or (on Windows)
the child actually starting far longer than 1.0s.

## Goals / Non-Goals

**Goals:**

- Whichever thread eventually learns a renderer process exists is the thread that
  terminates it, so no interleaving of `close()` and `_launch_renderer()` can leave
  a live process with nothing pointed at it.
- `close()` keeps its existing bounded wait. A hung renderer launch must not make
  shutdown hang.
- The fix covers all three ways `_run` can end — the two `return`s already in its
  loop and an uncaught exception — with one mechanism, not three special cases.

**Non-Goals:**

- Bounding `Popen()` itself, e.g. with a launch timeout that gives up and reports
  failure. That is a real gap (a renderer that never starts also never gets marked
  unhealthy on this path), but it is a different defect with a different fix, and
  fixing it is not required to stop the leak.
- Covering a daemon process that is killed outright rather than shut down through
  `Daemon.shutdown()` or `serve_forever`. See Risks.
- Anything about why a renderer might be slow to launch (toolkit init, a compositor
  handshake). Not this change's concern; the fix has to hold regardless of the
  reason `Popen()` is slow.

## Decisions

### `_run`'s own `finally` owns teardown, not `close()`

Wrap `_run`'s whole body — including the initial `_launch_renderer()` call — in
`try/finally`, and in the `finally` call `_close_transport()` then
`_terminate_process()`. `_run`'s two `return` statements no longer close the
transport inline; the `finally` does it once, on every exit path.

This is the only place that can see the write to `self._process` at the moment it
happens: `_launch_renderer()` and the loop that follows it share a thread, and the
`finally` runs immediately after the loop returns, on that same thread, with no
`join()` or timeout in between. There is no interleaving where `_launch_renderer()`
assigns `self._process` after `_run`'s `finally` has already run — the assignment
happens, at the latest, at the very call `finally` is waiting on the return of.

### `close()`'s `_terminate_process()` call stays

It is safe to run twice: `_terminate_process()` swaps `self._process` to `None`
under `_transport_lock` before touching it, so a second call — whether from `close()`
after `_run`'s `finally` already ran, or the reverse order — finds `None` and does
nothing. It is not redundant, either. Two situations still need it:

- The thread finished launching and exited normally while `close()`'s joins were
  still running. `_run`'s `finally` already tore the process down in that case, but
  `close()` calling it again costs nothing.
- The thread never reaches `_run`'s `finally` at all — for instance a `join()` that
  never returns because of a bug elsewhere. `close()`'s own call is the only thing
  that still terminates a process in that case, though it can only do so once
  `self._process` has actually been assigned; a launch still in progress at that
  point is exactly the case this change fixes on the `_run` side instead.

### `close()`'s wait stays bounded, unchanged

Considered making `close()` wait for `_launch_renderer()` to finish before giving up,
by threading a cancellable launch or a longer join through it. Rejected: `Popen()`
has no portable, host-independent timeout, so "wait for it" becomes "wait
indefinitely" on exactly the host where the renderer is slowest to start — the one
where a hang is worst to have shutdown depend on. The fix instead accepts that
`close()` returns without knowing whether a launch is still in flight, and moves the
guarantee to "whatever launches eventually, gets torn down" rather than "close()
does not return until it is torn down."

## Risks / Trade-offs

- **A renderer launched right as the daemon process is killed outright (`SIGKILL`,
  a crash, power loss) is still stranded.** `_run` is a daemon thread; nothing runs
  its `finally` if the interpreter never gets to run Python during exit. This is not
  new exposure — every child process Murmly has ever launched has the same property
  under a hard kill — and it is not what `Daemon.shutdown()` or `serve_forever`'s
  `finally` exercise, both of which run inside the process, in Python, with time to
  reach `close()`. The requirement this change adds is scoped to those paths.

- **`close()` can still take up to its existing ~1.0s bound even when the renderer
  never had a problem.** Unchanged from today; this fix does not touch how long
  `close()` waits, only what happens to the process after it stops waiting.

## Open Questions

None.
