"""The speech queue and the thread that plays it.

Everything here is about one exchange between a person and a sender: named
units of text arrive over time, are spoken in the order they arrived, and the
sender is told what was heard. The transport that carries them lives in
`daemon.py`; what is here does not know it is a socket.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
import logging
import threading

from murmly.audio import SoundDevicePlayer
from murmly.config import MurmlyConfig
from murmly.tts import KokoroSynthesizer


logger = logging.getLogger(__name__)

EVENT_STARTED = "started"
EVENT_HEARD_ALL = "heard_all"
EVENT_INTERRUPTED = "interrupted"
EVENT_TRANSCRIPT = "transcript"
EVENT_SHUTTING_DOWN = "shutting_down"
EVENT_FAILED = "failed"

# How long the producer may run ahead of the loudspeaker. Enough that a sentence
# taking a second to produce never starves the device, and little enough that
# the played position stays close to the produced one.
LOOKAHEAD_SECONDS = 3.0
POLL_SECONDS = 0.02
PLAYBACK_JOIN_SECONDS = 1.0

EventSink = Callable[[dict[str, object]], None]


@dataclass(frozen=True, slots=True)
class SpeechUnit:
    """A piece of text the sender named, which is how it is reported back."""

    name: str
    text: str


@dataclass(slots=True)
class _Scheduled:
    """Where one unit's audio sits in the stream handed to the device."""

    name: str
    start_frame: int
    end_frame: int | None = None
    started: bool = False
    heard: bool = False


class SpeechQueue:
    """Named units in the order they were sent, with an end-of-input marker.

    The marker is what separates a queue that is empty because the sender is
    still thinking from one that is empty because the exchange is over. Without
    it there is no moment at which everything can be reported as heard.
    """

    def __init__(self) -> None:
        self._units: deque[SpeechUnit] = deque()
        # The unit handed to the producer that has not registered audio of its
        # own yet. Between the two it belongs to no other structure, and an
        # interruption in that window would report it as neither playing nor
        # pending -- so the queue keeps holding it.
        self._in_flight: SpeechUnit | None = None
        self._lock = threading.Lock()
        self._arrived = threading.Event()
        self._end_of_input = False

    def send(self, unit: SpeechUnit) -> None:
        with self._lock:
            self._units.append(unit)
            # More text means the sender was not finished after all. The marker
            # latched for the life of the session otherwise, so a sender that
            # said `end` and then sent more was told everything was heard the
            # moment that new text drained -- for a batch it had never ended.
            self._end_of_input = False
            self._arrived.set()

    def end_input(self) -> None:
        with self._lock:
            self._end_of_input = True
            # Wakes the player, which has to re-examine whether everything is
            # heard now that no more text is coming.
            self._arrived.set()

    @property
    def input_ended(self) -> bool:
        with self._lock:
            return self._end_of_input

    @property
    def waiting(self) -> list[str]:
        """The names of units that have not been taken for production yet."""
        with self._lock:
            return [unit.name for unit in self._units]

    def take(self, timeout: float) -> SpeechUnit | None:
        if not self._arrived.wait(timeout):
            return None
        with self._lock:
            if not self._units:
                self._arrived.clear()
                return None
            unit = self._units.popleft()
            # Recorded in the same locked step as the pop, so a discard can
            # never run between the two and lose sight of it.
            self._in_flight = unit
            if not self._units:
                self._arrived.clear()
            return unit

    def settle(self, unit: SpeechUnit) -> None:
        """Release a unit that something else now reports for.

        Called once the unit has a scheduled entry, which reports its position
        from that point on. Holding it in both would name it twice in an
        interruption -- once as playing and again as pending.
        """
        with self._lock:
            if self._in_flight is unit:
                self._in_flight = None

    def holds(self, unit: SpeechUnit) -> bool:
        """Whether this unit is still the one the queue handed out."""
        with self._lock:
            return self._in_flight is unit

    @property
    def holding(self) -> bool:
        """Whether a unit is out with the producer and has no audio yet.

        `waiting` cannot answer this: it reports what has not been taken, and a
        unit being produced has been. Anything asking whether the exchange is
        finished has to count this one, or it counts a unit that is about to be
        spoken as one that already was.
        """
        with self._lock:
            return self._in_flight is not None

    def putback(self, unit: SpeechUnit) -> None:
        """Return a unit to the front, so its place in the order is kept."""
        with self._lock:
            if self._in_flight is unit:
                self._in_flight = None
            self._units.appendleft(unit)
            self._arrived.set()

    def discard(self) -> list[str]:
        """Drop everything not yet spoken, and report what was dropped."""
        with self._lock:
            taken = () if self._in_flight is None else (self._in_flight,)
            dropped = [unit.name for unit in (*taken, *self._units)]
            self._in_flight = None
            self._units.clear()
            self._arrived.clear()
            # An interruption throws away text the sender may already have
            # finished sending. Leaving the marker latched would let the next
            # publish find an empty queue and a finished sender and report
            # everything as heard, when it was discarded unspoken.
            self._end_of_input = False
            return dropped

    def wake(self) -> None:
        self._arrived.set()


