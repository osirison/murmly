from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import ctypes
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
from murmly.config import WINDOWS_PIPE_NAME, MurmlyConfig
from murmly.focus import (
    FocusObserver,
    WindowIdentity,
    create_focus_observer,
    record_target,
    should_deliver,
)
from murmly.hotkey_record import HotkeyRecordStore, default_hotkey_record_path, rebind_from_record
from murmly.idle import IdleRelease
from murmly.integrations import Paster, create_clipboard_paster
from murmly.overlay import (
    detect_overlay_backend,
    NullOverlayController,
    OverlayController,
    OverlayLifecycle,
    OverlayState,
)
from murmly.platform import (
    OperatingSystem,
    PlatformProfile,
    hotkey_mechanism_is_in_process,
    resolve_platform,
)
from murmly.silence import SilenceDetector
from murmly.speech import (
    EVENT_INTERRUPTED,
    EVENT_SHUTTING_DOWN,
    EVENT_TRANSCRIPT,
    Interruption,
    SpeechEngine,
    SpeechSuspendError,
)
from murmly.stt import FasterWhisperTranscriber
from murmly.win_pipe import is_pipe_name


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
#: Task 5.5: what `murmly install` reaches a running daemon for, since an
#: in-process hotkey registration cannot be changed by writing a file the
#: desktop reads. See `hotkey_record.py` -- a no-op reply on every platform
#: this change targets, since neither Plasma nor GNOME registers in-process.
COMMAND_REBIND_HOTKEYS = "rebind_hotkeys"

