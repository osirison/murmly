from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path, PurePosixPath, PureWindowsPath
import os
import re
import tomllib

from murmly.platform import OperatingSystem, resolve_platform


@dataclass(frozen=True, slots=True)
class ModelProfile:
    model_name: str
    model_revision: str | None
    beam_size: int
    vad_filter: bool


MODEL_PROFILES = {
    "fast": ModelProfile("tiny.en", model_revision=None, beam_size=1, vad_filter=False),
    "balanced": ModelProfile(
        "large-v3-turbo",
        model_revision="0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
        beam_size=5,
        vad_filter=True,
    ),
    "accurate": ModelProfile(
        "large-v3",
        model_revision=None,
        beam_size=5,
        vad_filter=True,
    ),
}
VALID_DEVICES = {"auto", "cpu", "cuda"}
DEFAULT_RESTORE_DELAY_MS = 500
MAX_RESTORE_DELAY_MS = 5_000

VALID_AUTO_TRANSCRIBE_MODES = {"off", "stop", "continuous"}
DEFAULT_AUTO_TRANSCRIBE_MODE = "off"

DEFAULT_LIVE_INTERVAL_MS = 1_000
MIN_LIVE_INTERVAL_MS = 250
MAX_LIVE_INTERVAL_MS = 10_000

DEFAULT_LIVE_WINDOW_SECONDS = 15
MIN_LIVE_WINDOW_SECONDS = 5
MAX_LIVE_WINDOW_SECONDS = 60

DEFAULT_SILENCE_MS = 2_000
MIN_SILENCE_MS = 250
MAX_SILENCE_MS = 30_000

DEFAULT_MIN_SPEECH_MS = 300
MIN_MIN_SPEECH_MS = 0
MAX_MIN_SPEECH_MS = 10_000

DEFAULT_OVERLAY_TEXT_SIZE_PX = 13
MIN_OVERLAY_TEXT_SIZE_PX = 8
MAX_OVERLAY_TEXT_SIZE_PX = 48
VALID_COMPUTE_TYPES = {
    "auto",
    "bfloat16",
    "float16",
    "float32",
    "int8",
    "int8_bfloat16",
    "int8_float16",
    "int8_float32",
    "int16",
}

# The English voices the synthesis model carries. The rest of its voices speak
# other languages, and transcription is fixed to English (`stt.py`
# `language="en"`), so offering them would let speech and transcription disagree
# about what language a conversation is in.
VALID_TTS_VOICES = {
    "af_alloy",
    "af_aoede",
    "af_bella",
    "af_heart",
    "af_jessica",
    "af_kore",
    "af_nicole",
    "af_nova",
    "af_river",
    "af_sarah",
    "af_sky",
    "am_adam",
    "am_echo",
    "am_eric",
    "am_fenrir",
    "am_liam",
    "am_michael",
    "am_onyx",
    "am_puck",
    "am_santa",
    "bf_alice",
    "bf_emma",
    "bf_isabella",
    "bf_lily",
    "bm_daniel",
    "bm_fable",
    "bm_george",
    "bm_lewis",
}
DEFAULT_TTS_VOICE = "af_heart"

# A percentage of the model's own speaking rate, so 100 is unmodified. Bounded
# rather than free: the model distorts badly outside roughly half to double.
DEFAULT_TTS_RATE_PERCENT = 100
MIN_TTS_RATE_PERCENT = 50
MAX_TTS_RATE_PERCENT = 200

# The processor synthesis runs on, taking the same vocabulary as `[stt] device`
# and deliberately independent of it: transcription is a burst the person waits
# on with nothing to overlap it, while synthesis is produced a sentence ahead of
# playback. The CPU is the default because the CUDA provider never gives back
# what it takes for synthesis -- 876 MiB of system memory and 1208 MiB of
# accelerator memory stay held once a synthesis session has existed -- and the
# CPU costs only about 200 ms more before the first word.
DEFAULT_TTS_DEVICE = "cpu"

