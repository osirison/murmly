from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from murmly import idle
from murmly.idle import (
    return_free_heap,
    system_memory_returnable,
    system_memory_unreturnable_reason,
)


class ReturnFreeHeapTests(unittest.TestCase):
    """`_MALLOC_TRIM` stands in for "this platform's allocator can be asked"."""

    def test_a_platform_that_can_be_asked_reports_memory_returned(self) -> None:
        trim = Mock(return_value=0)

        with patch.object(idle, "_MALLOC_TRIM", trim):
            self.assertTrue(return_free_heap())

        trim.assert_called_once_with(0)

    def test_a_platform_that_cannot_be_asked_reports_memory_not_returned(self) -> None:
        """Task 3.2 / 18.16: silence is exactly what the requirement forbids."""
        with patch.object(idle, "_MALLOC_TRIM", None):
            self.assertFalse(return_free_heap())

    def test_a_trim_call_that_raises_still_reports_not_returned(self) -> None:
        # The memory was freed by `gc.collect()` either way; only the "reached
        # the system" half of the answer is false here.
        trim = Mock(side_effect=OSError("boom"))

        with patch.object(idle, "_MALLOC_TRIM", trim):
            self.assertFalse(return_free_heap())

    def test_gc_collect_runs_even_where_the_allocator_cannot_be_asked(self) -> None:
        with (
            patch.object(idle, "_MALLOC_TRIM", None),
            patch("murmly.idle.gc.collect") as collect,
        ):
            return_free_heap()

        collect.assert_called_once_with()


class SystemMemoryReturnableTests(unittest.TestCase):
    def test_returnable_when_the_allocator_can_be_asked(self) -> None:
        with patch.object(idle, "_MALLOC_TRIM", Mock()):
            self.assertTrue(system_memory_returnable())
            self.assertIsNone(system_memory_unreturnable_reason())

    def test_not_returnable_reports_a_reason(self) -> None:
        with (
            patch.object(idle, "_MALLOC_TRIM", None),
            patch.object(
                idle,
                "_SYSTEM_MEMORY_UNRETURNABLE_REASON",
                "this platform has no malloc_trim",
            ),
        ):
            self.assertFalse(system_memory_returnable())
            self.assertEqual(
                "this platform has no malloc_trim", system_memory_unreturnable_reason()
            )

    def test_a_libc_with_no_malloc_trim_is_recorded_as_the_reason(self) -> None:
        """Exercises `_malloc_trim()` itself against a stand-in C library.

        `_SYSTEM_MEMORY_UNRETURNABLE_REASON` is left as it was found: `_malloc_trim`
        writes it as a module global through a `global` statement, and
        `patch.object` restores whatever was there before this test regardless
        of what happened inside the block, so the real platform's own answer
        is not left overwritten for every test that runs after this one.
        """
        libc = Mock(spec=[])  # no `malloc_trim` attribute at all

        with (
            patch("murmly.idle.ctypes.util.find_library", return_value="c"),
            patch("murmly.idle.ctypes.CDLL", return_value=libc),
            patch.object(idle, "_SYSTEM_MEMORY_UNRETURNABLE_REASON", None),
        ):
            trim = idle._malloc_trim()
            self.assertIsNone(trim)
            self.assertIn("malloc_trim", idle._SYSTEM_MEMORY_UNRETURNABLE_REASON)


class IdleReleaseStillDropsTheModelTests(unittest.TestCase):
    """18.16: a model still drops on schedule where the allocator cannot be asked."""

    def test_a_release_the_countdown_fires_still_runs_where_memory_cannot_return(
        self,
    ) -> None:
        released = []

        countdown = idle.IdleRelease(0.01, lambda: released.append(True), name="test")
        with patch.object(idle, "_MALLOC_TRIM", None):
            # The countdown does not itself call `return_free_heap` -- the model
            # holders do, inside the callable this test stands in for -- but its
            # only job, dropping the model on schedule, must not depend on
            # whether the platform can also return the memory to the system.
            self.assertFalse(system_memory_returnable())
            countdown.arm()
            self.assertTrue(
                self._wait_for(lambda: released, timeout=2.0),
                "the release never ran",
            )

        self.assertEqual([True], released)

    @staticmethod
    def _wait_for(predicate, timeout: float) -> bool:
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return bool(predicate())


if __name__ == "__main__":
    unittest.main()
