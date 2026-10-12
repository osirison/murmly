"""What Settings shows and writes: plain Python, no Qt.

Every control on the page names the one configuration key it writes. `to_value`
turns what a control reports (a switch's state, a slider's position, the text of
a time field) into the value for that key, `SettingsModel.apply` hands it to the
`SettingsWriter` (which checks it against `murmly.config`'s own rules and keeps
the rest of the file as the person wrote it), and `build_view` turns the file's
values into the strings and flags the page draws.

A control whose key is not in `murmly.config.SETTING_RULES` does not belong here:
the writer would refuse it, and widening the writer is a decision of its own.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from murmly import config
from murmly.config import MODEL_PROFILES, MurmlyConfig, SettingValueError, load_config
from murmly.settings_apply import RestartOutcome, RestartStatus
from murmly.settings_writer import ConfigParseError, SettingsFileError, SettingsWriter

#: The quiet window written when the person switches quiet hours on.
DEFAULT_QUIET_WINDOW = "22:00-07:00"

NOTICE_FILE_CHANGED = "The settings file changed on disk, so it was read again."
NOTICE_RESTART_WAITING = "will apply when Murmly is idle"


# ---------------------------------------------------------------------------
# The controls
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Control:
    """One control and the key it writes.

    `kind` says how its reported value becomes the key's value:
    `choice`, `flag` and `number` pass through; `idle` is a switch for a number of
    seconds where off is 0; `quiet` is a switch for a window where off is empty;
    `quiet-from` and `quiet-to` write one end of the window.
    """

    id: str
    key: str
    kind: str


CONTROLS: tuple[Control, ...] = (
    Control("profile", "stt.model_profile", "choice"),
    Control("stt-device", "stt.device", "choice"),
    Control("unload", "stt.unload_after_idle_s", "idle"),
    Control("finish", "stt.auto_transcribe", "choice"),
    Control("pause", "stt.auto_transcribe_silence_ms", "number"),
    Control("live", "stt.live_transcribe", "flag"),
    Control("verify", "clipboard.verify_target", "flag"),
    Control("restore", "clipboard.restore", "flag"),
    Control("overlay", "overlay.enabled", "flag"),
    Control("reduced", "overlay.reduced_motion", "flag"),
    Control("text-size", "overlay.text_size_px", "number"),
    Control("speak", "tts.enabled", "flag"),
    Control("voice", "tts.voice", "choice"),
    Control("rate", "tts.rate", "number"),
    Control("quiet", "tts.quiet_hours", "quiet"),
    Control("quiet-from", "tts.quiet_hours", "quiet-from"),
    Control("quiet-to", "tts.quiet_hours", "quiet-to"),
    Control("tts-device", "tts.device", "choice"),
)
CONTROL_BY_ID: dict[str, Control] = {control.id: control for control in CONTROLS}


class UnknownControl(ValueError):
    """A control id this page does not have."""


def quiet_ends(window: object) -> tuple[str, str]:
    """The two ends of a `tts.quiet_hours` value, or the default window's."""
    if isinstance(window, str) and window.count("-") == 1:
        start, end = window.split("-")
        return start, end
    start, end = DEFAULT_QUIET_WINDOW.split("-")
    return start, end


def to_value(control_id: str, raw: object, values: Mapping[str, object]) -> object:
    """The value to write for what `control_id` reported.

    `values` are the file's current values by key; only the two ends of the quiet
    window need them, because each is written together with the other.
    """
    control = CONTROL_BY_ID.get(control_id)
    if control is None:
        raise UnknownControl(f"{control_id} is not a setting on this page.")
    if control.kind == "number":
        # A slider reports a float; a whole one is the integer the rule wants.
        if isinstance(raw, float) and raw.is_integer():
            return int(raw)
        return raw
    if control.kind == "idle":
        return config.DEFAULT_STT_UNLOAD_AFTER_IDLE_S if raw else 0
    if control.kind == "quiet":
        return DEFAULT_QUIET_WINDOW if raw else ""
    if control.kind in ("quiet-from", "quiet-to"):
        start, end = quiet_ends(values.get(control.key))
        text = str(raw).strip()
        return f"{text}-{end}" if control.kind == "quiet-from" else f"{start}-{text}"
    return raw


# ---------------------------------------------------------------------------
# Reading and writing the file
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SettingsState:
    """What the file said when it was read.

    `values` are by key, and are what Murmly would use (a key the file leaves out
    holds its default). `error` is the parser's own message, or the operating
    system's, exactly as raised; while it is set no control may write.
    """

    path: Path
    values: dict[str, object] = field(default_factory=dict)
    error: str = ""
    parse_error: bool = False
    #: A hand edit was noticed and the file read again.
    changed: bool = False


