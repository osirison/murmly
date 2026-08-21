from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import json
import logging
import os
from pathlib import Path
import socket
import stat
import struct
import threading
import time

from murmly.audio import SoundDeviceRecorder
from murmly.config import MurmlyConfig
from murmly.focus import (
    FocusObserver,
    WindowIdentity,
    create_focus_observer,
    record_target,
    should_deliver,
)
from murmly.integrations import ClipboardPaster
from murmly.overlay import (
    detect_overlay_backend,
    NullOverlayController,
    OverlayController,
    OverlayLifecycle,
    OverlayState,
)
from murmly.silence import SilenceDetector
from murmly.speech import (
    EVENT_INTERRUPTED,
    EVENT_SHUTTING_DOWN,
    EVENT_TRANSCRIPT,
    Interruption,
    SpeechEngine,
)
from murmly.stt import FasterWhisperTranscriber


logger = logging.getLogger(__name__)
MAX_COMMAND_BYTES = 4_096
MAX_COMMAND_WORKERS = 8
COMMAND_TIMEOUT_SECONDS = 2.0
CLIENT_CONNECT_TIMEOUT_SECONDS = 2.0
CLIENT_RESPONSE_TIMEOUT_SECONDS = 600.0
REFUSAL_SEND_TIMEOUT_SECONDS = 0.5
SHUTDOWN_DRAIN_SECONDS = 0.5
SHUTDOWN_DRAIN_POLL_SECONDS = 0.01
SOCKET_MODE = 0o600
MAX_SOCKET_PATH_LINKS = 40
SOCKET_DIRECTORY_MODE = 0o700
LIVE_WORKER_JOIN_SECONDS = 0.5
MIN_LIVE_TICK_SECONDS = 0.05
SILENCE_TICK_SECONDS = 0.25
MAX_SILENCE_TICK_SECONDS = 1.0
MAX_TICK_FAILURES = 5

COMMAND_SPEECH_SESSION = "speech_session"
COMMAND_TOGGLE = "toggle"
COMMAND_TOGGLE_SESSION = "toggle_session"
COMMAND_STATUS = "status"
# A speech session's own bound. `MAX_COMMAND_BYTES` stays 4096 for every
# existing command: a sender streaming a reply carries paragraphs, and a state
# query does not.
MAX_SPEECH_FRAME_BYTES = 65_536
SESSION_POLL_SECONDS = 0.2
SESSION_SEND_TIMEOUT_SECONDS = 2.0
# Enough backlog that an ordinary consumer never trips it, few enough that a
# session which has stopped reading is disconnected rather than accumulating.
SESSION_EVENT_BACKLOG = 64
SESSION_WRITER_JOIN_SECONDS = 1.0

STATE_IDLE = "IDLE"
STATE_LISTENING = "LISTENING"
STATE_THINKING = "THINKING"
# The fourth state. `OverlayState` deliberately keeps its three: speech has no
# visual indicator in this change, and `recording-overlay` forbids one outside
# the capture lifecycle.
STATE_SPEAKING = "SPEAKING"

DESTINATION_WINDOW = "window"
DESTINATION_SESSION = "session"


class CommandCode(StrEnum):
    """The closed set of failure categories an unsuccessful response reports.

    A caller that has to decide what to do next cannot branch on prose, which is
    free to be reworded. SHUTTING_DOWN is separate from COMMAND_FAILED because a
    caller should retry it and must not retry the other.
    """

    BUSY = "busy"
    UNSUPPORTED_COMMAND = "unsupported_command"
    MALFORMED_REQUEST = "malformed_request"
    OVER_CAPACITY = "over_capacity"
    NOT_PERMITTED = "not_permitted"
    SHUTTING_DOWN = "shutting_down"
    COMMAND_FAILED = "command_failed"
    SPEECH_DISABLED = "speech_disabled"
    SPEECH_UNAVAILABLE = "speech_unavailable"
    SPEECH_SESSION_IN_USE = "speech_session_in_use"
    # Not a refusal: it is the category the interruption event reports itself
    # under, because a sender branching on what happened cannot branch on prose.
    SPEECH_INTERRUPTED = "speech_interrupted"


class RequestError(ValueError):
    """A request Murmly could not read, carrying the wording to report for it."""


class DaemonStartupError(RuntimeError):
    """The daemon refuses to start for a reason it detected itself."""


class DaemonNotRespondingError(RuntimeError):
    """The daemon accepted the connection and then neither answered nor stayed.

    Covers both shapes of the same outcome: a connection closed without a
    response, and one held open past the point where an answer could still be
    called an answer.

    Subclasses RuntimeError so a caller that already guards a bare RuntimeError
    keeps working, while the CLI names this type rather than catching bare
    RuntimeError and swallowing genuine programming errors with it.
    """


def failure_response(code: CommandCode, message: str, **fields: object) -> dict[str, object]:
    """Build an unsuccessful response.

    Takes the extra fields rather than the code and message alone: several
    failures carry `state`, and an existing reader of one of those responses must
    keep finding every field it reads today.
    """
    response: dict[str, object] = {"ok": False}
    response.update(fields)
    response["error"] = message
    response["code"] = code
    return response


def socket_path_detail(socket_path: Path) -> str | None:
    """Why the configured socket path is not private, or None when it is.

    The predicate is control over the path, not reachability. Connecting to a
    UNIX socket requires write permission on the node and the node is owner-only,
    so a directory other accounts can merely read or traverse is not an exposure
    -- refusing on that would reject a 0755 home directory. What another account
    needs is the ability to put its own node at this path, and three things grant
    it: write permission on the directory that holds the node, write permission
    on any directory above that one -- renaming a directory replaces everything
    under it -- and ownership of any of them, because an owner can grant itself
    the write permission whenever it likes.

    So every directory the lookup passes through is judged, symlinks resolved as
    they are reached rather than all at once: a caller opens the configured path,
    so the directories it is reached *through* matter as much as the one the node
    lands in. Above the deepest existing one the sticky bit is accepted in place
    of the write bits -- it is exactly the permission that stops one account
    renaming or removing another's entry, which is what /tmp relies on. The
    deepest existing directory gets no such exemption, because the components
    below it do not exist yet and the sticky bit does not stop anyone creating
    them.
    """
    traversed = _traversed_directories(socket_path.parent)
    if isinstance(traversed, str):
        return traversed
    deepest = traversed[-1]
    for directory in traversed:
        try:
            info = directory.stat()
        except OSError as error:
            return f"The permissions of {directory} could not be read: {error}."
        detail = _directory_exposure(
            directory, info, socket_path, sticky_accepted=directory != deepest
        )
        if detail is not None:
            return detail
    return None


