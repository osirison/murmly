from __future__ import annotations

from dataclasses import dataclass
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


def default_runtime_dir(env: dict[str, str] | None = None) -> Path:
    environment = env or os.environ
    runtime_dir = environment.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir)
    return Path(f"/run/user/{os.getuid()}")


def default_socket_path(env: dict[str, str] | None = None) -> Path:
    return default_runtime_dir(env) / "murmly.sock"


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
    restore_clipboard: bool = True
    restore_clipboard_delay_ms: int = 200
    overlay_enabled: bool = True
    overlay_bottom_margin_px: int = 32
    overlay_reduced_motion: bool = False

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
        restore_clipboard=bool(clipboard.get("restore", True)),
        restore_clipboard_delay_ms=int(clipboard.get("restore_delay_ms", 200)),
        overlay_enabled=_boolean(overlay.get("enabled"), True),
        overlay_bottom_margin_px=_bounded_int(
            overlay.get("bottom_margin_px"),
            32,
            minimum=0,
            maximum=512,
        ),
        overlay_reduced_motion=_boolean(overlay.get("reduced_motion"), False),
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


def _boolean(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default
