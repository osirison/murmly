"""What Home shows and when it asks the daemon: plain Python, no Qt.

Kept free of Qt so every CI job tests it. `controller.py` is the thin Qt layer:
a timer asks `Poller` whether a request is due, a worker thread runs
`HomeModel.refresh`, and the result comes back by signal to become the `view`
QML reads.

Nothing here logs a transcript, and nothing here knows what a window is, so
nothing here can take focus.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from murmly.daemon import (
    STATE_IDLE,
    STATE_LISTENING,
    STATE_SPEAKING,
    STATE_THINKING,
    send_command,
)

if TYPE_CHECKING:
    from murmly.config import MurmlyConfig

# The design says the app asks at most this often, and only while a window or
# the tray panel is visible. A change shows within one second of it happening:
# one interval to notice it, and a request that is bounded below.
STATUS_INTERVAL_S = 0.5
# `send_command`'s own bounds are sized for a toggle that waits for a
# transcription. A status answer is immediate, so a daemon that takes longer
# than this is shown as not answering rather than freezing a poll.
STATUS_CONNECT_TIMEOUT_S = 1.0
STATUS_RESPONSE_TIMEOUT_S = 1.0
# How long the "starting" note stays after a successful start request, for a
# daemon that is slow to bind its socket.
START_GRACE_S = 15.0

# The five things Home can say, by name. `needs_you` is not a daemon state: it
# is a delivery the daemon refused, which the last-words slot records.
READY = "ready"
LISTENING = "listening"
WRITING = "writing"
NEEDS_YOU = "needs_you"
NOT_RUNNING = "not_running"
PRESENTATIONS = (READY, LISTENING, WRITING, NEEDS_YOU, NOT_RUNNING)

# The same words `murmly toggle` uses when nothing is installed.
NOT_INSTALLED_TEXT = (
    "No service is installed. "
    "Run 'murmly install <hotkey>' to install it, for example: murmly install Meta+X"
)

# The hotkey record's purposes, in the order Home lists them.
KEY_PURPOSES = (
    ("window", "Dictate", "Press once to start listening, and again to type what you said."),
    (
        "session",
        "Reply to your assistant",
        "Sends your reply straight back to a coding assistant that is talking to you.",
    ),
)

Send = Callable[[str], Mapping[str, object]]


def default_send(socket_path: str) -> Send:
    """The existing command-socket client, bounded for polling."""

    def send(command: str) -> Mapping[str, object]:
        return send_command(
            socket_path,
            command,
            connect_timeout=STATUS_CONNECT_TIMEOUT_S,
            response_timeout=STATUS_RESPONSE_TIMEOUT_S,
        )

    return send


class Poller:
    """Decides when a status request may be sent.

    Visible means some surface the person can see is showing the state: the
    window today, the tray panel when it exists. Each surface reports itself by
    name, so one hiding does not hide the other. A request is allowed when
    something is visible, none is in flight, and at least `interval` has passed
    since the last one started. Nothing is allowed while nothing is visible, and
    the first request after a surface appears is allowed at once.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        interval: float = STATUS_INTERVAL_S,
    ) -> None:
        self._clock = clock
        self._interval = interval
        self._surfaces: set[str] = set()
        self._last_started: float | None = None
        self._in_flight = False

    @property
    def visible(self) -> bool:
        return bool(self._surfaces)

    def set_visible(self, surface: str, visible: bool) -> bool:
        """Record that `surface` is or is not showing; True when "anything visible" changed."""
        before = self.visible
        if visible:
            self._surfaces.add(surface)
        else:
            self._surfaces.discard(surface)
        after = self.visible
        if after and not before:
            # Shown after being hidden: the first request is not made to wait.
            self._last_started = None
        return after != before

    def begin(self) -> bool:
        """True, and the request counts as started, when one may be sent now."""
        if not self._surfaces or self._in_flight:
            return False
        now = self._clock()
        if self._last_started is not None and now - self._last_started < self._interval:
            return False
        self._last_started = now
        self._in_flight = True
        return True

    def finish(self) -> None:
        self._in_flight = False


@dataclass(frozen=True)
class LastWords:
    """The daemon's last-words record. `text` is never logged."""

    text: str
    produced_at: str
    delivered: bool
    detail: str | None = None

    @classmethod
    def from_record(cls, record: object) -> "LastWords | None":
        if not isinstance(record, Mapping):
            return None
        text = record.get("text")
        if not isinstance(text, str) or not text:
            return None
        detail = record.get("detail")
        return cls(
            text=text,
            produced_at=str(record.get("produced_at", "")),
            delivered=bool(record.get("delivered", False)),
            detail=detail if isinstance(detail, str) else None,
        )

    def __repr__(self) -> str:
        # A repr ends up in tracebacks and logs; the transcript must not.
        return f"LastWords(delivered={self.delivered}, produced_at={self.produced_at!r})"