def _traversed_directories(directory: Path) -> list[Path] | str:
    """Every directory a lookup of this path passes through, or why it cannot be walked.

    Resolving the whole path first and walking what comes back judges where the
    node lands but not how it is reached, and a symlink on the configured path is
    reached every time a caller opens it. An account that can replace that link
    substitutes the socket without touching the directory the link points at. So
    each component is resolved at the point it is reached, and the directory
    holding it is judged before the step is taken.

    The walk stops at the first component that is not an existing directory.
    Below that there is nothing to judge: either Murmly creates the rest, under a
    directory already judged here, or the path runs through something that is not
    a directory at all, which the bind reports.
    """
    if not directory.is_absolute():
        directory = Path.cwd() / directory
    root = Path(directory.anchor)
    current = root
    traversed = [root]
    pending = list(reversed(directory.parts[1:]))
    links = 0
    while pending:
        name = pending.pop()
        if name == ".":
            continue
        if name == "..":
            current = current.parent
            traversed.append(current)
            continue
        candidate = current / name
        try:
            is_link = candidate.is_symlink()
        except OSError as error:
            return f"The path {candidate} could not be read: {error}."
        if is_link:
            links += 1
            if links > MAX_SOCKET_PATH_LINKS:
                return f"The path {directory} passes through too many symbolic links."
            try:
                target = Path(os.readlink(candidate))
            except OSError as error:
                return f"The symbolic link {candidate} could not be read: {error}."
            if target.is_absolute():
                current = root
                pending.extend(reversed(target.parts[1:]))
            else:
                pending.extend(reversed(target.parts))
            continue
        if not candidate.is_dir():
            break
        current = candidate
        traversed.append(current)
    return traversed


def _directory_exposure(
    directory: Path,
    info: os.stat_result,
    socket_path: Path,
    *,
    sticky_accepted: bool,
) -> str | None:
    """How this one directory would let another account take over the socket path.

    Each detail carries its own remedy, because they differ: a directory this
    account does not own cannot be corrected with chmod.
    """
    if info.st_uid not in (0, os.getuid()):
        return (
            f"{directory} is owned by uid {info.st_uid}, which can grant itself write "
            f"access to it at any time and replace {socket_path}, so the commands meant "
            "for Murmly would reach a socket it does not serve. Either move the socket "
            "under your per-user runtime directory ($XDG_RUNTIME_DIR), or serve it from "
            "a directory this account owns."
        )
    if not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return None
    if sticky_accepted and info.st_mode & stat.S_ISVTX:
        return None
    return (
        f"{directory} is writable by other accounts, so another account could create "
        f"or replace {socket_path} and receive the commands meant for Murmly. Either "
        "move the socket under your per-user runtime directory ($XDG_RUNTIME_DIR), or "
        f"remove write access for group and other from {directory}."
    )


def peer_identity_supported() -> bool:
    """Whether this platform can report the account behind an accepted connection."""
    return hasattr(socket, "SO_PEERCRED")


def read_peer_identity(connection: socket.socket) -> int | None:
    """The user id on the other end of the connection, or None where unreadable."""
    if not peer_identity_supported():
        return None
    try:
        credentials = connection.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
    except OSError as error:
        logger.warning("Unable to read the peer identity of a connection: %s", error)
        return None
    _pid, uid, _gid = struct.unpack("3i", credentials)
    return uid


def create_socket_directory(directory: Path) -> None:
    """Create the socket's directory, and any missing ancestor, owner-only.

    `Path.mkdir(mode=...)` cannot be relied on here: it applies the mode to the
    final directory only, leaving intermediates at the umask, and skips a
    directory that already exists. So each missing directory is created and moded
    on its own, and one Murmly did not create is left alone -- an existing
    XDG_RUNTIME_DIR is the session's, not Murmly's, and is already private.
    """
    missing: list[Path] = []
    current = directory
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for path in reversed(missing):
        try:
            path.mkdir()
        except FileExistsError:
            continue
        path.chmod(SOCKET_DIRECTORY_MODE)


@dataclass(slots=True)
class ProcessingResult:
    text: str
    state: str
    delivered: bool = False
    detail: str | None = None


