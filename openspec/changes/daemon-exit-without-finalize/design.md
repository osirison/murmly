## Context

See `proposal.md` — Why, for the core dumps and the reproduction table.

The relevant shape of the code today:

```python
def _run_daemon(config: MurmlyConfig) -> int:
    try:
        return _serve_daemon(config)
    finally:
        disable_portaudio_exit_teardown()
```

`_run_daemon` returns an int to `_dispatch`, which returns it to `main`, which returns it
to the console-script wrapper. Between that last return and the process actually ending,
CPython runs `Py_Finalize`, and that is the window the crash lives in.

Two callers constrain where a hard exit can go:

| caller | what it does | what a hard exit inside `_run_daemon` would do |
| --- | --- | --- |
| `UnhandledFailureTests` (`tests/test_cli.py`) | calls `main([... "daemon"])` in-process | kills the test process |
| `DaemonExitTeardownTests` (`tests/test_cli.py`) | calls `_run_daemon` and asserts on the return | kills the test process |

## Goals / Non-Goals

**Goals:**

- The daemon's determined exit status is the process's exit status.
- The change is confined to the daemon's exit boundary; no other command's behaviour
  moves.
- The tests can exercise the boundary without the test process leaving with it.

**Non-Goals:**

- Re-registering or conditionally restoring PortAudio's teardown. See `proposal.md` —
  Why not the alternatives.
- Stopping the `pw-PortAudio` threads. Nothing available does this without
  `Pa_Terminate`, and the point of this change is to stop caring that they are running.
- Making finalization safe in general. Only the daemon is in the crashing configuration,
  and only the daemon skips finalization.

## Decisions

### Leave via `os._exit` at the daemon branch, not inside `_run_daemon`

`os._exit` terminates without unwinding, without running atexit handlers, and without
`Py_Finalize`. That is precisely the property wanted: nothing unloads the libraries the
audio threads are executing in.

It goes **after** `_run_daemon` returns, in the branch that dispatches the daemon
command, for the reason in the table above. `_run_daemon` keeps returning an int, so both
existing test classes keep working unchanged.

*Alternative considered:* `signal.signal(SIGSEGV, SIG_IGN)` or otherwise surviving the
fault. Rejected — it hides a real use-after-unmap rather than preventing it, and the
behaviour after ignoring it is undefined.

*Alternative considered:* `atexit.register(os._exit)`. Rejected — atexit handlers run
*before* `finalize_modules`, but relying on ordering inside the shutdown sequence is more
fragile than not entering it, and it would apply to every command rather than the daemon.

### The seam has to be patchable

A bare `os._exit(code)` in `main` makes the daemon branch untestable: any test that
reaches it takes the test runner down with it. The exit is therefore made an injectable
seam — a module-level reference the tests can patch, in the same style as the existing
`patch("murmly.cli.disable_portaudio_exit_teardown")`.

That keeps the *decision* to hard-exit testable (assert it was called, with the right
status) even though the *act* cannot be performed in-process.

### Flushing becomes explicit

`os._exit` skips the flushing that finalization would otherwise do. What must be flushed
before it:

- `sys.stdout` and `sys.stderr` — the daemon prints startup refusals to stderr, and the
  spec requires that output to survive.
- `logging` handlers — `logging.shutdown()` is what the logging module registers with
  atexit, and atexit does not run.

Anything else the daemon holds — sockets, file descriptors, the model memory — is
reclaimed by the kernel on exit and needs nothing.

This is the part most likely to regress silently, because losing output looks like the
feature simply not logging. It gets its own scenario in the spec and its own test.

### The daemon still closes its own streams first

Unchanged, and worth stating because it is what makes the hard exit safe rather than
merely convenient. `disable_portaudio_exit_teardown`'s docstring already says the caller
owns closing the streams Murmly opened. That remains true; this change only removes the
finalization that ran afterwards.

## Risks / Trade-offs

- **`os._exit` skips any cleanup a future contributor adds to an atexit handler or a
  `__del__`** → The requirement states the flush obligation explicitly, and the exit sits
  at one boundary with a comment naming what it skips and why.
- **Coverage tools and profilers write their data at interpreter shutdown** → They would
  lose the daemon's data. No coverage measurement runs against a live daemon process
  today; if one is added, the seam is patchable, which is the same escape hatch the tests
  use.
- **The reproduction depends on which extension modules are loaded**, so a dependency
  change could make the crash stop reproducing while the underlying unsafety remains →
  The verification harness pins the reproduction as its own check rather than relying on
  the suite's exit code to notice.
- **A hard exit could mask a hang**: a daemon that fails to release something would
  previously stall in finalization and be visible → It would now exit cleanly instead.
  Accepted; a stalled shutdown was never a useful signal, and systemd's stop timeout
  covers the case that matters.

## Migration Plan

None. No configuration, no state, no on-disk format. Rollback is reverting the commit.

## Open Questions

None.