@dataclass(frozen=True)
class Snapshot:
    """Everything one refresh learned."""

    reachable: bool
    state: str | None = None
    error: str | None = None
    held: tuple[str, ...] = ()
    last_words: LastWords | None = None
    keys: Mapping[str, str] | None = None
    config: "MurmlyConfig | None" = None


@dataclass(frozen=True)
class StartResult:
    ok: bool
    detail: str = ""


def start_installed_service(service: object | None = None) -> StartResult:
    """Start the installed background service, as the platform's installer does.

    `service` is injectable so a test never touches systemd or Task Scheduler.
    """
    try:
        if service is None:
            from murmly.installer import default_service

            service = default_service()
        if not service.is_installed:
            return StartResult(False, NOT_INSTALLED_TEXT)
        if service.start():
            return StartResult(True)
    except Exception as error:  # noqa: BLE001 - shown to the person, never raised into the GUI
        return StartResult(False, f"The service could not be started: {error}")
    return StartResult(False, "The service did not start.")


class HomeModel:
    """Fetches what Home shows. Blocking: it runs on the controller's worker thread."""

    def __init__(
        self,
        send: Send,
        config_loader: Callable[[], "MurmlyConfig"] | None = None,
        key_reader: Callable[[], Mapping[str, str]] | None = None,
        start_service: Callable[[], StartResult] = start_installed_service,
    ) -> None:
        self._send = send
        self._config_loader = config_loader
        self._key_reader = key_reader
        self._start_service = start_service
        self._config: "MurmlyConfig | None" = None
        self._keys: Mapping[str, str] | None = None
        self._last_words: LastWords | None = None
        self._previous_state: str | None = None
        self._reload = True

    def reload(self) -> None:
        """Re-read the last words, keys and setup on the next refresh (Home opened)."""
        self._reload = True

    def refresh(self) -> Snapshot:
        try:
            status = dict(self._send("status"))
            if not status.get("ok"):
                raise RuntimeError(str(status.get("error") or "The daemon refused the status request."))
        except Exception as error:  # noqa: BLE001 - any failure to ask is "not reachable"
            self._previous_state = None
            self._last_words = None
            self._reload = True
            return Snapshot(reachable=False, error=str(error) or type(error).__name__)

        state = str(status.get("state") or STATE_IDLE)
        held = tuple(str(name) for name in status.get("hotkeys_held") or ())
        previous = self._previous_state
        self._previous_state = state

        # The slot changes when a transcript ends, so it is read when Home
        # opens, when the daemon comes back, and when a busy state returns to
        # idle -- not on every poll.
        settled = state == STATE_IDLE and previous != STATE_IDLE
        if self._reload or settled:
            self._load_setup()
            self._last_words = self._read_last_words()
            self._reload = False
        return Snapshot(
            reachable=True,
            state=state,
            held=held,
            last_words=self._last_words,
            keys=self._keys,
            config=self._config,
        )

    def clear_last_words(self) -> Snapshot:
        try:
            self._send("clear_last_words")
        except Exception:  # noqa: BLE001 - the next refresh reports whatever is wrong
            pass
        self._last_words = None
        self._reload = True
        return self.refresh()

    def start_service(self) -> StartResult:
        return self._start_service()

    def _read_last_words(self) -> LastWords | None:
        try:
            response = self._send("last_words")
        except Exception:  # noqa: BLE001 - the status request just worked; try again next time
            self._reload = True
            return self._last_words
        return LastWords.from_record(response.get("last_words"))

    def _load_setup(self) -> None:
        if self._config_loader is not None:
            try:
                self._config = self._config_loader()
            except Exception:  # noqa: BLE001 - keeps the last good setup
                pass
        if self._key_reader is not None:
            try:
                self._keys = dict(self._key_reader())
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# From a snapshot to words


def present(snapshot: Snapshot, dismissed: str | None = None) -> str:
    """Which of the five presentations a snapshot is.

    Listening and writing come first: a new recording is the most recent thing,
    and the refused words are still in the last-words slot underneath it.
    `dismissed` is the `produced_at` of a refusal the person has acknowledged.
    """
    if not snapshot.reachable:
        return NOT_RUNNING
    if snapshot.state == STATE_LISTENING:
        return LISTENING
    if snapshot.state in (STATE_THINKING, STATE_SPEAKING):
        return WRITING
    if snapshot.state not in (None, STATE_IDLE):
        return WRITING
    words = snapshot.last_words
    if words is not None and not words.delivered and words.produced_at != dismissed:
        return NEEDS_YOU
    return READY


def relative_time(produced_at: str, now: datetime) -> str:
    """How long ago, in the words the canvas uses ("12 s ago", "2 minutes ago")."""
    try:
        then = datetime.fromisoformat(produced_at)
    except ValueError:
        return ""
    if then.tzinfo is None and now.tzinfo is not None:
        then = then.astimezone()
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 60:
        return f"{seconds} s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


