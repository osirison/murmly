from __future__ import annotations

import argparse
from array import array
from dataclasses import replace
import json
import logging
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import traceback
from collections.abc import Callable

from murmly.audio import (
    SoundDeviceRecorder,
    SoundDevicePlayer,
    disable_portaudio_exit_teardown,
)
from murmly.config import MurmlyConfig, default_config_path, load_config
from murmly.daemon import (
    DaemonNotRespondingError,
    DaemonStartupError,
    MurmlyDaemon,
    peer_identity_supported,
    send_command,
    socket_path_detail,
)
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
    PasteInjection,
    choose_clipboard_copy_command,
    is_wayland_session,
    select_paste_injection,
)
from murmly.focus import FocusObserver, create_focus_observer, record_target, should_deliver
from murmly.overlay import SYSTEM_PYTHON, detect_overlay_backend, renderer_environment
from murmly.silence import SilenceDetector
from murmly.stt import FasterWhisperTranscriber
from murmly.tts import KokoroSynthesizer, resolve_providers


RunCommand = Callable[..., subprocess.CompletedProcess[str]]

DAEMON_START_TIMEOUT_SECONDS = 10.0
DAEMON_POLL_INTERVAL_SECONDS = 0.1

#: Subcommands whose daemon-side name differs from the one argparse takes.
DAEMON_COMMANDS = {"toggle-session": "toggle_session"}


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
    subparsers.add_parser(
        "toggle-session",
        help="Toggle capture, delivering the transcript to the open speech session.",
    )
    subparsers.add_parser("status", help="Query the daemon state.")

    install = subparsers.add_parser(
        "install",
        help="Install the session service and bind a hotkey.",
    )
    install.add_argument(
        "hotkey",
        help="Hotkey to bind, such as Meta+X. Requires at least one modifier.",
    )
    install.add_argument(
        "session_hotkey",
        nargs="?",
        default=None,
        help=(
            "Optional second hotkey, such as Meta+A, that dictates into the open "
            "speech session instead of the focused window. Omitting it binds the "
            "focused-window hotkey alone."
        ),
    )
    subparsers.add_parser("uninstall", help="Remove the session service and release the hotkeys.")

    spike = subparsers.add_parser("spike", help="Record a short clip, transcribe it, print it, and copy it.")
    spike.add_argument("--seconds", type=float, default=5.0, help="How long to record before transcribing.")
    spike.add_argument("--paste", action="store_true", help="Also paste the transcription after copying it.")

    subparsers.add_parser("doctor", help="Show detected session and integration commands.")
    return parser


