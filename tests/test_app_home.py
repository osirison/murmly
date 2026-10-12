"""Home's logic, with no Qt: it runs on every CI job."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from channel_helpers import command_channel_address
from home_fakes import FakeChannel, delivered, refused
from murmly.app import home
from murmly.config import MurmlyConfig
from murmly.daemon import MurmlyDaemon, send_command
from test_daemon import DummySession, FakeOverlay, wait_until_served


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class PollerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.poller = home.Poller(clock=self.clock)

    def test_nothing_is_sent_while_nothing_is_visible(self) -> None:
        for _ in range(10):
            self.clock.now += 1
            self.assertFalse(self.poller.begin())

    def test_the_first_request_after_a_surface_appears_is_sent_at_once(self) -> None:
        self.poller.set_visible("window", True)
        self.assertTrue(self.poller.begin())

    def test_two_requests_are_never_closer_than_500_ms(self) -> None:
        self.poller.set_visible("window", True)
        starts = []
        for _ in range(400):
            if self.poller.begin():
                starts.append(self.clock.now)
                self.poller.finish()
            self.clock.now += 0.01
        self.assertGreater(len(starts), 5)
        gaps = [later - earlier for earlier, later in zip(starts, starts[1:])]
        self.assertGreaterEqual(min(gaps), home.STATUS_INTERVAL_S - 1e-9)
        self.assertLess(max(gaps), 0.52)

    def test_a_slow_answer_is_never_overtaken_by_another_request(self) -> None:
        self.poller.set_visible("window", True)
        self.assertTrue(self.poller.begin())
        self.clock.now += 5
        self.assertFalse(self.poller.begin())
        self.poller.finish()
        self.assertTrue(self.poller.begin())

    def test_hiding_stops_requests_and_showing_again_resumes_at_once(self) -> None:
        self.poller.set_visible("window", True)
        self.assertTrue(self.poller.begin())
        self.poller.finish()
        self.assertTrue(self.poller.set_visible("window", False))
        self.clock.now += 10
        self.assertFalse(self.poller.begin())
        self.assertTrue(self.poller.set_visible("window", True))
        self.assertTrue(self.poller.begin())

    def test_the_tray_panel_keeps_polling_while_the_window_is_hidden(self) -> None:
        self.poller.set_visible("window", True)
        self.poller.set_visible("tray", True)
        self.assertFalse(self.poller.set_visible("window", False))
        self.clock.now += 1
        self.assertTrue(self.poller.begin())
        self.poller.finish()
        self.assertTrue(self.poller.set_visible("tray", False))
        self.clock.now += 1
        self.assertFalse(self.poller.begin())


class PresentationTests(unittest.TestCase):
    def snapshot(self, state: str | None = "IDLE", words=None, reachable: bool = True) -> home.Snapshot:
        return home.Snapshot(
            reachable=reachable,
            state=state,
            last_words=home.LastWords.from_record(words) if words else None,
        )

    def test_each_daemon_state_maps_to_one_of_the_five_presentations(self) -> None:
        cases = {
            "ready": self.snapshot("IDLE"),
            "listening": self.snapshot("LISTENING"),
            "writing": self.snapshot("THINKING"),
            "needs_you": self.snapshot("IDLE", refused()),
            "not_running": self.snapshot(None, reachable=False),
        }
        self.assertEqual(set(home.PRESENTATIONS), set(cases))
        for expected, snapshot in cases.items():
            with self.subTest(expected):
                self.assertEqual(expected, home.present(snapshot))

    def test_speaking_counts_as_writing_and_says_so(self) -> None:
        snapshot = self.snapshot("SPEAKING")
        self.assertEqual(home.WRITING, home.present(snapshot))
        view = home.build_view(snapshot, now=datetime.now(timezone.utc))
        self.assertEqual("speaking", view["chip"])

    def test_a_delivered_transcript_is_ready_not_needs_you(self) -> None:
        self.assertEqual(home.READY, home.present(self.snapshot("IDLE", delivered())))

    def test_a_new_recording_takes_precedence_over_a_refusal(self) -> None:
        for state in ("LISTENING", "THINKING"):
            with self.subTest(state):
                self.assertNotEqual(home.NEEDS_YOU, home.present(self.snapshot(state, refused())))

    def test_an_acknowledged_refusal_is_ready_but_its_words_stay(self) -> None:
        snapshot = self.snapshot("IDLE", refused())
        dismissed = snapshot.last_words.produced_at
        self.assertEqual(home.READY, home.present(snapshot, dismissed=dismissed))
        view = home.build_view(snapshot, now=datetime.now(timezone.utc), dismissed=dismissed)
        self.assertEqual("Move the review to Thursday.", view["lastWords"]["text"])
        self.assertFalse(view["lastWords"]["delivered"])

    def test_the_detail_is_carried_through_byte_for_byte(self) -> None:
        detail = "  <b>No</b> & \u00e9 paste:\nsecond line \u2014 \t  "
        view = home.build_view(
            self.snapshot("IDLE", refused(detail=detail)), now=datetime.now(timezone.utc)
        )
        self.assertEqual(detail, view["lastWords"]["detail"])

    def test_the_refusal_view_reassures_before_anything_else(self) -> None:
        view = home.build_view(self.snapshot("IDLE", refused()), now=datetime.now(timezone.utc))
        self.assertEqual("needs_you", view["state"])
        self.assertIn("clipboard", view["headline"])

    def test_the_last_words_repr_does_not_contain_the_transcript(self) -> None:
        words = home.LastWords.from_record(delivered("a secret sentence"))
        self.assertNotIn("secret", repr(words))

    def test_relative_times(self) -> None:
        now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
        for seconds, expected in (
            (0, "0 s ago"),
            (12, "12 s ago"),
            (60, "1 minute ago"),
            (120, "2 minutes ago"),
            (3600, "1 hour ago"),
            (86400 * 3, "3 days ago"),
        ):
            with self.subTest(seconds=seconds):
                then = (now - timedelta(seconds=seconds)).isoformat()
                self.assertEqual(expected, home.relative_time(then, now))
        self.assertEqual("", home.relative_time("not a time", now))


class KeysAndSetupTests(unittest.TestCase):
    def test_keys_are_split_and_say_who_holds_them(self) -> None:
        cards = home.keys_view({"window": "Meta+X"}, held=("window",))
        self.assertEqual(["Meta", "X"], cards[0]["keys"])
        self.assertEqual("registered \u00b7 held by murmly", cards[0]["note"])
        self.assertEqual([], cards[1]["keys"])
        self.assertEqual("not set", cards[1]["note"])

    def test_a_key_the_daemon_does_not_hold_is_not_called_registered(self) -> None:
        cards = home.keys_view({"window": "Ctrl+Alt+D"}, held=())
        self.assertEqual(["Ctrl", "Alt", "D"], cards[0]["keys"])
        self.assertNotIn("registered", cards[0]["note"])

    def test_setup_shows_a_plain_value_beside_the_raw_setting(self) -> None:
        config = MurmlyConfig(
            socket_path=Path("s"),
            config_path=Path("c.toml"),
            device="cuda",
            compute_type="float16",
            speaker_mode="mine-only",
        )
        tiles = {tile["label"]: tile for tile in home.setup_view(config)}
        self.assertEqual("Balanced", tiles["Accuracy"]["value"])
        self.assertEqual("large-v3-turbo", tiles["Accuracy"]["note"])
        self.assertEqual("Graphics card", tiles["Runs on"]["value"])
        self.assertEqual("cuda \u00b7 float16", tiles["Runs on"]["note"])
        self.assertEqual("after 5 min", tiles["Memory"]["note"])
        self.assertEqual("auto_transcribe = off", tiles["Finishing"]["note"])
        self.assertEqual("Only your voice", tiles["Voices"]["value"])
        self.assertEqual([], home.setup_view(None))


class ModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.channel = FakeChannel()
        self.model = home.HomeModel(self.channel)

    def test_the_last_words_are_read_when_home_opens_not_on_every_poll(self) -> None:
        self.channel.last_words = delivered()
        for _ in range(5):
            snapshot = self.model.refresh()
        self.assertEqual(1, self.channel.count("last_words"))
        self.assertEqual("Move the review to Thursday.", snapshot.last_words.text)

    def test_they_are_read_again_when_a_busy_state_returns_to_idle(self) -> None:
        self.model.refresh()
        self.channel.state = "LISTENING"
        self.model.refresh()
        self.channel.state = "THINKING"
        self.model.refresh()
        self.assertEqual(1, self.channel.count("last_words"))
        self.channel.last_words = refused()
        self.channel.state = "IDLE"
        snapshot = self.model.refresh()
        self.assertEqual(2, self.channel.count("last_words"))
        self.assertFalse(snapshot.last_words.delivered)

    def test_a_capture_seen_once_between_polls_still_refreshes_them(self) -> None:
        self.model.refresh()
        self.channel.state = "THINKING"
        self.model.refresh()
        self.channel.last_words = delivered()
        self.channel.state = "IDLE"
        self.assertEqual("Move the review to Thursday.", self.model.refresh().last_words.text)

    def test_an_unreachable_daemon_reports_the_raw_error_and_no_words(self) -> None:
        self.channel.last_words = delivered()
        self.model.refresh()
        self.channel.failure = FileNotFoundError("[Errno 2] No such file or directory")
        snapshot = self.model.refresh()
        self.assertFalse(snapshot.reachable)
        self.assertEqual("[Errno 2] No such file or directory", snapshot.error)
        self.assertIsNone(snapshot.last_words)
        self.assertEqual(home.NOT_RUNNING, home.present(snapshot))

    def test_the_words_are_read_again_when_the_daemon_comes_back(self) -> None:
        self.channel.last_words = delivered()
        self.model.refresh()
        self.channel.failure = ConnectionRefusedError("refused")
        self.model.refresh()
        self.channel.failure = None
        self.channel.last_words = None
        self.assertIsNone(self.model.refresh().last_words)

    def test_clear_asks_the_daemon_and_the_words_are_gone(self) -> None:
        self.channel.last_words = delivered()
        self.assertIsNotNone(self.model.refresh().last_words)
        snapshot = self.model.clear_last_words()
        self.assertEqual(1, self.channel.count("clear_last_words"))
        self.assertIsNone(snapshot.last_words)
        self.assertIsNone(self.channel.last_words)

    def test_reload_reads_them_again_on_the_next_refresh(self) -> None:
        self.model.refresh()
        self.channel.last_words = delivered()
        self.model.refresh()
        self.model.reload()
        self.assertIsNotNone(self.model.refresh().last_words)

    def test_a_refused_request_is_not_reachable(self) -> None:
        model = home.HomeModel(lambda command: {"ok": False, "error": "nope"})
        snapshot = model.refresh()
        self.assertFalse(snapshot.reachable)
        self.assertEqual("nope", snapshot.error)


class FakeService:
    def __init__(self, installed: bool = True, started: bool = True, error: Exception | None = None) -> None:
        self.is_installed = installed
        self.started = started
        self.error = error
        self.starts = 0

    def start(self) -> bool:
        self.starts += 1
        if self.error is not None:
            raise self.error
        return self.started


class StartServiceTests(unittest.TestCase):
    def test_an_installed_service_is_started(self) -> None:
        service = FakeService()
        self.assertTrue(home.start_installed_service(service).ok)
        self.assertEqual(1, service.starts)

    def test_a_missing_service_says_to_install_it_and_starts_nothing(self) -> None:
        service = FakeService(installed=False)
        result = home.start_installed_service(service)
        self.assertFalse(result.ok)
        self.assertEqual(home.NOT_INSTALLED_TEXT, result.detail)
        self.assertEqual(0, service.starts)

    def test_a_failed_or_raising_start_is_reported_not_raised(self) -> None:
        self.assertFalse(home.start_installed_service(FakeService(started=False)).ok)
        result = home.start_installed_service(FakeService(error=RuntimeError("boom")))
        self.assertFalse(result.ok)
        self.assertIn("boom", result.detail)


class NoSocketTests(unittest.TestCase):
    def test_with_no_socket_home_says_the_service_is_not_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            address = command_channel_address(temp_dir)
            snapshot = home.HomeModel(home.default_send(str(address))).refresh()
        self.assertFalse(snapshot.reachable)
        self.assertEqual(home.NOT_RUNNING, home.present(snapshot))
        view = home.build_view(snapshot, now=datetime.now(timezone.utc))
        self.assertEqual("service not running", view["chip"])
        self.assertTrue(view["service"]["reason"])


class RealDaemonTests(unittest.TestCase):
    """The polling loop against a real daemon on its real command channel."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        address = command_channel_address(self.temp.name)
        config = MurmlyConfig(
            socket_path=address,
            config_path=Path(self.temp.name) / "config.toml",
            overlay_enabled=False,
        )
        self.daemon = MurmlyDaemon(config, session=DummySession(), overlay=FakeOverlay())
        thread = threading.Thread(target=self.daemon.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.daemon.shutdown)
        wait_until_served(self, self.daemon)
        self.sent: list[tuple[float, str]] = []
        send = home.default_send(str(address))

        def counting(command: str):
            self.sent.append((time.monotonic(), command))
            return send(command)

        self.model = home.HomeModel(counting)
        self.poller = home.Poller()
        self.snapshot: home.Snapshot | None = None
        self.stop = threading.Event()
        self.driver = threading.Thread(target=self._drive, daemon=True)
        self.driver.start()
        self.addCleanup(self.driver.join, 2)
        self.addCleanup(self.stop.set)

    def _drive(self) -> None:
        # What the controller's timer and worker do, without Qt.
        while not self.stop.is_set():
            if self.poller.begin():
                self.snapshot = self.model.refresh()
                self.poller.finish()
            time.sleep(0.01)

    def wait_for(self, key: str, timeout: float) -> float:
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            snapshot = self.snapshot
            if snapshot is not None and home.present(snapshot) == key:
                return time.monotonic() - started
            time.sleep(0.01)
        self.fail(f"Home never showed {key!r}; last saw {self.snapshot!r}")

    def test_a_state_change_appears_within_one_second(self) -> None:
        self.poller.set_visible("window", True)
        self.wait_for(home.READY, 2)

        self.daemon.handle_command("toggle")
        self.assertLess(self.wait_for(home.LISTENING, 1.0), 1.0)

        self.daemon.handle_command("toggle")
        self.wait_for(home.READY, 1.0)
        # The transcript the daemon just produced is what Home now shows.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and (self.snapshot.last_words is None):
            time.sleep(0.01)
        self.assertEqual("hello world", self.snapshot.last_words.text)

    def test_polling_stops_when_hidden_and_is_never_faster_than_500_ms(self) -> None:
        self.poller.set_visible("window", True)
        self.wait_for(home.READY, 2)
        time.sleep(1.3)
        statuses = [at for at, command in list(self.sent) if command == "status"]
        self.assertGreaterEqual(len(statuses), 3)
        gaps = [later - earlier for earlier, later in zip(statuses, statuses[1:])]
        self.assertGreaterEqual(min(gaps), home.STATUS_INTERVAL_S - 0.02)

        self.poller.set_visible("window", False)
        time.sleep(0.1)  # a request already on its way may still land
        before = len(self.sent)
        time.sleep(1.3)
        self.assertEqual(before, len(self.sent))

    def test_the_daemon_going_away_shows_not_running(self) -> None:
        self.poller.set_visible("window", True)
        self.wait_for(home.READY, 2)
        self.daemon.shutdown()
        self.wait_for(home.NOT_RUNNING, 3)


if __name__ == "__main__":
    unittest.main()
