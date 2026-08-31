"""Task 8.3-8.5, 18.10: `WindowsHotkeyRegistrar`'s policy layer, exercised
against fakes standing in for every Win32 seam (`register`, `unregister`,
`pump`, `post_quit`, `current_thread_id`).

None of this touches `ctypes.windll` -- that half (`_real_register`,
`_real_pump`, and friends) can only be confirmed on Windows. What is proven
here is everything `win_hotkey.py`'s own module docstring claims for the
fakeable half: rebuilding the whole binding set on one fresh thread per
`rebind()`, treating the platform's own registration refusal as the collision
(task 8.4) without querying first, unwinding a partial batch so a later
purpose's failure never leaves an earlier one bound, and releasing every held
key when the thread stops (task 8.5).
"""

from __future__ import annotations

import queue
import threading
import unittest

from murmly.hotkey import HotkeyError
from murmly.win_hotkey import (
    WindowsHotkeyRegistrar,
    _PumpHotkey,
    _PumpOther,
    _PumpQuit,
)


class FakeWin32:
    """A single-threaded stand-in for the whole Win32 hotkey surface.

    `register` refuses (returns ``False``) for any id in `claimed_elsewhere`,
    modelling "another application already holds this key" without querying
    an owner first -- there is nothing to query; the refusal *is* the signal
    (task 8.4). `pump` blocks on a queue fed by `fire`, `quit`, and the
    registrar's own `post_quit`, so `GetMessageW`'s real blocking wait is
    reproduced without a real message queue.
    """

    def __init__(self, claimed_elsewhere: frozenset[int] = frozenset()) -> None:
        self.claimed_elsewhere = claimed_elsewhere
        self.registered: list[int] = []
        self.unregistered: list[int] = []
        self.register_calls: list[tuple[int, int, int]] = []
        self._queue: "queue.Queue[object]" = queue.Queue()
        self._thread_id_counter = 0
        self._current_thread_id = 0
        self._lock = threading.Lock()

    def register(self, id_: int, modifiers: int, vk: int) -> bool:
        self.register_calls.append((id_, modifiers, vk))
        if id_ in self.claimed_elsewhere:
            return False
        self.registered.append(id_)
        return True

    def unregister(self, id_: int) -> None:
        self.unregistered.append(id_)

    def current_thread_id(self) -> int:
        with self._lock:
            self._thread_id_counter += 1
            self._current_thread_id = self._thread_id_counter
            return self._current_thread_id

    def pump(self):
        item = self._queue.get()
        if item == "quit":
            return _PumpQuit()
        if isinstance(item, int):
            return _PumpHotkey(id=item)
        return _PumpOther()

    def post_quit(self, thread_id: int) -> None:
        self._queue.put("quit")

    def fire(self, id_: int) -> None:
        self._queue.put(id_)


class RebindTests(unittest.TestCase):
    def _registrar(self, on_hotkey=None, **kwargs) -> tuple[WindowsHotkeyRegistrar, FakeWin32]:
        fake = FakeWin32(**{k: v for k, v in kwargs.items() if k == "claimed_elsewhere"})
        registrar = WindowsHotkeyRegistrar(
            on_hotkey=on_hotkey if on_hotkey is not None else (lambda purpose: None),
            register=fake.register,
            unregister=fake.unregister,
            pump=fake.pump,
            post_quit=fake.post_quit,
            current_thread_id=fake.current_thread_id,
            ready_timeout=2.0,
            stop_timeout=2.0,
        )
        return registrar, fake

    def test_no_thread_running_before_the_first_rebind(self) -> None:
        registrar, _fake = self._registrar()

        self.assertEqual(frozenset(), registrar.held_purposes())

    def test_rebind_registers_every_purpose_and_reports_it_held(self) -> None:
        registrar, fake = self._registrar()

        registrar.rebind({"window": "Meta+X", "session": "Meta+Shift+X"})

        self.assertEqual({"window", "session"}, registrar.held_purposes())
        self.assertEqual(2, len(fake.registered))
        registrar.stop()

    def test_rebind_with_an_empty_record_holds_nothing(self) -> None:
        registrar, _fake = self._registrar()

        registrar.rebind({})

        self.assertEqual(frozenset(), registrar.held_purposes())

    def test_collision_is_the_platforms_own_refusal_not_a_query(self) -> None:
        registrar, fake = self._registrar(claimed_elsewhere=frozenset({1}))

        with self.assertRaises(HotkeyError) as raised:
            registrar.rebind({"window": "Meta+X"})

        self.assertIn("already claimed", str(raised.exception))
        self.assertEqual(frozenset(), registrar.held_purposes())

    def test_a_collision_on_the_second_purpose_releases_the_first(self) -> None:
        # `rebind` registers purposes in sorted-key order ("session" before
        # "window"), so id 1 is "session" and id 2 is "window". Refusing id 2
        # proves the already-claimed id 1 is released rather than left bound.
        registrar, fake = self._registrar(claimed_elsewhere=frozenset({2}))

        with self.assertRaises(HotkeyError):
            registrar.rebind({"window": "Meta+X", "session": "Meta+Shift+X"})

        self.assertEqual(frozenset(), registrar.held_purposes())
        self.assertIn(1, fake.unregistered)

    def test_an_unencodable_key_is_refused_before_anything_is_registered(self) -> None:
        registrar, fake = self._registrar()

        with self.assertRaises(HotkeyError) as raised:
            registrar.rebind({"window": "Hyper+X"})

        self.assertIn("'window'", str(raised.exception))
        self.assertEqual([], fake.register_calls)

    def test_rebind_replaces_the_previous_registration_entirely(self) -> None:
        registrar, fake = self._registrar()

        registrar.rebind({"window": "Meta+X"})
        registrar.rebind({"session": "Meta+Shift+X"})

        self.assertEqual(frozenset({"session"}), registrar.held_purposes())
        # The first thread's own registration was released before the second
        # thread registered anything.
        self.assertEqual(1, len(fake.unregistered))
        registrar.stop()

    def test_no_repeat_is_folded_into_every_registration(self) -> None:
        from murmly.hotkey import WINDOWS_MOD_NOREPEAT

        registrar, fake = self._registrar()

        registrar.rebind({"window": "Meta+X"})

        [(_id, modifiers, _vk)] = fake.register_calls
        self.assertTrue(modifiers & WINDOWS_MOD_NOREPEAT)
        registrar.stop()