#: Which command an in-process hotkey purpose (`installer.HotkeyPurpose.key`,
#: also `HotkeyRecordStore`'s own keys) fires when the platform's own message
#: loop -- not a connection -- is what learned it was pressed (task 8.3).
#: Kept as its own small table here, hardcoding the two purpose keys rather
#: than importing them from `installer.py`, the same way `hotkey_record.py`'s
#: docstring already names `"window"`/`"session"` without importing that
#: module.
IN_PROCESS_HOTKEY_COMMANDS: dict[str, str] = {
    "window": COMMAND_TOGGLE,
    "session": COMMAND_TOGGLE_SESSION,
}
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

    The whole analysis below is skipped, not merely `os.getuid()`, on a host
    that has no `os.getuid` at all. In production this function is reached
    only through the Linux/macOS branch of `_require_private_channel`, where
    the resolved profile and the real host agree and `os.getuid` always
    exists. A test may deliberately resolve one of those profiles on a real
    Windows interpreter instead, to keep this branch exercised on every host
    (see `MurmlyDaemon.__init__`'s own docstring for the matching seam on the
    peer-identity side). On that host every mode bit this function reads is
    synthetic: CPython's Windows `os.stat` derives `st_mode` from the
    FILE_ATTRIBUTE_READONLY flag alone, so an ordinary writable directory
    reports `0o777` -- group- and other-writable, with no sticky bit -- which
    would read as "writable by other accounts" for the first directory in
    every walk, and there is no real uid or mode-bit information underneath
    it to judge either way. Returning `None` immediately is the same "nothing
    to report" a `os.getuid()`-only guard would still get wrong one line
    later.
    """
    if not hasattr(os, "getuid"):
        return None
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


def peer_identity_supported(profile: PlatformProfile | None = None) -> bool:
    """Whether this platform can report the account behind an accepted connection.

    Windows always answers True: task 7.4 reads it from the pipe's client
    process token, a mechanism every Windows version this project targets
    carries, unlike `SO_PEERCRED`, which is a Linux-only socket option. macOS
    also always answers True (task 13.2): `getpeereid(3)` is a libSystem
    function every Darwin kernel this project targets carries, not a
    `socket`-module attribute the way `SO_PEERCRED` is, so it is named by
    operating system here rather than by `hasattr`.
    """
    resolved = profile if profile is not None else resolve_platform()
    if resolved.operating_system in (OperatingSystem.WINDOWS, OperatingSystem.MACOS):
        return True
    return hasattr(socket, "SO_PEERCRED")


def read_peer_identity(connection: socket.socket) -> int | None:
    """The user id on the other end of the connection, or None where unreadable.

    The UNIX transport's own mechanism -- `SO_PEERCRED` -- kept under its
    original name and signature because it is still exactly what a UNIX socket
    connection is read with. `peer_identity_mechanism_for` is what a caller
    that does not already know which transport it holds should use instead;
    this function remains the concrete Linux answer it dispatches to.
    """
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


#: `sys/ucred.h`'s `uid_t`/`gid_t` -- both `__uint32_t` on Darwin.
_DARWIN_UID_T = ctypes.c_uint32
_DARWIN_GID_T = ctypes.c_uint32


def read_peer_identity_macos(connection: socket.socket) -> int | None:
    """The user id on the other end of a UNIX socket connection, read through
    `getpeereid(3)` (task 13.2).

    macOS's `socket` module carries no `SO_PEERCRED` -- it is a Linux-only
    `getsockopt` option (`read_peer_identity`'s own docstring) -- but BSD
    sockets (and Darwin's, which descends from them) answer the same question
    through a dedicated libSystem call instead: `int getpeereid(int fd, uid_t
    *euid, gid_t *egid)`, declared here with real `ctypes` types rather than
    left to default -- `uid_t`/`gid_t` are 4 bytes and the function's own
    return is `c_int`, so nothing here is at risk of the pointer-truncation
    defect class this change's other new ctypes code carries a comment about,
    but declaring the signature explicitly is the rule regardless of whether
    a given call happens to be safe by accident.

    `getpeereid` reports no pid at all (design.md's "The command channel"),
    unlike `SO_PEERCRED`'s three-way `(pid, uid, gid)` tuple -- which is
    exactly enough for the one comparison `_peer_permitted` ever makes: it
    already compares only the uid `read_peer_identity`'s Linux branch reports,
    ignoring the pid entirely, so this platform's mechanism satisfies
    `PeerIdentityMechanism`'s contract with nothing missing.

    Loaded through `ctypes.CDLL(None)` -- the running process's own image,
    which already links every libSystem symbol -- rather than a framework
    path, because `getpeereid` is a plain libc function, not
    framework-bundled Objective-C or Carbon API the way this change's other
    macOS ctypes calls are.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError as error:
        logger.warning("Unable to load libSystem to read a peer identity: %s", error)
        return None
    libc.getpeereid.restype = ctypes.c_int
    libc.getpeereid.argtypes = [ctypes.c_int, ctypes.POINTER(_DARWIN_UID_T), ctypes.POINTER(_DARWIN_GID_T)]

    uid = _DARWIN_UID_T()
    gid = _DARWIN_GID_T()
    status = libc.getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid))
    if status != 0:
        logger.warning(
            "Unable to read the peer identity of a connection: getpeereid failed "
            "(errno=%s)",
            ctypes.get_errno(),
        )
        return None
    return int(uid.value)


@dataclass(frozen=True, slots=True)
class PeerIdentityMechanism:
    """One platform's own way of reading a peer's identity, and comparing it.

    Returned as one paired value rather than two separately defaulted
    constructor parameters, so a caller cannot accidentally pair one
    platform's reader with another platform's comparison -- the
    `command-interface` spec's "Peer identity read by the platform's own
    mechanism" scenario requires the read and the comparison it feeds to both
    be the same platform's own rule (task 18.7).
    """

    supported: bool
    read: Callable[[object], object | None]
    # `| None`: `peer_identity_mechanism_for`'s non-Windows branch stores
    # `None` here on a host with no `os.getuid` at all, never called in that
    # case -- see that function's own docstring for why.
    local: Callable[[], object] | None


def peer_identity_mechanism_for(profile: PlatformProfile) -> PeerIdentityMechanism:
    """Dispatch to `profile`'s own way of reading and comparing peer identity.

    Windows compares SID strings, both read by `win_pipe`'s own mechanism --
    `os.getuid()`, the UNIX comparison, is not a concept a pipe's client-token
    SID could ever equal. Every other resolved platform keeps the existing
    `SO_PEERCRED`/`os.getuid()` pair unchanged, which is what keeps this
    dispatch a zero-behaviour-change addition on Linux.

    `getattr(os, "getuid", None)`, not the bare name: on a real Linux or
    macOS host this returns the exact same function object (`PeerIdentity
    MechanismDispatchTests.test_linux_keeps_the_existing_socket_functions`
    asserts `is`, not merely equality), so nothing changes there. The bare
    name is an `AttributeError` waiting to happen the moment this function
    is *called* -- not merely referenced -- for a non-Windows profile on a
    real Windows interpreter, which only a test deliberately does (see
    `MurmlyDaemon.__init__`'s own comment on this same hazard). `None`
    reaches `PeerIdentityMechanism.local` there instead, and `.supported`
    reports `False` alongside it -- `hasattr`, the same idiom
    `peer_identity_supported` and `_directory_exposure` both already use to
    ask what a platform can do rather than assume it, instead of the bare
    `AttributeError` that reaching this branch used to raise for exactly
    that host/profile pair. `local` is never called there regardless:
    `_peer_permitted` only calls it once `self._peer_identity` -- the
    platform's `read` -- has already answered with a real identity, and on a
    host missing `os.getuid` altogether that never happens: `read_peer_
    identity` answers `None` with no `SO_PEERCRED` (every real Windows
    host), and `read_peer_identity_macos` is never reachable on a
    non-Darwin host in the first place.
    """
    if profile.operating_system is OperatingSystem.WINDOWS:
        from murmly.win_pipe import current_user_sid_string, read_peer_identity_from_pipe

        return PeerIdentityMechanism(True, read_peer_identity_from_pipe, current_user_sid_string)
    if profile.operating_system is OperatingSystem.MACOS:
        # `read_peer_identity_macos` (task 13.2), compared with the same
        # `os.getuid()` the Linux branch below uses: `getpeereid` answers a
        # uid, exactly what the UID-only comparison `_peer_permitted` already
        # makes needs, so macOS shares the Linux branch's `local` callable
        # rather than needing one of its own. `getattr(os, "getuid", None)`,
        # not the bare name -- see this function's own docstring: a real
        # Windows interpreter has no `os.getuid` at all, and a macOS profile
        # resolved there (only a test does this) must report the mechanism
        # unsupported rather than raise while building it.
        local = getattr(os, "getuid", None)
        return PeerIdentityMechanism(local is not None, read_peer_identity_macos, local)
    return PeerIdentityMechanism(
        peer_identity_supported(profile), read_peer_identity, getattr(os, "getuid", None)
    )


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
        profile: PlatformProfile | None = None,
    ) -> None:
        self._config = config
        # Resolved once, like `MurmlyDaemon.__init__` resolves it (task 1.3),
        # and passed here rather than re-resolved -- `_ensure_paster` and the
        # default focus observer both read it, and must agree with whatever
        # decided the rest of this process is on Windows.
        self._profile = profile if profile is not None else resolve_platform()
        self._recorder = SoundDeviceRecorder(config, level_sink=level_sink)
        self._transcriber = FasterWhisperTranscriber(config)
        self._paster: Paster | None = None
        self._focus = (
            focus_observer
            if focus_observer is not None
            else create_focus_observer(profile=self._profile)
        )
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
        try:
            self._transcriber.stop_partials()
            self._stop_live_worker()
        except Exception:
            # The stream closes even when the live worker's teardown fails.
            # Nothing else closes it now that PortAudio's exit-time teardown is
            # disabled, so what this would otherwise leave open is the
            # microphone, held for the life of the process.
            try:
                self._recorder.stop()
            except Exception as stop_error:
                logger.warning("Unable to close capture after a failed stop: %s", stop_error)
            raise
        return self._recorder.stop()

    @property
    def model_resident(self) -> bool:
        """Whether the transcription model's weights are held right now.

        The same reach from a capture session to its transcriber that
        `release_model` makes, asked as a question rather than made as a
        release. The daemon answers `status` with this, so it must neither load
        nor wait: `resident` is specified to do neither, and deliberately skips
        the transcriber's model lock so the answer cannot park behind a decode
        that is already running.
        """
        return self._transcriber.resident

    def release_model(self) -> None:
        """Give the transcription model's memory back, if it still holds any.

        The countdown that calls this lives on the daemon rather than here,
        because the daemon is what knows the recording has ended and what
        abandons the countdown on its way out. What is here is only the reach
        from a capture session to the model it transcribes with.

        Nothing is checked first. The transcriber answers a model that was never
        built, one already released, and a runtime that cannot be asked all the
        same way, and it waits for a pass in flight rather than interrupting
        one -- so a countdown that expires mid-transcription is late, not wrong.
        """
        self._transcriber.release()

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

    def _ensure_paster(self) -> Paster:
        if self._paster is None:
            self._paster = create_clipboard_paster(
                restore_clipboard=self._config.restore_clipboard,
                restore_delay_ms=self._config.restore_clipboard_delay_ms,
                profile=self._profile,
            )
        return self._paster