@dataclass(frozen=True, slots=True)
class SaveOutcome:
    """What a change came to. `message` is the writer's words, unaltered."""

    #: "saved", "refused" (the value is outside its range), "unreadable" (the
    #: file does not parse or cannot be read) or "failed" (it could not be written).
    status: str
    key: str = ""
    message: str = ""
    #: The file had changed since it was read; it was read again before writing.
    file_changed: bool = False

    @property
    def saved(self) -> bool:
        return self.status == "saved"


def quiet_text(config_: MurmlyConfig) -> str:
    if config_.tts_quiet_start is None or config_.tts_quiet_end is None:
        return ""
    return f"{config_.tts_quiet_start:%H:%M}-{config_.tts_quiet_end:%H:%M}"


def values_from(config_: MurmlyConfig) -> dict[str, object]:
    """The value of every key a control writes, as Murmly reads the file."""
    return {
        "stt.model_profile": config_.model_profile,
        "stt.device": config_.device,
        "stt.unload_after_idle_s": config_.unload_after_idle_s,
        "stt.auto_transcribe": config_.auto_transcribe,
        "stt.auto_transcribe_silence_ms": config_.auto_transcribe_silence_ms,
        "stt.live_transcribe": config_.live_transcribe,
        "clipboard.verify_target": config_.verify_target,
        "clipboard.restore": config_.restore_clipboard,
        "overlay.enabled": config_.overlay_enabled,
        "overlay.reduced_motion": config_.overlay_reduced_motion,
        "overlay.text_size_px": config_.overlay_text_size_px,
        "tts.enabled": config_.tts_enabled,
        "tts.voice": config_.tts_voice,
        "tts.rate": config_.tts_rate_percent,
        "tts.quiet_hours": quiet_text(config_),
        "tts.device": config_.tts_device,
    }


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


class SettingsModel:
    """Reads the configuration file and writes one key at a time.

    Everything here blocks on the file system, so the app calls it from its one
    worker thread, never the GUI thread. `writer` and `loader` are injectable so a
    test points it at a temporary file.
    """

    def __init__(
        self,
        writer: SettingsWriter | None = None,
        loader: Callable[[Path], MurmlyConfig] = load_config,
    ) -> None:
        self.writer = writer if writer is not None else SettingsWriter()
        self._loader = loader
        self._values: dict[str, object] = {}
        self._stamp: tuple[int, int] | None = None
        self._read_once = False

    @property
    def path(self) -> Path:
        return self.writer.path

    def load(self, *, changed: bool = False) -> SettingsState:
        """Read the file. A file that does not parse comes back as an error, not raised."""
        path = self.writer.path
        self._stamp = _stamp(path)
        self._read_once = True
        try:
            self.writer.read()
            values = values_from(self._loader(path))
        except ConfigParseError as error:
            return SettingsState(path, error=str(error), parse_error=True, changed=changed)
        except tomllib.TOMLDecodeError as error:
            # The file changed between the two reads and no longer parses.
            return SettingsState(path, error=str(error), parse_error=True, changed=changed)
        except (SettingsFileError, OSError) as error:
            return SettingsState(path, error=str(error), changed=changed)
        self._values = values
        return SettingsState(path, values=dict(values), changed=changed)

    def poll(self) -> SettingsState | None:
        """The file read again if it changed on disk since the last read, else None."""
        if not self._read_once or _stamp(self.writer.path) == self._stamp:
            return None
        return self.load(changed=True)

    def apply(self, control_id: str, raw: object) -> tuple[SaveOutcome, SettingsState]:
        """Write what a control reported, then read the file again for what it now says."""
        control = CONTROL_BY_ID.get(control_id)
        if control is None:
            raise UnknownControl(f"{control_id} is not a setting on this page.")
        key = control.key
        try:
            result = self.writer.set(key, to_value(control_id, raw, self._values))
        except SettingValueError as error:
            outcome = SaveOutcome("refused", key, str(error))
        except SettingsFileError as error:
            status = "unreadable" if isinstance(error, ConfigParseError) else "failed"
            outcome = SaveOutcome(status, key, str(error))
        else:
            outcome = SaveOutcome("saved", key, file_changed=result.file_changed)
        return outcome, self.load()


# ---------------------------------------------------------------------------
# What the page draws
# ---------------------------------------------------------------------------

_PROFILE_TEXT = {
    "fast": ("Fast", "Quick on any computer, even without a graphics card. Misses more words."),
    "balanced": (
        "Balanced",
        "Close to the most careful model at a fraction of the wait. The default.",
    ),
    "accurate": (
        "Accurate",
        "The most careful listener. The slowest, and it needs the most memory.",
    ),
}

