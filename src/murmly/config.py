from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import tomllib


@dataclass(frozen=True, slots=True)
class ModelProfile:
    model_name: str
    beam_size: int
    vad_filter: bool


MODEL_PROFILES = {
    "fast": ModelProfile("tiny.en", beam_size=1, vad_filter=False),
    "balanced": ModelProfile("large-v3-turbo", beam_size=5, vad_filter=True),
    "accurate": ModelProfile("large-v3", beam_size=5, vad_filter=True),
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

    @property
    def model_name(self) -> str:
        return MODEL_PROFILES.get(self.model_profile, MODEL_PROFILES["balanced"]).model_name


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

    socket_path = Path(str(daemon.get("socket_path", default_socket_path(env))))
    model_profile = str(stt.get("model_profile", "balanced"))
    if model_profile not in MODEL_PROFILES:
        model_profile = "balanced"
    model = MODEL_PROFILES[model_profile]

    return MurmlyConfig(
        socket_path=socket_path,
        config_path=config_path,
        model_profile=model_profile,
        sample_rate_hz=int(audio.get("sample_rate_hz", 16_000)),
        channels=int(audio.get("channels", 1)),
        device=str(stt.get("device", "auto")),
        compute_type=str(stt.get("compute_type", "auto")),
        beam_size=int(stt.get("beam_size", model.beam_size)),
        vad_filter=bool(stt.get("vad_filter", model.vad_filter)),
        lazy_load_model=bool(stt.get("lazy_load_model", True)),
        restore_clipboard=bool(clipboard.get("restore", True)),
        restore_clipboard_delay_ms=int(clipboard.get("restore_delay_ms", 200)),
    )


def _get_table(data: dict[str, object], key: str) -> dict[str, object]:
    value = data.get(key, {})
    if isinstance(value, dict):
        return value
    return {}
