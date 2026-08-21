from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from fakes import FakeSynthesizer, expected_audio, split_for_fake
from murmly.config import MurmlyConfig
from murmly.speech import (
    EVENT_HEARD_ALL,
    EVENT_STARTED,
    LOOKAHEAD_SECONDS,
    SpeechEngine,
    SpeechQueue,
    SpeechUnit,
)


class RecordingPlayer:
    """A `SoundDevicePlayer` stand-in whose playback the test advances itself.

    Nothing here is timing dependent: `play(frames)` is the only thing that
    moves the played position, so a test can put the frontier exactly where a
    scenario needs it.
    """

    def __init__(self, sample_rate_hz: int = 24_000, start_error: Exception | None = None) -> None:
        self.sample_rate_hz = sample_rate_hz
        self.frames_written = 0
        self.frames_played = 0
        self.started = 0
        self.stopped = 0
        self.aborted = 0
        self.active = False
        self.written: list[tuple[int, int]] = []
        self._start_error = start_error

    def start(self) -> None:
        if self._start_error is not None:
            raise self._start_error
        self.started += 1
        self.active = True

    def stop(self) -> None:
        self.stopped += 1
        self.active = False

    def abort(self) -> int:
        self.aborted += 1
        self.frames_written = self.frames_played
        return self.frames_played

    def write(self, samples, sample_rate_hz: int) -> int:
        frames = len(samples)
        self.frames_written += frames
        self.written.append((frames, sample_rate_hz))
        return frames

    @property
    def pending_frames(self) -> int:
        return max(self.frames_written - self.frames_played, 0)

    def play(self, frames: int | None = None) -> None:
        """Let the device consume what is queued, or `frames` of it."""
        available = self.pending_frames if frames is None else min(frames, self.pending_frames)
        self.frames_played += available


class EngineHarness(unittest.TestCase):
    def engine(self, **overrides) -> tuple[SpeechEngine, RecordingPlayer, list[dict]]:
        temp_dir = tempfile.mkdtemp()
        config = MurmlyConfig(
            socket_path=Path(temp_dir) / "murmly.sock",
            config_path=Path(temp_dir) / "config.toml",
            tts_enabled=True,
        )
        player = overrides.pop("player", None) or RecordingPlayer()
        synthesizer = overrides.pop("synthesizer", None) or FakeSynthesizer()
        engine = SpeechEngine(config, synthesizer=synthesizer, player=player)
        events: list[dict] = []
        lock = threading.Lock()

        def sink(event: dict) -> None:
            with lock:
                events.append(event)

        self.addCleanup(engine.end)
        return engine, player, events

    def wait_for(self, predicate, timeout: float = 3.0, message: str = "condition") -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail(f"timed out waiting for {message}")

    def begin(self, engine: SpeechEngine, events: list[dict]):
        lock = threading.Lock()

        def sink(event: dict) -> None:
            with lock:
                events.append(event)

        engine.begin(sink)
        return sink

    @staticmethod
    def names(events: list[dict], kind: str) -> list[str]:
        return [event.get("name") for event in events if event.get("event") == kind]


class SpeechQueueTests(unittest.TestCase):
    def test_units_come_back_in_the_order_they_were_sent(self) -> None:
        queue = SpeechQueue()
        for name in ("a", "b", "c"):
            queue.send(SpeechUnit(name=name, text=f"{name}."))

        taken = [queue.take(0.01).name for _ in range(3)]

        self.assertEqual(["a", "b", "c"], taken)

    def test_an_empty_queue_is_not_an_ended_one(self) -> None:
        """The distinction the end-of-input marker exists for."""
        queue = SpeechQueue()

        self.assertIsNone(queue.take(0.01))
        self.assertFalse(queue.input_ended)

        queue.end_input()
        self.assertIsNone(queue.take(0.01))
        self.assertTrue(queue.input_ended)

    def test_discard_reports_what_it_dropped(self) -> None:
        queue = SpeechQueue()
        queue.send(SpeechUnit("a", "A."))
        queue.send(SpeechUnit("b", "B."))

        self.assertEqual(["a", "b"], queue.discard())
        self.assertEqual([], queue.waiting)

    def test_text_sent_after_the_marker_is_still_queued(self) -> None:
        """A sender that says more after ending is not silently dropped."""
        queue = SpeechQueue()
        queue.end_input()
        queue.send(SpeechUnit("late", "Late."))

        self.assertEqual("late", queue.take(0.01).name)