# How long each model may sit unused before Murmly hands its memory back, with
# `0` meaning never. The floor is 30 s because anything shorter fires between
# dictations in an ordinary working session, and the ceiling is 24 hours because
# past that the setting is indistinguishable from `0`.
MIN_UNLOAD_AFTER_IDLE_S = 30
MAX_UNLOAD_AFTER_IDLE_S = 86_400

# The two defaults differ because the two releases are not alike. Transcription
# returns 2080 MiB of accelerator memory and its 0.78 s reload is started when
# capture begins, so the wait is spent while the person is still speaking and
# every install may as well have it. Synthesis returns system memory under the
# default `[tts] device = "cpu"` and costs 759-767 ms of silence before speech
# resumes, with nothing to overlap it, so it stays opt-in like speech output
# itself.
DEFAULT_STT_UNLOAD_AFTER_IDLE_S = 300
DEFAULT_TTS_UNLOAD_AFTER_IDLE_S = 0

# What `[speakers] mode` does with the people the microphone hears. `off` is the
# default and runs no speaker code at all; `mine-only` delivers only the
# configured owner's speech; `label-everyone` delivers all of it, prefixed by
# who said it.
VALID_SPEAKER_MODES = {"off", "mine-only", "label-everyone"}
DEFAULT_SPEAKER_MODE = "off"

# The similarity, as an integer percent, at which a voice counts as an enrolled
# one. An integer for the reason `[tts] rate` is: the existing helpers read
# integers, and `0.55` then reads as 0, falls back and is reported rather than
# being silently truncated. 30 is the highest value that kept every one of the
# owner's parts and let no other voice through in the recordings measured for
# the change's design.md (task 4.7).
DEFAULT_SPEAKER_MATCH_THRESHOLD_PERCENT = 30
MIN_SPEAKER_MATCH_THRESHOLD_PERCENT = 30
MAX_SPEAKER_MATCH_THRESHOLD_PERCENT = 90


# The window speech is refused in, written as one string in the machine's own
# local time. One setting rather than a start and an end because a window is one
# thing and two settings can be half-written: a start with no end has no honest
# reading -- silence forever, silence never, or a refusal to start, and each is a
# bad answer to a typo.
#
# A single-digit hour is accepted because `9:00` is what people write. Seconds
# are not: nobody sets a bedtime to the second, and accepting them widens the
# parser for no gain.
QUIET_HOURS_PATTERN = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


#: Windows has no `AF_UNIX`, so the command channel there is a named pipe
#: rather than a file (see design.md's "The command channel"). A module
#: constant rather than a literal inline, so the daemon's actual pipe creation
#: in a later phase revises this one name rather than a string that also
#: appears here.
WINDOWS_PIPE_NAME = r"\\.\pipe\murmly"


def absolute_setting(
    environment: Mapping[str, str], name: str, windows: bool = False
) -> Path | None:
    """The folder `name` sets, or None when it is unset, empty or not an absolute path.

    The XDG Base Directory specification says a relative value must be ignored, and
    that covers `~/x`, which no shell expanded here. Used verbatim, one would aim
    Murmly at whatever folder the current directory happens to hold.
    """
    value = environment.get(name)
    if not value:
        return None
    # Absolute in the shape the platform being answered for uses, or on this host.
    pure = PureWindowsPath(value) if windows else PurePosixPath(value)
    return Path(value) if pure.is_absolute() or Path(value).is_absolute() else None


def home_directory(env: Mapping[str, str] | None = None, windows: bool = False) -> Path:
    """The home folder: from `env` when one is given, and only then from `Path.home()`.

    A given environment answers for itself, so a caller that supplies a temporary
    one is never sent to the real person's home. No usable home in it raises
    rather than guessing one.
    """
    if env is None:
        return Path.home()
    name = "USERPROFILE" if windows else "HOME"
    home = absolute_setting(env, name, windows)
    if home is None:
        raise RuntimeError(f"The environment names no absolute home folder in {name}.")
    return home


