from __future__ import annotations

import argparse
from array import array
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable

from murmly.audio import SoundDeviceRecorder
from murmly.config import MurmlyConfig, load_config
from murmly.daemon import MurmlyDaemon, send_command
from murmly.hotkey import HotkeyError, parse_hotkey
from murmly.installer import (
    HotkeyNotConfirmedError,
    InstallError,
    Installer,
    UserService,
)
from murmly.integrations import (
    ClipboardPaster,
    MissingToolError,
    choose_clipboard_copy_command,
    choose_paste_command,
    is_wayland_session,
)
from murmly.focus import FocusObserver, create_focus_observer, record_target, should_deliver
from murmly.overlay import SYSTEM_PYTHON, detect_overlay_backend
from murmly.silence import SilenceDetector
from murmly.stt import FasterWhisperTranscriber


RunCommand = Callable[..., subprocess.CompletedProcess[str]]

DAEMON_START_TIMEOUT_SECONDS = 10.0
DAEMON_POLL_INTERVAL_SECONDS = 0.1


class DaemonUnavailableError(RuntimeError):
    """The daemon is not accepting commands and could not be brought up.

    A hotkey has no visible output channel, so this is raised and reported as a
    single line rather than allowed to surface as a traceback.
    """


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local voice-to-text for Fedora-first Linux desktops.")
    parser.add_argument("--config", help="Path to config.toml", default=None)

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("daemon", help="Run the UNIX socket daemon.")
    subparsers.add_parser("toggle", help="Toggle capture via the running daemon.")
    subparsers.add_parser("status", help="Query the daemon state.")

    install = subparsers.add_parser(
        "install",
        help="Install the session service and bind a hotkey.",
    )
    install.add_argument(
        "hotkey",
        help="Hotkey to bind, such as Meta+X. Requires at least one modifier.",
    )
    subparsers.add_parser("uninstall", help="Remove the session service and release the hotkey.")

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
    if args.command in {"toggle", "status"}:
        return _run_client_command(config, args.command)
    if args.command == "install":
        return _run_install(args.hotkey)
    if args.command == "uninstall":
        return _run_uninstall()
    if args.command == "spike":
        return _run_spike(config, args.seconds, args.paste)
    if args.command == "doctor":
        _run_doctor(config)
        return 0
    parser.error(f"Unsupported command: {args.command}")
    return 2


def _run_client_command(config: MurmlyConfig, command: str) -> int:
    try:
        response = send_command_with_recovery(config, command)
    except DaemonUnavailableError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(response, indent=2))
    return 0