class HotkeyFiringTests(unittest.TestCase):
    def test_a_fired_hotkey_calls_the_handler_with_its_purpose(self) -> None:
        fired: list[str] = []
        fake = FakeWin32()
        registrar = WindowsHotkeyRegistrar(
            on_hotkey=fired.append,
            register=fake.register,
            unregister=fake.unregister,
            pump=fake.pump,
            post_quit=fake.post_quit,
            current_thread_id=fake.current_thread_id,
            ready_timeout=2.0,
            stop_timeout=2.0,
        )

        registrar.rebind({"window": "Meta+X"})
        fake.fire(1)
        registrar.stop()

        self.assertEqual(["window"], fired)

    def test_a_handler_that_raises_does_not_kill_the_loop(self) -> None:
        calls: list[str] = []

        def flaky(purpose: str) -> None:
            calls.append(purpose)
            raise RuntimeError("boom")

        fake = FakeWin32()
        registrar = WindowsHotkeyRegistrar(
            on_hotkey=flaky,
            register=fake.register,
            unregister=fake.unregister,
            pump=fake.pump,
            post_quit=fake.post_quit,
            current_thread_id=fake.current_thread_id,
            ready_timeout=2.0,
            stop_timeout=2.0,
        )

        registrar.rebind({"window": "Meta+X"})
        fake.fire(1)
        fake.fire(1)
        registrar.stop()

        self.assertEqual(["window", "window"], calls)


class StopTests(unittest.TestCase):
    def test_stop_with_no_thread_running_is_a_no_op(self) -> None:
        fake = FakeWin32()
        registrar = WindowsHotkeyRegistrar(
            on_hotkey=lambda purpose: None,
            register=fake.register,
            unregister=fake.unregister,
            pump=fake.pump,
            post_quit=fake.post_quit,
            current_thread_id=fake.current_thread_id,
        )

        registrar.stop()  # must not raise

        self.assertEqual(frozenset(), registrar.held_purposes())

    def test_stop_releases_every_held_purpose(self) -> None:
        fake = FakeWin32()
        registrar = WindowsHotkeyRegistrar(
            on_hotkey=lambda purpose: None,
            register=fake.register,
            unregister=fake.unregister,
            pump=fake.pump,
            post_quit=fake.post_quit,
            current_thread_id=fake.current_thread_id,
            ready_timeout=2.0,
            stop_timeout=2.0,
        )
        registrar.rebind({"window": "Meta+X", "session": "Meta+Shift+X"})

        registrar.stop()

        self.assertEqual(frozenset(), registrar.held_purposes())
        self.assertEqual({1, 2}, set(fake.unregistered))

    def test_stop_is_idempotent(self) -> None:
        fake = FakeWin32()
        registrar = WindowsHotkeyRegistrar(
            on_hotkey=lambda purpose: None,
            register=fake.register,
            unregister=fake.unregister,
            pump=fake.pump,
            post_quit=fake.post_quit,
            current_thread_id=fake.current_thread_id,
            ready_timeout=2.0,
            stop_timeout=2.0,
        )
        registrar.rebind({"window": "Meta+X"})

        registrar.stop()
        registrar.stop()  # must not raise, must not double-unregister

        self.assertEqual([1], fake.unregistered)


if __name__ == "__main__":
    unittest.main()