_DEVICE_LABELS = {"auto": "Automatic", "cpu": "Processor", "cuda": "Graphics card"}

_FINISH_OPTIONS = (
    ("off", "Press the keys again"),
    ("stop", "Stop when I pause"),
    ("continuous", "Type each sentence"),
)
_FINISH_TEXT = {
    "off": "Silence never ends a recording. You decide when you are done by pressing the keys again.",
    "stop": "A pause ends the recording, just as pressing the keys again would.",
    "continuous": (
        "Each pause types what you have said so far, and murmly keeps listening for your next sentence."
    ),
}

# The voices the page offers. The file may name any other voice the model carries;
# that one is shown as well, so it never goes missing from the list.
_VOICES = (
    ("af_heart", "Heart"),
    ("af_bella", "Bella"),
    ("bf_emma", "Emma"),
    ("am_michael", "Michael"),
)
_ACCENTS = {"a": "American", "b": "British"}

# The slider's own span. The file accepts a wider range; a value outside the
# span is shown as it is and kept unless the person moves the slider.
PAUSE_SLIDER = {"min": 250, "max": 5_000, "step": 250}
RATE_SLIDER = {
    "min": config.MIN_TTS_RATE_PERCENT,
    "max": config.MAX_TTS_RATE_PERCENT,
    "step": 5,
}
TEXT_SIZE = {"min": config.MIN_OVERLAY_TEXT_SIZE_PX, "max": config.MAX_OVERLAY_TEXT_SIZE_PX}

_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine"}


def _number(count: int) -> str:
    return _WORDS.get(count, str(count))


def _plural(count: int, unit: str) -> str:
    return f"{_number(count)} {unit}" + ("" if count == 1 else "s")


