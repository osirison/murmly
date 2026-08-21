from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from fakes import GATE_TIMEOUT_SECONDS, FakeSynthesizer, expected_audio, split_for_fake
from murmly.config import MurmlyConfig
from murmly.speech import (
    EVENT_HEARD_ALL,
    EVENT_STARTED,
    LOOKAHEAD_SECONDS,
    SpeechEngine,
    SpeechQueue,
    SpeechSuspendError,
    SpeechUnit,
)


class RecordingPlayer:
    """A `SoundDevicePlayer` stand-in whose playback the test advances itself.

    Nothing here is timing dependent: `play(frames)` is the only thing that
    moves the played position, so a test can put the frontier exactly where a
    scenario needs it.
    """

    def __init__(self, sample_rate_hz: int = 24_000, start_error: Exception | None = None) -> None:
        # Public so a test can make the device fail on a *later* open, which is
        # the case that matters: a sink that goes away while capture is running.
        self.start_error = start_error
        self.sample_rate_hz = sample_rate_hz
        self.frames_written = 0
        self.frames_played = 0
        self.started = 0
        self.stopped = 0
        self.aborted = 0
        self.active = False
        self.written: list[tuple[int, int]] = []

    def start(self) -> None:
        if self.start_error is not None:
            raise self.start_error
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


class GatedQueue(SpeechQueue):
    """Blocks after handing a unit out, holding none of its own locks.

    The window between the queue releasing a unit and the producer claiming a
    generation for it is a few instructions wide and cannot be reached by
    timing. A test that needs an interruption to land inside it has to hold the
    producer there.
    """

    def __init__(self) -> None:
        super().__init__()
        self.handed = threading.Event()
        self.release = threading.Event()

    def take(self, timeout: float):
        unit = super().take(timeout)
        if unit is not None:
            self.handed.set()
            self.release.wait(GATE_TIMEOUT_SECONDS)
        return unit