class PlaybackTests(EngineHarness):
    def test_text_is_spoken_in_the_order_it_was_sent(self) -> None:
        engine, player, events = self.engine()
        self.begin(engine, events)

        for name in ("one", "two", "three"):
            engine.speak(name, f"Sentence {name}.")
        self.wait_for(lambda: len(player.written) >= 3, message="three units produced")
        player.play()
        self.wait_for(
            lambda: len(self.names(events, EVENT_STARTED)) == 3, message="three started events"
        )

        self.assertEqual(["one", "two", "three"], self.names(events, EVENT_STARTED))

    def test_a_unit_is_reported_started_only_once_it_is_audible(self) -> None:
        """Never at production time: production runs whole sentences ahead."""
        engine, player, events = self.engine()
        self.begin(engine, events)

        engine.speak("first", "The first sentence.")
        self.wait_for(lambda: player.frames_written > 0, message="audio produced")
        time.sleep(0.05)

        self.assertEqual([], self.names(events, EVENT_STARTED), "reported before anything played")

        player.play()
        self.wait_for(
            lambda: self.names(events, EVENT_STARTED) == ["first"], message="started event"
        )

    def test_the_position_never_names_a_unit_that_was_only_produced(self) -> None:
        engine, player, events = self.engine()
        self.begin(engine, events)

        engine.speak("heard", "A short one.")
        engine.speak("unheard", "A second one that is never played.")
        self.wait_for(lambda: len(player.written) >= 2, message="both units produced")

        first_frames = player.written[0][0]
        player.play(first_frames)
        self.wait_for(
            lambda: self.names(events, EVENT_STARTED) == ["heard"], message="the first started"
        )
        time.sleep(0.05)

        self.assertEqual(["heard"], self.names(events, EVENT_STARTED))

    def test_everything_heard_is_reported_only_after_the_sender_says_it_is_finished(self) -> None:
        engine, player, events = self.engine()
        self.begin(engine, events)

        engine.speak("only", "The only thing.")
        self.wait_for(lambda: player.frames_written > 0, message="audio produced")
        player.play()
        time.sleep(0.05)

        self.assertEqual([], [e for e in events if e.get("event") == EVENT_HEARD_ALL])

        engine.end_input()
        self.wait_for(
            lambda: any(e.get("event") == EVENT_HEARD_ALL for e in events),
            message="the heard-all event",
        )

    def test_a_queue_that_empties_before_the_marker_speaks_what_comes_next(self) -> None:
        engine, player, events = self.engine()
        self.begin(engine, events)

        engine.speak("first", "First.")
        self.wait_for(lambda: player.frames_written > 0, message="the first unit")
        player.play()
        self.wait_for(lambda: self.names(events, EVENT_STARTED) == ["first"], message="first started")

        engine.speak("second", "Second.")
        self.wait_for(lambda: len(player.written) >= 2, message="the second unit")
        player.play()
        self.wait_for(
            lambda: self.names(events, EVENT_STARTED) == ["first", "second"],
            message="second started",
        )

    def test_the_producer_does_not_run_away_with_the_whole_text(self) -> None:
        """A position taken from production would otherwise be the whole passage."""
        engine, player, events = self.engine()
        self.begin(engine, events)
        lookahead = int(LOOKAHEAD_SECONDS * player.sample_rate_hz)

        text = " ".join(
            ["A sentence of a length that makes the arithmetic here plain enough."] * 3
        )
        # Sized from the fake's own output rather than guessed. A passage that
        # cannot exceed the lookahead never enters the throttle at all, and the
        # assertion below then holds for an engine with no throttle whatsoever.
        self.assertGreater(
            40 * len(expected_audio(text)),
            2 * lookahead,
            "the passage must be able to exceed the lookahead, or this cannot fail",
        )
        for index in range(40):
            engine.speak(f"unit{index}", text)
        self.wait_for(
            lambda: player.pending_frames > lookahead, message="the lookahead to be reached"
        )
        time.sleep(0.3)

        self.assertLessEqual(
            player.pending_frames,
            lookahead + player.written[-1][0],
            "the producer ran further ahead than the lookahead allows",
        )

    def test_a_unit_that_fails_to_produce_does_not_end_the_session(self) -> None:
        engine, player, events = self.engine(
            synthesizer=FakeSynthesizer(error=RuntimeError("model exploded"))
        )
        self.begin(engine, events)

        engine.speak("doomed", "This will fail.")
        self.wait_for(
            lambda: any(e.get("event") == "failed" for e in events), message="a failure event"
        )

        self.assertTrue(engine.active)