def send_command_with_recovery(
    config: MurmlyConfig,
    command: str,
    service: UserService | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout: float = DAEMON_START_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Send a command, starting the installed service once if nothing answers.

    A hotkey press is the main caller and has nowhere to show a traceback, so a
    daemon that is merely not running yet must be recovered rather than raising.
    Exactly one retry is attempted; a daemon that will not come up is reported.
    """
    socket_path = str(config.socket_path)
    try:
        return send_command(socket_path, command)
    except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError, socket.timeout, OSError):
        pass

    controller = service if service is not None else UserService()
    try:
        installed = controller.is_installed
    except InstallError as error:
        raise DaemonUnavailableError(f"Unable to check the Murmly service: {error}") from error

    if not installed:
        raise DaemonUnavailableError(
            "The Murmly daemon is not running and no service is installed. "
            "Run 'murmly install <hotkey>' to install it, for example: murmly install Meta+X"
        )

    try:
        controller.start()
    except InstallError as error:
        raise DaemonUnavailableError(f"Unable to start the Murmly service: {error}") from error

    # Wait for the daemon to publish its socket, then retry the command exactly
    # once. Polling the socket rather than the command keeps a daemon that comes
    # up broken from being hammered.
    if not _wait_for_socket(config.socket_path, sleep, clock, timeout):
        raise DaemonUnavailableError(
            f"The Murmly daemon did not start within {timeout:g} seconds. "
            "Check 'systemctl --user status murmly.service' and "
            "'journalctl --user -u murmly.service -b'."
        )

    try:
        return send_command(socket_path, command)
    except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError, socket.timeout, OSError) as error:
        raise DaemonUnavailableError(
            f"The Murmly daemon started but did not accept the '{command}' command: {error}"
        ) from error


def _wait_for_socket(
    socket_path: Path,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    timeout: float,
) -> bool:
    deadline = clock() + timeout
    while True:
        if socket_path.exists():
            return True
        if clock() >= deadline:
            return False
        sleep(DAEMON_POLL_INTERVAL_SECONDS)


def _run_install(hotkey_text: str) -> int:
    try:
        hotkey = parse_hotkey(hotkey_text)
    except HotkeyError as error:
        print(str(error), file=sys.stderr)
        return 2

    try:
        outcome = Installer().install(hotkey)
    except HotkeyNotConfirmedError as error:
        print(str(error), file=sys.stderr)
        print(
            f"{hotkey.portable} is not active in this session. The binding is saved and will "
            "take effect at your next login.",
            file=sys.stderr,
        )
        return 1
    except InstallError as error:
        print(str(error), file=sys.stderr)
        return 1

    for message in outcome.messages:
        print(message)
    return 0


def _run_uninstall() -> int:
    try:
        outcome = Installer().uninstall()
    except InstallError as error:
        print(str(error), file=sys.stderr)
        return 1

    for message in outcome.messages:
        print(message)
    return 0


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
    observer = create_focus_observer()
    clip = recorder.record_for_seconds(seconds)
    target = record_target(observer)
    text = transcriber.transcribe_pcm16(clip, recorder.sample_rate_hz)
    if not text:
        return 0
    print(text)
    allowed, reason = should_deliver(observer, target, config.verify_target) if paste else (False, None)
    paster = ClipboardPaster(
        restore_clipboard=config.restore_clipboard and allowed,
        restore_delay_ms=config.restore_clipboard_delay_ms,
    )
    if allowed:
        paster.copy_and_paste(text)
        return 0
    paster.copy(text)
    if paste:
        print(f"Transcript copied to the clipboard but not pasted: {reason}.", file=sys.stderr)
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
                "live_transcription": live_transcription_diagnostics(config),
                "delivery": delivery_diagnostics(config),
                "overlay": overlay,
                "installation": installation_diagnostics(),
            },
            indent=2,
        )
    )


def live_transcription_diagnostics(
    config: MurmlyConfig,
    transcriber: FasterWhisperTranscriber | None = None,
) -> dict[str, object]:
    """Live transcription and auto-transcribe state for `murmly doctor`.

    The silence check uses the configured capture rate. The rate a session
    actually negotiates can differ, so the daemon repeats this check when capture
    starts and disables auto-transcribe there if the negotiated rate cannot work.
    """
    report: dict[str, object] = {
        "live_transcribe": config.live_transcribe,
        "live_interval_ms": config.live_interval_ms,
        "live_window_seconds": config.live_window_seconds,
        "auto_transcribe": config.auto_transcribe,
        "auto_transcribe_silence_ms": config.auto_transcribe_silence_ms,
        "auto_transcribe_min_speech_ms": config.auto_transcribe_min_speech_ms,
        "overlay_text_size_px": config.overlay_text_size_px,
        "configured_sample_rate_hz": config.sample_rate_hz,
    }
    if config.auto_transcribe_rejected_value is not None:
        report["auto_transcribe_rejected_value"] = config.auto_transcribe_rejected_value

    try:
        detector = SilenceDetector(
            config.sample_rate_hz,
            config.channels,
            silence_ms=config.auto_transcribe_silence_ms,
            min_speech_ms=config.auto_transcribe_min_speech_ms,
        )
        report["silence_detection_available"] = detector.available
        if not detector.available:
            report["silence_detection_detail"] = detector.unavailable_reason
    except Exception as error:  # noqa: BLE001 - diagnostics must not raise
        report["silence_detection_available"] = False
        report["silence_detection_detail"] = f"Unable to check silence detection: {error}"

    report["partial_pass_ceiling_ms"] = None
    if config.live_transcribe:
        measured, detail = measure_partial_pass_ms(config, transcriber)
        report["partial_pass_ceiling_ms"] = measured
        if detail is not None:
            report["partial_pass_detail"] = detail
        elif measured is not None:
            report["partial_pass_keeps_pace"] = measured <= config.live_interval_ms
            report["partial_pass_detail"] = (
                "Worst case: a full window of speech. Real partials are usually faster "
                "because the voice activity filter trims silence."
            )
    else:
        report["partial_pass_detail"] = "Live transcription is disabled."
    return report


def measure_partial_pass_ms(
    config: MurmlyConfig,
    transcriber: FasterWhisperTranscriber | None = None,
) -> tuple[int | None, str | None]:
    """Time the slowest partial pass this configuration can produce.

    Measures a full window decoded end to end, which is the case that decides
    whether partials keep pace. The voice activity filter is disabled for the
    measurement on purpose: with it on, synthetic audio is discarded as non-speech
    and the pass would time almost no decoding at all.

    Only runs when live transcription is enabled, because it loads the model.
    """
    try:
        if transcriber is None:
            transcriber = FasterWhisperTranscriber(replace(config, vad_filter=False))
        transcriber.begin_capture()
        clip = _measurement_clip(config.sample_rate_hz, config.live_window_seconds)
        # Discard one pass first. The daemon holds the model in memory for the
        # whole session, so steady-state latency is what decides whether partials
        # keep pace; a cold pass would report the one-time load instead.
        transcriber.transcribe_partial(clip, config.sample_rate_hz)
        started = time.perf_counter()
        transcriber.transcribe_partial(clip, config.sample_rate_hz)
        elapsed_ms = round((time.perf_counter() - started) * 1_000)
        if not transcriber.partials_available:
            return None, "A partial pass failed during measurement."
        return elapsed_ms, None
    except Exception as error:  # noqa: BLE001 - diagnostics must not raise
        return None, f"Unable to measure a partial pass: {error}"


def _measurement_clip(sample_rate_hz: int, window_seconds: int) -> bytes:
    """A quiet, deterministic clip that is not digital silence.

    Digital silence short-circuits before the model runs, which would measure
    nothing.
    """
    sample_count = sample_rate_hz * window_seconds
    samples = array("h", (int(220 * math.sin(index / 37.0)) for index in range(sample_count)))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def installation_diagnostics(installer: Installer | None = None) -> dict[str, object]:
    """Installation state for `murmly doctor`.

    Diagnostics must never fail, so an unreachable desktop or service manager is
    reported rather than raised.
    """
    try:
        return (installer if installer is not None else Installer()).status()
    except Exception as error:  # noqa: BLE001 - diagnostics must not raise
        return {
            "installed": False,
            "service_active": False,
            "entrypoint": None,
            "hotkey": None,
            "hotkey_held": False,
            "detail": f"Unable to determine installation state: {error}",
        }


def delivery_diagnostics(
    config: MurmlyConfig,
    env: dict[str, str] | None = None,
    observer: FocusObserver | None = None,
) -> dict[str, object]:
    focus = observer if observer is not None else create_focus_observer(env)
    report: dict[str, object] = {
        "verification_supported": focus.supported,
        "verification_enabled": config.verify_target,
        "restore_clipboard": config.restore_clipboard,
        "restore_delay_ms": config.restore_clipboard_delay_ms,
    }
    if not focus.supported:
        report["detail"] = focus.detail or "Delivery target verification is unavailable in this session."
    elif not config.verify_target:
        report["detail"] = "Delivery target verification is supported but disabled in configuration."
    else:
        report["detail"] = "Delivery target verification is supported and enabled."
    return report


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
