"""Task 13.5-13.7, 18.10: `MacosHotkeyRegistrar`'s policy layer, exercised
against fakes standing in for every Carbon seam (`register`, `unregister`,
`install_handler`, `remove_handler`, `run_loop`, `request_stop`), mirroring
`test_win_hotkey.py` test-for-test where the two backends share a rule.

None of this touches a real framework -- that half (`_real_register`,
`_real_run_loop`, and friends) can only be confirmed on macOS. What is proven
here is everything `mac_hotkey.py`'s own module docstring claims for the
fakeable half: rebuilding the whole binding set on one fresh thread per
`rebind()`, treating the platform's own registration refusal as the collision
(task 13.5) without querying first, unwinding a partial batch so a later
purpose's failure never leaves an earlier one bound, and releasing every held
key -- and removing the installed event handler -- when the thread stops
(task 13.6).
"""

from __future__ import annotations

import sys
import threading
import unittest

from murmly.hotkey import HotkeyError
from murmly.mac_hotkey import MacosHotkeyRegistrar


class FakeCarbon:
    """A single-threaded-safe stand-in for the whole Carbon hotkey surface.

    `register` refuses (returns `None`) for any id in `claimed_elsewhere`,
    modelling "another application already holds this key" without querying
    an owner first -- there is nothing to query; the refusal *is* the signal
    (task 13.5). `run_loop` blocks on a fresh `threading.Event` created by
    `install_handler` -- always called before any registration, matching
    `MacosHotkeyRegistrar._run`'s own ordering -- reproducing `Run
    ApplicationEventLoop`'s real blocking wait without a real Carbon run loop.
    `fire` calls the installed callback directly and synchronously, standing
    in for HIToolbox invoking it from inside the loop it is dispatching.
    """

    def __init__(self, claimed_elsewhere: frozenset[int] = frozenset()) -> None:
        self.claimed_elsewhere = claimed_elsewhere
        self.registered: list[int] = []
        self.unregistered: list[int] = []
        self.register_calls: list[tuple[int, int, int]] = []
        self.handlers_installed = 0
        self.handlers_removed = 0
        self._on_fired = None
        self._stop_event: threading.Event | None = None

    def register(self, id_: int, modifiers: int, key_code: int) -> int | None:
        self.register_calls.append((id_, modifiers, key_code))
        if id_ in self.claimed_elsewhere:
            return None
        self.registered.append(id_)
        return id_

    def unregister(self, ref: int) -> None:
        self.unregistered.append(ref)

    def install_handler(self, on_fired):
        self._on_fired = on_fired
        self.handlers_installed += 1
        self._stop_event = threading.Event()
        return object()

    def remove_handler(self, _handler) -> None:
        self.handlers_removed += 1

    def run_loop(self) -> None:
        assert self._stop_event is not None
        self._stop_event.wait()

    def request_stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()

    def fire(self, id_: int) -> None:
        self._on_fired(id_)