def idle_period(seconds: int) -> str:
    """A period in the words a person says it in: "five minutes", "90 seconds"."""
    if seconds % 3600 == 0:
        return _plural(seconds // 3600, "hour")
    if seconds % 60 == 0:
        return _plural(seconds // 60, "minute")
    return f"{seconds} seconds"


def key_line(key: str, value: object) -> str:
    """`key = value` as the file writes it, shown in mono beside a control."""
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, str):
        text = json.dumps(value)
    else:
        text = str(value)
    return f"{key} = {text}"


def seconds_label(milliseconds: int) -> str:
    return f"{milliseconds / 1000:g} s"


def _voice_options(current: object) -> list[dict[str, object]]:
    voices = list(_VOICES)
    if isinstance(current, str) and current and all(current != known for known, _ in voices):
        voices.append((current, current.split("_", 1)[-1].capitalize()))
    options = []
    for voice, name in voices:
        accent = _ACCENTS.get(voice[:1], "")
        options.append(
            {
                "id": voice,
                "name": name,
                "detail": f"{voice} · {accent}" if accent else voice,
                "checked": voice == current,
            }
        )
    return options


def build_options(values: Mapping[str, object]) -> dict[str, list[dict[str, object]]]:
    """The choices each choice control offers, with the current one marked."""
    profile = values.get("stt.model_profile")
    finish = values.get("stt.auto_transcribe")
    stt_device = values.get("stt.device")
    tts_device = values.get("tts.device")
    tts_devices = ["cpu", "cuda"] + (["auto"] if tts_device == "auto" else [])
    return {
        "profile": [
            {
                "id": name,
                "name": _PROFILE_TEXT[name][0],
                "detail": MODEL_PROFILES[name].model_name,
                "text": _PROFILE_TEXT[name][1],
                "checked": name == profile,
            }
            for name in ("fast", "balanced", "accurate")
        ],
        "stt-device": [
            {"id": name, "label": _DEVICE_LABELS[name], "checked": name == stt_device}
            for name in ("auto", "cpu", "cuda")
        ],
        "finish": [
            {"id": name, "label": label, "checked": name == finish} for name, label in _FINISH_OPTIONS
        ],
        "voice": _voice_options(values.get("tts.voice")),
        "tts-device": [
            {"id": name, "label": _DEVICE_LABELS[name], "checked": name == tts_device}
            for name in tts_devices
        ],
    }


def build_view(
    state: SettingsState | None,
    *,
    notice: str = "",
    outcome: SaveOutcome | None = None,
    restart: str = "",
) -> dict[str, object]:
    """Everything the page draws, as plain data.

    `notice` is the short line about the file having changed. `outcome` is the
    last change, shown when it was refused or could not be written. `restart` is
    "" (nothing to say), "waiting", "restarted" or the words of a failure.
    """
    if state is None:
        return {"loaded": False, "disabled": True, "values": {}, "options": {}, "ranges": ranges()}
    values = state.values
    window = values.get("tts.quiet_hours")
    quiet_on = isinstance(window, str) and bool(window)
    start, end = quiet_ends(window)
    idle = values.get("stt.unload_after_idle_s")
    idle_on = isinstance(idle, int) and idle > 0
    finish = values.get("stt.auto_transcribe")
    silence = values.get("stt.auto_transcribe_silence_ms")
    rate = values.get("tts.rate")
    live = values.get("stt.live_transcribe") is True
    # Keys the file does not set show Murmly's default, so the page is never empty.
    controls: dict[str, object] = {
        "profile": values.get("stt.model_profile"),
        "stt-device": values.get("stt.device"),
        "unload": idle_on,
        "finish": finish,
        "pause": silence,
        "live": live,
        "verify": values.get("clipboard.verify_target"),
        "restore": values.get("clipboard.restore"),
        "overlay": values.get("overlay.enabled"),
        "reduced": values.get("overlay.reduced_motion"),
        "text-size": values.get("overlay.text_size_px"),
        "speak": values.get("tts.enabled"),
        "voice": values.get("tts.voice"),
        "rate": rate,
        "quiet": quiet_on,
        "quiet-from": start,
        "quiet-to": end,
        "tts-device": values.get("tts.device"),
    }
    keys = {
        control.id: key_line(control.key, values[control.key])
        for control in CONTROLS
        if control.key in values
    }
    if isinstance(rate, int):
        rate_note = (
            "The voice at its own pace."
            if rate == 100
            else "Faster than the voice speaks on its own."
            if rate > 100
            else "Slower than the voice speaks on its own."
        )
    else:
        rate_note = ""
    hours = ""
    if quiet_on and start != end:
        hours = _quiet_summary(start, end)
    status = ""
    if restart == "waiting":
        status = f"Saved, and {NOTICE_RESTART_WAITING}."
    elif restart == "restarted":
        status = "Saved. Murmly restarted to use it."
    elif restart:
        status = f"Saved, but Murmly could not restart: {restart}"
    message = ""
    if outcome is not None and outcome.status in ("refused", "failed", "unreadable"):
        message = outcome.message
    return {
        "loaded": True,
        "disabled": bool(state.error),
        # The parser's words (or the operating system's), exactly as raised.
        "error": state.error,
        "errorIsParse": state.parse_error,
        "notice": notice,
        "message": message,
        "status": status,
        "path": str(state.path),
        "footer": f"saved to {state.path}",
        "values": controls,
        "keys": keys,
        "options": build_options(values),
        "ranges": ranges(),
        "finishText": _FINISH_TEXT.get(finish, "") if isinstance(finish, str) else "",
        "showPause": finish in ("stop", "continuous"),
        "pauseLabel": seconds_label(silence) if isinstance(silence, int) else "",
        "unloadText": (
            ""
            if idle is None
            else f"After {idle_period(idle)} quiet the model leaves memory. It reloads while you start "
            "speaking, so you never wait for it."
            if idle_on
            else "The model stays in memory until murmly restarts. It is ready the moment you speak."
        ),
        "rateLabel": f"{rate} %" if isinstance(rate, int) else "",
        "rateNote": rate_note,
        "quietSummary": hours,
        "liveBehind": live
        and values.get("stt.device") == "cpu"
        and values.get("stt.model_profile") != "fast",
        "textSizeLabel": f"{controls['text-size']} px" if "overlay.text_size_px" in values else "",
    }


def _minutes(clock: str) -> int | None:
    parts = clock.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]) * 60 + int(parts[1])


def _quiet_summary(start: str, end: str) -> str:
    begin, finish = _minutes(start), _minutes(end)
    if begin is None or finish is None or begin == finish:
        return ""
    length = (finish - begin) % 1440
    hours = round(length / 60, 1)
    text = f"{hours:g}"
    return f"{text} quiet hours, every day"


def ranges() -> dict[str, dict[str, int]]:
    return {"pause": PAUSE_SLIDER, "rate": RATE_SLIDER, "text-size": TEXT_SIZE}


def restart_status(outcome: RestartOutcome) -> str | None:
    """What a restart attempt says to the page; None when it changes nothing."""
    if outcome.status is RestartStatus.NOTHING_PENDING:
        return None
    if outcome.status is RestartStatus.WAITING:
        return "waiting"
    if outcome.status is RestartStatus.RESTARTED:
        return "restarted"
    return outcome.detail or "The service did not restart."