@dataclass(frozen=True, slots=True)
class Interruption:
    """What the person did and did not hear when speech was stopped."""

    playing: str | None
    pending: tuple[str, ...]

    @property
    def nothing_unheard(self) -> bool:
        return self.playing is None and not self.pending


class SpeechSuspendError(RuntimeError):
    """The output device would not close, but speech had already stopped.

    Carries the report anyway. The interruption is what a sender needs most and
    is the one thing it cannot learn any other way, so it must not be lost with
    the failure that happened after speech was already silent.
    """

    def __init__(self, interruption: "Interruption | None", error: BaseException) -> None:
        super().__init__(str(error))
        self.interruption = interruption


class SpeechEngine:
    """Speaks queued units and reports the position the person actually heard.

    Never reports the production frontier. Producing runs a sentence or more
    ahead of the loudspeaker, so a position taken from it would tell a sender
    the person heard something they did not.
    """

    def __init__(
        self,
        config: MurmlyConfig,
        *,
        synthesizer=None,
        player=None,
    ) -> None:
        self._config = config
        # Not built at all when speech output is off: the probe runs espeak-ng
        # and reads distribution metadata, and a daemon that will never speak
        # should pay none of it.
        if synthesizer is not None:
            self._synthesizer = synthesizer
        elif config.tts_enabled:
            self._synthesizer = KokoroSynthesizer(config)
        else:
            self._synthesizer = None
        self._player = player if player is not None else SoundDevicePlayer(config)
        self._queue = SpeechQueue()
        self._sink: EventSink | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._hold = threading.Event()
        self._lock = threading.Lock()
        self._scheduled: deque[_Scheduled] = deque()
        self._generation = 0
        self._reported_heard_all = False

    @property
    def available(self) -> bool:
        return (
            bool(self._config.tts_enabled)
            and self._synthesizer is not None
            and self._synthesizer.available
        )

    @property
    def unavailable_reason(self) -> str | None:
        if not self._config.tts_enabled:
            return "Speech output is not enabled."
        if self._synthesizer is None:
            return "Speech output is not enabled."
        return self._synthesizer.unavailable_reason

    @property
    def synthesizer(self):
        return self._synthesizer

    @property
    def player(self) -> SoundDevicePlayer:
        return self._player

    @property
    def active(self) -> bool:
        """Whether a session's playback is running."""
        return self._sink is not None

    @property
    def speaking(self) -> bool:
        """Whether anything is queued, being produced, or still to be heard."""
        with self._lock:
            outstanding = any(not entry.heard for entry in self._scheduled)
        return self.active and (
            outstanding or bool(self._queue.waiting) or self._queue.holding
        )

    def begin(self, sink: EventSink) -> None:
        """Take the output device and start playing whatever this session sends.

        The device is opened here rather than at the first word: a session that
        cannot be given one has to be refused at the moment it is declared, not
        accepted and failed once somebody is listening.
        """
        if self._sink is not None or self._thread is not None:
            self.end()
        self._player.start()
        self._queue = SpeechQueue()
        self._scheduled = deque()
        # Held by the thread that owns it rather than read off the attribute
        # every pass. A thread that outlived end()'s bounded join would
        # otherwise find this rebinding, read an event nobody has set, and
        # rejoin the loop alongside the thread that replaced it.
        stop = threading.Event()
        self._stop = stop
        self._hold.clear()
        self._reported_heard_all = False
        self._sink = sink
        thread = threading.Thread(
            target=self._run, args=(stop, sink), name="murmly-speech", daemon=True
        )
        try:
            thread.start()
        except RuntimeError as error:
            # Degrades the feature rather than failing the command, as the level
            # meter does. The caller reports speech as unavailable for now.
            self._sink = None
            self._player.stop()
            raise RuntimeError(f"Speech playback could not start: {error}") from error
        self._thread = thread

    def speak(self, name: str, text: str) -> None:
        # Queued first, suppressor re-armed second. The other order leaves a
        # window in which a publish sees the suppressor cleared while the
        # end-of-input marker is still latched, which is the report this pair
        # exists to prevent.
        self._queue.send(SpeechUnit(name=name, text=text))
        self._reported_heard_all = False

    def end_input(self) -> None:
        self._queue.end_input()

    def hold(self) -> None:
        """Stop taking new units, because the person is speaking.

        Text that arrives now is kept rather than spoken over them.
        """
        self._hold.set()

    def release(self) -> None:
        self._hold.clear()
        self._queue.wake()

    def suspend(self) -> Interruption | None:
        """Stop speech and close the output, because capture is about to start.

        The device is closed rather than merely silenced: the three consumers
        that read the live microphone would ingest Murmly's own voice, and a
        stream that is shut is the only version of that guarantee a test can
        check.
        """
        interruption = self.interrupt()
        self.hold()
        try:
            self._player.stop()
        except Exception as error:  # noqa: BLE001 - the report survives the failure
            # Speech has already stopped by this point, so the session is owed
            # its interruption whatever the device did on the way out. Raising
            # bare would take the report with it: the caller has nothing to
            # send, and the sender keeps generating for someone who stopped
            # listening -- which is the one thing the event exists to prevent.
            raise SpeechSuspendError(interruption, error) from error
        return interruption

    def resume(self) -> None:
        """Speak again now that capture has ended, including anything held."""
        if self._sink is None:
            return
        try:
            self._player.start()
        except RuntimeError as error:
            logger.warning("Speech output could not be reopened after capture: %s", error)
            self._emit(self._sink, {"event": EVENT_FAILED, "error": str(error)})
        finally:
            # Released whether or not the device came back. Capture has ended,
            # and the hold means "the person is talking" -- keeping it set for a
            # device fault stops the queue being drained at all, so the sender
            # is told nothing about any unit it goes on to send and `status`
            # reports SPEAKING for a daemon that is idle and silent. A unit that
            # cannot be produced is reported failed, which is what the sender
            # can act on.
            self.release()

    def interrupt(self) -> Interruption | None:
        """Stop speech and report what was playing and what never started.

        The position is taken after the abort, from the frames the device was
        given, so it is what the person heard rather than what was produced.
        """
        if self._sink is None:
            return None
        with self._lock:
            # Discards a synthesis that is already inside the model: it cannot be
            # interrupted, so its result is thrown away instead.
            self._generation += 1
            played = self._player.abort()
            self._settle(played)
            playing = next(
                (entry.name for entry in self._scheduled if entry.started and not entry.heard),
                None,
            )
            never_started = [entry.name for entry in self._scheduled if not entry.started]
            self._scheduled = deque()
            # Discarded under the same lock that emptied the schedule, so the
            # teardown is one step. Released first, two things fit in the gap: a
            # publish seeing an empty schedule, an empty queue and a finished
            # sender, which reports everything heard for a batch just cut off;
            # and the producer's own stale return clearing the unit it holds, so
            # the discard finds nothing and the report names it nowhere.
            pending = tuple(never_started + self._queue.discard())
        return Interruption(playing=playing, pending=pending)

    def end(self) -> None:
        """Finish with this session: stop speech, drop its queue, close the device."""
        thread = self._thread
        self._thread = None
        self._sink = None
        self._stop.set()
        self._queue.wake()
        with self._lock:
            self._generation += 1
            self._scheduled = deque()
            # One step, as in interrupt(). Nothing reads the report here, but
            # the split is what let a publish run against a half-torn-down
            # engine, and leaving one of the two shapes in place invites it back.
            self._queue.discard()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=PLAYBACK_JOIN_SECONDS)
            if thread.is_alive():
                # A pass already inside the model cannot be interrupted. Its
                # result is discarded by the generation check, so the close
                # proceeds now rather than waiting for it.
                logger.debug("Speech playback did not exit within the join timeout.")
        try:
            self._player.abort()
        finally:
            self._player.stop()

    # ------------------------------------------------------------------ inside

    def _run(self, stop: threading.Event, sink: EventSink | None) -> None:
        while not stop.is_set():
            self._publish(sink)
            if self._hold.is_set():
                stop.wait(POLL_SECONDS)
                continue
            unit = self._queue.take(POLL_SECONDS)
            if unit is None:
                continue
            if self._hold.is_set():
                # Taken in the window between the check above and the hotkey
                # press. Put back rather than spoken: the person is talking.
                self._queue.putback(unit)
                continue
            try:
                self._speak(unit, sink, stop)
            except Exception as error:  # noqa: BLE001 - one unit must not end the session
                logger.warning("Speech for %s failed: %s", unit.name, error)
                self._emit(sink, {"event": EVENT_FAILED, "name": unit.name, "error": str(error)})
            finally:
                # Whatever happened, the queue is no longer holding it: either a
                # scheduled entry reports for it now, or it produced nothing.
                self._queue.settle(unit)

    def _speak(self, unit: SpeechUnit, sink: EventSink | None, stop: threading.Event) -> None:
        with self._lock:
            generation = self._generation
        entry: _Scheduled | None = None
        for samples, sample_rate_hz in self._synthesizer.synthesize(unit.text):
            if stop.is_set() or self._stale(generation):
                return
            self._wait_for_room(sink, generation, stop)
            with self._lock:
                # The write happens under the lock interrupt() aborts under, so
                # there is no window in which a chunk checked as current is
                # handed to the device after the abort has emptied it -- which
                # is audible speech after the person asked for silence.
                if stop.is_set() or self._stale(generation):
                    return
                if entry is None and not self._queue.holds(unit):
                    # Discarded in the window between being taken and the
                    # generation being read, so the staleness check above cannot
                    # see it. Once there is an entry, that entry and the
                    # generation are what report for this unit.
                    return
                if not self._player.active:
                    raise RuntimeError("The speech output device is not open.")
                frames = self._player.write(samples, sample_rate_hz)
                if frames == 0:
                    continue
                if entry is None:
                    # Registered on the first chunk rather than the last, so a
                    # unit whose audio starts playing while its later sentences
                    # are still being produced is not missed by the crossing.
                    entry = _Scheduled(unit.name, self._player.frames_written - frames)
                    self._scheduled.append(entry)
                    # This entry reports the unit's position from here on, so
                    # the queue stops reporting for it and an interruption names
                    # it once rather than twice.
                    self._queue.settle(unit)
                entry.end_frame = self._player.frames_written

    def _wait_for_room(
        self, sink: EventSink | None, generation: int, stop: threading.Event
    ) -> None:
        """Hold the producer back until the device has drained enough.

        Without this the whole passage is produced before its first sentence is
        audible, and the played position lags the produced one by the entire
        text -- which is exactly what the position must not do.
        """
        lookahead = int(LOOKAHEAD_SECONDS * self._player.sample_rate_hz)
        while self._player.pending_frames > lookahead:
            if stop.is_set() or self._stale(generation):
                return
            self._publish(sink)
            stop.wait(POLL_SECONDS)

    def _stale(self, generation: int) -> bool:
        return generation != self._generation

    def _publish(self, sink: EventSink | None) -> None:
        """Send whatever the played position has newly crossed."""
        started: list[str] = []
        with self._lock:
            played = self._player.frames_played
            for entry in self._scheduled:
                # Strictly past the start: a unit beginning at frame N has not
                # been heard at all until frame N itself has gone to the device.
                if not entry.started and played > entry.start_frame:
                    entry.started = True
                    started.append(entry.name)
                if entry.end_frame is not None and played >= entry.end_frame:
                    entry.heard = True
            outstanding = any(not entry.heard for entry in self._scheduled)
            while self._scheduled and self._scheduled[0].heard:
                self._scheduled.popleft()
            heard_all = (
                self._queue.input_ended
                and not outstanding
                and not self._queue.waiting
                and not self._queue.holding
                and self._player.pending_frames == 0
                and not self._reported_heard_all
            )
            if heard_all:
                self._reported_heard_all = True
        for name in started:
            self._emit(sink, {"event": EVENT_STARTED, "name": name})
        if heard_all:
            self._emit(sink, {"event": EVENT_HEARD_ALL})

    def _settle(self, played: int) -> None:
        """Mark what the frozen played position says was started and heard.

        Called under the lock with the count taken after the abort, so the
        report cannot move while it is being built.
        """
        for entry in self._scheduled:
            if played > entry.start_frame:
                entry.started = True
            if entry.end_frame is not None and played >= entry.end_frame:
                entry.heard = True

    @staticmethod
    def _emit(sink: EventSink | None, event: dict[str, object]) -> None:
        if sink is None:
            return
        try:
            sink(event)
        except Exception as error:  # noqa: BLE001 - a sender must not affect audio
            logger.warning("A speech event could not be delivered: %s", error)