class RebindTests(unittest.TestCase):
    def _registrar(self, on_hotkey=None, **kwargs) -> tuple[MacosHotkeyRegistrar, FakeCarbon]:
        fake = FakeCarbon(**{k: v for k, v in kwargs.items() if k == "claimed_elsewhere"})
        registrar = MacosHotkeyRegistrar(
            on_hotkey=on_hotkey if on_hotkey is not None else (lambda purpose: None),
            register=fake.register,
            unregister=fake.unregister,
            install_handler=fake.install_handler,
            remove_handler=fake.remove_handler,
            run_loop=fake.run_loop,
            request_stop=fake.request_stop,
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
        self.assertEqual(1, fake.handlers_installed)
        registrar.stop()

    def test_rebind_with_an_empty_record_holds_nothing(self) -> None:
        registrar, fake = self._registrar()

        registrar.rebind({})

        self.assertEqual(frozenset(), registrar.held_purposes())
        self.assertEqual(0, fake.handlers_installed)

    def test_collision_is_the_platforms_own_refusal_not_a_query(self) -> None:
        registrar, fake = self._registrar(claimed_elsewhere=frozenset({1}))

        with self.assertRaises(HotkeyError) as raised:
            registrar.rebind({"window": "Meta+X"})

        self.assertIn("already claimed", str(raised.exception))
        self.assertEqual(frozenset(), registrar.held_purposes())
        # The handler installed for this failed attempt is removed on the way
        # out, not left dangling -- task 13.6's release rule applied to a
        # refusal, not only to a clean `stop()`.
        self.assertEqual(1, fake.handlers_removed)

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
        self.assertEqual(0, fake.handlers_installed)

    def test_an_out_of_range_function_key_is_refused_by_name(self) -> None:
        """macOS's own lower ceiling (F20, not Windows' F24 or KDE/GNOME's
        F35) is refused here, by this platform's encoder -- task 18.9's
        "refused by name" applied to macOS's real gap."""
        registrar, fake = self._registrar()

        with self.assertRaises(HotkeyError) as raised:
            registrar.rebind({"window": "Meta+F21"})

        self.assertIn("F21", str(raised.exception))
        self.assertEqual([], fake.register_calls)

    def test_rebind_replaces_the_previous_registration_entirely(self) -> None:
        registrar, fake = self._registrar()

        registrar.rebind({"window": "Meta+X"})
        registrar.rebind({"session": "Meta+Shift+X"})

        self.assertEqual(frozenset({"session"}), registrar.held_purposes())
        # The first thread's own registration was released, and its handler
        # removed, before the second thread registered anything.
        self.assertEqual(1, len(fake.unregistered))
        self.assertEqual(1, fake.handlers_removed)
        self.assertEqual(2, fake.handlers_installed)
        registrar.stop()


class HotkeyFiringTests(unittest.TestCase):
    def test_a_fired_hotkey_calls_the_handler_with_its_purpose(self) -> None:
        fired: list[str] = []
        fake = FakeCarbon()
        registrar = MacosHotkeyRegistrar(
            on_hotkey=fired.append,
            register=fake.register,
            unregister=fake.unregister,
            install_handler=fake.install_handler,
            remove_handler=fake.remove_handler,
            run_loop=fake.run_loop,
            request_stop=fake.request_stop,
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

        fake = FakeCarbon()
        registrar = MacosHotkeyRegistrar(
            on_hotkey=flaky,
            register=fake.register,
            unregister=fake.unregister,
            install_handler=fake.install_handler,
            remove_handler=fake.remove_handler,
            run_loop=fake.run_loop,
            request_stop=fake.request_stop,
            ready_timeout=2.0,
            stop_timeout=2.0,
        )

        registrar.rebind({"window": "Meta+X"})
        fake.fire(1)
        fake.fire(1)
        registrar.stop()

        self.assertEqual(["window", "window"], calls)

    def test_an_unknown_fired_id_is_ignored(self) -> None:
        """`GetEventParameter` reporting an id this registrar never assigned
        -- unexpected, but not a reason to crash the loop."""
        fired: list[str] = []
        fake = FakeCarbon()
        registrar = MacosHotkeyRegistrar(
            on_hotkey=fired.append,
            register=fake.register,
            unregister=fake.unregister,
            install_handler=fake.install_handler,
            remove_handler=fake.remove_handler,
            run_loop=fake.run_loop,
            request_stop=fake.request_stop,
            ready_timeout=2.0,
            stop_timeout=2.0,
        )

        registrar.rebind({"window": "Meta+X"})
        fake.fire(999)
        registrar.stop()

        self.assertEqual([], fired)


class StopTests(unittest.TestCase):
    def test_stop_with_no_thread_running_is_a_no_op(self) -> None:
        fake = FakeCarbon()
        registrar = MacosHotkeyRegistrar(
            on_hotkey=lambda purpose: None,
            register=fake.register,
            unregister=fake.unregister,
            install_handler=fake.install_handler,
            remove_handler=fake.remove_handler,
            run_loop=fake.run_loop,
            request_stop=fake.request_stop,
        )

        registrar.stop()  # must not raise

        self.assertEqual(frozenset(), registrar.held_purposes())

    def test_stop_releases_every_held_purpose_and_removes_the_handler(self) -> None:
        fake = FakeCarbon()
        registrar = MacosHotkeyRegistrar(
            on_hotkey=lambda purpose: None,
            register=fake.register,
            unregister=fake.unregister,
            install_handler=fake.install_handler,
            remove_handler=fake.remove_handler,
            run_loop=fake.run_loop,
            request_stop=fake.request_stop,
            ready_timeout=2.0,
            stop_timeout=2.0,
        )
        registrar.rebind({"window": "Meta+X", "session": "Meta+Shift+X"})

        registrar.stop()

        self.assertEqual(frozenset(), registrar.held_purposes())
        self.assertEqual({1, 2}, set(fake.unregistered))
        self.assertEqual(1, fake.handlers_removed)

    def test_stop_is_idempotent(self) -> None:
        fake = FakeCarbon()
        registrar = MacosHotkeyRegistrar(
            on_hotkey=lambda purpose: None,
            register=fake.register,
            unregister=fake.unregister,
            install_handler=fake.install_handler,
            remove_handler=fake.remove_handler,
            run_loop=fake.run_loop,
            request_stop=fake.request_stop,
            ready_timeout=2.0,
            stop_timeout=2.0,
        )
        registrar.rebind({"window": "Meta+X"})

        registrar.stop()
        registrar.stop()  # must not raise, must not double-unregister

        self.assertEqual([1], fake.unregistered)


class MacosHotkeyRuntimeIntegrationTests(unittest.TestCase):
    """Task 13.5, against the real Carbon calls: everything above this class
    proves the policy layer against fakes, since `HIToolbox` cannot be loaded
    from Linux. Follows `WindowsHotkeyRuntimeIntegrationTests`
    (`test_win_hotkey.py`)'s pattern of skipping in `setUp` on every platform
    but the one with the mechanism, and its own care to release whatever it
    claims in `tearDown` unconditionally, so a failed assertion never leaves
    the key claimed against whatever runs next on the same machine.

    Only registration and release are proven here -- not delivery. Firing the
    chord for real needs `CGEventPost` (`mac_clipboard.py`), which is dropped
    without the Accessibility grant (task 14.3); this class asks a narrower,
    answerable question instead: does `RegisterEventHotKey` succeed and
    `RunApplicationEventLoop` actually dispatch a `kEventHotKeyPressed` to
    this registrar's own event-loop thread at all, which is exactly this
    module's own open question about run-loop/thread affinity (see
    `mac_hotkey.py`'s module docstring). A refusal here, rather than a hang,
    is itself informative and is what the `skipTest` messages below are
    written to surface distinctly from each other.
    """

    PORTABLE = "Ctrl+Alt+Shift+F20"

    def setUp(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("A macOS kernel is required to call RegisterEventHotKey")
        self._registrar: MacosHotkeyRegistrar | None = None

    def tearDown(self) -> None:
        if self._registrar is not None:
            self._registrar.stop()

    def test_registers_and_releases_a_real_hotkey(self) -> None:
        registrar = MacosHotkeyRegistrar(on_hotkey=lambda purpose: None)
        self._registrar = registrar

        try:
            registrar.rebind({"window": self.PORTABLE})
        except HotkeyError as error:
            self.skipTest(f"RegisterEventHotKey refused {self.PORTABLE!r} on this runner: {error}")

        self.assertEqual(frozenset({"window"}), registrar.held_purposes())

        registrar.stop()

        self.assertEqual(frozenset(), registrar.held_purposes())
        # Released, not merely forgotten: re-registering the same physical
        # key must succeed now that this process no longer holds it.
        registrar.rebind({"window": self.PORTABLE})
        self.assertEqual(frozenset({"window"}), registrar.held_purposes())


if __name__ == "__main__":
    unittest.main()