def default_runtime_dir(env: dict[str, str] | None = None) -> Path | None:
    """Where Murmly's own runtime files -- today, just the command socket -- go.

    None on Windows rather than a fabricated path: there is no per-user runtime
    directory there, because the command channel is a named pipe living in the
    kernel's pipe namespace, not a file anywhere on disk. Section 7 implements
    the pipe itself; this stays honest about there being no filesystem answer
    until then, rather than inventing one nothing will ever create.
    """
    profile = resolve_platform(env)
    if profile.operating_system is OperatingSystem.WINDOWS:
        return None
    if profile.operating_system is OperatingSystem.MACOS:
        return home_directory(env) / "Library" / "Caches" / "murmly"
    # Linux, and anything `resolve_platform` could not name (OperatingSystem.OTHER)
    # keep the answer this always gave: an existing install must not see its
    # runtime directory move.
    environment = env if env is not None else os.environ
    runtime_dir = absolute_setting(environment, "XDG_RUNTIME_DIR")
    if runtime_dir:
        return runtime_dir
    return Path(f"/run/user/{os.getuid()}")


def default_socket_path(env: dict[str, str] | None = None) -> Path:
    """Where the command channel listens by default.

    `default_runtime_dir` returning None is Windows' way of saying there is no
    directory to build a path under, so that is answered with the pipe name
    directly rather than joining a filename onto nothing.
    """
    runtime_dir = default_runtime_dir(env)
    if runtime_dir is None:
        return Path(WINDOWS_PIPE_NAME)
    return runtime_dir / "murmly.sock"


def default_data_dir(env: dict[str, str] | None = None) -> Path:
    """Where Murmly keeps the files it downloads or writes for itself.

    The synthesis model and its voices live directly in it, and so will the
    speaker model and the voiceprints.
    """
    profile = resolve_platform(env)
    if profile.operating_system is OperatingSystem.WINDOWS:
        environment = env if env is not None else os.environ
        local_app_data = absolute_setting(environment, "LOCALAPPDATA", windows=True)
        if local_app_data:
            return local_app_data / "murmly"
        return home_directory(env, windows=True) / "AppData" / "Local" / "murmly"
    if profile.operating_system is OperatingSystem.MACOS:
        return home_directory(env) / "Library" / "Application Support" / "murmly"
    # Linux, and OperatingSystem.OTHER, unchanged.
    environment = env if env is not None else os.environ
    xdg_data_home = absolute_setting(environment, "XDG_DATA_HOME")
    if xdg_data_home:
        return xdg_data_home / "murmly"
    return home_directory(env) / ".local" / "share" / "murmly"


def default_tts_model_dir(env: dict[str, str] | None = None) -> Path:
    """Where the synthesis model and its voices are looked for by default."""
    return default_data_dir(env)


def default_config_path(env: dict[str, str] | None = None) -> Path:
    profile = resolve_platform(env)
    if profile.operating_system is OperatingSystem.WINDOWS:
        environment = env if env is not None else os.environ
        app_data = absolute_setting(environment, "APPDATA", windows=True)
        if app_data:
            return app_data / "murmly" / "config.toml"
        return home_directory(env, windows=True) / "AppData" / "Roaming" / "murmly" / "config.toml"
    if profile.operating_system is OperatingSystem.MACOS:
        return home_directory(env) / "Library" / "Application Support" / "murmly" / "config.toml"
    # Linux, and OperatingSystem.OTHER, unchanged.
    environment = env if env is not None else os.environ
    xdg_config_home = absolute_setting(environment, "XDG_CONFIG_HOME")
    if xdg_config_home:
        return xdg_config_home / "murmly" / "config.toml"
    return home_directory(env) / ".config" / "murmly" / "config.toml"


def is_quiet_at(start: time | None, end: time | None, now: time) -> bool:
    """Whether local time `now` falls inside the quiet window `start`-`end`.

    Half-open, so quiet begins at the start and ends at the end: `22:00-07:00`
    refuses at 22:00:00 and accepts at 07:00:00. A start later than its end spans
    midnight, which is the shape almost every night has.

    A pure function of three times, deliberately: it is the whole of what decides
    whether Murmly is quiet, so it can be asked about any hour without a test
    having to wait for one, and no state is kept that could be wrong across a
    suspend, a resume, or a daylight-saving change.
    """
    if start is None or end is None:
        return False
    if start > end:
        return now >= start or now < end
    return start <= now < end