_HERO = {
    READY: (
        "ready",
        "Ready when you are",
        "Press your keys in any window, say what you mean, then press them again. "
        "Murmly types your words where you started, and nothing you say leaves this computer.",
    ),
    LISTENING: (
        "listening",
        "Listening",
        "Take your time. Press your keys again when you are done, "
        "and your words are typed into the window you started in.",
    ),
    WRITING: (
        "writing",
        "Writing your words",
        "Murmly is turning what you said into text. It types it where you started.",
    ),
    NEEDS_YOU: (
        "1 waiting on you",
        "Your words are safe on the clipboard",
        "Nothing was lost. Murmly held back rather than type into the wrong place.",
    ),
    NOT_RUNNING: (
        "service not running",
        "The background service is not running",
        "Murmly cannot hear the dictation key until its background service is running.",
    ),
}

_TONE = {READY: "neutral", LISTENING: "warm", WRITING: "cool", NEEDS_YOU: "crimson", NOT_RUNNING: "crimson"}

_ACCURACY = {"fast": "Fast", "balanced": "Balanced", "accurate": "Accurate"}
_DEVICE = {"auto": "Chosen automatically", "cuda": "Graphics card", "cpu": "Processor"}
_FINISHING = {
    "off": "Press the keys again",
    "stop": "Stops when you stop talking",
    "continuous": "Types as you pause",
}
_VOICES = {"off": "Everyone", "mine-only": "Only your voice", "label-everyone": "Everyone, by name"}


def keys_view(keys: Mapping[str, str] | None, held: tuple[str, ...]) -> list[dict[str, object]]:
    """The keys bound, one card each. `keys` is the hotkey record (purpose to `Meta+X`)."""
    cards = []
    for purpose, label, helper in KEY_PURPOSES:
        text = (keys or {}).get(purpose)
        parts = [part for part in text.split("+") if part] if text else []
        if not parts:
            note = "not set"
        elif purpose in held:
            note = "registered · held by murmly"
        else:
            note = "set by murmly install"
        cards.append({"label": label, "keys": parts, "note": note, "helper": helper})
    return cards


def setup_view(config: "MurmlyConfig | None") -> list[dict[str, str]]:
    """The setup summary: a plain value and the raw setting it came from."""
    if config is None:
        return []
    if config.unload_after_idle_s > 0:
        minutes = config.unload_after_idle_s / 60
        after = f"{minutes:g} min" if config.unload_after_idle_s % 60 == 0 else f"{config.unload_after_idle_s} s"
        memory = ("Given back when idle", f"after {after}")
    else:
        memory = ("Kept ready", "unload_after_idle_s = 0")
    return [
        {
            "label": "Accuracy",
            "value": _ACCURACY.get(config.model_profile, config.model_profile),
            "note": config.model_name,
        },
        {
            "label": "Runs on",
            "value": _DEVICE.get(config.device, config.device),
            "note": f"{config.device} · {config.compute_type}",
        },
        {"label": "Memory", "value": memory[0], "note": memory[1]},
        {
            "label": "Finishing",
            "value": _FINISHING.get(config.auto_transcribe, config.auto_transcribe),
            "note": f"auto_transcribe = {config.auto_transcribe}",
        },
        {
            "label": "Voices",
            "value": _VOICES.get(config.speaker_mode, config.speaker_mode),
            "note": config.speaker_mode,
        },
    ]


def build_view(
    snapshot: Snapshot,
    *,
    now: datetime,
    dismissed: str | None = None,
    starting: bool = False,
    start_error: str = "",
) -> dict[str, object]:
    """Everything QML shows on Home and in the header, as plain values.

    Strings the daemon wrote (`detail`, a connection error) go in unaltered.
    """
    key = present(snapshot, dismissed)
    chip, headline, body = _HERO[key]
    if key == WRITING and snapshot.state == STATE_SPEAKING:
        chip, headline, body = "speaking", "Speaking", "Murmly is reading a reply aloud."
    words = snapshot.last_words
    last: dict[str, object] | None = None
    if words is not None:
        outcome = "typed" if words.delivered else "not typed · copied to the clipboard"
        last = {
            "text": words.text,
            "when": relative_time(words.produced_at, now),
            "delivered": words.delivered,
            "outcome": outcome,
            "detail": words.detail or "",
        }
    config_path = str(snapshot.config.config_path) if snapshot.config is not None else ""
    running = "service running" if snapshot.reachable else "service not running"
    return {
        "state": key,
        "tone": _TONE[key],
        "chip": chip,
        "headline": headline,
        "body": body,
        "lastWords": last,
        "keys": keys_view(snapshot.keys, snapshot.held),
        "setup": setup_view(snapshot.config),
        "footer": f"{running} · {config_path}" if config_path else running,
        "service": {
            "starting": starting,
            "error": start_error,
            # What the daemon (or the socket layer) said, unaltered.
            "reason": snapshot.error or "",
        },
    }
