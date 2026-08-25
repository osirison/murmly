"""Stand in for a module in `sys.modules`, and put back only what was changed.

`patch.dict(sys.modules, ...)` restores the whole mapping when it exits, so every
module the code under test imported while it was active is evicted along with the
stand-in. `murmly.audio.pcm16_from_float32` imports numpy lazily, inside the
patched region, and Python 3.14 refuses to load an extension module a second
time:

    ImportError: cannot load module more than once per process

so that eviction breaks every later test reaching that import. It surfaced only
when `test_audio.py` ran on its own: in the full suite an earlier module had
already put numpy into the mapping being restored, so the accident of import
order hid it. See issue #28.

These touch one key and leave the rest of `sys.modules` where the import system
put it. A module that is legitimately `None` there - which is how a test spells
"importing this fails" - is preserved as `None` rather than treated as absent,
so the sentinel below is deliberate.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager

_MISSING = object()


@contextmanager
def injected_module(name: str, module: object) -> Iterator[None]:
    """Install `module` under `name` for the duration of the block."""
    previous = sys.modules.get(name, _MISSING)
    sys.modules[name] = module
    try:
        yield
    finally:
        if previous is _MISSING:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


@contextmanager
def removed_module(name: str) -> Iterator[None]:
    """Hide `name` for the duration of the block, as if it were never imported."""
    previous = sys.modules.pop(name, _MISSING)
    try:
        yield
    finally:
        if previous is not _MISSING:
            sys.modules[name] = previous
