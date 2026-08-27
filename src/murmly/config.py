from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
import tomllib


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


def default_runtime_dir(env: dict[str, str] | None = None) -> Path:
    environment = env or os.environ
    runtime_dir = environment.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir)
    return Path(f"/run/user/{os.getuid()}")


def default_socket_path(env: dict[str, str] | None = None) -> Path:
    return default_runtime_dir(env) / "murmly.sock"


def default_tts_model_dir(env: dict[str, str] | None = None) -> Path:
    """Where the synthesis model and its voices are looked for by default."""
    environment = env or os.environ
    xdg_data_home = environment.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / "murmly"
    return Path.home() / ".local" / "share" / "murmly"


def default_config_path(env: dict[str, str] | None = None) -> Path:
    environment = env or os.environ
    xdg_config_home = environment.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home) / "murmly" / "config.toml"
    return Path.home() / ".config" / "murmly" / "config.toml"


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
    )


def _get_table(data: dict[str, object], key: str) -> dict[str, object]:
    value = data.get(key, {})
    if isinstance(value, dict):
        return value
    return {}


def _bounded_int(value: object, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return parsed if minimum <= parsed <= maximum else default


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
    return _bounded_int(
        value,
        default,
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


def _boolean(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default