@dataclass(slots=True)
class MurmlyConfig:
    socket_path: Path
    config_path: Path
    model_profile: str = "balanced"
    sample_rate_hz: int = 16_000
    channels: int = 1
    device: str = "auto"
    compute_type: str = "auto"
    beam_size: int = 5
    vad_filter: bool = True
    lazy_load_model: bool = True
    unload_after_idle_s: int = DEFAULT_STT_UNLOAD_AFTER_IDLE_S
    live_transcribe: bool = False
    live_interval_ms: int = DEFAULT_LIVE_INTERVAL_MS
    live_window_seconds: int = DEFAULT_LIVE_WINDOW_SECONDS
    auto_transcribe: str = DEFAULT_AUTO_TRANSCRIBE_MODE
    auto_transcribe_rejected_value: str | None = None
    auto_transcribe_silence_ms: int = DEFAULT_SILENCE_MS
    auto_transcribe_min_speech_ms: int = DEFAULT_MIN_SPEECH_MS
    restore_clipboard: bool = True
    restore_clipboard_delay_ms: int = DEFAULT_RESTORE_DELAY_MS
    verify_target: bool = True
    overlay_enabled: bool = True
    overlay_bottom_margin_px: int = 32
    overlay_reduced_motion: bool = False
    overlay_text_size_px: int = DEFAULT_OVERLAY_TEXT_SIZE_PX
    tts_enabled: bool = False
    tts_voice: str = DEFAULT_TTS_VOICE
    tts_voice_rejected_value: str | None = None
    tts_rate_percent: int = DEFAULT_TTS_RATE_PERCENT
    tts_rate_rejected_value: object | None = None
    tts_device: str = DEFAULT_TTS_DEVICE
    tts_device_rejected_value: str | None = None
    tts_unload_after_idle_s: int = DEFAULT_TTS_UNLOAD_AFTER_IDLE_S
    tts_output_device: str = ""
    tts_model_dir: Path = field(default_factory=default_tts_model_dir)
    tts_quiet_start: time | None = None
    tts_quiet_end: time | None = None
    tts_quiet_rejected_value: str | None = None
    speaker_mode: str = DEFAULT_SPEAKER_MODE
    speaker_mode_rejected_value: str | None = None
    speaker_owner: str = ""
    speaker_match_threshold_percent: int = DEFAULT_SPEAKER_MATCH_THRESHOLD_PERCENT
    speaker_match_threshold_rejected_value: object | None = None

    @property
    def model_name(self) -> str:
        return MODEL_PROFILES.get(self.model_profile, MODEL_PROFILES["balanced"]).model_name

    @property
    def model_revision(self) -> str | None:
        return MODEL_PROFILES.get(
            self.model_profile,
            MODEL_PROFILES["balanced"],
        ).model_revision


