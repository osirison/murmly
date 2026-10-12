---
title: ReleaseSemaphore crash on the Windows Python 3.14 job
description: Fatal Python error from the daemon's pipe flush thread, caused by two threads closing the same pipe handle at shutdown; fixed in #86, so a recurrence means a second concurrent close somewhere
trigger: ReleaseSemaphore, _PySemaphore_Wakeup, parking_lot, murmly-pipe-flush, unittest (Windows, Python 3.14)
depends_on: src/murmly/win_pipe.py, src/murmly/daemon.py, tests/test_platform.py, .github/workflows/tests.yml
recorded: 2026-10-12
---

## What it looks like

The Windows Python 3.14 test job dies in the middle of the suite with

```
Fatal Python error: _PySemaphore_Wakeup: parking_lot: ReleaseSemaphore failed (error: 6...)
```

The thread named in the dump is the daemon's `murmly-pipe-flush` thread, in
`win_pipe.py` (`_flush_completed_within`, or `shutdown` calling it). It happens
during the speech-session tests in `tests/test_daemon.py`. No test reports a
failure first: the process is gone, so there is no unittest summary.

It was seen three times in about twenty runs on 2026-10-11 and 2026-10-12, never
on another operating system or Python version. Rerunning the job passed each
time, which is why it was first taken for a flake. It was not one.

## Cause, and the fix (#86)

Confirmed from the code: two threads closed the same pipe handle at once at every
daemon shutdown.

- `NamedPipeServer.close()` ran on the caller's thread from `daemon.shutdown()`
  and again on the accept thread when `_serve_named_pipe`'s `with server:` block
  exited. The first close cancels the pending `ConnectNamedPipe`, which is what
  makes the accept thread leave the block and close a second time.
- `NamedPipeConnection.close()` had the same shape: `_shutdown_socket` on a
  session thread against `daemon.shutdown()` closing every connection.

pywin32's `PyHANDLE::Close` releases the GIL around `CloseHandle` before it marks
the handle closed, so two callers can both close the same raw handle value.
CPython's own socket close marks the descriptor closed before releasing the GIL,
which is the contract `daemon.py` was written against.

Inferred, consistent with every dump: Python 3.13 and later create a Windows
semaphore for each blocking wait on a threading primitive and close it on waking,
so a freed handle value is handed to a live semaphore within microseconds. The
second `CloseHandle` then closes that semaphore, and the next wake fails with
error 6 (`ERROR_INVALID_HANDLE`). Python 3.12 holds no per-wait handle, which is
why only the 3.14 job showed it.

#86 adds a close-once gate in `win_pipe.py` (a lock and a flag flipped before the
Win32 call) used by both `close()` methods. Tests in `tests/test_platform.py`
hold a fake `CloseHandle` open while a second `close()` arrives from another
thread, and fail with the gate removed.

## If it comes back

Then something else closes a shared pywin32 handle from two threads. Look for
any `CloseHandle`/`.Close()` on a handle more than one thread can reach, and give
it the same close-once gate. A reproduction settles it: loop the speech-session
tests in `test_daemon` on a Windows 3.14 machine with `PYTHONFAULTHANDLER=1`.
