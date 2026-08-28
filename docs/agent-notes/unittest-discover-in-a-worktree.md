---
title: The documented test command fails in a worktree, and the fix is recorded elsewhere
description: uv run --no-sync leaves a worktree with an empty .venv and sixteen load errors; the two notes that explain it are keyed to triggers nobody greps for when running tests
trigger: uv run --no-sync python -m unittest discover -s tests, uv run --no-sync python -m unittest, unittest discover -s tests

depends_on: AGENTS.md, docs/agent-notes/onnxruntime-gpu-cuda-version.md, docs/agent-notes/capturing-the-overlay-for-docs.md
recorded: 2026-08-28
---

# The documented test command fails in a worktree

This note exists to be found. Both halves of it are recorded already, under
triggers nobody greps for while trying to run the tests.

**Symptom:** inside a worktree under `.worktrees/`, the command `AGENTS.md` and
CI both name

```bash
uv run --no-sync python -m unittest discover -s tests
```

reports a load error for every test module that imports a project dependency --
sixteen of them, starting with `ModuleNotFoundError: No module named 'numpy'` --
while the stdlib-only modules pass. The worktree has a `.venv`, so nothing looks
missing. It holds three entries against the main checkout's ninety-five, because
`--no-sync` created it and then skipped installing into it. Already recorded in
`onnxruntime-gpu-cuda-version.md`, under the `uv sync` trigger.

**Do not fix it by syncing the worktree.** Same note, same reason the documented
command carries `--no-sync`: a sync puts the CPU `onnxruntime` back over the GPU
build. If you need the worktree to give real synthesis or transcription numbers,
that note is authoritative -- it needs its own synced environment with the swap
reapplied, and nothing measured before that means anything.

**To run the suite,** which is not a measurement, borrow the main checkout's
interpreter and point it at the worktree's source. This is the pattern
`capturing-the-overlay-for-docs.md` already uses for `murmly doctor`:

```bash
MAIN=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")
PYTHONPATH="$PWD/src" "$MAIN/.venv/bin/python3" -m unittest discover -s tests
```

`--git-common-dir` resolves to the main checkout's `.git` from inside any
worktree, so its parent is the checkout whose `.venv` was populated.

**Confirm it imported the right branch before trusting the result.** The
borrowed venv carries an editable install pointing at the main checkout's
`src/`, on another branch. `PYTHONPATH` does win -- verified on 2026-08-28,
Python 3.14 -- but it is one line to check and a green run against the wrong
branch is the failure this guards against:

```bash
PYTHONPATH="$PWD/src" "$MAIN/.venv/bin/python3" -c \
  "import murmly; print(murmly.__file__)"
```

It must print a path under `.worktrees/`.

**Why it was not obvious:** the failure is loud and misattributed. Sixteen
errors reads as a broken change rather than an unpopulated environment, and the
sixteen failing modules are exactly the ones importing dependencies. Stashing
every local change and re-running is what separates the two -- the same sixteen
fail on an unmodified tree.
