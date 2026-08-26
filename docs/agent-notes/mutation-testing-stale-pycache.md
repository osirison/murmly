---
title: Clear __pycache__ after every restore in a write-restore mutation harness
description: A mutation that preserves a file's byte size and is restored within the same mtime second leaves a .pyc CPython considers valid, so the suite then runs mutated bytecode against pristine source
trigger: mutation testing, shutil.copyfile, write-restore harness, .venv/bin/python -m unittest discover -s tests, __pycache__, mutmut, cosmic-ray

depends_on: tests/, src/murmly/
recorded: 2026-08-26
verified_on: Fedora 44, CPython 3.14.7
---

## Symptom

A test fails in the full suite and passes when run on its own, with no
concurrency and nothing shared between them to explain it. Re-running the suite
reproduces the failure. Reading the source shows code that cannot produce it.

The decisive tell is source and running code disagreeing about the same
function. During this repo's `unload-idle-gpu-models` work:

```text
inspect.getsource(MurmlyDaemon.shutdown)   ->  cancels after _stop_capture()
live traceback                             ->  cancel called from the line
                                               number of _close_speech_session
```

That combination is only possible when the bytecode being executed was compiled
from a different source than the one on disk.

## Cause

A mutation harness that writes a mutated file, runs the suite, and restores the
original with `shutil.copyfile` produces a `__pycache__/*.pyc` compiled from the
mutated text. CPython's default bytecode invalidation is timestamp-based: it
compares the source file's **mtime and size** against the values recorded in the
`.pyc` header.

A mutation that only **moves or swaps lines**, or replaces a token with one of
equal length, leaves the file's size unchanged. If the restore also lands inside
the same one-second mtime granularity, both recorded values still match and the
mutated `.pyc` is accepted as current. Every later run in that process tree then
executes the mutation against source that no longer contains it.

Mutations that change the file's length are self-correcting, which is why this
appears intermittently and looks like a flaky test rather than a tooling fault.

It is not confined to the harness that caused it. A second agent working in the
same tree during the same session reported a full-suite run with 4 failures and
2 errors, concluded the code was fine after finding the source clean, and never
identified a cause. That is the same failure: the stale `.pyc` outlives the
harness that wrote it and reaches anything that imports the module afterwards.

## Fix

Clear the caches after every restore, and before every mutation:

```bash
find . -name __pycache__ -prune -exec rm -rf {} +
```

Better, make the harness incapable of causing it by never writing bytecode at
all:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests
```

Restore with `shutil.copyfile` rather than `cp`, which is aliased interactive on
this machine and silently skips the overwrite, leaving the mutation in place --
a different way to reach the same "source does not match what ran" state.

## Confirming rather than assuming

If a test fails in the suite and passes alone, clear the caches and re-run
before investigating the code. If the failure vanishes, it was this. If it
survives, it is real and the caches were never the question.

`inspect.getsource()` reads the file; a traceback reports the running bytecode's
line numbers. When those two disagree about the same function, stop reading the
code and clear the caches.