def load_config(path: str | Path | None = None, env: dict[str, str] | None = None) -> MurmlyConfig:
    config_path = Path(path) if path else default_config_path(env)
    data: dict[str, object] = {}
    if config_path.exists():
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)

    daemon = _get_table(data, "daemon")
    audio = _get_table(data, "audio")
    stt = _get_table(data, "stt")
    clipboard = _get_table(data, "clipboard")
    overlay = _get_table(data, "overlay")
    tts = _get_table(data, "tts")
    speakers = _get_table(data, "speakers")

    socket_path = Path(str(daemon.get("socket_path", default_socket_path(env))))
    model_profile = str(stt.get("model_profile", "balanced"))
    if model_profile not in MODEL_PROFILES:
        model_profile = "balanced"
    model = MODEL_PROFILES[model_profile]
    device = str(stt.get("device", "auto"))
    if device not in VALID_DEVICES:
        device = "auto"
    compute_type = str(stt.get("compute_type", "auto"))
    if compute_type not in VALID_COMPUTE_TYPES:
        compute_type = "auto"
    beam_size = _bounded_int(stt.get("beam_size"), model.beam_size, minimum=1, maximum=10)

    auto_transcribe = str(stt.get("auto_transcribe", DEFAULT_AUTO_TRANSCRIBE_MODE))
    auto_transcribe_rejected_value: str | None = None
    if auto_transcribe not in VALID_AUTO_TRANSCRIBE_MODES:
        auto_transcribe_rejected_value = auto_transcribe
        auto_transcribe = DEFAULT_AUTO_TRANSCRIBE_MODE

    tts_voice = str(tts.get("voice", DEFAULT_TTS_VOICE))
    tts_voice_rejected_value: str | None = None
    if tts_voice not in VALID_TTS_VOICES:
        tts_voice_rejected_value = tts_voice
        tts_voice = DEFAULT_TTS_VOICE
    tts_rate_percent = _bounded_int(
        tts.get("rate"),
        DEFAULT_TTS_RATE_PERCENT,
        minimum=MIN_TTS_RATE_PERCENT,
        maximum=MAX_TTS_RATE_PERCENT,
    )
    tts_rate_rejected_value = _rejected_value(tts.get("rate"), tts_rate_percent)
    tts_device = str(tts.get("device", DEFAULT_TTS_DEVICE))
    tts_device_rejected_value: str | None = None
    if tts_device not in VALID_DEVICES:
        tts_device_rejected_value = tts_device
        tts_device = DEFAULT_TTS_DEVICE
    tts_quiet_start, tts_quiet_end, tts_quiet_rejected_value = _quiet_window(
        tts.get("quiet_hours")
    )
    speaker_mode = str(speakers.get("mode", DEFAULT_SPEAKER_MODE))
    speaker_mode_rejected_value: str | None = None
    if speaker_mode not in VALID_SPEAKER_MODES:
        speaker_mode_rejected_value = speaker_mode
        speaker_mode = DEFAULT_SPEAKER_MODE
    speaker_match_threshold_percent = _bounded_int(
        speakers.get("match_threshold"),
        DEFAULT_SPEAKER_MATCH_THRESHOLD_PERCENT,
        minimum=MIN_SPEAKER_MATCH_THRESHOLD_PERCENT,
        maximum=MAX_SPEAKER_MATCH_THRESHOLD_PERCENT,
    )
    speaker_match_threshold_rejected_value = _rejected_value(
        speakers.get("match_threshold"),
        speaker_match_threshold_percent,
    )
    model_dir = tts.get("model_dir")
    tts_model_dir = (
        Path(str(model_dir)).expanduser() if model_dir else default_tts_model_dir(env)
    )

    return MurmlyConfig(
        socket_path=socket_path,
        config_path=config_path,
        model_profile=model_profile,
        sample_rate_hz=int(audio.get("sample_rate_hz", 16_000)),
        channels=int(audio.get("channels", 1)),
        device=device,
        compute_type=compute_type,
        beam_size=beam_size,
        vad_filter=bool(stt.get("vad_filter", model.vad_filter)),
        lazy_load_model=bool(stt.get("lazy_load_model", True)),
        unload_after_idle_s=_idle_period(
            stt.get("unload_after_idle_s"),
            DEFAULT_STT_UNLOAD_AFTER_IDLE_S,
        ),
        live_transcribe=_boolean(stt.get("live_transcribe"), False),
        live_interval_ms=_bounded_int(
            stt.get("live_interval_ms"),
            DEFAULT_LIVE_INTERVAL_MS,
            minimum=MIN_LIVE_INTERVAL_MS,
            maximum=MAX_LIVE_INTERVAL_MS,
        ),
        live_window_seconds=_bounded_int(
            stt.get("live_window_seconds"),
            DEFAULT_LIVE_WINDOW_SECONDS,
            minimum=MIN_LIVE_WINDOW_SECONDS,
            maximum=MAX_LIVE_WINDOW_SECONDS,
        ),
        auto_transcribe=auto_transcribe,
        auto_transcribe_rejected_value=auto_transcribe_rejected_value,
        auto_transcribe_silence_ms=_bounded_int(
            stt.get("auto_transcribe_silence_ms"),
            DEFAULT_SILENCE_MS,
            minimum=MIN_SILENCE_MS,
            maximum=MAX_SILENCE_MS,
        ),
        auto_transcribe_min_speech_ms=_bounded_int(
            stt.get("auto_transcribe_min_speech_ms"),
            DEFAULT_MIN_SPEECH_MS,
            minimum=MIN_MIN_SPEECH_MS,
            maximum=MAX_MIN_SPEECH_MS,
        ),
        restore_clipboard=bool(clipboard.get("restore", True)),
        restore_clipboard_delay_ms=_bounded_int(
            clipboard.get("restore_delay_ms"),
            DEFAULT_RESTORE_DELAY_MS,
            minimum=0,
            maximum=MAX_RESTORE_DELAY_MS,
        ),
        verify_target=_boolean(clipboard.get("verify_target"), True),
        overlay_enabled=_boolean(overlay.get("enabled"), True),
        overlay_bottom_margin_px=_bounded_int(
            overlay.get("bottom_margin_px"),
            32,
            minimum=0,
            maximum=512,
        ),
        overlay_reduced_motion=_boolean(overlay.get("reduced_motion"), False),
        overlay_text_size_px=_bounded_int(
            overlay.get("text_size_px"),
            DEFAULT_OVERLAY_TEXT_SIZE_PX,
            minimum=MIN_OVERLAY_TEXT_SIZE_PX,
            maximum=MAX_OVERLAY_TEXT_SIZE_PX,
        ),
        tts_enabled=_boolean(tts.get("enabled"), False),
        tts_voice=tts_voice,
        tts_voice_rejected_value=tts_voice_rejected_value,
        tts_rate_percent=tts_rate_percent,
        tts_rate_rejected_value=tts_rate_rejected_value,
        tts_device=tts_device,
        tts_device_rejected_value=tts_device_rejected_value,
        tts_unload_after_idle_s=_idle_period(
            tts.get("unload_after_idle_s"),
            DEFAULT_TTS_UNLOAD_AFTER_IDLE_S,
        ),
        tts_output_device=str(tts.get("output_device", "")),
        tts_model_dir=tts_model_dir,
        tts_quiet_start=tts_quiet_start,
        tts_quiet_end=tts_quiet_end,
        tts_quiet_rejected_value=tts_quiet_rejected_value,
        speaker_mode=speaker_mode,
        speaker_mode_rejected_value=speaker_mode_rejected_value,
        speaker_owner=str(speakers.get("owner", "")).strip(),
        speaker_match_threshold_percent=speaker_match_threshold_percent,
        speaker_match_threshold_rejected_value=speaker_match_threshold_rejected_value,
    )


