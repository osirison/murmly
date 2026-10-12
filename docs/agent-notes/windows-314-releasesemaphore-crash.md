---
title: Intermittent ReleaseSemaphore crash on the Windows Python 3.14 job
description: Fatal Python error from the daemon's pipe flush thread, seen twice in about twenty runs; a rerun passes, and the cause is not confirmed
trigger: ReleaseSemaphore, _PySemaphore_Wakeup, parking_lot, murmly-pipe-flush, unittest (Windows, Python 3.14)
depends_on: src/murmly/win_pipe.py, tests/test_daemon.py, .github/workflows/tests.yml
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

## How often

Intermittent. Seen twice in about 20 runs, on 2026-10-11 and 2026-10-12. Rerunning
the failed job passed both times. It has not been seen on any other operating
system or Python version, so it is not a reason to hold a pull request: rerun the
job, and look at the failure only if it repeats on the same commit.

## Cause: not confirmed

**Everything in this section is inferred. None of it has been reproduced.**

Error 6 is `ERROR_INVALID_HANDLE`. CPython 3.14 releases a Windows semaphore when
it wakes a parked thread, and it was told the handle is invalid. One way that
happens: a handle is closed twice. The number of a closed handle can be handed out
again, so a second close of the same number could close a handle CPython has since
opened for its own semaphore, and the later wake fails with error 6.

What the code shows, which is confirmed: `_flush_completed_within` runs
`FlushFileBuffers(self._handle)` on a daemon thread and gives up waiting after a
timeout, so that thread can still be running when `close()` calls
`CloseHandle(self._handle)` on the same handle (its own docstring says the
daemon closes every server handle right after). Whether that, or a second close
elsewhere, produces the crash is the unconfirmed part.

## First thing to check

Whether any pywin32 handle in `win_pipe.py` can be closed twice, or used after it
was closed, in particular `self._handle` between the flush thread and `close()`.
If so, the fix is to close it exactly once and keep the flush thread from using it
afterwards (for example `DuplicateHandle` it for the flush, or join the thread
before closing). If every handle is closed once and never used after, this
inference is wrong, and the next suspect is the flush thread still running when
the interpreter finalizes.

A reproduction would settle it: loop the speech-session tests in `test_daemon`
on a Windows 3.14 machine until it fails, with `PYTHONFAULTHANDLER=1`.