class InterruptionTests(EngineHarness):
    def test_the_interruption_names_what_was_playing_and_what_never_started(self) -> None:
        engine, player, events = self.engine()
        self.begin(engine, events)

        for name in ("one", "two", "three", "four"):
            engine.speak(name, f"Sentence number {name} here.")
        self.wait_for(lambda: len(player.written) >= 2, message="two units produced")
        player.play(player.written[0][0] + 1)
        self.wait_for(
            lambda: self.names(events, EVENT_STARTED) == ["one", "two"], message="the second started"
        )

        interruption = engine.interrupt()

        self.assertEqual("two", interruption.playing)
        self.assertEqual(("three", "four"), interruption.pending)

    def test_the_position_is_what_was_heard_not_what_was_produced(self) -> None:
        engine, player, events = self.engine()
        self.begin(engine, events)

        for name in ("one", "two", "three"):
            engine.speak(name, f"Sentence {name}.")
        self.wait_for(lambda: len(player.written) >= 3, message="all three produced")
        player.play(1)

        interruption = engine.interrupt()

        self.assertEqual("one", interruption.playing, "named a unit that had only been produced")
        self.assertEqual(("two", "three"), interruption.pending)

    def test_nothing_left_unheard_is_reported_as_nothing(self) -> None:
        engine, player, events = self.engine()
        self.begin(engine, events)

        engine.speak("only", "The only sentence.")
        self.wait_for(lambda: player.frames_written > 0, message="audio produced")
        player.play()
        self.wait_for(
            lambda: self.names(events, EVENT_STARTED) == ["only"], message="the started event"
        )

        interruption = engine.interrupt()

        self.assertTrue(interruption.nothing_unheard)
        self.assertIsNone(interruption.playing)
        self.assertEqual((), interruption.pending)

    def test_interrupting_stops_audio_already_handed_to_the_device(self) -> None:
        engine, player, events = self.engine()
        self.begin(engine, events)
        engine.speak("long", "One. Two. Three. Four. Five.")
        self.wait_for(lambda: player.pending_frames > 0, message="audio queued")

        engine.interrupt()

        self.assertEqual(1, player.aborted)
        self.assertEqual(0, player.pending_frames)

    def test_a_late_synthesis_result_is_discarded(self) -> None:
        """A pass already inside the model cannot be interrupted, so it is dropped.

        The producer is parked inside the model by the gate rather than by a
        sleep. Without it the fake finishes all seven sentences before the
        interrupt lands, and the test passes without a discard ever happening.
        """
        gate = threading.Event()
        self.addCleanup(gate.set)
        engine, player, events = self.engine(synthesizer=FakeSynthesizer(gate=gate))
        self.begin(engine, events)
        engine.speak("first", "One. Two. Three. Four. Five. Six. Seven.")
        self.wait_for(lambda: player.frames_written > 0, message="the first sentence")

        engine.interrupt()
        written_at_interrupt = player.frames_written
        gate.set()
        time.sleep(0.1)

        self.assertEqual(written_at_interrupt, player.frames_written)

    def test_a_unit_taken_but_not_yet_produced_is_reported_as_pending(self) -> None:
        """It is in the queue no longer and in the schedule not yet.

        The producer parks holding one unit for the whole lookahead, so this is
        the steady state of any long reply rather than a narrow window. A report
        built from the queue and the schedule alone omits it, and a sender told
        it was neither playing nor pending treats it as heard.
        """
        gate = threading.Event()
        self.addCleanup(gate.set)
        synthesizer = FakeSynthesizer(gate=gate, gate_after=0)
        engine, _player, events = self.engine(synthesizer=synthesizer)
        self.begin(engine, events)

        engine.speak("taken", "One.")
        engine.speak("queued", "Two.")
        self.wait_for(lambda: synthesizer.spoken == ["One."], message="the unit to be taken")

        interruption = engine.interrupt()

        self.assertEqual(("taken", "queued"), interruption.pending)

    def test_a_unit_being_spoken_is_reported_once_not_twice(self) -> None:
        """Named as playing, and not also as pending.

        The schedule takes over reporting a unit the moment it has audio, and a
        unit held in both places would be named in both halves of the report.
        """
        engine, player, events = self.engine()
        self.begin(engine, events)
        engine.speak("one", "A sentence.")
        self.wait_for(lambda: player.frames_written > 0, message="audio produced")
        # One frame: past the start and nowhere near the end, which is what
        # "being spoken" means. Playing it all would make it heard instead.
        player.play(1)
        self.wait_for(lambda: self.names(events, EVENT_STARTED) == ["one"], message="started")

        interruption = engine.interrupt()

        self.assertEqual("one", interruption.playing)
        self.assertNotIn("one", interruption.pending)

    def test_an_interrupted_session_is_not_told_everything_was_heard(self) -> None:
        """The sender said it had finished; the text was thrown away unspoken.

        Reporting heard_all here tells the sender the person heard units that
        were discarded, which is the one thing the event must never mean.
        """
        engine, player, events = self.engine()
        self.begin(engine, events)
        for name in ("a", "b", "c"):
            engine.speak(name, "A sentence.")
        engine.end_input()
        self.wait_for(lambda: player.frames_written > 0, message="audio produced")

        interruption = engine.interrupt()
        time.sleep(0.1)

        self.assertFalse(interruption.nothing_unheard)
        self.assertEqual([], [e for e in events if e.get("event") == EVENT_HEARD_ALL])

    def test_a_batch_after_an_interruption_can_still_report_heard_all(self) -> None:
        """Suppressed for the discarded batch, not for the session."""
        engine, player, events = self.engine()
        self.begin(engine, events)
        engine.speak("dropped", "A sentence.")
        engine.end_input()
        self.wait_for(lambda: player.frames_written > 0, message="audio produced")
        engine.interrupt()

        engine.speak("kept", "Another sentence.")
        self.wait_for(lambda: player.pending_frames > 0, message="the new unit")
        player.play()
        engine.end_input()

        self.wait_for(
            lambda: any(e.get("event") == EVENT_HEARD_ALL for e in events),
            message="heard_all for the new batch",
        )

    def test_interrupting_without_a_session_reports_nothing(self) -> None:
        engine, _player, _events = self.engine()

        self.assertIsNone(engine.interrupt())


