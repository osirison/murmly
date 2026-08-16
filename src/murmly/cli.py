from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
from collections.abc import Callable

from murmly.audio import SoundDeviceRecorder
from murmly.config import MurmlyConfig, load_config
from murmly.daemon import MurmlyDaemon, send_command
from murmly.integrations import (
    ClipboardPaster,
    MissingToolError,
    choose_clipboard_copy_command,
    choose_paste_command,
    is_wayland_session,
)
from murmly.overlay import SYSTEM_PYTHON, detect_overlay_backend
from murmly.stt import FasterWhisperTranscriber


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local voice-to-text for Fedora-first Linux desktops.")
    parser.add_argument("--config", help="Path to config.toml", default=None)

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("daemon", help="Run the UNIX socket daemon.")
    subparsers.add_parser("toggle", help="Toggle capture via the running daemon.")
    subparsers.add_parser("status", help="Query the daemon state.")

    spike = subparsers.add_parser("spike", help="Record a short clip, transcribe it, print it, and copy it.")
    spike.add_argument("--seconds", type=float, default=5.0, help="How long to record before transcribing.")
    spike.add_argument("--paste", action="store_true", help="Also paste the transcription after copying it.")

    subparsers.add_parser("doctor", help="Show detected session and integration commands.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)

    if args.command == "daemon":
        return _run_daemon(config)
    if args.command == "toggle":
        print(json.dumps(send_command(str(config.socket_path), "toggle"), indent=2))
        return 0
    if args.command == "status":
        print(json.dumps(send_command(str(config.socket_path), "status"), indent=2))
        return 0
    if args.command == "spike":
        return _run_spike(config, args.seconds, args.paste)
    if args.command == "doctor":
        _run_doctor(config)
        return 0
    parser.error(f"Unsupported command: {args.command}")
    return 2


def _run_daemon(config: MurmlyConfig) -> int:
    daemon = MurmlyDaemon(config)

    def request_shutdown(_signal_number: int, _frame: object) -> None:
        daemon.shutdown()

    previous_handlers = {
        signal_number: signal.signal(signal_number, request_shutdown)
        for signal_number in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        daemon.serve_forever()
    finally:
        for signal_number, handler in previous_handlers.items():
            signal.signal(signal_number, handler)
    return 0


def _run_spike(config: MurmlyConfig, seconds: float, paste: bool) -> int:
    recorder = SoundDeviceRecorder(config)
    transcriber = FasterWhisperTranscriber(config)
    clip = recorder.record_for_seconds(seconds)
    text = transcriber.transcribe_pcm16(clip, recorder.sample_rate_hz)
    if text:
        print(text)
        paster = ClipboardPaster(
            restore_clipboard=config.restore_clipboard and paste,
            restore_delay_ms=config.restore_clipboard_delay_ms,
        )
        if paste:
            paster.copy_and_paste(text)
        else:
            paster.copy(text)
    return 0


def _run_doctor(config: MurmlyConfig) -> None:
    try:
        clipboard_command: list[str] | str = choose_clipboard_copy_command()
    except MissingToolError as error:
        clipboard_command = f"unavailable: {error}"

    try:
        paste_command: list[str] | str = choose_paste_command()
    except MissingToolError as error:
        paste_command = f"unavailable: {error}"

    runtime_device, runtime_compute_type = FasterWhisperTranscriber.resolve_runtime(config)
    overlay = overlay_diagnostics(config)

    print(
        json.dumps(
            {
                "config_path": str(config.config_path),
                "socket_path": str(config.socket_path),
                "session": "wayland" if is_wayland_session() else "x11",
                "clipboard_command": clipboard_command,
                "paste_command": paste_command,
                "model_profile": config.model_profile,
                "model_name": config.model_name,
                "device": config.device,
                "compute_type": config.compute_type,
                "runtime_device": runtime_device,
                "runtime_compute_type": runtime_compute_type,
                "beam_size": config.beam_size,
                "vad_filter": config.vad_filter,
                "overlay": overlay,
            },
            indent=2,
        )
    )


def overlay_diagnostics(
    config: MurmlyConfig,
    env: dict[str, str] | None = None,
    run_command: RunCommand = subprocess.run,
    helper_path: Path | None = None,
) -> dict[str, object]:
    environment = env if env is not None else os.environ
    desktop = environment.get("XDG_CURRENT_DESKTOP") or environment.get("XDG_SESSION_DESKTOP", "")
    session = "wayland" if is_wayland_session(environment) else "x11"
    backend = detect_overlay_backend(environment)
    supported_session = backend is not None
    renderer_path = (helper_path or Path(__file__).with_name("overlay_renderer.py")).resolve()
    report: dict[str, object] = {
        "enabled": config.overlay_enabled,
        "desktop": desktop or "unknown",
        "session": session,
        "backend": backend.value if backend is not None else None,
        "supported_session": supported_session,
        "system_python": str(SYSTEM_PYTHON),
        "pygobject": False,
        "gtk4": None,
        "gdk_x11": False,
        "native_x11": False,
        "gtk4_layer_shell": False,
        "available": False,
    }
    if backend is None:
        if not config.overlay_enabled:
            report["detail"] = "Overlay is disabled in configuration."
        else:
            report["detail"] = "Overlay requires KDE Plasma on X11 or Wayland."
        return report
    try:
        result = run_command(
            [str(SYSTEM_PYTHON), str(renderer_path), "--check", "--backend", backend.value],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        helper_report = json.loads(result.stdout)
        if not isinstance(helper_report, dict):
            raise ValueError("Overlay helper returned a non-object report.")
        for key in (
            "system_python",
            "pygobject",
            "gtk4",
            "gdk_x11",
            "native_x11",
            "gtk4_layer_shell",
        ):
            if key in helper_report:
                report[key] = helper_report[key]
        runtime_available = bool(helper_report.get("available")) and result.returncode == 0
        report["available"] = config.overlay_enabled and supported_session and runtime_available
        if not config.overlay_enabled:
            report["detail"] = "Overlay is disabled in configuration."
        elif not runtime_available:
            report["detail"] = str(helper_report.get("error") or result.stderr.strip() or "Visual runtime is unavailable.")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError) as error:
        report["detail"] = f"Overlay helper check failed: {error}"
    return report
