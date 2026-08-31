"""The countdown that gives an idle model's memory back.

Its own module rather than more of `daemon.py`, because both models want the
same countdown and neither model holder should learn about periods. `stt.py` and
`tts.py` each expose a release that is safe to call at any moment and from any
thread; what is here decides only when to call one.
"""

from __future__ import annotations

from collections.abc import Callable
import ctypes
import ctypes.util
import gc
import logging
import threading


logger = logging.getLogger(__name__)


#: Why `_MALLOC_TRIM` is None, for a platform that has no way to answer the
#: question `system_memory_returnable()` exists to ask. Set alongside it, by
#: `_malloc_trim()`, so the two can never disagree about which is true.
_SYSTEM_MEMORY_UNRETURNABLE_REASON: str | None = None


def _malloc_trim():
    """glibc's `malloc_trim`, or None where the libc in use has no such call.

    Looked up once, into `_MALLOC_TRIM` below, alongside the reason recorded
    into `_SYSTEM_MEMORY_UNRETURNABLE_REASON` when the answer is None -- a build
    against musl, or anything else without it, and the release simply stops
    after freeing, which is all it could do there anyway. That reason is what
    `return_free_heap` used to leave unsaid: it tried nothing and reported
    nothing, so a person who set an idle period to get memory back had no way
    to learn the platform never gave any back at all.
    """
    global _SYSTEM_MEMORY_UNRETURNABLE_REASON
    name = ctypes.util.find_library("c")
    if name is None:
        _SYSTEM_MEMORY_UNRETURNABLE_REASON = "No C library could be located on this platform."
        return None
    try:
        libc = ctypes.CDLL(name)
    except OSError as error:
        _SYSTEM_MEMORY_UNRETURNABLE_REASON = f"The C library ({name}) could not be loaded: {error}"
        return None
    trim = getattr(libc, "malloc_trim", None)
    if trim is None:
        _SYSTEM_MEMORY_UNRETURNABLE_REASON = (
            f"The C library in use ({name}) has no malloc_trim. This is glibc's own "
            "extension for returning freed heap memory to the system; musl and "
            "other C libraries provide no equivalent, so freed memory stays in "
            "this process's own allocator rather than reaching the system."
        )
        return None
    trim.argtypes = [ctypes.c_size_t]
    trim.restype = ctypes.c_int
    return trim


_MALLOC_TRIM = _malloc_trim()


def system_memory_returnable() -> bool:
    """Whether this platform's allocator can be asked to return freed memory.

    Static for the life of the process -- it depends only on which C library
    this interpreter is linked against, resolved once above alongside
    `_MALLOC_TRIM`. Read by `murmly doctor` and by the daemon's own residency
    diagnostics, neither of which should have to know `_MALLOC_TRIM` exists to
    ask the same question.
    """
    return _MALLOC_TRIM is not None


def system_memory_unreturnable_reason() -> str | None:
    """Why `system_memory_returnable()` is False, or None where it is True.

    A function rather than the module constant itself, so a caller that
    imports it by name (as `murmly.cli` does with everything else from this
    package) gets the current answer through the call rather than a copy of
    whatever `_SYSTEM_MEMORY_UNRETURNABLE_REASON` held at import time -- the
    same reason `return_free_heap` is a function and not a cached bool.
    """
    return _SYSTEM_MEMORY_UNRETURNABLE_REASON


def return_free_heap() -> bool:
    """Hand what a release just freed back to the operating system.

    Dropping a model frees its host allocations into glibc's arenas, which is
    not the same as returning them: the pages stay mapped, stay counted against
    this process, and stay unavailable to every other one. The requirement a
    release exists to meet is that the memory reach the system rather than an
    internal pool, and freeing alone reaches the pool.

    Measured on the synthesis path, five release cycles under the shipped
    `[tts] device = "cpu"`: the release on its own left RSS between 465 and
    669 MiB, and the same release followed by this left it at 83 MiB every time.
    On the transcription path a release returns its weights to the device rather
    than to the heap, so there is little to trim at that moment -- but the reload
    before it allocates around 1416 MiB of staging that is free by then, and this
    is what gives that back.

    Called after the model's own lock has been dropped. It walks the arenas,
    which is not free, and no caller should wait behind it holding a lock a
    transcription needs.

    Returns whether the platform's allocator could be asked at all -- not
    whether the call found anything to hand back, which `malloc_trim`'s own
    return value describes and this has never surfaced. A platform that cannot
    be asked still gets the model dropped above and `gc.collect()` run here;
    what it does not get is memory reaching the system, and this is how the
    caller learns that happened so it can say so rather than stay silent.
    """
    gc.collect()
    if _MALLOC_TRIM is None:
        return False
    try:
        _MALLOC_TRIM(0)
    except Exception as error:  # noqa: BLE001 - a failed trim is not a failed release
        # The memory was freed either way. Reporting this as a release failure
        # would say the model is still resident when it is not.
        logger.debug("Trimming the heap after a release failed: %s", error)
        return False
    return True


