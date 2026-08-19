from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from murmly.cli import (
    _measurement_clip,
    _run_doctor,
    command_socket_diagnostics,
    delivery_diagnostics,
    live_transcription_diagnostics,
    main,
    measure_partial_pass_ms,
    overlay_diagnostics,
    paste_injection_diagnostics,
)
from murmly.config import MurmlyConfig
from murmly.daemon import DaemonStartupError, MurmlyDaemon
from murmly.integrations import PasteInjection
from murmly.overlay import OverlayBackend, renderer_environment
from murmly.stt import FasterWhisperTranscriber


class CliTests(unittest.TestCase):
    def test_daemon_exits_cleanly_on_sigterm(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            socket_path = Path(temp_dir) / "murmly.sock"
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                f'[daemon]\nsocket_path = "{socket_path}"\n\n[overlay]\nenabled = false\n',
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
            process = subprocess.Popen(
                [sys.executable, "-m", "murmly", "--config", str(config_path), "daemon"],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 5
                while not socket_path.exists():
                    if process.poll() is not None:
                        self.fail(f"daemon exited before creating its socket: {process.stderr.read()}")
                    if time.monotonic() >= deadline:
                        self.fail("daemon socket was not created")
                    time.sleep(0.01)

                process.send_signal(signal.SIGTERM)
                _stdout, stderr = process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

        self.assertEqual(0, process.returncode, stderr)
        self.assertFalse(socket_path.exists())

    def test_doctor_reports_effective_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )

            with (
                patch.object(
                    FasterWhisperTranscriber,
                    "resolve_runtime",
                    return_value=("cuda", "float16"),
                ),
                patch("murmly.cli.choose_clipboard_copy_command", return_value=["xclip"]),
                patch(
                    "murmly.cli.select_paste_injection",
                    return_value=PasteInjection("xdotool", ("xdotool", "key", "ctrl+v")),
                ),
                redirect_stdout(StringIO()) as output,
            ):
                _run_doctor(config)

        report = json.loads(output.getvalue())
        self.assertEqual("auto", report["device"])
        self.assertEqual("auto", report["compute_type"])
        self.assertEqual("cuda", report["runtime_device"])
        self.assertEqual("float16", report["runtime_compute_type"])

    def test_paste_injection_diagnostics_names_the_method_when_it_can_inject(self) -> None:
        report = paste_injection_diagnostics(
            PasteInjection("ydotool", ("ydotool", "key", "29:1", "47:1", "47:0", "29:0"))
        )

        self.assertTrue(report["available"])
        self.assertEqual("ydotool", report["method"])
        self.assertEqual(["ydotool", "key", "29:1", "47:1", "47:0", "29:0"], report["command"])
        self.assertNotIn("remedy", report)

    def test_paste_injection_diagnostics_separates_absent_from_unusable(self) -> None:
        absent = paste_injection_diagnostics(
            PasteInjection(
                None,
                None,
                reason="No Wayland paste injector is installed; install wtype or ydotool.",
                remedy=("sudo dnf install ydotool",),
            )
        )
        unusable = paste_injection_diagnostics(
            PasteInjection(
                None,
                None,
                reason="wtype is installed but cannot inject in this session: no virtual keyboard",
                remedy=("sudo dnf install ydotool",),
            )
        )

        self.assertFalse(absent["available"])
        self.assertIn("is installed", absent["reason"])
        self.assertEqual(["sudo dnf install ydotool"], absent["remedy"])
        self.assertFalse(unusable["available"])
        self.assertIn("cannot inject in this session", unusable["reason"])
        self.assertEqual(["sudo dnf install ydotool"], unusable["remedy"])

    def test_overlay_diagnostics_reports_available_plasma_x11_runtime(self) -> None:
        config = self._config()
        completed = self._helper_result(
            {
                "available": True,
                "pygobject": True,
                "gtk4": "4.22.4",
                "gdk_x11": True,
                "native_x11": True,
                "gtk4_layer_shell": False,
            }
        )

        report = overlay_diagnostics(
            config,
            env={
                "XDG_SESSION_TYPE": "x11",
                "DISPLAY": ":0",
                "XDG_CURRENT_DESKTOP": "KDE",
            },
            run_command=lambda *_args, **_kwargs: completed,
        )

        self.assertTrue(report["available"])
        self.assertTrue(report["supported_session"])
        self.assertEqual("x11", report["backend"])
        self.assertTrue(report["gdk_x11"])
        self.assertTrue(report["native_x11"])
        self.assertEqual("4.22.4", report["gtk4"])

    def test_overlay_diagnostics_checks_under_the_renderer_environment(self) -> None:
        config = self._config()
        completed = self._helper_result({"available": True, "gtk4_layer_shell": True})
        recorded: dict[str, object] = {}

        def record(*arguments: object, **keywords: object) -> object:
            recorded.update(keywords)
            return completed

        report = overlay_diagnostics(
            config,
            env={
                "XDG_SESSION_TYPE": "wayland",
                "WAYLAND_DISPLAY": "wayland-0",
                "XDG_CURRENT_DESKTOP": "KDE",
                "LD_PRELOAD": "/tmp/injected.so",
            },
            run_command=record,
        )

        self.assertTrue(report["available"])
        # Whatever the renderer will be launched with, down to the preload that
        # decides whether Layer Shell works at all.
        self.assertEqual(
            renderer_environment(
                OverlayBackend.WAYLAND,
                {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0", "XDG_CURRENT_DESKTOP": "KDE"},
            ),
            recorded["env"],
        )

    def test_overlay_diagnostics_reports_partial_install(self) -> None:
        config = self._config()
        completed = self._helper_result(
            {
                "available": False,
                "pygobject": True,
                "gtk4": "4.22.4",
                "gdk_x11": False,
                "native_x11": False,
                "gtk4_layer_shell": False,
                "error": "Gtk4LayerShell namespace not available",
            },
            returncode=1,
        )

        report = overlay_diagnostics(
            config,
            env={
                "XDG_SESSION_TYPE": "wayland",
                "WAYLAND_DISPLAY": "wayland-0",
                "XDG_CURRENT_DESKTOP": "KDE",
            },
            run_command=lambda *_args, **_kwargs: completed,
        )

        self.assertFalse(report["available"])
        self.assertEqual("wayland", report["backend"])
        self.assertTrue(report["pygobject"])
        self.assertFalse(report["gtk4_layer_shell"])
        self.assertIn("Gtk4LayerShell", report["detail"])

    def test_overlay_diagnostics_rejects_unsupported_session(self) -> None:
        config = self._config()
        report = overlay_diagnostics(
            config,
            env={"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "GNOME"},
            run_command=lambda *_args, **_kwargs: self.fail("helper should not run"),
        )

        self.assertFalse(report["available"])
        self.assertFalse(report["supported_session"])
        self.assertIsNone(report["backend"])
        self.assertIn("KDE Plasma", report["detail"])

    def test_overlay_diagnostics_reports_disabled_overlay_on_unsupported_session(self) -> None:
        config = self._config(overlay_enabled=False)
        report = overlay_diagnostics(
            config,
            env={"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "GNOME"},
            run_command=lambda *_args, **_kwargs: self.fail("helper should not run"),
        )

        self.assertFalse(report["enabled"])
        self.assertFalse(report["available"])
        self.assertFalse(report["supported_session"])
        self.assertEqual("Overlay is disabled in configuration.", report["detail"])

    def test_overlay_diagnostics_handles_helper_failure(self) -> None:
        config = self._config()

        def failed_helper(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise OSError("system interpreter missing")

        report = overlay_diagnostics(
            config,
            env={"XDG_SESSION_TYPE": "x11", "XDG_CURRENT_DESKTOP": "KDE", "DISPLAY": ":0"},
            run_command=failed_helper,
        )

        self.assertFalse(report["available"])
        self.assertIn("system interpreter missing", report["detail"])

    def _run_spike(self, *, paste: bool, focus_changed: bool, verify_target: bool = True):
        from murmly.focus import WindowIdentity

        target = WindowIdentity(1, 10, "editor")
        current = WindowIdentity(2, 20, "browser") if focus_changed else target

        class Observer:
            supported = True
            detail = None

            def active_window(self):
                return Observer.window

        Observer.window = target
        config = MurmlyConfig(
            socket_path=Path("/tmp/murmly.sock"),
            config_path=Path("/tmp/config.toml"),
            verify_target=verify_target,
        )
        with (
            patch("murmly.cli.SoundDeviceRecorder") as recorder,
            patch("murmly.cli.FasterWhisperTranscriber") as transcriber,
            patch("murmly.cli.ClipboardPaster") as paster,
            patch("murmly.cli.create_focus_observer", return_value=Observer()),
            redirect_stdout(StringIO()),
        ):
            recorder.return_value.record_for_seconds.return_value = b"pcm"
            recorder.return_value.sample_rate_hz = 16_000

            def transcribe_while_focus_moves(*_args, **_kwargs):
                # Focus moves during transcription, after the target was recorded.
                Observer.window = current
                return "hello world"

            transcriber.return_value.transcribe_pcm16.side_effect = transcribe_while_focus_moves
            from murmly.cli import _run_spike

            _run_spike(config, 1.0, paste)
        return paster.return_value

    def test_spike_pastes_when_focus_is_unchanged(self) -> None:
        paster = self._run_spike(paste=True, focus_changed=False)

        paster.copy_and_paste.assert_called_once_with("hello world")
        paster.copy.assert_not_called()

    def test_spike_copies_without_pasting_when_focus_changed(self) -> None:
        paster = self._run_spike(paste=True, focus_changed=True)

        paster.copy.assert_called_once_with("hello world")
        paster.copy_and_paste.assert_not_called()

    def test_spike_with_verification_disabled_pastes_despite_focus_change(self) -> None:
        paster = self._run_spike(paste=True, focus_changed=True, verify_target=False)

        paster.copy_and_paste.assert_called_once_with("hello world")

    def test_spike_without_paste_flag_only_copies(self) -> None:
        paster = self._run_spike(paste=False, focus_changed=False)

        paster.copy.assert_called_once_with("hello world")
        paster.copy_and_paste.assert_not_called()

    class _Focus:
        def __init__(self, supported: bool, detail: str | None = None) -> None:
            self.supported = supported
            self.detail = detail

        def active_window(self):
            return None

    def test_delivery_diagnostics_reports_supported_and_enabled(self) -> None:
        report = delivery_diagnostics(self._config(), observer=self._Focus(True))

        self.assertTrue(report["verification_supported"])
        self.assertTrue(report["verification_enabled"])
        self.assertEqual(500, report["restore_delay_ms"])
        self.assertIn("supported and enabled", report["detail"])

    def test_delivery_diagnostics_reports_supported_but_disabled(self) -> None:
        config = MurmlyConfig(
            socket_path=Path("/tmp/murmly.sock"),
            config_path=Path("/tmp/config.toml"),
            verify_target=False,
        )

        report = delivery_diagnostics(config, observer=self._Focus(True))

        self.assertTrue(report["verification_supported"])
        self.assertFalse(report["verification_enabled"])
        self.assertIn("disabled in configuration", report["detail"])

    def test_delivery_diagnostics_reports_unverified_session(self) -> None:
        observer = self._Focus(False, "Delivery target verification requires an X11 session.")

        report = delivery_diagnostics(self._config(), observer=observer)

        self.assertFalse(report["verification_supported"])
        self.assertIn("X11 session", report["detail"])

    def test_doctor_includes_delivery_section(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            with (
                patch.object(FasterWhisperTranscriber, "resolve_runtime", return_value=("cpu", "int8")),
                patch("murmly.cli.choose_clipboard_copy_command", return_value=["xclip"]),
                patch(
                    "murmly.cli.select_paste_injection",
                    return_value=PasteInjection("xdotool", ("xdotool", "key", "ctrl+v")),
                ),
                redirect_stdout(StringIO()) as output,
            ):
                _run_doctor(config)

        report = json.loads(output.getvalue())
        self.assertIn("delivery", report)
        self.assertIn("verification_supported", report["delivery"])
        self.assertEqual(500, report["delivery"]["restore_delay_ms"])

    @staticmethod
    def _config(overlay_enabled: bool = True) -> MurmlyConfig:
        return MurmlyConfig(
            socket_path=Path("/tmp/murmly.sock"),
            config_path=Path("/tmp/config.toml"),
            overlay_enabled=overlay_enabled,
        )

    @staticmethod
    def _helper_result(report: dict[str, object], returncode: int = 0) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["/usr/bin/python3", "overlay_renderer.py", "--check"],
            returncode=returncode,
            stdout=json.dumps(report),
            stderr="",
        )

class StubService:
    """A stand-in for the installed systemd user unit."""

    def __init__(self, installed: bool = True, on_start=None) -> None:
        self.is_installed = installed
        self.starts = 0
        self._on_start = on_start

    def start(self) -> bool:
        self.starts += 1
        if self._on_start is not None:
            self._on_start()
        return True


class CountingSender:
    """Counts send_command attempts and replays scripted outcomes."""

    def __init__(self, outcomes) -> None:
        self._outcomes = list(outcomes)
        self.attempts = 0

    def __call__(self, _socket_path, command):
        self.attempts += 1
        outcome = self._outcomes.pop(0) if self._outcomes else self._outcomes
        if isinstance(outcome, Exception):
            raise outcome
        return {"ok": True, "state": "LISTENING", "command": command}


class ToggleRecoveryTests(unittest.TestCase):
    def _config(self, socket_path: Path) -> MurmlyConfig:
        return MurmlyConfig(socket_path=socket_path, config_path=Path("/tmp/config.toml"))

    def test_running_daemon_is_used_without_touching_the_service(self) -> None:
        from murmly.cli import send_command_with_recovery

        service = StubService()
        sender = CountingSender([None])
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(Path(temp_dir) / "murmly.sock")
            with patch("murmly.cli.send_command", sender):
                response = send_command_with_recovery(config, "toggle", service=service)

        self.assertTrue(response["ok"])
        self.assertEqual(1, sender.attempts)
        self.assertEqual(0, service.starts, "a healthy daemon must not be restarted")

    def test_installed_but_not_listening_starts_the_service_and_retries_once(self) -> None:
        from murmly.cli import send_command_with_recovery

        with tempfile.TemporaryDirectory() as temp_dir:
            socket_path = Path(temp_dir) / "murmly.sock"
            config = self._config(socket_path)

            def create_socket() -> None:
                socket_path.write_text("", encoding="utf-8")

            service = StubService(installed=True, on_start=create_socket)
            sender = CountingSender([FileNotFoundError("no socket"), None])

            with patch("murmly.cli.send_command", sender):
                response = send_command_with_recovery(
                    config, "toggle", service=service, sleep=lambda _s: None
                )

        self.assertTrue(response["ok"])
        self.assertEqual(1, service.starts)
        self.assertEqual(2, sender.attempts, "exactly one retry after the initial attempt")

    def test_not_installed_names_the_install_command(self) -> None:
        from murmly.cli import DaemonUnavailableError, send_command_with_recovery

        service = StubService(installed=False)
        sender = CountingSender([FileNotFoundError("no socket")])
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(Path(temp_dir) / "murmly.sock")
            with patch("murmly.cli.send_command", sender):
                with self.assertRaises(DaemonUnavailableError) as raised:
                    send_command_with_recovery(config, "toggle", service=service)

        self.assertIn("murmly install", str(raised.exception))
        self.assertEqual(0, service.starts)
        self.assertEqual(1, sender.attempts, "no retry when nothing can be started")

    def test_service_that_never_comes_up_fails_within_the_bound(self) -> None:
        from murmly.cli import DaemonUnavailableError, send_command_with_recovery

        service = StubService(installed=True)
        sender = CountingSender([FileNotFoundError("no socket")])
        ticks = iter([0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0])
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(Path(temp_dir) / "murmly.sock")
            with patch("murmly.cli.send_command", sender):
                with self.assertRaises(DaemonUnavailableError) as raised:
                    send_command_with_recovery(
                        config,
                        "toggle",
                        service=service,
                        sleep=lambda _s: None,
                        clock=lambda: next(ticks),
                        timeout=1.0,
                    )

        self.assertIn("did not start", str(raised.exception))
        self.assertEqual(1, service.starts)
        self.assertEqual(1, sender.attempts, "the command is not retried when the socket never appears")

    def test_daemon_that_starts_but_refuses_the_command_is_reported(self) -> None:
        from murmly.cli import DaemonUnavailableError, send_command_with_recovery

        with tempfile.TemporaryDirectory() as temp_dir:
            socket_path = Path(temp_dir) / "murmly.sock"
            config = self._config(socket_path)
            socket_path.write_text("", encoding="utf-8")
            service = StubService(installed=True)
            sender = CountingSender([ConnectionRefusedError("refused"), ConnectionRefusedError("refused")])

            with patch("murmly.cli.send_command", sender):
                with self.assertRaises(DaemonUnavailableError) as raised:
                    send_command_with_recovery(config, "toggle", service=service, sleep=lambda _s: None)

        self.assertIn("did not accept", str(raised.exception))
        self.assertEqual(2, sender.attempts)

    def test_client_command_reports_the_failure_without_a_traceback(self) -> None:
        from murmly.cli import _run_client_command

        service = StubService(installed=False)
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(Path(temp_dir) / "murmly.sock")
            with (
                patch("murmly.cli.send_command", CountingSender([FileNotFoundError("no socket")])),
                patch("murmly.cli.UserService", return_value=service),
                redirect_stderr(StringIO()) as errors,
            ):
                exit_code = _run_client_command(config, "toggle")

        self.assertEqual(1, exit_code)
        self.assertIn("murmly install", errors.getvalue())


class InstallCommandTests(unittest.TestCase):
    def test_rejects_an_unparseable_hotkey_before_touching_anything(self) -> None:
        from murmly.cli import _run_install

        with (
            patch("murmly.cli.Installer") as installer,
            redirect_stderr(StringIO()) as errors,
        ):
            exit_code = _run_install("Meta+Frobnicate")

        self.assertEqual(2, exit_code)
        self.assertIn("Frobnicate", errors.getvalue())
        installer.assert_not_called()

    def test_prints_outcome_messages_on_success(self) -> None:
        from murmly.cli import _run_install
        from murmly.installer import InstallOutcome

        outcome = InstallOutcome(
            entrypoint=Path("/bin/murmly"),
            hotkey=None,
            service_installed=True,
            hotkey_registered=True,
            already_bound=False,
            session_supported=True,
            session_verified=True,
            user_override=None,
            messages=("Registered Meta+X.", "Press it once to confirm."),
        )
        with (
            patch("murmly.cli.Installer") as installer,
            redirect_stdout(StringIO()) as output,
        ):
            installer.return_value.install.return_value = outcome
            exit_code = _run_install("Meta+X")

        self.assertEqual(0, exit_code)
        self.assertIn("Registered Meta+X.", output.getvalue())

    def test_conflict_exits_non_zero_naming_the_owner(self) -> None:
        from murmly.cli import _run_install
        from murmly.installer import HotkeyConflictError

        with (
            patch("murmly.cli.Installer") as installer,
            redirect_stderr(StringIO()) as errors,
        ):
            installer.return_value.install.side_effect = HotkeyConflictError("Meta+X is used by Klipper.")
            exit_code = _run_install("Meta+X")

        self.assertEqual(1, exit_code)
        self.assertIn("Klipper", errors.getvalue())

    def test_unconfirmed_binding_reports_next_login_and_fails(self) -> None:
        from murmly.cli import _run_install
        from murmly.installer import HotkeyNotConfirmedError

        with (
            patch("murmly.cli.Installer") as installer,
            redirect_stderr(StringIO()) as errors,
        ):
            installer.return_value.install.side_effect = HotkeyNotConfirmedError("timed out")
            exit_code = _run_install("Meta+X")

        self.assertEqual(1, exit_code)
        self.assertIn("next login", errors.getvalue())
        self.assertIn("not active in this session", errors.getvalue())


class UninstallCommandTests(unittest.TestCase):
    def test_prints_what_was_removed(self) -> None:
        from murmly.cli import _run_uninstall
        from murmly.installer import InstallOutcome

        outcome = InstallOutcome(
            entrypoint=None,
            hotkey=None,
            service_installed=False,
            hotkey_registered=False,
            already_bound=False,
            session_supported=True,
            session_verified=True,
            user_override=None,
            messages=("Removed the Murmly service.", "Released the Murmly hotkey."),
        )
        with (
            patch("murmly.cli.Installer") as installer,
            redirect_stdout(StringIO()) as output,
        ):
            installer.return_value.uninstall.return_value = outcome
            exit_code = _run_uninstall()

        self.assertEqual(0, exit_code)
        self.assertIn("Removed the Murmly service.", output.getvalue())

    def test_reports_a_failure_without_a_traceback(self) -> None:
        from murmly.cli import _run_uninstall
        from murmly.installer import InstallError

        with (
            patch("murmly.cli.Installer") as installer,
            redirect_stderr(StringIO()) as errors,
        ):
            installer.return_value.uninstall.side_effect = InstallError("systemctl unavailable")
            exit_code = _run_uninstall()

        self.assertEqual(1, exit_code)
        self.assertIn("systemctl unavailable", errors.getvalue())


class InstallationDiagnosticsTests(unittest.TestCase):
    def test_reports_the_installer_status(self) -> None:
        from murmly.cli import installation_diagnostics

        class Stub:
            def status(self):
                return {"installed": True, "hotkey": "Meta+X", "hotkey_held": True}

        report = installation_diagnostics(Stub())

        self.assertTrue(report["installed"])
        self.assertEqual("Meta+X", report["hotkey"])

    def test_never_raises_when_the_desktop_is_unreachable(self) -> None:
        from murmly.cli import installation_diagnostics

        class Broken:
            def status(self):
                raise RuntimeError("bus is down")

        report = installation_diagnostics(Broken())

        self.assertFalse(report["installed"])
        self.assertIn("bus is down", report["detail"])

    def test_doctor_includes_the_installation_section(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            with (
                patch.object(FasterWhisperTranscriber, "resolve_runtime", return_value=("cpu", "int8")),
                patch("murmly.cli.choose_clipboard_copy_command", return_value=["xclip"]),
                patch(
                    "murmly.cli.select_paste_injection",
                    return_value=PasteInjection("xdotool", ("xdotool", "key", "ctrl+v")),
                ),
                patch(
                    "murmly.cli.installation_diagnostics",
                    return_value={"installed": False, "detail": "not installed"},
                ),
                redirect_stdout(StringIO()) as output,
            ):
                _run_doctor(config)

        report = json.loads(output.getvalue())
        self.assertIn("installation", report)
        self.assertFalse(report["installation"]["installed"])


class LiveTranscriptionDiagnosticsTests(unittest.TestCase):
    def _config(self, **overrides: object) -> MurmlyConfig:
        temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp_dir, ignore_errors=True)
        return MurmlyConfig(
            socket_path=Path(temp_dir) / "murmly.sock",
            config_path=Path(temp_dir) / "config.toml",
            **overrides,
        )

    def test_defaults_report_both_features_off_without_loading_anything(self) -> None:
        with patch("murmly.cli.SilenceDetector") as detector, patch(
            "murmly.cli.FasterWhisperTranscriber"
        ) as transcriber:
            report = live_transcription_diagnostics(self._config())

        self.assertFalse(report["live_transcribe"])
        self.assertEqual("off", report["auto_transcribe"])
        self.assertIsNone(report["partial_pass_ceiling_ms"])
        self.assertEqual("Auto-transcribe is disabled.", report["silence_detection_detail"])
        # Neither the VAD nor the model may be loaded for a disabled feature.
        detector.assert_not_called()
        transcriber.assert_not_called()

    def test_a_rejected_mode_is_surfaced(self) -> None:
        config = self._config(auto_transcribe="off", auto_transcribe_rejected_value="whenever")

        with patch("murmly.cli.SilenceDetector"):
            report = live_transcription_diagnostics(config)

        self.assertEqual("whenever", report["auto_transcribe_rejected_value"])

    def test_silence_is_checked_against_the_negotiated_rate(self) -> None:
        config = self._config(auto_transcribe="stop", sample_rate_hz=16_000)

        with (
            patch("murmly.cli.negotiated_capture_rate", return_value=(44_100, None)),
            patch("murmly.cli.SilenceDetector") as detector,
        ):
            detector.return_value.available = False
            detector.return_value.unavailable_reason = "capture rate 44100 Hz is not supported"
            report = live_transcription_diagnostics(config)

        self.assertEqual(44_100, report["negotiated_sample_rate_hz"])
        self.assertFalse(report["silence_detection_available"])
        self.assertEqual(44_100, detector.call_args.args[0])

    def test_keeps_pace_compares_the_ceiling_against_the_interval(self) -> None:
        config = self._config(live_transcribe=True, live_interval_ms=1_000)

        for measured, expected in ((300, True), (12_000, False)):
            with self.subTest(measured=measured):
                with (
                    patch("murmly.cli.SilenceDetector"),
                    patch("murmly.cli.measure_partial_pass_ms", return_value=(measured, None)),
                ):
                    report = live_transcription_diagnostics(config)

                self.assertEqual(measured, report["partial_pass_ceiling_ms"])
                self.assertEqual(expected, report["partial_pass_keeps_pace"])

    def test_a_failed_measurement_reports_detail_and_no_verdict(self) -> None:
        config = self._config(live_transcribe=True)

        with (
            patch("murmly.cli.SilenceDetector"),
            patch("murmly.cli.measure_partial_pass_ms", return_value=(None, "model exploded")),
        ):
            report = live_transcription_diagnostics(config)

        self.assertIsNone(report["partial_pass_ceiling_ms"])
        self.assertEqual("model exploded", report["partial_pass_detail"])
        self.assertNotIn("partial_pass_keeps_pace", report)

    def test_measurement_clip_is_never_digital_silence(self) -> None:
        """A silent clip short-circuits before the model runs.

        The measurement would then report None forever without anything failing.
        """
        clip = _measurement_clip(16_000, 15)

        self.assertTrue(any(clip))
        self.assertEqual(16_000 * 15 * 2, len(clip))

    def test_measurement_refuses_a_transcriber_with_the_vad_filter_on(self) -> None:
        """With the filter on, the synthetic clip is discarded as non-speech.

        The pass would then time no decoding and report a fast ceiling on a
        machine that cannot keep pace at all.
        """
        config = self._config(live_transcribe=True, vad_filter=True)
        transcriber = FasterWhisperTranscriber(config)

        measured, detail = measure_partial_pass_ms(config, transcriber)

        self.assertIsNone(measured)
        self.assertIn("voice activity filter", detail)

    def test_measurement_uses_a_vad_free_config_when_it_builds_its_own(self) -> None:
        config = self._config(live_transcribe=True, vad_filter=True)

        with patch("murmly.cli.FasterWhisperTranscriber") as factory:
            factory.return_value.partials_available = True
            factory.return_value.transcribe_partial.return_value = "text"
            measured, detail = measure_partial_pass_ms(config)

        self.assertIsNone(detail)
        self.assertIsInstance(measured, int)
        self.assertFalse(factory.call_args.args[0].vad_filter)


class UnhandledFailureTests(unittest.TestCase):
    """No Murmly command terminates with an unhandled error."""

    def test_a_daemon_that_answers_nothing_is_reported_and_exits_non_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            socket_path = Path(temp_dir) / "murmly.sock"
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                f'[daemon]\nsocket_path = "{socket_path}"\n', encoding="utf-8"
            )
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(server.close)
            server.bind(str(socket_path))
            server.listen()
            stop = threading.Event()

            def accept_and_close() -> None:
                server.settimeout(0.2)
                while not stop.is_set():
                    try:
                        connection, _address = server.accept()
                    except socket.timeout:
                        continue
                    with connection:
                        connection.settimeout(1)
                        try:
                            connection.recv(4_096)
                        except OSError:
                            pass

            thread = threading.Thread(target=accept_and_close, daemon=True)
            thread.start()
            try:
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as errors:
                    exit_code = main(["--config", str(config_path), "status"])
            finally:
                stop.set()
                thread.join(timeout=3)

        self.assertEqual(1, exit_code)
        self.assertIn("closed the connection before responding", errors.getvalue())

    def test_a_configuration_that_cannot_be_read_is_reported_and_exits_non_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text("[daemon\nsocket_path = \n", encoding="utf-8")

            with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as errors:
                exit_code = main(["--config", str(config_path), "doctor"])

        self.assertEqual(1, exit_code)
        self.assertIn(str(config_path), errors.getvalue())
        self.assertIn("Unable to read the configuration", errors.getvalue())

    def test_a_daemon_that_refuses_to_start_is_reported_and_exits_non_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "shared"
            directory.mkdir()
            directory.chmod(0o777)
            socket_path = directory / "murmly.sock"
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                f'[daemon]\nsocket_path = "{socket_path}"\n\n[overlay]\nenabled = false\n',
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as errors:
                exit_code = main(["--config", str(config_path), "daemon"])

        self.assertEqual(1, exit_code)
        self.assertIn(str(socket_path), errors.getvalue())
        self.assertIn("XDG_RUNTIME_DIR", errors.getvalue())

    def test_the_top_level_guard_reports_an_unexpected_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text("", encoding="utf-8")

            with (
                patch("murmly.cli._run_doctor", side_effect=RuntimeError("probe exploded")),
                redirect_stdout(StringIO()),
                redirect_stderr(StringIO()) as errors,
            ):
                exit_code = main(["--config", str(config_path), "doctor"])

        reported = errors.getvalue()
        self.assertEqual(1, exit_code)
        self.assertIn("probe exploded", reported)
        # One line for a person at a terminal. The frames go to the daemon only.
        self.assertNotIn("Traceback", reported)

    def test_the_daemon_guard_keeps_the_traceback_its_journal_needs(self) -> None:
        # The daemon runs unattended, so the one line the guard prints is all
        # that would survive of a crash nothing anticipated. Whoever reads the
        # journal afterwards has nothing else to work from.
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text("", encoding="utf-8")

            with (
                patch("murmly.cli._run_daemon", side_effect=RuntimeError("worker exploded")),
                redirect_stdout(StringIO()),
                redirect_stderr(StringIO()) as errors,
            ):
                exit_code = main(["--config", str(config_path), "daemon"])

        reported = errors.getvalue()
        self.assertEqual(1, exit_code)
        self.assertIn("murmly: unexpected failure: worker exploded", reported)
        self.assertIn("Traceback (most recent call last)", reported)

    def test_argument_errors_still_reach_the_parser(self) -> None:
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            main(["not-a-command"])


class DoctorCompletenessTests(unittest.TestCase):
    """`murmly doctor` reports every section it can and explains the ones it cannot."""

    SECTIONS = (
        "config_path",
        "socket_path",
        "command_socket",
        "session",
        "clipboard_command",
        "paste_injection",
        "model_profile",
        "model_name",
        "device",
        "compute_type",
        "runtime_device",
        "runtime_compute_type",
        "beam_size",
        "vad_filter",
        "live_transcription",
        "delivery",
        "overlay",
        "installation",
    )

    def _report(self, config: MurmlyConfig, resolve_runtime: object) -> dict[str, object]:
        with (
            patch.object(FasterWhisperTranscriber, "resolve_runtime", resolve_runtime),
            patch("murmly.cli.choose_clipboard_copy_command", return_value=["xclip"]),
            patch(
                "murmly.cli.select_paste_injection",
                return_value=PasteInjection("xdotool", ("xdotool", "key", "ctrl+v")),
            ),
            redirect_stdout(StringIO()) as output,
        ):
            _run_doctor(config)
        return json.loads(output.getvalue())

    def test_the_report_is_complete_when_the_configured_runtime_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
                device="cuda",
            )
            report = self._report(
                config,
                Mock(side_effect=RuntimeError("the cuda extra is not installed")),
            )

        self.assertEqual("cuda", report["device"])
        self.assertIsNone(report["runtime_device"])
        self.assertIsNone(report["runtime_compute_type"])
        self.assertIn("the cuda extra is not installed", report["runtime_detail"])
        for section in self.SECTIONS:
            self.assertIn(section, report)

    def test_the_success_shape_of_every_section_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            report = self._report(config, Mock(return_value=("cuda", "float16")))

        self.assertEqual(set(self.SECTIONS), set(report))
        self.assertEqual("cuda", report["runtime_device"])
        self.assertEqual("float16", report["runtime_compute_type"])
        self.assertIn(report["session"], {"wayland", "x11"})
        self.assertEqual(
            {"available", "method", "command", "confirms_delivery"},
            set(report["paste_injection"]),
        )
        self.assertEqual(
            {
                "verification_supported",
                "verification_enabled",
                "restore_clipboard",
                "restore_delay_ms",
                "detail",
            },
            set(report["delivery"]),
        )
        self.assertEqual(
            {"path", "path_private", "peer_identity_supported"},
            set(report["command_socket"]),
        )
        self.assertTrue(report["command_socket"]["path_private"])

    def test_diagnostics_report_a_socket_path_the_daemon_would_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "shared"
            directory.mkdir()
            directory.chmod(0o777)
            config = MurmlyConfig(
                socket_path=directory / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
                overlay_enabled=False,
            )
            # The same path the daemon refuses, so the two cannot drift apart.
            with self.assertRaises(DaemonStartupError):
                MurmlyDaemon(config)

            report = self._report(config, Mock(return_value=("cpu", "int8")))

        self.assertFalse(report["command_socket"]["path_private"])
        self.assertIn(str(directory), report["command_socket"]["detail"])
        for section in self.SECTIONS:
            self.assertIn(section, report)

    def test_diagnostics_explain_a_platform_that_cannot_report_peer_identity(self) -> None:
        # The section exists to say what is protecting the socket. Where the
        # identity check is unavailable, file permissions are the whole answer
        # and the report has to say so.
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )

            with patch("murmly.cli.peer_identity_supported", return_value=False):
                report = command_socket_diagnostics(config)

        self.assertFalse(report["peer_identity_supported"])
        self.assertIn("file permissions alone", report["peer_identity_detail"])

    def test_a_private_socket_path_is_reported_as_private(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )

            report = command_socket_diagnostics(config)

        self.assertTrue(report["path_private"])
        self.assertNotIn("detail", report)
        self.assertTrue(report["peer_identity_supported"])

    def test_an_unreadable_focus_probe_does_not_abandon_the_delivery_section(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )

            with patch(
                "murmly.cli.create_focus_observer",
                side_effect=RuntimeError("the display went away"),
            ):
                report = delivery_diagnostics(config)

        self.assertFalse(report["verification_supported"])
        self.assertIn("the display went away", report["detail"])
