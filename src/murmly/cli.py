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
from murmly.config import WINDOWS_PIPE_NAME, MurmlyConfig, default_config_path, load_config
from murmly.daemon import (
    COMMAND_REBIND_HOTKEYS,
    COMMAND_STATUS,
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
    MissingToolError,
    PasteInjection,
    choose_clipboard_copy_command,
    create_clipboard_paster,
    is_wayland_session,
    select_paste_injection,
)
from murmly.focus import FocusObserver, create_focus_observer, record_target, should_deliver
from murmly.idle import system_memory_returnable, system_memory_unreturnable_reason
from murmly.overlay import (
    SYSTEM_PYTHON,
    OverlayBackend,
    detect_overlay_backend,
    renderer_environment,
    renderer_python,
    renderer_script,
)
from murmly.platform import (
    BACKEND_REGISTRIES,
    PERMISSIONS,
    PermissionState,
    PlatformProfile,
    SUPPORTED_OPERATING_SYSTEMS,
    OperatingSystem,
    environment_preconditions_for,
    hotkey_mechanism_is_in_process,
    resolve_platform,
    transcription_runtime_gap,
    runtime_gaps_for,
    TRANSCRIPTION_CAPABILITY,
)
from murmly.silence import SilenceDetector
from murmly.stt import FasterWhisperTranscriber
from murmly.tts import CUDA_PROVIDER, KokoroSynthesizer, resolve_providers
from murmly.win_pipe import is_pipe_name


logger = logging.getLogger(__name__)

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
    # Nothing here may prevent the exit. A journal socket that has already gone
    # raises BrokenPipeError from a flush, and an exception escaping this
    # function returns through main() into Py_Finalize -- the crash this exists
    # to avoid. Each is attempted on its own so that one failure does not skip
    # the rest: a broken stdout says nothing about the log handlers.
    try:
        for flush in (sys.stdout.flush, sys.stderr.flush, logging.shutdown):
            try:
                flush()
            except Exception:  # noqa: BLE001 - losing output beats dumping core
                pass
    finally:
        os._exit(exit_code)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Decided before the attempt, so the exit below has a status even when a
    # BaseException never reaches an assignment.
    exit_code = 1
    try:
        try:
            exit_code = _dispatch(parser, args)
        except KeyboardInterrupt:
            # `except Exception` does not catch this. It arrives when the daemon
            # is run from a terminal rather than the service manager, which is
            # how it is run while developing.
            exit_code = 130
            raise
        except Exception as error:  # noqa: BLE001 - no command terminates with an unhandled error
            # A backstop only. Every failure with something specific to say --
            # the daemon's startup refusal, an unreadable configuration, a
            # daemon that did not respond -- reports it itself, because a
            # generic message names the wrong thing.
            #
            # Guarded because reporting must not become the failure. A daemon
            # whose journal socket has gone raises BrokenPipeError from both
            # calls below, and that would escape before the exit is reached.
            # It is what "no command terminates with an unhandled error" asks
            # for anyway: a shell that closed the pipe is not a reason to raise.
            try:
                if args.command == "daemon":
                    # The daemon runs unattended under the service manager,
                    # where this one line would be all that survived of a crash
                    # nothing anticipated. Whoever reads the journal needs the
                    # frames.
                    traceback.print_exc(file=sys.stderr)
                print(f"murmly: unexpected failure: {error}", file=sys.stderr)
            except Exception:  # noqa: BLE001 - losing the report beats dumping core
                pass
            exit_code = 1
    finally:
        if args.command == "daemon":
            # Structural rather than positional. Every way out of the daemon
            # route leaves through here: a clean stop, a startup refusal, the
            # backstop above, and a BaseException that backstop cannot catch.
            # Reaching Py_Finalize instead is the SIGSEGV in issue #27, and both
            # review findings on this change were a path that slipped past a
            # call placed after the try rather than inside a finally.
            leave_without_finalizing(exit_code)
    return exit_code


def _unsupported_platform_message(profile: PlatformProfile) -> str | None:
    """None when `profile` is one Murmly supports; otherwise the refusal text.

    Checked before any command touches a config path, a socket, or a file: a
    partial install on a platform Murmly cannot run on is worse than none,
    because the uninstaller for that platform does not exist to remove it.
    """
    if profile.supported:
        return None
    supported = ", ".join(supported_os.value for supported_os in SUPPORTED_OPERATING_SYSTEMS)
    return (
        f"murmly: unsupported platform: {profile.operating_system.value}. "
        f"Murmly supports: {supported}."
    )