class IdleRelease:
    """One release, run after a quiet period and abandoned if the quiet ends.

    Arming restarts the countdown from zero rather than adding a second one, so
    a lifecycle that opens and closes all day never accumulates pending
    releases. A period of zero registers nothing at all -- not a countdown of no
    length -- because zero is how a person says the model should stay resident,
    which is the shipped default for synthesis and the rollback for
    transcription.

    Not `threading.Timer`. A `Timer` takes its daemon flag from the thread that
    creates it, and every arm site here runs on a non-daemon thread, so a
    pending one would be joined by interpreter finalization: five minutes of a
    test suite refusing to exit, and a daemon whose exit waits on a countdown
    that exists precisely because nothing is happening.
    """

    def __init__(self, period_s: float, release: Callable[[], object], *, name: str) -> None:
        self._period_s = period_s
        self._release = release
        self._name = name
        self._lock = threading.Lock()
        # The countdown in flight, or None. Its event is what a cancel sets, and
        # the generation beside it is what a cancel that arrives too late to set
        # anything is caught by.
        self._pending: threading.Event | None = None
        self._generation = 0

    @property
    def period_s(self) -> float:
        return self._period_s

    @property
    def armed(self) -> bool:
        """Whether a countdown is running right now."""
        with self._lock:
            return self._pending is not None

    def arm(self) -> None:
        """Start the countdown again from zero."""
        if self._period_s <= 0:
            return
        with self._lock:
            self._cancel_locked()
            generation = self._generation
            waiter = threading.Event()
            self._pending = waiter
            thread = threading.Thread(
                target=self._wait_and_release,
                args=(waiter, generation),
                name=self._name,
                daemon=True,
            )
            try:
                thread.start()
            except RuntimeError as error:
                # Recorded as unarmed rather than left looking armed with
                # nothing behind it, which would make the next cancel report
                # success over a countdown that never ran.
                self._pending = None
                logger.warning("Idle release %s did not start: %s", self._name, error)

    def cancel(self) -> None:
        """Abandon the countdown, if one is running."""
        with self._lock:
            self._cancel_locked()

    def _cancel_locked(self) -> None:
        # The generation moves whether or not there is a thread to wake, because
        # the thread this is cancelling may already be past its wait and about
        # to fire. Setting the event alone would not stop it; failing the
        # generation check does.
        self._generation += 1
        pending = self._pending
        self._pending = None
        if pending is not None:
            pending.set()

    def _wait_and_release(self, waiter: threading.Event, generation: int) -> None:
        if waiter.wait(self._period_s):
            return
        self._fire(generation)

    def _fire(self, generation: int) -> None:
        """Release, unless this countdown was abandoned while it was expiring."""
        with self._lock:
            if generation != self._generation:
                return
            # Spent, so the generation moves here too. Only one thread ever
            # holds a given generation, but a countdown that stayed answerable
            # after it had fired would run its release twice for anything that
            # reached this a second time, and a release is not free.
            self._generation += 1
            self._pending = None
        # Outside the lock. A release waits for the model's own lock, which a
        # transcription pass can hold for the length of a decode, and the cancel
        # that runs when capture begins must not queue behind that: the toggle
        # calling it holds the daemon's state lock while it does.
        try:
            self._release()
        except Exception as error:  # noqa: BLE001 - nothing above this catches it
            # On its own thread, with no caller to report to. Left uncaught it
            # would be printed by the threading machinery and the countdown
            # would look as though it had run.
            logger.warning("Idle release %s failed: %s", self._name, error)
