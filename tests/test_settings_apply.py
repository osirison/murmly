from __future__ import annotations

import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from murmly.daemon import DaemonNotRespondingError
from murmly.installer import InstallError, LaunchdUserService, UserService, WindowsUserService
from murmly.platform import OperatingSystem, PlatformProfile
from murmly.settings_apply import (
    MIN_POLL_INTERVAL_SECONDS,
    RestartStatus,
    RestartWhenIdle,
    default_restart,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeDaemon:
    """Answers `status` from a script, one reply per request, then the last again."""

    def __init__(self, *replies: object) -> None:
        self.replies = list(replies)
        self.asked = 0

    def __call__(self) -> dict[str, object]:
        self.asked += 1
        reply = self.replies[min(self.asked, len(self.replies)) - 1]
        if isinstance(reply, BaseException):
            raise reply
        return reply  # type: ignore[return-value]


def idle() -> dict[str, object]:
    return {"ok": True, "state": "IDLE", "restart_ready": True}


def busy(state: str = "LISTENING") -> dict[str, object]:
    return {"ok": True, "state": state, "restart_ready": False}


class RestartWhenIdleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.restarts = 0

    def restart(self) -> bool:
        self.restarts += 1
        return True

    def applier(self, daemon: FakeDaemon, restart=None) -> RestartWhenIdle:
        return RestartWhenIdle(
            status=daemon,
            restart=restart or self.restart,
            clock=self.clock,
            sleep=self.clock.sleep,
        )

    def test_a_save_while_idle_restarts_at_once(self) -> None:
        applier = self.applier(FakeDaemon(idle()))

        applier.request()
        outcome = applier.tick()

        self.assertEqual(RestartStatus.RESTARTED, outcome.status)
        self.assertEqual(1, self.restarts)
        self.assertFalse(applier.pending)

    def test_nothing_is_asked_or_restarted_without_a_save(self) -> None:
        daemon = FakeDaemon(idle())
        applier = self.applier(daemon)

        self.assertEqual(RestartStatus.NOTHING_PENDING, applier.tick().status)
        self.assertEqual(0, daemon.asked)
        self.assertEqual(0, self.restarts)

    def test_a_save_during_capture_waits_then_restarts_once_idle(self) -> None:
        applier = self.applier(FakeDaemon(busy("LISTENING"), busy("THINKING"), idle()))
        applier.request()

        self.assertEqual(RestartStatus.WAITING, applier.tick().status)
        self.assertEqual(0, self.restarts)
        self.clock.now += 0.5
        self.assertEqual(RestartStatus.WAITING, applier.tick().status)
        self.assertEqual(0, self.restarts)
        self.clock.now += 0.5
        self.assertEqual(RestartStatus.RESTARTED, applier.tick().status)
        self.assertEqual(1, self.restarts)

    def test_every_busy_state_waits(self) -> None:
        for state in ("LISTENING", "THINKING", "SPEAKING"):
            with self.subTest(state=state):
                self.restarts = 0
                applier = self.applier(FakeDaemon(busy(state)))
                applier.request()

                self.assertEqual(RestartStatus.WAITING, applier.tick().status)
                self.assertEqual(0, self.restarts)

    def test_several_saves_while_busy_give_one_restart(self) -> None:
        applier = self.applier(FakeDaemon(busy(), busy(), busy(), idle()))
        for _ in range(3):
            applier.request()
            applier.tick()
            self.clock.now += 0.5
        applier.request()

        self.assertEqual(RestartStatus.RESTARTED, applier.tick().status)
        self.clock.now += 0.5
        self.assertEqual(RestartStatus.NOTHING_PENDING, applier.tick().status)
        self.assertEqual(1, self.restarts)

    def test_a_save_after_a_restart_is_a_new_restart(self) -> None:
        applier = self.applier(FakeDaemon(idle()))

        applier.request()
        applier.tick()
        applier.request()
        applier.tick()

        self.assertEqual(2, self.restarts)

    def test_the_daemon_is_not_asked_more_often_than_every_500_ms(self) -> None:
        daemon = FakeDaemon(busy())
        applier = self.applier(daemon)
        applier.request()

        for _ in range(50):
            applier.tick()
            self.clock.now += 0.01

        self.assertLessEqual(daemon.asked, 2)
        self.assertEqual(0.5, MIN_POLL_INTERVAL_SECONDS)

    def test_a_faster_poll_interval_is_raised_to_the_floor(self) -> None:
        daemon = FakeDaemon(busy())
        applier = RestartWhenIdle(
            status=daemon, restart=self.restart, clock=self.clock, sleep=self.clock.sleep,
            poll_interval=0.01,
        )
        applier.request()
        applier.tick()
        self.clock.now += 0.1
        applier.tick()

        self.assertEqual(1, daemon.asked)

    def test_run_sleeps_between_asks_until_idle(self) -> None:
        applier = self.applier(FakeDaemon(busy(), busy(), busy(), idle()))
        applier.request()

        outcome = applier.run()

        self.assertEqual(RestartStatus.RESTARTED, outcome.status)
        self.assertEqual([0.5, 0.5, 0.5], self.clock.sleeps)
        self.assertEqual(1, self.restarts)

    def test_run_gives_up_waiting_at_its_timeout_and_keeps_the_restart_pending(self) -> None:
        applier = self.applier(FakeDaemon(busy()))
        applier.request()

        outcome = applier.run(timeout=2)

        self.assertEqual(RestartStatus.WAITING, outcome.status)
        self.assertEqual(0, self.restarts)
        self.assertTrue(applier.pending)

    def test_no_daemon_listening_restarts_anyway(self) -> None:
        for error in (FileNotFoundError("no socket"), ConnectionRefusedError("refused")):
            with self.subTest(error=type(error).__name__):
                self.restarts = 0
                applier = self.applier(FakeDaemon(error))
                applier.request()

                self.assertEqual(RestartStatus.RESTARTED, applier.tick().status)
                self.assertEqual(1, self.restarts)

    def test_a_daemon_that_does_not_answer_is_treated_as_busy(self) -> None:
        applier = self.applier(FakeDaemon(DaemonNotRespondingError("slow")))
        applier.request()

        self.assertEqual(RestartStatus.WAITING, applier.tick().status)
        self.assertEqual(0, self.restarts)

    def test_an_unclear_connection_error_is_treated_as_busy(self) -> None:
        applier = self.applier(FakeDaemon(ConnectionResetError("reset")))
        applier.request()

        self.assertEqual(RestartStatus.WAITING, applier.tick().status)
        self.assertEqual(0, self.restarts)

    def test_an_old_daemon_without_restart_ready_is_idle_only_in_the_idle_state(self) -> None:
        applier = self.applier(FakeDaemon({"ok": True, "state": "LISTENING"}))
        applier.request()
        self.assertEqual(RestartStatus.WAITING, applier.tick().status)
        self.assertEqual(0, self.restarts)

        applier = self.applier(FakeDaemon({"ok": True, "state": "IDLE"}))
        applier.request()
        self.assertEqual(RestartStatus.RESTARTED, applier.tick().status)
        self.assertEqual(1, self.restarts)

    def test_restart_ready_wins_over_state(self) -> None:
        # A daemon speaking reports IDLE with speech playing; the field is what
        # says a restart would cut something off.
        applier = self.applier(FakeDaemon({"ok": True, "state": "IDLE", "restart_ready": False}))
        applier.request()

        self.assertEqual(RestartStatus.WAITING, applier.tick().status)
        self.assertEqual(0, self.restarts)

    def test_a_failed_restart_is_reported_and_not_retried_in_a_loop(self) -> None:
        def explode() -> bool:
            self.restarts += 1
            raise InstallError("systemctl --user restart failed: no such unit")

        applier = self.applier(FakeDaemon(idle()), restart=explode)
        applier.request()

        outcome = applier.tick()

        self.assertEqual(RestartStatus.FAILED, outcome.status)
        self.assertIn("no such unit", outcome.detail)
        self.assertFalse(applier.pending)
        self.assertEqual(RestartStatus.NOTHING_PENDING, applier.tick().status)
        self.assertEqual(1, self.restarts)

    def test_a_restart_that_returns_false_is_a_failure(self) -> None:
        applier = self.applier(FakeDaemon(idle()), restart=lambda: False)
        applier.request()

        self.assertEqual(RestartStatus.FAILED, applier.tick().status)

    def test_without_a_config_or_a_status_function_it_refuses_to_start(self) -> None:
        with self.assertRaises(ValueError):
            RestartWhenIdle(restart=self.restart)


class SilentDaemonTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "needs a UNIX socket")
    def test_a_daemon_that_accepts_and_never_answers_does_not_hold_the_poll(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = str(Path(directory.name) / "d.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(path)
        server.listen(4)
        held: list[socket.socket] = []

        def accept() -> None:
            try:
                while True:
                    held.append(server.accept()[0])
            except OSError:
                pass

        threading.Thread(target=accept, daemon=True).start()
        self.addCleanup(lambda: [connection.close() for connection in held])
        restarts: list[int] = []
        applier = RestartWhenIdle(
            SimpleNamespace(socket_path=path), restart=lambda: restarts.append(1) or True
        )
        applier.request()
        outcomes: list[object] = []
        poll = threading.Thread(target=lambda: outcomes.append(applier.tick()), daemon=True)

        started = time.monotonic()
        poll.start()
        poll.join(5.0)

        self.assertFalse(poll.is_alive(), "the poll is still waiting for an answer")
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(RestartStatus.WAITING, outcomes[0].status)
        self.assertEqual([], restarts)
        self.assertTrue(applier.pending)


class DefaultRestartTests(unittest.TestCase):
    def restart_for(self, operating_system: OperatingSystem):
        return default_restart(PlatformProfile(operating_system=operating_system, architecture="x86_64"))

    def test_linux_restarts_the_systemd_unit(self) -> None:
        restart = self.restart_for(OperatingSystem.LINUX)

        self.assertIs(UserService, type(restart.__self__))
        self.assertEqual("restart", restart.__name__)

    def test_windows_restarts_the_scheduled_task(self) -> None:
        restart = self.restart_for(OperatingSystem.WINDOWS)

        self.assertIs(WindowsUserService, type(restart.__self__))
        self.assertEqual("restart", restart.__name__)

    def test_macos_restarts_the_launchd_agent(self) -> None:
        restart = self.restart_for(OperatingSystem.MACOS)

        self.assertIs(LaunchdUserService, type(restart.__self__))
        self.assertEqual("restart", restart.__name__)


if __name__ == "__main__":
    unittest.main()