def _get_table(data: dict[str, object], key: str) -> dict[str, object]:
    value = data.get(key, {})
    if isinstance(value, dict):
        return value
    return {}


def _parse_bounded_int(value: object, *, minimum: int, maximum: int) -> int | None:
    """The integer `value` reads as when it is within the bounds, else None."""
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if minimum <= parsed <= maximum else None


def _bounded_int(value: object, default: int, *, minimum: int, maximum: int) -> int:
    parsed = _parse_bounded_int(value, minimum=minimum, maximum=maximum)
    return default if parsed is None else parsed


def _idle_period(value: object, default: int) -> int:
    """An idle period in seconds, where zero means never rather than out of range.

    This exists so that it is not `_bounded_int`. That helper answers the
    default for anything outside its bounds, and zero is outside these bounds:
    routing an idle period through it turns a deliberate
    `unload_after_idle_s = 0` -- which switches idle release off -- into the
    default period, switching the feature on for the person who asked for it
    off. The disable knob would enable the feature. So zero is answered before
    the bounds are consulted, and only a value that is neither zero nor within
    the bounds falls back to this setting's own default. `_bounded_int` keeps
    its meaning for its many other callers.
    """
    parsed = _parse_idle_period(value)
    return default if parsed is None else parsed


def _parse_idle_period(value: object) -> int | None:
    """The idle period `value` reads as, or None when it is not one."""
    if value is not None:
        try:
            # Compared, not truncated. `int(value)` reads 0.5 and -0.9 as zero,
            # so a mistyped fractional period switched release off instead of
            # falling back -- and for `[stt]`, whose default is 300, that
            # silently disabled a feature that ships on. Only an exact zero
            # means never; everything else outside the bounds falls through.
            if float(value) == 0:
                return 0
        except (TypeError, ValueError):
            pass
    return _parse_bounded_int(
        value,
        minimum=MIN_UNLOAD_AFTER_IDLE_S,
        maximum=MAX_UNLOAD_AFTER_IDLE_S,
    )