class PlaybackThreadTests(EngineHarness):
    def test_a_thread_that_outlived_the_join_does_not_rejoin_the_loop(self) -> None:
        """The next session must not find itself sharing the engine.

        `end()` joins for a bounded time and gives up, because a pass inside the
        model cannot be interrupted. If the thread reads its stop signal off an
        attribute the next `begin()` rebinds, it wakes to an event nobody set
        and runs on beside the thread that replaced it -- two producers, one
        device, and the events of one session going to the sink of another.
        """
        gate = threading.Event()
        self.addCleanup(gate.set)
        engine, player, events = self.engine(synthesizer=FakeSynthesizer(gate=gate))
        self.begin(engine, events)
        engine.speak("stuck", "One. Two. Three.")
        self.wait_for(lambda: player.frames_written > 0, message="the producer to park")
        outlived = next(
            thread for thread in threading.enumerate() if thread.name == "murmly-speech"
        )

        engine.end()
        self.assertTrue(outlived.is_alive(), "the join was expected to give up here")
        self.begin(engine, [])
        gate.set()

        self.wait_for(lambda: not outlived.is_alive(), message="the outlived thread to exit")

    def test_a_unit_is_not_written_into_a_closed_device(self) -> None:
        """Reported as a failure for that unit rather than queued into nothing."""
        engine, player, events = self.engine()
        self.begin(engine, events)
        player.active = False

        engine.speak("orphan", "A sentence.")

        self.wait_for(
            lambda: any(e.get("event") == "failed" for e in events), message="a failure event"
        )
        self.assertEqual(0, player.frames_written)