class SpeechSession:
    def __init__(
        self,
        config: MurmlyConfig,
        level_sink=None,
        focus_observer: FocusObserver | None = None,
        partial_sink: Callable[[str], None] | None = None,
        on_silence: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._recorder = SoundDeviceRecorder(config, level_sink=level_sink)
        self._transcriber = FasterWhisperTranscriber(config)
        self._paster: ClipboardPaster | None = None
        self._focus = focus_observer if focus_observer is not None else create_focus_observer()
        self._partial_sink = partial_sink
        self._on_silence = on_silence
        self._clock = clock
        self._silence: SilenceDetector | None = None
        self._live_stop = threading.Event()
        self._live_threads: list[threading.Thread] = []
        self._delivery_lock = threading.Lock()

    @property
    def focus_observer(self) -> FocusObserver:
        return self._focus

    def start_recording(self) -> None:
        self._recorder.start()
        try:
            self._transcriber.begin_capture()
            self._silence = self._create_silence_detector()
            self._start_live_worker()
        except Exception:
            # The stream is already open. Without this the daemon reports failure
            # and stays IDLE while the microphone stays live, and the next toggle
            # orphans it by overwriting the handle.
            try:
                self._recorder.stop()
            except Exception as stop_error:
                logger.warning("Unable to close capture after a failed start: %s", stop_error)
            raise

    def stop_recording(self) -> bytes:
        # Refuse new passes first: a tick already past its wait would otherwise
        # start a full decode during the join and hold the model lock, delaying
        # the final transcription by a whole pass whose result is discarded.
        self._transcriber.stop_partials()
        self._stop_live_worker()
        return self._recorder.stop()

    def take_segment(self) -> bytes:
        """Close the current segment and start speech tracking over for the next."""
        segment = self._recorder.take_segment()
        if self._silence is not None:
            self._silence.reset()
        return segment

    def capture_delivery_target(self) -> WindowIdentity | None:
        return record_target(self._focus)

    def process_recording(
        self,
        pcm_audio: bytes,
        target: WindowIdentity | None = None,
    ) -> ProcessingResult:
        text = self._transcriber.transcribe_pcm16(pcm_audio, self._recorder.sample_rate_hz)
        if not text:
            return ProcessingResult(text=text, state="DONE")
        # Held across delivery because clipboard restoration runs inside
        # copy_and_paste: without it a restore could overwrite the next
        # segment's transcript before that segment is pasted.
        with self._delivery_lock:
            allowed, reason = should_deliver(self._focus, target, self._config.verify_target)
            if allowed:
                # A session that cannot inject a paste is a refusal, not an error:
                # the transcript stays on the clipboard and the caller is told.
                outcome = self._ensure_paster().copy_and_paste(text)
                if outcome.injected:
                    return ProcessingResult(text=text, state="DONE", delivered=True)
                logger.warning("Transcript delivery could not inject a paste: %s", outcome.reason)
            else:
                logger.warning("Transcript delivery refused: %s", reason)
                self._ensure_paster().copy(text)
        return ProcessingResult(
            text=text,
            state="DONE",
            delivered=False,
            detail="Transcript copied to the clipboard but not pasted.",
        )

    def process_for_session(
        self,
        pcm_audio: bytes,
        deliver: Callable[[str], bool],
    ) -> ProcessingResult:
        """Transcribe, and hand the text to the speech session that asked for it.

        No window is recorded and none is verified. When the session has gone,
        the text goes to the clipboard and is reported as undelivered: the
        person still said the words, but the destination they chose no longer
        exists and Murmly must not substitute one for it.
        """
        text = self._transcriber.transcribe_pcm16(pcm_audio, self._recorder.sample_rate_hz)
        if not text:
            return ProcessingResult(text=text, state="DONE")
        if deliver(text):
            return ProcessingResult(text=text, state="DONE", delivered=True)
        with self._delivery_lock:
            self._ensure_paster().copy(text)
        logger.warning("A transcript had no speech session to be delivered to.")
        return ProcessingResult(
            text=text,
            state="DONE",
            delivered=False,
            detail="No speech session to deliver to. Transcript copied to the clipboard.",
        )

    def _create_silence_detector(self) -> SilenceDetector | None:
        if self._config.auto_transcribe == "off":
            return None
        detector = SilenceDetector(
            self._recorder.sample_rate_hz,
            self._config.channels,
            silence_ms=self._config.auto_transcribe_silence_ms,
            min_speech_ms=self._config.auto_transcribe_min_speech_ms,
        )
        if not detector.available:
            logger.warning("Auto-transcribe disabled for this session: %s", detector.unavailable_reason)
        return detector

    def _start_live_worker(self) -> None:
        wants_partials = self._config.live_transcribe
        wants_silence = self._silence is not None and self._silence.available
        self._live_stop = threading.Event()
        self._live_threads = []
        if wants_silence:
            # Its own thread, not a shared tick: a partial pass takes hundreds of
            # milliseconds on CUDA and seconds on CPU, and running both on one
            # thread would black out silence detection for that whole time --
            # which is the defect that let recordings run through pauses.
            self._spawn_live_thread(
                self._run_silence_loop,
                "murmly-silence",
                self._silence_interval(),
            )
        if wants_partials:
            self._spawn_live_thread(
                self._run_partial_loop,
                "murmly-partial",
                max(self._config.live_interval_ms / 1_000, MIN_LIVE_TICK_SECONDS),
            )

    def _silence_interval(self) -> float:
        """How often to re-measure the trailing window.

        Each tick re-runs the voice activity model over the whole window, so a
        fixed rate would recompute a 30 s window four times a second. Scaling with
        the window keeps the cost flat while staying far below the threshold the
        reading has to detect.
        """
        window = self._silence.window_seconds if self._silence is not None else 0.0
        return min(max(SILENCE_TICK_SECONDS, window / 40), MAX_SILENCE_TICK_SECONDS)

    def _spawn_live_thread(self, loop, name: str, interval: float) -> None:
        thread = threading.Thread(
            target=loop,
            args=(self._live_stop, interval),
            name=name,
            daemon=True,
        )
        try:
            thread.start()
        except RuntimeError as error:
            logger.warning("Live worker %s did not start: %s", name, error)
            return
        self._live_threads.append(thread)

    def _stop_live_worker(self) -> None:
        threads = list(self._live_threads)
        self._live_stop.set()
        for thread in threads:
            if thread is threading.current_thread():
                continue
            thread.join(timeout=LIVE_WORKER_JOIN_SECONDS)
            if thread.is_alive():
                # A pass already inside the engine cannot be interrupted. Its
                # result is discarded by the transcriber, so the final pass
                # proceeds now rather than waiting on it.
                logger.debug("Live worker %s did not exit within the join timeout.", thread.name)
        self._live_threads = [
            thread for thread in threads if thread is threading.current_thread() and thread.is_alive()
        ]

    def _run_silence_loop(self, stop_event: threading.Event, interval: float) -> None:
        self._run_tick_loop(stop_event, interval, self._silence_tick, "silence")

    def _run_partial_loop(self, stop_event: threading.Event, interval: float) -> None:
        self._run_tick_loop(stop_event, interval, self._partial_tick, "partial")

    def _run_tick_loop(self, stop_event: threading.Event, interval: float, tick, label: str) -> None:
        # Per loop, not shared: a healthy loop resetting the counter would hide a
        # neighbour failing every tick, and a broken one could kill a healthy one.
        failures = 0
        next_tick = self._clock() + interval
        while True:
            delay = next_tick - self._clock()
            if stop_event.wait(delay if delay > 0 else MIN_LIVE_TICK_SECONDS):
                return
            try:
                tick()
                failures = 0
            except Exception as error:
                failures += 1
                logger.warning(
                    "Live worker %s tick failed (%d/%d): %s",
                    label,
                    failures,
                    MAX_TICK_FAILURES,
                    error,
                )
                if failures >= MAX_TICK_FAILURES:
                    return
            # Advance against the time the tick finished: a slow tick would
            # otherwise leave the schedule in the past and fire a catch-up burst.
            after = self._clock()
            while next_tick <= after:
                next_tick += interval

    def _silence_tick(self) -> None:
        if self._silence is None or not self._silence.available:
            return
        reading = self._silence.observe(self._recorder.snapshot(self._silence.window_seconds))
        if reading.triggered and self._on_silence is not None:
            self._on_silence()

    def _partial_tick(self) -> None:
        if not self._config.live_transcribe or not self._transcriber.partials_available:
            return
        audio = self._recorder.snapshot(self._config.live_window_seconds)
        text = self._transcriber.transcribe_partial(audio, self._recorder.sample_rate_hz)
        if text is not None and self._partial_sink is not None:
            self._partial_sink(text)

    def _ensure_paster(self) -> ClipboardPaster:
        if self._paster is None:
            self._paster = ClipboardPaster(
                restore_clipboard=self._config.restore_clipboard,
                restore_delay_ms=self._config.restore_clipboard_delay_ms,
            )
        return self._paster


class _Adopted:
    """Sentinel: this connection became a speech session and owes no response."""


ADOPT_SESSION = _Adopted()


class SpeechSessionConnection:
    """A connection a caller declared a speech session.

    Not the same thing as `SpeechSession` above, which is a *capture* session:
    the microphone, the transcriber and the delivery path for one recording. The
    word is overloaded because it is overloaded in the specs; this one is the
    socket a sender holds open while it streams text and reads what was heard.

    Reads many frames and writes many, which is the exception
    `command-interface` carves for exactly this. Every other connection is still
    read by `_read_request` and answered once by `_write_response`.
    """

    def __init__(
        self,
        connection: socket.socket,
        on_frame: Callable[[dict[str, object]], None],
        on_closed: Callable[[SpeechSessionConnection], None],
        shutdown: threading.Event,
    ) -> None:
        self._connection = connection
        self._on_frame = on_frame
        self._on_closed = on_closed
        self._shutdown = shutdown
        self._outbox: deque[dict[str, object] | None] = deque()
        self._outbox_ready = threading.Event()
        self._outbox_lock = threading.Lock()
        self._closing = threading.Event()
        self._reader: threading.Thread | None = None
        self._writer: threading.Thread | None = None
        # Its own handle on the same connection, with its own timeout. One
        # socket object cannot carry a short read timeout and a longer write one
        # at the same time, and two threads changing one timeout would race.
        self._write_handle = connection.dup()
        self._write_handle.settimeout(SESSION_SEND_TIMEOUT_SECONDS)

    @property
    def connection(self) -> socket.socket:
        return self._connection

    def dispose(self) -> None:
        """Release the duplicated handle of a session that never started.

        Only that handle: the original is still owed the refusal that is about
        to be written on it.
        """
        try:
            self._write_handle.close()
        except OSError:
            pass

    def start(self) -> None:
        self._connection.settimeout(SESSION_POLL_SECONDS)
        self._writer = threading.Thread(
            target=self._write_loop, name="murmly-speech-writer", daemon=True
        )
        self._writer.start()
        self._reader = threading.Thread(
            target=self._read_loop, name="murmly-speech-session", daemon=True
        )
        self._reader.start()

    def send(self, frame: dict[str, object]) -> None:
        """Queue one frame for this session, never waiting on it.

        A session that will not read what it is sent is disconnected rather than
        allowed to hold up the playback thread, which is the posture the level
        meter takes with a sink that raises.
        """
        if self._closing.is_set():
            return
        with self._outbox_lock:
            if len(self._outbox) >= SESSION_EVENT_BACKLOG:
                logger.warning("Disconnecting a speech session that is not reading its events.")
                self._closing.set()
                self._outbox.clear()
                self._outbox.append(None)
                self._outbox_ready.set()
                self._shutdown_socket()
                return
            self._outbox.append(frame)
            self._outbox_ready.set()

    def close(self, *, drain: bool = False) -> None:
        """Stop this session, optionally letting queued frames reach the peer."""
        if not self._closing.is_set():
            self._closing.set()
            with self._outbox_lock:
                self._outbox.append(None)
                self._outbox_ready.set()
        writer = self._writer
        if drain and writer is not None and writer is not threading.current_thread():
            writer.join(timeout=SESSION_WRITER_JOIN_SECONDS)
        self._shutdown_socket()

    def _shutdown_socket(self) -> None:
        for handle in (self._connection, self._write_handle):
            try:
                handle.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                handle.close()
            except OSError:
                pass

    def _write_loop(self) -> None:
        while True:
            if not self._outbox_ready.wait(SESSION_POLL_SECONDS):
                continue
            with self._outbox_lock:
                if not self._outbox:
                    self._outbox_ready.clear()
                    continue
                frame = self._outbox.popleft()
                if not self._outbox:
                    self._outbox_ready.clear()
            if frame is None:
                return
            try:
                self._write_handle.sendall((json.dumps(frame) + "\n").encode("utf-8"))
            except OSError as error:
                if not self._shutdown.is_set():
                    logger.debug("A speech session event could not be written: %s", error)
                self._closing.set()
                return

    def _read_loop(self) -> None:
        payload = bytearray()
        try:
            while not self._closing.is_set() and not self._shutdown.is_set():
                try:
                    chunk = self._connection.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                payload.extend(chunk)
                while b"\n" in payload:
                    line, _, rest = payload.partition(b"\n")
                    payload = bytearray(rest)
                    self._deliver(bytes(line))
                if len(payload) > MAX_SPEECH_FRAME_BYTES:
                    self.send(
                        failure_response(
                            CommandCode.MALFORMED_REQUEST,
                            f"A speech session frame exceeds the "
                            f"{MAX_SPEECH_FRAME_BYTES}-byte limit.",
                        )
                    )
                    return
        finally:
            self._on_closed(self)

    def _deliver(self, line: bytes) -> None:
        if not line.strip():
            return
        try:
            frame = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            # Reported rather than fatal: one unreadable frame in a long
            # exchange must not silently end it.
            self.send(
                failure_response(
                    CommandCode.MALFORMED_REQUEST, f"Frame could not be read: {error}"
                )
            )
            return
        if not isinstance(frame, dict):
            self.send(
                failure_response(CommandCode.MALFORMED_REQUEST, "Frame is not a JSON object.")
            )
            return
        self._on_frame(frame)


class MurmlyDaemon:
    def __init__(
        self,
        config: MurmlyConfig,
        session: SpeechSession | None = None,
        overlay: OverlayLifecycle | None = None,
        peer_identity: Callable[[socket.socket], int | None] = read_peer_identity,
        speech: SpeechEngine | None = None,
    ) -> None:
        self._config = config
        self._peer_identity = peer_identity
        # Before the overlay, which spawns a renderer from its constructor: a
        # daemon that refuses to run must not start anything first.
        self._require_private_socket_path()
        self._overlay = overlay or self._create_overlay(config)
        self._session = session or SpeechSession(
            config,
            level_sink=self._publish_level,
            partial_sink=self._publish_partial,
            on_silence=self._on_silence,
        )
        self._speech = speech if speech is not None else SpeechEngine(config)
        # The connection a sender holds open, at most one at a time. With two
        # open, "deliver the transcript to that session" and "tell the session it
        # was interrupted" would both have to guess which one was meant.
        self._speech_session: SpeechSessionConnection | None = None
        self._speech_session_lock = threading.Lock()
        self._capture_destination = DESTINATION_WINDOW
        self._segments: list[str] = []
        self._session_delivered = True
        # Held for the whole produce-and-deliver of one unit of audio, by both the
        # live worker and the toggle path. Mutual exclusion here is what keeps
        # segment order, `_segments`, and `_session_delivered` consistent.
        self._unit_lock = threading.Lock()
        self._segment_thread: threading.Thread | None = None
        self._partial_sink_owner = self._publish_partial
        self._state = "IDLE"
        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        self._server: socket.socket | None = None
        self._worker_slots = threading.BoundedSemaphore(MAX_COMMAND_WORKERS)
        self._connections_lock = threading.Lock()
        self._connections: set[socket.socket] = set()
        # The subset whose request has already been read and whose one response is
        # still owed. Shutdown waits for these and for no others: a peer that has
        # not spoken has nothing to be told.
        self._answering: set[socket.socket] = set()
        # Connections whose response has been claimed by whoever will write it.
        # Shutdown answers a command that outlives the drain, so the claim is what
        # keeps it and the worker from both writing to the same connection.
        self._claimed: set[socket.socket] = set()

    @property
    def state(self) -> str:
        """The daemon state, including the one that means output is active.

        Derived rather than stored, so every existing transition is untouched:
        speech only ever runs while capture is not, so it can only be seen in
        place of IDLE.
        """
        with self._lock:
            state = self._state
        if state == STATE_IDLE and self._speech.speaking:
            return STATE_SPEAKING
        return state

    @property
    def speech(self) -> SpeechEngine:
        return self._speech

    def serve_forever(self) -> None:
        try:
            # Re-checked here, before the unlink below deletes whatever sits at
            # the configured path: that unlink must never run against a path
            # Murmly would refuse.
            self._require_private_socket_path()
            if not peer_identity_supported():
                logger.warning(
                    "This platform cannot report the account behind a connection. The "
                    "command socket is protected by its file permissions alone."
                )
            try:
                create_socket_directory(self._config.socket_path.parent)
            except OSError as error:
                # Named as the startup refusal it is. Left as an OSError it would
                # reach the caller's backstop and be reported as an unexpected
                # failure, which is the one thing it is not.
                raise DaemonStartupError(
                    f"Refusing to serve at {self._config.socket_path}. Its directory "
                    f"could not be created: {error}."
                ) from error
            try:
                self._config.socket_path.unlink(missing_ok=True)
            except OSError as error:
                raise DaemonStartupError(
                    f"Refusing to serve at {self._config.socket_path}. What is already "
                    f"at that path could not be removed: {error}."
                ) from error
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                    self._server = server
                    try:
                        server.bind(str(self._config.socket_path))
                        # The window between bind and chmod is accepted. What
                        # keeps it closed is the umask, which leaves the node
                        # unwritable by other accounts even before the chmod; the
                        # directory rule above does not, since it deliberately
                        # serves a directory others can traverse.
                        self._config.socket_path.chmod(SOCKET_MODE)
                        server.listen()
                    except OSError as error:
                        raise DaemonStartupError(
                            f"Refusing to serve at {self._config.socket_path}. Its socket "
                            f"could not be created: {error}."
                        ) from error
                    server.settimeout(0.2)
                    while not self._shutdown.is_set():
                        try:
                            connection, _address = server.accept()
                        except socket.timeout:
                            continue
                        except OSError:
                            if self._shutdown.is_set():
                                break
                            raise
                        # Checked before a worker slot is taken: inside a worker, a
                        # foreign account could occupy every slot and deny service to
                        # the owner -- the pool this change is otherwise hardening.
                        if not self._peer_permitted(connection):
                            self._refuse(
                                connection,
                                failure_response(
                                    CommandCode.NOT_PERMITTED,
                                    "Murmly serves only the account it runs as.",
                                ),
                            )
                            connection.close()
                            continue
                        self._dispatch_connection(connection)
            finally:
                self._server = None
                try:
                    self._config.socket_path.unlink(missing_ok=True)
                except OSError as error:
                    # Reported rather than raised: a failure to clean up must not
                    # replace the reason the daemon is unwinding.
                    logger.warning("The command socket could not be removed: %s", error)
        finally:
            # Outside the socket's own unwinding, because a refusal that happens
            # before the socket exists still has an overlay to close: the
            # constructor started one.
            self._close_overlay()

    def _require_private_socket_path(self) -> None:
        detail = socket_path_detail(self._config.socket_path)
        if detail is None:
            return
        # The remedy belongs to the detail rather than to this message: the
        # directory at fault is not always the socket's own parent, and an
        # ownership fault is not corrected the way a mode fault is.
        raise DaemonStartupError(f"Refusing to serve at {self._config.socket_path}. {detail}")

    def _peer_permitted(self, connection: socket.socket) -> bool:
        """Whether the account behind this connection is the one Murmly serves.

        An identity this platform cannot report is served rather than refused,
        which is reported at startup and in diagnostics: refusing would make the
        daemon unusable on a platform whose only fault is not offering the check.
        """
        uid = self._peer_identity(connection)
        if uid is None:
            return True
        if uid == os.getuid():
            return True
        logger.warning("Refused a command from uid %d.", uid)
        return False

    def shutdown(self) -> None:
        self._shutdown.set()
        server = self._server
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        # Before the drain, and before the blanket close below: a session has to
        # be told what happened while its connection is still open.
        self._close_speech_session()
        self._drain_answering()
        self._answer_the_rest()
        # A worker that took the claim before the drain expired is writing its
        # own response right now. It stays owed until that write returns, so this
        # second wait is what keeps the close below from truncating it.
        self._drain_answering()
        with self._connections_lock:
            connections = tuple(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        self._config.socket_path.unlink(missing_ok=True)
        self._close_overlay()

    def _close_speech_session(self) -> None:
        """Stop speech, tell the session, and close it."""
        with self._speech_session_lock:
            session = self._speech_session
            self._speech_session = None
        try:
            self._speech.end()
        except Exception as error:  # noqa: BLE001 - shutdown continues regardless
            logger.warning("Speech output did not stop cleanly: %s", error)
        if session is None:
            return
        session.send({"event": EVENT_SHUTTING_DOWN, "code": CommandCode.SHUTTING_DOWN})
        session.close(drain=True)
        with self._connections_lock:
            self._connections.discard(session.connection)

    def _drain_answering(self) -> None:
        """Let a worker whose request was read write its response before the close.

        Waits only for connections that carry a request. Waiting for a peer that
        connected and never spoke would make shutdown latency depend on an idle
        client, and there is nothing to tell it.
        """
        deadline = time.monotonic() + SHUTDOWN_DRAIN_SECONDS
        while True:
            with self._connections_lock:
                answering = len(self._answering)
            if answering == 0:
                return
            if time.monotonic() >= deadline:
                logger.warning("Shutting down with %d command(s) still answering.", answering)
                return
            time.sleep(SHUTDOWN_DRAIN_POLL_SECONDS)

    def _answer_the_rest(self) -> None:
        """Answer the commands that outlived the drain instead of closing on them.

        A transcription runs for seconds, far longer than the drain will wait, so
        the drain alone would still leave an empty read on the one case this
        exists for: the service restarting mid-transcription. The claim is what
        makes this safe -- the worker finds the connection already answered and
        writes nothing, so the connection still carries exactly one response. A
        connection whose claim is already taken is left alone: a worker holds it
        and is writing its own response, which the second drain waits for.
        """
        with self._connections_lock:
            answering = tuple(self._answering)
        for connection in answering:
            if not self._claim_response(connection):
                continue
            try:
                self._refuse(
                    connection,
                    failure_response(CommandCode.SHUTTING_DOWN, "Murmly is shutting down."),
                )
            finally:
                self._finish_response(connection)

    def _claim_response(self, connection: socket.socket) -> bool:
        """Take the exclusive right to write this connection's one response.

        Taking the claim does not discharge what the connection is owed: the
        response is still unwritten here. Shutdown waits on what is owed, so the
        connection stays in `_answering` until `_finish_response`. Discarding it
        here instead would let the drain find nothing to wait for and close on a
        response already on its way.
        """
        with self._connections_lock:
            if connection in self._claimed:
                return False
            self._claimed.add(connection)
            return True

    def _finish_response(self, connection: socket.socket) -> None:
        """Record that this connection's one response has been written, or cannot be."""
        with self._connections_lock:
            self._answering.discard(connection)

    def _serve_connection(self, connection: socket.socket) -> None:
        adopted = False
        try:
            connection.settimeout(COMMAND_TIMEOUT_SECONDS)
            try:
                response = self._answer(connection)
            except OSError:
                # The connection itself failed. There is nothing left to
                # answer on, so this is reported rather than replied to.
                raise
            except Exception as error:  # noqa: BLE001 - an accepted connection is answered
                response = self._unexpected_failure(error)
            if isinstance(response, _Adopted):
                # A speech session now. It owes no single response, it must not
                # be closed here, and it must not hold a worker slot for the
                # length of an exchange -- eight open sessions would otherwise
                # deny the state query and the capture toggle.
                adopted = True
                return
            self._write_response(connection, response)
        except OSError as error:
            if not self._shutdown.is_set():
                logger.warning("Command connection failed: %s", error)
        finally:
            with self._connections_lock:
                self._answering.discard(connection)
                self._claimed.discard(connection)
                if not adopted:
                    self._connections.discard(connection)
            if not adopted:
                try:
                    connection.close()
                except OSError:
                    pass
            self._worker_slots.release()

    def _write_response(self, connection: socket.socket, response: dict[str, object]) -> None:
        if not self._claim_response(connection):
            # Shutdown answered this connection while the command was still
            # running. A second frame would break the one-response rule.
            return
        try:
            connection.sendall((json.dumps(response) + "\n").encode("utf-8"))
        except OSError:
            # A peer that closed first, or a connection shutdown force-closed:
            # the response is discarded rather than retried.
            if not self._shutdown.is_set():
                raise
        finally:
            # Discharged whether or not the bytes landed. Nothing further will be
            # written on this connection, so shutdown must stop waiting for it.
            self._finish_response(connection)

    def _unexpected_failure(self, error: Exception) -> dict[str, object]:
        """Answer a command that failed in a way Murmly does not anticipate.

        A worker thread that dies takes its response with it, and the caller
        cannot tell that empty read from a daemon that crashed. The shape checks
        answer the failures Murmly expects; this answers the rest.
        """
        if self._shutdown.is_set():
            return failure_response(CommandCode.SHUTTING_DOWN, "Murmly is shutting down.")
        logger.warning("Command handling failed: %s", error, exc_info=error)
        return failure_response(CommandCode.COMMAND_FAILED, f"Command failed: {error}")

    def _refuse(self, connection: socket.socket, response: dict[str, object]) -> None:
        """Answer a connection that never reaches a worker.

        Written on the accept loop, so the send timeout comes first and a failed
        write is discarded rather than retried: a peer that connects and never
        reads must not hold up the next command.
        """
        try:
            connection.settimeout(REFUSAL_SEND_TIMEOUT_SECONDS)
            connection.sendall((json.dumps(response) + "\n").encode("utf-8"))
        except OSError as error:
            logger.debug("A refusal could not be delivered: %s", error)

    def _dispatch_connection(self, connection: socket.socket) -> bool:
        if not self._worker_slots.acquire(blocking=False):
            # Written here rather than from a worker: the bound exists to stop
            # this connection from taking one, and a reserved permit would only
            # move the refusal to the next connection.
            self._refuse(
                connection,
                failure_response(
                    CommandCode.OVER_CAPACITY,
                    f"Murmly is already handling {MAX_COMMAND_WORKERS} commands.",
                ),
            )
            connection.close()
            return False
        started = False
        refusal: dict[str, object] | None = None
        try:
            with self._connections_lock:
                if self._shutdown.is_set():
                    refusal = failure_response(
                        CommandCode.SHUTTING_DOWN, "Murmly is shutting down."
                    )
                    return False
                self._connections.add(connection)
                thread = threading.Thread(
                    target=self._serve_connection,
                    args=(connection,),
                    name="murmly-command",
                    daemon=True,
                )
                try:
                    thread.start()
                except RuntimeError:
                    self._connections.discard(connection)
                    raise
                started = True
            return True
        except RuntimeError as error:
            logger.warning("Unable to start command worker: %s", error)
            refusal = failure_response(
                CommandCode.COMMAND_FAILED, "Murmly could not start a command worker."
            )
            return False
        finally:
            # Both refusals are written after the connections lock is released:
            # a send must never run under it. Neither release site moves, because
            # a second release on a BoundedSemaphore raises.
            if not started:
                if refusal is not None:
                    self._refuse(connection, refusal)
                connection.close()
                self._worker_slots.release()

    def _answer(self, connection: socket.socket) -> dict[str, object] | _Adopted:
        """Produce the one response this connection is owed.

        Or `ADOPT_SESSION`, for the one connection type that is owed none: a
        caller that declared itself a speech session and exchanges frames until
        it goes away.
        """
        try:
            request = self._read_request(connection)
        except RequestError as error:
            return failure_response(CommandCode.MALFORMED_REQUEST, str(error))
        except socket.timeout:
            return failure_response(
                CommandCode.MALFORMED_REQUEST,
                f"No request arrived within {COMMAND_TIMEOUT_SECONDS:g} seconds.",
            )
        except UnicodeDecodeError:
            return failure_response(
                CommandCode.MALFORMED_REQUEST, "Request is not valid UTF-8 text."
            )
        except json.JSONDecodeError as error:
            return failure_response(
                CommandCode.MALFORMED_REQUEST, f"Request is not valid JSON: {error}"
            )
        if self._shutdown.is_set():
            return failure_response(CommandCode.SHUTTING_DOWN, "Murmly is shutting down.")
        if isinstance(request, dict) and request.get("command") == COMMAND_SPEECH_SESSION:
            return self._declare_session(connection)
        return self._dispatch_request(request)

    def _read_request(self, connection: socket.socket) -> object:
        payload = bytearray()
        while not payload.endswith(b"\n"):
            chunk = connection.recv(4096)
            if not chunk:
                break
            if not payload:
                # The peer has spoken, so an answer is owed from here -- including
                # one that reports the request could not be read. Registered
                # before the request is decoded, because a registration after the
                # decode leaves a window where shutdown closes on a connection it
                # already owes a response.
                with self._connections_lock:
                    self._answering.add(connection)
            if len(payload) + len(chunk) > MAX_COMMAND_BYTES:
                raise RequestError("Command exceeds the 4096-byte limit.")
            payload.extend(chunk)
        return json.loads(payload.decode("utf-8") or "{}")

    def _dispatch_request(self, request: object) -> dict[str, object]:
        """Check the request's shape, then run the command it names.

        Checked rather than caught: a caught AttributeError would report a
        request problem for a class of bugs that are not request problems.
        """
        if not isinstance(request, dict):
            return failure_response(
                CommandCode.MALFORMED_REQUEST, "Request is not a JSON object."
            )
        command = request.get("command", "")
        if not isinstance(command, str):
            return failure_response(
                CommandCode.UNSUPPORTED_COMMAND, f"Unsupported command: {command!r}"
            )
        return self.handle_command(command)

    def _declare_session(self, connection: socket.socket) -> dict[str, object] | _Adopted:
        """Accept a speech session, or refuse it with one response saying why."""
        if not self._config.tts_enabled:
            return failure_response(
                CommandCode.SPEECH_DISABLED,
                "Speech output is not enabled. Set enabled = true under [tts].",
            )
        if not self._speech.available:
            return failure_response(
                CommandCode.SPEECH_UNAVAILABLE,
                f"Speech output is unavailable: {self._speech.unavailable_reason}",
            )

        with self._speech_session_lock:
            if self._speech_session is not None:
                return failure_response(
                    CommandCode.SPEECH_SESSION_IN_USE,
                    "A speech session is already open.",
                )
            session = SpeechSessionConnection(
                connection,
                self._handle_session_frame,
                self._session_closed,
                self._shutdown,
            )
            try:
                # Opens the output device, so a session that cannot be given one
                # is refused now rather than accepted and failed once somebody
                # is listening.
                self._speech.begin(session.send)
            except Exception as error:  # noqa: BLE001 - a refusal, not a crash
                session.dispose()
                return failure_response(
                    CommandCode.SPEECH_UNAVAILABLE,
                    f"Speech output could not be started: {error}",
                )
            self._speech_session = session

        # Nothing further is owed on this connection as a one-shot command, so
        # shutdown must stop waiting to answer it.
        with self._connections_lock:
            self._answering.discard(connection)
            self._claimed.discard(connection)
        session.start()
        session.send({"ok": True, "session": "speech"})
        return ADOPT_SESSION

    def _handle_session_frame(self, frame: dict[str, object]) -> None:
        """Act on one frame a speech session sent."""
        command = frame.get("command")
        if command == "speak":
            name = frame.get("name")
            text = frame.get("text")
            if not isinstance(name, str) or not name or not isinstance(text, str):
                self._send_to_session(
                    failure_response(
                        CommandCode.MALFORMED_REQUEST,
                        "A speak frame needs a name and text.",
                    )
                )
                return
            self._speech.speak(name, text)
            return
        if command == "end":
            self._speech.end_input()
            return
        if command == "cancel":
            self._report_interruption(self._speech.interrupt())
            return
        self._send_to_session(
            failure_response(
                CommandCode.UNSUPPORTED_COMMAND, f"Unsupported command: {command!r}"
            )
        )

    def _session_closed(self, session: SpeechSessionConnection) -> None:
        """A session's connection ended, for any reason.

        Speech that outlives its sender is speech nobody can stop through the
        interface that produced it, so it stops here and the queue goes with it.
        """
        with self._speech_session_lock:
            if self._speech_session is not session:
                return
            self._speech_session = None
        self._speech.end()
        session.close()
        with self._connections_lock:
            self._connections.discard(session.connection)

    def _send_to_session(self, frame: dict[str, object]) -> bool:
        with self._speech_session_lock:
            session = self._speech_session
        if session is None:
            return False
        session.send(frame)
        return True

    def _report_interruption(self, interruption: Interruption | None) -> None:
        if interruption is None:
            return
        self._send_to_session(
            {
                "event": EVENT_INTERRUPTED,
                "code": CommandCode.SPEECH_INTERRUPTED,
                "playing": interruption.playing,
                "pending": list(interruption.pending),
            }
        )

    def _barge_in(self) -> None:
        """Stop speech and close the output before the microphone opens.

        Both hotkeys do this, because a sender has to stop generating whoever
        the person was talking to. Playback and capture never overlap, which is
        what makes echo cancellation unnecessary.
        """
        interruption = self._speech.suspend()
        self._report_interruption(interruption)

    def handle_command(self, command: str) -> dict[str, object]:
        if command == COMMAND_STATUS:
            return {"ok": True, "state": self.state}
        if command not in (COMMAND_TOGGLE, COMMAND_TOGGLE_SESSION):
            return failure_response(
                CommandCode.UNSUPPORTED_COMMAND, f"Unsupported command: {command}"
            )
        # Which hotkey was pressed decides where the transcript goes, and it is
        # fixed here rather than inferred when the transcript is ready.
        destination = (
            DESTINATION_SESSION if command == COMMAND_TOGGLE_SESSION else DESTINATION_WINDOW
        )

        with self._lock:
            if self._state == STATE_IDLE:
                self._segments = []
                self._session_delivered = True
                self._capture_destination = destination
                # Before the microphone opens, never after. The silence
                # detector, live partials and the final transcript all read the
                # live stream, and each would hear Murmly.
                self._barge_in()
                try:
                    self._session.start_recording()
                except Exception as error:
                    self._publish_error()
                    self._speech.resume()
                    return failure_response(CommandCode.COMMAND_FAILED, str(error), state="IDLE")
                self._state = STATE_LISTENING
                state = self._state
                self._publish_state(OverlayState.LISTENING)
                return {"ok": True, "state": state}
            if self._state != STATE_LISTENING:
                return failure_response(
                    CommandCode.BUSY, "Daemon is busy.", state=self._state
                )
            self._state = STATE_THINKING
        # Claimed before stopping capture so an in-flight segment finishes first;
        # otherwise its transcript could paste after this one, out of order.
        with self._unit_lock:
            try:
                pcm_audio = self._session.stop_recording()
            except Exception as error:
                with self._lock:
                    self._state = "IDLE"
                self._publish_error()
                return failure_response(CommandCode.COMMAND_FAILED, str(error), state="IDLE")
            # Published only once capture has stopped: the processing presentation
            # must never be shown while the waveform still represents live input.
            self._publish_state(OverlayState.THINKING)
            target = self._delivery_target()
            return self._finish_toggle(pcm_audio, target)

    def _delivery_target(self) -> WindowIdentity | None:
        """The window this capture is for, or None when it is for a session.

        A session-bound capture records no window and verifies none: its
        recipient was known when capture started and cannot change while capture
        runs, which is the whole point of recording a target before
        transcription.
        """
        if self._capture_destination == DESTINATION_SESSION:
            return None
        return self._session.capture_delivery_target()

    def _process_recording(
        self,
        pcm_audio: bytes,
        target: WindowIdentity | None,
    ) -> ProcessingResult:
        if self._capture_destination == DESTINATION_SESSION:
            return self._session.process_for_session(pcm_audio, self._deliver_to_session)
        return self._session.process_recording(pcm_audio, target)

    def _deliver_to_session(self, text: str) -> bool:
        return self._send_to_session({"event": EVENT_TRANSCRIPT, "text": text})

    def _finish_toggle(self, pcm_audio: bytes, target: WindowIdentity | None) -> dict[str, object]:

        try:
            result = self._process_recording(pcm_audio, target)
            if result.detail is None:
                self._publish_state(OverlayState.IDLE)
            else:
                self._publish_error()
            return self._session_response(result)
        except Exception as error:
            self._publish_error()
            return failure_response(CommandCode.COMMAND_FAILED, str(error), state="IDLE")
        finally:
            with self._lock:
                self._state = STATE_IDLE
            # After delivery, so a session that was interrupted receives its
            # transcript before whatever the sender says next.
            self._speech.resume()

    def _session_response(self, result: ProcessingResult) -> dict[str, object]:
        """Report the whole session, keeping single-transcript responses identical."""
        with self._lock:
            segments = list(self._segments)
            session_delivered = self._session_delivered
        texts = [text for text in (*segments, result.text) if text]
        if segments:
            # A final segment that produced no text cannot make the session undelivered.
            delivered = session_delivered and (result.delivered or not result.text)
        else:
            delivered = result.delivered
        response: dict[str, object] = {
            "ok": True,
            "state": result.state,
            "text": " ".join(texts),
            "delivered": delivered,
        }
        if len(texts) > 1:
            response["segments"] = len(texts)
        detail = result.detail
        if detail is None and segments and not delivered:
            detail = "Transcript copied to the clipboard but not pasted."
        if detail is not None:
            response["detail"] = detail
        return response

    def _on_silence(self) -> None:
        mode = self._config.auto_transcribe
        if mode == "stop":
            self._begin_auto_stop()
        elif mode == "continuous":
            self._deliver_segment()

    def _claim_listening(self, *, advance: bool) -> bool:
        """Take the state lock without waiting, and only while still listening.

        Never blocks: a toggle already holding the lock is stopping this very
        recording, and it also joins this worker thread. Waiting here would stall
        that toggle for the whole join timeout, so an in-flight toggle simply wins.
        """
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if self._state != "LISTENING":
                return False
            if advance:
                self._state = "THINKING"
            return True
        finally:
            self._lock.release()

    def _begin_auto_stop(self) -> None:
        # Claiming THINKING before releasing the lock means a toggle arriving now
        # is told the daemon is busy instead of starting a second stop.
        if not self._claim_listening(advance=True):
            return
        # Off the live worker thread: stopping capture joins that worker, and a
        # thread cannot join itself.
        self._start_transition(self._finish_auto_stop, "murmly-auto-stop")

    def _finish_auto_stop(self) -> None:
        try:
            pcm_audio = self._session.stop_recording()
            # Published only once capture has stopped, matching the toggle path:
            # the processing presentation must never be shown while the waveform
            # still represents live input.
            self._publish_state(OverlayState.THINKING)
            target = self._delivery_target()
            result = self._process_recording(pcm_audio, target)
            if result.detail is None:
                self._publish_state(OverlayState.IDLE)
            else:
                self._publish_error()
        except Exception as error:
            logger.warning("Auto-transcribe stop failed: %s", error)
            self._publish_error()
        finally:
            with self._lock:
                self._state = STATE_IDLE
            self._speech.resume()

    def _deliver_segment(self) -> None:
        """Hand the segment to its own thread and return to polling.

        Decode plus paste plus the clipboard restore delay runs for seconds; on
        the silence thread that would black out detection for the whole time and
        swallow the next pause -- the defect the separate thread exists to avoid.
        """
        if self._segment_thread is not None and self._segment_thread.is_alive():
            return
        try:
            thread = threading.Thread(
                target=self._run_segment,
                name="murmly-segment",
                daemon=True,
            )
            thread.start()
        except RuntimeError as error:
            logger.warning("Unable to start segment delivery: %s", error)
            return
        self._segment_thread = thread

    def _run_segment(self) -> None:
        # Non-blocking: a toggle already holding this is ending the recording, so
        # it should win rather than queue behind another segment.
        if not self._unit_lock.acquire(blocking=False):
            return
        try:
            if not self._claim_listening(advance=False):
                return
            segment = self._session.take_segment()
            target = self._delivery_target()
            try:
                result = self._process_recording(segment, target)
            except Exception as error:
                logger.warning("Segment transcription failed: %s", error)
                self._end_continuous_session()
                return
            if not result.text:
                return
            with self._lock:
                self._segments.append(result.text)
                if not result.delivered:
                    self._session_delivered = False
            # This text has been pasted; leaving it under the indicator would show
            # the user speech that is no longer pending.
            if self._partial_sink_owner is not None:
                self._partial_sink_owner("")
            if not result.delivered:
                # Claimed under the unit lock: releasing first lets a toggle take
                # the session over and deliver normally, leaving the user a
                # success overlay and a failure response for the same session.
                self._end_continuous_session()
        finally:
            self._unit_lock.release()

    def _end_continuous_session(self) -> None:
        if not self._claim_listening(advance=True):
            return
        self._start_transition(self._finish_continuous_session, "murmly-auto-end")

    def _start_transition(self, target, name: str) -> None:
        """Run a state transition off the live worker thread.

        The state is already THINKING by the time this is called, so a thread
        that never starts would leave the daemon wedged there with the microphone
        open and every later toggle answered "busy". Failing back to IDLE keeps
        the daemon usable.
        """
        try:
            threading.Thread(target=target, name=name, daemon=True).start()
        except RuntimeError as error:
            logger.warning("Unable to start %s: %s", name, error)
            try:
                self._session.stop_recording()
            except Exception as stop_error:
                logger.warning("Unable to stop capture after a failed transition: %s", stop_error)
            self._publish_error()
            with self._lock:
                self._state = "IDLE"
            self._speech.resume()

    def _finish_continuous_session(self) -> None:
        try:
            # The audio captured since the refused segment closed is discarded:
            # the session is ending because delivery was refused, so delivering
            # more of it would repeat the mistake.
            self._session.stop_recording()
        except Exception as error:
            logger.warning("Ending a continuous session failed: %s", error)
        finally:
            self._publish_error()
            with self._lock:
                self._state = "IDLE"
            # Every path that ends capture releases the hold, this one included:
            # text a sender queued while the person was speaking is owed its turn
            # as soon as the microphone closes, however the recording ended.
            self._speech.resume()

    @staticmethod
    def _create_overlay(config: MurmlyConfig) -> OverlayLifecycle:
        if not config.overlay_enabled:
            return NullOverlayController()
        backend = detect_overlay_backend()
        if backend is None:
            return NullOverlayController("Overlay requires KDE Plasma on X11 or Wayland.")
        return OverlayController(
            bottom_margin_px=config.overlay_bottom_margin_px,
            reduced_motion=config.overlay_reduced_motion,
            text_size_px=config.overlay_text_size_px,
            transcript_panel=config.live_transcribe,
            backend=backend,
        )

    def _publish_state(self, state: OverlayState) -> None:
        try:
            self._overlay.publish_state(state)
        except Exception as error:
            logger.warning("Overlay state update failed: %s", error)

    def _publish_level(self, level: float) -> None:
        try:
            self._overlay.publish_level(level)
        except Exception as error:
            logger.warning("Overlay level update failed: %s", error)

    def _publish_partial(self, text: str) -> None:
        try:
            self._overlay.publish_partial(text)
        except Exception as error:
            # Deliberately no transcript text in the log line.
            logger.warning("Overlay partial update failed: %s", error)

    def _publish_error(self) -> None:
        try:
            self._overlay.publish_error()
        except Exception as error:
            logger.warning("Overlay error update failed: %s", error)

    def _close_overlay(self) -> None:
        try:
            self._overlay.close()
        except Exception as error:
            logger.warning("Overlay shutdown failed: %s", error)


def send_command(
    socket_path: str,
    command: str,
    connect_timeout: float = CLIENT_CONNECT_TIMEOUT_SECONDS,
    response_timeout: float = CLIENT_RESPONSE_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Send one command over the command socket and return the one response.

    The two bounds differ because the two waits do. Reaching a UNIX socket either
    happens at once or is not going to, so a short bound is right for it. Waiting
    for the answer is not that: a toggle that stops a recording answers only once
    the transcription is done, which takes as long as the audio deserves. Both
    are bounded, because a hotkey press has nowhere to show a caller that never
    returns.

    A response that never arrives is reported as the daemon not responding rather
    than as a timeout, which is what it is from here: the daemon took the request,
    so restarting the service would answer a question the caller did not ask.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(connect_timeout)
        client.connect(socket_path)
        client.sendall((json.dumps({"command": command}) + "\n").encode("utf-8"))
        client.settimeout(response_timeout)
        payload = b""
        try:
            while not payload.endswith(b"\n"):
                chunk = client.recv(4096)
                if not chunk:
                    break
                payload += chunk
        except socket.timeout as error:
            raise DaemonNotRespondingError(
                f"Murmly daemon did not respond within {response_timeout:g} seconds."
            ) from error
    if not payload:
        raise DaemonNotRespondingError("Murmly daemon closed the connection before responding.")
    return json.loads(payload.decode("utf-8"))
