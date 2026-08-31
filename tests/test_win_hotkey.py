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
import sys
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


class WindowsHotkeyRuntimeIntegrationTests(unittest.TestCase):
    """Task 8.3, against the real seams: `RegisterHotKey` on a real
    message-loop thread pumping real `GetMessageW`. Everything above this
    class proves the policy layer against fakes; this is the half that can
    only be confirmed on Windows, following the `X11RuntimeIntegrationTests`
    (`test_focus.py`) / `WindowsPipeSecurityDescriptorIntegrationTests`
    (`test_platform.py`) pattern of skipping in `setUp` on every platform but
    the one that has the mechanism.

    `Ctrl+Alt+Shift+F24` is chosen specifically as unlikely to already be
    claimed by another application on a CI runner -- a triple-modifier bind
    to the least-used function key Windows defines -- but it is still a real
    machine-wide registration, and `stop()` in `tearDown` releases it
    unconditionally so a failed assertion never leaves the key claimed
    against whatever runs next on the same machine (task 8.5).

    `RegisterHotKey` needs a message queue, which every thread gets
    implicitly, but whether it also needs a window station attached to an
    interactive desktop -- true of a service running outside any logged-on
    session -- is exactly the unknown this class exists to answer: a
    `HotkeyError` surfacing here as a skip, naming Win32's own
    `GetLastError`, is what tells a session's CI log apart from a session
    where this genuinely regressed.
    """

    PORTABLE = "Ctrl+Alt+Shift+F24"

    #: `VK_CONTROL`, `VK_MENU` (Alt), `VK_SHIFT`, `VK_F24` -- `winuser.h`.
    _CHORD_VKS = (0x11, 0x12, 0x10, 0x87)

    def setUp(self) -> None:
        if sys.platform != "win32":
            self.skipTest("A Windows kernel is required to call RegisterHotKey")
        self._registrar: WindowsHotkeyRegistrar | None = None

    def tearDown(self) -> None:
        if self._registrar is not None:
            self._registrar.stop()

    @classmethod
    def _send_chord(cls) -> None:
        """`SendInput` for every key in `_CHORD_VKS` down, then up in reverse.

        Standing in for a physical keypress of the same chord this class
        registers: proving delivery this way, rather than asking a person to
        press the combination during a CI run, is what lets this test run
        unattended.

        The structs come from `win_clipboard` rather than being declared here.
        This helper originally declared its own, and they carried the same
        defect the production ones did -- a union holding only `KEYBDINPUT`,
        which makes `sizeof(INPUT)` 32 on Windows where the real size is 40 --
        so when the product's copy was fixed this one was not, and the test
        went on skipping itself with `GetLastError=87` while reporting the
        runner as the reason. Two declarations of one Windows structure is
        how that happens; there is now one.

        `_real_send_ctrl_v` itself is still not reused: it is hard-coded to
        Ctrl+V, and this needs an arbitrary four-key chord ending in a
        function key it has no seam for.
        """
        import ctypes

        from murmly.win_clipboard import (
            INPUT_KEYBOARD,
            KEYEVENTF_KEYUP,
            _INPUT,
            _KEYBDINPUT,
            _user32,
        )

        def entry(vk: int, key_up: bool) -> "_INPUT":
            item = _INPUT()
            item.type = INPUT_KEYBOARD
            item.ki = _KEYBDINPUT(
                wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP if key_up else 0, time=0, dwExtraInfo=0
            )
            return item

        sequence = [entry(vk, key_up=False) for vk in cls._CHORD_VKS]
        sequence += [entry(vk, key_up=True) for vk in reversed(cls._CHORD_VKS)]
        array = (_INPUT * len(sequence))(*sequence)
        sent = _user32().SendInput(len(sequence), array, ctypes.sizeof(_INPUT))
        if sent != len(sequence):
            raise OSError(
                f"SendInput accepted {sent} of {len(sequence)} events; "
                f"GetLastError={ctypes.GetLastError()}"
            )

    def test_registers_fires_and_releases_a_real_hotkey(self) -> None:
        from ctypes import GetLastError

        fired: queue.Queue[str] = queue.Queue()
        registrar = WindowsHotkeyRegistrar(on_hotkey=fired.put)
        self._registrar = registrar

        try:
            registrar.rebind({"window": self.PORTABLE})
        except HotkeyError as error:
            last_error = GetLastError()
            self.skipTest(
                f"RegisterHotKey refused {self.PORTABLE!r} on this runner "
                f"(GetLastError={last_error}): {error}"
            )

        self.assertEqual(frozenset({"window"}), registrar.held_purposes())

        try:
            self._send_chord()
        except OSError as error:
            self.skipTest(f"SendInput could not inject the chord on this runner: {error}")

        try:
            purpose = fired.get(timeout=5.0)
        except queue.Empty:
            self.fail(
                "RegisterHotKey accepted the chord but no WM_HOTKEY was "
                "delivered within 5 seconds of injecting it."
            )
        self.assertEqual("window", purpose)

        registrar.stop()

        self.assertEqual(frozenset(), registrar.held_purposes())
        # Released, not merely forgotten: re-registering the same physical
        # key must succeed now that this process no longer holds it, which it
        # would not if `stop()` had merely dropped the bookkeeping without
        # calling `UnregisterHotKey`.
        registrar.rebind({"window": self.PORTABLE})
        self.assertEqual(frozenset({"window"}), registrar.held_purposes())


if __name__ == "__main__":
    unittest.main()