class CaptureGatingTests(EngineHarness):
    def test_suspending_closes_the_output_before_capture(self) -> None:
        engine, player, events = self.engine()
        self.begin(engine, events)
        engine.speak("one", "A sentence.")
        self.wait_for(lambda: player.frames_written > 0, message="audio produced")

        engine.suspend()

        self.assertFalse(player.active, "the output stream was still open")
        self.assertEqual(1, player.aborted)

    def test_text_arriving_while_capture_runs_is_held_and_spoken_after(self) -> None:
        engine, player, events = self.engine()
        self.begin(engine, events)
        engine.suspend()

        engine.speak("held", "Said while the person was speaking.")
        time.sleep(0.1)
        self.assertEqual(0, player.frames_written, "spoke over the person")

        engine.resume()
        self.wait_for(lambda: player.frames_written > 0, message="the held text")
        player.play()
        self.wait_for(
            lambda: self.names(events, EVENT_STARTED) == ["held"], message="the held unit started"
        )

    def test_resuming_reopens_the_output(self) -> None:
        engine, player, events = self.engine()
        self.begin(engine, events)
        engine.suspend()

        engine.resume()

        self.assertTrue(player.active)
        self.assertEqual(2, player.started)

    def test_resuming_without_a_session_opens_nothing(self) -> None:
        engine, player, _events = self.engine()

        engine.resume()

        self.assertEqual(0, player.started)


class SessionLifetimeTests(EngineHarness):
    def test_a_device_that_will_not_open_is_reported_at_the_start(self) -> None:
        engine, _player, events = self.engine(
            player=RecordingPlayer(start_error=RuntimeError("Unable to open an audio output."))
        )

        with self.assertRaises(RuntimeError) as raised:
            self.begin(engine, events)

        self.assertIn("Unable to open an audio output", str(raised.exception))
        self.assertFalse(engine.active)

    def test_ending_a_session_stops_speech_and_closes_the_device(self) -> None:
        engine, player, events = self.engine()
        self.begin(engine, events)
        engine.speak("one", "A sentence.")
        self.wait_for(lambda: player.frames_written > 0, message="audio produced")

        engine.end()

        self.assertFalse(engine.active)
        self.assertFalse(player.active)
        self.assertEqual(1, player.stopped)

    def test_ending_discards_whatever_was_queued(self) -> None:
        engine, player, events = self.engine()
        self.begin(engine, events)
        engine.speak("one", "First.")
        engine.speak("two", "Second.")

        engine.end()
        before = player.frames_written
        time.sleep(0.05)

        self.assertEqual(before, player.frames_written)

    def test_speaking_is_true_only_while_something_is_outstanding(self) -> None:
        engine, player, events = self.engine()
        self.assertFalse(engine.speaking)

        self.begin(engine, events)
        engine.speak("one", "A sentence.")
        self.wait_for(lambda: engine.speaking, message="speaking to become true")

        # Queued counts as speaking before a sample exists, so the audio has to
        # be produced before there is anything for the device to consume.
        self.wait_for(lambda: player.frames_written > 0, message="audio produced")
        player.play()
        engine.end_input()
        self.wait_for(lambda: not engine.speaking, message="speaking to become false")

    def test_availability_is_a_flag_and_a_reason_rather_than_a_failure(self) -> None:
        temp_dir = tempfile.mkdtemp()
        config = MurmlyConfig(
            socket_path=Path(temp_dir) / "murmly.sock",
            config_path=Path(temp_dir) / "config.toml",
            tts_enabled=False,
        )

        engine = SpeechEngine(config, player=RecordingPlayer())

        self.assertFalse(engine.available)
        self.assertEqual("Speech output is not enabled.", engine.unavailable_reason)


class ProducedAudioTests(EngineHarness):
    def test_every_sentence_of_a_unit_reaches_the_device(self) -> None:
        engine, player, events = self.engine()
        self.begin(engine, events)
        text = "One thing. Then another. And a third."

        engine.speak("passage", text)
        self.wait_for(
            lambda: len(player.written) >= len(split_for_fake(text)), message="every sentence"
        )

        self.assertEqual(3, len(player.written))
        for _frames, rate in player.written:
            self.assertEqual(24_000, rate)


if __name__ == "__main__":
    unittest.main()