def leave_without_finalizing(exit_code: int) -> None:
    """End the daemon process without running interpreter finalization.

    Murmly leaves PortAudio's exit-time teardown unregistered, because that
    teardown aborts when the audio server has already stopped -- which a logout
    is. That teardown was also the only thing that stopped PortAudio's own
    `pw-PortAudio` loop threads, and nothing else calls `Pa_Terminate`. So the
    daemon reaches the end of its run with those threads still executing.

    `Py_Finalize` then clears module dictionaries, which unloads the extension
    libraries underneath them, and the threads fault on the next call into
    memory that has just been unmapped. `murmly daemon` dumped core exactly that
    way on 2026-08-22, and systemd recorded the unit as failed -- the state the
    teardown was unregistered to avoid in the first place.

    Reproduced outside Murmly: real PortAudio plus a loaded `onnxruntime` plus
    the teardown unregistered exits 139; the same run leaving here exits 0.
    Whether streams were ever opened makes no difference.

    Skipping finalization means skipping the flushing it would have done, so
    everything whose loss would be observable is flushed first. Sockets, file
    descriptors and the model memory are reclaimed by the kernel and need
    nothing.

    Patched by the tests: performing this in-process would take the test runner
    down with it, so the decision to call it is what gets asserted.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    logging.shutdown()
    os._exit(exit_code)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        exit_code = _dispatch(parser, args)
    except Exception as error:  # noqa: BLE001 - no command terminates with an unhandled error
        # A backstop only. Every failure with something specific to say -- the
        # daemon's startup refusal, an unreadable configuration, a daemon that
        # did not respond -- reports it itself, because a generic message names
        # the wrong thing.
        if args.command == "daemon":
            # The daemon runs unattended under the service manager, where this
            # one line would be all that survived of a crash nothing anticipated.
            # A person reading the journal needs the frames.
            traceback.print_exc(file=sys.stderr)
        print(f"murmly: unexpected failure: {error}", file=sys.stderr)
        exit_code = 1
    if args.command == "daemon":
        # Every route out of the daemon reaches here with its status already
        # decided: a clean stop, a startup refusal, and the backstop above.
        leave_without_finalizing(exit_code)
    return exit_code


def _dispatch(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    config_path = Path(args.config) if args.config else default_config_path()
    try:
        config = load_config(args.config)
    except Exception as error:  # noqa: BLE001 - the file is named rather than raised
        print(f"Unable to read the configuration at {config_path}: {error}", file=sys.stderr)
        return 1

    if args.command == "daemon":
        return _run_daemon(config)
    if args.command in {"toggle", "status", "toggle-session"}:
        # The daemon's command vocabulary is not the CLI's: argparse spells a
        # subcommand with a hyphen and the wire protocol does not.
        return _run_client_command(config, DAEMON_COMMANDS.get(args.command, args.command))
    if args.command == "install":
        return _run_install(args.hotkey, args.session_hotkey)
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
    except (DaemonUnavailableError, DaemonNotRespondingError) as error:
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
    except DaemonNotRespondingError as error:
        # Something is listening, so starting the service would answer a question
        # the caller did not ask. Reported instead.
        raise DaemonUnavailableError(str(error)) from error
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
    except DaemonNotRespondingError as error:
        raise DaemonUnavailableError(str(error)) from error
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


def _run_install(hotkey_text: str, session_hotkey_text: str | None = None) -> int:
    try:
        hotkey = parse_hotkey(hotkey_text)
        session_hotkey = parse_hotkey(session_hotkey_text) if session_hotkey_text else None
    except HotkeyError as error:
        print(str(error), file=sys.stderr)
        return 2

    try:
        outcome = Installer().install(hotkey, session_hotkey)
    except HotkeyNotConfirmedError as error:
        print(str(error), file=sys.stderr)
        # Only the keys that were not confirmed. An install of two hotkeys can
        # fail on either, and the other one is bound and working right now.
        unconfirmed = ", ".join(key.portable for key in error.hotkeys) or ", ".join(
            key.portable for key in (hotkey, session_hotkey) if key is not None
        )
        print(
            f"{unconfirmed} is not active in this session. The binding is saved and will "
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
    # Wrapped rather than placed in the inner unwinding below, because every
    # return from here is the daemon process on its way out: a clean stop, a
    # startup refusal, and an exception climbing to the top all reach the exit
    # where PortAudio's teardown would otherwise run.
    try:
        return _serve_daemon(config)
    finally:
        disable_portaudio_exit_teardown()


def _serve_daemon(config: MurmlyConfig) -> int:
    try:
        daemon = MurmlyDaemon(config)
    except DaemonStartupError as error:
        print(str(error), file=sys.stderr)
        return 1

    def request_shutdown(_signal_number: int, _frame: object) -> None:
        daemon.shutdown()

    previous_handlers = {
        signal_number: signal.signal(signal_number, request_shutdown)
        for signal_number in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        daemon.serve_forever()
    except DaemonStartupError as error:
        print(str(error), file=sys.stderr)
        return 1
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
        outcome = paster.copy_and_paste(text)
        if outcome.injected:
            return 0
        reason = outcome.reason
    else:
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
        injection_report = paste_injection_diagnostics(select_paste_injection())
    except Exception as error:  # noqa: BLE001 - diagnostics must not raise
        injection_report = {
            "available": False,
            "method": None,
            "reason": f"Unable to choose a paste method: {error}",
            "remedy": [],
        }

    # Guarded like every other probe: this is the exact misconfiguration the
    # command exists to explain, and a report that stops here withholds it.
    runtime_detail: str | None = None
    try:
        runtime_device, runtime_compute_type = FasterWhisperTranscriber.resolve_runtime(config)
    except Exception as error:  # noqa: BLE001 - diagnostics must not raise
        # Reported in its own field rather than substituted into the two runtime
        # values, so a program reading them never has to tell a device name from
        # an error message.
        runtime_device, runtime_compute_type = None, None
        runtime_detail = f"Unable to determine the transcription runtime: {error}"

    session_detail: str | None = None
    try:
        session: str | None = "wayland" if is_wayland_session() else "x11"
    except Exception as error:  # noqa: BLE001 - diagnostics must not raise
        session = None
        session_detail = f"Unable to determine the session type: {error}"

    try:
        overlay = overlay_diagnostics(config)
    except Exception as error:  # noqa: BLE001 - diagnostics must not raise
        overlay = {"available": False, "detail": f"Unable to check the overlay: {error}"}

    # Guarded on its own, like every other probe: a speech stack that cannot be
    # inspected must not take the rest of the report with it.
    try:
        speech = speech_output_diagnostics(config)
    except Exception as error:  # noqa: BLE001 - diagnostics must not raise
        speech = {
            "enabled": config.tts_enabled,
            "available": False,
            "detail": f"Unable to check speech output: {error}",
        }

    report: dict[str, object] = {
        "config_path": str(config.config_path),
        "socket_path": str(config.socket_path),
        "command_socket": command_socket_diagnostics(config),
        "session": session,
        "clipboard_command": clipboard_command,
        "paste_injection": injection_report,
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
        "speech_output": speech,
        "installation": installation_diagnostics(),
    }
    if session_detail is not None:
        report["session_detail"] = session_detail
    if runtime_detail is not None:
        report["runtime_detail"] = runtime_detail
    print(json.dumps(report, indent=2))


def command_socket_diagnostics(config: MurmlyConfig) -> dict[str, object]:
    """Who can reach the command socket, for `murmly doctor`.

    Reported, never refused. Only the daemon refuses to start on a path another
    account can write, because every command loads this configuration and the
    command that exists to explain the condition must keep running.
    """
    detail = socket_path_detail(config.socket_path)
    report: dict[str, object] = {
        "path": str(config.socket_path),
        "path_private": detail is None,
        "peer_identity_supported": peer_identity_supported(),
    }
    if detail is not None:
        report["detail"] = detail
    if not report["peer_identity_supported"]:
        report["peer_identity_detail"] = (
            "This platform cannot report the account behind a connection. The "
            "command socket is protected by its file permissions alone."
        )
    return report


def speech_output_diagnostics(
    config: MurmlyConfig,
    synthesizer: KokoroSynthesizer | None = None,
) -> dict[str, object]:
    """What `murmly doctor` says about speech output.

    Reports the settings in use alongside any configured value that was not
    honoured, because a setting that silently falls back looks to its owner like
    one that was ignored. Never loads the model: the probe checks that every
    part is present, which is what decides whether a session can be opened.
    """
    report: dict[str, object] = {
        "enabled": config.tts_enabled,
        "voice": config.tts_voice,
        "rate_percent": config.tts_rate_percent,
        "model_dir": str(config.tts_model_dir),
        "output_device": config.tts_output_device or None,
    }
    if config.tts_voice_rejected_value is not None:
        report["voice_rejected_value"] = config.tts_voice_rejected_value
    if config.tts_rate_rejected_value is not None:
        report["rate_rejected_value"] = config.tts_rate_rejected_value

    if not config.tts_enabled:
        report["available"] = False
        report["detail"] = "Speech output is disabled. Set enabled = true under [tts]."
        return report

    probe = synthesizer if synthesizer is not None else KokoroSynthesizer(config)
    report["available"] = probe.available
    if not probe.available:
        # The reason is written as the remedy: what to install, or what to place
        # where, so the report is the whole answer rather than the start of one.
        report["detail"] = probe.unavailable_reason
        return report

    try:
        report["providers"] = resolve_providers(config)
    except Exception as error:  # noqa: BLE001 - diagnostics must not raise
        report["providers"] = None
        report["provider_detail"] = str(error)

    rate, device_name, device_detail, probe_detail = negotiated_output(config)
    report["negotiated_output_rate_hz"] = rate
    report["output_device_in_use"] = device_name
    if device_detail is not None:
        report["output_device_detail"] = device_detail
    if probe_detail is not None:
        # A device that will not open means no speech at all: the daemon refuses
        # every session with this same reason. Reporting availability from the
        # probe alone would say speech works while every session is refused.
        report["available"] = False
        report["detail"] = probe_detail
    return report


def negotiated_output(
    config: MurmlyConfig,
) -> tuple[int | None, str | None, str | None, str | None]:
    """Open the output device briefly to learn what a session would get.

    The same reason the capture side is probed rather than reported from
    configuration: the device negotiates the rate, and the configured output
    device may not be the one that opens.
    """
    player = SoundDevicePlayer(config)
    try:
        player.start()
    except Exception as error:  # noqa: BLE001 - diagnostics must not raise
        return None, None, None, f"No output device could be opened: {error}"
    try:
        return player.sample_rate_hz, player.output_device, player.device_detail, None
    finally:
        try:
            player.stop()
        except Exception:  # noqa: BLE001 - diagnostics must not raise
            pass


def paste_injection_diagnostics(injection: PasteInjection) -> dict[str, object]:
    """What `murmly doctor` says about pasting: the method, or the way to get one."""
    report: dict[str, object] = {
        "available": injection.available,
        "method": injection.method,
    }
    if injection.available:
        report["command"] = list(injection.command or ())
        report["confirms_delivery"] = injection.confirms_delivery
        if injection.advisory:
            report["advisory"] = injection.advisory
        return report
    report["reason"] = injection.reason
    report["remedy"] = list(injection.remedy)
    return report


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

    if config.auto_transcribe == "off":
        report["silence_detection_detail"] = "Auto-transcribe is disabled."
    else:
        # Checked against the rate a session actually negotiates, not the
        # configured one: a device that refuses 16 kHz silently disables
        # auto-transcribe, and reporting the configured rate would hide that.
        rate, rate_detail = negotiated_capture_rate(config)
        report["negotiated_sample_rate_hz"] = rate
        try:
            detector = SilenceDetector(
                rate if rate is not None else config.sample_rate_hz,
                config.channels,
                silence_ms=config.auto_transcribe_silence_ms,
                min_speech_ms=config.auto_transcribe_min_speech_ms,
            )
            report["silence_detection_available"] = detector.available
            if not detector.available:
                report["silence_detection_detail"] = detector.unavailable_reason
            elif rate_detail is not None:
                report["silence_detection_detail"] = rate_detail
        except Exception as error:  # noqa: BLE001 - diagnostics must not raise
            report["silence_detection_available"] = False
            report["silence_detection_detail"] = f"Unable to check silence detection: {error}"

    report["partial_pass_ceiling_ms"] = None
    if config.live_transcribe:
        # At the negotiated rate, not the configured one: a session that lands on
        # 48 kHz takes the slower temp-WAV route, and measuring at 16 kHz would
        # report the in-memory path's ceiling for a setup that never uses it.
        measured_rate = report.get("negotiated_sample_rate_hz")
        if measured_rate is None:
            measured_rate, _detail = negotiated_capture_rate(config)
        measurement_config = (
            config if measured_rate is None else replace(config, sample_rate_hz=measured_rate)
        )
        measured, detail = measure_partial_pass_ms(measurement_config, transcriber)
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


def negotiated_capture_rate(config: MurmlyConfig) -> tuple[int | None, str | None]:
    """Open the capture device briefly to learn the rate a session would get.

    `murmly doctor` must not report on the configured rate alone: the recorder
    falls back to a device's native rate when 16 kHz is refused, and that is
    exactly the case that disables auto-transcribe.
    """
    recorder = SoundDeviceRecorder(config)
    try:
        recorder.start()
    except Exception as error:  # noqa: BLE001 - diagnostics must not raise
        return None, f"Unable to open the capture device: {error}"
    try:
        return recorder.sample_rate_hz, None
    finally:
        try:
            recorder.stop()
        except Exception:  # noqa: BLE001 - diagnostics must not raise
            pass


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
        # vad_filter is disabled for the measurement whoever supplies the
        # transcriber: with it on, the synthetic clip is discarded as non-speech
        # and the pass would time no decoding at all.
        if transcriber is None:
            transcriber = FasterWhisperTranscriber(replace(config, vad_filter=False))
        elif getattr(transcriber, "vad_filter", False):
            return None, (
                "Refusing to measure with the voice activity filter enabled: "
                "it discards the synthetic clip and would time no decoding."
            )
        transcriber.begin_capture()
        clip = _measurement_clip(config.sample_rate_hz, config.live_window_seconds, config.channels)
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


def _measurement_clip(sample_rate_hz: int, window_seconds: int, channels: int = 1) -> bytes:
    """A quiet, deterministic clip that is not digital silence.

    Digital silence short-circuits before the model runs, which would measure
    nothing.
    """
    # Frames, not samples: a stereo configuration would otherwise be handed half
    # a window and under-report the ceiling by the channel count.
    sample_count = sample_rate_hz * window_seconds * channels
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
    if observer is not None:
        focus = observer
    else:
        try:
            focus = create_focus_observer(env)
        except Exception as error:  # noqa: BLE001 - diagnostics must not raise
            return {
                "verification_supported": False,
                "verification_enabled": config.verify_target,
                "restore_clipboard": config.restore_clipboard,
                "restore_delay_ms": config.restore_clipboard_delay_ms,
                "detail": f"Unable to check delivery target verification: {error}",
            }
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
            # The environment the renderer is launched with, not this process's:
            # checking under anything else answers a question the user did not ask.
            env=renderer_environment(backend, environment),
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