def _rejected_value(value: object, resolved: int) -> object | None:
    """The configured value when it was not the one used, for diagnostics.

    `_bounded_int` falls back silently, which is right for starting up and
    wrong for explaining: a report that cannot name what the user asked for
    leaves them looking at a setting that appears to have been ignored.
    """
    if value is None:
        return None
    try:
        if int(value) == resolved:
            return None
    except (TypeError, ValueError):
        # Stringified because the report is serialised as JSON, and every TOML
        # type json cannot encode -- date, datetime, time -- reaches this branch
        # exactly because `int()` refuses it. Returned raw, one mistyped setting
        # made `murmly doctor` print no report at all.
        return str(value)
    return value


def _quiet_window(value: object) -> tuple[time | None, time | None, str | None]:
    """The window speech is refused in: a start, an end, and what was not honoured.

    Falls back to no window rather than to some other window, which is the point
    of the fallback here. Someone who expects quiet and gets spoken to has a bug
    they can see; someone silenced at hours they never wrote has one they cannot.

    A start equal to its end is read as no window rather than as a whole day.
    Someone who wants Murmly permanently silent has `enabled = false`, and
    reading an equal pair as 24 hours would turn a plausible typo into a daemon
    that never speaks again. It is still returned as a value that was not
    honoured, because a report showing no window against a file that plainly sets
    one leaves its owner with nothing to look at.
    """
    if value is None:
        return None, None, None
    if not isinstance(value, str):
        # Stringified for the reason `_rejected_value` gives: the report is
        # serialised as JSON, and an unquoted `22:00:00` is TOML's own local-time
        # literal, which json cannot encode. An unquoted `22:00-07:00` does not
        # reach here at all -- it is not valid TOML, and the whole file is lost
        # before this function is called, which is why the example must show the
        # quotes.
        return None, None, str(value)
    if not value.strip():
        return None, None, None
    match = QUIET_HOURS_PATTERN.match(value)
    if match is None:
        return None, None, value
    start_hour, start_minute, end_hour, end_minute = (int(part) for part in match.groups())
    try:
        start = time(start_hour, start_minute)
        end = time(end_hour, end_minute)
    except ValueError:
        # An hour past 23 or a minute past 59 matched the shape and is still not
        # a time of day.
        return None, None, value
    if start == end:
        return None, None, value
    return start, end, None