class LockObservingQueue(SpeechQueue):
    """Records whether the engine lock was held when the queue was discarded.

    Asserting the ordering rather than trying to out-race it. The window this
    guards is a few bytecodes wide and a test that tried to land a publish
    inside it would pass on a machine that happened not to switch there; what
    is actually required is that the discard and the schedule teardown are one
    step, and that is directly observable.
    """

    def __init__(self, engine) -> None:
        super().__init__()
        self._engine = engine
        self.discarded_under_lock: bool | None = None
        self.sent_under_lock: bool | None = None

    def _engine_lock_held(self) -> bool:
        free = self._engine._lock.acquire(blocking=False)
        if free:
            self._engine._lock.release()
        return not free

    def send(self, unit) -> None:
        self.sent_under_lock = self._engine_lock_held()
        return super().send(unit)

    def discard(self) -> list[str]:
        # A non-reentrant lock refuses this if anyone holds it, including the
        # thread asking -- which is the case being checked.
        self.discarded_under_lock = self._engine_lock_held()
        return super().discard()


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
        gate = threading.Event()
        self.addCleanup(gate.set)
        engine, player, events = self.engine(synthesizer=FakeSynthesizer(gate=gate))
        self.begin(engine, events)
        # Interrupted with its first sentence audible and its second still
        # inside the model, which is the only state in which the queue and the
        # schedule could both be holding it.
        engine.speak("one", "First part. Second part.")
        self.wait_for(lambda: player.frames_written > 0, message="the first sentence")
        # One frame: past the start and nowhere near the end, which is what
        # "being spoken" means. Playing it all would make it heard instead.
        # No `started` event to wait on here -- the loop publishes between
        # units, and this one is parked inside the model. The interruption
        # settles the position itself from the count taken after the abort.
        player.play(1)

        interruption = engine.interrupt()

        self.assertEqual("one", interruption.playing)
        self.assertNotIn("one", interruption.pending)

    def test_a_unit_discarded_before_its_generation_was_read_is_not_spoken(self) -> None:
        """The producer claims a generation after the queue hands the unit out.

        An interruption landing between the two bumps the generation first, so
        the producer reads the new one and never registers as stale -- and would
        write audio to the device after the person asked for silence, while the
        sender was told nothing was left unheard.
        """
        engine, player, events = self.engine()
        self.begin(engine, events)
        queue = GatedQueue()
        self.addCleanup(queue.release.set)
        engine._queue = queue

        engine.speak("cancelled", "A sentence.")
        self.assertTrue(queue.handed.wait(5), "the queue never handed the unit out")
        interruption = engine.interrupt()
        queue.release.set()
        time.sleep(0.1)

        self.assertEqual(("cancelled",), interruption.pending)
        self.assertEqual(0, player.frames_written, "audio reached the device after the abort")

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

    def test_a_half_produced_unit_is_not_reported_as_heard(self) -> None:
        """`end_frame` is where the audio SO FAR ends, and it moves per chunk.

        A playback position that has caught up to it means the person heard
        everything produced, never that they heard the unit. Reported as heard,
        an interruption tells the sender nothing was cut off and it never
        re-sends the half of the reply the person did not get.
        """
        gate = threading.Event()
        self.addCleanup(gate.set)
        engine, player, events = self.engine(synthesizer=FakeSynthesizer(gate=gate))
        self.begin(engine, events)
        engine.speak("half", "The first part. The second part.")
        self.wait_for(lambda: player.frames_written > 0, message="the first sentence")
        # Everything produced so far has been played, and the rest of the unit
        # is still inside the model.
        player.play()

        interruption = engine.interrupt()

        self.assertEqual("half", interruption.playing)
        self.assertFalse(interruption.nothing_unheard, "a cut-off unit was reported as heard")

    def test_everything_heard_waits_for_the_rest_of_the_unit_being_produced(self) -> None:
        """The publish path, which the interruption test does not reach.

        `_settle` and `_publish` each decide `heard` for themselves, so guarding
        one leaves the other reporting a unit as heard the moment playback
        catches up to the audio produced so far -- mid-unit, with the rest still
        in the model.
        """
        gate = threading.Event()
        self.addCleanup(gate.set)
        engine, player, events = self.engine(synthesizer=FakeSynthesizer(gate=gate))
        sink = self.begin(engine, events)
        engine.speak("half", "The first part. The second part.")
        self.wait_for(lambda: player.frames_written > 0, message="the first sentence")
        player.play()
        engine.end_input()

        # Published from here: the thread that publishes is parked inside the
        # model, so nothing would evaluate the gate while the unit is half done.
        engine._publish(sink)

        self.assertEqual([], [e for e in events if e.get("event") == EVENT_HEARD_ALL])

    def test_speak_is_one_step_under_the_engine_lock(self) -> None:
        """`_publish` holds this lock across the whole gate and the flag it sets.

        A speak that took the queue without it could land between the gate
        deciding to report and the flag being written. Asserting the ordering
        rather than racing it: the window is a few instructions wide and a test
        that tried to land inside it would pass on any machine that did not
        happen to switch there.
        """
        engine, _player, events = self.engine()
        self.begin(engine, events)
        engine._queue = LockObservingQueue(engine)

        engine.speak("one", "A sentence.")

        self.assertTrue(
            engine._queue.sent_under_lock,
            "speak queued text outside the lock the heard-all gate is decided under",
        )

    def test_a_speak_racing_the_heard_all_gate_still_gets_its_own_report(self) -> None:
        """The queue write and the suppressor reset are one step.

        Split, a speak could land between the gate deciding to report and the
        flag being set: the flag then latched for a batch that had not been
        heard, and the batch that arrived never got a report at all.
        """
        engine, player, events = self.engine()
        self.begin(engine, events)
        engine.speak("first", "A sentence.")
        self.wait_for(lambda: player.frames_written > 0, message="audio produced")
        player.play()
        engine.end_input()
        self.wait_for(
            lambda: [e for e in events if e.get("event") == EVENT_HEARD_ALL],
            message="heard_all for the first batch",
        )

        engine.speak("second", "Another sentence.")
        self.wait_for(lambda: player.pending_frames > 0, message="the second unit")
        player.play()
        engine.end_input()

        self.wait_for(
            lambda: len([e for e in events if e.get("event") == EVENT_HEARD_ALL]) == 2,
            message="heard_all for the second batch",
        )

    def test_interrupting_without_a_session_reports_nothing(self) -> None:
        engine, _player, _events = self.engine()

        self.assertIsNone(engine.interrupt())

    def test_an_interruption_tears_down_the_schedule_and_the_queue_in_one_step(self) -> None:
        """Nothing can observe the engine half torn down.

        Two things fit in a gap between emptying the schedule and discarding the
        queue: a publish that sees no schedule, no queue and a finished sender,
        and so reports everything heard for the batch being cut off; and the
        producer's own stale return releasing the unit it holds, so the discard
        finds nothing and the report names it nowhere.
        """
        engine, player, events = self.engine()
        self.begin(engine, events)
        engine._queue = LockObservingQueue(engine)

        for name in ("a", "b"):
            engine.speak(name, "A sentence.")
        self.wait_for(lambda: player.frames_written > 0, message="audio produced")
        engine.end_input()

        interruption = engine.interrupt()

        self.assertTrue(
            engine._queue.discarded_under_lock,
            "the queue was discarded outside the lock that emptied the schedule",
        )
        self.assertFalse(interruption.nothing_unheard)
        self.assertEqual([], [e for e in events if e.get("event") == EVENT_HEARD_ALL])

    def test_everything_heard_waits_for_a_unit_the_producer_is_holding(self) -> None:
        """A unit being produced has been taken, so `waiting` does not see it.

        Anything asking whether the exchange is over has to count it, or a unit
        about to be spoken is reported as one already heard.
        """
        engine, player, events = self.engine()
        sink = self.begin(engine, events)
        queue = GatedQueue()
        self.addCleanup(queue.release.set)
        engine._queue = queue

        engine.speak("held", "A sentence.")
        self.assertTrue(queue.handed.wait(5), "the queue never handed the unit out")
        engine.end_input()

        # Published from here rather than waited for. The thread that publishes
        # is the thread that produces, so while it is holding a unit it cannot
        # publish at all -- a test that waited for the event would pass whatever
        # the predicate said, because nothing would ever evaluate it.
        engine._publish(sink)

        self.assertTrue(engine.speaking, "a unit about to be spoken is not speech")
        self.assertEqual(0, player.frames_written, "the producer was expected to be parked")
        self.assertEqual([], [e for e in events if e.get("event") == EVENT_HEARD_ALL])


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

    def test_the_hold_is_taken_before_the_interruption(self) -> None:
        """Order, not timing.

        Taken afterwards, a unit sent in the gap is picked up by the producer
        and written into a device that is about to close: neither spoken nor
        named in the report, so the sender is never told it was dropped. The
        gap is a few instructions, so what is asserted is that there is no gap.
        """
        engine, _player, events = self.engine()
        self.begin(engine, events)
        held_when_interrupted: list[bool] = []
        original = engine.interrupt

        def observe():
            held_when_interrupted.append(engine._hold.is_set())
            return original()

        engine.interrupt = observe
        try:
            engine.suspend()
        finally:
            engine.interrupt = original

        self.assertEqual(
            [True], held_when_interrupted, "the hold was taken after the interruption"
        )

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

    def test_a_device_that_will_not_reopen_still_ends_the_hold(self) -> None:
        """Capture has ended, so the hold ends with it.

        The hold means the person is talking. Keeping it set for a device fault
        stops the queue being drained at all: the sender is told nothing about
        anything it sends afterwards, and `speaking` stays true for a daemon
        that is idle and silent. A unit that cannot be produced is reported
        failed, which is something the sender can act on.
        """
        engine, player, events = self.engine()
        self.begin(engine, events)
        engine.suspend()
        player.start_error = RuntimeError("Unable to open an audio output.")

        engine.resume()

        engine.speak("after", "A sentence.")
        self.wait_for(
            lambda: [e for e in events if e.get("event") == "failed" and e.get("name") == "after"],
            message="the unit to be reported failed",
        )
        self.assertFalse(engine.speaking, "the daemon would report SPEAKING while idle")

    def test_an_interruption_survives_a_device_that_will_not_close(self) -> None:
        """Speech has already stopped by the time the close is attempted.

        Raising bare took the report with it: the daemon had nothing to send,
        so the sender was never told it had been cut off and kept generating
        for someone who had stopped listening.
        """
        player = RecordingPlayer()

        def refuse_to_stop() -> None:
            player.active = False
            raise RuntimeError("the output stream would not close")

        engine, _player, events = self.engine(player=player)
        self.begin(engine, events)
        engine.speak("one", "A sentence.")
        self.wait_for(lambda: player.frames_written > 0, message="audio produced")
        engine.speak("two", "Another sentence.")
        self.wait_for(lambda: engine._queue.waiting or player.written[1:], message="a second unit")
        # Restored before the harness tears the engine down, which closes the
        # device too and would otherwise fail the cleanup rather than the test.
        self.addCleanup(setattr, player, "stop", player.stop)
        player.stop = refuse_to_stop

        with self.assertRaises(SpeechSuspendError) as raised:
            engine.suspend()

        self.assertIsNotNone(raised.exception.interruption)
        self.assertFalse(raised.exception.interruption.nothing_unheard)

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
