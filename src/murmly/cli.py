from __future__ import annotations

import argparse
import json

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
from murmly.stt import FasterWhisperTranscriber


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
        MurmlyDaemon(config).serve_forever()
        return 0
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
            },
            indent=2,
        )
    )