def _boolean(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


@dataclass(frozen=True, slots=True)
class SettingRule:
    """What one configuration key accepts, as a test and as a sentence.

    Built from the constants and helpers `load_config` reads with, so a value
    the writer accepts is one the reader honours and a value the reader would
    set aside is one the writer refuses. The sentence is what a person is told
    when their value is refused.
    """

    expectation: str
    accepts: Callable[[object], bool]


class SettingValueError(ValueError):
    """A value a key does not accept; the message says what it does."""


def _is_whole_number(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _choice_rule(choices: "set[str] | dict[str, object]") -> SettingRule:
    names = sorted(choices)
    return SettingRule(
        f"one of {', '.join(names)}",
        lambda value: isinstance(value, str) and value in choices,
    )


def _range_rule(minimum: int, maximum: int, unit: str = "") -> SettingRule:
    return SettingRule(
        f"a whole number from {minimum} to {maximum}{unit}",
        lambda value: (
            _is_whole_number(value)
            and _parse_bounded_int(value, minimum=minimum, maximum=maximum) is not None
        ),
    )


def _idle_rule() -> SettingRule:
    return SettingRule(
        f"0 (never) or a whole number of seconds from {MIN_UNLOAD_AFTER_IDLE_S} "
        f"to {MAX_UNLOAD_AFTER_IDLE_S}",
        lambda value: _is_whole_number(value) and _parse_idle_period(value) is not None,
    )


def _flag_rule() -> SettingRule:
    return SettingRule("true or false", lambda value: isinstance(value, bool))


def _text_rule(meaning: str) -> SettingRule:
    return SettingRule(meaning, lambda value: isinstance(value, str))


def _quiet_hours_rule() -> SettingRule:
    # Empty is accepted because it is how the window is switched off.
    return SettingRule(
        'a window written as "HH:MM-HH:MM" in local time with different start and '
        'end, for example "22:00-07:00", or empty for none',
        lambda value: isinstance(value, str) and _quiet_window(value)[2] is None,
    )


#: Every key the app may write, by dotted name. Keys `load_config` reads that are
#: not here (`daemon.socket_path`, `audio.*`) have no documented range and are
#: not edited by the app.
SETTING_RULES: dict[str, SettingRule] = {
    "stt.model_profile": _choice_rule(MODEL_PROFILES),
    "stt.device": _choice_rule(VALID_DEVICES),
    "stt.compute_type": _choice_rule(VALID_COMPUTE_TYPES),
    "stt.beam_size": _range_rule(1, 10),
    "stt.vad_filter": _flag_rule(),
    "stt.lazy_load_model": _flag_rule(),
    "stt.unload_after_idle_s": _idle_rule(),
    "stt.live_transcribe": _flag_rule(),
    "stt.live_interval_ms": _range_rule(MIN_LIVE_INTERVAL_MS, MAX_LIVE_INTERVAL_MS, " milliseconds"),
    "stt.live_window_seconds": _range_rule(
        MIN_LIVE_WINDOW_SECONDS, MAX_LIVE_WINDOW_SECONDS, " seconds"
    ),
    "stt.auto_transcribe": _choice_rule(VALID_AUTO_TRANSCRIBE_MODES),
    "stt.auto_transcribe_silence_ms": _range_rule(MIN_SILENCE_MS, MAX_SILENCE_MS, " milliseconds"),
    "stt.auto_transcribe_min_speech_ms": _range_rule(
        MIN_MIN_SPEECH_MS, MAX_MIN_SPEECH_MS, " milliseconds"
    ),
    "clipboard.restore": _flag_rule(),
    "clipboard.restore_delay_ms": _range_rule(0, MAX_RESTORE_DELAY_MS, " milliseconds"),
    "clipboard.verify_target": _flag_rule(),
    "overlay.enabled": _flag_rule(),
    "overlay.bottom_margin_px": _range_rule(0, 512, " pixels"),
    "overlay.reduced_motion": _flag_rule(),
    "overlay.text_size_px": _range_rule(
        MIN_OVERLAY_TEXT_SIZE_PX, MAX_OVERLAY_TEXT_SIZE_PX, " pixels"
    ),
    "tts.enabled": _flag_rule(),
    "tts.voice": _choice_rule(VALID_TTS_VOICES),
    "tts.rate": _range_rule(MIN_TTS_RATE_PERCENT, MAX_TTS_RATE_PERCENT, " percent"),
    "tts.device": _choice_rule(VALID_DEVICES),
    "tts.unload_after_idle_s": _idle_rule(),
    "tts.output_device": _text_rule("text naming an output device, or empty for the default"),
    "tts.model_dir": _text_rule("text giving a directory"),
    "tts.quiet_hours": _quiet_hours_rule(),
    "speakers.mode": _choice_rule(VALID_SPEAKER_MODES),
    "speakers.owner": _text_rule("text naming an enrolled voice, or empty for none"),
    "speakers.match_threshold": _range_rule(
        MIN_SPEAKER_MATCH_THRESHOLD_PERCENT, MAX_SPEAKER_MATCH_THRESHOLD_PERCENT, " percent"
    ),
}


def validate_setting(key: str, value: object) -> None:
    """Raise `SettingValueError` unless `load_config` would honour `value` for `key`."""
    rule = SETTING_RULES.get(key)
    if rule is None:
        raise SettingValueError(f"{key} is not a setting Murmly reads.")
    if not rule.accepts(value):
        raise SettingValueError(f"{key} must be {rule.expectation}, not {value!r}.")