def _dispatch(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    # Resolved once and passed on from here, rather than re-resolved at each
    # command below: this is the one call site in `cli.py` written for the
    # platform layer, and it is what lets the refusal below run before
    # `load_config()` touches a config path, a socket, or a file on a platform
    # Murmly does not support. `default_runtime_dir`'s `os.getuid()` no longer
    # raises there -- it sits behind a Linux branch now -- but a partial run on
    # an unsupported platform is still worth refusing before it starts.
    profile = resolve_platform()
    unsupported = _unsupported_platform_message(profile)
    if unsupported is not None:
        print(unsupported, file=sys.stderr)
        return 1

    config_path = Path(args.config) if args.config else default_config_path()
    try:
        config = load_config(args.config)
    except Exception as error:  # noqa: BLE001 - the file is named rather than raised
        print(f"Unable to read the configuration at {config_path}: {error}", file=sys.stderr)
        return 1

    if args.command == "daemon":
        return _run_daemon(config, profile)
    if args.command in {"toggle", "status", "toggle-session"}:
        # The daemon's command vocabulary is not the CLI's: argparse spells a
        # subcommand with a hyphen and the wire protocol does not.
        return _run_client_command(config, DAEMON_COMMANDS.get(args.command, args.command))
    if args.command == "install":
        return _run_install(args.hotkey, args.session_hotkey, profile=profile, config=config)
    if args.command == "uninstall":
        return _run_uninstall()
    if args.command == "spike":
        return _run_spike(config, args.seconds, args.paste)
    if args.command == "doctor":
        _run_doctor(config, profile)
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


def _run_install(
    hotkey_text: str,
    session_hotkey_text: str | None = None,
    profile: PlatformProfile | None = None,
    config: MurmlyConfig | None = None,
) -> int:
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

    if outcome.hotkey_registered:
        _request_hotkey_rebind(profile, config)
    return 0


def _request_hotkey_rebind(
    profile: PlatformProfile | None = None,
    config: MurmlyConfig | None = None,
) -> None:
    """Task 5.5: reach a running daemon to rebind, since an in-process
    registration cannot be changed by writing a file the desktop reads.

    A no-op on every platform this change targets: `Installer.install` already
    wrote the record `hotkey_record.py` reads, and `hotkey_mechanism_is_in_process`
    is false for both `plasma` and `gnome`, so nothing is sent. Best-effort
    where it is not a no-op: a daemon that is not running picks the new keys up
    from the record when it next starts, so a failure to reach one now is not
    reported as an install failure.
    """
    resolved_profile = profile if profile is not None else resolve_platform()
    if not hotkey_mechanism_is_in_process(resolved_profile):
        return
    resolved_config = config if config is not None else load_config(default_config_path())
    try:
        send_command(str(resolved_config.socket_path), COMMAND_REBIND_HOTKEYS)
    except (OSError, DaemonNotRespondingError) as error:
        logger.debug("Could not reach the running daemon to rebind hotkeys: %s", error)


def _run_uninstall() -> int:
    try:
        outcome = Installer().uninstall()
    except InstallError as error:
        print(str(error), file=sys.stderr)
        return 1

    for message in outcome.messages:
        print(message)
    return 0


def _run_daemon(config: MurmlyConfig, profile: PlatformProfile | None = None) -> int:
    # Wrapped rather than placed in the inner unwinding below, because every
    # return from here is the daemon process on its way out: a clean stop, a
    # startup refusal, and an exception climbing to the top all reach the exit
    # where PortAudio's teardown would otherwise run.
    try:
        return _serve_daemon(config, profile)
    finally:
        disable_portaudio_exit_teardown()


def _serve_daemon(config: MurmlyConfig, profile: PlatformProfile | None = None) -> int:
    # Resolved here rather than only in `_dispatch`, so a caller that reaches
    # `_run_daemon` directly -- every existing test does -- still gets the
    # check without having to supply a profile of its own.
    resolved = profile if profile is not None else resolve_platform()

    gap = transcription_runtime_gap(resolved)
    if gap is not None:
        # The runtime's own load error is never what is shown here: it would
        # name a missing package rather than the machine characteristic that
        # has no build of it, and a person reading it cannot act on a loader
        # error the way they can act on "no build exists for a musl C
        # library".
        print(
            f"murmly: refusing to start. No {gap.runtime} build exists for "
            f"{gap.characteristic}, and transcription is what Murmly is.",
            file=sys.stderr,
        )
        return 1

    # Everything else missing a runtime build degrades rather than refusing:
    # the daemon still starts and serves capture, transcription and delivery,
    # and each such capability is only logged here. `murmly doctor` is where
    # this becomes a report a person reads (section 6), not built yet.
    for other_gap in runtime_gaps_for(resolved):
        if other_gap.capability != TRANSCRIPTION_CAPABILITY:
            logger.warning(
                "%s is unavailable: no %s build exists for %s.",
                other_gap.capability,
                other_gap.runtime,
                other_gap.characteristic,
            )

    try:
        daemon = MurmlyDaemon(config, profile=resolved)
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
    paster = create_clipboard_paster(
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


def _run_doctor(config: MurmlyConfig, profile: PlatformProfile | None = None) -> None:
    # Resolved once, like `_dispatch` resolves it once for every other command
    # (task 1.3): a second `resolve_platform()` call here could in principle
    # read a different environment than the one that decided everything else
    # this process did. `profile` still defaults so the existing call sites --
    # this module's own tests among them -- do not have to supply one just to
    # exercise the machine they already run on.
    resolved_profile = profile if profile is not None else resolve_platform()

    # Asked first, before any section runs and long before the report is
    # assembled. `live_transcription_diagnostics` below loads the model when live
    # transcription is enabled, and the speech probe opens the output device;
    # residency is meant to say what the daemon held when the question was put,
    # not what this report caused on its way past.
    (model_resident, model_resident_detail), synthesis_residency = daemon_residency(config)

    # `choose_clipboard_copy_command` has no Windows branch of its own (task
    # 9.1's clipboard is a Win32 API call, not a command to name), so this
    # reports the mechanism directly rather than asking a Linux-only chooser
    # about a platform it was never taught, which would otherwise surface a
    # misleading "install xclip" on a machine that never needed it.
    if resolved_profile.operating_system is OperatingSystem.WINDOWS:
        clipboard_command = "Win32 clipboard API (CF_UNICODETEXT)"
    else:
        try:
            clipboard_command = choose_clipboard_copy_command()
        except MissingToolError as error:
            clipboard_command = f"unavailable: {error}"

    try:
        injection_report = paste_injection_diagnostics(select_paste_injection(profile=resolved_profile))
    except Exception as error:  # noqa: BLE001 - diagnostics must not raise
        injection_report = {
            "available": False,
            "method": None,
            "reason": f"Unable to choose a paste method: {error}",
            "remedy": [],
        }

    model_cache_path, model_cache_detail = transcription_model_cache_path()

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
        # Unchanged on Linux: `is_wayland_session()`'s own precedence still
        # decides between the two values this field has always reported.
        # Off Linux there is no wayland/x11 distinction to make -- Windows and
        # macOS sessions are not display protocols in that sense -- so the
        # field names the operating system itself rather than misreporting a
        # non-Linux session as one of the two Linux values (task 6.3; this is
        # the field the proposal's BREAKING note names).
        if resolved_profile.operating_system is OperatingSystem.LINUX:
            session: str | None = "wayland" if is_wayland_session() else "x11"
        else:
            session = resolved_profile.operating_system.value
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
        speech = speech_output_diagnostics(config, residency=synthesis_residency)
    except Exception as error:  # noqa: BLE001 - diagnostics must not raise
        synthesis_resident, synthesis_resident_detail = synthesis_residency
        speech = {
            "enabled": config.tts_enabled,
            "available": False,
            # Both residency keys survive a failed probe. A section that drops
            # them reads as one the report never asked about, which is the
            # distinction the residency requirement exists to preserve. The
            # daemon's answer survives it too: it was taken before this probe
            # ran and nothing about the probe failing makes it less true.
            "unload_after_idle_s": config.tts_unload_after_idle_s,
            "resident": synthesis_resident,
            "detail": f"Unable to check speech output: {error}",
        }
        if synthesis_resident_detail is not None:
            speech["resident_detail"] = synthesis_resident_detail

    report: dict[str, object] = {
        "config_path": str(config.config_path),
        "socket_path": str(config.socket_path),
        "command_socket": command_socket_diagnostics(config, resolved_profile),
        "platform": platform_diagnostics(resolved_profile),
        "session": session,
        "clipboard_command": clipboard_command,
        "paste_injection": injection_report,
        "model_profile": config.model_profile,
        "model_name": config.model_name,
        "model_cache_path": model_cache_path,
        "device": config.device,
        "compute_type": config.compute_type,
        "runtime_device": runtime_device,
        "runtime_compute_type": runtime_compute_type,
        "beam_size": config.beam_size,
        "vad_filter": config.vad_filter,
        "model_resident": model_resident,
        # Reported even when it is zero. Zero is how release is switched off, and
        # a reader who cannot see the value cannot tell a model that will never
        # be released from one this report forgot to mention.
        "unload_after_idle_s": config.unload_after_idle_s,
        # One answer for both models: they share a process and an allocator, so
        # whether a release's freed memory reaches the system is the same fact
        # for the transcription model and the synthesis session alike.
        "system_memory_returnable": system_memory_returnable(),
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
    if model_resident_detail is not None:
        report["model_resident_detail"] = model_resident_detail
    if not report["system_memory_returnable"]:
        report["system_memory_returnable_detail"] = system_memory_unreturnable_reason()
    if model_cache_detail is not None:
        report["model_cache_detail"] = model_cache_detail
    print(json.dumps(report, indent=2))


def transcription_model_cache_path() -> tuple[str | None, str | None]:
    """Where `huggingface_hub` resolves its cache, and nothing this reports could
    have moved.

    Deliberately not one of Murmly's own locations: `faster-whisper` downloads
    the transcription model through `huggingface_hub`, whose own default --
    `~/.cache/huggingface/hub` on every operating system, not
    `~/Library/Caches` and not `%LOCALAPPDATA%` -- would be re-derived wrongly by
    guessing at it here, and moving it with `WhisperModel(download_root=)` would
    strand the 1.6 GB an existing install already has cached under the Hub's own
    answer. So this asks `huggingface_hub` for the path it actually resolved to,
    through a deferred import: `murmly doctor` should not pay to import it on a
    machine where transcription never runs.
    """
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
    except Exception as error:  # noqa: BLE001 - diagnostics must not raise
        return None, f"Unable to determine the transcription model cache: {error}"
    return HF_HUB_CACHE, None


def platform_diagnostics(profile: PlatformProfile) -> dict[str, object]:
    """The `platform` section of `murmly doctor` (task 6.1).

    Names the resolved platform, and for each of the eight platform-dependent
    concerns in `BACKEND_REGISTRIES`, the mechanism it selected or the reason
    none was. `BackendChoice.remedy` is what decides whether an unavailable
    concern's report may say what to install: empty renders as "the platform
    offers none", non-empty as what to install, enable, or grant -- the
    distinction is read off that field, never re-derived by inspecting
    `reason`'s wording (see `BackendChoice`'s docstring).

    Every concern is a key in the returned `concerns` mapping on every
    platform, present whether or not this platform can serve it, which is what
    keeps this section's shape identical everywhere (task 6.5, 18.17): a
    concern this platform cannot serve is `available: False` with a reason,
    never a key the report omits.

    A concern whose mechanism is gated behind a permission must AND that
    permission's state into its own `available`: a present mechanism whose
    permission is denied is not an available concern, and deriving
    `available` from `BackendChoice.available` alone, as this does today,
    would report it as one. None of the eight registries in
    `BACKEND_REGISTRIES` is permission-gated yet -- `PERMISSIONS`' first entry
    (Windows' microphone privacy setting, task 9.5) gates microphone capture,
    which is not one of the eight -- so that rule still has no case to apply
    to here, and `permissions` is rendered as its own section below rather
    than folded into any concern.
    """
    concerns: dict[str, object] = {}
    for concern, registry in BACKEND_REGISTRIES.items():
        choice = registry.select(profile)
        concern_report: dict[str, object] = {
            "mechanism": choice.mechanism,
            "available": choice.available,
        }
        if not choice.available:
            concern_report["reason"] = choice.reason
            concern_report["remedy"] = list(choice.remedy)
        concerns[concern] = concern_report

    permissions: dict[str, object] = {}
    for name, permission in PERMISSIONS.items():
        # Filtered before the check runs, not only before rendering: a
        # platform this permission does not apply to must not pay for -- or
        # be reported on the strength of -- a check written for a different
        # operating system (task 9.5's Windows registry read has no meaning
        # to run against a Linux profile, and no `winreg` module to run it
        # with).
        if not permission.applies(profile):
            continue
        try:
            state = permission.check(profile)
        except Exception as error:  # noqa: BLE001 - a permission check must not raise
            # Coerced to undetermined rather than propagated: a check that
            # fails to run is exactly as uninformative about the grant as a
            # platform that offers no way to read it, and the spec forbids
            # reporting either as granted.
            state = PermissionState.UNDETERMINED
            permissions[name] = {
                "capability": permission.capability,
                "state": state.value,
                "grant_location": permission.grant_location,
                "detail": f"Unable to determine whether {name} is granted: {error}",
            }
            continue
        permissions[name] = {
            "capability": permission.capability,
            "state": state.value,
            "grant_location": permission.grant_location,
        }

    return {
        "operating_system": profile.operating_system.value,
        "supported": profile.supported,
        "architecture": profile.architecture,
        "libc": profile.libc,
        "desktop": profile.desktop.value,
        "concerns": concerns,
        "permissions": permissions,
        # Tasks 11.5, 11.6: machine settings that change what installing or
        # running Murmly costs without blocking any capability outright, so
        # they are their own section rather than folded into `permissions`
        # (`EnvironmentPrecondition`'s docstring has the distinction) or
        # `concerns` (neither gates a registry entry). `windows-long-paths`
        # is chiefly an install-time fact; `murmly install`'s bootstrap
        # (section 16) checks it before `uv sync` runs, where it can still
        # change anything -- this is the ongoing report for a machine already
        # running Murmly, same relationship `concerns` has to the registries
        # it also reports.
        "environment": environment_preconditions_for(profile),
    }


def command_socket_diagnostics(
    config: MurmlyConfig, profile: PlatformProfile | None = None
) -> dict[str, object]:
    """Who can reach the command socket, for `murmly doctor`.

    Reported, never refused. Only the daemon refuses to start on a path another
    account can write, because every command loads this configuration and the
    command that exists to explain the condition must keep running -- the
    `command-interface` spec's "Other commands still run when the daemon would
    refuse" scenario.

    Task 7.5: `socket_path_detail`'s directory-privacy analysis presumes a
    filesystem object, which a pipe name is not, so a pipe-shaped configured
    value skips it entirely rather than being walked as a filesystem path. The
    same presumption runs the other way on Windows: `socket_path_detail` reads
    `os.stat().st_uid` through `os.getuid()`, an attribute Windows' `os` module
    does not have at all, so a filesystem-shaped value configured on a Windows
    resolution -- the mismatch `MurmlyDaemon._require_private_channel` refuses
    at startup -- is reported here rather than walked, the same way the
    reverse mismatch already is. This function reports and never refuses, so
    it cannot let that mismatch reach `socket_path_detail` and crash instead.
    """
    resolved = profile if profile is not None else resolve_platform()
    path = str(config.socket_path)
    report: dict[str, object] = {
        "path": path,
        "peer_identity_supported": peer_identity_supported(resolved),
    }
    if is_pipe_name(path):
        # A named pipe's DACL is built owner-only unconditionally (task 7.2),
        # so any pipe-shaped value is private wherever Windows is the platform
        # actually serving it; a pipe-shaped value configured anywhere else is
        # the mismatch `MurmlyDaemon._require_private_channel` also refuses.
        report["path_private"] = resolved.operating_system is OperatingSystem.WINDOWS
        if resolved.operating_system is not OperatingSystem.WINDOWS:
            report["detail"] = (
                f"{path} is a Windows named-pipe name, but "
                f"{resolved.operating_system.value} serves its command channel as "
                "a filesystem socket."
            )
    elif resolved.operating_system is OperatingSystem.WINDOWS:
        report["path_private"] = False
        report["detail"] = (
            f"{path} is a filesystem path, but Windows serves its command "
            "channel as a named pipe, so this daemon cannot create it "
            f"privately. Configure daemon.socket_path as a pipe name such as "
            f"{WINDOWS_PIPE_NAME}."
        )
    else:
        try:
            detail = socket_path_detail(config.socket_path)
        except Exception as error:  # noqa: BLE001 - diagnostics must not raise
            # `socket_path_detail` reads `os.stat().st_uid` through
            # `os.getuid()` for a resolved profile the caller says is Linux or
            # macOS -- true of every real machine that profile could name,
            # but a test may deliberately resolve one of those profiles on a
            # real Windows interpreter, to keep this section's Linux/macOS
            # behaviour exercised on every host, which has no `os.getuid` for
            # it to find at all. This function reports and never refuses, so
            # that mismatch is named rather than left to crash the report.
            detail = f"Unable to determine whether {path} is private: {error}"
        report["path_private"] = detail is None
        if detail is not None:
            report["detail"] = detail
    if not report["peer_identity_supported"]:
        report["peer_identity_detail"] = (
            "This platform cannot report the account behind a connection. The "
            "command channel is protected by its own access control alone."
        )
    return report


def daemon_residency(
    config: MurmlyConfig,
    send: Callable[[str, str], dict[str, object]] | None = None,
) -> tuple[tuple[bool | None, str | None], tuple[bool | None, str | None]]:
    """What the running daemon holds, as a value and a reason for each model.

    Asked of the daemon rather than answered here. `murmly doctor` runs in its
    own process and holds neither model, so its own residency is a constant
    `False` that says nothing about the system -- and the models this reports on
    live in the daemon's process, where the only thing that can see them is the
    daemon.

    `send_command` rather than `send_command_with_recovery`: the recovery path
    starts the installed service when nothing answers, which would have this
    report boot a daemon and then say its models are idle, inverting the one
    distinction it exists to draw. Its timeouts are not extended either. Doctor
    is what someone runs when things are wrong, and a daemon that will not
    answer in time is a fact to report rather than one to wait for.

    Three outcomes per model, in the shape this file already uses for a value it
    could not determine: True, False, or None beside a detail naming the reason.
    Nothing is loaded to produce any of them.
    """
    ask = send if send is not None else send_command
    try:
        response = ask(str(config.socket_path), COMMAND_STATUS)
    except (FileNotFoundError, ConnectionRefusedError) as error:
        return _residency_unknown(
            f"No Murmly daemon is running, so what it holds could not be asked: {error}"
        )
    except DaemonNotRespondingError as error:
        return _residency_unknown(f"The Murmly daemon did not answer: {error}")
    except Exception as error:  # noqa: BLE001 - diagnostics must not raise
        return _residency_unknown(f"Unable to ask the Murmly daemon what it holds: {error}")

    if not isinstance(response, dict):
        return _residency_unknown(
            "The Murmly daemon answered with something other than a status response."
        )
    if "model_resident" not in response:
        # Absent rather than null, which is how a daemon too old to know the
        # question answers it. A daemon that knows and cannot answer reports null
        # beside its own reason, and that reason is carried through below.
        return _residency_unknown(
            "The running Murmly daemon does not report residency. Restart the "
            "service to pick up a version that does."
        )

    transcription = _residency_field(response, "model_resident", "transcription")
    if "synthesis_resident" in response:
        synthesis = _residency_field(response, "synthesis_resident", "synthesis")
    else:
        # The daemon answered and left synthesis out, which is how it says it
        # never built a synthesizer. Reported from the daemon's side rather than
        # from the configuration read here: the two can disagree after an edit
        # that nothing has restarted for, and the daemon is the one holding it.
        synthesis = (
            None,
            "The Murmly daemon holds no synthesis session: speech output is not "
            "enabled in the daemon that is running.",
        )
    return transcription, synthesis


def _residency_unknown(
    detail: str,
) -> tuple[tuple[None, str], tuple[None, str]]:
    """Both models unanswered for the same reason, which is one reason each."""
    return (None, detail), (None, detail)


def _residency_field(
    response: dict[str, object],
    key: str,
    subject: str,
) -> tuple[bool | None, str | None]:
    """One model's residency out of the daemon's answer, with its own reason.

    A null the daemon sent carries the daemon's detail, not one written here: it
    knows why it could not read its own holder and this process does not.
    """
    value = response.get(key)
    if isinstance(value, bool):
        return value, None
    detail = response.get(f"{key}_detail")
    if isinstance(detail, str) and detail:
        return None, f"The Murmly daemon could not read {subject} residency: {detail}"
    return None, (
        f"The Murmly daemon reported {subject} residency as {value!r}, which is "
        "not an answer."
    )


def speech_output_diagnostics(
    config: MurmlyConfig,
    synthesizer: KokoroSynthesizer | None = None,
    residency: tuple[bool | None, str | None] = (None, None),
) -> dict[str, object]:
    """What `murmly doctor` says about speech output.

    Reports the settings in use alongside any configured value that was not
    honoured, because a setting that silently falls back looks to its owner like
    one that was ignored. Never loads the model: the probe checks that every
    part is present, which is what decides whether a session can be opened.

    Residency is passed in rather than read off the probe. The probe deliberately
    never builds a session, so its own `resident` is a constant False; the
    session this section is about lives in the daemon, and `daemon_residency`
    is what asked it.
    """
    synthesis_resident, synthesis_resident_detail = residency
    report: dict[str, object] = {
        "enabled": config.tts_enabled,
        "voice": config.tts_voice,
        "rate_percent": config.tts_rate_percent,
        "device": config.tts_device,
        "model_dir": str(config.tts_model_dir),
        "output_device": config.tts_output_device or None,
        # Carried by the disabled and the unavailable report as well, and carried
        # when the period is zero, which is how synthesis release ships. A reader
        # who cannot see the value cannot tell a release that is switched off
        # from one this report did not mention. Residency is carried through
        # every early return below for the same reason, and it is the daemon's
        # answer on all of them -- including the one for speech output being
        # disabled here, because a daemon still running from before that edit is
        # holding whatever it is holding regardless of what this file now reads.
        "unload_after_idle_s": config.tts_unload_after_idle_s,
        "resident": synthesis_resident,
    }
    if synthesis_resident_detail is not None:
        report["resident_detail"] = synthesis_resident_detail
    if config.tts_voice_rejected_value is not None:
        report["voice_rejected_value"] = config.tts_voice_rejected_value
    if config.tts_rate_rejected_value is not None:
        report["rate_rejected_value"] = config.tts_rate_rejected_value
    if config.tts_device_rejected_value is not None:
        report["device_rejected_value"] = config.tts_device_rejected_value

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

    providers: list[str] | None = None
    fallback_reason: str | None = None
    try:
        providers, fallback_reason = synthesis_providers(config)
        report["providers"] = providers
    except Exception as error:  # noqa: BLE001 - diagnostics must not raise
        report["providers"] = None
        report["provider_detail"] = str(error)

    # Read off a constructed session where there is one, and otherwise off what
    # resolution would choose. Never off `onnxruntime.get_available_providers()`,
    # which advertises CUDA on a runtime whose session then falls back to the
    # CPU, so a report built from it names a processor that never ran the model
    # -- see docs/agent-notes/onnxruntime-gpu-cuda-version.md. In `murmly doctor`
    # the probe holds no session, because the probe deliberately does not
    # construct the model, so what is reported there is resolution's choice.
    in_use = probe.provider or (providers[0] if providers else None)
    report["device_in_use"] = None if in_use is None else _processor_name(in_use)
    if fallback_reason is not None:
        report["device_detail"] = fallback_reason
    elif in_use != CUDA_PROVIDER and CUDA_PROVIDER in (providers or ()):
        # Resolution got the accelerator and the session is running on something
        # else: accept-then-fail, which nothing but a constructed session can
        # show. Resolution logged nothing here because from where it stands
        # nothing went wrong.
        report["device_detail"] = (
            f"Speech output resolved {CUDA_PROVIDER} and the session reports {in_use}."
        )

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


class _CollectedWarnings(logging.Handler):
    """Keeps the warnings logged while it is attached, for the report to carry."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def synthesis_providers(config: MurmlyConfig) -> tuple[list[str], str | None]:
    """The providers synthesis would use, and why it fell back to the CPU.

    The reason is taken from the warning `resolve_providers` already logs rather
    than worked out a second time here. Three separate conditions send synthesis
    to the CPU -- a runtime build without the provider, the CUDA extra missing
    behind it, and a library that refuses to load -- and each names a different
    remedy. A second copy of that test would drift from the one that decides and
    name the wrong one, which is worse than naming none, because a remedy is
    followed. The warning reaches standard error either way; it is carried into
    the report as well so nobody has to read the two together.
    """
    collected = _CollectedWarnings()
    # Named off the function rather than written out, so resolution and the
    # report cannot end up watching two different loggers.
    resolution = logging.getLogger(resolve_providers.__module__)
    resolution.addHandler(collected)
    try:
        providers = resolve_providers(config)
    finally:
        resolution.removeHandler(collected)
    return providers, collected.messages[-1] if collected.messages else None


def _processor_name(provider: str) -> str:
    """An execution provider in the vocabulary `[tts] device` is written in.

    So the two can be read as a pair -- what was asked for, and what is in use.
    A provider name does not compare against a device setting.
    """
    return "cuda" if provider == CUDA_PROVIDER else "cpu"


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
    # Declared, not implied. Reporting residency loads nothing, and this section
    # is the one part of the report that does: `measure_partial_pass_ms`
    # constructs a transcriber and runs two passes over a full window. A reader
    # comparing the residency above against what the machine is holding after
    # the report has to know that this ran.
    report["partial_pass_loaded_model"] = False
    if config.live_transcribe:
        report["partial_pass_loaded_model"] = True
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
                "because the voice activity filter trims silence. Measuring this "
                "loaded the transcription model in this process; the residency "
                "reported above was read from the daemon before it ran."
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


#: Every key a renderer's own `--check` report can contribute, regardless of
#: which renderer answered. Keeping both renderers' keys in one place, copied
#: unconditionally into every report's defaults below, is what keeps the
#: `platform-support` spec's "the diagnostics report keeps its shape"
#: requirement true here too: a GTK4-only report on Linux still carries
#: `pyside6`/`qt_version` (as their does-not-apply defaults), and a Qt-only
#: report on Windows still carries `pygobject`/`gtk4`/etc, rather than the
#: field set depending on which platform answered.
_GTK4_CHECK_KEYS: tuple[str, ...] = ("pygobject", "gtk4", "gdk_x11", "native_x11", "gtk4_layer_shell")
_QT_CHECK_KEYS: tuple[str, ...] = ("pyside6", "qt_version")
_RENDERER_CHECK_KEYS: tuple[str, ...] = ("system_python", *_GTK4_CHECK_KEYS, *_QT_CHECK_KEYS)


def overlay_diagnostics(
    config: MurmlyConfig,
    env: dict[str, str] | None = None,
    run_command: RunCommand = subprocess.run,
    helper_path: Path | None = None,
) -> dict[str, object]:
    environment = env if env is not None else os.environ
    profile = resolve_platform(environment)
    backend = detect_overlay_backend(environment)
    if profile.operating_system is OperatingSystem.LINUX:
        desktop = environment.get("XDG_CURRENT_DESKTOP") or environment.get("XDG_SESSION_DESKTOP", "")
        session = "wayland" if is_wayland_session(environment) else "x11"
    else:
        # No wayland/x11 distinction to misreport off Linux (task 6.3's same
        # reasoning, applied to this report's own `session`/`desktop` fields
        # rather than only the top-level one): the platform itself is what
        # both name instead.
        desktop = profile.operating_system.value
        session = profile.operating_system.value
    supported_session = backend is not None
    renderer_path = (helper_path or renderer_script(backend if backend is not None else OverlayBackend.X11)).resolve()
    report: dict[str, object] = {
        "enabled": config.overlay_enabled,
        "desktop": desktop or "unknown",
        "session": session,
        "backend": backend.value if backend is not None else None,
        "supported_session": supported_session,
        # Backend-specific once there is a second renderer to disagree with
        # this: no backend has been selected yet where `backend` is None, so
        # this reports the interpreter Linux's own renderer needs, same as it
        # always has.
        "system_python": str(renderer_python(backend) if backend is not None else SYSTEM_PYTHON),
        "pygobject": False,
        "gtk4": None,
        "gdk_x11": False,
        "native_x11": False,
        "gtk4_layer_shell": False,
        "pyside6": False,
        "qt_version": None,
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
            [str(renderer_python(backend)), str(renderer_path), "--check", "--backend", backend.value],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5,
            # The environment the renderer is launched with, not this process's:
            # checking under anything else answers a question the user did not ask.
            env=renderer_environment(backend, environment),
        )
        helper_report = json.loads(result.stdout)
        if not isinstance(helper_report, dict):
            raise ValueError("Overlay helper returned a non-object report.")
        for key in _RENDERER_CHECK_KEYS:
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