class _Adopted:
    """Sentinel: this connection became a speech session and owes no response."""


ADOPT_SESSION = _Adopted()


@dataclass(slots=True)
class _Confirmed:
    """A queued frame whose sender is waiting to learn whether it went out.

    The event alone cannot carry the answer: a discard has to wake the waiter
    too, and a waiter woken by a discard must not read that as success.
    """

    frame: dict[str, object]
    written: threading.Event
    delivered: bool = False


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
        self._outbox: deque[dict[str, object] | _Confirmed | None] = deque()
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
        to be written on it, and shutting the connection down would close the
        socket the refusal has to go out on.

        A writer thread that did start is stopped first. `start` can fail
        between the two threads, and a writer left running would spin against a
        handle that has gone.
        """
        self._closing.set()
        with self._outbox_lock:
            self._wake_waiters_locked()
            self._outbox.clear()
            self._outbox.append(None)
            self._outbox_ready.set()
        writer = self._writer
        if writer is not None and writer is not threading.current_thread():
            writer.join(timeout=SESSION_WRITER_JOIN_SECONDS)
        try:
            self._write_handle.close()
        except OSError:
            pass

    def start(self) -> None:
        self._connection.settimeout(SESSION_POLL_SECONDS)
        writer = threading.Thread(
            target=self._write_loop, name="murmly-speech-writer", daemon=True
        )
        writer.start()
        # Assigned only once it is running. A thread that failed to start is one
        # close() would join and one shutdown() would raise on.
        self._writer = writer
        reader = threading.Thread(
            target=self._read_loop, name="murmly-speech-session", daemon=True
        )
        reader.start()
        self._reader = reader

    def send(self, frame: dict[str, object], *, confirm: bool = False) -> bool:
        """Queue one frame for this session, never waiting on it.

        A session that will not read what it is sent is disconnected rather than
        allowed to hold up the playback thread, which is the posture the level
        meter takes with a sink that raises.

        Reports whether the frame was queued. Both refusals drop it silently
        otherwise, and a transcript dropped without the caller knowing is the
        person's words lost with nothing on the clipboard either.
        """
        with self._outbox_lock:
            # Checked while holding the lock, not before taking it. `close` and
            # `dispose` both set the flag and then queue the sentinel under this
            # lock, so a send that wins the lock afterwards must see the flag --
            # and a send that checked outside it would append behind a sentinel
            # the writer has already stopped at, report the frame as queued, and
            # leave the transcript on neither the socket nor the clipboard.
            if self._closing.is_set():
                return False
            if len(self._outbox) >= SESSION_EVENT_BACKLOG:
                logger.warning("Disconnecting a speech session that is not reading its events.")
                self._closing.set()
                self._wake_waiters_locked()
                self._outbox.clear()
                self._outbox.append(None)
                self._outbox_ready.set()
                self._shutdown_socket()
                return False
            pending = _Confirmed(frame, threading.Event()) if confirm else None
            self._outbox.append(pending if pending is not None else frame)
            self._outbox_ready.set()
        if pending is None:
            return True
        # Only the transcript waits. Every other event is a report about audio
        # and is worth nothing late, but a transcript is the person's words: a
        # sender that dies between the queue and the write leaves them on
        # neither the socket nor the clipboard, because the clipboard fallback
        # was skipped on the strength of the queueing alone.
        pending.written.wait(SESSION_SEND_TIMEOUT_SECONDS + SESSION_WRITER_JOIN_SECONDS)
        return pending.delivered

    def _wake_waiters_locked(self) -> None:
        """Release anything waiting on a frame that is about to be dropped.

        Woken rather than left to time out, and left undelivered rather than
        marked written: a discard is the answer, not the absence of one.
        """
        for queued in self._outbox:
            if isinstance(queued, _Confirmed):
                queued.written.set()

    def close(self, *, drain: bool = False) -> None:
        """Stop this session, optionally letting queued frames reach the peer."""
        if not self._closing.is_set():
            self._closing.set()
            with self._outbox_lock:
                if not drain:
                    # Dropped, so the waiters are woken undelivered. Draining
                    # instead means the writer is about to send them and will
                    # resolve each one itself -- waking them here would report a
                    # transcript undelivered and then put it on the wire, which
                    # is the clipboard copy and the socket both.
                    self._wake_waiters_locked()
                    self._outbox.clear()
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
                queued = self._outbox.popleft()
                if not self._outbox:
                    self._outbox_ready.clear()
            if queued is None:
                return
            pending = queued if isinstance(queued, _Confirmed) else None
            frame = pending.frame if pending is not None else queued
            try:
                self._write_handle.sendall((json.dumps(frame) + "\n").encode("utf-8"))
            except OSError as error:
                if not self._shutdown.is_set():
                    logger.debug("A speech session event could not be written: %s", error)
                self._closing.set()
                if pending is not None:
                    pending.written.set()
                return
            if pending is not None:
                # Marked only once sendall has returned, so a waiter learns the
                # difference between queued and gone out.
                pending.delivered = True
                pending.written.set()

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
        peer_identity: Callable[[object], object | None] | None = None,
        speech: SpeechEngine | None = None,
        profile: PlatformProfile | None = None,
        local_identity: Callable[[], object] | None = None,
        hotkey_registrar: object | None = None,
    ) -> None:
        self._config = config
        # Resolved once and kept, like every other platform-dependent decision
        # in this change (task 1.3) -- `serve_forever` and `shutdown` both read
        # it later to decide whether the channel is a UNIX socket or a named
        # pipe, and must agree with what was decided here.
        self._profile = profile if profile is not None else resolve_platform()
        # Before anything else, peer-identity mechanism dispatch included: a
        # daemon that refuses to run must not start anything first, and on a
        # deliberately mismatched profile/host pair -- a non-Windows profile
        # constructed on a real Windows interpreter, which only a test does,
        # to keep asserting the shape refusal on every host -- a refusal
        # this mismatch is here to produce must still run before anything
        # else does. `peer_identity_mechanism_for` itself no longer raises
        # for that pair (it answers an unsupported mechanism instead, see
        # its own docstring), but the ordering stays: nothing else should
        # start before a refusal that applies has had its chance to fire.
        self._require_private_channel()
        mechanism = peer_identity_mechanism_for(self._profile)
        self._peer_identity = peer_identity if peer_identity is not None else mechanism.read
        self._local_identity = local_identity if local_identity is not None else mechanism.local
        self._overlay = overlay or self._create_overlay(config)
        self._session = session or SpeechSession(
            config,
            level_sink=self._publish_level,
            partial_sink=self._publish_partial,
            on_silence=self._on_silence,
            profile=self._profile,
        )
        self._speech = speech if speech is not None else SpeechEngine(config)
        # Two countdowns rather than one. The two models reclaim different
        # resources, cost different amounts to restore, and are keyed to
        # lifecycles the other does not have -- capture for one, a speech
        # session for the other -- so a shared period could not serve both.
        # Each is inert when its configured period is zero, which is what
        # leaves synthesis untouched under the shipped defaults.
        self._transcription_idle = IdleRelease(
            config.unload_after_idle_s,
            self._release_transcription,
            name="murmly-model-release",
        )
        self._synthesis_idle = IdleRelease(
            config.tts_unload_after_idle_s,
            self._release_synthesis,
            name="murmly-speech-release",
        )
        # The connection a sender holds open, at most one at a time. With two
        # open, "deliver the transcript to that session" and "tell the session it
        # was interrupted" would both have to guess which one was meant.
        self._speech_session: SpeechSessionConnection | None = None
        self._speech_session_lock = threading.Lock()
        self._capture_destination = DESTINATION_WINDOW
        # The session this capture is for, fixed when it starts. The one that
        # happens to be open when the transcript is ready is a different
        # question, and answering with it delivers a person's words to a
        # sender that was not there when they spoke them.
        self._capture_session: SpeechSessionConnection | None = None
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
        # Set once the channel is actually accepting, not merely assigned to
        # `_server` -- `wait_until_served` (tests/test_daemon.py) waits on this
        # rather than polling `getsockopt(SO_ACCEPTCONN)`, which macOS refuses
        # for an AF_UNIX socket with `OSError: [Errno 42] Protocol not
        # available`; a probe connection is not a substitute either, since the
        # daemon counts every accepted connection through its peer-identity
        # check, and a test that answers that check differently per
        # connection would be answering one of these probes.
        self._listening = threading.Event()
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
        # Task 5.4/5.5's design boundary: whatever object holds an in-process
        # hotkey registration, if this platform's mechanism is one -- `None`
        # for Plasma and GNOME, since neither registers here: a launcher file
        # and a dconf value are both desktop-held state. Windows'
        # `WindowsHotkeyRegistrar` (section 8) and macOS's
        # `MacosHotkeyRegistrar` (section 13) are the two populators, chosen by
        # the resolved operating system -- `hotkey_mechanism_is_in_process`
        # only says *that* this platform's mechanism registers here, not
        # *which* platform, so the registrar class itself still has to be
        # picked by `self._profile.operating_system` rather than assumed to be
        # Windows' now that a second one exists.
        # `hotkey_registrar` lets a test substitute a fake without touching a
        # real message-loop or event-loop thread; every real caller leaves it
        # `None` and gets whatever `hotkey_mechanism_is_in_process(self._profile)`
        # and `self._profile.operating_system` together say.
        if hotkey_registrar is not None:
            self._hotkey_registrar: object | None = hotkey_registrar
        elif hotkey_mechanism_is_in_process(self._profile):
            if self._profile.operating_system is OperatingSystem.MACOS:
                from murmly.mac_hotkey import MacosHotkeyRegistrar

                self._hotkey_registrar = MacosHotkeyRegistrar(on_hotkey=self._handle_in_process_hotkey)
            else:
                from murmly.win_hotkey import WindowsHotkeyRegistrar

                self._hotkey_registrar = WindowsHotkeyRegistrar(on_hotkey=self._handle_in_process_hotkey)
        else:
            self._hotkey_registrar = None

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

    def _uses_named_pipe(self) -> bool:
        """Whether this daemon's channel is Windows' named pipe rather than a
        UNIX socket (task 7.1) -- decided once, from the resolved platform, the
        same way every other platform-dependent choice in this change is."""
        return self._profile.operating_system is OperatingSystem.WINDOWS

    def serve_forever(self) -> None:
        try:
            # Re-checked here, before anything below acts on the configured
            # path or name: that action must never run against one Murmly
            # would refuse.
            self._require_private_channel()
            if not peer_identity_supported(self._profile):
                logger.warning(
                    "This platform cannot report the account behind a connection. The "
                    "command channel is protected by its own access control alone."
                )
            if self._uses_named_pipe():
                self._serve_named_pipe()
            else:
                self._serve_unix_socket()
        finally:
            # Outside the channel's own unwinding, because a refusal that
            # happens before the channel exists still has an overlay to close,
            # and, on Windows, a hotkey thread `_rebind_hotkeys` may already
            # have started. `serve_forever` ending through an exception --
            # not only through an explicit `shutdown()` call -- must not leave
            # that thread holding `RegisterHotKey` bindings with no daemon
            # left to answer them (task 8.5); `shutdown()` releases the same
            # registrar too, and a second `stop()` here is a no-op on it.
            self._release_hotkey_registrar()
            self._close_overlay()

    def _serve_unix_socket(self) -> None:
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
                self._listening.set()
                logger.debug("Hotkey rebind at startup: %s", self._rebind_hotkeys())
                server.settimeout(0.2)
                self._accept_loop(server)
        finally:
            self._server = None
            self._listening.clear()
            try:
                self._config.socket_path.unlink(missing_ok=True)
            except OSError as error:
                # Reported rather than raised: a failure to clean up must not
                # replace the reason the daemon is unwinding.
                logger.warning("The command socket could not be removed: %s", error)

    def _serve_named_pipe(self) -> None:
        """Windows' command channel: a named pipe, never a file (tasks 7.1, 7.6).

        No directory to create, and nothing to unlink or chmod afterwards --
        `_require_private_channel` already refused a configured value that is
        not shaped like a pipe name, and the DACL `win_pipe.
        create_named_pipe_server` builds into the pipe itself is what grants
        only this account's SID (task 7.2), rather than a filesystem mode bit.
        `NamedPipeServer` is imported here rather than at module load, matching
        the deferred-import shape every other platform-specific subsystem in
        this codebase uses (`installer._current_session`,
        `stt._load_model_locked`, and every loader in `platform/__init__.py`) --
        `win_pipe.py` itself imports no `pywin32` name until one of its own
        methods runs, so this is a convention kept for consistency, not a
        requirement for `daemon.py`'s own importability.
        """
        from murmly.win_pipe import NamedPipeServer

        try:
            server = NamedPipeServer(str(self._config.socket_path))
        except OSError as error:
            raise DaemonStartupError(
                f"Refusing to serve at {self._config.socket_path}. Its named pipe "
                f"could not be created: {error}."
            ) from error
        self._server = server
        # Already connectable: `NamedPipeServer.__init__` builds its one
        # waiting pipe instance before returning (see its own docstring), so
        # there is no separate listen() call whose completion `_listening`
        # would otherwise need to wait for.
        self._listening.set()
        try:
            with server:
                logger.debug("Hotkey rebind at startup: %s", self._rebind_hotkeys())
                server.settimeout(0.2)
                self._accept_loop(server)
        finally:
            self._server = None
            self._listening.clear()

    def _accept_loop(self, server) -> None:
        """Accept connections until shutdown, against either transport.

        Written once and shared, rather than once per transport: `server` is
        either a UNIX `socket.socket` or a `win_pipe.NamedPipeServer`, and this
        loop calls only the subset of methods the two share
        (`accept`/`settimeout`, with a connection object answering to
        `close`), which is exactly what `win_pipe.NamedPipeServer` and
        `win_pipe.NamedPipeConnection` were built to satisfy.
        """
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

    def _require_private_channel(self) -> None:
        """Refuse a channel this account cannot serve privately, before anything opens it.

        Dispatches on the resolved platform's channel kind (task 7.5), not on
        the configured value's shape alone: the shape decides which of the two
        wrong combinations this is, and either one is refused regardless of
        which platform is actually running, so a value copied from one
        platform's configuration onto the other is refused with a reason
        naming the mismatch rather than silently misread. A filesystem
        socket's directory-privacy analysis (`socket_path_detail`) does not
        apply to a name that is not in the filesystem, and is not run for one.
        """
        configured = str(self._config.socket_path)
        if self._uses_named_pipe():
            if is_pipe_name(configured):
                return
            raise DaemonStartupError(
                f"Refusing to serve at {configured}. Windows serves its command "
                "channel as a named pipe, not a filesystem path, so this daemon "
                "cannot create it privately. Configure daemon.socket_path as a "
                f"pipe name such as {WINDOWS_PIPE_NAME}."
            )
        if is_pipe_name(configured):
            raise DaemonStartupError(
                f"Refusing to serve at {configured}. This is a Windows named-pipe "
                f"name, but {self._profile.operating_system.value} serves its "
                "command channel as a filesystem socket, so this daemon cannot "
                "create it privately. Configure daemon.socket_path as a "
                "filesystem path instead."
            )
        detail = socket_path_detail(self._config.socket_path)
        if detail is None:
            return
        # The remedy belongs to the detail rather than to this message: the
        # directory at fault is not always the socket's own parent, and an
        # ownership fault is not corrected the way a mode fault is.
        raise DaemonStartupError(f"Refusing to serve at {self._config.socket_path}. {detail}")

    def _peer_permitted(self, connection: object) -> bool:
        """Whether the account behind this connection is the one Murmly serves.

        The comparison is `==` against `self._local_identity()`, not a
        UID-specific comparison: on Linux and macOS both sides are `os.getuid()`
        integers, unchanged from before this dispatch existed, and on Windows
        both sides are the SID strings `win_pipe` reads (task 18.7's "the same
        rule is applied to the result").

        An identity this platform cannot report is served rather than refused,
        which is reported at startup and in diagnostics: refusing would make the
        daemon unusable on a platform whose only fault is not offering the check.
        """
        identity = self._peer_identity(connection)
        if identity is None:
            return True
        if identity == self._local_identity():
            return True
        logger.warning("Refused a command from %r.", identity)
        return False

    def _release_hotkey_registrar(self) -> None:
        """Stop the in-process hotkey registrar, if this platform holds one (task 8.5).

        Released before anything else unwinds wherever it is called from: a
        hotkey firing mid-shutdown would call `handle_command` through
        `_handle_in_process_hotkey` and could start a new capture on a daemon
        that is already on its way out. On a platform holding no in-process
        hotkey, `self._hotkey_registrar` is `None` and this is a no-op.

        Called from both `shutdown()` and `serve_forever()`'s own unwinding:
        a `serve_forever` that ends through an unhandled exception, not only
        through an explicit `shutdown()` call, must not leave the message-loop
        thread holding `RegisterHotKey` bindings with no daemon left to answer
        them. Safe to call twice -- `WindowsHotkeyRegistrar.stop()` is
        idempotent on a thread that is already stopped -- so both call sites
        keep this line rather than one deferring to the other.
        """
        if self._hotkey_registrar is None:
            return
        try:
            self._hotkey_registrar.stop()
        except Exception:  # noqa: BLE001 - releasing a hotkey must not raise
            logger.warning("Could not release the in-process hotkey registration.", exc_info=True)

    def shutdown(self) -> None:
        self._shutdown.set()
        self._release_hotkey_registrar()
        server = self._server
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        # Before the drain, and before the blanket close below: a session has to
        # be told what happened while its connection is still open.
        self._close_speech_session()
        self._stop_capture()
        # After both of those, never before. Shutdown ends a recording on its
        # way out, and `_stop_capture` goes through `_stop_recording` like every
        # other path that does, so a cancel placed above would be undone by the
        # line below it -- leaving a five-minute countdown armed with nothing
        # left to cancel it. The same reading applies to a sender that
        # disconnects while shutdown is running and reaches `_session_closed`.
        # Ordering the cancels last was chosen over making every arm site test
        # the shutdown flag: it is one place rather than five, and it keeps a
        # shutdown check off the path every recording takes.
        self._transcription_idle.cancel()
        self._synthesis_idle.cancel()
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
        # A named pipe has no filesystem node to remove -- closing every
        # handle above is what releases it (task 7.6: `socket_path` still
        # names the channel, but naming it is not the same as it existing on
        # disk).
        if not self._uses_named_pipe():
            self._config.socket_path.unlink(missing_ok=True)
        self._close_overlay()

    def _stop_capture(self) -> None:
        """Release the microphone, whether or not a recording is running.

        The audio is discarded. A stop signal is not a request to transcribe
        what happened to be in the buffer, and the transcript would have nowhere
        to go: the connections it would be delivered to are closed below.

        Reported rather than raised, like every other step of the shutdown: the
        socket, the overlay, and the speech output still have to be released
        whatever one device does.
        """
        try:
            self._stop_recording()
        except Exception as error:  # noqa: BLE001 - shutdown continues regardless
            logger.warning("Capture did not stop cleanly: %s", error)

    def _stop_recording(self) -> bytes:
        """Close the microphone and start the transcription model's countdown.

        Every path that ends a recording comes through here rather than calling
        the capture session directly, so the countdown cannot be lost to
        whichever exit a recording happens to take: the toggle, the auto-stop,
        a refused segment ending a continuous session, a transition thread that
        would not start, and shutdown.

        `take_segment` deliberately does not come through here. A continuous
        session closing a segment is still capturing, so it is not idle however
        long its pauses are, and arming there would release the model in the
        silence between two utterances of one session -- the case that keying on
        the session lifecycle rather than a last-use timestamp exists to rule
        out.

        Armed in a `finally` because a stop that raises has still ended the
        capture. Every caller treats the raise that way, and the model is idle
        from that moment whether or not the stream closed cleanly.
        """
        try:
            return self._session.stop_recording()
        finally:
            self._transcription_idle.arm()

    def _release_transcription(self) -> None:
        self._session.release_model()

    def _release_synthesis(self) -> None:
        # A daemon with speech output off never builds a synthesizer, and its
        # countdown is never armed either, since arming needs a speech session
        # and one cannot be declared without it. Checked rather than assumed:
        # the two facts are established in different places.
        synthesizer = self._speech.synthesizer
        if synthesizer is None:
            return
        synthesizer.release()

    def _close_speech_session(self) -> None:
        """Stop speech, tell the session, and close it.

        The one place a speech session ends without the synthesis countdown
        being armed. `shutdown` is the only caller, and it cancels both
        countdowns a few lines after calling this, so arming one here would
        start a thread for the sole purpose of killing it. A second caller
        would have to arm.
        """
        with self._speech_session_lock:
            session = self._speech_session
            self._speech_session = None
            # Under the lock, as in `_session_closed`. Released first, a session
            # declaring itself in the window is accepted and given the engine,
            # and then has it closed underneath it by this call. Shutdown is
            # racing incoming connections by definition, so this is the path
            # where that window is most likely to be hit.
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

        # That answered for the moment this daemon started. Asked again here for
        # now, because the environment can have lost the synthesizer since --
        # this is the same rule `begin` applies to the output device a few lines
        # below, and for the same reason: a session that cannot be served is
        # refused before anyone commits to it, not failed once somebody is
        # listening. Skipped when a synthesizer is resident, which is proof.
        reason = self._speech.unavailable_reason_now()
        if reason is not None:
            logger.warning("Speech output is no longer available: %s", reason)
            return failure_response(
                CommandCode.SPEECH_UNAVAILABLE,
                f"Speech output is unavailable: {reason}",
            )

        # The state lock first, and held across `begin`. Declaring a session
        # opens the output device and clears the barge-in hold, so one arriving
        # while the microphone is open put speech and capture back into the room
        # together -- which is the arrangement the whole design rules out, and
        # the reason there is no echo cancellation to fall back on. The raw
        # state, not the derived property: SPEAKING is IDLE with speech running
        # and must still accept a session.
        #
        # The order matters. A toggle holds this lock and then reaches
        # `_speech_session_lock` through `_barge_in`, so taking them the other
        # way round here would deadlock.
        with self._lock:
            if self._state != STATE_IDLE:
                return failure_response(
                    CommandCode.BUSY, "Daemon is busy.", state=self._state
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
                    # Opens the output device, so a session that cannot be given
                    # one is refused now rather than accepted and failed once
                    # somebody is listening.
                    self._speech.begin(session.send)
                except Exception as error:  # noqa: BLE001 - a refusal, not a crash
                    session.dispose()
                    return failure_response(
                        CommandCode.SPEECH_UNAVAILABLE,
                        f"Speech output could not be started: {error}",
                    )
                self._speech_session = session
                # A speech session beginning is what makes the synthesizer
                # non-idle, exactly as capture beginning does for the
                # transcription model. Cancelled here rather than at the first
                # `speak` frame: the sender may take seconds to produce its
                # first words, and a rebuild in that gap is silence a listener
                # hears at the front of the reply.
                self._synthesis_idle.cancel()

        try:
            session.start()
        except Exception as error:  # noqa: BLE001 - a refusal, not a crash
            # The device is open and the session is registered by this point, so
            # a half-started session left in place would refuse every later
            # declaration for the life of the daemon.
            with self._speech_session_lock:
                if self._speech_session is session:
                    self._speech_session = None
                    try:
                        self._speech.end()
                    except Exception as stop_error:  # noqa: BLE001 - the handle still goes
                        # A device that will not close must not also cost the
                        # duplicated handle and the writer thread below.
                        logger.warning(
                            "Speech output did not stop cleanly: %s", stop_error
                        )
                    # A session that never started is a session that has ended,
                    # so the countdown the declaration cancelled goes back on.
                    # Under the lock and inside this branch, because the session
                    # registered here is the one that ended: armed outside it,
                    # a declaration that had already replaced this one would
                    # have a countdown running against its live session.
                    self._synthesis_idle.arm()
            session.dispose()
            return failure_response(
                CommandCode.COMMAND_FAILED, f"The speech session could not be started: {error}"
            )

        # Nothing further is owed on this connection as a one-shot command, so
        # shutdown must stop waiting to answer it.
        with self._connections_lock:
            self._answering.discard(connection)
            self._claimed.discard(connection)
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
            # Torn down while still holding the lock. Released first, a session
            # declared in the window would be accepted, given the engine, and
            # then have it closed underneath it by this call.
            try:
                self._speech.end()
            except Exception as error:  # noqa: BLE001 - the session still has to be closed
                # An output device that will not close would otherwise take this
                # thread out through the reader's `finally`, leaving the writer
                # thread and both socket handles alive for the life of the
                # daemon -- and the session still registered as connected.
                logger.warning("Speech output did not stop cleanly: %s", error)
            # Under the lock and inside this branch, for the reason the failed
            # start site gives: the session registered here is the one that
            # ended. Armed after the lock is released, a declaration landing in
            # the drain below would register its own session, cancel the
            # countdown, and then be handed this one -- leaving a countdown
            # running against a session that has never been idle, which is the
            # state both arm sites exist to avoid. The countdown starting before
            # the drain rather than after it costs at most the drain's own
            # bounded wait against a period of at least thirty seconds.
            self._synthesis_idle.arm()
        # Drained, and outside the lock. A session is sometimes closed by Murmly
        # having just written it a refusal -- a frame that could not be read, or
        # one past the size bound -- and shutting the socket down without
        # waiting for the writer loses the very explanation for the
        # disconnection it explains. A peer that has already gone makes the
        # write fail at once, so this waits only when there is something to
        # wait for.
        session.close(drain=True)
        with self._connections_lock:
            self._connections.discard(session.connection)

    def _send_to_session(self, frame: dict[str, object]) -> bool:
        """Send one frame to the open session, reporting whether it was taken."""
        with self._speech_session_lock:
            session = self._speech_session
        if session is None:
            return False
        # Not merely that a session object exists: a session already closing
        # drops what it is handed, and a transcript reported as delivered on
        # that basis reaches neither the socket nor the clipboard.
        return session.send(frame)

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
        try:
            interruption = self._speech.suspend()
        except SpeechSuspendError as error:
            # Speech stopped; the device did not close. The session is still
            # owed the report, and the caller still has to refuse the capture.
            self._report_interruption(error.interruption)
            raise
        self._report_interruption(interruption)

    def _residency(self) -> dict[str, object]:
        """What each model holds right now, for the `status` response to carry.

        Read from the model holders, never inferred from whether an idle
        countdown is armed. A countdown that has fired says a release was
        attempted, not that it succeeded -- a runtime whose build cannot be
        asked reports resident and releases nothing -- so only the holders know.

        Nothing is locked and nothing is constructed. `status` is what a hotkey
        press and `murmly doctor` both send, and neither may be parked behind a
        transcription already in flight.

        A holder that cannot be asked is reported as null beside the reason
        rather than left out, because leaving a field out is how this response
        says "this daemon does not know the question". Synthesis is left out for
        exactly that reason when no synthesizer was ever built: a daemon with
        `[tts] enabled = false` holds nothing and has nothing to hold.
        """
        residency: dict[str, object] = {}
        try:
            residency["model_resident"] = bool(self._session.model_resident)
        except Exception as error:  # noqa: BLE001 - status still owes an answer
            residency["model_resident"] = None
            residency["model_resident_detail"] = (
                f"Unable to determine transcription residency: {error}"
            )
        try:
            synthesizer = self._speech.synthesizer
        except Exception as error:  # noqa: BLE001 - status still owes an answer
            residency["synthesis_resident"] = None
            residency["synthesis_resident_detail"] = (
                f"Unable to determine synthesis residency: {error}"
            )
            return residency
        if synthesizer is None:
            return residency
        try:
            residency["synthesis_resident"] = bool(synthesizer.resident)
        except Exception as error:  # noqa: BLE001 - status still owes an answer
            residency["synthesis_resident"] = None
            residency["synthesis_resident_detail"] = (
                f"Unable to determine synthesis residency: {error}"
            )
        return residency

    def _rebind_hotkeys(self) -> str:
        """Re-register every recorded hotkey, where this platform needs it.

        Never raises: called both from a command a sender is waiting on and
        from the daemon's own startup, and a hotkey rebind failing must not be
        why either one stops answering.

        Reads `self._profile` -- resolved once at construction (task 1.3) --
        rather than resolving again here: `self._hotkey_registrar` was built
        from that same resolution, and a second, independent
        `resolve_platform()` call could in principle disagree with it (an
        environment variable changing between the two calls), which would
        leave `rebind_from_record` asking whether the *wrong* profile's
        mechanism is in-process while actually driving the registrar this
        daemon already holds.
        """
        try:
            record = HotkeyRecordStore(default_hotkey_record_path())
            return rebind_from_record(self._profile, record, self._hotkey_registrar)
        except Exception as error:  # noqa: BLE001 - see docstring
            logger.warning("Hotkey rebind failed: %s", error)
            return f"Hotkey rebind failed: {error}"

    def _handle_in_process_hotkey(self, purpose: str) -> None:
        """The callback a platform's own message-loop thread fires when it
        learns a hotkey was pressed (task 8.3) -- Windows' `WindowsHotkeyRegistrar`
        today, a future macOS registrar tomorrow.

        Runs on that thread, not a connection's worker thread, and calls
        `handle_command` directly rather than reaching through the command
        channel the way a keypress on Linux launches `murmly toggle` as a new
        process: there is no channel to reach through from inside the very
        process that would have to answer it. Never raises: the registrar
        already treats a raising handler as one bad event rather than a reason
        to stop pumping messages, but nothing here should ever need that.
        """
        command = IN_PROCESS_HOTKEY_COMMANDS.get(purpose)
        if command is None:
            logger.warning("A hotkey fired for an unrecognized purpose %r.", purpose)
            return
        try:
            self.handle_command(command)
        except Exception:  # noqa: BLE001 - the hotkey thread must keep running
            logger.warning("Handling the %r hotkey failed.", purpose, exc_info=True)

    def handle_command(self, command: str) -> dict[str, object]:
        if command == COMMAND_REBIND_HOTKEYS:
            return {"ok": True, "detail": self._rebind_hotkeys()}
        if command == COMMAND_STATUS:
            # Residency travels with the state rather than under a command of
            # its own. `status` already means "what is the daemon doing right
            # now", a reader that only looks at `state` is unaffected, and a
            # caller talking to a daemon too old to include the fields sees them
            # absent -- which is the same case it must handle for a daemon that
            # does not answer at all.
            response: dict[str, object] = {"ok": True, "state": self.state, **self._residency()}
            # Present on every platform (platform-support's "What does not
            # depend on the platform is identical on every platform": the
            # command protocol's responses are the same shape everywhere,
            # and a concern the platform cannot serve is reported as such
            # rather than the field being silently absent). Task 8.6/8.7,
            # 18.10's registrar reports the purposes it actually holds; where
            # there is no in-process registrar at all -- Linux and macOS,
            # where Plasma and GNOME hold their own bindings and this daemon
            # holds none in-process for `status` to report -- the true
            # answer is the empty list, not an omitted key: this daemon
            # genuinely holds no hotkeys in-process there, which is a fact
            # about the platform, not a value this daemon failed to produce.
            held = getattr(self._hotkey_registrar, "held_purposes", None)
            response["hotkeys_held"] = sorted(held()) if held is not None else []
            return response
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
                with self._speech_session_lock:
                    self._capture_session = (
                        self._speech_session
                        if destination == DESTINATION_SESSION
                        else None
                    )
                # Before the microphone opens, never after. The silence
                # detector, live partials and the final transcript all read the
                # live stream, and each would hear Murmly.
                try:
                    self._barge_in()
                except Exception as error:  # noqa: BLE001 - the microphone stays shut
                    # Capture must not begin: speech is not known to have
                    # stopped, and the two running at once is the one thing the
                    # barge-in exists to prevent. The hold is released so the
                    # session is not left silent until some later capture.
                    self._publish_error()
                    self._speech.resume()
                    return failure_response(
                        CommandCode.COMMAND_FAILED,
                        f"Speech could not be stopped before capture: {error}",
                        state="IDLE",
                    )
                # Ahead of `start_recording` rather than after it, because that
                # call begins warming the model: a countdown expiring in the
                # window between the two would take the weights back out from
                # under the warm-up. A countdown already past its own check still
                # releases, which the transcriber makes correct rather than
                # merely survivable: the pass reloads behind the same lock, so
                # that window costs one reload and never a transcript.
                self._transcription_idle.cancel()
                try:
                    self._session.start_recording()
                except Exception as error:
                    self._publish_error()
                    self._speech.resume()
                    # Capture never began, so the model is idle again and the
                    # countdown cancelled above has to go back on. Nothing else
                    # would put it there: the only other arm site is a recording
                    # ending, and this one never started.
                    self._transcription_idle.arm()
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
                pcm_audio = self._stop_recording()
            except Exception as error:
                self._publish_error()
                # Capture has ended, so the hold ends with it. Every other path
                # that ends capture releases it; without this one the session
                # stays silent and `status` keeps reporting SPEAKING until some
                # later capture happens to finish cleanly.
                with self._lock:
                    try:
                        self._speech.resume()
                    finally:
                        self._state = "IDLE"
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
        """Deliver to the session this capture was started for, or to nobody.

        Not "whichever session is open now": one sender can disconnect and
        another connect while a transcript is being produced, and the second
        one would receive words spoken to the first. Undelivered is the
        correct answer there -- the clipboard fallback then keeps them.
        """
        with self._speech_session_lock:
            session = self._capture_session
            if session is not self._speech_session:
                session = None
        if session is None:
            return False
        return session.send({"event": EVENT_TRANSCRIPT, "text": text}, confirm=True)

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
            # Under the state lock, and before the state goes IDLE. `resume`
            # reopens the output device and clears the barge-in hold, so a
            # capture that starts between the state going IDLE and this call
            # would have the loudspeaker opened underneath it -- speech and the
            # microphone in the room together, which is what the design rules
            # out and has no echo cancellation behind it. The inner finally
            # keeps a failing resume from wedging the daemon in THINKING.
            # Delivery has already happened in the body above, so a session
            # that was interrupted still receives its transcript before whatever
            # the sender says next.
            with self._lock:
                try:
                    self._speech.resume()
                finally:
                    self._state = STATE_IDLE

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
            "delivered": delivered,
        }
        if self._capture_destination == DESTINATION_SESSION:
            # A transcript produced inside a speech session goes to that session
            # and nowhere else. The hotkey process is not the recipient the
            # person chose, and a command response is not the one exception the
            # spec makes for carrying it.
            response["text"] = ""
        else:
            response["text"] = " ".join(texts)
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
            pcm_audio = self._stop_recording()
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
                try:
                    self._speech.resume()
                finally:
                    self._state = STATE_IDLE

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
                self._stop_recording()
            except Exception as stop_error:
                logger.warning("Unable to stop capture after a failed transition: %s", stop_error)
            self._publish_error()
            with self._lock:
                try:
                    self._speech.resume()
                finally:
                    self._state = "IDLE"

    def _finish_continuous_session(self) -> None:
        try:
            # The audio captured since the refused segment closed is discarded:
            # the session is ending because delivery was refused, so delivering
            # more of it would repeat the mistake.
            self._stop_recording()
        except Exception as error:
            logger.warning("Ending a continuous session failed: %s", error)
        finally:
            self._publish_error()
            # Every path that ends capture releases the hold, this one included:
            # text a sender queued while the person was speaking is owed its turn
            # as soon as the microphone closes, however the recording ended.
            with self._lock:
                try:
                    self._speech.resume()
                finally:
                    self._state = "IDLE"

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

    Dispatches on `socket_path`'s own shape (task 7.6): a pipe-shaped value is
    Windows' channel regardless of which platform this call happens to run on,
    the same rule `MurmlyDaemon._require_private_channel` applies from the
    server side.
    """
    if is_pipe_name(socket_path):
        return _send_command_over_pipe(socket_path, command, connect_timeout, response_timeout)
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


def _send_command_over_pipe(
    pipe_name: str,
    command: str,
    connect_timeout: float,
    response_timeout: float,
) -> dict[str, object]:
    """Windows' client half of `send_command`: same protocol and response
    shape as the UNIX branch, a named pipe underneath instead of a socket.

    `connect_named_pipe_client` raises `FileNotFoundError` and
    `ConnectionRefusedError` for the same conditions `socket.connect` does on
    the UNIX transport -- no pipe of this name, and a pipe that exists but has
    no instance free to accept -- which is what lets every existing caller of
    `send_command` (`cli.send_command_with_recovery`'s "no daemon running" and
    "daemon is not responding" branches) keep catching the same exception
    types regardless of which transport actually ran.
    """
    from murmly.win_pipe import connect_named_pipe_client

    client = connect_named_pipe_client(pipe_name, connect_timeout)
    try:
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
    finally:
        client.close()
    if not payload:
        raise DaemonNotRespondingError("Murmly daemon closed the connection before responding.")
    return json.loads(payload.decode("utf-8"))
